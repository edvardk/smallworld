"""P11 acceptance inside the e2e-mock container (run as root after agent_check.py; stdlib only): the reboot gate.

site.yml installed mc-reboot-gate (as mcops, sandboxed) with REBOOT_TARGET=mc-e2e-reboot.target and a whole-day
window (tests/e2e/vars.yml): starting that target is the "reboot". Checks, through the real unit and polkit:
  - nothing pending -> no reboot;
  - admin-stopped -> it waits (and so does it while a maintenance window is open);
  - /run/reboot-required + nobody online (RCON `list`) -> the target is started by mcops through polkit, a
    maintenance window is opened, the decision is audited and recorded with this boot's id;
  - a run right after (same boot) doesn't reboot again; a "new boot" (the recorded boot id changed) -> "back";
  - outside the window -> it waits.
The players-online and 3-day-warning paths are unit-tested (tests/test_reboot_gate.py): no fake players here.

    python3 tests/e2e/reboot_check.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

from e2e_common import run_oneshot, timers_paused

TARGET = "mc-e2e-reboot.target"
REQUIRED = Path("/run/reboot-required")
STATE = Path("/var/lib/mc-reboot-gate/state.json")
MARKER = Path("/var/lib/minecraft/admin-stopped")
MAINT = Path("/var/lib/minecraft/maintenance.json")
DECIDED = re.compile(r"^(AUDIT reboot-gate action=reboot |[a-z]+: )", re.M)
failures: list[str] = []


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def sh(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def gate_run() -> str:
    """One run of the real unit; the journal lines of exactly this invocation."""
    _, out = run_oneshot("mc-reboot-gate.service", until=DECIDED.search)  # its last line: the decision
    return out


def target_active() -> bool:
    return sh("systemctl", "is-active", TARGET).stdout.strip() == "active"


def main() -> int:
    with timers_paused():  # every timer, not only the gate's: nothing else starts or restarts units meanwhile
        checks()
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


def checks() -> None:
    Path(f"/etc/systemd/system/{TARGET}").write_text(
        "[Unit]\nDescription=e2e stand-in for reboot.target (the reboot gate's mock reboot)\n"
    )
    sh("systemctl", "daemon-reload")
    props = sh("systemctl", "show", "mc-reboot-gate.service", "-p", "User", "-p", "Environment").stdout
    expect(
        "User=mcops" in props and f"REBOOT_TARGET={TARGET}" in props,
        f"the gate runs as mcops ({props.strip()})",
    )
    REQUIRED.unlink(missing_ok=True)
    out = gate_run()
    expect(
        "none: no reboot needed" in out and not target_active(),
        f"nothing pending: no reboot ({out.strip()[-120:]})",
    )

    REQUIRED.write_text("*** System restart required ***\n")
    MARKER.write_text(json.dumps({"by": "e2e", "reason": "reboot gate check"}) + "\n")
    out = gate_run()
    expect("admin-stopped" in out and not target_active(), f"admin-stopped: it waits ({out.strip()[-120:]})")
    MARKER.unlink()

    MAINT.write_text(json.dumps({"until": 0, "reason": "e2e: no window left open by earlier checks"}))
    out = gate_run()
    expect(
        "AUDIT reboot-gate action=reboot" in out,
        f"pending + window + nobody online: reboot ({out.strip()[-160:]})",
    )
    expect(target_active(), f"{TARGET} started by mcops through polkit (the mock reboot)")
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    boot_id = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    expect(
        state.get("rebooting", {}).get("boot_id") == boot_id,
        f"the reboot is recorded with this boot ({state})",
    )
    maint = json.loads(MAINT.read_text()) if MAINT.exists() else {}
    expect(
        maint.get("until", 0) > time.time() and "reboot" in maint.get("reason", ""),
        f"maintenance window ({maint})",
    )

    sh("systemctl", "stop", TARGET)
    out = gate_run()
    expect(
        "a moment ago" in out and not target_active(),
        f"same boot, minutes later: no second reboot ({out.strip()[-120:]})",
    )

    # "after the boot": the recorded boot id is another one; the pending flag is gone with the reboot
    state["rebooting"]["boot_id"] = "e2e-previous-boot"
    STATE.write_text(json.dumps(state))
    REQUIRED.unlink()
    out = gate_run()
    expect(
        "AUDIT reboot-gate action=back" in out and not target_active(),
        f"a new boot: back ({out.strip()[-120:]})",
    )
    after = json.loads(STATE.read_text())
    expect("rebooting" not in after and "since" not in after, f"state cleared ({after})")
    maint = json.loads(MAINT.read_text()) if MAINT.exists() else {}
    expect(
        not maint.get("until") or maint["until"] <= time.time() + 1,
        f"the maintenance window closed ({maint})",
    )

    # outside the window (an empty one-minute window that isn't now)
    REQUIRED.write_text("*** System restart required ***\n")
    hour = (time.localtime().tm_hour + 12) % 24
    dropin = Path("/etc/systemd/system/mc-reboot-gate.service.d")
    dropin.mkdir(exist_ok=True)
    (dropin / "e2e-window.conf").write_text(
        f'[Service]\nEnvironment="REBOOT_WINDOW={hour:02d}:00-{hour:02d}:01"\n'
    )
    sh("systemctl", "daemon-reload")
    out = gate_run()
    expect(
        "outside the reboot window" in out and not target_active(),
        f"outside the window: it waits ({out.strip()[-120:]})",
    )
    (dropin / "e2e-window.conf").unlink()
    dropin.rmdir()
    REQUIRED.unlink()
    STATE.unlink(missing_ok=True)
    sh("systemctl", "daemon-reload")


if __name__ == "__main__":
    sys.exit(main())
