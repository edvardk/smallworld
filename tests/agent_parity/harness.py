"""The black-box parity harness for mc-agent (design: docs/ci.md "The agent parity harness").

An implementation is an argv prefix: `python` = this checkout's scripts/mc_agent.py, `rust` = a binary
(`--agent-bin`). Both are run the same way, `<prefix> run` (the daemon; Python ignores the word) and
`<prefix> cli LOGIN ACTION [ARGS_JSON]`, inside `unshare -rm` (tests/agent_parity/nsinit.py), so the
production paths (/opt/minecraft, /var/lib/minecraft, /var/lib/mc-agent, /var/lib/mc-snapshots,
/run/mc-verdicts, /run/mc-telemetry, /run/systemd/journal/socket) are the harness's directories and no
production code needs a test hook. Everything the agent reaches is a fake (tests/agent_parity/fakes.py,
fake_systemd.py). A scenario drives the agent and returns a trace: what each fake saw, normalised (times,
volatile host metrics), plus the files the run changed. The Python agent's traces are the golden files.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fakes import FakeGitHub, FakeHttps, FakeRcon, FakeWorker, Journal, wait_until

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
SCRIPTS = REPO / "scripts"
GOLDEN = HERE / "golden"

# Test-only secrets (not real anywhere): the scrubber must remove every one of them from shipped logs.
AGENT_TOKEN = "AgentTokenParity-" + "0123456789abcdefABCDEF"  # split: a fixture, not a secret (gitleaks)
HMAC_KEY = "CommandKeyParity-" + "0123456789abcdefABCDEF"  # split: a fixture, not a secret (gitleaks)
HEARTBEAT_TOKEN = "HeartbeatParity-" + "0123456789abcdef"  # split: a fixture, not a secret (gitleaks)
RCON_PASSWORD = "rconPassParity42"
CONFIG_REPO = "parity-owner/parity-config"
LOGIN = "admin@parity.test"
SYNC = {"PANEL_SYNC_S": "1"}  # the whitelist/world -> Git loop every second (scenarios that test it)

UUIDS = {
    "Alice_Op": "11111111-1111-4111-8111-111111111111",
    "Bob": "22222222-2222-4222-8222-222222222222",
    "Carol": "33333333-3333-4333-8333-333333333333",
    "Dave": "44444444-4444-4444-8444-444444444444",
}
PLAYERS_YML = (
    "whitelist:\n"
    f'  - {{name: "Alice_Op", uuid: "{UUIDS["Alice_Op"]}"}}\n'
    f'  - {{name: "Bob", uuid: "{UUIDS["Bob"]}"}}\n'
    "ops:\n"
    f'  - {{name: "Alice_Op", uuid: "{UUIDS["Alice_Op"]}", level: 4}}\n'
)
SERVER_YML = (
    '# the parity config repo\nname: parity\n\nworld:\n  # kept\n  active: "main"\n  library:\n'
    '    - {name: "main", seed: "42", type: "default", preset: "survival_smp"}\n  border_radius: 5000\n'
)
PLUGINS_LOCK = [
    {"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1.4.40"},
    {"name": "spark", "filename": "spark-1.10.jar", "version": "1.10"},
]
GC_LOG = (
    "[2026-11-08T10:00:00.000+0000][120.1s][info][gc] GC(11) Pause Young (Normal) (G1 Evacuation Pause) "
    "2900M->1100M(6656M) 9.8ms\n"
    "[2026-11-08T10:00:10.000+0000][130.4s][info][gc] GC(12) Pause Young (Normal) (G1 Evacuation Pause) "
    "3000M->1200M(6656M) 12.3ms\n"
)


def snapshot_entry(sid: str, tags: list[str], time_: str = "2026-11-01T10:00:00Z") -> dict[str, Any]:
    return {"id": sid * 8, "short_id": sid, "time": time_, "tags": tags, "host": "mc", "size": 1234}


SNAPSHOTS = {
    "snapshots": [
        snapshot_entry("aaaaaaaa", ["daily"]),
        snapshot_entry("cccccccc", ["world-archive", "world:creative", "dv:4671"], "2026-10-20T10:00:00Z"),
    ]
}


def world_archive(name: str, sid: str, dv: int) -> dict[str, Any]:
    return {"name": name, "id": sid * 8, "short_id": sid, "time": "2026-10-20T10:00:00Z", "size": 1000,
            "data_version": dv, "snapshots": [sid * 8], "times": ["2026-10-20T10:00:00Z"]}  # fmt: skip


WORLDS_STATE = {
    "active": "main",
    "since": "2026-10-01T00:00:00+00:00",
    "data_version": 4671,
    "archives": [world_archive("creative", "cccccccc", 4671), world_archive("future", "ffffffff", 9999)],
    "deleted": [{"name": "gone", "at": "2026-10-30T00:00:00+00:00"}],
    "updated": "2026-11-01T00:00:00+00:00",
}
WORLDS_LIBRARY = {
    "active": "main",
    "library": [
        {"name": "main", "seed": "42", "type": "default", "preset": "survival_smp"},
        {"name": "creative", "seed": "", "type": "flat_classic", "preset": None},
    ],
}

# production path -> the box's host directory for it
MOUNTS = {
    "/opt/minecraft": "opt-minecraft",
    "/var/lib/minecraft": "var-lib-minecraft",
    "/var/lib/mc-agent": "var-lib-mc-agent",
    "/var/lib/mc-snapshots": "var-lib-mc-snapshots",
    "/run/mc-verdicts": "run-mc-verdicts",
    "/run/mc-telemetry": "run-mc-telemetry",
}
ROOTS = ["/opt", "/var/lib", "/run"]
STARTUP_PUSHES = ("POST /ingest/metrics", "POST /ingest/inventory")


@dataclass(frozen=True)
class Impl:
    """An agent implementation: the argv that starts it, before `run` / `cli ...`."""

    name: str
    prefix: tuple[str, ...]

    def daemon(self) -> list[str]:
        return [*self.prefix, "run"]

    def cli(self, args: list[str]) -> list[str]:
        return [*self.prefix, "cli", *args]


# -X faulthandler: a hung scenario dumps the agent's threads (SIGABRT), nothing else changes
PYTHON = Impl("python", (sys.executable, "-B", "-X", "faulthandler", str(SCRIPTS / "mc_agent.py")))


def namespaces_work(tmp: Path) -> str:
    """'' when `unshare -rm` can mount here, else why not (AppArmor's userns restriction, no unshare...)."""
    if not sys.platform.startswith("linux"):
        return "Linux only (user + mount namespaces)"
    if shutil.which("unshare") is None or shutil.which("mount") is None:
        return "unshare/mount not installed"
    if shutil.which("openssl") is None:
        return "openssl not installed (the fake Discord/watchdog's certificate)"
    probe = tmp / "nsprobe"
    probe.mkdir(exist_ok=True)
    r = subprocess.run(["unshare", "-rm", "mount", "-t", "tmpfs", "probe", str(probe)],
                       capture_output=True, text=True, timeout=30, check=False)  # fmt: skip
    if r.returncode:
        return f"unshare -rm can't mount here ({r.stderr.strip()[:200]})"
    return ""


# --- normalisation ------------------------------------------------------------------------------------------

TIME_KEYS = {"ts", "at", "iat", "since", "until", "finished_at", "deadline", "started", "finished", "created",
             "checked", "received", "changed", "updated"}  # fmt: skip
STRING_RULES = [
    (re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})"), "<iso>"),
    (re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}"), "<local-time>"),
    (re.compile(r"panel/(world-)?\d{8}-\d{6}"), r"panel/\1<ts>"),
    (re.compile(r"mcctl-\d{9,}"), "mcctl-<epoch>"),
]


