#!/usr/bin/env python3
"""Consistent restic backup of the server directory.

    backup.py            # hourly: save-off -> save-all flush -> restic backup -> save-on (always) -> forget
    backup.py --prune    # weekly: restic prune (separate timer)
    backup.py --list-snapshots   # restic's snapshot list -> /var/lib/mc-snapshots/snapshots.json (the panel's
                                 # Backups tab and its restore picker; mc-backup runs it after each backup)

If the server isn't running, the files are already consistent and the backup proceeds without RCON.
restic settings come from the environment (RESTIC_REPOSITORY, RESTIC_PASSWORD, AWS_*), provided by
systemd EnvironmentFile=/etc/minecraft/restic.env, as do WATCHDOG_URL / HEARTBEAT_TOKEN: a backup reports
start, success and failure to the watchdog's "backup" check; a prune reports only a failure (a successful
prune says nothing about how fresh the last backup is). While restore.py holds its lock the backup is
skipped (it would snapshot a half-restored world); the restore's maintenance window covers the gap.

v1.4: chunk generation while saving is off piles the new chunks up in memory, so a running Chunky task
(pre-generation, health.py) is paused before `save-off` and continued after `save-on`, only if it was running,
and even when the backup fails. While it is paused the pre-generation hold (notify.py pregen-hold.json) keeps
health.py from continuing it mid-backup.

v1.5 smooth backups (server.yml backups.*: the unit's BACKUP_* environment, docs/operations.md "Smooth
backups"). Only a run the timer started (systemd sets TRIGGER_UNIT=mc-backup.timer) may be skipped or
deferred; every other start of mc-backup.service (`mcctl backup now`, the panel, Discord, a restart's "back
up first", a migration's final backup, a world switch) backs up at once, as before.
- Skip when idle and unchanged (BACKUP_SKIP_WHEN_IDLE): nobody online, no pre-generation task running, and
  every file the backup would read (path, size, mtime) exactly as at the last successful backup, which
  recorded that fingerprint in backup-state.json. The skip is recorded there (backup_check.py allows for it)
  and reported to the watchdog as an OK heartbeat, so the freshness check doesn't fire. At most
  BACKUP_SKIP_MAX_HOURS (notify.py) after the last real backup a run backs up anyway.
- Defer under load (BACKUP_DEFER_WHEN_BUSY, BACKUP_MAX_DEFER_MINUTES): players online and the server lagging
  (1-minute TPS or MSPT bad, or a tick spike in the last 5 s): wait DEFER_STEP_S and look again, at most
  BACKUP_MAX_DEFER_MINUTES, then back up anyway. The "start" heartbeat goes out only once the backup starts.
- Gentler save (BACKUP_GENTLE_SAVE): `save-all` without `flush`, then wait until the world's files have been
  quiet for SETTLE_QUIET_S; `save-all flush` only as the fallback when they haven't after SETTLE_MAX_S. In
  the pinned Purpur (26.3, Moonrise chunk system) `flush` is what stalls the main thread:
  ChunkHolderManager.saveAllChunks then calls MoonriseRegionFileIO.partialFlush during the loop and
  MoonriseRegionFileIO.flush at the end, waiting for the disk; without it the chunks are serialized and
  handed to the region-file IO threads.
- restic limits: BACKUP_UPLOAD_LIMIT_KIB (restic's global --limit-upload, KiB/s; 0 = none),
  BACKUP_S3_CONNECTIONS (-o s3.connections=N); RESTIC_READ_CONCURRENCY, GOMAXPROCS and GOMEMLIMIT reach
  restic directly (the unit sets them for performance.vm small).
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import mc_rcon
from notify import (
    BACKUP_SKIP_MAX_HOURS,
    BACKUP_STATE_FILE,
    SNAPSHOTS_FILE,
    heartbeat,
    pregen_hold,
    pregen_release,
    restore_running,
)

SERVER_DIR = str(mc_rcon.SERVER_DIR)
# The R2 bucket lock (server.yml backups.lock_days -> terraform var r2_lock_days, default 7) makes snapshot
# files undeletable for that long; `forget` must never try, or it fails. Keep everything for lock + 1 day
# (clock-skew margin). The deploy writes KEEP_WITHIN_DAYS into restic.env (mc_backup_keep_within_days);
# this default matches the default lock, and tests/test_consistency.py keeps all of them in step.
KEEP_WITHIN_DAYS = 8


def keep_within_days() -> int:
    raw = os.environ.get("KEEP_WITHIN_DAYS") or str(KEEP_WITHIN_DAYS)
    days = int(raw)
    if days < 2:  # lock_days >= 1, so keep_within >= 2
        raise ValueError(f"KEEP_WITHIN_DAYS must be >= 2, got {days}")
    return days


def retention(days: int) -> list[str]:
    return [
        "--keep-within",
        f"{days}d",
        "--keep-hourly",
        "24",
        "--keep-daily",
        "14",
        "--keep-weekly",
        "8",
        "--keep-monthly",
        "6",
    ]


RETENTION = retention(KEEP_WITHIN_DAYS)
# Wait for another restic run's lock (e.g. the weekly prune) instead of failing the hourly backup.
RETRY_LOCK = ["--retry-lock", "30m"]
# Relative to the server dir. Shared with restore.py, which keeps these paths on a live restore.
EXCLUDE_PATTERNS = [
    "logs",
    "cache",
    "libraries",
    "versions",
    "crash-reports",
    "plugins/.paper-remapped",
    "plugins/*.jar",  # managed by plugins.lock.yml
    "server.jar",  # symlink to the pinned jar
    ".restored-from",  # restore marker written by restore.py; describes this box, not the world
    ".restore-staging",
]
EXCLUDES = [f"{SERVER_DIR}/{p}" for p in EXCLUDE_PATTERNS]


def restic_limits(env: Mapping[str, str] | None = None) -> list[str]:
    """v1.5: restic's global --limit-upload (KiB/s) and the S3 backend's connection limit, from the unit's
    environment (server.yml backups.upload_limit_kib, performance.vm). Nothing when unset or 0."""
    env = os.environ if env is None else env
    out: list[str] = []
    if (kib := _env_int(env, "BACKUP_UPLOAD_LIMIT_KIB", 0)) > 0:
        out += ["--limit-upload", str(kib)]
    if (conns := _env_int(env, "BACKUP_S3_CONNECTIONS", 0)) > 0:
        out += ["-o", f"s3.connections={conns}"]
    return out


def restic(*args: str) -> None:
    subprocess.run(["restic", *RETRY_LOCK, *restic_limits(), *args], check=True)


def world_dir() -> str:
    props = mc_rcon.SERVER_DIR / "server.properties"
    level = mc_rcon.read_properties(props).get("level-name", "world") if props.exists() else "world"
    return f"{SERVER_DIR}/{level}"


# Chunky 1.5's lang/en.json task_update, the line `chunky progress` prints per running task (= health.py's
# CHUNKY_RUNNING; tests/test_backup.py keeps them equal).
CHUNKY_RUNNING = "Task running for"


def chunky_installed() -> bool:
    """Chunky is among the server's plugins (plugins.lock.yml installs Chunky-<platform>-<version>.jar)."""
    try:
        return any(
            p.name.lower().startswith("chunky") and p.suffix == ".jar"
            for p in (mc_rcon.SERVER_DIR / "plugins").iterdir()
        )
    except OSError:
        return False


