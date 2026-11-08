"""v1.4: `smallworld-agent cli` (rust/smallworld-agent) behaves exactly like `mc_agent.py cli`.

Differential and black-box: every scenario runs `scripts/mc_agent.py cli ...` and the Rust binary for this machine
(rust/dist/smallworld-agent-<target>; SMALLWORLD_AGENT_BIN points at another build, e.g. `cargo build`'s) in the
same fresh fake box, and compares what each did:

- stdout lines and the exit status;
- the audit entries (journald's native protocol, captured on the socket at /run/systemd/journal/socket);
- the RCON commands a fake server received (tellraw/title JSON byte for byte);
- the systemctl/journalctl argv (fake binaries first on PATH);
- the files left in /var/lib/minecraft, /var/lib/mc-snapshots and the agent's state dir (JSON compared
  semantically, with timestamps and job ids replaced; modes compared exactly).

The box is a user + mount namespace (`unshare -rm`, a fake root): the test's directories are bind-mounted over
/var/lib, /opt and /run, which both implementations hardcode, so nothing on the machine is touched. Skipped
where unprivileged user namespaces aren't available. Plus one cross-implementation test: the job lock (flock on
/var/lib/minecraft/job.lock) held by one implementation's countdown refuses the other's job.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO

SCRIPTS = REPO / "scripts"
TARGETS = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}
PASSWORD = "agent-test-pw"
LOGIN = "mcctl:sam"


def _binary() -> Path | None:
    if os.environ.get("SMALLWORLD_AGENT_BIN"):
        return Path(os.environ["SMALLWORLD_AGENT_BIN"])
    if sys.platform != "linux" or platform.machine() not in TARGETS:
        return None
    return REPO / "rust" / "dist" / f"smallworld-agent-{TARGETS[platform.machine()]}"


def _userns() -> bool:
    if shutil.which("unshare") is None:
        return False
    try:
        r = subprocess.run(["unshare", "-rm", "true"], capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


BINARY = _binary()
pytestmark = [
    pytest.mark.skipif(
        BINARY is None or not BINARY.is_file(),
        reason="no smallworld-agent binary for this platform (Linux x86_64/aarch64; or set SMALLWORLD_AGENT_BIN)",
    ),
    pytest.mark.skipif(not _userns(), reason="needs unprivileged user namespaces (unshare -rm)"),
]


# --- the fakes ---------------------------------------------------------------------------------------------


class Rcon:
    """A one-thread RCON server: login with PASSWORD, `list` answers with `players` online, everything else
    `ran <command>`; records every command."""

    def __init__(self, players: int = 0) -> None:
        self.players = players
        self.commands: list[str] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self.session, args=(conn,), daemon=True).start()

    @staticmethod
    def _read(conn: socket.socket, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                raise ConnectionError
            buf += chunk
        return buf

    @staticmethod
    def _send(conn: socket.socket, rid: int, kind: int, body: str) -> None:
        payload = struct.pack("<ii", rid, kind) + body.encode() + b"\0\0"
        conn.sendall(struct.pack("<i", len(payload)) + payload)

    def session(self, conn: socket.socket) -> None:
        with conn:
            try:
                while True:
                    (length,) = struct.unpack("<i", self._read(conn, 4))
                    data = self._read(conn, length)
                    rid, kind = struct.unpack("<ii", data[:8])
                    body = data[8:-2].decode()
                    if kind == 3:
                        self._send(conn, rid if body == PASSWORD else -1, 2, "")
                    elif kind == 2:
                        self.commands.append(body)
                        if body == "list":
                            reply = f"There are {self.players} of a max of 20 players online: "
                        else:
                            reply = f"ran {body}"
                        self._send(conn, rid, 0, reply)
                    else:
                        self._send(conn, rid, 0, "Unknown request 0")
            except (ConnectionError, OSError, struct.error):
                return

    def close(self) -> None:
        self.sock.close()


FAKE_SYSTEMCTL = r'''#!/usr/bin/env python3
"""Fake systemctl: records argv; `start mc-world.service` applies world-request.json to worlds.json like
worlds.py would; argv listed in fake.json "fail" exit 1."""
import json, os, sys
d = os.environ["FAKE_DIR"]
cfg = json.load(open(os.path.join(d, "fake.json")))
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps(["systemctl", *sys.argv[1:]]) + "\n")
args = [a for a in sys.argv[1:] if a != "--no-ask-password"]
if " ".join(args) in cfg.get("fail", []):
    sys.exit(1)
if args == ["start", "mc-world.service"] and cfg.get("world_applies", True):
    req = json.load(open("/var/lib/minecraft/world-request.json"))
    path = "/var/lib/mc-snapshots/worlds.json"
    st = json.load(open(path))
    if req["op"] == "delete":
        st["archives"] = [a for a in st["archives"] if a["name"] != req["name"]]
        st["deleted"] = st.get("deleted", []) + [{"name": req["name"]}]
    else:
        st["active"] = req["name"]
    json.dump(st, open(path, "w"))
'''

FAKE_JOURNALCTL = r"""#!/usr/bin/env python3
import json, os, sys
d = os.environ["FAKE_DIR"]
cfg = json.load(open(os.path.join(d, "fake.json")))
with open(os.path.join(d, "calls.jsonl"), "a") as f:
    f.write(json.dumps(["journalctl", *[a if not a[:1].isdigit() else "<since>" for a in sys.argv[1:]]]) + "\n")
