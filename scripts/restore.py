#!/usr/bin/env python3
"""Restore the server directory from restic.

    restore.py --if-missing          # fresh box: restore latest snapshot only if no world exists (Ansible)
    restore.py --snapshot ID         # explicit restore: the server must be stopped, and admin-stopped
                                     # (admin.py stop) or inside a maintenance window
    restore.py --verify-into DIR     # restore latest into scratch DIR and verify file count + checksum
                                     # sample, plus a usable world (level.dat, >= 1 region file)
    restore.py --verify-into DIR --max-age-hours 2   # also fail if the latest snapshot is older (CI)
    restore.py --snapshot ID --panel # the panel's restore (mc-restore@ID.service, P10): safety snapshot
                                     # (tag pre-restore) -> stop -> fetch -> apply -> start, with STEP lines

After a live restore (--if-missing or --snapshot) the server dir gets a `.restored-from` marker: one
line of JSON {"snapshot_id", "short_id", "snapshot_time", "source_path", "restored_at"}. Backups
exclude it.

Snapshots normally come from the server (tag "auto"), but any snapshot in the repository works
(e.g. a world imported by hand with `restic backup`): its own root path is restored, so it can be
taken from any directory.
Prints RESTORED / SKIPPED / NO_SNAPSHOT / VERIFIED on the last line for automation.

A live restore holds an exclusive flock on /var/lib/minecraft/restore.lock for its whole run: health.py's
self-heal won't start the server while it is held, a backup skips, and a second restore refuses.

Privilege separation (SECURITY.md): restore.py runs as the ops user (`sudo mc-restic restore.py ...`), which
holds the restic credentials but can't write the server dir. It fetches the snapshot into
/var/lib/mc-restore/staging, makes it readable for group minecraft and writes a manifest, then starts
mc-restore-apply.service (polkit allows the ops user exactly that), which runs

    restore.py --apply-staged        # as minecraft, no credentials: copy the staged tree into the server dir

and refuses unless a restore holds restore.lock, the server is stopped and the manifest is there. The world
is therefore always written by the user that runs the server, and no credential is ever in a process that
code running as minecraft could inspect.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import fnmatch
import grp
import hashlib
import json
import os
import random
import re
import shutil
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import backup
import mc_rcon
import notify
from backup import EXCLUDE_PATTERNS

SERVER_DIR = mc_rcon.SERVER_DIR
STAGING_ROOT = Path("/var/lib/mc-restore")  # = mc_restore_dir (ansible/group_vars/all/main.yml)
STAGING = STAGING_ROOT / "staging"
MANIFEST = STAGING_ROOT / "manifest.json"
APPLY_UNIT = "mc-restore-apply.service"
SERVER_GROUP = "minecraft"
MARKER = ".restored-from"
MARKER_KEYS = ("snapshot_id", "short_id", "snapshot_time", "source_path", "restored_at")


def restic_json(*args: str) -> Any:  # noqa: ANN401 - restic JSON
    out = subprocess.run(["restic", *args, "--json"], check=True, capture_output=True, text=True).stdout
    return json.loads(out)


def latest_snapshot(snapshot_id: str | None = None) -> dict[str, Any] | None:
    snaps = restic_json("snapshots")
    if snapshot_id:
        matches = [s for s in snaps if s["id"].startswith(snapshot_id) or s.get("short_id") == snapshot_id]
        if not matches:
            raise SystemExit(f"snapshot {snapshot_id} not found")
        return matches[0]
    # v1.2: a world library archive (tag world-archive) holds a world that isn't the active one; a fresh box
    # restores the newest snapshot of the server as it ran (an explicit id may still name an archive).
    snaps = [s for s in snaps if "world-archive" not in (s.get("tags") or [])]
    return max(snaps, key=lambda s: s["time"]) if snaps else None


def check_not_other_world(snap: dict[str, Any]) -> None:
    """v1.2: the panel's restore never puts an archived world in place of the active one (the Worlds tab
    switches worlds: it archives the active one first and keeps the library's record straight)."""
    tags = [str(t) for t in snap.get("tags") or []]
    if "world-archive" not in tags:
        return
    import worlds

    name = worlds.tag_value(tags, "world:")
    active = worlds.load_state()["active"] or worlds.read_library()["active"]
    if name and name != active:
        raise SystemExit(
            f"FAILED: snapshot {snap.get('short_id', snap['id'][:8])} is the archive of world {name}, not "
            f"of the active world {active or '?'}: switch worlds in the panel's Worlds tab instead"
        )