def pause_pregen(rcon: mc_rcon.Rcon) -> bool:
    """Before save-off: hold pre-generation and pause Chunky if a task runs. True = it ran (continue it
    after). A failure here never fails the backup."""
    if not chunky_installed():
        return False
    pregen_hold("backup")  # first, so health.py can't continue a task between our look and the pause
    try:
        running = CHUNKY_RUNNING in mc_rcon.strip_formatting(rcon.command("chunky progress"))
    except (OSError, mc_rcon.RconError) as e:
        print(f"pre-generation: `chunky progress` failed ({type(e).__name__}); not paused")
        pregen_release()
        return False
    if not running:
        pregen_release()
        return False
    try:
        rcon.command("chunky pause")
    except (OSError, mc_rcon.RconError) as e:
        print(f"pre-generation: `chunky pause` failed ({type(e).__name__})")
    print("pre-generation paused for the backup")
    return True


def resume_pregen(rcon: mc_rcon.Rcon, paused: bool) -> None:
    """After save-on: continue what pause_pregen paused, and end the hold (always). Never raises: if the
    continue fails, health.py continues the task at its next check (nobody online)."""
    if not paused:
        return
    try:
        rcon.command("chunky continue")
        print("pre-generation continued after the backup")
    except (OSError, mc_rcon.RconError) as e:
        print(f"pre-generation: `chunky continue` failed ({type(e).__name__}); health.py continues it")
    finally:
        pregen_release()


