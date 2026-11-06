#!/usr/bin/env python3
"""CI boot test of a lock update (design §6 "Same-MC build/plugin updates", P11): start the locked server jar
with the locked plugins and a throwaway world on the runner until "Done", then a Minecraft status ping, then
`stop`. Runs only in CI: the config repo's `update` job, before an update PR is opened, and the `boot-test`
job of a Minecraft upgrade PR (P12, `mcctl upgrade --minecraft-version`). Running the jar is fine there
(scripts/config_keys.py still never runs it, and nothing here runs on a developer's machine).

    boot_test.py --lock-dir CONFIG_DIR [--java java] [--timeout 900]

Every download is checked against the lock's sha256 (fetch_server_jar/fetch_plugins verify_locked). The world
is generated in a temporary directory; the server binds 127.0.0.1 on a free port, online-mode off (no Mojang
session needed), no RCON or query. Fails (exit 1) if the server doesn't reach "Done" in time, a locked plugin
doesn't enable ("Enabling <name>"), the log shows "Could not load" / "Error occurred while enabling", the
status ping fails, or the JVM doesn't stop cleanly. The server.properties overlay keys are checked statically
against the pinned jar (tests/test_overlays.py); the boot test uses the jar's defaults + the settings above.
"""

from __future__ import annotations

import argparse
import queue
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path

import fetch_plugins
import fetch_server_jar

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # the product checkout: mcctl's stdlib ping
from mcctl import mcping

BAD = re.compile(
    r"Could not load '|Error occurred while enabling|Exception in server tick loop"
    r"|Encountered an unexpected exception"
)
DONE = re.compile(r"\bDone \(\d+(?:[.,]\d+)?s\)!")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def prepare(lock_dir: Path, server_dir: Path, port: int) -> list[str]:
    """Jar + plugins (sha256-verified) + eula + minimal server.properties; returns the plugin names."""
    jar = fetch_server_jar.verify_locked(lock_dir / "server.lock.yml")
    (server_dir / "server.jar").write_bytes(jar)
    plugins = server_dir / "plugins"
    plugins.mkdir()
    names = []
    lock = fetch_plugins.load_yaml(lock_dir / "plugins.lock.yml")
    for name, data in fetch_plugins.verify_locked(lock_dir / "plugins.lock.yml").items():
        (plugins / name).write_bytes(data)
    names = [p["name"] for p in lock.get("plugins") or []]
    # P13: the product's own plugin boots with every lock update too (its sha256 is the product's pin)
    own = Path(__file__).resolve().parent.parent / "plugin" / "mc-telemetry" / "dist" / "mc-telemetry.jar"
    if own.is_file():
        (plugins / "mc-telemetry.jar").write_bytes(own.read_bytes())
        names.append("mc-telemetry")
    (server_dir / "eula.txt").write_text("# CI boot test of a lock update (throwaway world)\neula=true\n")
    (server_dir / "server.properties").write_text(
        f"server-ip=127.0.0.1\nserver-port={port}\nonline-mode=false\nenable-rcon=false\nenable-query=false\n"
        "level-name=boot-test\nview-distance=4\nsimulation-distance=4\nspawn-protection=0\n"
    )
    return names


def run(
    server_dir: Path,
    java: str,
    port: int,
    plugins: list[str],
    timeout: float,
    during: Callable[[], list[str]] | None = None,
    heap: str = "1G",
) -> list[str]:
    """Start, wait for Done, ping, stop. Returns problems (empty = passed). The log is scanned for BAD lines
    the whole time, through the ping and the stop too (p11-review), not just until "Done". `during` runs
    after a successful ping, before the stop (v1.2 scripts/seed_check.py: its /locate claims over RCON), and
    returns more problems."""
    proc = subprocess.Popen(
        [java, f"-Xms{heap}", f"-Xmx{heap}", "-jar", "server.jar", "--nogui"],
        cwd=server_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    lines: queue.Queue[str] = queue.Queue()
    log: list[str] = []
    reader = threading.Thread(target=lambda: [lines.put(ln) for ln in proc.stdout or []], daemon=True)
    reader.start()
    problems: list[str] = []

    def take(line: str) -> None:
        line = line.rstrip()
        log.append(line)
        print(line, flush=True)
        if BAD.search(line):
            problems.append(f"server log: {line[:200]}")

    def drain() -> None:
        while True:
            try:
                take(lines.get_nowait())
            except queue.Empty:
                return

    deadline = time.monotonic() + timeout
    done = False
    while time.monotonic() < deadline and proc.poll() is None:
        try:
            line = lines.get(timeout=1)
        except queue.Empty:
            continue
        take(line)
        if DONE.search(line):
            done = True
            break
    if not done:
        problems.append(
            f"the server didn't finish starting within {int(timeout)} s (exit code {proc.poll()})"
        )
    else:
        try:
            status = mcping.status("127.0.0.1", port, host="127.0.0.1")
            print(f"status ping: {status.get('version', {}).get('name')}")
        except Exception as e:  # any failure of the ping is a failed boot test
            problems.append(f"status ping failed: {type(e).__name__}: {e}")
        else:
            if during is not None:
                problems += during()
        drain()
        text = "\n".join(log)
        for name in plugins:
            if not re.search(rf"Enabling {re.escape(name)}\b", text):
                problems.append(f"plugin {name} wasn't enabled")
    if proc.poll() is None:
        try:
            if proc.stdin:
                proc.stdin.write("stop\n")
                proc.stdin.flush()
            proc.wait(timeout=120)
        except (OSError, subprocess.TimeoutExpired):
            proc.kill()
            problems.append("the server didn't stop within 120 s of `stop`")
    reader.join(timeout=10)  # the rest of the log, up to the JVM's exit
    drain()
    return problems


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--lock-dir", type=Path, required=True)
    p.add_argument("--java", default="java")
    p.add_argument("--timeout", type=float, default=900)
    args = p.parse_args(argv)
    with tempfile.TemporaryDirectory(prefix="mc-boot-test-") as d:
        server_dir = Path(d)
        port = free_port()
        plugins = prepare(args.lock_dir, server_dir, port)
        problems = run(server_dir, args.java, port, plugins, args.timeout)
    for problem in problems:
        print(f"BOOT TEST FAILED: {problem}", file=sys.stderr)
    if not problems:
        print("boot test passed: Done, status ping answered, every locked plugin enabled, clean stop")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