def level_name(default: str, server_dir: Path | None = None) -> str:
    props = (server_dir or SERVER_DIR) / "server.properties"
    return mc_rcon.read_properties(props).get("level-name", default) if props.exists() else default


def server_running() -> bool:
    r = subprocess.run(["systemctl", "is-active", "--quiet", "minecraft.service"], check=False)
    return r.returncode == 0


def restore_to(snap: dict[str, Any], target: Path, verify: bool = False) -> Path:
    root = snap["paths"][0]
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    cmd = ["restic", "restore", f"{snap['id']}:{root}", "--target", str(target)]
    if verify:
        cmd.append("--verify")
    subprocess.run(cmd, check=True)
    return target


def is_excluded(rel: str) -> bool:
    return any(fnmatch.fnmatchcase(rel, pat) for pat in EXCLUDE_PATTERNS)


def is_real_dir(p: Path) -> bool:
    return p.is_dir() and not p.is_symlink()


def remove(p: Path) -> None:
    if is_real_dir(p):
        shutil.rmtree(p)
    else:
        p.unlink()


def clear_except_excluded(path: Path, base: Path) -> None:
    """Delete path, keeping anything under it that backups exclude (jars, logs, caches...).

    Those aren't in any snapshot, so deleting them would leave e.g. plugins/ without its jars.
    """
    rel = path.relative_to(base).as_posix()
    if is_excluded(rel):
        return
    if is_real_dir(path) and any(pat.startswith(rel + "/") for pat in EXCLUDE_PATTERNS):
        for entry in list(path.iterdir()):
            clear_except_excluded(entry, base)  # keep the dir itself: it holds excluded paths
    elif path.exists() or path.is_symlink():
        remove(path)


def copy_into(src: Path, dest: Path) -> None:
    """Copy src's tree into dest as the running user (minecraft owns what it copies). Paths that already
    exist in dest (only excluded ones remain after clear_except_excluded) win. Symlinks are copied as
    symlinks, never followed; anything that isn't a file, a directory or a symlink is skipped."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in sorted(src.iterdir()):
        target = dest / entry.name
        if is_real_dir(entry):
            if target.is_symlink() or (target.exists() and not is_real_dir(target)):
                continue
            fresh = not target.exists()
            copy_into(entry, target)
            if fresh:
                shutil.copymode(entry, target)
        elif target.exists() or target.is_symlink():
            continue
        elif entry.is_symlink():
            os.symlink(os.readlink(entry), target)
        elif entry.is_file():
            shutil.copy2(entry, target, follow_symlinks=False)


def share_with_server(root: Path, gid: int) -> None:
    """Make a fetched tree readable for the server's group (run by the ops user, which owns it): the apply
    step runs as minecraft and copies it. restic restores the snapshot's modes; a 0600 file would fail."""
    for path in [root, *root.rglob("*")]:
        if path.is_symlink():
            continue
        st = path.lstat()
        if st.st_gid != gid:
            os.lchown(path, -1, gid)
        extra = 0o050 if path.is_dir() else 0o040
        if st.st_mode & extra != extra or st.st_mode & 0o6000:
            path.chmod((st.st_mode | extra) & 0o1777)  # never set-id bits (RestrictSUIDSGID in mc-run)


