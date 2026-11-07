"""v1.3 acceptance inside the e2e-mock container (run as root after reboot_check.py; stdlib only): the restart a
deploy left for the maintenance window (server.yml maintenance.window, scripts/restart_window.py).

tests/e2e/vars.yml sets a whole-day window, so site.yml installed mc-restart-window.{service,timer} and every deploy
still restarted at once. Here a deferred restart is staged by hand, as tasks/defer.yml would (a staged file and the
marker, owned by minecraft), then the real unit runs it:
  - nothing pending -> the unit's condition skips it (no run at all);
  - pending -> mcops restarts minecraft.service through polkit (nobody online: no countdown), the unit's
    ExecStartPre copies the staged file into the server dir and empties the pending directory, the restart is
    audited and the maintenance window is closed again.
The window, the players-online and the blocked paths are unit-tested (tests/test_restart_window.py).

    python3 tests/e2e/restart_window_check.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from e2e_common import run_oneshot, timers_paused

UNIT = "mc-restart-window.service"
PENDING = Path("/var/lib/minecraft/restart-pending")
SERVER = Path("/opt/minecraft/server")
PROBE = "restart-window-e2e.txt"
MAINT = Path("/var/lib/minecraft/maintenance.json")
failures: list[str] = []


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def sh(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def show(unit: str, prop: str) -> str:
    return sh("systemctl", "show", unit, "-p", prop, "--value").stdout.strip()


def main() -> int:
    with timers_paused():  # nothing else starts or restarts units meanwhile
        checks()
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


def checks() -> None:
    expect(show(UNIT, "User") == "mcops", f"{UNIT} runs as mcops")
    expect(
        "restart_window.py apply" in show("minecraft.service", "ExecStartPre"),
        "minecraft.service applies at start",
    )

    (PENDING / "pending.json").unlink(missing_ok=True)
    shutil.rmtree(PENDING / "files", ignore_errors=True)
    sh("systemctl", "start", UNIT)
    expect(show(UNIT, "ConditionResult") == "no", "nothing pending: the unit's condition skips it")

    before = show("minecraft.service", "InvocationID")
    (SERVER / PROBE).unlink(missing_ok=True)
    (PENDING / "files").mkdir(parents=True, exist_ok=True)
    (PENDING / "files" / PROBE).write_text("staged by restart_window_check.py\n")
    (PENDING / "pending.json").write_text('{"since": "2026-11-07T12:00:00Z", "window": "00:00-23:59"}\n')
    shutil.chown(PENDING / "files", "minecraft", "minecraft")
    for f in (PENDING / "files" / PROBE, PENDING / "pending.json"):
        shutil.chown(f, "minecraft", "minecraft")
        f.chmod(0o640)
    shown, out = run_oneshot(UNIT, "Result", until=lambda text: "restart:" in text, timeout=60)
    expect(
        shown.get("Result") == "success", f"the deferred restart succeeded ({shown}; {out.strip()[-300:]})"
    )
    expect("AUDIT restart-window action=restart" in out, "the restart is audited")
    expect(show("minecraft.service", "InvocationID") != before, "minecraft.service was restarted")
    expect(show("minecraft.service", "ActiveState") == "active", "and runs again")
    probe = SERVER / PROBE
    expect(
        probe.is_file() and "staged by" in probe.read_text(), "ExecStartPre copied the staged file into place"
    )
    expect(probe.is_file() and probe.owner() == "minecraft", "as minecraft")
    expect(
        not (PENDING / "pending.json").exists() and not (PENDING / "files").exists(),
        "the pending dir is empty",
    )
    expect(not MAINT.exists(), "the maintenance window is closed again")
    probe.unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(main())
