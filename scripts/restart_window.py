#!/usr/bin/env python3
"""v1.3: a Minecraft restart a deploy left for the maintenance window (server.yml maintenance.window).

Stdlib only (>= 3.11). A deploy that needs a restart outside the window (and isn't forced) restarts nothing:
the minecraft role stages the merged config overlays under PENDING_DIR/files/<path under the server dir>
(mc_config `stage`), writes the marker PENDING_DIR/pending.json and runs `deferred` (Discord). The jar,
plugin, unit and JRE changes are already on disk; they take effect at the next start.

    restart_window.py apply               minecraft.service ExecStartPre (as minecraft, the server stopped):
                                          copy the staged files into the server dir, then empty PENDING_DIR.
                                          Every start applies a pending change: this timer's, a panel or
                                          `mcctl restart`, self-heal, a reboot.
    restart_window.py                     mc-restart-window.service (timer: the window's start and every
                                          15 minutes; as the ops user): inside the window, with a marker and
                                          nothing in the way, the deploy's restart (maintenance window,
                                          in-game countdown if anyone is online, `systemctl restart`)
    restart_window.py --status            what it would do now (no action)
    restart_window.py deferred WINDOW TZ  the Discord note that a restart is pending (the deploy, via mc-run)

Env (the timer's unit): RESTART_WINDOW (HH:MM-HH:MM, may cross midnight), RESTART_TZ, RESTART_COUNTDOWN
(seconds), DISCORD_WEBHOOK_URL + WATCHDOG_URL + HEARTBEAT_TOKEN (notify.env).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo

import mc_rcon
import notify
from alerts import Alert, render

PENDING_DIR = notify.STATE_DIR / "restart-pending"  # = mc_restart_pending_dir (group_vars/all/main.yml)
MARKER = "pending.json"
FILES = "files"
SERVER_DIR = mc_rcon.SERVER_DIR
UNIT = "minecraft.service"
BACKUP_UNIT = "mc-backup.service"
MAINTENANCE_MIN = 20
RCON_WAIT_S = 900
WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")


def parse_window(text: str) -> str:
    if not WINDOW.fullmatch(text.strip()):
        raise ValueError(f"the window must be HH:MM-HH:MM, not {text!r}")
    return text.strip()


def in_window(window: str, now: datetime.datetime) -> bool:
    """Start included, end excluded; a window may cross midnight (= mcctl.schema.in_window, site.yml)."""
    start, end = window.split("-")
    t = f"{now.hour:02d}:{now.minute:02d}"
    return start <= t < end if start <= end else (t >= start or t < end)


def deferred_alert(window: str, tz: str) -> str:
    start = window.split("-")[0]
    return render(
        Alert(
            "info",
            f"A config change waits for the maintenance window ({window} {tz})",
            impact="Nothing for players now: the server keeps running the old settings until it restarts.",
            cause="A deploy needed a Minecraft restart outside server.yml maintenance.window, so it applied "
            "the files and left the restart for later.",
            todo=f"No action needed: the server restarts at {start} {tz} (any earlier restart applies the "
            "change too). To restart now: `mcctl deploy --force-restart`, or run the workflow with "
            "force_restart.",
            details=f"pending restart in {PENDING_DIR}",
        )
    )


def restarted_alert(window: str, tz: str, since: str) -> str:
    return render(
        Alert(
            "recovered",
            "The server restarted in the maintenance window: the pending config change is live",
            impact="Players can join again.",
            details=f"window {window} {tz}; pending since {since or 'unknown'}",
        )
    )


# --- apply (ExecStartPre, as minecraft) --------------------------------------------------------------------


def apply(pending: Path = PENDING_DIR, server_dir: Path = SERVER_DIR) -> list[str]:
    """Copy the staged files into the server dir (atomically each, keeping an existing file's mode), then
    empty the pending directory: the restart it waited for is this start. Returns the copied paths."""
    files, copied = pending / FILES, []
    if files.is_dir():
        for src in sorted(files.rglob("*")):
            if src.is_symlink() or not src.is_file():
                continue  # staged by mc_config: regular files only
            rel = src.relative_to(files)
            dest = server_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            mode = dest.stat().st_mode & 0o777 if dest.is_file() else 0o640
            fd, tmp = tempfile.mkstemp(dir=dest.parent, prefix=".restart-window.")
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(src.read_bytes())
                os.chmod(tmp, mode)
                os.replace(tmp, dest)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
            copied.append(str(rel))
        shutil.rmtree(files)
    (pending / MARKER).unlink(missing_ok=True)
    return copied


# --- the timer (as the ops user) ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Facts:
    now: datetime.datetime  # aware, in the window's time zone
    pending: bool
    server_active: bool
    admin_stopped: bool  # or fenced
    restore_running: bool
    backup_running: bool
    maintenance: str | None  # an open maintenance window's reason (a deploy, a restore, the reboot gate)


def decide(facts: Facts, window: str) -> tuple[str, str]:
    """(none | wait | restart, why): the timer's whole policy, pure."""
    if not facts.pending:
        return "none", "no restart is pending"
    reasons = [
        (not in_window(window, facts.now), f"outside the maintenance window ({window})"),
        (facts.admin_stopped, "the server is admin-stopped (its next start applies the change)"),
        (not facts.server_active, "the server isn't running (its next start applies the change)"),
        (facts.maintenance is not None, f"a maintenance window is open ({facts.maintenance})"),
        (facts.restore_running, "a restore is running"),
        (facts.backup_running, "a backup is running"),
    ]
    for blocked, why in reasons:
        if blocked:
            return "wait", why
    return "restart", f"inside the maintenance window ({window})"


def unit_active(unit: str) -> bool:
    out = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=30, check=False
    )
    return out.stdout.strip() in ("active", "activating", "reloading")


