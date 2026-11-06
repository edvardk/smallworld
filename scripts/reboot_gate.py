#!/usr/bin/env python3
"""mc-reboot-gate: reboot for pending updates only when nobody is playing (design §6 "Reboots", Q6, P11).

Stdlib only (>= 3.11). Runs as the ops user from mc-reboot-gate.timer (every 30 minutes, and 3 minutes after
boot). Unattended upgrades never reboot by themselves (base role); this decides when:

- `/var/run/reboot-required` exists (Debian/Ubuntu packages that need a reboot write it),
- the local time (REBOOT_TZ, = host.timezone) is inside REBOOT_WINDOW (automation.reboot_window,
  `HH:MM-HH:MM`, may wrap midnight),
- nobody is online (RCON `list`; the server down counts as 0, RCON unreachable while it runs as unknown),
- no backup and no restore are running, the server isn't admin-stopped and no maintenance window is open.

Then: a Discord message, a 20-minute watchdog maintenance window, an audit line, the state file, and
`systemctl start --job-mode=replace-irreversibly --no-block <REBOOT_TARGET>` (reboot.target; polkit allows the
ops user exactly that unit + verb, base role 50-mc-ops.rules). After the boot (a new boot id) it announces
that the host is back and closes the window once the server runs again. **Never forced:** with players online
it waits for the next window; still pending after 3 days, Discord says so (at most once a day), nothing else
happens.

    reboot_gate.py            # mc-reboot-gate.service (timer)
    reboot_gate.py --status   # what it would do now (no action)

Env: REBOOT_WINDOW, REBOOT_TZ, REBOOT_TARGET (reboot.target; the e2e run uses a harmless target),
DISCORD_WEBHOOK_URL + WATCHDOG_URL + HEARTBEAT_TOKEN (notify.env), STATE_DIRECTORY (systemd).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import mc_rcon
import notify
from alerts import Alert, render

REQUIRED = Path("/var/run/reboot-required")
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
UNIT = "minecraft.service"
BACKUP_UNIT = "mc-backup.service"
WARN_AFTER_S = 3 * 86400  # design §6 / Q6: still pending after 3 days -> Discord
WARN_EVERY_S = 86400
REBOOT_GRACE_S = 15 * 60  # a reboot we started that hasn't happened yet: don't start another
MAINTENANCE_MIN = 20
WINDOW = re.compile(r"([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)")
TARGET = re.compile(r"[a-z0-9][a-z0-9@._-]{0,63}\.target")


@dataclass(frozen=True)
class Facts:
    now: datetime.datetime  # aware, local (REBOOT_TZ)
    required: bool
    boot_id: str
    server_active: bool
    players: int | None  # None = unknown (RCON unreachable while the server runs)
    backup_running: bool
    restore_running: bool
    admin_stopped: bool
    maintenance: str | None  # the open window's reason


@dataclass(frozen=True)
class Decision:
    action: str  # none | wait | reboot
    reason: str
    warn: str | None = None  # a Discord message (pending for 3+ days)
    back: bool = False  # the host came back from a reboot this gate started


def rebooting_alert(reason: str) -> str:
    return render(
        Alert(
            "info",
            "Rebooting the server host for security updates",
            impact="Nobody is online; the server is back in a few minutes.",
            cause="Security updates installed overnight need a reboot, and the reboot window found the "
            "server empty.",
            details=reason,
        )
    )


def back_alert(boot_id: str) -> str:
    return render(
        Alert(
            "recovered",
            "The server host is back after its reboot for security updates",
            impact="Players can join again.",
            details=f"boot {boot_id[:8]}",
        )
    )


def parse_window(text: str) -> tuple[datetime.time, datetime.time]:
    m = WINDOW.fullmatch(text.strip())
    if not m:
        raise ValueError(f"REBOOT_WINDOW must be HH:MM-HH:MM, not {text!r}")
    h1, m1, h2, m2 = map(int, m.groups())
    return datetime.time(h1, m1), datetime.time(h2, m2)


def in_window(now: datetime.datetime, window: tuple[datetime.time, datetime.time]) -> bool:
    start, end = window
    t = now.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= t < end
    return t >= start or t < end  # wraps midnight (e.g. 23:30-01:00)


def decide(
    facts: Facts, state: dict[str, Any], window: tuple[datetime.time, datetime.time]
) -> tuple[Decision, dict[str, Any]]:
    """The gate's whole policy, pure: (what to do, the new state)."""
    new = dict(state)
    ts = facts.now.timestamp()
    back = False
    rebooting = new.get("rebooting")
    if isinstance(rebooting, dict):
        if rebooting.get("boot_id") != facts.boot_id:
            back = True
            new.pop("rebooting", None)
        elif ts - float(rebooting.get("at", 0)) < REBOOT_GRACE_S:
            return Decision("wait", "a reboot was started a moment ago"), new
        else:
            new.pop("rebooting", None)  # it never happened (refused?): decide afresh
    if not facts.required:
        new.pop("since", None)
        new.pop("warned", None)
        return Decision("none", "no reboot needed", back=back), new
    since = float(new.get("since") or ts)
    new["since"] = since
    warn = None
    pending = ts - since
    if pending >= WARN_AFTER_S and ts - float(new.get("warned") or 0) >= WARN_EVERY_S:
        new["warned"] = ts
        warn = render(
            Alert(
                "warning",
                f"Security updates are waiting for a reboot ({int(pending // 86400)} days now)",
                impact="Nothing for players yet; the host runs without the latest security fixes until it "
                "reboots.",
                cause="The nightly reboot window never found the server empty, and a reboot is never forced "
                "while players are online.",
                todo="Reboot by hand when nobody minds a few minutes' break: `sudo systemctl reboot` on "
                "the box "
                "(the server stops gracefully and starts again on boot).",
                details=f"pending for {int(pending // 86400)} days; reminder at most once a day",
            )
        )
    reasons = [
        (
            not in_window(facts.now, window),
            f"outside the reboot window ({window[0]:%H:%M}-{window[1]:%H:%M})",
        ),
        (facts.admin_stopped, "the server is admin-stopped"),
        (facts.maintenance is not None, f"a maintenance window is open ({facts.maintenance})"),
        (facts.restore_running, "a restore is running"),
        (facts.backup_running, "a backup is running"),
        (facts.server_active and facts.players is None, "can't tell who is online (RCON unreachable)"),
        (bool(facts.players), f"{facts.players} player(s) online"),
    ]
    for blocked, why in reasons:
        if blocked:
            return Decision("wait", why, warn, back), new
    new["rebooting"] = {"at": ts, "boot_id": facts.boot_id}
    return Decision(
        "reboot",
        f"reboot pending since {datetime.datetime.fromtimestamp(since, facts.now.tzinfo):%Y-%m-%d %H:%M}",
        warn,
        back,
    ), new


