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
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import mc_rcon
from notify import SNAPSHOTS_FILE, heartbeat, pregen_hold, pregen_release, restore_running

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


def restic(*args: str) -> None:
    subprocess.run(["restic", *RETRY_LOCK, *args], check=True)


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


def run_backup(tag: str = "auto", forget: bool = True, extra_tags: list[str] | None = None) -> None:
    # Never snapshot a box without a world: an empty snapshot would become "latest" and a later
    # fresh-box restore would pick it over the real world.
    if not os.path.isdir(world_dir()):
        raise RuntimeError(f"no world at {world_dir()}; refusing to back up")
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
            rcon.command("save-all flush", timeout=120)  # synchronous; can take a while on a big world
        cmd = ["backup", SERVER_DIR, "--tag", tag, "--host", "mc-server", "--one-file-system"]
        for extra in extra_tags or []:  # v1.2 world archives: world:<name>, dv:<DataVersion>
            cmd += ["--tag", extra]
        for ex in EXCLUDES:
            cmd += ["--exclude", ex]
        restic(*cmd)
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