def gather(tz: ZoneInfo, pending: Path = PENDING_DIR) -> Facts:
    return Facts(
        now=datetime.datetime.now(tz),
        pending=(pending / MARKER).exists(),
        server_active=unit_active(UNIT),
        admin_stopped=notify.admin_stopped() or notify.fenced(),
        restore_running=notify.restore_running(),
        backup_running=unit_active(BACKUP_UNIT),
        maintenance=notify.maintenance_active(),
    )


def since(pending: Path | None = None) -> str:
    try:
        return str(json.loads(((pending or PENDING_DIR) / MARKER).read_text()).get("since", ""))
    except (OSError, ValueError, AttributeError):
        return ""


def wait_for_rcon(deadline_s: float = RCON_WAIT_S) -> bool:
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        try:
            with mc_rcon.connect(timeout=5.0) as r:
                r.command("list")
            return True
        except (OSError, mc_rcon.RconError, KeyError, ValueError):
            time.sleep(5)
    return False


def restart(window: str, tz: str, countdown: int) -> int:
    pending_since = since()
    print(f"AUDIT restart-window action=restart window={window} pending_since={pending_since}", flush=True)
    notify.maintenance(MAINTENANCE_MIN, "restart in the maintenance window")
    try:
        mc_rcon.countdown(countdown)  # skipped when nobody is online; Discord when someone is
        subprocess.run(["systemctl", "--no-ask-password", "restart", UNIT], check=True, timeout=600)
        if not wait_for_rcon():
            print(f"RCON didn't answer within {RCON_WAIT_S} s after the restart", file=sys.stderr)
            return 1
    finally:
        notify.maintenance(0, "restart in the maintenance window")
    if (PENDING_DIR / MARKER).exists():
        print("restarted, but the pending change is still staged (ExecStartPre failed?)", file=sys.stderr)
        return 1
    notify.discord(restarted_alert(window, tz, pending_since))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args == ["apply"]:
        copied = apply()
        print(f"restart-window: applied {', '.join(copied) if copied else 'no staged files'}")
        return 0
    if len(args) == 3 and args[0] == "deferred":
        notify.discord(deferred_alert(parse_window(args[1]), args[2]))
        return 0
    if args not in ([], ["--status"]):
        print(__doc__, file=sys.stderr)
        return 2
    env = os.environ
    window = parse_window(env.get("RESTART_WINDOW", ""))
    tz_name = env.get("RESTART_TZ") or "UTC"
    action, why = decide(gather(ZoneInfo(tz_name)), window)
    print(f"{action}: {why}", flush=True)
    if action != "restart" or args == ["--status"]:
        return 0
    return restart(window, tz_name, int(env.get("RESTART_COUNTDOWN", "60")))


if __name__ == "__main__":
    sys.exit(main())