for line in cfg.get("steps", []):
    print(line)
"""


@dataclass
class Box:
    """One fake server box: the directory tree bind-mounted over /var/lib, /opt and /run, fake binaries, an
    RCON server, the journald socket."""

    root: Path
    rcon: Rcon
    cfg: dict[str, Any] = field(default_factory=dict)
    audits: list[dict[str, str]] = field(default_factory=list)

    @property
    def varlib(self) -> Path:
        return self.root / "varlib"

    def start_journal(self) -> None:
        path = self.root / "run" / "systemd" / "journal" / "socket"
        path.parent.mkdir(parents=True, exist_ok=True)
        self.journal = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.journal.bind(str(path))
        self.journal.settimeout(0.2)

    def drain_journal(self) -> None:
        while True:
            try:
                data = self.journal.recv(1 << 20)
            except (TimeoutError, OSError):
                return
            self.audits.append(decode_journal(data))

    def write_cfg(self) -> None:
        (self.root / "fake.json").write_text(json.dumps(self.cfg))


def decode_journal(data: bytes) -> dict[str, str]:
    """journald's native protocol: KEY=value lines, or KEY\\n<u64 le length><bytes>\\n."""
    out: dict[str, str] = {}
    while data:
        nl = data.index(b"\n")
        line = data[:nl]
        if b"=" in line:
            k, v = line.split(b"=", 1)
            out[k.decode()] = v.decode()
            data = data[nl + 1 :]
        else:
            (n,) = struct.unpack("<Q", data[nl + 1 : nl + 9])
            out[line.decode()] = data[nl + 9 : nl + 9 + n].decode()
            data = data[nl + 10 + n :]
    return out


def snap(name: str, sid: str, dv: int = 4671) -> dict[str, Any]:
    return {"name": name, "id": sid * 8, "short_id": sid, "time": "2026-11-02T10:00:00Z", "size": 10,
            "data_version": dv, "snapshots": [sid * 8], "times": ["2026-11-02T10:00:00Z"]}  # fmt: skip


def make_box(tmp: Path, players: int = 0, state_dir: bool = True, **cfg: Any) -> Box:
    rcon = Rcon(players)
    box = Box(tmp, rcon, cfg)
    v, opt = tmp / "varlib", tmp / "opt"
    (v / "minecraft").mkdir(parents=True)
    (v / "mc-snapshots").mkdir()
    if state_dir:
        (v / "mc-agent").mkdir(mode=0o700)
    (opt / "minecraft" / "server").mkdir(parents=True)
    (opt / "minecraft" / "server" / "server.properties").write_text(
        f"enable-rcon=true\nrcon.port={rcon.port}\nrcon.password={PASSWORD}\nrcon.ip=127.0.0.1\n"
    )
    (opt / "minecraft" / "plugins-lock.json").write_text(json.dumps([
        {"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1.4.40"},
        {"name": "spark", "filename": "spark-1.10.jar", "version": "1.10"},
    ]))  # fmt: skip
    (v / "minecraft" / "plugins-disabled.json").write_text(
        json.dumps({"disabled": ["spark"], "by": "x", "at": "y"})
    )
    (opt / "minecraft" / "worlds-library.json").write_text(json.dumps({
        "active": "main",
        "library": [{"name": "creative", "seed": "42", "type": "flat_classic", "preset": "creative_build"}],
    }))  # fmt: skip
    (v / "mc-snapshots" / "worlds.json").write_text(json.dumps({
        "active": "main", "since": "2026-11-01T00:00:00Z", "data_version": 4671, "deleted": [],
        "archives": [snap("creative", "cccccccc"), snap("future", "ffffffff", 9999), snap("old", "aaaaaaaa", 100)],
    }))  # fmt: skip
    bin_dir = tmp / "bin"
    bin_dir.mkdir()
    for name, text in (("systemctl", FAKE_SYSTEMCTL), ("journalctl", FAKE_JOURNALCTL)):
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(0o755)
    box.write_cfg()
    box.start_journal()
    return box


