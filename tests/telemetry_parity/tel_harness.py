"""The black-box parity harness for the player-experience telemetry (docs/ci.md "The telemetry parity harness").

An implementation runs as `<prefix> telemetry`, the mc-telemetry.service sidecar's entry point (Python:
`scripts/mc_agent.py telemetry` = make_telemetry + SidecarTelemetry + the overhead journal loop; Rust: the
binary given with `--agent-bin`). It reuses tests/agent_parity's machinery: the namespace box (`unshare -rm`
through nsinit.py, so the production paths are the harness's directories), FakeWorker (with scripted
/ingest/experience and /ingest/segment replies), fake `systemctl`/`journalctl` and AgentProc.

Time control without test hooks: a minute row closes when a snapshot of a *later* minute arrives, or at the first
read loop after the minute is 75 s old (the "plugin went quiet" close). The snapshots carry fixed timestamps in
the past (T0, January 2026), so every row is deterministic and closes at the latest one read loop (5 s, fixed:
Config.read_s) after its snapshots appear. The intervals the sidecar reads from agent.env are set to their
minimums: SEGMENT_S=30 (a segment flush after 30 s), LOG_BATCH_S=10 (the overhead journal round),
BACKFILL_PAUSE_S=0. What would need minutes of real time (the Pacer's back-off ladder and the row give-up after
BACKFILL_ROW_GIVE_UP server errors) is proved by the vectors (scripts/telemetry_vectors.py: Pacer and
Backfill.run_once decision traces with an injected clock), not by a clock hook.
"""

from __future__ import annotations

import calendar
import contextlib
import gzip
import json
import os
import re
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
AGENT_PARITY = HERE.parent / "agent_parity"
if str(AGENT_PARITY) not in sys.path:
    sys.path.insert(0, str(AGENT_PARITY))

from fakes import FakeWorker, wait_until  # noqa: E402
from harness import (  # noqa: E402
    AGENT_TOKEN,
    GC_LOG,
    HMAC_KEY,
    MOUNTS,
    PYTHON,
    ROOTS,
    AgentProc,
    Box,
    Impl,
    canonical,
    install_fake_systemd,
)

GOLDEN = HERE / "golden"
PYTHON_IMPL = PYTHON  # `<prefix> telemetry` = scripts/mc_agent.py telemetry
TEL_MOUNTS = MOUNTS | {"/var/lib/mc-telemetry": "var-lib-mc-telemetry"}
T0 = calendar.timegm((2026, 1, 15, 10, 0, 0)) * 1000  # the first snapshot minute (ms)
MIN = 60_000
SRC = "0badc0de"  # the preset installation id (telemetry-id.json) of most scenarios
UUIDS = {
    "Alice": "aaaaaaaa-1111-4111-8111-111111111111",
    "Bob": "bbbbbbbb-2222-4222-8222-222222222222",
    "Carol": "cccccccc-3333-4333-8333-333333333333",
    "Dave": "dddddddd-4444-4444-8444-444444444444",
    "Erin": "eeeeeeee-5555-4555-8555-555555555555",
}
ONLINE = "/var/lib/mc-telemetry/online.json"
IDENTITY = "/var/lib/mc-agent/telemetry-id.json"
SEGMENTS = "/var/lib/mc-telemetry/segments"
SHOW = {
    "minecraft.service": {"CPUUsageNSec": "5000000000", "MemoryCurrent": str(3 << 30),
                          "MemoryPeak": str(4 << 30), "IOReadBytes": "1048576", "IOWriteBytes": "2097152"},
    "mc-agent.service": {"CPUUsageNSec": "200000000", "MemoryCurrent": str(40 << 20),
                         "MemoryPeak": str(48 << 20), "IOReadBytes": "4096", "IOWriteBytes": "8192"},
    "mc-telemetry.service": {"CPUUsageNSec": "100000000", "MemoryCurrent": str(20 << 20),
                             "MemoryPeak": str(24 << 20), "IOReadBytes": "0", "IOWriteBytes": "0"},
}  # fmt: skip


# --- snapshots (what the mc-telemetry plugin writes) --------------------------------------------------------


