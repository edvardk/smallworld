"""mc-agent's player-experience telemetry (design §8 P13). Stdlib only, imported by mc_agent.py.

Data path (all off the JVM; the plugin's main-thread cost is its own concern, plugin/mc-telemetry):

  mc-telemetry plugin --(tmpfs: /run/mc-telemetry/snap-<seq>.json every 10 s)--> this module
    -> one *minute row* per minute (pre-aggregated: server tick distribution, per-player summary, the raw 10 s
       samples, join/leave events, lag spikes with context, product overhead, backlog)
    -> live: POST /ingest/experience (D1 via the Worker; the reply carries the verdicts, written to
       /run/mc-verdicts/verdicts.tsv for the plugin's /lag)
    -> RAM buffer -> every SEGMENT_S one gzip NDJSON *segment* (/var/lib/mc-telemetry/segments/, one
    sequential
       write, no fsync per row) -> PUT /ingest/segment (R2 archive, keyed by the segment name)
    -> anything not delivered is *backfilled* later, oldest first, only in quiet time (nobody online and the
       server healthy, or inside the configured window), throttled, resumable, stopped as soon as a player
       joins.

Every row has a stable id `<src>:<seq>` (src = this installation's random id, seq = a persisted counter) and
its
event time `ts` (the minute); the Worker upserts on the minute, so a replayed row never duplicates. Segments
are
kept SEGMENT_DAYS days and under SEGMENT_CAP_MB (oldest dropped first) as the box's own redundant copy.

No IP address is read or written anywhere here (the plugin never collects one).
"""

from __future__ import annotations

import contextlib
import gzip
import io
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mc_rcon