# --- v1.5 smooth backups -----------------------------------------------------------------------------

# systemd passes TRIGGER_UNIT=<timer> to a service a timer started (systemd >= 250; Debian 12 has 252).
TIMER_UNIT = "mc-backup.timer"
# Defer under load (only with players online): TPS (1 min) below DEFER_TPS or MSPT (1 min average) above
# DEFER_MSPT = lagging; a tick spike in progress = the last 5 s averaged over a tick's budget or had a
# tick of at least SPIKE_MS (the Experience tab's "Poor" line).
DEFER_TPS = 18.0
DEFER_MSPT = 40.0
TICK_MS = 50.0
SPIKE_MS = 100.0
DEFER_STEP_S = 60
MAX_DEFER_MINUTES = 15  # = schema backups.max_defer_minutes's default
# Gentler save: the world's files quiet this long after `save-all` = the save's writes are on disk; not quiet
# after SETTLE_MAX_S (chunk unloads keep writing while players explore, autosave or not) -> `save-all flush`.
SETTLE_QUIET_S = 2.0
SETTLE_MAX_S = 15.0
SETTLE_POLL_S = 0.5
STATE_FILE = BACKUP_STATE_FILE
TRIPLE = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")  # = health.py's
PLAYERS = re.compile(r"There are (\d+)")  # = health.py's (vanilla commands.list.players)


