#!/usr/bin/env python3
"""Periodic health check and self-heal (systemd timer, every 5 min).

- Liveness = RCON answers (a TCP connect and a reply to the login, even a refused one). MSPT and TPS (Paper's
  `mspt` and `tps` commands) are read on top; a reply that can't be parsed, or a refused password, is a
  warning in the journal and never makes a running server look dead.
- Reads GC pauses and post-GC heap occupancy from the JVM's unified GC log (/run/mc-telemetry/gc.log, P13).
- Heartbeats the Cloudflare watchdog's "health" check (design §5): success while healthy or while a
  self-heal is under way, /fail when it alerts (server down and not healed, MSPT above the threshold for N
  consecutive checks). The watchdog alerts when heartbeats stop, and separately pings the server from outside.
- Self-heal (design §6): server down and not admin-stopped -> `systemctl restart minecraft.service` (a polkit
  rule allows exactly that to the ops user health.py runs as). Process up but RCON dead for 2 checks (after a
  start-up grace, HEAL_STARTUP_GRACE_S) -> a thread dump (`systemctl kill --signal=SIGQUIT`: the JVM prints it
  to its stdout, i.e. the journal; health.py isn't the JVM's user, so it asks systemd), then the restart. No
  alert for a heal that works; an alert when it doesn't (still down at the next check), when more than 2 heals
  happen in 24 h, and when it gives up (at most MAX_HEALS_PER_DAY restarts in 24 h). Never during a
  maintenance window (deploys, restores: /var/lib/minecraft/maintenance.json), while restore.py holds its
  lock, while the unit is starting or stopping (or its state can't be read), or when
  /var/lib/minecraft/admin-stopped exists. A crash loop (systemd restarting the unit again and again, or its
  start limit hit) is reported instead of being left at "starting". Every heal is logged (`AUDIT ...`).
- The heartbeat carries the admin-stopped marker (JSON body), so the watchdog doesn't alert on a server
  that was stopped on purpose. Its reply carries the age of the watchdog's last cron run: if that cron looks
  stale (> 15 min), or the watchdog can't be reached for 3 checks, health.py alerts on Discord directly.
- Host checks (Discord only, never fail the heartbeat): disk usage of the server dir's filesystem and CPU
  steal from /proc/stat deltas between runs (noisy neighbours on a shared-vCPU VPS). Pending reboots
  (/var/run/reboot-required) are mc-reboot-gate's (P11: it reboots when nobody is online and warns after 3
  days), so health.py only logs the flag.
- Posts state changes (and resize recommendations) to Discord if a webhook is configured.
- Chunky pre-generation (PREGEN_WORLDS, e.g. "world,world_nether,world_the_end": the dimensions that have a
  border, in that order): one world at a time up to its vanilla world border (`chunky world W`, `chunky
  worldborder`, `chunky start`), and only while nobody is online: a player joins -> `chunky pause`; empty
  again -> `chunky continue`. A task Chunky has nothing left to continue for is done; then the next world
  starts. Chunky's own `continue-on-restart: true` resumes a task after a restart (and the next check pauses
  it if players are on). Progress lives in the state file; the replies matched are Chunky 1.5's lang/en.json
  strings. v1.4: each check records the running task's percentage from `chunky progress` and writes one line
  for `mcctl status` to pregen.json next to state.json; one Discord note when every bordered pass is done.
  While backup.py holds pre-generation (notify.py pregen-hold.json: it paused Chunky for its save-off window
  and continues it itself) nothing is done. Restarts need nothing: RCON is down (nothing is done) and Chunky's
  continue-on-restart resumes the task; a check that finds it running with players online pauses it again.
- v1.4 frontier expansion (PREGEN_FRONTIER: every dimension, "world:overworld:5000,world_the_end:the_end:",
  border empty = none; empty = off): once the bordered passes are done, the same idle-only loop keeps
  generating ahead of where players have been. Target radius (square, around 0,0 where the border is centred)
  = the farthest generated chunk, read from the region files' names and 4 KiB headers, + PREGEN_MARGIN, capped
  at the border and PREGEN_MAX_RADIUS; a new task (`chunky start W square 0 0 R`: Chunky skips chunks that
  exist, so only the new ring is generated) only when the target grew by FRONTIER_STEP blocks or reaches the
  cap, and never while the disk is at DISK_ALERT_PCT.

- Alerts (docs/operations.md "Alerts"): one plain-English template (scripts/alerts.py: headline, impact,
  likely cause, what to do, details) with what we know right now (players online, pre-generation running, the
  collector). Recurring conditions (GC freezes, lag, memory, disk, CPU steal, the watchdog) go through
  alerts.Gate: one message when they start, at most one reminder per REPEAT_HOURS while they persist (with the
  occurrences folded into it), one "recovered" message when they clear; its state is in state.json, so it
  survives restarts. GC: G1 pauses or ZGC allocation stalls longer than GC_PAUSE_ALERT_MS (default 50 ms =
  one tick's budget); ZGC's own pauses are sub-millisecond.

Env: WATCHDOG_URL, HEARTBEAT_TOKEN, DISCORD_WEBHOOK_URL, MSPT_ALERT (45), MSPT_ALERT_CHECKS (3),
     HEAP_ALERT_PCT (85), HEAP_ALERT_CHECKS (6), GC_PAUSE_ALERT_MS (50), REPEAT_HOURS (6),
     DISK_ALERT_PCT (85),
     STEAL_ALERT_PCT (10), STEAL_ALERT_CHECKS (3), HEAL_STARTUP_GRACE_S (900), PREGEN_WORLDS (empty = off),
     PREGEN_FRONTIER (empty = off), PREGEN_MARGIN (1024), PREGEN_MAX_RADIUS (10000),
     STATE_DIRECTORY (set by systemd).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

import mc_rcon
from alerts import Alert, Gate, Verdict, duration, reminder_note, render
from notify import (
    ADMIN_STOPPED_FILE,
    admin_stopped,
    discord,
    fenced,
    heartbeat_reply,
    maintenance_active,
    pregen_held,
    restore_running,
)

TRIPLE = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
FLOAT = re.compile(r"\*?(\d+(?:\.\d+)?)")
# G1 (-Xlog:gc), e.g.
#   [..][123.4s][info][gc] GC(12) Pause Young (Normal) (G1 Evacuation Pause) 3000M->1200M(6656M) 12.3ms
GC_LINE = re.compile(
    r"\[(?P<uptime>\d+(?:\.\d+)?)s\].*?GC\(\d+\) (?P<kind>Pause .*?) "
    r"(?P<before>\d+)M->(?P<after>\d+)M\((?P<cap>\d+)M\) (?P<ms>\d+(?:\.\d+)?)ms"
)
# P16: Generational ZGC (-Xlog:gc,gc+phases; Temurin 25). Pauses are gc+phases lines per generation (Y/y =
# young of a major/minor collection, O = old), allocation stalls (a thread waiting for memory: as bad as a
# pause for that thread) are `gc` lines, the heap is on the collection's summary line, as M(% of capacity):
#   [..][12.5s][info][gc,phases] GC(7) y: Pause Mark Start 0.013ms
#   [..][12.6s][info][gc       ] Allocation Stall (Server thread) 3.120ms
#   [..][12.7s][info][gc       ] GC(7) Minor Collection (Allocation Rate) 416M(81%)->362M(71%) 0.006s
ZGC_PAUSE = re.compile(
    r"\[(?P<uptime>\d+(?:\.\d+)?)s\].*?(?:GC\(\d+\) [YyOo]: (?P<kind>Pause [A-Za-z ()]+?)|"
    r"(?P<stall>Allocation Stall) \(.*\)) (?P<ms>\d+(?:\.\d+)?)ms\s*$"
)
ZGC_CYCLE = re.compile(
    r"\[(?P<uptime>\d+(?:\.\d+)?)s\].*?GC\(\d+\) (?:Minor|Major) Collection \(.*?\) "
    r"(?P<before>\d+)M\((?P<before_pct>\d+)%\)->(?P<after>\d+)M\((?P<after_pct>\d+)%\) \d+(?:\.\d+)?s"
)
REBOOT_REQUIRED = Path("/var/run/reboot-required")
ADMIN_STOPPED = ADMIN_STOPPED_FILE
UNIT = "minecraft.service"
MAX_HEALS_PER_DAY = 3  # restarts within 24 h before self-heal gives up (alerts from the 3rd on)
ALERT_HEALS = 2  # more heals than this in 24 h -> alert even though they worked
RCON_DEAD_CHECKS = 2  # process up but RCON dead this many checks in a row -> dump + restart
STARTUP_GRACE_S = 900  # a server active for less than this may still be starting (HEAL_STARTUP_GRACE_S)
CRASH_LOOP_RESTARTS = 2  # systemd restarted the unit this often since the previous check -> crash loop
WATCHDOG_STALE_S = 900  # the watchdog's cron runs every 5 min; older than this = it stopped
WATCHDOG_UNREACHABLE_CHECKS = 3  # heartbeats not delivered this many checks in a row -> alert
DAY_S = 86400
# Default for resize alerts; the deploy sets RESIZE_HINT in health.env from server.yml host.resize_hint.
RESIZE_HINT = "a host with dedicated cores or more RAM"


def resize_hint() -> str:
    return os.environ.get("RESIZE_HINT") or RESIZE_HINT


def resize_to() -> str:
    """The hint after "move to": host.resize_hint may or may not start with an article."""
    hint = resize_hint()
    return hint if re.match(r"(?i)(?:a|an|the)\s", hint) else f"a {hint}"


def env_float(name: str, default: float) -> float:
    return float(os.environ.get(name) or default)


@dataclass
class State:
    status: str = "unknown"  # ok | degraded | down
    mspt_high: int = 0
    heap_high: int = 0
    heap_alerted: bool = False
    last_gc_uptime: float = 0.0
    disk_alerted: bool = False
    reboot_notice_day: str = ""
    cpu_prev: list[int] = field(default_factory=list)
    steal_high: int = 0
    steal_alerted: bool = False
    # self-heal
    heals: list[float] = field(default_factory=list)  # epoch seconds of restarts by self-heal
    rcon_dead: int = 0  # consecutive checks with the process up but RCON dead
    heal_pending: bool = False  # restarted at the previous check; not confirmed up yet
    heal_failed_alerted: bool = False
    gave_up_alerted: bool = False
    down_alerted: bool = False  # a down alert went out: the recovery is announced too
    n_restarts_prev: int | None = None  # systemd NRestarts at the previous check with RCON dead
    crash_loop_alerted: bool = False
    # the watchdog itself
    watchdog_stale_alerted: bool = False
    watchdog_unreachable: int = 0
    watchdog_unreachable_alerted: bool = False
    # Chunky pre-generation: worlds finished, the one started last (None = none yet), paused for players
    pregen_done: list[str] = field(default_factory=list)
    pregen_current: str | None = None
    pregen_paused: bool = False
    pregen_for: str = ""  # v1.2 world library: the world (name@since) the progress above belongs to
    # v1.4: each bordered pass's last progress ({pct, chunks} from `chunky progress`), frontier expansion: the
    # radius generated around 0,0 per world, and the ring task running now (world, its radius, percent)
    pregen_progress: dict[str, dict[str, float]] = field(default_factory=dict)
    pregen_frontier: dict[str, int] = field(default_factory=dict)
    pregen_frontier_current: str | None = None
    pregen_frontier_target: int = 0
    pregen_frontier_pct: float | None = None
    # alerts.Gate: key -> when it started, was last sent, ... (one message, reminders, one recovery)
    alerts: dict[str, dict[str, object]] = field(default_factory=dict)
    down_since: float | None = None


@dataclass
class Ctx:
    """What we know right now, for alerts that name a specific cause (all optional: unknown = not said)."""

    now: float = 0.0
    players: int | None = None
    pregen: str | None = None  # the world being pre-generated (running, not paused)
    backup: bool = False
    repeat_s: float = 6 * 3600
    log_hint: str = ""  # the last error line of the server log (scrubbed), for a down alert

    def online(self) -> str:
        if self.players is None:
            return ""
        if self.players == 0:
            return "Nobody is online right now."
        return f"{self.players} player(s) online right now."

    def busy(self) -> str:
        parts = [f"chunk pre-generation is running ({self.pregen})"] if self.pregen else []
        if self.backup:
            parts.append("a backup is running")
        return " and ".join(parts)

    def busy_noun(self) -> str:
        return " and ".join(
            x
            for x in ("chunk pre-generation" if self.pregen else "", "the backup" if self.backup else "")
            if x
        )


TICK_MS = 50.0  # one tick's budget at 20 TPS
GC_CLEAR_S = 1800  # no GC freeze for this long = recovered
DOWN_HEADLINE = "The Minecraft server is down"
BACK_HEADLINE = "The Minecraft server is back"


@dataclass
class HostStats:
    disk_pct: float | None = None
    reboot_required: bool = False
    cpu: list[int] = field(default_factory=list)  # aggregate `cpu` counters from /proc/stat


@dataclass
class GcStats:
    max_pause_ms: float = 0.0
    long_pauses: list[float] = field(default_factory=list)
    heap_after_pct: float | None = None
    last_uptime: float = 0.0
    collector: str = ""  # g1 | zgc (from the log's line format), "" = no event seen
    stalls: list[float] = field(default_factory=list)  # ZGC allocation stalls (ms) since the last check

    def freeze_ms(self) -> float:
        """The longest time the server stood still: a pause, or (ZGC) all stalls of the check together."""
        return max(self.max_pause_ms, sum(self.stalls))


def parse_mspt(text: str) -> float:
    """1-minute average MSPT from `mspt` output (5s, 10s, 1m triples of avg/min/max)."""
    triples = TRIPLE.findall(mc_rcon.strip_formatting(text))
    if not triples:
        raise ValueError(f"unrecognised mspt output: {text!r}")
    return float(triples[-1][0])


def parse_tps(text: str) -> float:
    """1-minute TPS from `tps` output.

    Purpur: 'TPS from last 5s, 1m, 5m, 15m: 20.0, 20.0, ...'; Paper: 'TPS from last 1m, 5m, 15m: ...'.
    """
    head, sep, tail = mc_rcon.strip_formatting(text).partition(":")
    labels = [w.strip() for w in head.rsplit("last", 1)[-1].split(",")]
    values = [float(v) for v in FLOAT.findall(tail)]
    if not sep or "1m" not in labels or len(values) != len(labels):
        raise ValueError(f"unrecognised tps output: {text!r}")
    return values[labels.index("1m")]


@dataclass(frozen=True)
class GcEvent:
    """One line of the GC log that matters: a pause (ms) and/or the heap after a collection (MB, % of the
    capacity)."""

    uptime: float
    pause_ms: float | None = None
    after_mb: int | None = None
    after_pct: float | None = None
    cap_mb: int | None = None
    stall: bool = False
    zgc: bool = False


def gc_event(line: str) -> GcEvent | None:
    """G1 (P13) or ZGC (P16) line -> event; None for every other line."""
    if m := GC_LINE.search(line):
        after, cap = int(m["after"]), int(m["cap"])
        return GcEvent(float(m["uptime"]), float(m["ms"]), after, 100.0 * after / cap if cap else None, cap)
    if m := ZGC_PAUSE.search(line):
        return GcEvent(float(m["uptime"]), pause_ms=float(m["ms"]), stall=bool(m["stall"]), zgc=True)
    if m := ZGC_CYCLE.search(line):
        # ZGC prints no capacity: estimate it from the larger of the two (more precise) M/% pairs.
        pairs = ((int(m["before"]), int(m["before_pct"])), (int(m["after"]), int(m["after_pct"])))
        mb, pct = max(pairs, key=lambda x: x[1])
        cap = round(mb * 100 / pct) if pct else None
        return GcEvent(
            float(m["uptime"]),
            after_mb=int(m["after"]),
            after_pct=float(m["after_pct"]),
            cap_mb=cap,
            zgc=True,
        )
    return None


def gc_events(lines: list[str]) -> list[GcEvent]:
    return [e for e in map(gc_event, lines) if e]


def parse_gc(lines: list[str], since_uptime: float, pause_alert_ms: float) -> GcStats:
    events = gc_events(lines)
    if events and events[-1].uptime < since_uptime:
        since_uptime = 0.0  # JVM restarted (uptime reset): everything in the log is new
    stats = GcStats(last_uptime=since_uptime)
    afters: list[float] = []
    if events:
        stats.collector = "zgc" if any(e.zgc for e in events) else "g1"
    for e in events:
        if e.after_pct is not None:
            afters.append(e.after_pct)
        if e.uptime <= since_uptime:
            continue
        stats.last_uptime = max(stats.last_uptime, e.uptime)
        if e.pause_ms is None:
            continue
        if e.stall:
            stats.stalls.append(e.pause_ms)
            continue
        stats.max_pause_ms = max(stats.max_pause_ms, e.pause_ms)
        if e.pause_ms > pause_alert_ms:
            stats.long_pauses.append(e.pause_ms)
    if afters:
        # Minimum post-GC occupancy over the recent window approximates the live set.
        stats.heap_after_pct = min(afters[-10:])
    return stats


def last_heap(lines: list[str]) -> tuple[int, int] | None:
    """(heap used after the last collection, heap capacity) in MB from the GC log. G1 states the capacity on
    every line; for ZGC it is the median of the recent collections' estimates (each within ~1 %)."""
    heaps = [e for e in gc_events(lines) if e.after_mb is not None and e.cap_mb]
    if not heaps:
        return None
    last = heaps[-1]
    if last.pause_ms is not None:  # G1
        return last.after_mb or 0, last.cap_mb or 0
    caps = sorted(e.cap_mb or 0 for e in heaps[-20:] if e.pause_ms is None)
    return last.after_mb or 0, caps[len(caps) // 2]


# P13: the JVM logs GC to tmpfs (minecraft.service RuntimeDirectory); logs/gc.log on older hosts.
GC_LOG_TMPFS = Path("/run/mc-telemetry/gc.log")
GC_LOG_TAIL_BYTES = 1 << 20


def read_gc_log(server_dir: Path, max_lines: int = 2000) -> list[str]:
    """The GC log's last lines. The JVM (minecraft) writes it: read with mc_rcon.read_untrusted (no FIFO, no
    symlink, the last 1 MiB at most; P13 review L4)."""
    path = GC_LOG_TMPFS if GC_LOG_TMPFS.exists() else server_dir / "logs" / "gc.log"
    try:
        data = mc_rcon.read_untrusted(path, GC_LOG_TAIL_BYTES, tail=True)
    except OSError:
        return []
    return data.decode("utf-8", "replace").splitlines(keepends=True)[-max_lines:]


def load_state(path: Path) -> State:
    try:
        return State(**json.loads(path.read_text()))
    except (FileNotFoundError, ValueError, TypeError):
        return State()


def ticks(ms: float) -> str:
    n = max(1, round(ms / TICK_MS))
    return f"~{n} tick" + ("s" if n > 1 else "")


def gc_alert(v: Verdict, gc: GcStats, cfg: dict[str, float], ctx: Ctx) -> str:
    if v.kind == "recovered":
        return render(
            Alert(
                "recovered",
                "The short freezes from Java memory management have stopped",
                impact=f"No freeze over {cfg['gc_pause_ms']:.0f} ms for {duration(GC_CLEAR_S)}.",
                details=f"they went on for {duration(v.bad_for_s)}; worst {v.worst or 0:.0f} ms",
            )
        )
    zgc = gc.collector == "zgc"
    worst = gc.freeze_ms()
    busy = ctx.busy()
    if zgc:
        cause = (
            f"the server ran out of free memory faster than ZGC could free it: {len(gc.stalls)} "
            f"allocation stall(s), {sum(gc.stalls):.0f} ms in total"
        )
        fix = (
            "give the JVM more heap if the box has free RAM (`mcctl config set jvm.heap_mb <MB>`, push) or "
            f"move to {resize_to()}"
        )
    else:
        cause = f"Java garbage collection (G1) stopped the server for up to {gc.max_pause_ms:.0f} ms"
        fix = "switch to ZGC (`mcctl config set jvm.gc zgc`, push: sub-millisecond pauses)"
    if gc.heap_after_pct is not None and gc.heap_after_pct >= cfg["heap_pct"]:
        cause += f"; the heap is still {gc.heap_after_pct:.0f}% full after collection (short on memory)"
    if busy:
        cause += f" while {busy}"
    todo = (
        f"Usually nothing: it clears when {ctx.busy_noun()} finishes. If it keeps happening: {fix}."
        if busy
        else f"If it keeps happening: {fix}."
    )
    n = len(gc.stalls) if zgc else len(gc.long_pauses)
    details = f"{n} {'stall(s)' if zgc else 'pause(s)'} this check, longest freeze {worst:.0f} ms"
    details += f" (alert above {cfg['gc_pause_ms']:.0f} ms); collector {gc.collector or 'unknown'}"
    if note := reminder_note(v):
        details += f"; {note}"
    return render(
        Alert(
            "warning",
            "Short freezes: Java memory management paused the server",
            impact=f"Players would notice freezes of up to {worst:.0f} ms ({ticks(worst)}). "
            f"{ctx.online()}".strip(),
            cause=cause[0].upper() + cause[1:] + ".",
            todo=todo,
            details=details,
        )
    )


def lag_alert(v: Verdict, mspt: float | None, tps: float | None, cfg: dict[str, float], ctx: Ctx) -> str:
    if v.kind == "recovered":
        return render(
            Alert(
                "recovered",
                "The server runs at full speed again",
                impact=f"Ticks are back under {cfg['mspt_alert']:.0f} ms.",
                details=f"it lagged for {duration(v.bad_for_s)}; worst {v.worst or 0:.1f} ms per tick; "
                f"now {mspt if mspt is not None else '?'} ms, TPS {tps if tps is not None else '?'}",
            )
        )
    busy = ctx.busy()
    cause = (
        f"{busy[0].upper() + busy[1:]}."
        if busy
        else "Something costs more than the 50 ms each tick may take: usually a big farm or many entities in "
        "one place, or new chunks being generated by fast exploration."
    )
    details = (
        f"{mspt:.1f} ms per tick (alert above {cfg['mspt_alert']:.0f} ms) "
        f"for {cfg['mspt_checks']:.0f}+ checks, TPS {tps}"
    )
    if note := reminder_note(v):
        details += f"; {note}"
    return render(
        Alert(
            "warning",
            "The server is lagging: ticks take longer than they should",
            impact=f"The game runs slower for everyone (TPS {tps}; 20 is full speed). {ctx.online()}".strip(),
            cause=cause,
            todo="Look at the panel's Experience tab (Busiest chunks, lag events). To see what costs the "
            "time, "
            "run `spark profiler start --timeout 120` in the console. "
            f"If it never clears, consider {resize_to()}.",
            details=details,
        )
    )


def heap_alert(v: Verdict, gc: GcStats, cfg: dict[str, float], ctx: Ctx) -> str:
    if v.kind == "recovered":
        return render(
            Alert(
                "recovered",
                "Memory pressure is over",
                impact=f"After garbage collection the heap is below {cfg['heap_pct']:.0f}% again.",
                details=f"it was high for {duration(v.bad_for_s)}; worst {v.worst or 0:.0f}%",
            )
        )
    details = (
        f"{gc.heap_after_pct:.0f}% of the heap in use after collection (alert above {cfg['heap_pct']:.0f}%)"
    )
    if note := reminder_note(v):
        details += f"; {note}"
    return render(
        Alert(
            "warning",
            "The server's memory is nearly full",
            impact="Nothing visible yet; if it grows, players get freezes and the server may crash. "
            + ctx.online(),
            cause="The world, the entities and the plugins need almost all of the Java heap, even after "
            "cleaning up.",
            todo="Raise the heap if the box has free RAM (`mcctl config set jvm.heap_mb <MB>`, push), or "
            "plan a "
            f"move to {resize_to()} (README: move to a new VPS).",
            details=details,
        )
    )


def gate_of(new: State, ctx: Ctx) -> Gate:
    return Gate(new.alerts, ctx.repeat_s)


def evaluate(
    state: State,
    alive: bool | None,
    mspt: float | None,
    tps: float | None,
    gc: GcStats,
    cfg: dict[str, float],
    ctx: Ctx | None = None,
) -> tuple[State, str, list[str]]:
    """Pure decision logic. Returns (new state, heartbeat status '' or 'fail', messages).

    alive: RCON answered (True), didn't (False), or couldn't be checked (None: e.g. server.properties
    unreadable; changes nothing). mspt/tps None while alive = the reply couldn't be parsed (a warning only).
    """
    ctx = ctx or Ctx()
    msgs: list[str] = []
    new = State(**asdict(state))
    new.last_gc_uptime = gc.last_uptime
    gate = gate_of(new, ctx)

    if alive is False:
        new.status, new.mspt_high = "down", 0
        if state.status != "down":
            new.down_since = ctx.now
            msgs.append(down_alert(ctx))
        return new, "fail", msgs
    if alive is None:
        return new, "fail" if state.status == "degraded" else "", msgs

    if state.status == "down":
        msgs.append(back_alert(state, ctx, mspt, tps))
        new.down_since = None
    if mspt is None:  # alive, but no usable MSPT this time: keep the MSPT verdict as it was
        degraded = state.status == "degraded"
        new.status = "degraded" if degraded else "ok"
    else:
        new.mspt_high = state.mspt_high + 1 if mspt > cfg["mspt_alert"] else 0
        degraded = new.mspt_high >= cfg["mspt_checks"]
        new.status = "degraded" if degraded else "ok"
        if v := gate.check("lag", degraded, ctx.now, mspt):
            msgs.append(lag_alert(v, mspt, tps, cfg, ctx))

    freeze = gc.freeze_ms()
    if v := gate.check("gc", freeze > cfg["gc_pause_ms"], ctx.now, freeze, clear_after_s=GC_CLEAR_S):
        msgs.append(gc_alert(v, gc, cfg, ctx))

    if gc.heap_after_pct is not None and gc.heap_after_pct > cfg["heap_pct"]:
        new.heap_high = state.heap_high + 1
    else:
        new.heap_high = 0
    high = new.heap_high >= cfg["heap_checks"]
    if gc.heap_after_pct is not None or not high:
        if v := gate.check("heap", high, ctx.now, gc.heap_after_pct):
            msgs.append(heap_alert(v, gc, cfg, ctx))
    new.heap_alerted = "heap" in new.alerts
    return new, "fail" if degraded else "", msgs


def down_alert(ctx: Ctx) -> str:
    return render(
        Alert(
            "action",
            DOWN_HEADLINE,
            impact="Nobody can join or play.",
            cause="It doesn't answer on its console port (RCON)"
            + (f"; the server log last said: {ctx.log_hint}" if ctx.log_hint else "")
            + ".",
            todo="Usually nothing: self-heal restarts it within a few minutes. If it doesn't come back: "
            "`mcctl "
            "doctor`, then `journalctl -u minecraft -n 100` on the box.",
            details="RCON unreachable",
        )
    )


def back_alert(state: State, ctx: Ctx, mspt: float | None, tps: float | None) -> str:
    gone = f" after {duration(ctx.now - state.down_since)}" if state.down_since and ctx.now else ""
    return render(
        Alert(
            "recovered",
            f"{BACK_HEADLINE}{gone}",
            impact="Players can join again. " + ctx.online(),
            details=f"RCON answers; MSPT {mspt if mspt is not None else 'n/a'}, "
            f"TPS {tps if tps is not None else 'n/a'}",
        )
    )


def parse_proc_stat(text: str) -> list[int]:
    """Aggregate CPU counters (user nice system idle iowait irq softirq steal ...) from /proc/stat."""
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0] == "cpu":
            return [int(x) for x in parts[1:]]
    raise ValueError("no aggregate cpu line in /proc/stat")