def argv_for(impl: str, args: list[str]) -> list[str]:
    if impl == "python":
        return [sys.executable, str(SCRIPTS / "mc_agent.py"), "cli", *args]
    assert BINARY is not None
    return [str(BINARY), "cli", *args]


def namespaced(box: Box, argv: list[str]) -> list[str]:
    mounts = (
        f"mount --bind {box.root / 'varlib'} /var/lib && mount --bind {box.root / 'opt'} /opt && "
        f'mount --bind {box.root / "run"} /run && exec "$@"'
    )
    return ["unshare", "-rm", "sh", "-c", mounts, "sh", *argv]


def env_for(box: Box) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(("MC_", "WATCHDOG", "HEARTBEAT", "DISCORD"))
    }
    env["PATH"] = f"{box.root / 'bin'}:{env.get('PATH', '/usr/bin:/bin')}"
    env["FAKE_DIR"] = str(box.root)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["TZ"] = "UTC"
    return env


def start(box: Box, impl: str, args: list[str]) -> subprocess.Popen[str]:
    return subprocess.Popen(
        namespaced(box, argv_for(impl, args)), env=env_for(box), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


NORMALISE = [
    (re.compile(r"mcctl-\d+"), "mcctl-<ts>"),
]


def norm(text: str) -> str:
    for pat, rep in NORMALISE:
        text = pat.sub(rep, text)
    return text


def norm_json(v: Any) -> Any:
    """Timestamps (floats, the `at` string) and job ids replaced, so two runs compare."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if k in ("ts", "changed", "until") and isinstance(x, (int, float)) and x:
                out[k] = "<time>"
            elif k == "at" and isinstance(x, str):
                out[k] = "<utc>"
            else:
                out[k] = norm_json(x)
        return out
    if isinstance(v, list):
        return [norm_json(x) for x in v]
    if isinstance(v, str):
        return norm(v)
    return v


def files_of(box: Box) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in sorted(box.varlib.rglob("*")):
        if p.is_dir() or p.is_socket():
            continue
        rel = str(p.relative_to(box.varlib))
        mode = oct(p.stat().st_mode & 0o777)
        text = p.read_text()
        try:
            content: Any = norm_json(json.loads(text)) if text.strip() else ""
        except ValueError:
            content = norm(text)
        out[rel] = (mode, content)
    return out


def calls_of(box: Box) -> list[list[str]]:
    path = box.root / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@dataclass
class Outcome:
    code: int
    stdout: list[str]
    stderr_last: str
    audits: list[dict[str, str]]
    rcon: list[str]
    calls: list[list[str]]
    files: dict[str, Any]


def run(tmp: Path, impl: str, args: list[str], players: int = 0, setup: Any = None, **kw: Any) -> Outcome:
    box = make_box(tmp / impl, players=players, **kw)
    if setup:
        setup(box)
    try:
        p = start(box, impl, args)
        out, err = p.communicate(timeout=120)
        box.drain_journal()
        return Outcome(
            p.returncode,
            [norm(line) for line in out.splitlines()],
            err.strip().splitlines()[-1] if err.strip() else "",
            [{k: norm(v) for k, v in a.items()} for a in box.audits],
            box.rcon.commands,
            calls_of(box),
            files_of(box),
        )
    finally:
        box.rcon.close()
        box.journal.close()


def both(tmp: Path, args: list[str], **kw: Any) -> tuple[Outcome, Outcome]:
    py = run(tmp, "python", args, **kw)
    rs = run(tmp, "rust", args, **kw)
    assert rs == py, f"python: {py}\nrust:   {rs}"
    return py, rs


def j(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


# --- scenarios ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [[], ["sam", "restart"], ["mcctl:", "restart"], ["mcctl:a b", "restart"], [LOGIN, "console", "{}"],
     [LOGIN, "stop", "{}"], [LOGIN, "restart", "[1]"], [LOGIN, "restart", "{nope"], [LOGIN, "restart", "{}", "x"],
     [LOGIN, "restart", "NaN"]],
)  # fmt: skip
def test_usage(tmp_path: Path, args: list[str]) -> None:
    py, _ = both(tmp_path, args)
    assert py.code == 2 and py.stdout[-1].startswith("usage: mc_agent.py cli")


def _pending_world(box: Box) -> None:
    (box.varlib / "mc-agent" / "pending.json").write_text(json.dumps({
        "entries": [], "world": {"active": "main", "library": [{"name": "main"}], "what": "switch to main",
                                 "open_pr": {"number": 7, "url": "https://github.com/o/r/pull/7"}, "error": ""},
    }))  # fmt: skip


@pytest.mark.parametrize("setup", [None, _pending_world])
def test_lists(tmp_path: Path, setup: Any) -> None:
    for i, what in enumerate(("plugins", "worlds")):
        py, _ = both(tmp_path / str(i), [LOGIN, what], setup=setup)
        assert py.code == 0 and json.loads(py.stdout[-1])


def test_restart_now(tmp_path: Path) -> None:
    py, _ = both(tmp_path, [LOGIN, "restart", j({"countdown": 0})])
    assert py.stdout == ["RESULT ok: restarted"]
    assert py.calls == [["systemctl", "--no-ask-password", "restart", "minecraft.service"]]
    assert py.audits[0]["MESSAGE"] == "mcctl action=restart ok=True login=mcctl:sam"
    assert "minecraft/maintenance.json" not in py.files


def test_restart_skipped_countdown_on_an_empty_server(tmp_path: Path) -> None:
    py, _ = both(tmp_path, [LOGIN, "restart", j({"countdown": 10, "backup": True})])
    assert py.stdout[-1] == "RESULT ok: restarted; the server answers again"
    assert ["systemctl", "--no-ask-password", "start", "mc-backup.service"] in py.calls


def test_restart_countdown_with_players(tmp_path: Path) -> None:
    py, _ = both(tmp_path, [LOGIN, "restart", j({"countdown": 10})], players=1)
    assert py.stdout[0] == "countdown: countdown 10 s, 1 player(s) online"
    assert any(c.startswith("tellraw @a ") for c in py.rcon)


@pytest.mark.parametrize(
    "case",
    ["admin-stopped", "fenced", "bad-countdown", "bad-backup", "fail", "fail-backup"],
)
def test_restart_refusals_and_failures(tmp_path: Path, case: str) -> None:
    args: dict[str, Any] = {"countdown": 0}
    kw: dict[str, Any] = {}
    if case in ("admin-stopped", "fenced"):
        kw["setup"] = lambda b: (b.varlib / "minecraft" / case).write_text("{}")
    elif case == "bad-countdown":
        args["countdown"] = 5
    elif case == "bad-backup":
        args["backup"] = "yes"
    elif case == "fail":
        kw["fail"] = ["restart minecraft.service"]
    else:
        args, kw["fail"] = {"countdown": 10, "backup": True}, ["start mc-backup.service"]
    py, _ = both(tmp_path, [LOGIN, "restart", j(args)], **kw)
    assert py.stdout[-1].startswith("RESULT ")


@pytest.mark.parametrize(
    "args",
    [{"name": "spark", "enabled": True, "confirm": "enable spark"},
     {"name": "chunky", "enabled": False, "confirm": "disable chunky", "countdown": 10},
     {"name": "chunky", "enabled": False},
     {"name": "chunky", "enabled": True, "confirm": "enable chunky"},
     {"name": "nope", "enabled": True, "confirm": "enable nope"},
     {"name": "spark", "enabled": "yes", "confirm": "disable spark"},
     {"name": "spark", "enabled": True, "confirm": "enable spark", "countdown": 7}],
)  # fmt: skip
def test_plugin(tmp_path: Path, args: dict[str, Any]) -> None:
    both(tmp_path, [LOGIN, "plugin", j(args)])


def test_plugin_while_admin_stopped(tmp_path: Path) -> None:
    py, _ = both(
        tmp_path,
        [LOGIN, "plugin", j({"name": "spark", "enabled": True, "confirm": "enable spark"})],
        setup=lambda b: (b.varlib / "minecraft" / "admin-stopped").write_text("{}"),
    )
    assert py.stdout[-1] == "RESULT ok: spark enabled; the server is stopped (applies at start)"


@pytest.mark.parametrize(
    "args",
    [{"verb": "kick", "target": "Steve", "reason": " too\nloud\u001c now "},
     {"verb": "pardon", "target": "Steve", "reason": "ignored"},
     {"verb": "ban-ip", "target": "203.0.113.7"},
     {"verb": "op", "target": "Steve"},
     {"verb": "kick", "target": "@e"},
     {"verb": "kick", "target": 5}],
)  # fmt: skip
def test_moderate(tmp_path: Path, args: dict[str, Any]) -> None:
    both(tmp_path, [LOGIN, "moderate", j(args)])


def test_moderate_without_a_server(tmp_path: Path) -> None:
    def no_server(box: Box) -> None:
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()  # nothing listens there now
        props = box.root / "opt" / "minecraft" / "server" / "server.properties"
        props.write_text(props.read_text().replace(f"rcon.port={box.rcon.port}", f"rcon.port={port}"))

    py, _ = both(tmp_path, [LOGIN, "moderate", j({"verb": "kick", "target": "Steve"})], setup=no_server)
    assert py.stdout[-1] == "RESULT failed: moderate failed: ConnectionRefusedError"


@pytest.mark.parametrize(
    ("action", "args"),
    [("world_create", {"name": "fresh", "seed": "-5", "type": "amplified", "confirm": "create fresh"}),
     ("world_create", {"name": "fresh", "border_radius": {"minecraft:overworld": 100}, "pregen": True,
                       "confirm": "create fresh"}),
     ("world_create", {"name": "creative", "confirm": "create creative"}),
     ("world_create", {"name": "fresh", "type": "nope", "confirm": "create fresh"}),
     ("world_create", {"name": "Bad", "confirm": "create Bad"}),
     ("world_switch", {"name": "creative", "confirm": "switch to creative"}),
     ("world_switch", {"name": "old", "confirm": "switch to old", "countdown": 10}),
     ("world_switch", {"name": "future", "confirm": "switch to future"}),
     ("world_switch", {"name": "main", "confirm": "switch to main"}),
     ("world_switch", {"name": "nope", "confirm": "switch to nope"}),
     ("world_switch", {"name": "creative", "confirm": "switch creative"}),
     ("world_delete", {"name": "old", "confirm": "delete old"}),
     ("world_delete", {"name": "main", "confirm": "delete main"})],
)  # fmt: skip
def test_worlds(tmp_path: Path, action: str, args: dict[str, Any]) -> None:
    both(tmp_path, [LOGIN, action, j(args)])


def test_world_unit_failure_with_steps(tmp_path: Path) -> None:
    py, _ = both(
        tmp_path,
        [LOGIN, "world_switch", j({"name": "creative", "confirm": "switch to creative"})],
        fail=["start mc-world.service"],
        world_applies=False,
        steps=["STEP 1/7: archiving main", "FAILED: no space left"],
    )
    assert py.stdout[-1] == "RESULT failed: world_switch failed (FAILED: no space left)"


def test_world_actions_need_the_state_dir(tmp_path: Path) -> None:
    py, _ = both(
        tmp_path, [LOGIN, "world_delete", j({"name": "old", "confirm": "delete old"})], state_dir=False
    )
    assert "panel.mode: cloudflare" in py.stdout[-1]


def test_broken_state_crashes_alike(tmp_path: Path) -> None:
    def broken(box: Box) -> None:
        (box.varlib / "mc-snapshots" / "worlds.json").write_text(json.dumps({"archives": 5}))

    py, _ = both(tmp_path, [LOGIN, "worlds"], setup=broken)
    assert py.code == 1 and py.stderr_last.startswith("TypeError")


# --- the job lock across implementations and signals --------------------------------------------------------


@pytest.mark.parametrize(("holder", "other"), [("python", "rust"), ("rust", "python")])
def test_job_lock_is_shared_across_implementations(tmp_path: Path, holder: str, other: str) -> None:
    """A countdown started by one implementation holds job.lock; the other's job is refused as busy; SIGTERM
    (mcctl's session gone) cancels the countdown."""
    box = make_box(tmp_path, players=1)
    try:
        first = start(box, holder, [LOGIN, "restart", j({"countdown": 60})])
        deadline = time.monotonic() + 30
        while not any(c.startswith("tellraw") for c in box.rcon.commands):
            assert time.monotonic() < deadline, "the countdown didn't start"
            time.sleep(0.1)
        second = start(
            box, other, [LOGIN, "plugin", j({"name": "spark", "enabled": True, "confirm": "enable spark"})]
        )
        out, _ = second.communicate(timeout=60)
        assert second.returncode == 1
        assert out.splitlines()[-1] == (
            "RESULT refused: plugin: busy: another action is running (started from the panel or mcctl); wait, "
            "or cancel it there"
        )
        first.send_signal(signal.SIGTERM)
        out, _ = first.communicate(timeout=60)
        assert first.returncode == 1
        assert out.splitlines()[-1] == "RESULT cancelled: cancelled during the countdown"
        assert box.rcon.commands[-2].endswith('{"text": "Restart cancelled."}]')
        assert calls_of(box) == []
    finally:
        box.rcon.close()
        box.journal.close()
