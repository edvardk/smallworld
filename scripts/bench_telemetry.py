#!/usr/bin/env python3
"""Tail-latency A/B of the product's in-game plugins (design §8 P13 "TAIL-LATENCY RULE"): tick-time
p99/p99.9 with an in-house plugin on vs off, on the real pinned server jar under a synthetic load.

    bench_telemetry.py --jdk JAVA_HOME [--plugin mc-telemetry|mc-brand] [--server-jar FILE] [--rounds 2]
                       [--seconds 120] [--warmup 45] [--entities 640] [--report-only] [--out results.json]

--plugin mc-brand (P17): arm B runs the branding plugin with its HUD on by default, the tab list on and the
shortest refresh (2 s), the drink on; its own task time comes from `/mcbrand status` (max per run).
With no players connected its task finds nobody to update: the arm measures the plugin's idle cost (the
repeating task, its listeners), not the per-player sidebar. --report-only: print the gate's verdict, never
fail on it.

Both arms run the bench-only probe plugin plugin/tickprobe (every tick's duration into a preallocated array:
identical cost in both arms) on the same pre-generated flat world with the same entities (cows in force-loaded
chunks, so they tick without players); arm B adds mc-telemetry (snapshots every 10 s to a temporary directory,
its default per-tick budget). Arms alternate ABBA... so slow drift of the machine hits both. The probe's ticks
are pooled per arm. Gate (generous: CI runners are noisy, only clear regressions fail):
    p99(B) <= 1.5 * p99(A) + 2 ms   and   mean(B) <= 1.3 * mean(A) + 1 ms.
p99.9 and max are reported, not gated (a handful of ticks per run). Also reported: the plugin's own
main-thread time per tick from its snapshots (self_p99_us/self_max_us). Runs the jar (CI and developer
machines only; never the live server); online-mode off, bound to 127.0.0.1, RCON on loopback with a random
password.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shutil
import socket
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import build_plugin
from lockfile import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import mc_rcon

PROBE_DIR = REPO_ROOT / "plugin" / "tickprobe"
TELEMETRY_JAR = REPO_ROOT / "plugin" / "mc-telemetry" / "dist" / "mc-telemetry.jar"
BRAND_JAR = REPO_ROOT / "plugin" / "mc-brand" / "dist" / "mc-brand.jar"
BRAND_CONFIG = """name: Bench
colors: {text: "#F2F5F7", accent: "#B03A5B", secondary: "#7B1E3A", subtle: "#D9E8EE"}
rules: [Be kind.]
hud: {enabled: true, default_on: true, interval_seconds: 2, max_players: 8, tab_list: true}
drink: {enabled: true, cooldown_seconds: 30, nausea_seconds: 10}
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def build_probe(jdk: Path, server: bytes, out: Path) -> None:
    sources = sorted((PROBE_DIR / "src" / "main" / "java").rglob("*.java"))
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "lib").mkdir()
        (tmp / "classes").mkdir()
        cp = build_plugin.classpath(server, build_plugin.load_build()["classpath"], tmp / "lib")
        subprocess.run(
            [
                str(jdk / "bin" / "javac"),
                "--release",
                "25",
                "-proc:none",
                "-nowarn",
                "-d",
                str(tmp / "classes"),
                "-cp",
                os.pathsep.join(map(str, cp)),
                *map(str, sources),
            ],
            check=True,
            env=build_plugin._env(),
        )
        out.write_bytes(build_plugin.jar_bytes(tmp / "classes", PROBE_DIR / "src" / "main" / "resources"))


# The JVM flags of both telemetry arms (bench_gc.py passes the product's G1/ZGC sets instead).
DEFAULT_JVM = ["-Xms2G", "-Xmx2G", "-XX:+UseG1GC", "-XX:+AlwaysPreTouch", "-Xlog:gc:file=gc.log:time"]