def norm_str(s: str) -> str:
    for pat, repl in STRING_RULES:
        s = pat.sub(repl, s)
    return s


def norm(v: Any, keep_times: bool = False) -> Any:
    """Times (TIME_KEYS, ISO and local-time strings, the panel's branch stamps, mcctl's command ids) become
    placeholders; everything else stays."""
    if isinstance(v, dict):
        out = {}
        for k, x in v.items():
            if not keep_times and k in TIME_KEYS and x is not None and not isinstance(x, dict | list | bool):
                out[k] = "<time>"
            else:
                out[k] = norm(x, keep_times)
        return out
    if isinstance(v, list | tuple):
        return [norm(x, keep_times) for x in v]
    if isinstance(v, str):
        return norm_str(v)
    return v


# A metrics sample: what the fakes decide is compared exactly; host values (/proc, the disk) by type only.
# (`ws` races the first WebSocket connect at start: type only.)
SAMPLE_EXACT = {"up", "mspt", "tps", "tps5", "tps15", "players", "max_players", "players_online",
                "heap_used_mb", "heap_max_mb", "gc_max_pause_ms", "jvm_mem_mb", "admin_stopped"}  # fmt: skip


def norm_sample(sample: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in sample.items():
        if re.fullmatch(r"cpu\d+_pct", k):
            out["cpuN_pct"] = "number|null"
            continue
        if k in SAMPLE_EXACT:
            out[k] = v
        else:
            out[k] = (
                "null"
                if v is None
                else (
                    "bool"
                    if isinstance(v, bool)
                    else "number"
                    if isinstance(v, int | float)
                    else type(v).__name__
                )
            )
    return dict(sorted(out.items()))


def canonical(obj: Any) -> Any:
    """JSON round trip (tuples -> lists), as the golden files hold it."""
    return json.loads(json.dumps(obj))


def digest(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True).encode()).hexdigest()