def player(name: str, join: int, **kw: Any) -> dict[str, Any]:
    """One `pl` entry as the plugin writes it (Writer.java)."""
    p: dict[str, Any] = {"u": UUIDS.get(name, name), "n": name, "ping": 40, "brand": "vanilla", "view": 10,
                         "send": 10, "loc": "en_us", "w": "world", "cx": 0, "cz": 0, "ent": 12,
                         "glide": False, "tips": True, "join": join}  # fmt: skip
    p.update(kw)
    return p


def snap(seq: int, ts: int, players: list[dict[str, Any]] | None = None, **srv: Any) -> dict[str, Any]:
    """A v1 snapshot: a healthy 10 s window unless `srv` says otherwise."""
    base: dict[str, Any] = {"tps": 20.0, "ticks": 200, "mean": 12.5, "p50": 11.0, "p95": 20.0, "p99": 30.0,
                            "p999": 40.0, "max": 45.0, "self_p99_us": 80.25, "self_max_us": 150.0,
                            "budget_skips": 0, "lost_windows": 0, "heap_used_mb": 2048, "heap_max_mb": 6656,
                            "spikes": [], "scanned": 120,
                            "worlds": [{"n": "world", "chunks": 400, "entities": 300, "tiles": 50,
                                        "ticking": 40, "players": len(players or [])}],
                            "hot": []}  # fmt: skip
    base.update(srv)
    return {"v": 1, "seq": seq, "ts": ts, "win_ms": 10_000, "srv": base, "pl": players or []}


# --- the scenario world ------------------------------------------------------------------------------------