class Server:
    def __init__(
        self, jdk: Path, root: Path, jvm: list[str] | None = None, prefix: list[str] | None = None
    ) -> None:
        self.jdk, self.root = jdk, root
        self.jvm = DEFAULT_JVM if jvm is None else jvm
        self.prefix = prefix or []  # e.g. taskset -c 1-3 (bench_gc.py's CPU split)
        self.port, self.rcon_port = free_port(), free_port()
        self.password = secrets.token_hex(16)
        self.proc: subprocess.Popen[str] | None = None

    def configure(self) -> None:
        (self.root / "eula.txt").write_text("eula=true\n")
        (self.root / "server.properties").write_text(
            f"server-ip=127.0.0.1\nserver-port={self.port}\nonline-mode=false\nenable-rcon=true\n"
            f"rcon.port={self.rcon_port}\nrcon.password={self.password}\nlevel-name=bench\n"
            "level-type=minecraft\\:flat\ngenerate-structures=false\nview-distance=6\nsimulation-distance=6\n"
            "spawn-protection=0\nmax-players=10\n"
        )

    def start(self, timeout: float = 600) -> None:
        self.configure()
        log = (self.root / "bench.log").open("w")
        self.proc = subprocess.Popen(
            [
                *self.prefix,
                str(self.jdk / "bin" / "java"),
                *self.jvm,
                "-jar",
                "server.jar",
                "--nogui",
            ],
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
        end = time.time() + timeout
        while time.time() < end:
            if "Done (" in (self.root / "bench.log").read_text(errors="replace"):
                return
            if self.proc.poll() is not None:
                raise SystemExit(f"the server exited during start-up (see {self.root / 'bench.log'})")
            time.sleep(1)
        raise SystemExit("the server didn't start in time")

    def cmd(self, command: str) -> str:
        with mc_rcon.Rcon("127.0.0.1", self.rcon_port, self.password, timeout=60) as r:
            return r.command(command)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.cmd("stop")
            except (OSError, mc_rcon.RconError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=120)
            except subprocess.TimeoutExpired:
                self.proc.kill()


def percentile(sorted_vals: list[float], q: float) -> float:
    if not sorted_vals:
        return float("nan")
    k = max(0, min(len(sorted_vals) - 1, round(q * len(sorted_vals) + 0.5) - 1))
    return sorted_vals[k]


def stats(ms: list[float]) -> dict[str, float]:
    s = sorted(ms)
    return {
        "ticks": len(s),
        "mean": round(statistics.fmean(s), 3) if s else float("nan"),
        "p50": round(percentile(s, 0.50), 3),
        "p95": round(percentile(s, 0.95), 3),
        "p99": round(percentile(s, 0.99), 3),
        "p999": round(percentile(s, 0.999), 3),
        "max": round(s[-1], 3) if s else float("nan"),
    }


def prepare(jdk: Path, server_jar: bytes, base: Path, probe: Path, entities: int) -> Path:
    """A server dir with the jar's patched files, a flat world, forced chunks and the entities, saved."""
    prep = base / "prep"
    (prep / "plugins").mkdir(parents=True)
    (prep / "server.jar").write_bytes(server_jar)
    shutil.copy(probe, prep / "plugins" / "tickprobe.jar")
    srv = Server(jdk, prep)
    srv.start()
    try:
        with mc_rcon.Rcon("127.0.0.1", srv.rcon_port, srv.password, timeout=60) as r:
            r.command("forceload add -64 -64 63 63")
            per = max(1, entities // 64)
            for i in range(64):
                x, z = (i % 8) * 16 - 64 + 8, (i // 8) * 16 - 64 + 8
                for _ in range(per):
                    r.command(f"summon minecraft:cow {x} -60 {z}")
            r.command("save-all flush")
    finally:
        srv.stop()
    return prep


def run_arm(
    jdk: Path,
    prep: Path,
    base: Path,
    name: str,
    on: bool,
    warmup: int,
    seconds: int,
    plugin: str = "mc-telemetry",
) -> dict:
    root = base / name
    shutil.copytree(prep, root)
    (root / "bench.log").unlink(missing_ok=True)
    snaps = root / "snapshots"
    telemetry = on and plugin == "mc-telemetry"
    brand = on and plugin == "mc-brand"
    if brand:
        shutil.copy(BRAND_JAR, root / "plugins" / "mc-brand.jar")
        (root / "plugins" / "mc-brand").mkdir(exist_ok=True)
        (root / "plugins" / "mc-brand" / "config.yml").write_text(BRAND_CONFIG)
    if telemetry:
        shutil.copy(TELEMETRY_JAR, root / "plugins" / "mc-telemetry.jar")
        snaps.mkdir()
        (root / "plugins" / "mc-telemetry").mkdir(exist_ok=True)
        (root / "plugins" / "mc-telemetry" / "config.yml").write_text(
            f"snapshot-dir: {snaps}\nverdict-dir: {root / 'verdicts'}\ninterval-seconds: 10\n"
        )
    srv = Server(jdk, root)
    srv.start()
    try:
        time.sleep(warmup)
        srv.cmd("tickprobe reset")
        time.sleep(seconds)
        out = root / "ticks.txt"
        srv.cmd(f"tickprobe dump {out}")
        ticks = [line.split() for line in out.read_text().splitlines() if line.strip()]
        ms = [int(t[0]) / 1000.0 for t in ticks]
        slow = [(int(t[1]), int(t[0]) / 1000.0) for t in ticks if int(t[0]) >= 20000]
        selfs = []
        for p in sorted(snaps.glob("snap-*.json")) if telemetry else []:
            with_srv = json.loads(p.read_text())["srv"]
            selfs.append((with_srv.get("self_p99_us"), with_srv.get("self_max_us")))
        if brand:  # "... hud task runs=N last=Xus max=Yus ..."
            status = srv.cmd("mcbrand status")
            m = re.search(r"last=(\d+)us max=(\d+)us", status)
            if not m:
                raise SystemExit(f"mc-brand didn't answer /mcbrand status: {status!r}")
            selfs.append((int(m.group(1)), int(m.group(2))))
    finally:
        srv.stop()
    shutil.rmtree(root / "bench", ignore_errors=True)
    gc = [
        ln.strip()
        for ln in (root / "gc.log").read_text(errors="replace").splitlines()
        if "Pause" in ln and ln.rstrip().endswith("ms") and float(ln.rsplit(" ", 1)[-1][:-2]) >= 20.0
    ]
    snap_ts = sorted(json.loads(p.read_text())["ts"] for p in snaps.glob("snap-*.json")) if telemetry else []
    return {
        "arm": "B" if on else "A",
        "ms": ms,
        "self": selfs,
        "slow": slow,
        "snap_ts": snap_ts,
        "gc": gc,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--jdk", required=True, help="JAVA_HOME of a JDK 25 (javac for the probe, java for the server)"
    )
    ap.add_argument("--server-jar", help="the pinned server jar (default: download it, sha256-checked)")
    ap.add_argument("--rounds", type=int, default=2, help="ABBA rounds (default 2: A B B A)")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--warmup", type=int, default=45)
    ap.add_argument("--entities", type=int, default=640)
    ap.add_argument("--out", default="")
    ap.add_argument("--plugin", choices=["mc-telemetry", "mc-brand"], default="mc-telemetry")
    ap.add_argument("--report-only", action="store_true", help="never fail on the gate (report it)")
    args = ap.parse_args(argv)
    jdk = Path(args.jdk)
    cache = Path(tempfile.gettempdir()) / "bench-telemetry-cache"
    server_jar = build_plugin.server_jar(args.server_jar, cache)
    base = Path(tempfile.mkdtemp(prefix="bench-telemetry-"))
    try:
        probe = base / "tickprobe.jar"
        build_probe(jdk, server_jar, probe)
        prep = prepare(jdk, server_jar, base, probe, args.entities)
        order = []
        for r in range(args.rounds):
            order += [False, True] if r % 2 == 0 else [True, False]
        runs = [
            run_arm(jdk, prep, base, f"run{i}", t, args.warmup, args.seconds, args.plugin)
            for i, t in enumerate(order)
        ]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    a = [x for r in runs if r["arm"] == "A" for x in r["ms"]]
    b = [x for r in runs if r["arm"] == "B" for x in r["ms"]]
    sa, sb = stats(a), stats(b)
    selfs = [s for r in runs for s in r["self"] if s[0] is not None]
    result = {
        "A_off": sa,
        "B_on": sb,
        "rounds": [
            {
                "arm": r["arm"],
                **stats(r["ms"]),
                "slow_ticks": r["slow"],
                "snapshots": r["snap_ts"],
                "gc_pauses_over_20ms": r["gc"],
            }
            for r in runs
        ],
        "plugin_self_p99_us_max": max((s[0] for s in selfs), default=None),
        "plugin_self_max_us_max": max((s[1] for s in selfs), default=None),
        "plugin": args.plugin,
        "entities": args.entities,
        "seconds": args.seconds,
        "cpus": os.cpu_count(),
    }
    ok_p99 = sb["p99"] <= 1.5 * sa["p99"] + 2.0
    ok_mean = sb["mean"] <= 1.3 * sa["mean"] + 1.0
    result["pass"] = ok_p99 and ok_mean
    table = [
        "| arm | ticks | mean | p50 | p95 | p99 | p99.9 | max |",
        "|---|---|---|---|---|---|---|---|",
        *(
            f"| {n} | {s['ticks']} | {s['mean']} | {s['p50']} | {s['p95']} | {s['p99']} | {s['p999']} "
            f"| {s['max']} |"
            for n, s in ((f"A: {args.plugin} off", sa), (f"B: {args.plugin} on", sb))
        ),
        "",
        (
            f"plugin main-thread cost: p99 per tick up to {result['plugin_self_p99_us_max']} us, worst tick "
            if args.plugin == "mc-telemetry"
            else f"plugin task: last run up to {result['plugin_self_p99_us_max']} us, worst run "
        )
        + f"{result['plugin_self_max_us_max']} us; gate p99(B) <= 1.5 p99(A) + 2 ms: "
        + f"{'pass' if ok_p99 else 'FAIL'}, mean(B) <= 1.3 mean(A) + 1 ms: {'pass' if ok_mean else 'FAIL'}"
        + (" (report only)" if args.report_only else ""),
    ]
    print("\n".join(table))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(f"### {args.plugin} tail-latency A/B (ms per tick)\n\n" + "\n".join(table) + "\n")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    return 0 if result["pass"] or args.report_only else 1


if __name__ == "__main__":
    sys.exit(main())