def summarise(items: Any, head: int = 25, tail: int = 5) -> Any:
    """A long list (a 5000-entry log batch) as its length, digest, first and last entries."""
    if not isinstance(items, list) or len(items) <= head + tail:
        return items
    return {"count": len(items), "sha256": digest(items), "head": items[:head], "tail": items[-tail:]}


# --- the box --------------------------------------------------------------------------------------------------


class Box:
    """The production paths the agent sees, as host directories (bound into its mount namespace)."""

    def __init__(self, root: Path) -> None:
        self.root = root
        for sub in MOUNTS.values():
            (root / sub).mkdir(parents=True, exist_ok=True)

    def host(self, path: str) -> Path:
        for prod, sub in sorted(MOUNTS.items(), key=lambda kv: -len(kv[0])):
            if path == prod or path.startswith(prod + "/"):
                return self.root / sub / path[len(prod) :].lstrip("/")
        raise ValueError(f"{path} isn't in the box")

    def write(self, path: str, content: str | dict | list, mode: int = 0o644) -> None:
        p = self.host(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content if isinstance(content, str) else json.dumps(content, indent=1))
        p.chmod(mode)

    def read(self, path: str) -> str | None:
        p = self.host(path)
        return p.read_text() if p.exists() else None

    def json(self, path: str) -> Any:
        text = self.read(path)
        return json.loads(text) if text is not None else None

    def files(self) -> dict[str, tuple[str, int, bytes]]:
        """production path -> (type, mode, content) of every file and directory in the box."""
        out: dict[str, tuple[str, int, bytes]] = {}
        for prod, sub in MOUNTS.items():
            base = self.root / sub
            for p in sorted(base.rglob("*")):
                rel = prod + "/" + str(p.relative_to(base))
                st = p.lstat()
                if p.is_symlink():
                    out[rel] = ("symlink", 0, os.readlink(p).encode())
                elif p.is_dir():
                    out[rel] = ("dir", st.st_mode & 0o7777, b"")
                elif p.is_socket():
                    out[rel] = ("socket", st.st_mode & 0o7777, b"")
                else:
                    out[rel] = ("file", st.st_mode & 0o7777, p.read_bytes())
        return out

    def populate(self, rcon_port: int) -> None:
        self.write("/opt/minecraft/server/server.properties",
                   "#Minecraft server properties\nenable-rcon=true\nrcon.ip=127.0.0.1\n"
                   f"rcon.port={rcon_port}\nrcon.password={RCON_PASSWORD}\nlevel-name=world\n")  # fmt: skip
        self.write("/opt/minecraft/server/whitelist.json",
                   [{"uuid": UUIDS["Alice_Op"], "name": "Alice_Op"}, {"uuid": UUIDS["Bob"], "name": "Bob"}])  # fmt: skip
        self.write("/opt/minecraft/server/ops.json",
                   [{"uuid": UUIDS["Alice_Op"], "name": "Alice_Op", "level": 4, "bypassesPlayerLimit": False}])  # fmt: skip
        self.write("/opt/minecraft/server/banned-players.json",
                   [{"uuid": "55555555-5555-4555-8555-555555555555", "name": "Griefer", "created": "2026-10-01",
                     "source": "Server", "expires": "forever", "reason": "griefing"}])  # fmt: skip
        self.write("/opt/minecraft/server/banned-ips.json", [])
        self.write("/opt/minecraft/server/plugins/Chunky-1.4.40.jar", "jar")
        self.write("/opt/minecraft/server/plugins/spark-1.10.jar", "jar")
        self.host("/opt/minecraft/server/plugins/disabled").mkdir(parents=True, exist_ok=True)
        self.write("/opt/minecraft/server/logs/gc.log", GC_LOG)
        self.write(
            "/opt/minecraft/product-version.json", {"version": "1.3.1", "commit": "e786b14" + "0" * 33}
        )
        self.write("/opt/minecraft/plugins-lock.json", PLUGINS_LOCK)
        self.write("/opt/minecraft/worlds-library.json", WORLDS_LIBRARY)
        self.write("/var/lib/mc-snapshots/snapshots.json", SNAPSHOTS)
        self.write("/var/lib/mc-snapshots/worlds.json", WORLDS_STATE)
        self.host("/run/mc-telemetry/client").mkdir(parents=True, exist_ok=True)
        self.host("/var/lib/mc-agent").chmod(0o700)


