#!/usr/bin/env python3
"""Stop or start the Minecraft server on purpose (design §5 "Stop"): the admin-stopped marker.

    sudo /opt/minecraft/bin/admin.py stop [REASON]   # marker, then a graceful stop (save-all flush + stop)
    sudo /opt/minecraft/bin/admin.py start           # marker removed, server started
    sudo /opt/minecraft/bin/admin.py status          # prints the marker (JSON) or "running as usual"

While /var/lib/minecraft/admin-stopped exists, nothing starts the server behind the admin's back:
health.py's self-heal stands down, the watchdog (told through health.py's heartbeat) doesn't alert on the
probes, and a deploy leaves it stopped (with a warning). `mcctl stop|start -C <config>` runs this over
Tailscale SSH; the panel's stop/start (P10) writes the same marker. `start` refuses on a box `mcctl migrate`
fenced (/var/lib/minecraft/fenced). Stdlib only, run as root.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import notify

UNIT = "minecraft.service"
HEALTH_UNIT = "mc-health.service"
MAX_REASON = 200


def write_marker(reason: str, path: Path | None = None, now: datetime | None = None) -> Path:
    marker = path or notify.ADMIN_STOPPED_FILE
    record = {
        "since": (now or datetime.now(UTC)).astimezone(UTC).isoformat(timespec="seconds"),
        "reason": " ".join(reason.split())[:MAX_REASON] or "stopped by an admin",
        "by": os.environ.get("SUDO_USER") or os.environ.get("USER") or "unknown",
    }
    tmp = marker.with_name(marker.name + ".tmp")
    tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
    tmp.chmod(0o644)
    os.replace(tmp, marker)
    return marker


def systemctl(*args: str) -> None:
    subprocess.run(["systemctl", *args], check=True, timeout=300)


def report_now() -> None:
    """Run the health check now, so the watchdog learns the new state without waiting for the timer."""
    try:
        systemctl("start", "--no-block", HEALTH_UNIT)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"couldn't start {HEALTH_UNIT}: {e} (the timer reports within 5 minutes)", file=sys.stderr)


def stop(reason: str) -> int:
    # Marker first: from this moment self-heal won't restart the server, even mid-stop.
    write_marker(reason)
    systemctl("stop", UNIT)  # ExecStop: save-all flush + stop over RCON
    report_now()
    print(f"ADMIN_STOPPED: {UNIT} stopped; it stays down until `admin.py start`.")
    return 0


def start() -> int:
    if notify.fenced():
        print(
            f"FENCED: {notify.FENCED_FILE} exists (mcctl migrate moved the world to another box); "
            "not starting. Only `mcctl migrate --rollback` unfences this box.",
            file=sys.stderr,
        )
        return 3
    notify.ADMIN_STOPPED_FILE.unlink(missing_ok=True)
    systemctl("start", UNIT)
    report_now()
    print(f"STARTED: {UNIT} started; self-heal and the watchdog watch it again.")
    return 0


def status() -> int:
    try:
        print(notify.ADMIN_STOPPED_FILE.read_text().strip())
    except FileNotFoundError:
        print("not admin-stopped: running as usual (or being healed)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["stop"] and len(args) <= 2:
        return stop(args[1] if len(args) == 2 else "")
    if args == ["start"]:
        return start()
    if args == ["status"]:
        return status()
    print("usage: admin.py stop [REASON] | start | status", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