def steal_pct(prev: list[int], cur: list[int]) -> float | None:
    """CPU steal % between two samples; None without a usable previous sample (first run, reboot)."""
    if len(prev) < 8 or len(cur) < 8:
        return None
    # guest/guest_nice (fields 9-10) are already counted in user/nice: total = first 8 fields.
    delta = [c - p for c, p in zip(cur[:8], prev[:8], strict=True)]
    total = sum(delta)
    if total <= 0 or any(d < 0 for d in delta):
        return None  # counters reset (reboot) or no time passed
    return 100.0 * delta[7] / total


def disk_used_pct(path: Path) -> float | None:
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    return 100.0 * usage.used / usage.total if usage.total else None


def read_host_stats(server_dir: Path) -> HostStats:
    try:
        cpu = parse_proc_stat(Path("/proc/stat").read_text())
    except (OSError, ValueError):
        cpu = []
    return HostStats(disk_used_pct(server_dir), REBOOT_REQUIRED.exists(), cpu)


def evaluate_host(
    state: State, host: HostStats, cfg: dict[str, float], today: date, ctx: Ctx | None = None
) -> tuple[State, float | None, list[str]]:
    """Pure decision logic for host checks. Returns (new state, steal %, messages)."""
    ctx = ctx or Ctx()
    msgs: list[str] = []
    new = State(**asdict(state))
    gate = gate_of(new, ctx)

    if host.disk_pct is not None:
        full = host.disk_pct >= cfg["disk_pct"]
        if v := gate.check("disk", full, ctx.now, host.disk_pct):
            if v.kind == "recovered":
                msgs.append(
                    render(
                        Alert(
                            "recovered",
                            "Disk space is fine again",
                            impact=f"The server's disk is below {cfg['disk_pct']:.0f}% full.",
                            details=f"it was over for {duration(v.bad_for_s)}; now {host.disk_pct:.0f}%",
                        )
                    )
                )
            else:
                details = f"{host.disk_pct:.0f}% used (alert at {cfg['disk_pct']:.0f}%)"
                if note := reminder_note(v):
                    details += f"; {note}"
                msgs.append(
                    render(
                        Alert(
                            "action",
                            "The server's disk is filling up",
                            impact="Nothing yet; a full disk stops saving the world and backups (risk of "
                            "losing progress).",
                            cause="World growth (new chunks), logs or crash reports.",
                            todo="See what is big: `du -sh /opt/minecraft/server/*` on the box; delete "
                            "old logs/crash "
                            "reports, keep the world border (`world.border_radius`), or grow the disk.",
                            details=details,
                        )
                    )
                )
        new.disk_alerted = "disk" in new.alerts

    # (P11: /var/run/reboot-required is announced by mc-reboot-gate, not here: nothing alerts twice.)

    steal = steal_pct(state.cpu_prev, host.cpu)
    new.cpu_prev = host.cpu
    if steal is not None:
        new.steal_high = state.steal_high + 1 if steal > cfg["steal_pct"] else 0
        if v := gate.check(
            "steal",
            new.steal_high >= cfg["steal_checks"] or (new.steal_high > 0 and "steal" in new.alerts),
            ctx.now,
            steal,
        ):
            if v.kind == "recovered":
                msgs.append(
                    render(
                        Alert(
                            "recovered",
                            "The host's CPU is ours again",
                            impact="Other customers on the same machine no longer take our CPU time.",
                            details=f"it lasted {duration(v.bad_for_s)}; worst {v.worst or 0:.0f}% steal",
                        )
                    )
                )
            else:
                limit = cfg["steal_pct"]
                details = f"CPU steal {steal:.0f}% (alert above {limit:.0f}%) for {new.steal_high} checks"
                if note := reminder_note(v):
                    details += f"; {note}"
                msgs.append(
                    render(
                        Alert(
                            "warning",
                            "The hosting provider is short on CPU for this server",
                            impact="Ticks may get slower (lag) while it lasts. " + ctx.online(),
                            cause="Other virtual machines on the same physical host are using the shared "
                            "CPUs ('steal').",
                            todo="Usually nothing: it passes. The panel's CPU chart shows the steal over "
                            f"time; if it keeps happening and players notice lag, move to {resize_to()}.",
                            details=details,
                        )
                    )
                )
        new.steal_alerted = "steal" in new.alerts
    return new, steal, msgs