def diff_files(
    before: dict[str, tuple[str, int, bytes]], after: dict[str, tuple[str, int, bytes]]
) -> dict[str, Any]:
    """What a run changed: new, modified and removed paths (mode, type, content normalised)."""
    out: dict[str, Any] = {}
    for path in sorted(set(before) | set(after)):
        if path not in after:
            out[path] = "removed"
            continue
        if before.get(path) == after[path]:
            continue
        kind, mode, data = after[path]
        entry: dict[str, Any] = {"type": kind, "mode": oct(mode)}
        if kind == "file":
            entry["content"] = file_content(path, data)
        out[path] = entry
    return out


def file_content(path: str, data: bytes) -> Any:
    if path.endswith(".lock") or not data:
        return f"<{len(data)} bytes>"
    name = path.rsplit("/", 1)[-1]
    if name == "spool.jsonl":
        return f"<{len(data.splitlines())} spooled samples>"
    text = data.decode("utf-8", "replace")
    if name == "nonces.json":
        return {k: "<time>" for k in json.loads(text)}
    if name.endswith(".json") or name == "admin-stopped":
        with contextlib.suppress(ValueError):
            return norm(json.loads(text))
    return norm_str(text)


# --- an agent process ---------------------------------------------------------------------------------------


class AgentProc:
    def __init__(self, argv: list[str], env: dict[str, str], layout: Path, nsinit: Path) -> None:
        cmd = ["unshare", "-rm", sys.executable, "-I", str(nsinit), str(layout), "--", *argv]
        self.proc = subprocess.Popen(cmd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True)  # fmt: skip
        self.stdout: list[str] = []
        self.stderr: list[str] = []
        self.threads = [
            threading.Thread(target=self._pump, args=(self.proc.stdout, self.stdout), daemon=True),
            threading.Thread(target=self._pump, args=(self.proc.stderr, self.stderr), daemon=True),
        ]
        for t in self.threads:
            t.start()

    @staticmethod
    def _pump(stream: Any, into: list[str]) -> None:
        for line in stream:
            into.append(line.rstrip("\n"))

    def signal(self, sig: int) -> None:
        if self.proc.poll() is None:
            self.proc.send_signal(sig)

    def wait(self, timeout: float = 60) -> int:
        try:
            code = self.proc.wait(timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait(10)
            raise
        for t in self.threads:
            t.join(5)
        return code

    def stop(self) -> int:
        self.signal(signal.SIGTERM)
        return self.wait(20)

    def alive(self) -> bool:
        return self.proc.poll() is None


# --- a scenario ---------------------------------------------------------------------------------------------


class Scenario:
    """One scenario's world: the box, the fakes and the observations. `trace()` is what is compared."""

    def __init__(self, name: str, impl: Impl, tmp: Path, cert: tuple[Path, Path]) -> None:
        self.name, self.impl, self.tmp = name, impl, tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.cert = cert[0]
        self.box = Box(tmp / "box")
        self.worker = FakeWorker(AGENT_TOKEN)
        self.github = FakeGitHub(CONFIG_REPO, PLAYERS_YML, SERVER_YML)
        self.github.mojang = {"steve": {"id": "8667ba71b85a4004af54457a9734eed7", "name": "Steve"},
                              "carol": {"id": UUIDS["Carol"].replace("-", ""), "name": "Carol"}}  # fmt: skip
        self.https = FakeHttps(cert[0], cert[1], HEARTBEAT_TOKEN)
        self.rcon = FakeRcon(RCON_PASSWORD, self.box.host("/opt/minecraft/server"))
        self.rcon.known = {n.lower(): (u, n) for n, u in UUIDS.items()}
        self.journal = Journal(tmp / "journal.sock")
        self.ctl = tmp / "ctl"
        self.ctl.mkdir(exist_ok=True)
        self.fakebin = tmp / "fakebin"
        self.fakebin.mkdir(exist_ok=True)
        source = (HERE / "fake_systemd.py").read_text()
        source = source.replace('os.environ.get("PARITY_FAKE_CTL", ".")', repr(str(self.ctl)))
        for tool in ("systemctl", "journalctl"):
            p = self.fakebin / tool
            p.write_text(f"#!{sys.executable} -I\n{source}")
            p.chmod(0o755)
        self.systemd: dict[str, Any] = {
            "show": {
                "minecraft.service": {
                    "MemoryCurrent": str(3 << 30),
                    "ActiveState": "active",
                    "ActiveEnterTimestampMonotonic": "1000000",
                }
            },
            "is_active": {},
            "actions": {},
            "steps": {},
        }
        self.save_systemd()
        self.box.populate(self.rcon.port)
        self.layout = tmp / "layout.json"
        binds = {prod: str(self.box.root / sub) for prod, sub in MOUNTS.items()}
        binds["/run/systemd/journal/socket"] = str(self.journal.path)
        self.layout.write_text(json.dumps({"staging": str(tmp / "staging"), "roots": ROOTS, "binds": binds}))
        self.nsinit = HERE / "nsinit.py"
        self.notes: dict[str, Any] = {}
        self.exits: list[Any] = []
        self.procs: list[AgentProc] = []
        self.daemon_proc: AgentProc | None = None
        self.n = 0
        self.files_before: dict[str, tuple[str, int, bytes]] = {}
        self.skip: set[str] = set()  # trace sections a scenario declares timing-dependent

    @property
    def last_id(self) -> str:
        return f"cmd{self.n:03d}"

    def layout_without(self, prod: str) -> Path:
        """A layout in which `prod` (e.g. /var/lib/mc-agent: a box without the agent) doesn't exist."""
        data = json.loads(self.layout.read_text())
        data["binds"].pop(prod)
        path = self.tmp / f"layout-without-{prod.strip('/').replace('/', '-')}.json"
        path.write_text(json.dumps(data))
        return path

    def audits(self) -> list[dict[str, str]]:
        return [dict(e) for e in self.journal.snapshot()]

    def wait_audit(self, text: str, count: int = 1, timeout: float = 20) -> None:
        self.wait(lambda: sum(text in e.get("MESSAGE", "") for e in self.audits()) >= count, timeout,
                  f"an audit entry {text!r}")  # fmt: skip

    def begin(self) -> None:
        """Call after the scenario wrote its own fixtures: file changes are measured from here."""
        self.files_before = self.box.files()

    def close(self) -> None:
        for p in self.procs:
            if p.alive():
                p.proc.kill()
                with contextlib.suppress(Exception):
                    p.proc.wait(10)
        for f in (self.worker, self.github, self.https, self.rcon, self.journal):
            with contextlib.suppress(Exception):
                f.stop()

    def save_systemd(self) -> None:
        (self.ctl / "systemd.json").write_text(json.dumps(self.systemd))

    def note(self, key: str, value: Any) -> None:
        if key in self.notes:
            raise KeyError(f"note {key} recorded twice")
        self.notes[key] = canonical(value)

    # --- environments ---

    def base_env(self) -> dict[str, str]:
        return {
            "PATH": f"{self.fakebin}:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "HOME": str(self.tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "SSL_CERT_FILE": str(self.cert),
        }

    def notify_env(self) -> dict[str, str]:
        """/etc/minecraft/notify.env (what `mc-run -notify.env` gives the cli)."""
        return {
            "WATCHDOG_URL": self.https.url,
            "HEARTBEAT_TOKEN": HEARTBEAT_TOKEN,
            "DISCORD_WEBHOOK_URL": f"{self.https.url}/api/webhooks/123/parity-webhook-secret",
        }

    def agent_env(self, **extra: str) -> dict[str, str]:
        """agent.env as the agent role writes it, with the loops shortened (as the e2e run does)."""
        env = (
            self.base_env()
            | self.notify_env()
            | {
                "INGEST_URL": self.worker.url,
                "AGENT_TOKEN": AGENT_TOKEN,
                "COMMAND_HMAC_KEY": HMAC_KEY,
                "STATE_DIRECTORY": "/var/lib/mc-agent",
                "METRICS_INTERVAL_S": "300",
                "LOG_BATCH_S": "3600",
                "POLL_S": "5",
                "PANEL_SYNC_S": "3600",  # the Git-flow scenarios pass SYNC (1 s); elsewhere a round would race
                "PANEL_BATCH_S": "1",
                "PANEL_PR_CHECK_S": "1",
                "CONFIG_REPO": CONFIG_REPO,
                "CONFIG_BRANCH": "main",
                "GITHUB_API_URL": self.github.url,
                "MOJANG_LOOKUP_URLS": f"{self.github.url}/mojang/a/,{self.github.url}/mojang/b/",
                "EXPERIENCE": "0",
                "CLIENT": "0",
            }
        )
        env.update(extra)
        return {k: v for k, v in env.items() if v is not None}

    # --- the daemon ---

    def daemon(self, impl: Impl | None = None, ready: str = "ws", **extra: str) -> AgentProc:
        """Start the daemon; wait for its WebSocket hello (ready="ws"), its first poll ("poll") or nothing."""
        assert self.daemon_proc is None or not self.daemon_proc.alive(), "one daemon at a time"
        hellos = len([e for e in self.worker.events if e["event"] == "ws-hello"])
        polls = len(self.worker.requests_to("GET /agent/commands"))
        pushes = [len(self.worker.requests_to(r)) for r in STARTUP_PUSHES]
        proc = AgentProc((impl or self.impl).daemon(), self.agent_env(**extra), self.layout, self.nsinit)
        self.procs.append(proc)
        self.daemon_proc = proc
        if ready == "ws":
            self.wait(lambda: len([e for e in self.worker.events if e["event"] == "ws-hello"]) > hellos
                      or not proc.alive(), 30, "the agent's WebSocket hello")  # fmt: skip
        elif ready == "poll":
            self.wait(lambda: len(self.worker.requests_to("GET /agent/commands")) > polls or not proc.alive(),
                      30, "the agent's first poll")  # fmt: skip
        if ready != "none":
            # the startup pushes (metrics sample, inventory) and the spool's rewrite after the first delivery
            self.wait(lambda: all(len(self.worker.requests_to(r)) > n for r, n in zip(STARTUP_PUSHES, pushes,
                                                                                      strict=True))
                      or not proc.alive(), 30, "the startup pushes")  # fmt: skip
            spool = self.box.host("/var/lib/mc-agent/spool.jsonl")
            delivered = self.worker.requests_to("POST /ingest/metrics")[-1]["auth"]
            with contextlib.suppress(TimeoutError):  # a delivered sample leaves the spool empty
                self.wait(lambda: spool.exists() and not spool.with_name("spool.jsonl.tmp").exists()
                          and (spool.stat().st_size == 0 or not delivered), 3, "")  # fmt: skip
            if not proc.alive():
                raise RuntimeError(f"the agent exited: {proc.stderr[-20:]}")
        return proc

    def stop_daemon(self, label: str = "daemon") -> int:
        assert self.daemon_proc is not None
        code = self.daemon_proc.stop()
        self.exits.append({label: code})
        return code

    def wait(self, pred: Any, timeout: float = 15, what: str = "condition") -> Any:
        return wait_until(pred, timeout, f"{what} ({self.name})")

    # --- commands ---

    def signed(self, action: str, args: dict[str, Any] | None = None, *, login: str = LOGIN,
               iat_ms: int | None = None, nonce: str | None = None, cmd_id: str | None = None) -> dict[str, str]:  # fmt: skip
        self.n += 1
        payload = json.dumps({
            "id": cmd_id or f"cmd{self.n:03d}",
            "action": action,
            "args": args or {},
            "login": login,
            "iat": int(time.time() * 1000) if iat_ms is None else iat_ms,
            "nonce": nonce or f"nonce-{self.name[:20]}-{self.n:04d}",
        }, separators=(",", ":"))  # fmt: skip
        return {
            "payload": payload,
            "sig": hmac.new(HMAC_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest(),
        }

    def send(self, message: dict[str, Any], timeout: float = 20) -> dict[str, Any]:
        """Deliver one command message (WebSocket, or the next poll) and wait for its result."""
        before = len(self.worker.result_order)
        self.worker.send_command(message)  # type: ignore[arg-type]
        self.wait(lambda: len(self.worker.result_order) > before, timeout, "a command result")
        channel, rid = self.worker.result_order[before]
        result = dict(self.worker.results[rid])
        out = {k: v for k, v in result.items() if k != "finished_at"}
        if not isinstance(result.get("finished_at"), int):
            out["finished_at"] = repr(result.get("finished_at"))
        if channel != "ws":
            out["channel"] = channel
        return norm(out)

    def cmd(self, action: str, args: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.send(self.signed(action, args, **kw))

    def jobs(self, job_id: str) -> list[dict[str, Any]]:
        return [r["body"]["job"] for r in self.worker.requests_to("POST /ingest/job")
                if isinstance(r["body"], dict) and (r["body"].get("job") or {}).get("id") == job_id]  # fmt: skip

    def job_lock_free(self) -> bool:
        """Nobody holds /var/lib/minecraft/job.lock (probed with the same flock the agent takes)."""
        path = self.box.host("/var/lib/minecraft/job.lock")
        if not path.exists():
            return True
        fd = os.open(path, os.O_RDWR)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        finally:
            os.close(fd)  # closing drops the probe's lock
        return True

    def wait_job(self, job_id: str, timeout: float = 30) -> dict[str, Any]:
        """The job's final report, then its job.lock released: the Python agent reports the end before it
        releases the lock (Jobs._run), so a command sent at once could be refused as busy."""
        done = ("done", "failed", "cancelled")
        self.wait(lambda: any(j["state"] in done for j in self.jobs(job_id)), timeout, f"job {job_id}")
        self.wait(self.job_lock_free, 10, "job.lock to be released")
        return self.jobs(job_id)[-1]

    def inventories(self) -> list[dict[str, Any]]:
        return [r["body"]["inventory"] for r in self.worker.requests_to("POST /ingest/inventory")
                if isinstance(r["body"], dict) and isinstance(r["body"].get("inventory"), dict)]  # fmt: skip

    def wait_inventory(self, pred: Any, after: int = 0, timeout: float = 20) -> dict[str, Any]:
        """The first inventory push (from index `after`) that satisfies pred."""

        def find() -> dict[str, Any] | None:
            return next((i for i in self.inventories()[after:] if pred(i)), None)

        return self.wait(find, timeout, "an inventory push")

    # --- the cli ---

    def cli(self, args: list[str], *, impl: Impl | None = None, env: dict[str, str] | None = None,
            layout: Path | None = None) -> AgentProc:  # fmt: skip
        """Start `<impl> cli ARGS` as mcctl's `sudo mc-run -/etc/minecraft/notify.env` runs it."""
        proc = AgentProc((impl or self.impl).cli(args), env or (self.base_env() | self.notify_env()),
                         layout or self.layout, self.nsinit)  # fmt: skip
        self.procs.append(proc)
        return proc

    def cli_run(self, label: str, args: list[str], **kw: Any) -> dict[str, Any]:
        proc = self.cli(args, **kw)
        code = proc.wait(60)
        out = {"exit": code, "stdout": [norm_str(line) for line in proc.stdout]}
        self.note(label, out)
        return out

    # --- the trace ---

    def trace(self) -> dict[str, Any]:
        out = self._trace()
        return {k: ("<timing-dependent: not compared>" if k in self.skip else v) for k, v in out.items()}

    def _trace(self) -> dict[str, Any]:
        self.journal.drain()
        with contextlib.suppress(TimeoutError):  # the fake's side of a closed WebSocket ends a moment later
            self.wait(lambda: len([e for e in self.worker.events if e["event"] == "ws-ended"])
                      >= len(self.worker.sessions), 5, "")  # fmt: skip
        return canonical({
            "notes": self.notes,
            "exits": self.exits,
            "worker": self.worker_trace(),
            "websocket": [norm(e) for e in self.worker.events],
            "console": [norm(m, keep_times=True) for m in self.worker.console],
            "github": self.github_trace(),
            "https": [self.https_entry(r) for r in self.https.requests],
            "rcon": self.rcon_trace(),
            "systemd": self.systemd_trace(),
            "audit": self.audit_trace(),
            "files": diff_files(self.files_before, self.box.files()),
        })  # fmt: skip

    def worker_trace(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        seen_first: set[str] = set()
        for r in self.worker.requests:
            route = r["route"]
            entry: dict[str, Any] = {"auth": r["auth"], "user_agent": r["user_agent"]}
            if r["content_type"]:
                entry["content_type"] = r["content_type"]
            if r["query"]:
                entry["query"] = r["query"]
            body = r["body"]
            if route == "POST /ingest/metrics":
                if route in seen_first:
                    continue  # one per METRICS_INTERVAL_S: only the first is compared
                seen_first.add(route)
                samples = (body or {}).get("samples") or []
                entry["body"] = {
                    "agent": (body or {}).get("agent"),
                    "samples": [norm_sample(s) for s in samples],
                }
            elif route == "POST /ingest/inventory":
                if route in seen_first:
                    continue  # the startup push; later pushes depend on timing (checkpoints are notes)
                seen_first.add(route)
                entry["body"] = norm(body)
            elif route == "GET /agent/commands":
                continue  # one per POLL_S: scenarios note the polls they rely on
            elif route == "POST /ingest/archive":
                entry["body"] = summarise(body)  # deterministic from the journal fixture
            elif route == "POST /ingest/logs":
                entry["body"] = (
                    dict(body, rows=summarise(body.get("rows"))) if isinstance(body, dict) else body
                )
            else:
                entry["body"] = norm(body)
            out.setdefault(route, []).append(entry)
        return out

    def github_trace(self) -> list[dict[str, Any]]:
        tokens: dict[str, int] = {}
        out = []
        for r in self.github.requests:
            if r["path"].startswith("/mojang/"):
                out.append({"method": r["method"], "path": r["path"], "user_agent": r["user_agent"],
                            "accept": r["accept"]})  # fmt: skip
                continue
            tok = tokens.setdefault(r["auth"], len(tokens))
            entry = {"method": r["method"], "path": r["path"], "query": r["query"], "token": tok,
                     "accept": r["accept"], "api_version": r["api_version"], "user_agent": r["user_agent"]}  # fmt: skip
            if r["body"] is not None:
                body = dict(r["body"])
                if "content" in body:
                    import base64

                    body["content"] = base64.b64decode(body["content"]).decode()
                entry["body"] = norm(body)
            out.append(entry)
        return out

    def https_entry(self, r: dict[str, Any]) -> dict[str, Any]:
        path = r["path"]
        auth = r["auth"]
        return {"method": r["method"], "path": path, "content_type": r["content_type"],
                "auth": "heartbeat" if auth == f"Bearer {HEARTBEAT_TOKEN}" else auth, "body": norm(r["body"])}  # fmt: skip

    def rcon_trace(self) -> dict[str, Any]:
        """The kept stats connection (first command `mspt`) only by count; every other connection's commands."""
        by_conn: dict[int, list[str]] = {}
        for cid, cmd in self.rcon.log:
            by_conn.setdefault(cid, []).append(cmd)
        stats = [c for c, cmds in by_conn.items() if cmds[0] == "mspt"]
        return {
            "stats_connections": len(stats),
            "connections": [cmds for c, cmds in sorted(by_conn.items()) if c not in stats],
        }

    def calls(self) -> list[dict[str, Any]]:
        path = self.ctl / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    def systemd_trace(self) -> dict[str, Any]:
        """Mutating systemctl calls and journalctl reads in order; the read-only probes (`show`, `is-active`,
        one per metrics sample) as a sorted set."""
        seq, probes = [], set()
        for c in self.calls():
            argv = [norm_str(a) for a in c["argv"]]
            if c["tool"] == "systemctl" and argv[:1] in (["show"], ["is-active"]):
                probes.add(" ".join(argv))
            else:
                seq.append([c["tool"], *argv])
        return {"calls": seq, "probes": sorted(probes)}

    def audit_trace(self) -> list[list[list[str]]]:
        return [[[k, norm_str(v)] for k, v in e] for e in self.journal.snapshot()]


def read_golden(name: str) -> Any:
    path = GOLDEN / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def write_golden(name: str, trace: Any) -> None:
    GOLDEN.mkdir(exist_ok=True)
    (GOLDEN / f"{name}.json").write_text(json.dumps(trace, indent=1, ensure_ascii=False) + "\n")