class TelScenario:
    """One scenario: the box, the fake Worker, fake systemd and the observations; `trace()` is compared."""

    def __init__(self, name: str, impl: Impl, tmp: Path) -> None:
        self.name, self.impl, self.tmp = name, impl, tmp
        tmp.mkdir(parents=True, exist_ok=True)
        self.box = Box(tmp / "box", TEL_MOUNTS)
        self.worker = FakeWorker(AGENT_TOKEN)
        # scripted replies: fn(request record, n-th request to the route) -> (status, body)
        self.experience: Callable[[dict[str, Any], int], tuple[int, Any]] = lambda rec, n: (
            200,
            ok_reply(rec),
        )
        self.segment: Callable[[dict[str, Any], int], tuple[int, Any]] = lambda rec, n: (200, {"ok": True})
        self.worker.replies["POST /ingest/experience"] = self._scripted("experience")
        self.worker.replies["PUT /ingest/segment"] = self._scripted("segment")
        self.ctl, self.fakebin = install_fake_systemd(tmp)
        self.systemd: dict[str, Any] = {"show": json.loads(json.dumps(SHOW)), "is_active": {}, "actions": {}}
        self.save_systemd()
        self.layout = tmp / "layout.json"
        binds = {prod: str(self.box.root / sub) for prod, sub in TEL_MOUNTS.items()}
        self.layout.write_text(json.dumps({"staging": str(tmp / "staging"), "roots": ROOTS, "binds": binds}))
        self.nsinit = AGENT_PARITY / "nsinit.py"
        self.box.write("/opt/minecraft/server/logs/gc.log", GC_LOG)
        self.box.host("/var/lib/mc-agent").chmod(0o700)
        self.box.host("/var/lib/mc-telemetry").chmod(0o700)
        self.box.host("/run/mc-verdicts").chmod(0o2770)
        self.notes: dict[str, Any] = {}
        self.exits: list[Any] = []
        self.procs: list[AgentProc] = []
        self.proc: AgentProc | None = None
        self.files_before: dict[str, tuple[str, int, bytes]] = {}
        self.srcs: list[str] = []  # installation ids seen, in order (normalised to <src1>, <src2> ...)
        self.counts: dict[str, int] = {}

    def _scripted(self, which: str) -> Callable[[dict[str, Any]], tuple[int, Any]]:
        def reply(rec: dict[str, Any]) -> tuple[int, Any]:
            n = self.counts.get(which, 0)
            self.counts[which] = n + 1
            status, body = (self.experience if which == "experience" else self.segment)(rec, n)
            rec["status"] = status
            return status, body

        return reply

    def save_systemd(self) -> None:
        (self.ctl / "systemd.json").write_text(json.dumps(self.systemd))

    def note(self, key: str, value: Any) -> None:
        if key in self.notes:
            raise KeyError(f"note {key} recorded twice")
        self.notes[key] = canonical(value)

    def begin(self) -> None:
        """Call after the scenario wrote its fixtures: file changes are measured from here."""
        self.files_before = self.box.files()

    def close(self) -> None:
        for p in self.procs:
            if p.alive():
                p.proc.kill()
                with contextlib.suppress(Exception):
                    p.proc.wait(10)
        with contextlib.suppress(Exception):
            self.worker.stop()

    def wait(self, pred: Any, timeout: float = 20, what: str = "condition") -> Any:
        return wait_until(pred, timeout, f"{what} ({self.name})")

    # --- inputs ---

    def write_snap(self, s: dict[str, Any] | bytes, seq: int | None = None, name: str | None = None) -> None:
        """Into /run/mc-telemetry as the plugin does: .snap.tmp, then an atomic rename."""
        data = s if isinstance(s, bytes) else json.dumps(s).encode()
        if name is None:
            assert isinstance(s, dict) or seq is not None
            name = f"snap-{seq if seq is not None else s['seq']}.json"  # type: ignore[index]
        d = self.box.host("/run/mc-telemetry")
        tmp = d / ".snap.tmp"
        tmp.write_bytes(data)
        tmp.chmod(0o640)
        os.replace(tmp, d / name)

    def clear_snaps(self) -> None:
        """What the plugin does when it starts (Writer.clean)."""
        for p in self.box.host("/run/mc-telemetry").glob("snap-*.json"):
            if p.is_dir() and not p.is_symlink():
                p.rmdir()
            else:
                p.unlink()

    def identity(self, src: str = SRC, seq: int = 0) -> None:
        self.box.write(IDENTITY, json.dumps({"src": src, "seq": seq}), 0o600)

    # --- the sidecar ---

    def env(self, **extra: str) -> dict[str, str]:
        """agent.env as the agent role renders it for mc-telemetry.service, intervals at their minimums."""
        env = {
            "PATH": f"{self.fakebin}:/usr/local/bin:/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "TZ": "UTC",
            "HOME": str(self.tmp),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
            "INGEST_URL": self.worker.url,
            "AGENT_TOKEN": AGENT_TOKEN,
            "COMMAND_HMAC_KEY": HMAC_KEY,
            "STATE_DIRECTORY": "/var/lib/mc-agent:/var/lib/mc-telemetry",
            "METRICS_INTERVAL_S": "300",
            "LOG_BATCH_S": "3600",  # the overhead journal round: only the overhead scenario shortens it
            "LOG_MAX_ROWS_PER_DAY": "30000",
            "POLL_S": "5",
            "EXPERIENCE": "1",
            "TELEMETRY_DIR": "/run/mc-telemetry",
            "VERDICT_DIR": "/run/mc-verdicts",
            "SEGMENT_DIR": SEGMENTS,
            "SEGMENT_DAYS": "30",
            "SEGMENT_CAP_MB": "200",
            "SEGMENT_S": "3600",  # scenarios that test segments pass SEGMENT_S=30 (the minimum)
            "BACKFILL_WINDOW": "",
            "BACKFILL_PAUSE_S": "0",
            "MESSAGE_PER_PLAYER_S": "1800",
            "MESSAGE_BROADCAST_S": "1800",
            "MESSAGE_PER_HOUR": "20",
            "LEVEL_NAME": "world",
            "CLIENT": "0",
        }
        env.update(extra)
        return env

    def start(self, impl: Impl | None = None, wait_poll: bool = True, **extra: str) -> AgentProc:
        """Start `<impl> telemetry`; by default wait for its first read loop (online.json written)."""
        assert self.proc is None or not self.proc.alive(), "one sidecar at a time"
        before = self.online_stamp()
        proc = AgentProc(
            [*(impl or self.impl).prefix, "telemetry"], self.env(**extra), self.layout, self.nsinit
        )
        self.procs.append(proc)
        self.proc = proc
        if wait_poll:
            self.wait(lambda: self.online_stamp() != before or not proc.alive(), 30, "the first read loop")
            if not proc.alive():
                raise RuntimeError(f"the sidecar exited: {proc.stderr[-20:]}")
        return proc

    def stop(self, label: str = "sidecar") -> int:
        assert self.proc is not None
        self.proc.signal(signal.SIGTERM)
        code = self.proc.wait(20)
        self.exits.append({label: code})
        self.note_src()
        return code

    def run_to_exit(self, label: str, **extra: str) -> None:
        """A run that must end by itself (a configuration error): exit code and output."""
        proc = AgentProc([*self.impl.prefix, "telemetry"], self.env(**extra), self.layout, self.nsinit)
        self.procs.append(proc)
        self.note(label, {"exit": proc.wait(30)})  # its output: the trace's `output`, in run order

    def online_stamp(self) -> tuple[int, int] | None:
        """(inode, mtime_ns) of online.json: rewritten (tmp + rename, a new inode) after every read loop."""
        try:
            st = self.box.host(ONLINE).stat()
        except OSError:
            return None
        return st.st_ino, st.st_mtime_ns

    def next_poll(self, n: int = 1, timeout: float = 20) -> None:
        """Wait until the sidecar finished `n` more read loops."""
        for _ in range(n):
            before = self.online_stamp()
            self.wait(lambda b=before: self.online_stamp() != b, timeout, "the next read loop")

    def note_src(self) -> None:
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            src = str(json.loads(self.box.host(IDENTITY).read_text())["src"])
            if src not in self.srcs:
                self.srcs.append(src)

    # --- the Worker's view ---

    def requests(
        self, route: str = "POST /ingest/experience", live: bool | None = None
    ) -> list[dict[str, Any]]:
        out = self.worker.requests_to(route)
        if live is not None:
            out = [r for r in out if isinstance(r["body"], dict) and r["body"].get("live") is live]
        return out

    def wait_requests(self, n: int, route: str = "POST /ingest/experience", live: bool | None = None,
                      timeout: float = 20) -> list[dict[str, Any]]:  # fmt: skip
        return self.wait(lambda: len(self.requests(route, live)) >= n and self.requests(route, live),
                         timeout, f"{n} x {route} (live={live})")  # fmt: skip

    def calls(self) -> list[dict[str, Any]]:
        path = self.ctl / "calls.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]

    # --- the trace ---

    def trace(self) -> dict[str, Any]:
        self.note_src()
        rows = RowRefs()
        out = {
            "notes": self.notes,
            "exits": self.exits,
            "requests": [self.request_entry(r, rows) for r in self.worker.requests],
            "files": self.files_trace(rows),
            "systemd": [[c["tool"], *c["argv"]] for c in self.calls()],
            "output": [{"stdout": [norm_line(x) for x in p.stdout], "stderr": [norm_line(x) for x in p.stderr]}
                       for p in self.procs],
        }  # fmt: skip
        return canonical(self.norm_src(out))

    def norm_src(self, v: Any) -> Any:
        if not self.srcs:
            return v
        text = json.dumps(v)
        for i, src in enumerate(self.srcs, 1):
            if src != SRC:
                text = text.replace(src, f"<src{i}>")
        return json.loads(text)

    def request_entry(self, r: dict[str, Any], rows: RowRefs) -> dict[str, Any]:
        entry: dict[str, Any] = {"route": r["route"], "status": r.get("status", 200 if r["auth"] else 401),
                                 "auth": r["auth"], "user_agent": r["user_agent"],
                                 "content_type": r["content_type"]}  # fmt: skip
        if r["query"]:
            entry["query"] = r["query"]
        body = r["body"]
        if r["route"] == "POST /ingest/experience" and isinstance(body, dict):
            entry["body"] = {k: v for k, v in body.items() if k != "rows"} | {
                "rows": rows.refs(body.get("rows"))
            }
        elif r["route"] == "PUT /ingest/segment" and isinstance(body, list):
            entry["body"] = {"ndjson": rows.refs(body)}
        else:
            entry["body"] = body
        return entry

    def files_trace(self, rows: RowRefs) -> dict[str, Any]:
        """What the runs changed outside /run/mc-telemetry (the harness's own input), contents normalised."""
        before, after = self.files_before, self.box.files()
        out: dict[str, Any] = {}
        for path in sorted(set(before) | set(after)):
            if path.startswith("/run/mc-telemetry/"):
                continue
            if path not in after:
                out[path] = "removed"
                continue
            if before.get(path) == after[path]:
                continue
            kind, mode, data = after[path]
            entry: dict[str, Any] = {"type": kind, "mode": oct(mode)}
            if kind == "file":
                entry["content"] = file_content(path, data, rows)
            out[path] = entry
        return out