SNAP_DIR = Path("/run/mc-telemetry")
VERDICT_DIR = Path("/run/mc-verdicts")
SEGMENT_DIR = Path("/var/lib/mc-telemetry/segments")
SNAP_NAME = re.compile(r"^snap-(\d{1,12})\.json$")
SEGMENT_NAME = re.compile(r"^(\d{8}T\d{4})-([0-9a-f]{8})-(\d+)-(\d+)\.ndjson\.gz$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
NAME = re.compile(r"^[A-Za-z0-9_]{1,16}$")
MINUTE_MS = 60_000
SEGMENT_S = 300
MAX_SNAPSHOT_BYTES = 1 << 20
MAX_LAG_EVENTS = 20  # per minute row
LAG_CONTEXT_PLAYERS = 3
# Backfill rows per request (P13 review M2): the Worker judges and stores each one, inside the Free plan's
# 10 ms of CPU per request (measured in watchdog/test/experience.test.mjs). A 5xx halves the batch down to 1,
# then the backfill backs off (BACKFILL_BACKOFF_S doubling up to BACKFILL_BACKOFF_MAX_S); a single row the
# Worker refuses with a 5xx BACKFILL_ROW_GIVE_UP times in a row (while live rows go through) is given up (a
# gap, logged).
BACKFILL_BATCH = 3
BACKFILL_BACKOFF_S = 60.0
BACKFILL_BACKOFF_MAX_S = 3600.0
BACKFILL_ROW_GIVE_UP = 5
BACKFILL_GROW_AFTER = 20  # successful requests in a row before a halved batch grows by one row again
SERVER_ERROR = re.compile(r"\bHTTP 5\d\d\b")
OVERHEAD_UNITS = ("minecraft.service", "mc-agent.service")
# Oneshot units: systemd logs "<unit>: Consumed 1.234s CPU time[, ...]" when each run ends (systemd >= 252).
ONESHOT_UNITS = re.compile(r"^(mc-[a-z0-9@._-]+\.service)$")
CONSUMED = re.compile(r"Consumed (?:(\d+)min )?(\d+(?:\.\d+)?)(ms|s) CPU time")
MEMORY_PEAK = re.compile(r"(\d+(?:\.\d+)?)([KMG]) memory peak")
TEXT_MAX = 200


def clean_text(text: object, limit: int = TEXT_MAX) -> str:
    """One line for the plugin's tab-separated files: no tabs, newlines, U+001F or other control
    characters."""
    t = "".join(" " if ch.isspace() else ch for ch in str(text or ""))  # U+001F counts as whitespace too
    t = "".join(ch for ch in t if ch.isprintable())
    return " ".join(t.split())[:limit]


# --- snapshots (plugin -> agent) ----------------------------------------------------------------------------


def _num(v: object) -> float | None:
    """A finite float for an int or float (not a bool), else None. v1.4 fix (Python and Rust): `NaN`,
    `Infinity` or an int too big for a float in a snapshot used to raise from `int()` later (OverflowError,
    ValueError), so every telemetry round failed at that snapshot and the reader never moved past it."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return None
    try:
        f = float(v)
    except OverflowError:
        return None
    return f if math.isfinite(f) else None


HOT_KEYS = ("ent", "tiles", "vil", "items")
MAX_HOT = 10  # busiest chunks kept per snapshot / minute row


def parse_hot(raw: object) -> list[dict[str, Any]]:
    """The plugin's busiest chunks [{w, cx, cz, ent, tiles, vil, items}]: sane types only, <= MAX_HOT."""
    out = []
    for c in (raw if isinstance(raw, list) else [])[:MAX_HOT]:
        if not isinstance(c, dict) or not isinstance(c.get("w"), str):
            continue
        if not all(isinstance(c.get(k), int) for k in ("cx", "cz")):
            continue
        out.append({"w": clean_text(c["w"], 64), "cx": c["cx"], "cz": c["cz"]}
                   | {k: max(0, int(_num(c.get(k)) or 0)) for k in HOT_KEYS})  # fmt: skip
    return out


def merge_hot(snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The minute's busiest chunks: per chunk the highest count of each kind over its snapshots, the MAX_HOT
    with the most entities or tile entities (entity-heavy first)."""
    by: dict[tuple[str, int, int], dict[str, Any]] = {}
    for s in snaps:
        for c in s["srv"].get("hot", []):
            acc = by.setdefault((c["w"], c["cx"], c["cz"]), {"w": c["w"], "cx": c["cx"], "cz": c["cz"]})
            for k in HOT_KEYS:
                acc[k] = max(acc.get(k, 0), c[k])
    ranked = sorted(by.values(), key=lambda c: (c["ent"], c["tiles"]), reverse=True)
    tiles = sorted(by.values(), key=lambda c: c["tiles"], reverse=True)
    keep = ranked[: MAX_HOT // 2]
    for c in tiles + ranked:
        if len(keep) >= MAX_HOT:
            break
        if c not in keep and (c["ent"] or c["tiles"]):
            keep.append(c)
    return sorted(keep, key=lambda c: (c["ent"], c["tiles"]), reverse=True)


def parse_snapshot(data: bytes) -> dict[str, Any] | None:
    """A snapshot the plugin wrote, with only the fields we know and sane types (it is a hand-off from a less
    privileged process: never trusted blindly)."""
    try:
        raw = json.loads(data)
    except ValueError:
        return None
    if not isinstance(raw, dict) or raw.get("v") != 1 or not isinstance(raw.get("ts"), int):
        return None
    srv = raw.get("srv") if isinstance(raw.get("srv"), dict) else {}
    out_srv: dict[str, Any] = {}
    for k in ("tps", "ticks", "mean", "p50", "p95", "p99", "p999", "max", "self_p99_us", "self_max_us",
              "budget_skips", "lost_windows", "heap_used_mb", "heap_max_mb"):  # fmt: skip
        out_srv[k] = _num(srv.get(k))
    spikes = srv.get("spikes") if isinstance(srv.get("spikes"), list) else []
    out_srv["spikes"] = [
        [int(s[0]), float(s[1])]
        for s in spikes[:32]
        if isinstance(s, list) and len(s) == 2 and isinstance(s[0], int) and _num(s[1]) is not None
    ]
    worlds = []
    for w in (srv.get("worlds") if isinstance(srv.get("worlds"), list) else [])[:16]:
        if isinstance(w, dict) and isinstance(w.get("n"), str):
            worlds.append({"n": clean_text(w["n"], 64)} | {k: int(_num(w.get(k)) or 0) for k in
                          ("chunks", "entities", "tiles", "ticking", "players")})  # fmt: skip
    out_srv["worlds"] = worlds
    out_srv["scanned"] = int(_num(srv.get("scanned")) or 0)
    out_srv["hot"] = parse_hot(srv.get("hot"))
    players = []
    for p in (raw.get("pl") if isinstance(raw.get("pl"), list) else [])[:500]:
        if (
            not isinstance(p, dict)
            or not UUID.match(str(p.get("u", "")))
            or not NAME.match(str(p.get("n", "")))
        ):
            continue
        players.append({
            "u": p["u"], "n": p["n"],
            "ping": int(_num(p.get("ping")) or 0), "brand": clean_text(p.get("brand"), 32),
            "view": int(_num(p.get("view")) or 0), "send": int(_num(p.get("send")) or 0),
            "loc": clean_text(p.get("loc"), 16), "w": clean_text(p.get("w"), 64),
            "cx": int(_num(p.get("cx")) or 0), "cz": int(_num(p.get("cz")) or 0),
            "ent": int(_num(p.get("ent")) or 0), "glide": p.get("glide") is True,
            "tips": p.get("tips") is not False, "join": int(_num(p.get("join")) or 0),
            "fps": _fps(p.get("fps")),
        })  # fmt: skip
    seq, win = int(_num(raw.get("seq")) or 0), int(_num(raw.get("win_ms")) or 0)
    return {"seq": seq, "ts": raw["ts"], "win_ms": win, "srv": out_srv, "pl": players}


def _fps(v: object) -> int | None:
    """v1.2: the Smallworld client mod's FPS report (only players with the mod have one): 0..10 000 or
    None."""
    n = _num(v)
    return None if n is None or n < 0 else min(int(n), 10_000)


@dataclass
class SnapshotReader:
    """New snapshots since the last read, by sequence number; a restarted plugin (seq back at 0, a newer ts)
    is
    noticed and followed."""

    directory: Path = SNAP_DIR
    last_seq: int = -1
    last_ts: int = 0

    def read(self) -> list[dict[str, Any]]:
        try:
            names = [(int(m.group(1)), n) for n in os.listdir(self.directory) if (m := SNAP_NAME.match(n))]
        except OSError:
            return []
        out = []
        for seq, name in sorted(names):
            if seq <= self.last_seq:
                continue
            try:  # minecraft writes this directory: never block on a FIFO, never follow a link
                snap = parse_snapshot(mc_rcon.read_untrusted(self.directory / name, MAX_SNAPSHOT_BYTES))
            except OSError:
                continue
            if snap and snap["ts"] > self.last_ts:
                out.append(snap)
        if not out and names:
            newest = max(names)[0]
            if newest < self.last_seq:  # the plugin restarted: its sequence starts again at 0
                self.last_seq = -1
                return self.read()
        if out:
            self.last_seq, self.last_ts = out[-1]["seq"], out[-1]["ts"]
        return out


# --- minute rows --------------------------------------------------------------------------------------------


def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def _r(v: float | None, nd: int = 2) -> float | None:
    return None if v is None else round(v, nd)


def summarize_server(snaps: list[dict[str, Any]]) -> dict[str, Any]:
    """The minute's tick distribution from its 10 s windows. Percentiles can't be merged exactly from
    summaries:
    p99/p99.9 are the worst window's (an upper bound, never optimistic), p50 the median window's, the mean is
    tick-weighted, max is exact."""
    srv = [s["srv"] for s in snaps]
    ticks = sum(int(x.get("ticks") or 0) for x in srv)

    def vals(k: str) -> list[float]:
        return [x[k] for x in srv if x.get(k) is not None]

    weighted = sum((x.get("mean") or 0) * (x.get("ticks") or 0) for x in srv)
    last_worlds = srv[-1]["worlds"] if srv else []
    out = {
        "ticks": ticks,
        "tps": _r(min(vals("tps"), default=None)),
        "mean": _r(weighted / ticks) if ticks else None,
        "p50": _r(_median(vals("p50"))),
        "p95": _r(max(vals("p95"), default=None)),
        "p99": _r(max(vals("p99"), default=None)),
        "p999": _r(max(vals("p999"), default=None)),
        "max": _r(max(vals("max"), default=None)),
        "spikes": sum(len(x["spikes"]) for x in srv),
        "chunks": sum(w["chunks"] for w in last_worlds),
        "entities": sum(w["entities"] for w in last_worlds),
        "tiles": sum(w["ticking"] for w in last_worlds),
        "worlds": last_worlds,
        "hot": merge_hot(snaps),
        "heap_used_mb": vals("heap_used_mb")[-1] if vals("heap_used_mb") else None,
        "heap_max_mb": vals("heap_max_mb")[-1] if vals("heap_max_mb") else None,
        "self_p99_us": _r(max(vals("self_p99_us"), default=None), 1),
        "self_max_us": _r(max(vals("self_max_us"), default=None), 1),
        "budget_skips": int(sum(vals("budget_skips"))),
        "lost_windows": int(sum(vals("lost_windows"))),
        "samples": len(snaps),
    }
    return out


def summarize_players(snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by: dict[str, dict[str, Any]] = {}
    for s in snaps:
        for p in s["pl"]:
            acc = by.setdefault(p["u"], {"u": p["u"], "pings": [], "fpss": [], "glide": False, "ent": 0})
            acc["pings"].append(p["ping"])
            if p.get("fps") is not None:
                acc["fpss"].append(p["fps"])
            acc["glide"] = acc["glide"] or p["glide"]
            acc["ent"] = max(acc["ent"], p["ent"])
            for k in ("n", "brand", "view", "send", "loc", "w", "cx", "cz", "tips", "join"):
                acc[k] = p[k]
    out = []
    for acc in by.values():
        pings = acc.pop("pings")
        acc["ping"] = round(sum(pings) / len(pings))
        acc["ping_max"] = max(pings)
        acc["pings"] = pings  # the raw 10 s values (the minute's samples)
        acc["samples"] = len(pings)
        fpss = acc.pop("fpss")
        if fpss:  # v1.2: the client's frame rate over the minute (the "server fine / your PC" diagnosis)
            acc["fps"] = round(sum(fpss) / len(fpss))
            acc["fps_min"] = min(fpss)
        out.append(acc)
    return sorted(out, key=lambda p: p["n"].lower())


def lag_events(snaps: list[dict[str, Any]], running: list[str]) -> list[dict[str, Any]]:
    """Spikes the plugin saw, each with what was going on: the busiest worlds, who was near the most entities,
    what of ours was running (backup, restore)."""
    out = []
    for s in snaps:
        if not s["srv"]["spikes"]:
            continue
        near = sorted(s["pl"], key=lambda p: p["ent"], reverse=True)[:LAG_CONTEXT_PLAYERS]
        ctx = {
            "worlds": sorted(
                ({"n": w["n"], "entities": w["entities"], "chunks": w["chunks"]} for w in s["srv"]["worlds"]),
                key=lambda w: w["entities"],
                reverse=True,
            )[:3],
            "near": [
                {
                    "n": p["n"],
                    "u": p["u"],
                    "ent": p["ent"],
                    "w": p["w"],
                    "cx": p["cx"],
                    "cz": p["cz"],
                    "glide": p["glide"],
                }
                for p in near
            ],
            "running": running,
            "players": len(s["pl"]),
        }
        for ts, ms in s["srv"]["spikes"]:
            out.append({"t": ts, "ms": round(ms, 1), "ctx": ctx})
    out.sort(key=lambda e: e["ms"], reverse=True)
    return sorted(out[:MAX_LAG_EVENTS], key=lambda e: e["t"])


SESSION_GAP_MS = 60_000  # snapshots come every 10 s; a longer silence: the server (or the agent) was away


@dataclass
class Sessions:
    """Join/leave events from the online set of consecutive snapshots. The plugin's join time is the session's
    key, so an agent restart (everyone seen again) doesn't open a second session in the Worker.

    P13 review L6: a session never spans a period nobody could play. After a gap (no snapshot for more than
    SESSION_GAP_MS: the server was down or restarting) a player who is gone leaves at the last snapshot they
    were seen in, not when the server came back; a player seen again with a different join time (the server
    restarted and they reconnected) closes the old session at its last-seen time and opens the new one."""

    online: dict[str, dict[str, Any]] = field(default_factory=dict)
    last_ts: int | None = None

    def feed(self, snaps: list[dict[str, Any]]) -> list[dict[str, Any]]:
        events = []
        for s in snaps:
            gap = self.last_ts is not None and s["ts"] - self.last_ts > SESSION_GAP_MS
            now = {p["u"]: p for p in s["pl"]}

            def leave(u: str, p: dict[str, Any], ts: int = s["ts"], gap: bool = gap) -> dict[str, Any]:
                return {
                    "k": "leave",
                    "t": p["_last"] if gap else ts,
                    "u": u,
                    "n": p["n"],
                    "join": p["join"] or p.get("_seen", ts),
                }

            for u, p in self.online.items():
                if u not in now:
                    events.append(leave(u, p))
                elif p["join"] and now[u]["join"] and now[u]["join"] != p["join"]:
                    events.append(leave(u, p, gap=True))  # a new session: the old one ended when last seen
            for u, p in now.items():
                prev = self.online.get(u)
                if prev is None or (prev["join"] and p["join"] and p["join"] != prev["join"]):
                    events.append(
                        {
                            "k": "join",
                            "t": p["join"] or s["ts"],
                            "u": u,
                            "n": p["n"],
                            "join": p["join"] or s["ts"],
                        }
                    )
            same = {u for u in now if u in self.online and self.online[u]["join"] == now[u]["join"]}
            self.online = {
                u: p | {"_seen": self.online[u]["_seen"] if u in same else s["ts"], "_last": s["ts"]}
                for u, p in now.items()
            }
            self.last_ts = s["ts"]
        return events

    def names(self) -> list[str]:
        return sorted(p["n"] for p in self.online.values())


# --- product overhead (systemd cgroup accounting) ----------------------------------------------------------


def parse_show(text: str) -> dict[str, dict[str, int | None]]:
    """`systemctl show -p Id -p CPUUsageNSec ... unit...` output (blocks separated by blank lines)."""
    out: dict[str, dict[str, int | None]] = {}
    for block in text.strip().split("\n\n"):
        props = dict(line.split("=", 1) for line in block.splitlines() if "=" in line)
        unit = props.pop("Id", "")
        if unit:
            out[unit] = {k: int(v) if v.isdigit() else None for k, v in props.items()}
    return out


def unit_stats(
    units: Iterable[str] = OVERHEAD_UNITS, run: Callable[..., Any] = subprocess.run
) -> dict[str, dict[str, int | None]]:
    try:
        out = run(
            ["systemctl", "show", "-p", "Id", "-p", "CPUUsageNSec", "-p", "MemoryCurrent", "-p", "MemoryPeak",
             "-p", "IOReadBytes", "-p", "IOWriteBytes", *units],
            capture_output=True, text=True, timeout=10, check=True,
        ).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return {}
    return parse_show(out)


def consumed_cpu_ms(msg: str) -> tuple[float, float | None] | None:
    """(CPU ms, memory peak MB or None) from systemd's end-of-run line, None if it isn't one."""
    m = CONSUMED.search(msg)
    if not m:
        return None
    minutes, value, unit = m.groups()
    ms = float(value) * (1000.0 if unit == "s" else 1.0) + (int(minutes) * 60000.0 if minutes else 0.0)
    peak = MEMORY_PEAK.search(msg)
    mb = None
    if peak:
        mb = float(peak.group(1)) * {"K": 1 / 1024, "M": 1.0, "G": 1024.0}[peak.group(2)]
    return ms, mb


@dataclass
class Overhead:
    """Per-unit CPU/memory/IO of our own services vs minecraft, per minute: long-running units from their
    cgroup
    counters (deltas), oneshot units (backup, health, ...) from systemd's "Consumed" line when a run ends."""

    prev: dict[str, dict[str, int | None]] = field(default_factory=dict)
    prev_at: float = 0.0
    oneshots: dict[str, dict[str, float]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def observe_journal(self, entries: Iterable[dict[str, Any]]) -> None:
        for e in entries:
            unit = str(e.get("sd_unit") or "")
            if e.get("ident") != "systemd" or not ONESHOT_UNITS.match(unit):
                continue
            got = consumed_cpu_ms(str(e.get("msg", "")))
            if got is None:
                continue
            with self.lock:
                acc = self.oneshots.setdefault(
                    unit.split("@")[0], {"cpu_ms": 0.0, "runs": 0, "mem_peak_mb": 0.0}
                )
                acc["cpu_ms"] += got[0]
                acc["runs"] += 1
                acc["mem_peak_mb"] = max(acc["mem_peak_mb"], got[1] or 0.0)

    def sample(self, stats: dict[str, dict[str, int | None]], now: float) -> dict[str, Any]:
        out: dict[str, Any] = {}
        dt = now - self.prev_at if self.prev_at else 0.0
        for unit, cur in stats.items():
            old = self.prev.get(unit, {})
            mem, peak = cur.get("MemoryCurrent"), cur.get("MemoryPeak")
            row: dict[str, Any] = {
                "mem_mb": round(mem / 1048576, 1) if mem is not None else None,
                "mem_peak_mb": round(peak / 1048576, 1) if peak is not None else None,
            }
            c0, c1 = old.get("CPUUsageNSec"), cur.get("CPUUsageNSec")
            row["cpu_pct"] = (
                round(100.0 * (c1 - c0) / 1e9 / dt, 3)
                if dt > 0 and c0 is not None and c1 is not None and c1 >= c0
                else None
            )
            for k, name in (("IOReadBytes", "io_read_kb"), ("IOWriteBytes", "io_write_kb")):
                a, b = old.get(k), cur.get(k)
                row[name] = round((b - a) / 1024, 1) if a is not None and b is not None and b >= a else None
            out[unit] = row
        with self.lock:
            for unit, acc in self.oneshots.items():
                out[unit] = {
                    "cpu_ms": round(acc["cpu_ms"], 1),
                    "runs": acc["runs"],
                    "mem_peak_mb": round(acc["mem_peak_mb"], 1),
                }
            self.oneshots = {}
        self.prev, self.prev_at = stats, now
        return out


# --- player stats (world/stats/<uuid>.json, vanilla) -------------------------------------------------------

STAT_KEYS = {
    "play_ticks": "minecraft:play_time",
    "deaths": "minecraft:deaths",
    "mob_kills": "minecraft:mob_kills",
    "walk_cm": "minecraft:walk_one_cm",
    "sprint_cm": "minecraft:sprint_one_cm",
    "fly_cm": "minecraft:aviate_one_cm",
    "jumps": "minecraft:jump",
}


def read_stats(path: Path) -> dict[str, int] | None:
    """The few vanilla statistics the panel shows (playtime, deaths, distance), from one player's stats
    file."""
    try:
        data = json.loads(mc_rcon.read_untrusted(path, 4 << 20))
    except (OSError, ValueError):
        return None
    custom = ((data or {}).get("stats") or {}).get("minecraft:custom") or {}
    if not isinstance(custom, dict):
        return None
    return {k: int(custom.get(v, 0)) for k, v in STAT_KEYS.items() if isinstance(custom.get(v, 0), int)}


@dataclass
class StatsReader:
    directory: Path
    mtimes: dict[str, float] = field(default_factory=dict)

    def changed(self, uuids: Iterable[str]) -> dict[str, dict[str, int]]:
        out = {}
        for u in uuids:
            if not UUID.match(u):
                continue
            p = self.directory / f"{u}.json"
            try:
                m = p.stat().st_mtime
            except OSError:
                continue
            if self.mtimes.get(u) == m:
                continue
            stats = read_stats(p)
            if stats is not None:
                self.mtimes[u] = m
                out[u] = stats
        return out


# --- segments (the box's own copy) and the ack cursor ----------------------------------------------------


def segment_bytes(rows: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz:
        gz.write("".join(json.dumps(r, separators=(",", ":")) + "\n" for r in rows).encode())
    return buf.getvalue()


def read_segment(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


@dataclass
class Segments:
    """Append-only gzip NDJSON segments plus their delivery state (state.json in the same directory):
    {name: {"todo": [row indices not yet in D1], "r2": uploaded?}}. A segment is done when todo is empty
    and r2 is true. Retention: older than `days` or over `cap_mb` in total -> deleted, oldest first (done or
    not: the box's copy is bounded; an undelivered one dropped this way is a data gap in the panel)."""

    directory: Path = SEGMENT_DIR
    days: int = 30
    cap_mb: int = 200
    clock: Callable[[], float] = time.time
    lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def state_file(self) -> Path:
        return self.directory / "state.json"

    def load(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}
        return (
            {k: v for k, v in data.items() if SEGMENT_NAME.match(k) and isinstance(v, dict)}
            if isinstance(data, dict)
            else {}
        )

    def save(self, state: dict[str, dict[str, Any]]) -> None:
        tmp = self.state_file.with_name("state.json.tmp")
        tmp.write_text(json.dumps(state, sort_keys=True))
        os.replace(tmp, self.state_file)

    def write(self, rows: list[dict[str, Any]], acked: set[str], src: str) -> str | None:
        """One segment from the buffered rows, in one write; returns its name."""
        if not rows:
            return None
        start = time.strftime("%Y%m%dT%H%M", time.gmtime(rows[0]["ts"] / 1000))
        name = f"{start}-{src}-{rows[0]['seq']}-{rows[-1]['seq']}.ndjson.gz"
        data = segment_bytes(rows)
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            tmp = self.directory / f".{name}.tmp"
            with open(tmp, "wb") as f:
                f.write(data)
            os.replace(tmp, self.directory / name)
            state = self.load()
            state[name] = {
                "todo": [i for i, r in enumerate(rows) if r["id"] not in acked],
                "r2": False,
                "rows": len(rows),
            }
            self.save(state)
            self.prune(state)
        return name

    def mark(self, name: str, *, rows: Iterable[int] = (), r2: bool | None = None) -> None:
        with self.lock:
            state = self.load()
            if name not in state:
                return
            done = set(rows)
            state[name]["todo"] = [i for i in state[name].get("todo", []) if i not in done]
            if r2 is not None:
                state[name]["r2"] = r2
            self.save(state)

    def prune(self, state: dict[str, dict[str, Any]] | None = None) -> list[str]:
        state = self.load() if state is None else state
        files = sorted(p for p in self.directory.glob("*.ndjson.gz") if SEGMENT_NAME.match(p.name))
        cutoff = self.clock() - self.days * 86400
        total = sum(p.stat().st_size for p in files)
        dropped = []
        for p in files:
            too_old = p.stat().st_mtime < cutoff
            if not too_old and total <= self.cap_mb * 1048576:
                break
            total -= p.stat().st_size
            p.unlink(missing_ok=True)
            dropped.append(p.name)
            state.pop(p.name, None)
        for name in list(state):  # state of segments deleted by hand
            if not (self.directory / name).exists():
                state.pop(name)
        if dropped:
            self.save(state)
        return dropped

    def pending(self) -> list[tuple[str, dict[str, Any]]]:
        """Undelivered segments, oldest first."""
        with self.lock:
            state = self.load()
        return sorted((n, s) for n, s in state.items() if s.get("todo") or not s.get("r2"))

    def backlog(self) -> dict[str, Any]:
        pend = self.pending()
        rows = sum(len(s.get("todo", [])) for _, s in pend)
        size = 0
        for n, _ in pend:
            with contextlib.suppress(OSError):
                size += (self.directory / n).stat().st_size
        try:
            total = sum(p.stat().st_size for p in self.directory.glob("*.ndjson.gz"))
            count = len(list(self.directory.glob("*.ndjson.gz")))
        except OSError:
            total = count = 0
        oldest = pend[0][0][:13] if pend else None
        return {"segments": len(pend), "rows": rows, "bytes": size, "oldest": oldest,
                "stored_segments": count, "stored_bytes": total}  # fmt: skip


# --- quiet time + backfill --------------------------------------------------------------------------------


def in_window(window: str, now_local: time.struct_time) -> bool:
    """`HH:MM-HH:MM` local time (may wrap midnight); empty = never."""
    if not window:
        return False
    start, end = window.split("-")
    sh, sm = map(int, start.split(":"))
    eh, em = map(int, end.split(":"))
    t, a, b = now_local.tm_hour * 60 + now_local.tm_min, sh * 60 + sm, eh * 60 + em
    return a <= t < b if a <= b else (t >= a or t < b)


def quiet(
    latest: dict[str, Any] | None, window: str, now_local: time.struct_time, mspt_ok: float = 50.0
) -> bool:
    """Backfill may run: nobody online and the last window healthy (same idea as the reboot gate), or inside
    the
    configured window, but never while anyone is online."""
    if latest is None:
        return in_window(window, now_local)
    if latest["pl"]:
        return False
    p99 = latest["srv"].get("p99")
    healthy = p99 is None or p99 < mspt_ok
    return healthy or in_window(window, now_local)


@dataclass
class Pacer:
    """The backfill's adaptive batch size and back-off, kept across rounds (P13 review M2). A server error
    (5xx: the Worker hit its CPU limit, D1 failed) halves the batch; at one row, or on any other failure
    (network, 4xx), the backfill waits, doubling each time. BACKFILL_GROW_AFTER successes in a row grow a
    halved batch by one row."""

    clock: Callable[[], float] = time.time
    size: int = BACKFILL_BATCH
    failures: int = 0  # consecutive back-offs
    row_failures: int = 0  # consecutive 5xx for the same single row
    successes: int = 0  # requests in a row that went through
    retry_at: float = 0.0

    def ready(self) -> bool:
        return self.clock() >= self.retry_at

    def succeeded(self) -> None:
        self.failures = self.row_failures = 0
        self.successes += 1
        if self.size < BACKFILL_BATCH and self.successes >= BACKFILL_GROW_AFTER:
            self.size, self.successes = self.size + 1, 0

    def failed(self, err: BaseException, batch_len: int) -> bool:
        """Records a failed request; True when the (single) row should be given up."""
        self.successes = 0
        server = bool(SERVER_ERROR.search(str(err)))
        if server and batch_len > 1:
            self.size = max(1, batch_len // 2)  # retry at once with half
            return False
        self.failures += 1
        if server:
            self.row_failures += 1
        wait = min(BACKFILL_BACKOFF_MAX_S, BACKFILL_BACKOFF_S * 2 ** min(self.failures - 1, 10))
        self.retry_at = self.clock() + wait
        if self.row_failures >= BACKFILL_ROW_GIVE_UP:
            self.row_failures = 0
            return True
        return False


@dataclass
class Backfill:
    """Delivers undelivered segments, oldest first, a batch at a time with a pause in between; checks the
    quiet condition before every request (a player joining stops it within one snapshot interval) and saves
    its progress after every batch (a crash resumes where it stopped). A failed request is handled by the
    Pacer (smaller batches, then back-off); it never raises for one."""

    segments: Segments
    send_rows: Callable[[list[dict[str, Any]]], None]
    send_segment: Callable[[str, bytes], None]
    may_run: Callable[[], bool]
    pause_s: float = 2.0
    sleep: Callable[[float], None] = time.sleep
    pacer: Pacer = field(default_factory=Pacer)
    log: Callable[[str], None] = print

    def run_once(self, max_requests: int = 1000) -> dict[str, int]:
        done = {"rows": 0, "segments": 0, "requests": 0, "stopped": 0, "failed": 0, "given_up": 0}

        def stop() -> bool:
            if done["requests"] >= max_requests or not self.may_run() or not self.pacer.ready():
                done["stopped"] = 1
                return True
            return False

        for name, st in self.segments.pending():
            path = self.segments.directory / name
            try:
                rows = read_segment(path)
            except (OSError, ValueError, EOFError):
                self.segments.mark(
                    name, rows=st.get("todo", []), r2=True
                )  # unreadable: give up on it (a gap)
                continue
            todo = [i for i in st.get("todo", []) if i < len(rows)]
            while todo:
                if stop():
                    return done
                batch = todo[: self.pacer.size]
                done["requests"] += 1
                try:
                    self.send_rows([rows[i] | {"backfill": True} for i in batch])
                except Exception as e:
                    done["failed"] += 1
                    if self.pacer.failed(e, len(batch)):
                        self.segments.mark(name, rows=batch)  # a row the Worker can't take: a gap
                        todo = todo[len(batch) :]
                        done["given_up"] += len(batch)
                        self.log(
                            f"backfill: gave up row {rows[batch[0]].get('id')} after repeated server errors"
                        )
                    continue
                self.pacer.succeeded()
                self.segments.mark(name, rows=batch)
                todo = todo[len(batch) :]
                done["rows"] += len(batch)
                self.sleep(self.pause_s)
            if not st.get("r2"):
                if stop():
                    return done
                done["requests"] += 1
                try:
                    self.send_segment(name, path.read_bytes())
                except Exception as e:
                    done["failed"] += 1
                    self.pacer.failed(e, 0)  # never halves (one object); backs off
                    continue
                self.segments.mark(name, r2=True)
                self.sleep(self.pause_s)
            done["segments"] += 1
        return done


# --- verdicts and messages (agent -> plugin, through /run/mc-verdicts) ------------------------------------


def _atomic_text(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, 0o640)  # the directory is setgid minecraft: the plugin (group minecraft) may read it
    os.replace(tmp, path)


def _join(items: object) -> str:
    return "\x1f".join(clean_text(i) for i in (items if isinstance(items, list) else [])[:8] if clean_text(i))


def verdicts_tsv(verdicts: dict[str, Any], now_ms: int) -> str:
    lines = [f"mc-verdicts 1 {now_ms}"]
    srv = verdicts.get("server")
    if isinstance(srv, dict):
        lines.append("\t".join(["S", clean_text(srv.get("verdict"), 8), clean_text(srv.get("headline")),
                                _join(srv.get("reasons")), _join(srv.get("tips"))]))  # fmt: skip
    for u, v in (verdicts.get("players") or {}).items():
        if UUID.match(str(u)) and isinstance(v, dict):
            lines.append("\t".join(["P", u, clean_text(v.get("verdict"), 8), clean_text(v.get("headline")),
                                    _join(v.get("reasons")), _join(v.get("tips"))]))  # fmt: skip
    return "\n".join(lines) + "\n"


def write_verdicts(directory: Path, verdicts: dict[str, Any], now_ms: int) -> None:
    _atomic_text(directory / "verdicts.tsv", verdicts_tsv(verdicts, now_ms))


@dataclass
class Messages:
    """The in-game messages the Worker asked for (signed `player_message` commands), rate-limited again here:
    per player at most one per `per_player_s`, nobody in their first `join_grace_s` online (unless it is the
    join summary), nobody who turned tips off, broadcasts at most one per `broadcast_s`, at most `per_hour` in
    all. Kept in messages.tsv for EXPIRE_S, where the plugin picks each one up once."""

    directory: Path = VERDICT_DIR
    per_player_s: int = 1800
    broadcast_s: int = 1800
    join_grace_s: int = 60
    per_hour: int = 20
    EXPIRE_S = 120
    queue: list[dict[str, Any]] = field(default_factory=list)
    last: dict[str, float] = field(default_factory=dict)
    sent: list[float] = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def check(self, to: str, kind: str, now: float, online: dict[str, dict[str, Any]]) -> str | None:
        """None if allowed, else why not."""
        self.sent = [t for t in self.sent if now - t < 3600]
        if len(self.sent) >= self.per_hour:
            return "hourly message limit reached"
        if to == "*":
            if now - self.last.get("*", 0) < self.broadcast_s:
                return "a broadcast was sent recently"
            return None
        p = online.get(to)
        if p is None:
            return "player isn't online"
        if not p.get("tips", True):
            return "player turned tips off"
        if kind != "join" and p.get("join") and now * 1000 - p["join"] < self.join_grace_s * 1000:
            return "player joined less than a minute ago"
        if now - self.last.get(to, 0) < self.per_player_s:
            return "player got a message recently"
        return None

    def add(self, msg_id: str, to: str, kind: str, text: str, hover: str, now: float) -> None:
        with self.lock:
            self.queue = [m for m in self.queue if m["expires"] > now * 1000]
            self.queue.append({"id": msg_id, "to": to, "expires": int((now + self.EXPIRE_S) * 1000),
                               "text": clean_text(text, 256), "hover": clean_text(hover, 256)})  # fmt: skip
            self.last[to] = now
            self.sent.append(now)
            lines = ["mc-messages 1"] + [
                "\t".join(["M", m["id"], m["to"], str(m["expires"]), m["text"], m["hover"]])
                for m in self.queue
            ]
            _atomic_text(self.directory / "messages.tsv", "\n".join(lines) + "\n")


# --- the loop -----------------------------------------------------------------------------------------------


@dataclass
class Config:
    snap_dir: Path = SNAP_DIR
    verdict_dir: Path = VERDICT_DIR
    segment_dir: Path = SEGMENT_DIR
    stats_dir: Path | None = None
    segment_days: int = 30
    segment_cap_mb: int = 200
    backfill_window: str = ""
    backfill_pause_s: float = 2.0
    read_s: float = 5.0
    segment_s: int = SEGMENT_S

    @classmethod
    def from_env(cls, env: dict[str, str], server_dir: Path, level: str) -> Config:
        window = env.get("BACKFILL_WINDOW", "")
        return cls(
            snap_dir=Path(env.get("TELEMETRY_DIR") or SNAP_DIR),
            verdict_dir=Path(env.get("VERDICT_DIR") or VERDICT_DIR),
            segment_dir=Path(env.get("SEGMENT_DIR") or SEGMENT_DIR),
            stats_dir=server_dir / level / "stats",
            segment_days=max(1, int(env.get("SEGMENT_DAYS") or 30)),
            segment_cap_mb=max(10, int(env.get("SEGMENT_CAP_MB") or 200)),
            backfill_window=window if re.fullmatch(r"(\d\d:\d\d-\d\d:\d\d)?", window) else "",
            backfill_pause_s=max(0.0, float(env.get("BACKFILL_PAUSE_S") or 2.0)),
            segment_s=max(30, int(env.get("SEGMENT_S") or SEGMENT_S)),
        )  # fmt: skip


def load_identity(path: Path) -> tuple[str, int]:
    """This installation's source id and the next row sequence number (persisted)."""
    try:
        data = json.loads(path.read_text())
        src, seq = str(data["src"]), int(data["seq"])
        if re.fullmatch(r"[0-9a-f]{8}", src):
            return src, seq
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return secrets.token_hex(4), 0


def save_identity(path: Path, src: str, seq: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"src": src, "seq": seq}))
    os.replace(tmp, path)


class Telemetry:
    """Owns the snapshot reader, the minute aggregation, the RAM buffer, segments, backfill and the verdict
    cache.
    `post` sends JSON to the Worker and returns its reply (raises on failure); `put` uploads bytes."""

    def __init__(
        self,
        cfg: Config,
        state_dir: Path,
        post: Callable[[str, object], Any],
        put: Callable[[str, bytes], Any],
        running: Callable[[], list[str]] = list,
        clock: Callable[[], float] = time.time,
        stats: Callable[[], dict[str, dict[str, int | None]]] = unit_stats,
        extra: Callable[[], dict[str, Any]] = dict,
        messages: Messages | None = None,
    ) -> None:
        self.cfg = cfg
        self.post = post
        self.put = put
        self.running = running
        self.clock = clock
        self.stats = stats
        self.extra = extra
        self.reader = SnapshotReader(cfg.snap_dir)
        self.sessions = Sessions()
        self.overhead = Overhead()
        self.stats_reader = StatsReader(cfg.stats_dir) if cfg.stats_dir else None
        self.segments = Segments(cfg.segment_dir, cfg.segment_days, cfg.segment_cap_mb, clock)
        self.messages = messages or Messages(cfg.verdict_dir)
        self.id_file = state_dir / "telemetry-id.json"
        self.src, self.seq = load_identity(self.id_file)
        self.minute_snaps: list[dict[str, Any]] = []
        self.minute: int | None = None
        self.pending_events: list[dict[str, Any]] = []
        self.buffer: list[dict[str, Any]] = []
        self.acked: set[str] = set()
        self.segment_start = clock()
        self.latest: dict[str, Any] | None = None
        self.last_stats = 0.0
        self.stop = threading.Event()
        self.ok = True
        self.pacer = Pacer(clock=clock)

    def online(self) -> dict[str, dict[str, Any]]:
        """Who is online by the latest snapshot. Lock-free (P13 review L4): `latest` is replaced, never
        changed in place, so another thread (a player_message command) reads a consistent snapshot without
        waiting for this thread's file reads or HTTP."""
        latest = self.latest
        return {p["u"]: p for p in (latest or {}).get("pl", [])}

    # one tick of the loop: read snapshots; when a minute closed, build + ship its row
    def poll(self) -> dict[str, Any] | None:
        snaps = self.reader.read()
        row = None
        for s in snaps:
            self.latest = s
            minute = s["ts"] // MINUTE_MS * MINUTE_MS
            if self.minute is not None and minute != self.minute and self.minute_snaps:
                row = self.close_minute()
            self.minute = minute
            self.minute_snaps.append(s)
            self.pending_events += self.sessions.feed([s])
        if row is None and self.minute_snaps and self.clock() * 1000 - self.minute >= MINUTE_MS + 15000:
            row = self.close_minute()  # the plugin went quiet (server stopped): close the minute anyway
        if self.clock() - self.segment_start >= self.cfg.segment_s:
            self.flush_segment()
        return row

    def close_minute(self) -> dict[str, Any]:
        snaps, self.minute_snaps = self.minute_snaps, []
        assert self.minute is not None
        running = self.running()
        row: dict[str, Any] = {
            "v": 1, "id": f"{self.src}:{self.seq}", "src": self.src, "seq": self.seq, "ts": self.minute,
            "srv": summarize_server(snaps), "pl": summarize_players(snaps),
            "raw": [{"ts": s["ts"], "tps": s["srv"]["tps"], "p50": s["srv"]["p50"], "p99": s["srv"]["p99"],
                     "p999": s["srv"]["p999"], "max": s["srv"]["max"],
                     "pl": {p["u"]: [p["ping"], p["ent"]] for p in s["pl"]}} for s in snaps],
            "ev": self.pending_events, "lag": lag_events(snaps, running), "running": running,
            "ovh": self.overhead.sample(self.stats(), self.clock()), "backlog": self.segments.backlog(),
        }  # fmt: skip
        row["srv"] |= self.extra()  # the agent's GC/heap readings
        self.pending_events = []
        if self.stats_reader and (
            any(e["k"] == "leave" for e in row["ev"]) or self.clock() - self.last_stats > 3600
        ):
            self.last_stats = self.clock()
            row["stats"] = self.stats_reader.changed(self.online().keys() | {e["u"] for e in row["ev"]})
        self.seq += 1
        save_identity(self.id_file, self.src, self.seq)
        self.buffer.append(row)
        self.ship_live(row)
        return row

    def ship_live(self, row: dict[str, Any]) -> None:
        try:
            reply = self.post("/ingest/experience", {"agent": "1", "live": True, "rows": [row]})
        except Exception:  # the Worker is unreachable: the row waits in the segment for backfill
            self.ok = False
            return
        self.ok = True
        self.acked.add(row["id"])
        verdicts = reply.get("verdicts") if isinstance(reply, dict) else None
        if isinstance(verdicts, dict):
            with contextlib.suppress(OSError):
                write_verdicts(self.cfg.verdict_dir, verdicts, int(self.clock() * 1000))

    def flush_segment(self) -> str | None:
        rows, self.buffer = self.buffer, []
        self.segment_start = self.clock()
        name = self.segments.write(rows, self.acked, self.src)
        self.acked -= {r["id"] for r in rows}
        if name and self.ok:
            try:
                self.put(name, (self.segments.directory / name).read_bytes())
                self.segments.mark(name, r2=True)
            except Exception:
                self.ok = False
        return name

    def may_backfill(self) -> bool:
        """Quiet time by the latest snapshot (the loop reads one every few seconds between backfill
        batches)."""
        return not self.stop.is_set() and quiet(
            self.latest, self.cfg.backfill_window, time.localtime(self.clock())
        )

    def backfill(self, max_requests: int = 4) -> dict[str, int]:
        """A few backfill requests (the loop calls it between snapshot reads, so a join stops it quickly)."""
        bf = Backfill(
            self.segments,
            send_rows=lambda rows: self.post(
                "/ingest/experience", {"agent": "1", "live": False, "rows": rows}
            ),
            send_segment=self.put,
            may_run=self.may_backfill,
            pause_s=self.cfg.backfill_pause_s,
            sleep=self.stop.wait,
            pacer=self.pacer,
            log=lambda m: print(m, flush=True),
        )
        return bf.run_once(max_requests)

    def run(self) -> None:
        """The agent's telemetry thread: read snapshots every few seconds, ship minute rows and segments, and
        backfill in quiet time while the Worker answers."""
        while not self.stop.is_set():
            try:
                self.poll()  # no lock held: online() is lock-free, nothing else waits on this thread
                if self.ok and self.pacer.ready() and self.segments.pending() and self.may_backfill():
                    self.backfill()
            except Exception as e:  # one bad round must not end the loop
                self.ok = False
                print(f"telemetry round failed: {type(e).__name__}: {e}", flush=True)
            self.stop.wait(self.cfg.read_s)