@dataclass
class ServiceState:
    """What self-heal observes about the server process (besides RCON)."""

    active_state: str = "unknown"  # systemd ActiveState: active, inactive, failed, activating, ...
    active_for_s: float | None = None  # seconds since it last became active (None = unknown)
    admin_stopped: bool = False
    maintenance: str | None = None  # reason of an open maintenance window
    restore_running: bool = False  # restore.py holds its lock
    sub_state: str = ""  # e.g. auto-restart (between two restarts of Restart=on-failure)
    result: str = ""  # e.g. start-limit-hit
    n_restarts: int | None = None  # systemd NRestarts (automatic restarts since the last manual start)


@dataclass
class HealDecision:
    state: State
    action: str = "none"  # none | restart | dump-restart
    reason: str = ""
    quiet: bool = False  # the server is down but nobody should be alerted (healing, maintenance, stopped)
    alerts: list[str] = field(default_factory=list)


def startup_grace_s() -> float:
    return env_float("HEAL_STARTUP_GRACE_S", STARTUP_GRACE_S)


def decide_heal(
    state: State, alive: bool | None, svc: ServiceState, now: float, grace_s: float = STARTUP_GRACE_S
) -> HealDecision:
    """Pure self-heal policy (design §6); the caller performs the action.

    alive: RCON answered (True), didn't (False), couldn't be checked (None: never a reason to restart).
    grace_s: a server active for less than this is still starting, not hung.
    """
    new = State(**asdict(state))
    new.heals = [t for t in state.heals if now - t < DAY_S]
    d = HealDecision(new)
    if alive is True:
        new.rcon_dead, new.heal_pending = 0, False
        new.heal_failed_alerted = new.gave_up_alerted = new.crash_loop_alerted = False
        new.n_restarts_prev = None
        return d
    if alive is None:
        d.quiet, d.reason = True, "RCON not checkable"
        return d
    prev_restarts, new.n_restarts_prev = state.n_restarts_prev, svc.n_restarts
    if svc.admin_stopped:
        new.rcon_dead, new.heal_pending = 0, False
        d.quiet, d.reason = True, "admin-stopped"
        return d
    if svc.restore_running:
        new.rcon_dead, new.heal_pending = 0, False
        d.quiet, d.reason = True, "restore running"
        return d
    if svc.maintenance is not None:
        new.rcon_dead = 0
        d.quiet, d.reason = True, svc.maintenance
        return d
    restarted = (
        svc.n_restarts - prev_restarts if svc.n_restarts is not None and prev_restarts is not None else 0
    )
    if svc.result == "start-limit-hit" or restarted >= CRASH_LOOP_RESTARTS:
        # systemd is restarting it (Restart=on-failure) and it keeps dying: "starting" would hide that.
        new.rcon_dead, new.heal_pending = 0, False
        why = (
            "systemd's start limit was hit"
            if svc.result == "start-limit-hit"
            else f"systemd restarted it {restarted} times since the last check"
        )
        d.reason = f"crash loop: {why}"
        if not state.crash_loop_alerted:
            new.crash_loop_alerted = True
            d.alerts.append(
                render(
                    Alert(
                        "action",
                        "The Minecraft server keeps crashing",
                        impact="Nobody can play; it dies again right after each start.",
                        cause=f"It crashes on start-up ({why}): "
                        "often a broken plugin, a bad config or a damaged world file.",
                        todo="Read the reason: `journalctl -u minecraft -n 200` on the box (or `mcctl "
                        "doctor`). After fixing "
                        "it: `sudo systemctl reset-failed minecraft`, then start it from the panel.",
                        details=why,
                    )
                )
            )
        return d
    if svc.active_state in ("activating", "deactivating", "reloading", "unknown"):
        new.rcon_dead = 0
        d.quiet, d.reason = True, f"unit {svc.active_state}"
        return d
    if svc.active_state == "active":
        starting = svc.active_for_s is not None and svc.active_for_s < grace_s
        new.rcon_dead = 0 if starting else state.rcon_dead + 1
        if starting or new.rcon_dead < RCON_DEAD_CHECKS:
            d.quiet, d.reason = True, "starting" if starting else "RCON unresponsive, watching"
            return d
        action, reason = "dump-restart", f"process up but RCON dead for {new.rcon_dead} checks"
    else:
        action, reason = "restart", f"unit {svc.active_state}"
    if state.heal_pending and not state.heal_failed_alerted:
        new.heal_failed_alerted = True
        d.alerts.append(
            render(
                Alert(
                    "action",
                    f"{DOWN_HEADLINE} and a restart didn't help",
                    impact="Nobody can join or play.",
                    cause=f"Self-heal restarted it ({reason}), and it is still not answering.",
                    todo="Self-heal tries once more. Look at `journalctl -u minecraft -n 200` on the box "
                    "or run `mcctl doctor`.",
                    details=reason,
                )
            )
        )
    if len(new.heals) >= MAX_HEALS_PER_DAY:
        new.heal_pending = False
        if not state.gave_up_alerted:
            new.gave_up_alerted = True
            d.alerts.append(
                render(
                    Alert(
                        "action",
                        f"{DOWN_HEADLINE}; automatic restarts stopped",
                        impact="Nobody can join or play until someone fixes it.",
                        cause=f"Self-heal gave up after {len(new.heals)} restarts in 24 h ({reason}).",
                        todo="Find the reason in `journalctl -u minecraft -n 200` on the box (or `mcctl "
                        "doctor`), fix it, "
                        "then start the server from the panel.",
                        details=f"{len(new.heals)} restarts in 24 h; {reason}",
                    )
                )
            )
        d.reason = reason
        return d
    new.heals.append(now)
    new.rcon_dead, new.heal_pending = 0, True
    d.action, d.reason = action, reason
    d.quiet = not d.alerts
    if len(new.heals) > ALERT_HEALS:
        d.alerts.append(
            render(
                Alert(
                    "warning",
                    "The Minecraft server had to be restarted again",
                    impact="Players were kicked for a minute or two; it is starting again now.",
                    cause=f"It stopped answering ({reason}); "
                    f"{len(new.heals)} automatic restarts in 24 h is unusual.",
                    todo="Check `journalctl -u minecraft -n 200` for a crash reason (or `mcctl doctor`). "
                    "Self-heal stops "
                    f"after {MAX_HEALS_PER_DAY} restarts a day.",
                    details=f"{len(new.heals)} restarts in 24 h; {reason}",
                )
            )
        )
    return d