def ok_reply(rec: dict[str, Any], verdicts: Any = None) -> dict[str, Any]:
    """The Worker's /ingest/experience answer (watchdog/worker.js ingestExperience)."""
    n = len((rec["body"] or {}).get("rows") or []) if isinstance(rec["body"], dict) else 0
    return {"ok": True, "stored": n, "rejected": 0, "verdicts": verdicts if verdicts is not None else {},
            "messages": 0}  # fmt: skip


# --- normalisation ------------------------------------------------------------------------------------------

VERSION_LINE = re.compile(r"^mc-telemetry \S+: ")
VERDICT_HEADER = re.compile(r"^mc-verdicts 1 \d+$", re.M)


def norm_line(line: str) -> str:
    return VERSION_LINE.sub("mc-telemetry <version>: ", line)


def norm_row(row: Any) -> Any:
    """A minute row with its host-timing values as placeholders: `ovh.*.cpu_pct` (a cgroup counter delta over
    the wall-clock time between two samples) and the backlog's byte counts (gzip output sizes: not part of the
    contract; which segments exist is)."""
    if not isinstance(row, dict):
        return row
    out = dict(row)
    if isinstance(out.get("ovh"), dict):
        out["ovh"] = {u: (dict(v, cpu_pct="<number>") if isinstance(v, dict) and isinstance(v.get("cpu_pct"),
                                                                                           int | float) else v)
                      for u, v in out["ovh"].items()}  # fmt: skip
    if isinstance(out.get("backlog"), dict):
        b = dict(out["backlog"])
        for k in ("bytes", "stored_bytes"):
            if isinstance(b.get(k), int) and b[k] > 0:
                b[k] = "<bytes>"
        out["backlog"] = b
    return out


