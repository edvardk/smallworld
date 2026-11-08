#!/usr/bin/env python3
"""Outbound notifications: watchdog heartbeats and maintenance windows, and an optional Discord webhook.

Stdlib only. Settings come from the environment (systemd EnvironmentFile=/etc/minecraft/*.env):

    WATCHDOG_URL        https://hb.<hostname>.<zone> (the Cloudflare Worker watchdog, design §5)
    HEARTBEAT_TOKEN     bearer token for it (Terraform random_password; never on a command line)
    DISCORD_WEBHOOK_URL optional; empty = no Discord

Each is optional: unset = that channel is off. Nothing here ever raises into the caller (a failed
notification must not fail a backup or a health check), and no URL or token is ever printed.

CLI (run through `mc-run /etc/minecraft/notify.env ...` so the token stays out of argv):

    notify.py maintenance MINUTES REASON    open (MINUTES > 0) or close (0) a maintenance window: the local
                                            marker health.py's self-heal respects, and the watchdog's window

The markers that make self-heal (and the watchdog, through health.py's heartbeat) stand down live in
/var/lib/minecraft and are defined here: maintenance.json (this CLI), admin-stopped (admin.py stop, the
panel's stop), restore.lock (held with flock by restore.py for the whole restore) and fenced (`mcctl migrate`
fenced this box: nothing may start the server or its helpers, and a deploy refuses to run). One more marker
stands only pre-generation down: pregen-hold.json (v1.4, /var/lib/mc-snapshots: backup.py holds Chunky paused
for its save-off window, health.py leaves pre-generation alone while it is set).
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

STATE_DIR = Path("/var/lib/minecraft")  # = mc_state_dir (ansible/group_vars/all/main.yml)
# health.py reads this; written by `notify.py maintenance` (as the ops user, via mc-run).
MAINTENANCE_FILE = STATE_DIR / "maintenance.json"
# The server was stopped on purpose (admin.py stop / the panel): nothing may start it, nothing alerts.
ADMIN_STOPPED_FILE = STATE_DIR / "admin-stopped"
# restore.py holds an exclusive flock on this while it replaces the world.
RESTORE_LOCK_FILE = STATE_DIR / "restore.lock"
# `mcctl migrate` (or its rollback) fenced this box: another box owns the world now. Written as root by the
# fence script, removed only by mcctl's unfence; the units carry ConditionPathExists=! on it,
# admin.py/mc-agent refuse to start the server, self-heal and the reboot gate stand down, and site.yml fails
# before changing anything.
FENCED_FILE = STATE_DIR / "fenced"
# restic's snapshot list for the panel (P10): backup.py --list-snapshots writes it after every backup and
# restore (StateDirectory=mc-snapshots of those units, the ops user), mc-agent reads it.
SNAPSHOTS_FILE = Path("/var/lib/mc-snapshots/snapshots.json")
# v1.4: backup.py paused Chunky for its save-off window (chunks generated while saving is off pile up in
# memory): health.py must not continue it until backup.py does. In mc-snapshots, the StateDirectory every
# unit that backs up already writes (mc-backup, mc-world, mc-restore@), the ops user; health.py (the ops user)
# reads it. Like maintenance.json it carries its own expiry, so a backup killed before its `finally` holds
# nothing for long.
PREGEN_HOLD_FILE = SNAPSHOTS_FILE.with_name("pregen-hold.json")
PREGEN_HOLD_MAX_S = 4 * 3600
CHECKS = ("health", "backup")
STATUSES = ("", "start", "fail")
MAX_MAINTENANCE_MINUTES = 1440  # = the Worker's limit


def _post(url: str, body: bytes, content_type: str, token: str | None = None) -> bytes:
    """POST and return the start of the response body."""
    if not url.startswith("https://"):
        raise ValueError("notification URLs must be https")
    headers = {"Content-Type": content_type, "User-Agent": "smallworld"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")  # noqa: S310 - https only
    with urllib.request.urlopen(req, timeout=10) as resp:  # noqa: S310
        return resp.read(4096)


def _where(url: str) -> str:
    return url.split("?", 1)[0]


def heartbeat_reply(
    check: str, status: str = "", message: str = "", *, admin_stopped: bool | None = None
) -> dict[str, object] | None:
    """POST /hb/<check>[/start|/fail] to the watchdog. Never raises.

    Returns the watchdog's JSON reply ({} if it isn't JSON), or None if nothing was delivered (off or
    failed). With `admin_stopped` the body is JSON {message, admin_stopped}, which the watchdog stores.
    """
    if check not in CHECKS or status not in STATUSES:
        raise ValueError(f"unknown heartbeat {check}/{status}")
    base, token = os.environ.get("WATCHDOG_URL", ""), os.environ.get("HEARTBEAT_TOKEN", "")
    if not base or not token:
        return None
    url = f"{base.rstrip('/')}/hb/{check}" + (f"/{status}" if status else "")
    if admin_stopped is None:
        body, ctype = message.encode()[:4000], "text/plain; charset=utf-8"
    else:
        body = json.dumps({"message": message[:3000], "admin_stopped": admin_stopped}).encode()
        ctype = "application/json"
    try:
        reply = _post(url, body, ctype, token)
    except (OSError, ValueError, urllib.error.URLError) as e:
        print(f"watchdog heartbeat to {_where(url)} failed: {type(e).__name__}", file=sys.stderr)
        return None
    try:
        data = json.loads(reply or b"{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def heartbeat(check: str, status: str = "", message: str = "") -> bool:
    """POST /hb/<check>[/start|/fail] to the watchdog. Never raises; True if it was delivered."""
    return heartbeat_reply(check, status, message) is not None


def maintenance(minutes: int, reason: str, *, now: float | None = None, path: Path | None = None) -> None:
    """Open (minutes > 0) or close (0) a maintenance window, locally and on the watchdog. Never raises."""
    minutes = max(0, min(int(minutes), MAX_MAINTENANCE_MINUTES))
    reason = " ".join(reason.split())[:200]
    marker = path or MAINTENANCE_FILE
    try:
        if minutes:
            until = (time.time() if now is None else now) + 60 * minutes
            tmp = marker.with_name(marker.name + ".tmp")
            tmp.write_text(json.dumps({"until": until, "reason": reason}))
            os.replace(tmp, marker)
        else:
            marker.unlink(missing_ok=True)
    except OSError as e:
        print(f"maintenance marker {marker}: {e}", file=sys.stderr)
    base, token = os.environ.get("WATCHDOG_URL", ""), os.environ.get("HEARTBEAT_TOKEN", "")
    if base and token:
        url = f"{base.rstrip('/')}/maintenance"
        try:
            _post(url, json.dumps({"minutes": minutes, "reason": reason}).encode(), "application/json", token)
        except (OSError, ValueError, urllib.error.URLError) as e:
            print(f"watchdog maintenance at {_where(url)} failed: {type(e).__name__}", file=sys.stderr)


def maintenance_active(now: float | None = None, path: Path | None = None) -> str | None:
    """The reason of an open local maintenance window, or None."""
    try:
        data = json.loads((path or MAINTENANCE_FILE).read_text())
        until, reason = float(data["until"]), str(data.get("reason") or "maintenance")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return reason if until > (time.time() if now is None else now) else None


def pregen_hold(reason: str, *, now: float | None = None, path: Path | None = None) -> None:
    """Hold pre-generation (backup.py, around save-off). Never raises."""
    marker = path or PREGEN_HOLD_FILE
    until = (time.time() if now is None else now) + PREGEN_HOLD_MAX_S
    try:
        tmp = marker.with_name(marker.name + ".tmp")
        tmp.write_text(json.dumps({"until": until, "reason": " ".join(reason.split())[:100]}))
        os.replace(tmp, marker)
    except OSError as e:
        print(f"pre-generation hold {marker}: {e}", file=sys.stderr)


def pregen_release(path: Path | None = None) -> None:
    """End the hold. Never raises."""
    marker = path or PREGEN_HOLD_FILE
    try:
        marker.unlink(missing_ok=True)
    except OSError as e:
        print(f"pre-generation hold {marker}: {e}", file=sys.stderr)


def pregen_held(now: float | None = None, path: Path | None = None) -> str | None:
    """The reason of a current pre-generation hold (health.py leaves Chunky alone), or None."""
    try:
        data = json.loads((path or PREGEN_HOLD_FILE).read_text())
        until, reason = float(data["until"]), str(data.get("reason") or "hold")
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return reason if until > (time.time() if now is None else now) else None


def admin_stopped(path: Path | None = None) -> bool:
    return (path or ADMIN_STOPPED_FILE).exists()


def fenced(path: Path | None = None) -> bool:
    return (path or FENCED_FILE).exists()


def restore_running(path: Path | None = None) -> bool:
    """True while restore.py holds its lock (a stale lock file without a holder doesn't count)."""
    try:
        fd = os.open(path or RESTORE_LOCK_FILE, os.O_RDONLY)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    except OSError:
        return False
    finally:
        os.close(fd)  # closing drops our shared lock too
    return False


def discord(message: str, url: str | None = None) -> None:
    """Post to the Discord webhook if configured. Never raises."""
    url = url if url is not None else os.environ.get("DISCORD_WEBHOOK_URL", "")
    if not url:
        return
    try:
        _post(url, json.dumps({"content": message[:1900]}).encode(), "application/json")
    except Exception as e:
        print(f"discord notify failed: {type(e).__name__}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) == 3 and args[0] == "maintenance" and args[1].isdigit():
        maintenance(int(args[1]), args[2])
        return 0
    print("usage: notify.py maintenance MINUTES REASON", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