# --- the box -------------------------------------------------------------------------------------------


def unit_active(unit: str) -> bool:
    out = subprocess.run(
        ["systemctl", "is-active", unit], capture_output=True, text=True, timeout=30, check=False
    )
    return out.stdout.strip() in ("active", "activating", "reloading", "deactivating")


def players_online(active: bool) -> int | None:
    if not active:
        return 0
    try:
        with mc_rcon.connect(timeout=10.0) as r:
            text = mc_rcon.strip_formatting(r.command("list"))
    except (OSError, mc_rcon.RconError, KeyError, ValueError):
        return None
    m = re.search(r"There are (\d+) of a max", text)
    return int(m.group(1)) if m else None


def gather(tz: ZoneInfo) -> Facts:
    active = unit_active(UNIT)
    return Facts(
        now=datetime.datetime.now(tz),
        required=REQUIRED.exists(),
        boot_id=BOOT_ID.read_text().strip() if BOOT_ID.exists() else "",
        server_active=active,
        players=players_online(active),
        backup_running=unit_active(BACKUP_UNIT),
        restore_running=notify.restore_running(),
        admin_stopped=notify.admin_stopped() or notify.fenced(),  # fenced (mcctl migrate): stand down
        maintenance=notify.maintenance_active(),
    )


def load_state(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, state: dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(state, sort_keys=True) + "\n")
    tmp.replace(path)


def audit(action: str, reason: str) -> None:
    print(f"AUDIT reboot-gate action={action} reason={reason}", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    env = os.environ
    window = parse_window(env.get("REBOOT_WINDOW", "04:30-06:00"))
    tz = ZoneInfo(env.get("REBOOT_TZ") or "UTC")
    target = env.get("REBOOT_TARGET", "reboot.target")
    if not TARGET.fullmatch(target):
        raise SystemExit(f"REBOOT_TARGET must be a .target unit name, not {target!r}")
    state_path = Path(env.get("STATE_DIRECTORY", "/var/lib/mc-reboot-gate").split(":")[0]) / "state.json"
    facts = gather(tz)
    decision, new = decide(facts, load_state(state_path), window)
    if args == ["--status"]:
        print(f"{decision.action}: {decision.reason}")
        return 0
    if decision.back:
        audit("back", f"up again after the reboot (boot {facts.boot_id[:8]})")
        notify.discord(back_alert(facts.boot_id))
        if facts.server_active:
            notify.maintenance(0, "reboot")
    if decision.warn:
        notify.discord(decision.warn)
        audit("warn", "reboot pending for 3+ days")
    if decision.action == "reboot":
        save_state(state_path, new)  # first: the next run (after the boot) must know it was us
        audit("reboot", decision.reason)
        notify.maintenance(MAINTENANCE_MIN, "reboot for security updates")
        notify.discord(rebooting_alert(decision.reason))
        subprocess.run(
            ["systemctl", "--no-ask-password", "start", "--no-block", "--job-mode=replace-irreversibly",
             target],
            check=True, timeout=60,
        )  # fmt: skip
        return 0
    save_state(state_path, new)
    print(f"{decision.action}: {decision.reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