def _env_int(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        return int(env.get(name, "") or default)
    except ValueError:
        return default


def _env_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    raw = (env.get(name) or "").strip().lower()
    return default if not raw else raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Settings:
    """server.yml backups.* as the unit passes them (BACKUP_*); the defaults are the schema's."""

    skip_when_idle: bool = True
    defer_when_busy: bool = True
    max_defer_minutes: int = MAX_DEFER_MINUTES
    gentle_save: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = os.environ if env is None else env
        return cls(
            skip_when_idle=_env_bool(env, "BACKUP_SKIP_WHEN_IDLE", True),
            defer_when_busy=_env_bool(env, "BACKUP_DEFER_WHEN_BUSY", True),
            max_defer_minutes=max(0, _env_int(env, "BACKUP_MAX_DEFER_MINUTES", MAX_DEFER_MINUTES)),
            gentle_save=_env_bool(env, "BACKUP_GENTLE_SAVE", True),
        )


def scheduled(env: Mapping[str, str] | None = None) -> bool:
    """The timer started this run (a manual `systemctl start mc-backup.service` sets no TRIGGER_UNIT)."""
    return (os.environ if env is None else env).get("TRIGGER_UNIT") == TIMER_UNIT


def _excluded(rel: str) -> bool:
    """rel (relative to the server dir) is under one of EXCLUDE_PATTERNS (restic's matching: a pattern matches
    whole leading path components, `*` never crosses a `/`)."""
    parts = rel.split("/")
    for pattern in EXCLUDE_PATTERNS:
        pat = pattern.split("/")
        if len(parts) >= len(pat) and all(map(fnmatch.fnmatchcase, parts, pat)):
            return True
    return False


def _files(root: Path) -> Iterator[tuple[str, os.stat_result]]:
    """(relative path, lstat) of every file the backup reads, in a stable order."""
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = os.path.relpath(dirpath, root)
        prefix = "" if rel_dir == "." else rel_dir + "/"
        dirnames[:] = sorted(d for d in dirnames if not _excluded(prefix + d))
        for name in sorted(filenames):
            rel = prefix + name
            if _excluded(rel):
                continue
            try:
                yield rel, os.lstat(os.path.join(dirpath, name))
            except OSError:  # removed meanwhile
                continue


def fingerprint(root: Path | None = None) -> tuple[str, int]:
    """sha256 over (path, size, mtime) of every file the backup reads, and the file count. Equal
    fingerprints = restic would find nothing new (it uses the same metadata to skip unchanged files)."""
    h = hashlib.sha256()
    n = 0
    for rel, st in _files(root or Path(SERVER_DIR)):
        h.update(f"{rel}\0{st.st_size}\0{st.st_mtime_ns}\n".encode())
        n += 1
    return h.hexdigest(), n


def level_dirs() -> list[Path]:
    """The world's folders: <level> (26.x: every dimension in it), pre-26 <level>_nether and _the_end."""
    base = Path(world_dir())
    return [d for d in (base, base.with_name(base.name + "_nether"), base.with_name(base.name + "_the_end"))
            if d.is_dir()]  # fmt: skip


def newest_mtime(dirs: list[Path]) -> float:
    newest = 0.0
    for d in dirs:
        for _rel, st in _files(d):
            newest = max(newest, st.st_mtime)
    return newest


def wait_settled(
    newest: Callable[[], float],
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    quiet_s: float = SETTLE_QUIET_S,
    max_s: float = SETTLE_MAX_S,
) -> float | None:
    """After a `save-all` without flush: seconds until no world file had been written for quiet_s (the
    save's writes have reached the disk), or None if that didn't happen within max_s. Wall clock (mtimes)."""
    start = now()
    while True:
        t = now()
        if t - newest() >= quiet_s:
            return t - start
        if t - start >= max_s:
            return None
        sleep(SETTLE_POLL_S)


def save_world(rcon: mc_rcon.Rcon, gentle: bool, settle: Callable[[], float | None] | None = None) -> str:
    """With save-off in force: write everything to disk. Returns how (for the journal)."""
    if gentle:
        rcon.command("save-all", timeout=120)  # serializes the chunks; the IO threads write them
        waited = (settle or (lambda: wait_settled(lambda: newest_mtime(level_dirs()))))()
        if waited is not None:
            return f"gentle (save-all, files quiet after {waited:.1f} s)"
        rcon.command("save-all flush", timeout=120)
        return f"flush (world files still being written {SETTLE_MAX_S:.0f} s after save-all)"
    rcon.command("save-all flush", timeout=120)  # synchronous; can take a while on a big world
    return "flush"


@dataclass(frozen=True)
class Probe:
    """What the server says right now (None = unknown: no RCON, or a reply we couldn't read)."""

    reachable: bool = False
    players: int | None = None
    tps: float | None = None
    mspt_1m: float | None = None
    mspt_5s: float | None = None
    mspt_5s_max: float | None = None
    pregen_running: bool = False


def parse_tps(text: str) -> float | None:
    """The 1-minute TPS from Purpur's `tps` ("TPS from last 5s, 1m, 5m, 15m: ...")."""
    import health  # the same parser as the health check (Purpur's and Paper's label sets)

    try:
        return health.parse_tps(text)
    except ValueError:
        return None


def parse_mspt(text: str) -> tuple[float, float, float] | None:
    """(5 s average, 5 s max, 1 min average) from Paper's `mspt`: "Server tick times (avg/min/max) from last
    5s, 10s, 1m:" then one avg/min/max triple per window, in that order (PaperMSPTCommand, pinned jar)."""
    triples = TRIPLE.findall(mc_rcon.strip_formatting(text))
    if len(triples) < 3:
        return None
    return float(triples[0][0]), float(triples[0][2]), float(triples[-1][0])


def probe(rcon: mc_rcon.Rcon | None) -> Probe:
    """Players, TPS/MSPT and whether a Chunky task runs. Never raises: whatever fails stays unknown."""
    if rcon is None:
        return Probe()

    def ask(cmd: str) -> str | None:
        try:
            return mc_rcon.strip_formatting(rcon.command(cmd))
        except (OSError, mc_rcon.RconError):
            return None

    listing = ask("list")
    m = PLAYERS.search(listing or "")
    tps_text, mspt_text = ask("tps"), ask("mspt")
    mspt = parse_mspt(mspt_text) if mspt_text is not None else None
    progress = ask("chunky progress") if chunky_installed() else None
    return Probe(
        reachable=listing is not None,
        players=int(m.group(1)) if m else None,
        tps=parse_tps(tps_text) if tps_text is not None else None,
        mspt_5s=mspt[0] if mspt else None,
        mspt_5s_max=mspt[1] if mspt else None,
        mspt_1m=mspt[2] if mspt else None,
        pregen_running=progress is not None and CHUNKY_RUNNING in progress,
    )


def load_state(path: Path | None = None) -> dict[str, Any]:
    try:
        data = json.loads((path or STATE_FILE).read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    """Best effort (0640, atomic): a lost state only costs one unneeded backup."""
    target = path or STATE_FILE
    try:
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(state, sort_keys=True, separators=(",", ":")))
        tmp.chmod(0o640)
        os.replace(tmp, target)
    except OSError as e:
        print(f"backup state not saved ({type(e).__name__}: {e})")


def skip_reason(
    settings: Settings, state: dict[str, Any], p: Probe, current: tuple[str, int], now: float
) -> str | None:
    """Why this scheduled run may be skipped, or None to back up (pure). Every condition must hold: the
    switch is on, a successful backup recorded its fingerprint less than BACKUP_SKIP_MAX_HOURS ago, nobody is
    online (or the server isn't running), no pre-generation task runs (a change in progress), and nothing the
    backup reads changed since (a player who was on in between changed their player data at least)."""
    if not settings.skip_when_idle:
        return None
    last, fp = state.get("last_backup"), state.get("fingerprint")
    if not isinstance(last, int | float) or not isinstance(fp, str):
        return None  # no successful backup recorded yet
    if now - last >= BACKUP_SKIP_MAX_HOURS * 3600:
        return None
    if p.reachable and p.players != 0:
        return None  # someone online, or a player count we couldn't read
    if p.pregen_running:
        return None
    if current[0] != fp:
        return None
    hours = (now - last) / 3600
    return f"nobody played and nothing changed since the last backup {hours:.1f} h ago ({current[1]} files)"


def busy_reason(p: Probe) -> str | None:
    """Why a backup now would hurt players, or None (pure). Only with players online."""
    if not p.players:
        return None
    avg5, max5 = p.mspt_5s, p.mspt_5s_max
    if avg5 is not None and max5 is not None and (avg5 > TICK_MS or max5 >= SPIKE_MS):
        return f"tick spike in progress (last 5 s: {avg5:.1f} ms average, {max5:.0f} ms max)"
    if p.tps is not None and p.tps < DEFER_TPS:
        return f"TPS {p.tps:.1f} (1 min)"
    if p.mspt_1m is not None and p.mspt_1m > DEFER_MSPT:
        return f"MSPT {p.mspt_1m:.1f} ms (1 min average)"
    return None


def defer_while_busy(
    settings: Settings,
    look: Callable[[], Probe],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> float:
    """Wait in DEFER_STEP_S steps while busy_reason() says the server is struggling, at most
    max_defer_minutes; returns the seconds waited (then the backup runs, calm or not)."""
    if not settings.defer_when_busy or settings.max_defer_minutes <= 0:
        return 0.0
    start, limit = clock(), settings.max_defer_minutes * 60
    while (reason := busy_reason(look())) is not None:
        waited = clock() - start
        if waited >= limit:
            print(f"backup: still busy after {waited / 60:.0f} min ({reason}); backing up anyway")
            return waited
        print(f"backup deferred: {reason}; next look in {DEFER_STEP_S} s")
        sleep(min(DEFER_STEP_S, limit - waited))
    return clock() - start


def _connect() -> mc_rcon.Rcon | None:
    try:
        return mc_rcon.connect().__enter__()
    except (OSError, mc_rcon.RconError):
        return None


def smooth(
    settings: Settings,
    connect: Callable[[], mc_rcon.Rcon | None] = _connect,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    current: Callable[[], tuple[str, int]] = fingerprint,
    state_path: Path | None = None,
) -> str | None:
    """A scheduled run before it starts: the skip reason (recorded in the state file), or None after deferring
    while the server is busy. Never raises on its own account: a problem here means "back up"."""
    rcon = connect()
    try:
        state = load_state(state_path)
        first = probe(rcon)
        if settings.skip_when_idle and (reason := skip_reason(settings, state, first, current(), now())):
            state.update(last_skip=now(), skips=int(state.get("skips") or 0) + 1, last_skip_reason=reason)
            save_state(state, state_path)
            return reason
        looks = iter([first])
        defer_while_busy(settings, lambda: next(looks, None) or probe(rcon), sleep, clock)
        return None
    finally:
        if rcon is not None:
            rcon.__exit__(None, None, None)


def record_backup(current: tuple[str, int], at: float, path: Path | None = None) -> None:
    """After a successful `auto` snapshot: what it saw, for the next scheduled run's skip decision."""
    save_state({"last_backup": at, "fingerprint": current[0], "files": current[1], "skips": 0}, path)


def run_backup(
    tag: str = "auto", forget: bool = True, extra_tags: list[str] | None = None, gentle: bool | None = None
) -> None:
    """gentle: v1.5's gentler save (None = BACKUP_GENTLE_SAVE, default on)."""
    # Never snapshot a box without a world: an empty snapshot would become "latest" and a later
    # fresh-box restore would pick it over the real world.
    if not os.path.isdir(world_dir()):
        raise RuntimeError(f"no world at {world_dir()}; refusing to back up")
    if gentle is None:
        gentle = Settings.from_env().gentle_save
    rcon: mc_rcon.Rcon | None = None
    try:
        rcon = mc_rcon.connect().__enter__()
    except (OSError, mc_rcon.RconError) as e:
        print(f"server not reachable over RCON ({e}); backing up as-is")
        rcon = None

    save_off_at = 0.0
    paused = False
    try:
        if rcon:
            paused = pause_pregen(rcon)
            save_off_at = time.monotonic()
            rcon.command("save-off")
            print(f"save: {save_world(rcon, gentle)}")
        cmd = ["backup", SERVER_DIR, "--tag", tag, "--host", "mc-server", "--one-file-system"]
        for extra in extra_tags or []:  # v1.2 world archives: world:<name>, dv:<DataVersion>
            cmd += ["--tag", extra]
        for ex in EXCLUDES:
            cmd += ["--exclude", ex]
        # v1.5: what this snapshot saw (the next scheduled run's skip decision), taken with saving off
        seen, started = fingerprint(), time.time()
        restic(*cmd)
        if tag == "auto" and not extra_tags:
            record_backup(seen, started)
    finally:
        if rcon:
            try:
                rcon.command("save-on")
            finally:
                try:
                    resume_pregen(rcon, paused)
                finally:
                    rcon.__exit__(None, None, None)
            # P13 review M1: how long autosave was off (the crash-loss window); e2e bounds it under load.
            print(f"save-off window: {time.monotonic() - save_off_at:.1f} s")

    if forget:
        # Only snapshots tagged `auto` are ever selected: pre-restore safety snapshots and the world library's
        # archives (tag world-archive, scripts/worlds.py) are outside the retention policy.
        restic(*FORGET_AUTO, *retention(keep_within_days()))


FORGET_AUTO = ["forget", "--host", "mc-server", "--tag", "auto"]


def forget_world_archives() -> None:
    """v1.2: what world_delete left for later (snapshots younger than the bucket lock), and archive snapshots
    beyond the newest two per world, once the lock allows. Never fails the backup."""
    import worlds

    try:
        state = worlds.load_state()
        if state["deleted"] or any(
            len(a.get("snapshots") or []) > worlds.KEEP_ARCHIVES for a in state["archives"]
        ):
            gone = worlds.forget_due(state, worlds.Ops())
            worlds.save_state(state)
            if gone:
                print(f"world library: forgot {len(gone)} archive snapshot(s)")
    except Exception as e:
        print(f"world library: forgetting old archives failed ({type(e).__name__}: {e})")


SNAPSHOTS_KEEP = 200


def snapshot_list(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Newest first, the fields the panel shows (no paths beyond the root, no file lists)."""
    out = []
    for s in sorted(raw, key=lambda x: str(x.get("time", "")), reverse=True)[:SNAPSHOTS_KEEP]:
        summary = s.get("summary") or {}
        item = {
            "id": str(s.get("id", "")),
            "short_id": str(s.get("short_id") or str(s.get("id", ""))[:8]),
            "time": str(s.get("time", "")),
            "tags": [str(t) for t in s.get("tags") or []][:5],
            "host": str(s.get("hostname", ""))[:64],
        }
        if isinstance(summary.get("total_bytes_processed"), int):
            item["size"] = summary["total_bytes_processed"]
        if isinstance(summary.get("data_added"), int):
            item["added"] = summary["data_added"]
        out.append(item)
    return out


def list_snapshots(path: Path = SNAPSHOTS_FILE) -> int:
    proc = subprocess.run(
        ["restic", *RETRY_LOCK, "snapshots", "--json"], check=True, capture_output=True, text=True
    )
    raw = json.loads(proc.stdout or "[]") or []
    snaps = snapshot_list(raw)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"snapshots": snaps}, separators=(",", ":")))
    tmp.chmod(0o640)
    os.replace(tmp, path)
    print(f"{len(snaps)} snapshot(s) listed in {path}")
    refresh_worlds(raw, path.with_name("worlds.json"))
    return len(snaps)


def refresh_worlds(raw: list[dict[str, Any]], state_file: Path) -> None:
    """v1.2: the world library's archive list (restic tags) and the active world's DataVersion, for the panel
    (/var/lib/mc-snapshots/worlds.json; mc-agent reads it)."""
    import worlds

    try:
        level = Path(world_dir())
        worlds.refresh_state(raw, worlds.data_version(level), state_file)
    except Exception as e:
        print(f"world library: archive list not refreshed ({type(e).__name__}: {e})")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--prune", action="store_true", help="run restic prune instead of a backup")
    p.add_argument("--list-snapshots", action="store_true", help="write restic's snapshot list for the panel")
    args = p.parse_args(argv)
    if args.list_snapshots:
        list_snapshots()
        return 0
    if restore_running():
        print("a restore is running (restore.lock held): skipped")
        return 0
    if not args.prune and scheduled():
        settings = Settings.from_env()
        if settings.skip_when_idle or settings.defer_when_busy:
            try:
                reason = smooth(settings)
            except Exception as e:  # never let the smoothing stop a backup
                print(f"skip/defer check failed ({type(e).__name__}: {e}); backing up")
                reason = None
            if reason:
                print(f"backup skipped: {reason}")
                heartbeat("backup", "", f"backup skipped: {reason}")
                return 0
    if not args.prune:
        heartbeat("backup", "start")
    try:
        if args.prune:
            restic("prune")
        else:
            run_backup()
            forget_world_archives()
    except Exception as e:
        heartbeat("backup", "fail", f"{'prune' if args.prune else 'backup'} failed: {e}")
        raise
    if not args.prune:
        heartbeat("backup", "", "backup ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