class RowRefs:
    """Rows in full the first time they appear (in request order, then files), later as {"ref": id} when equal
    (a backfilled row: plus its `backfill` flag), so a trace carries each row once."""

    def __init__(self) -> None:
        self.seen: dict[str, Any] = {}

    def refs(self, rows: Any) -> Any:
        if not isinstance(rows, list):
            return rows
        out = []
        for r in rows:
            r = norm_row(r)
            if not isinstance(r, dict) or not isinstance(r.get("id"), str):
                out.append(r)
                continue
            base = {k: v for k, v in r.items() if k != "backfill"}
            if self.seen.get(r["id"]) == base:
                out.append({"ref": r["id"]} | ({"backfill": r["backfill"]} if "backfill" in r else {}))
            else:
                if r["id"] in self.seen:
                    r = dict(r, differs_from_earlier=True)
                self.seen[base["id"]] = base
                out.append(r)
        return out


def file_content(path: str, data: bytes, rows: RowRefs) -> Any:
    name = path.rsplit("/", 1)[-1]
    if not data:
        return "<0 bytes>"
    if name.endswith(".ndjson.gz"):
        try:
            text = gzip.decompress(data).decode("utf-8")
        except (OSError, EOFError, UnicodeDecodeError):
            return f"<{len(data)} bytes, not gzip>"
        return {"ndjson": rows.refs([json.loads(line) for line in text.splitlines() if line.strip()])}
    text = data.decode("utf-8", "replace")
    if name == "verdicts.tsv":
        return VERDICT_HEADER.sub("mc-verdicts 1 <now_ms>", text)
    if name.endswith(".json"):
        with contextlib.suppress(ValueError):
            return {"json": json.loads(text), "bytes": text}
    return text


def read_golden(name: str) -> Any:
    path = GOLDEN / f"{name}.json"
    return json.loads(path.read_text()) if path.exists() else None


def write_golden(name: str, trace: Any) -> None:
    GOLDEN.mkdir(exist_ok=True)
    (GOLDEN / f"{name}.json").write_text(json.dumps(trace, indent=1, ensure_ascii=False) + "\n")


def minute(i: int, sec: int = 0) -> int:
    """T0 + i minutes + sec seconds (ms)."""
    return T0 + i * MIN + sec * 1000