def restart_failed_alert(reason: str) -> str:
    return render(
        Alert(
            "action",
            f"{DOWN_HEADLINE} and self-heal couldn't restart it",
            impact="Nobody can join or play.",
            cause=f"The restart command itself failed ({reason}).",
            todo="`journalctl -u mc-health -n 50` and `journalctl -u minecraft -n 100` on the box; "
            "`mcctl doctor`.",
            details=reason,
        )
    )


def service_state(now: float) -> ServiceState:
    """systemd's view of the server unit, plus the admin-stopped, maintenance and restore markers."""
    svc = ServiceState(
        # a fenced box (mcctl migrate) counts as admin-stopped: self-heal must never start it
        admin_stopped=admin_stopped(ADMIN_STOPPED) or fenced(),
        maintenance=maintenance_active(now),
        restore_running=restore_running(),
    )
    try:
        out = subprocess.run(
            [
                "systemctl",
                "show",
                "--timestamp=unix",
                "-p",
                "ActiveState,SubState,Result,NRestarts,ActiveEnterTimestamp",
                UNIT,
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return svc
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    svc.active_state = props.get("ActiveState", "unknown") or "unknown"
    svc.sub_state, svc.result = props.get("SubState", ""), props.get("Result", "")
    restarts = props.get("NRestarts", "")
    svc.n_restarts = int(restarts) if restarts.isdigit() else None
    entered = props.get("ActiveEnterTimestamp", "").lstrip("@")
    if entered.isdigit() and int(entered) > 0:
        svc.active_for_s = now - int(entered)
    return svc


def heal(action: str, reason: str) -> bool:
    """Perform a self-heal action; True if systemd accepted the restart. Logged for the audit trail."""
    print(f"AUDIT self-heal action={action} reason={reason!r}")
    if action == "dump-restart":
        # Not os.kill: the JVM runs as another user; polkit lets the ops user signal it through systemd.
        try:
            subprocess.run(
                ["systemctl", "--no-ask-password", "kill", "--signal=SIGQUIT", UNIT], check=True, timeout=30
            )
            time.sleep(5)  # the JVM writes a thread dump to the journal
        except (OSError, subprocess.SubprocessError) as e:
            print(f"thread dump failed: {e}", file=sys.stderr)
    try:
        subprocess.run(
            ["systemctl", "--no-ask-password", "restart", "--no-block", UNIT], check=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError) as e:
        print(f"self-heal restart failed: {e}", file=sys.stderr)
        return False
    return True


def apply_heal(
    prev: State, d: HealDecision, msgs: list[str], hb_status: str, rcon_ok: bool | None
) -> tuple[State, list[str], str]:
    """Merge the self-heal decision into evaluate()'s result (`prev` = the state before this check): a quiet
    heal suppresses the DOWN alert and the recovery notice that would follow it; a failed heal alerts."""
    new = State(**asdict(d.state))
    if rcon_ok is True:
        if prev.status == "down" and not prev.down_alerted:
            msgs = [m for m in msgs if BACK_HEADLINE not in m]
        new.down_alerted = False
        return new, msgs, hb_status
    msgs = [m for m in msgs if f"**{DOWN_HEADLINE}**" not in m]
    if d.quiet:
        return new, msgs, ""
    if d.alerts:
        new.down_alerted = True
        return new, d.alerts + msgs, "fail"
    # Down, not healed (gave up earlier and already alerted): keep failing the heartbeat, no new message.
    return new, msgs, "fail"


@dataclass
class RconProbe:
    alive: bool | None  # None = couldn't be checked (no restart decision may rest on it)
    mspt: float | None = None
    tps: float | None = None
    warnings: list[str] = field(default_factory=list)
    players: int | None = None


def probe_rcon(server_dir: Path | None = None, timeout: float = 10.0) -> RconProbe:
    """Liveness and MSPT/TPS over RCON. Alive = the server replied to the login (even with a refusal)."""
    try:
        client = mc_rcon.connect(server_dir, timeout=timeout)
    except (OSError, KeyError, ValueError, mc_rcon.RconError) as e:
        return RconProbe(None, warnings=[f"RCON settings unreadable ({type(e).__name__}: {e})"])
    probe = RconProbe(False)
    try:
        with client as r:
            probe.alive = True
            texts = {name: r.command(name) for name in ("mspt", "tps")}
            try:  # the player count only adds context to alerts: never a reason to doubt liveness
                texts["list"] = r.command("list")
            except (OSError, mc_rcon.RconError):
                texts["list"] = ""
    except mc_rcon.RconAuthError:
        probe.alive = True
        probe.warnings.append("RCON refused the password (server.properties out of sync?): alive, no MSPT")
        return probe
    except (OSError, mc_rcon.RconError) as e:
        if probe.alive:
            probe.warnings.append(f"RCON reply failed after login ({type(e).__name__}): alive, no MSPT")
        else:
            probe.warnings.append(f"RCON unreachable ({type(e).__name__}: {e})")
        return probe
    if m := PLAYERS.search(mc_rcon.strip_formatting(texts["list"])):
        probe.players = int(m.group(1))
    for name, parse in (("mspt", parse_mspt), ("tps", parse_tps)):
        try:
            setattr(probe, name, parse(texts[name]))
        except ValueError as e:
            probe.warnings.append(f"{name}: {e}")
    return probe


def evaluate_watchdog(
    state: State, configured: bool, reply: dict[str, object] | None
) -> tuple[State, list[str]]:
    """Pure: alert (Discord, directly) when the watchdog can't be reached or its cron looks stale."""
    new = State(**asdict(state))
    msgs: list[str] = []
    if not configured:
        return new, msgs
    if reply is None:
        new.watchdog_unreachable = state.watchdog_unreachable + 1
        if new.watchdog_unreachable >= WATCHDOG_UNREACHABLE_CHECKS and not state.watchdog_unreachable_alerted:
            new.watchdog_unreachable_alerted = True
            msgs.append(
                render(
                    Alert(
                        "warning",
                        "The outside watchdog can't be reached from the server",
                        impact="Players are not affected. But if the server went down now, nobody would "
                        "be told.",
                        cause="The server's heartbeat to the Cloudflare watchdog failed "
                        f"{new.watchdog_unreachable} checks "
                        "in a row (network trouble or a Cloudflare problem).",
                        todo="Usually nothing: it recovers by itself. If it lasts: check "
                        "https://www.cloudflarestatus.com "
                        "and `mcctl doctor`.",
                        details=f"{new.watchdog_unreachable} heartbeats not delivered",
                    )
                )
            )
        return new, msgs
    if state.watchdog_unreachable_alerted:
        msgs.append(
            render(
                Alert(
                    "recovered",
                    "The outside watchdog is reachable again",
                    impact="Outages are reported again.",
                )
            )
        )
    new.watchdog_unreachable, new.watchdog_unreachable_alerted = 0, False
    age = reply.get("last_run_age_s")
    if not isinstance(age, int | float) or isinstance(age, bool):
        return new, msgs  # never ran yet (fresh deployment) or an old Worker: nothing to judge
    if age > WATCHDOG_STALE_S:
        if not state.watchdog_stale_alerted:
            new.watchdog_stale_alerted = True
            msgs.append(
                render(
                    Alert(
                        "warning",
                        "The outside watchdog has stopped checking",
                        impact="Players are not affected. But outages, late backups and lag would go "
                        "unreported.",
                        cause="The watchdog Worker's scheduled run hasn't happened for "
                        f"{int(age) // 60} min.",
                        todo="Cloudflare dashboard: Workers & Pages, the watchdog Worker, Settings > "
                        "Triggers (cron) and "
                        "its logs; https://www.cloudflarestatus.com.",
                        details=f"last cron run {int(age) // 60} min ago",
                    )
                )
            )
    elif state.watchdog_stale_alerted:
        new.watchdog_stale_alerted = False
        msgs.append(
            render(
                Alert(
                    "recovered",
                    "The outside watchdog is checking again",
                    impact="Outages are reported again.",
                )
            )
        )
    return new, msgs


PLAYERS = re.compile(r"There are (\d+)")
# Chunky 1.5.3's lang/en.json (checked in the pinned jar), each reply with Chunky's "[Chunky] " prefix:
CHUNKY_RUNNING = "Task running for"  # task_update / `chunky progress` with a task
CHUNKY_STARTED = "Task started in"  # format_start
CHUNKY_CONTINUING = "Task continuing for"  # format_continue
CHUNKY_NOTHING_TO_CONTINUE = "No tasks to continue"  # format_continue_no_tasks
CHUNKY_ALREADY = (
    "A task was already started",
    "Task already started",
)  # format_start_confirm/_started_already
# format_start_confirm (a saved, unfinished task for that world): `chunky confirm` runs the pending start
CHUNKY_CONFIRM = "A task was already started for this world"
# format_cancel_confirm: `chunky cancel` always asks first (CancelCommand sets a pending action, keyed by the
# sender's name); `chunky confirm` cancels the running and saved tasks
CHUNKY_CANCEL_CONFIRM = "Cancelled tasks cannot be continued"
# task_update: "Task running for %s. Processed: %s chunks (%s%%), ETA: %s:%s:%s, Rate: %s cps, Current:
# %s, %s", the percentage String.format("%.2f") in the JVM's locale (12.34, or 12,34); `chunky progress`
# prints one line per running task (GenerationTask$Progress.sendUpdate) and "No tasks running." without one.
# A paused task isn't listed: its last percentage stays in the state file.
CHUNKY_PROGRESS = re.compile(
    r"Task running for (?P<world>[A-Za-z0-9_.-]+?)\. Processed: (?P<chunks>\d+) chunks "
    r"\((?P<pct>\d+(?:[.,]\d+)?)%\)"
)
# v1.4 frontier expansion: a new ring only when the target grew by this much (no thrashing every 5 minutes).
FRONTIER_STEP = 256
FRONTIER_MARGIN = 1024  # = schema world.pregenerate.margin's default
FRONTIER_MAX_RADIUS = 10000  # = schema world.pregenerate.max_radius's default
# Region folders per dimension. Since 26.x every dimension lives under the one world folder (level-name):
# <level>/dimensions/minecraft/<dimension>/region, the overworld included (seen in a world written by the
# pinned Purpur 26.3; Paper's WorldFolderMigration moves older worlds there at start-up). The Bukkit world
# names Chunky takes are unchanged (PaperWorldLoader.defaultWorldName: <level>, <level>_nether,
# <level>_the_end). Fallback for a pre-26 world, in the Bukkit world's own folder: the overworld's
# <level>/region, <level>_nether/DIM-1/region, <level>_the_end/DIM1/region.
REGION_DIRS = {
    "overworld": "region",
    "the_nether": "DIM-1/region",
    "the_end": "DIM1/region",
}
REGION_FILE = re.compile(r"r\.(-?\d+)\.(-?\d+)\.mca")
REGION_HEADER = 4096  # 1024 chunk locations x 4 bytes (3-byte sector offset, 1-byte length; 0 = no chunk)


def pregen_worlds() -> list[str]:
    return [w for w in os.environ.get("PREGEN_WORLDS", "").split(",") if re.fullmatch(r"[A-Za-z0-9_.-]+", w)]


def parse_chunky_progress(text: str) -> dict[str, tuple[float, int]]:
    """`chunky progress` -> {world: (percent, chunks processed)} for every running task."""
    return {
        m["world"]: (float(m["pct"].replace(",", ".")), int(m["chunks"]))
        for m in CHUNKY_PROGRESS.finditer(mc_rcon.strip_formatting(text))
    }


@dataclass(frozen=True)
class FrontierDim:
    """A dimension frontier expansion may grow: its Bukkit world, which dimension it is, its border (None)."""

    world: str
    dimension: str  # overworld | the_nether | the_end (REGION_DIRS)
    border: int | None = None


@dataclass
class Frontier:
    """v1.4 frontier expansion (PREGEN_FRONTIER): keep pre-generating `margin` blocks beyond the farthest
    generated chunk, capped at the border and at `max_radius`. `explored` measures a dimension (blocks from
    0,0, square distance; None = unknown); `allowed` is False while the disk is at its alert threshold."""

    dims: list[FrontierDim]
    margin: int = FRONTIER_MARGIN
    max_radius: int = FRONTIER_MAX_RADIUS
    step: int = FRONTIER_STEP
    explored: Callable[[FrontierDim], int | None] = field(default=lambda d: None)
    allowed: bool = True


def chunk_reach(cx: int, cz: int) -> int:
    """How far the chunk's outer edge is from 0,0, in blocks (square distance, like the border and Chunky's
    square shape)."""
    return max(cx * 16 + 16 if cx >= 0 else -cx * 16, cz * 16 + 16 if cz >= 0 else -cz * 16)


BUKKIT_SUFFIX = {"overworld": "", "the_nether": "_nether", "the_end": "_the_end"}


def region_dir(server_dir: Path, d: FrontierDim, level: str | None = None) -> Path | None:
    """The dimension's region folder (26.x layout first, then the pre-26 one), refused if any part of it
    under the server dir is a symlink (the JVM can write there: never list a folder elsewhere). `level`:
    level-name (else derived from the Bukkit world name)."""
    suffix = BUKKIT_SUFFIX[d.dimension]
    if not level:
        level = d.world[: len(d.world) - len(suffix)] if suffix and d.world.endswith(suffix) else d.world
    for rel in (
        f"{level}/dimensions/minecraft/{d.dimension}/region",
        f"{d.world}/{REGION_DIRS[d.dimension]}",
    ):
        path = server_dir / rel
        try:
            if path.is_dir() and path.resolve() == server_dir.resolve() / rel:
                return path
        except OSError:
            continue
    return None


def level_name(server_dir: Path) -> str | None:
    """server.properties' level-name (the one world folder since 26.x), None if unreadable."""
    try:
        return mc_rcon.read_properties(server_dir / "server.properties").get("level-name") or None
    except (OSError, ValueError):
        return None


def read_region_header(path: Path) -> bytes:
    """A region file's location table (the JVM writes the folder: read_untrusted, no symlinks or FIFOs)."""
    return mc_rcon.read_untrusted(path, REGION_HEADER)


def explored_radius(directory: Path | None, read: Callable[[Path], bytes] = read_region_header) -> int | None:
    """The farthest generated chunk's outer edge (blocks from 0,0) in a region folder; 0 without chunks, None
    without the folder. Chunks are generated around players, so this is how far they have been. Cheap:
    region names give each file's farthest possible chunk, only the 4 KiB headers are read, outermost files
    first, and it stops once no remaining file can beat the best chunk found."""
    if directory is None:
        return None
    try:
        names = os.listdir(directory)
    except OSError:
        return None
    regions = [(int(m[1]), int(m[2]), n) for n in names if (m := REGION_FILE.fullmatch(n))]

    def outer(r: tuple[int, int, str]) -> int:
        rx, rz, _ = r
        return chunk_reach(rx * 32 + 31 if rx >= 0 else rx * 32, rz * 32 + 31 if rz >= 0 else rz * 32)

    best = 0
    for r in sorted(regions, key=outer, reverse=True):
        if outer(r) <= best:
            break
        try:
            header = read(directory / r[2])
        except OSError:
            continue
        for i in range(min(len(header), REGION_HEADER) // 4):
            if header[4 * i : 4 * i + 4] != b"\0\0\0\0":
                best = max(best, chunk_reach(r[0] * 32 + i % 32, r[1] * 32 + i // 32))
    return best


def frontier_env(disk_ok: bool = True, server_dir: Path | None = None) -> Frontier | None:
    """PREGEN_FRONTIER ("world:overworld:5000,world_the_end:the_end:", border empty = none; empty = off),
    PREGEN_MARGIN, PREGEN_MAX_RADIUS (health.env, server.yml world.pregenerate)."""
    dims = []
    for item in os.environ.get("PREGEN_FRONTIER", "").split(","):
        parts = item.split(":")
        if len(parts) != 3:
            continue
        world, dim, border = parts
        ok_border = not border or border.isdigit()
        if re.fullmatch(r"[A-Za-z0-9_.-]+", world) and dim in REGION_DIRS and ok_border:
            dims.append(FrontierDim(world, dim, int(border) if border else None))
    if not dims:
        return None
    root = server_dir or mc_rcon.SERVER_DIR
    return Frontier(
        dims,
        margin=int(env_float("PREGEN_MARGIN", FRONTIER_MARGIN)),
        max_radius=int(env_float("PREGEN_MAX_RADIUS", FRONTIER_MAX_RADIUS)),
        explored=lambda d: explored_radius(region_dir(root, d, level_name(root))),
        allowed=disk_ok,
    )


def frontier_plan(state: State, worlds: list[str], f: Frontier) -> tuple[FrontierDim, int] | None:
    """The next ring to generate (pure): per dimension, target = farthest generated chunk + margin (whole
    chunks), capped at the border and max_radius; a dimension qualifies once its bordered pass is done (or
    it has no border) and its target is at least a step beyond what is generated (or reaches the cap). The
    biggest gap wins."""
    best: tuple[FrontierDim, int, int] | None = None
    for d in f.dims:
        if d.world in worlds and d.world not in state.pregen_done:
            continue  # its bordered pass comes first
        limit = min(f.max_radius, d.border) if d.border is not None else f.max_radius
        have = state.pregen_frontier.get(d.world)
        if have is None:
            have = d.border if d.border is not None and d.world in state.pregen_done else 0
        if have >= limit:
            continue
        reach = f.explored(d)
        if reach is None:
            continue
        target = min(-(-(reach + f.margin) // 16) * 16, limit)
        if target <= have or (target < have + f.step and target < limit):
            continue
        if best is None or target - have > best[2]:
            best = (d, target, target - have)
    return (best[0], best[1]) if best else None


def pregen_step(
    state: State,
    worlds: list[str],
    command: Callable[[str], str],
    world_id: str | None = None,
    frontier: Frontier | None = None,
) -> tuple[State, str]:
    """One check's worth of pre-generation (see the module doc). Returns the new state and what it did.
    v1.2: `world_id` names the active world of the library (name@switched-at, scripts/worlds.py); after a
    switch the folders hold another world, so Chunky's tasks are cancelled and everything starts afresh.
    v1.4: records each task's progress; with `frontier`, once the bordered passes are done, grows the
    generated area ahead of where players have been (one Chunky task at a time, like the bordered passes)."""
    new = State(**asdict(state))
    if world_id is not None and world_id != new.pregen_for:
        if new.pregen_for or new.pregen_done or new.pregen_current or new.pregen_frontier_current:
            # v1.4: without the confirmation the old world's saved task survived the switch (and `chunky
            # start` then continued it on the new world's folders)
            cancel_chunky(command)
        new.pregen_done, new.pregen_current, new.pregen_paused = [], None, False
        new.pregen_progress, new.pregen_frontier = {}, {}
        new.pregen_frontier_current, new.pregen_frontier_target, new.pregen_frontier_pct = None, 0, None
        new.pregen_for = world_id
    todo = [w for w in worlds if w not in new.pregen_done]
    if not todo and frontier is None and not new.pregen_frontier_current:
        return new, "done" if worlds else ""
    m = PLAYERS.search(mc_rcon.strip_formatting(command("list")))
    players = int(m.group(1)) if m else None
    progress = mc_rcon.strip_formatting(command("chunky progress"))
    for world, (pct, chunks) in parse_chunky_progress(progress).items():
        if world == new.pregen_current:
            new.pregen_progress[world] = {"pct": pct, "chunks": chunks}
        elif world == new.pregen_frontier_current:
            new.pregen_frontier_pct = pct
    running = CHUNKY_RUNNING in progress
    task = new.pregen_current or (
        f"frontier {new.pregen_frontier_current}" if new.pregen_frontier_current else None
    )
    if players is None or players > 0:  # unknown counts as "someone": never generate under players
        if running:
            command("chunky pause")
            new.pregen_paused = True
            return new, f"paused ({task}: players online)"
        return new, "waiting for an empty server"
    if running:
        new.pregen_paused = False  # e.g. Chunky's continue-on-restart resumed a task paused for players
        return new, f"running ({task})"
    if new.pregen_current:
        reply = mc_rcon.strip_formatting(command("chunky continue"))
        if CHUNKY_CONTINUING in reply:
            new.pregen_paused = False
            return new, f"continued ({new.pregen_current})"
        if CHUNKY_NOTHING_TO_CONTINUE not in reply:
            return new, f"unexpected reply to `chunky continue`: {reply[:120]}"
        done = new.pregen_current
        new.pregen_done.append(done)
        new.pregen_progress[done] = {**new.pregen_progress.get(done, {}), "pct": 100.0}
        new.pregen_current, new.pregen_paused = None, False
    elif new.pregen_frontier_current:
        reply = mc_rcon.strip_formatting(command("chunky continue"))
        if CHUNKY_CONTINUING in reply:
            new.pregen_paused = False
            return new, f"continued (frontier {new.pregen_frontier_current})"
        if CHUNKY_NOTHING_TO_CONTINUE not in reply:
            return new, f"unexpected reply to `chunky continue`: {reply[:120]}"
        new.pregen_frontier[new.pregen_frontier_current] = new.pregen_frontier_target
        new.pregen_frontier_current, new.pregen_frontier_target, new.pregen_frontier_pct = None, 0, None
        new.pregen_paused = False
    todo = [w for w in worlds if w not in new.pregen_done]
    if todo:
        world = todo[0]
        command(f"chunky world {world}")
        command("chunky worldborder")
        reply = mc_rcon.strip_formatting(command("chunky start"))
        if CHUNKY_STARTED in reply or any(a in reply for a in CHUNKY_ALREADY):
            if CHUNKY_STARTED not in reply:
                command("chunky continue")
            new.pregen_current, new.pregen_paused = world, False
            return new, f"started ({world})"
        return new, f"unexpected reply to `chunky start`: {reply[:120]}"
    status = "done" if worlds else ""
    if frontier is None:
        return new, status
    if not frontier.allowed:
        return new, " ".join(x for x in (status, "(frontier held: the disk is at its alert threshold)") if x)
    plan = frontier_plan(new, worlds, frontier)
    if plan is None:
        return new, " ".join(x for x in (status, "(frontier: nothing to grow)") if x)
    d, target = plan
    reply = mc_rcon.strip_formatting(command(f"chunky start {d.world} square 0 0 {target}"))
    if CHUNKY_CONFIRM in reply:  # an older saved task for this world: replace it with the bigger square
        reply = mc_rcon.strip_formatting(command("chunky confirm"))
    if CHUNKY_STARTED in reply:
        new.pregen_frontier_current, new.pregen_frontier_target = d.world, target
        new.pregen_frontier_pct, new.pregen_paused = 0.0, False
        return new, f"frontier started ({d.world} to radius {target})"
    return new, f"unexpected reply to frontier `chunky start`: {reply[:120]}"


def all_done(state: State, worlds: list[str]) -> bool:
    return bool(worlds) and all(w in state.pregen_done for w in worlds)


def pregen_done_alert(worlds: list[str], state: State, frontier: bool) -> str:
    chunks = [
        f"{w} {int(state.pregen_progress[w]['chunks'])}"
        for w in worlds
        if "chunks" in state.pregen_progress.get(w, {})
    ]
    return render(
        Alert(
            "info",
            "Chunk pre-generation is finished",
            impact="Every dimension with a border is generated up to it, so exploring inside the border no "
            "longer generates terrain (no lag from new chunks). The next backup uploads the new region files "
            "once; later backups are small again.",
            details=(
                f"chunks processed at the last look: {', '.join(chunks)}"
                if chunks
                else f"pre-generated: {', '.join(worlds)}"
            )
            + ("; frontier expansion goes on beyond players' farthest chunks" if frontier else ""),
        )
    )


def pregen_notice(before: State, after: State, worlds: list[str], frontier: bool) -> str | None:
    """The one Discord note when the bordered passes finish (a transition: never repeated, never on an upgrade
    of a box that was already done)."""
    if all_done(after, worlds) and not all_done(before, worlds):
        return pregen_done_alert(worlds, after, frontier)
    return None


def pregen_summary(
    state: State, worlds: list[str], frontier: Frontier | None, held: str | None = None
) -> str:
    """One short line for `mcctl status` (health.py writes it to pregen.json), e.g.
    "world 63% · world_nether queued · world_the_end queued", "done", "done · frontier world_the_end 41%"."""
    if not worlds and frontier is None:
        return "off"
    parts: list[str] = []
    if all_done(state, worlds):
        parts.append("done")
    for w in worlds if not all_done(state, worlds) else []:
        if w in state.pregen_done:
            parts.append(f"{w} done")
        elif w == state.pregen_current:
            pct = state.pregen_progress.get(w, {}).get("pct")
            parts.append(f"{w} {pct:.0f}%" if pct is not None else f"{w} started")
            if state.pregen_paused:
                parts[-1] += " (paused)"
        else:
            parts.append(f"{w} queued")
    if frontier is not None:
        if state.pregen_frontier_current:
            pct = state.pregen_frontier_pct
            parts.append(
                f"frontier {state.pregen_frontier_current} to {state.pregen_frontier_target}"
                + (f" {pct:.0f}%" if pct is not None else "")
                + (" (paused)" if state.pregen_paused else "")
            )
        elif state.pregen_frontier:
            reached = sorted(state.pregen_frontier.items())
            parts.append("frontier " + ", ".join(f"{w} {r}" for w, r in reached))
        elif not worlds:
            parts.append("frontier on")
    line = " · ".join(parts)
    return f"{line} (held: {held})" if held else line


def active_world_id() -> str | None:
    """v1.2: name@since of the world library's active world, once a switch has set it (else None)."""
    try:
        import worlds

        st = worlds.load_state()
    except Exception:
        return None
    return f"{st['active']}@{st['since']}" if st["active"] and st["since"] else None


def run_pregen(state: State, alive: bool | None, disk_ok: bool = True) -> State:
    worlds, frontier = pregen_worlds(), frontier_env(disk_ok)
    now = time.time()
    if alive is not True or admin_stopped(ADMIN_STOPPED):
        return state
    if not worlds and frontier is None:
        return stop_pregen(state) if state.pregen_current or state.pregen_frontier_current else state
    if maintenance_active(now):
        return state
    if held := pregen_held(now):  # backup.py paused Chunky for its save-off window: it continues it
        print(f"pregen: held ({held})")
        return state
    try:
        with mc_rcon.connect(None, timeout=10.0) as r:
            new, what = pregen_step(state, worlds, r.command, active_world_id(), frontier)
    except (OSError, KeyError, ValueError, mc_rcon.RconError) as e:
        print(f"warning: pregen: {type(e).__name__}: {e}", file=sys.stderr)
        return state
    if what:
        print(f"pregen: {what}")
    if note := pregen_notice(state, new, worlds, frontier is not None):
        discord(note)
    return new


def cancel_chunky(command: Callable[[str], str]) -> None:
    if CHUNKY_CANCEL_CONFIRM in mc_rcon.strip_formatting(command("chunky cancel")):
        command("chunky confirm")


def stop_pregen(state: State) -> State:
    """Pre-generation was turned off (world.pregenerate.enabled: false, or the world's pregen: false) while a
    task it started was still saved: cancel it, or Chunky would go on (and continue-on-restart resume it)."""
    try:
        with mc_rcon.connect(None, timeout=10.0) as r:
            cancel_chunky(r.command)
    except (OSError, KeyError, ValueError, mc_rcon.RconError) as e:
        print(f"warning: pregen: {type(e).__name__}: {e}", file=sys.stderr)
        return state
    print("pregen: turned off: Chunky's task cancelled")
    new = State(**asdict(state))
    new.pregen_current, new.pregen_paused = None, False
    new.pregen_frontier_current, new.pregen_frontier_target, new.pregen_frontier_pct = None, 0, None
    return new


def write_pregen_status(path: Path, state: State, now: float) -> None:
    """pregen.json in health.py's StateDirectory: the line `mcctl status` shows (read over SSH)."""
    worlds, frontier = pregen_worlds(), frontier_env()
    line = pregen_summary(state, worlds, frontier, pregen_held(now))
    try:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"line": line, "at": int(now)}))
        os.replace(tmp, path)
    except OSError as e:
        print(f"warning: {path}: {e}", file=sys.stderr)


def pregen_task(state: State) -> str | None:
    """What pre-generation is running right now (not paused), for alerts' likely cause."""
    if state.pregen_paused:
        return None
    if state.pregen_current:
        return state.pregen_current
    return f"frontier {state.pregen_frontier_current}" if state.pregen_frontier_current else None


def unit_active(unit: str) -> bool:
    try:
        r = subprocess.run(["systemctl", "is-active", "--quiet", unit], check=False, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


LOG_ERROR = re.compile(r"\[[\d:]+ (?:ERROR|FATAL)\]: (.+)")


def last_log_error(server_dir: Path) -> str:
    """The server log's last ERROR line (short, no addresses: it goes to Discord), or ""."""
    try:
        data = mc_rcon.read_untrusted(server_dir / "logs" / "latest.log", 65536, tail=True)
    except OSError:
        return ""
    found = LOG_ERROR.findall(data.decode("utf-8", "replace"))
    return found[-1].strip()[:200] if found else ""


def main() -> int:
    cfg = {
        "mspt_alert": env_float("MSPT_ALERT", 45),
        "mspt_checks": env_float("MSPT_ALERT_CHECKS", 3),
        "heap_pct": env_float("HEAP_ALERT_PCT", 85),
        "heap_checks": env_float("HEAP_ALERT_CHECKS", 6),
        "gc_pause_ms": env_float("GC_PAUSE_ALERT_MS", TICK_MS),
        "disk_pct": env_float("DISK_ALERT_PCT", 85),
        "steal_pct": env_float("STEAL_ALERT_PCT", 10),
        "steal_checks": env_float("STEAL_ALERT_CHECKS", 3),
    }
    state_path = Path(os.environ.get("STATE_DIRECTORY", "/var/lib/mc-health")) / "state.json"
    state = load_state(state_path)

    probe = probe_rcon()
    for w in probe.warnings:
        print(f"warning: {w}", file=sys.stderr)
    mspt, tps = probe.mspt, probe.tps

    gc = parse_gc(read_gc_log(mc_rcon.SERVER_DIR), state.last_gc_uptime, cfg["gc_pause_ms"])
    now = time.time()
    ctx = Ctx(
        now=now,
        players=probe.players,
        pregen=pregen_task(state),
        backup=unit_active("mc-backup.service"),
        repeat_s=env_float("REPEAT_HOURS", 6) * 3600,
        log_hint=last_log_error(mc_rcon.SERVER_DIR) if probe.alive is False else "",
    )
    new, hb_status, msgs = evaluate(state, probe.alive, mspt, tps, gc, cfg, ctx)
    svc = service_state(now) if probe.alive is not True else ServiceState(active_state="active")
    decision = decide_heal(new, probe.alive, svc, now, startup_grace_s())
    new, msgs, hb_status = apply_heal(state, decision, msgs, hb_status, probe.alive)
    if decision.action != "none" and not heal(decision.action, decision.reason):
        msgs.append(restart_failed_alert(decision.reason))
        hb_status, new.down_alerted = "fail", True
    host = read_host_stats(mc_rcon.SERVER_DIR)
    new, steal, host_msgs = evaluate_host(new, host, cfg, date.today(), ctx)
    msgs += host_msgs

    heap = f"{gc.heap_after_pct:.0f}%" if gc.heap_after_pct is not None else "n/a"
    disk = f"{host.disk_pct:.0f}%" if host.disk_pct is not None else "n/a"
    steal_s = f"{steal:.1f}%" if steal is not None else "n/a"
    summary = (
        f"status={new.status} mspt={mspt} tps={tps} "
        f"max_gc_pause_ms={gc.max_pause_ms:.0f} heap_after_gc={heap} "
        f"disk={disk} steal={steal_s} reboot_required={host.reboot_required}"
    )
    if decision.reason:
        summary += f" heal={decision.action}:{decision.reason}"
    print(summary)
    stopped = svc.admin_stopped if probe.alive is not True else admin_stopped(ADMIN_STOPPED)
    reply = heartbeat_reply("health", hb_status, "\n".join([summary, *msgs]), admin_stopped=stopped)
    configured = bool(os.environ.get("WATCHDOG_URL") and os.environ.get("HEARTBEAT_TOKEN"))
    new, wd_msgs = evaluate_watchdog(new, configured, reply)
    msgs += wd_msgs
    for m in msgs:
        discord(m)
    new = run_pregen(new, probe.alive, host.disk_pct is None or host.disk_pct < cfg["disk_pct"])

    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(asdict(new)))
    write_pregen_status(state_path.with_name("pregen.json"), new, now)
    return 0


if __name__ == "__main__":
    sys.exit(main())