@contextlib.contextmanager
def restore_lock(path: Path | None = None) -> Iterator[None]:
    """Hold the restore lock (exclusive flock) for the duration; refuse if another restore holds it."""
    lock = path or notify.RESTORE_LOCK_FILE
    try:
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    except PermissionError:
        # Left by a version that ran restores as minecraft; the marker dir belongs to the ops user now.
        lock.unlink()
        fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another restore is running (restore.lock is held)") from None
        os.ftruncate(fd, 0)
        os.write(fd, json.dumps({"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()}).encode())
        yield
    finally:
        os.close(fd)  # releases the lock; the file stays (an unheld lock file means nothing)


def check_can_restore_live(explicit: bool) -> None:
    """A live restore needs the server stopped; an explicit one (--snapshot) also needs it stopped *on
    purpose*, or self-heal/the deploy could start it halfway (README: Restore the live world)."""
    if server_running():
        raise SystemExit("refusing to restore while minecraft.service is running; stop it first")
    if explicit and not (notify.admin_stopped() or notify.maintenance_active()):
        raise SystemExit(
            "refusing to restore: the server isn't admin-stopped and no maintenance window is open. "
            'Run `sudo /opt/minecraft/bin/admin.py stop "restore"` first (or open a maintenance window).'
        )


def restore_live(snap: dict[str, Any], explicit: bool = False) -> None:
    """The ops user's half: fetch into the staging dir, then let mc-restore-apply.service copy it in."""
    check_can_restore_live(explicit)
    with restore_lock():
        check_can_restore_live(explicit)  # re-checked under the lock
        _restore_live(snap)


def _restore_live(snap: dict[str, Any]) -> None:
    try:
        fetch(snap)
        start_apply()
    finally:
        clean_staging()


def fetch(snap: dict[str, Any], now: datetime | None = None) -> None:
    restore_to(snap, STAGING)
    share_with_server(STAGING, grp.getgrnam(SERVER_GROUP).gr_gid)
    tmp = MANIFEST.with_name(MANIFEST.name + ".tmp")
    tmp.write_text(json.dumps(marker_record(snap, now or datetime.now(UTC)), sort_keys=True) + "\n")
    tmp.chmod(0o640)
    tmp.replace(MANIFEST)


def start_apply() -> None:
    """Run mc-restore-apply.service and wait for it (oneshot); a failed apply fails the restore."""
    proc = subprocess.run(["systemctl", "--no-ask-password", "start", APPLY_UNIT], check=False)
    if proc.returncode != 0:
        raise SystemExit(f"{APPLY_UNIT} failed (journalctl -u {APPLY_UNIT}); the world may be half-restored")


def clean_staging() -> None:
    shutil.rmtree(STAGING, ignore_errors=True)
    MANIFEST.unlink(missing_ok=True)


def read_manifest() -> dict[str, str]:
    try:
        record = json.loads(MANIFEST.read_text())
    except (OSError, ValueError) as e:
        raise SystemExit(f"no usable restore manifest ({MANIFEST}): {type(e).__name__}") from None
    if not isinstance(record, dict) or not all(isinstance(record.get(k), str) for k in MARKER_KEYS):
        raise SystemExit(f"malformed restore manifest ({MANIFEST})")
    return {k: record[k] for k in MARKER_KEYS}


def apply_staged() -> dict[str, str]:
    """minecraft's half (mc-restore-apply.service): replace the world with the staged snapshot."""
    if not notify.restore_running():
        raise SystemExit("refusing to apply: no restore holds restore.lock")
    if server_running():
        raise SystemExit("refusing to apply while minecraft.service is running")
    record = read_manifest()
    if not is_real_dir(STAGING):
        raise SystemExit(f"nothing staged in {STAGING}")
    SERVER_DIR.mkdir(parents=True, exist_ok=True)
    # Replace each top-level entry the snapshot has; anything else (e.g. a world moved aside) stays.
    for entry in STAGING.iterdir():
        clear_except_excluded(SERVER_DIR / entry.name, SERVER_DIR)
    copy_into(STAGING, SERVER_DIR)
    # Backups exclude logs/, but the JVM's -Xlog:gc:file=logs/gc.log refuses to start without it.
    (SERVER_DIR / "logs").mkdir(mode=0o750, exist_ok=True)
    path = SERVER_DIR / MARKER
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
    tmp.replace(path)
    return record


PANEL_STEPS = 6
SNAPSHOT_ID = r"[0-9a-f]{8,64}"
UNIT = "minecraft.service"


def step(n: int, text: str) -> None:
    """Progress for the panel: mc-agent follows these lines in the unit's journal."""
    print(f"STEP {n}/{PANEL_STEPS}: {text}", flush=True)


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", "--no-ask-password", *args], check=True, timeout=600)


def mark_stopped(reason: str) -> None:
    record = {
        "since": datetime.now(UTC).isoformat(timespec="seconds"),
        "reason": reason[:200],
        "by": "restore",
    }
    tmp = notify.ADMIN_STOPPED_FILE.with_name(notify.ADMIN_STOPPED_FILE.name + ".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
    tmp.chmod(0o644)
    tmp.replace(notify.ADMIN_STOPPED_FILE)


def read_marker() -> str | None:
    try:
        return notify.ADMIN_STOPPED_FILE.read_text()
    except OSError:
        return None


def unmark(prior: str | None) -> None:
    """Put the admin-stopped marker back the way it was before the restore (absent, or the admin's own)."""
    if prior is None:
        notify.ADMIN_STOPPED_FILE.unlink(missing_ok=True)
        return
    tmp = notify.ADMIN_STOPPED_FILE.with_name(notify.ADMIN_STOPPED_FILE.name + ".tmp")
    tmp.write_text(prior)
    tmp.chmod(0o644)
    tmp.replace(notify.ADMIN_STOPPED_FILE)


def restore_panel(snapshot_id: str) -> str:
    """The panel's restore, run by mc-restore@<id>.service as the ops user (restic credentials, read access
    to the world with CAP_DAC_READ_SEARCH for the safety snapshot). Every step prints a STEP line.

    Safety first: the current world is snapshotted (tag pre-restore, kept by the bucket lock like every
    snapshot) while the server still runs (save-off/save-all flush over RCON), and a failure there stops
    the restore before anything changes. Then: mark the server admin-stopped (by=restore: a deploy or
    self-heal running meanwhile leaves it down), stop, fetch, apply (mc-restore-apply.service, as minecraft),
    put the marker back as it was and start (unless the server was admin-stopped before). If the apply fails
    the world may be half-restored: the server stays down (admin-stopped) with the safety snapshot's id in the
    marker."""
    if not re.fullmatch(SNAPSHOT_ID, snapshot_id):
        raise SystemExit("FAILED: the snapshot id must be 8-64 hex characters")
    step(1, f"looking up snapshot {snapshot_id}")
    snap = latest_snapshot(snapshot_id)
    assert snap is not None
    check_not_other_world(snap)
    short = snap.get("short_id", snap["id"][:8])
    was_stopped = notify.admin_stopped()
    prior = read_marker() if was_stopped else None
    notify.maintenance(120, f"panel restore of {short}")
    applied = False
    marked = False
    safety = ""
    try:
        with restore_lock():
            step(2, "safety snapshot of the current world (tag pre-restore)")
            if os.path.isdir(backup.world_dir()):
                before = {s["id"] for s in restic_json("snapshots")}
                backup.run_backup(tag="pre-restore", forget=False)
                new = [s for s in restic_json("snapshots") if s["id"] not in before]
                safety = new[0].get("short_id", new[0]["id"][:8]) if new else "?"
            else:
                safety = "none (there was no world to save)"
            step(3, f"stopping the server (safety snapshot {safety})")
            # P10 review: from here until the end the server is marked admin-stopped (by=restore), so a deploy
            # (minecraft role, restart handler) or self-heal that runs meanwhile can't start it mid-restore.
            marked = True
            mark_stopped(f"restore of {short} in progress (started {datetime.now(UTC):%H:%M} UTC)")
            if server_running():
                systemctl("stop", UNIT)
            check_can_restore_live(explicit=True)
            try:
                step(4, f"fetching snapshot {short} ({snap['time'][:19]})")
                fetch(snap)
                step(5, "applying it to the server dir (mc-restore-apply.service, as minecraft)")
                applied = True
                start_apply()
            finally:
                clean_staging()
        unmark(prior)
        marked = False
        if was_stopped:
            step(6, "done; the server stays stopped (it was admin-stopped before)")
        else:
            step(6, "starting the server")
            systemctl("start", UNIT)
    except BaseException as e:
        why = e.code if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}"
        if applied:
            mark_stopped(
                f"restore of {short} failed while applying; world may be half-restored: restore {safety}"
            )
            print(
                f"FAILED while applying ({why}); the server stays stopped. Safety snapshot: {safety}",
                flush=True,
            )
        else:
            if marked:
                unmark(prior)
            if not was_stopped and not server_running():
                with contextlib.suppress(Exception):
                    systemctl("start", UNIT)
            print(f"FAILED before anything changed ({why}); the world is untouched", flush=True)
        raise SystemExit(1) from None
    finally:
        notify.maintenance(0, f"panel restore of {short}")
        with contextlib.suppress(Exception):
            backup.list_snapshots()
    return f"RESTORED {short} (safety snapshot {safety})"


def marker_record(snap: dict[str, Any], now: datetime) -> dict[str, str]:
    return {
        "snapshot_id": snap["id"],
        "short_id": snap.get("short_id", snap["id"][:8]),
        "snapshot_time": snap["time"],
        "source_path": snap["paths"][0],
        "restored_at": now.astimezone(UTC).isoformat(timespec="seconds"),
    }


def snapshot_age_hours(snap: dict[str, Any], now: datetime | None = None) -> float:
    taken = datetime.fromisoformat(snap["time"])
    return ((now or datetime.now(UTC)) - taken).total_seconds() / 3600


def check_world(root: Path, default_level: str = "world") -> str:
    """Fail unless root holds a usable world: <level>/level.dat and >= 1 region file.

    The overworld's region files are in <level>/dimensions/minecraft/overworld/region/ since 26.x and in
    <level>/region/ before, so any region/*.mca under the world folder counts.
    """
    level = level_name(default_level, root)
    world = root / level
    if not (world / "level.dat").is_file():
        raise SystemExit(f"no {level}/level.dat in the snapshot")
    regions = [*world.glob("region/*.mca"), *world.glob("*/**/region/*.mca")]
    if not regions:
        raise SystemExit(f"no region files (region/*.mca) under {level}/")
    return f"{level}: level.dat + {len(regions)} region file(s)"


def snapshot_files(snap: dict[str, Any]) -> dict[str, int]:
    """Map of relative path -> size for regular files in the snapshot (under its root)."""
    root = snap["paths"][0].rstrip("/") + "/"
    proc = subprocess.run(["restic", "ls", "--json", snap["id"]], check=True, capture_output=True, text=True)
    files = {}
    for line in proc.stdout.splitlines():
        node = json.loads(line)
        if node.get("struct_type") == "node" and node.get("type") == "file" and node["path"].startswith(root):
            files[node["path"][len(root) :]] = node.get("size", 0)
    return files


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_dump(snap: dict[str, Any], rel: str) -> str:
    path = snap["paths"][0].rstrip("/") + "/" + rel
    h = hashlib.sha256()
    with subprocess.Popen(["restic", "dump", snap["id"], path], stdout=subprocess.PIPE) as p:
        assert p.stdout is not None
        for chunk in iter(lambda: p.stdout.read(1 << 20), b""):  # type: ignore[union-attr]
            h.update(chunk)
    if p.returncode != 0:
        raise SystemExit(f"restic dump failed for {rel}")
    return h.hexdigest()


def verify_into(target: Path, sample: int = 20, max_age_hours: float | None = None) -> None:
    snap = latest_snapshot()
    if snap is None:
        raise SystemExit("no snapshots to verify")
    age = snapshot_age_hours(snap)
    if max_age_hours is not None and age > max_age_hours:
        raise SystemExit(
            f"latest snapshot {snap['short_id']} is {age:.1f} h old (> {max_age_hours:g} h): "
            "backups have stopped"
        )
    restore_to(snap, target, verify=True)
    expected = snapshot_files(snap)
    restored = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file() and not p.is_symlink()}
    if set(expected) != restored:
        missing, extra = set(expected) - restored, restored - set(expected)
        raise SystemExit(f"file set mismatch: {len(missing)} missing, {len(extra)} unexpected")
    picks = random.sample(sorted(expected), min(sample, len(expected)))
    for rel in picks:
        if sha256_file(target / rel) != sha256_dump(snap, rel):
            raise SystemExit(f"checksum mismatch: {rel}")
    world = check_world(target)
    print(
        f"snapshot {snap['short_id']} ({age:.1f} h old): {len(expected)} files, "
        f"{len(picks)} checksums sampled, {world}"
    )
    print("VERIFIED")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--if-missing", action="store_true")
    g.add_argument("--snapshot")
    g.add_argument("--verify-into", type=Path)
    g.add_argument("--apply-staged", action="store_true", help="mc-restore-apply.service only")
    p.add_argument("--panel", action="store_true", help="with --snapshot: mc-restore@ID.service (P10)")
    p.add_argument("--level-name", default="world", help="world folder to look for with --if-missing")
    p.add_argument(
        "--max-age-hours", type=float, help="with --verify-into: fail if the latest snapshot is older"
    )
    args = p.parse_args(argv)
    if args.max_age_hours is not None and not args.verify_into:
        p.error("--max-age-hours only applies to --verify-into")

    if args.verify_into:
        verify_into(args.verify_into, max_age_hours=args.max_age_hours)
        return 0
    if args.panel:
        if not args.snapshot:
            p.error("--panel needs --snapshot")
        print(restore_panel(args.snapshot), flush=True)
        return 0
    if args.apply_staged:
        record = apply_staged()
        print(f"applied snapshot {record['short_id']} ({record['snapshot_time']}) to {SERVER_DIR}")
        return 0
    if args.if_missing and (SERVER_DIR / level_name(args.level_name)).exists():
        print("SKIPPED")
        return 0
    snap = latest_snapshot(args.snapshot)
    if snap is None:
        print("NO_SNAPSHOT")
        return 0
    restore_live(snap, explicit=bool(args.snapshot))
    print(f"restored snapshot {snap['short_id']} ({snap['time']}) from {snap['paths'][0]}")
    print("RESTORED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
