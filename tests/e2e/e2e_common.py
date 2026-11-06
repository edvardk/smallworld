"""Shared helpers for the e2e-mock container checks (stdlib only; imported by the scripts next to it).

Determinism (p11-e2e): the product's timers keep running inside the container (mc-backup hourly with a 5 min
random delay, mc-health every minute or so, mc-prune, mc-reboot-gate). A timer that fires while a check drives a
unit by hand can (a) make `systemctl start` join the timer's run instead of starting the check's own (run
41313721479: mc-backup's sandbox probe "ran" as the hourly backup, whose output it then read), (b) count towards a
unit's start rate limit, (c) interleave another run's lines with the ones a check reads, or (d) let self-heal
start a server a check stopped on purpose. So every check runs with the product's timers stopped (and their
services finished), reads only the journal lines of the invocation it started (`_SYSTEMD_INVOCATION_ID`), and
puts the timers back afterwards. Nothing the checks prove changes: the units, sandboxes and polkit rules are the
same; only the background noise is gone.
"""

from __future__ import annotations

import contextlib
import subprocess
import time
from collections.abc import Callable, Iterator


def _sh(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def product_timers() -> list[str]:
    """The product's timer units that are active right now (mc-*.timer)."""
    out = _sh(
        "systemctl", "list-units", "--type=timer", "--state=active", "--plain", "--no-legend", "mc-*.timer"
    )
    return sorted(line.split()[0] for line in out.stdout.splitlines() if line.strip())


def _service_of(timer: str) -> str:
    unit = _sh("systemctl", "show", timer, "-p", "Unit", "--value").stdout.strip()
    return unit or timer.removesuffix(".timer") + ".service"


def wait_inactive(units: list[str], timeout: float = 900) -> None:
    """Waits until none of `units` is activating/active/deactivating (a timer run that already started)."""
    deadline = time.monotonic() + timeout
    while units and time.monotonic() < deadline:
        busy = [u for u in units if _sh("systemctl", "is-active", u).stdout.strip() in ("active", "activating",
                                                                                         "deactivating", "reloading")]  # fmt: skip
        if not busy:
            return
        time.sleep(2)
    raise SystemExit(f"timer-driven units still running after {timeout:.0f} s: {units}")


@contextlib.contextmanager
def timers_paused() -> Iterator[list[str]]:
    """Stops the product's timers, waits for any run they started, and starts them again afterwards."""
    timers = product_timers()
    for timer in timers:
        _sh("systemctl", "stop", timer)
    wait_inactive([_service_of(t) for t in timers])
    try:
        yield timers
    finally:
        for timer in timers:
            _sh("systemctl", "start", timer)


def invocation_id(unit: str) -> str:
    return _sh("systemctl", "show", unit, "-p", "InvocationID", "--value").stdout.strip()


def journal_of(invocation: str) -> str:
    """The lines the processes of one unit invocation logged (none from other runs of the same unit)."""
    if not invocation:
        return ""
    _sh("journalctl", "--sync")
    return _sh("journalctl", f"_SYSTEMD_INVOCATION_ID={invocation}", "-o", "cat", "--no-pager").stdout


def run_oneshot(
    unit: str, *props: str, until: Callable[[str], bool] | None = None, timeout: float = 15
) -> tuple[dict[str, str], str]:
    """Starts a oneshot unit (returns when it finished) and gives its properties and its own journal lines.

    Fails loudly if no new invocation happened (the start joined a run somebody else started). `until` waits
    (up to `timeout` s) for the invocation's lines to satisfy it: journald may still be forwarding them."""
    # A run somebody else started (admin.py's `start --no-block mc-health`, an agent action) would be joined
    # by `systemctl start`: let it finish first, so the start below is ours.
    wait_inactive([unit])
    _sh("systemctl", "reset-failed", unit)  # many runs in a row: no start-rate limit
    before = invocation_id(unit)
    res = _sh("systemctl", "start", unit)
    after = invocation_id(unit)
    if not after or after == before:
        raise SystemExit(
            f"{unit}: no new invocation (before {before!r}, after {after!r}): {res.stderr.strip()}"
        )
    names = props or ("Result", "ExecMainStatus")
    out = _sh("systemctl", "show", unit, "-p", ",".join(names)).stdout
    shown = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    text = journal_of(after)
    deadline = time.monotonic() + timeout
    while until is not None and not until(text) and time.monotonic() < deadline:
        time.sleep(0.5)
        text = journal_of(after)
    if res.returncode != 0:
        text += f"\n(start failed: {res.stderr.strip()})"
    return shown, text
