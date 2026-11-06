#!/usr/bin/env python3
"""The weekly backup test, on the server (mc-backup-check.timer; until v1.2 the config repo's Monday
`backup-check` Actions job, which a manual dispatch still runs).

    backup_check.py [--max-age-hours H] [--scratch DIR]

As the ops user with the restic credentials (mc-backup-check.service: EnvironmentFile=restic.env), against the
off-site repository (R2), in this order:

1. `restic snapshots --latest 3` (the log shows what there is);
2. `restic check --read-data-subset=5%`: the repository's structure and a random 5% of the data;
3. room for a test restore: the newest snapshot's restore size (+10%, +256 MiB) must fit in the scratch
   directory's file system (the unit's private /var/tmp, on the server's disk);
4. restore.py's --verify-into: the newest snapshot restored with --verify, its file list compared, 20
   checksums sampled, a usable world (level.dat + region files), and the snapshot no older than
   --max-age-hours (the unit passes twice the backup period: backups have stopped otherwise).

The scratch copy is deleted afterwards, whatever happened. A failure goes to Discord in the alert template
(scripts/alerts.py; DISCORD_WEBHOOK_URL from restic.env, empty = only the journal) and fails the unit. Nothing
secret goes into the message: the step and our own description of what failed (never restic's output, which
can name the repository). Skipped (exit 0) while a restore holds restore.lock. Stdlib only, Python >= 3.11.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

import notify
from alerts import Alert, render

RETRY_LOCK = ["--retry-lock", "30m"]  # = backup.RETRY_LOCK: wait for a running backup's lock, don't fail
# The unit's PrivateTmp: nobody else sees it, and systemd removes it when the run ends.
SCRATCH = Path("/var/tmp/mc-backup-check")  # noqa: S108
MARGIN = 1.1
SLACK_BYTES = 256 << 20
STEPS = {
    "list": "listing the snapshots",
    "check": "checking the repository (structure + 5% of the data)",
    "space": "making room for the test restore",
    "restore": "restoring and verifying the newest snapshot",
}

Run = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class Failed(Exception):
    def __init__(self, step: str, detail: str) -> None:
        super().__init__(f"{step}: {detail}")
        self.step, self.detail = step, detail


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # stdout captured (stats is JSON); stderr goes to the journal as it is.
    return subprocess.run(list(argv), stdout=subprocess.PIPE, text=True, check=False)


def _verify(target: Path, max_age_hours: float) -> None:
    import restore  # the box's restore.py (imports grp: Linux only), next to this script

    restore.verify_into(target, max_age_hours=max_age_hours)


def restic(run: Run, step: str, *args: str) -> str:
    proc = run(["restic", *RETRY_LOCK, *args])
    if proc.returncode != 0:
        raise Failed(step, f"`restic {args[0]}` exited {proc.returncode}")
    return proc.stdout or ""


def check(
    run: Run = _run,
    verify: Callable[[Path, float], None] = _verify,
    free_bytes: Callable[[Path], int] = lambda p: shutil.disk_usage(p).free,
    scratch: Path = SCRATCH,
    max_age_hours: float = 2.0,
) -> None:
    print(restic(run, "list", "snapshots", "--latest", "3"), end="")
    restic(run, "check", "check", "--read-data-subset=5%")
    try:
        stats = json.loads(restic(run, "space", "stats", "latest", "--mode", "restore-size", "--json"))
        size = int(stats["total_size"])
    except (ValueError, KeyError, TypeError) as e:
        raise Failed("space", f"unexpected `restic stats` output ({type(e).__name__})") from e
    scratch.parent.mkdir(parents=True, exist_ok=True)
    need, free = int(size * MARGIN) + SLACK_BYTES, free_bytes(scratch.parent)
    if free < need:
        raise Failed("space", f"the newest snapshot needs {need >> 20} MiB, {free >> 20} MiB free")
    try:
        verify(scratch, max_age_hours)
    except SystemExit as e:  # restore.py's checks fail with SystemExit(message)
        raise Failed("restore", str(e.code)) from e
    except (subprocess.CalledProcessError, OSError) as e:
        raise Failed("restore", f"{type(e).__name__} while restoring") from e
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def alert(step: str, detail: str) -> str:
    return render(
        Alert(
            "action",
            "The weekly backup test failed",
            impact="Players are not affected right now, but the backups may not be restorable: a crash or a "
            "mistake could lose their progress.",
            cause=f"The server's weekly test failed while {STEPS.get(step, step)}: {detail}.",
            todo="On the server, `sudo journalctl -u mc-backup-check` shows the details. Then `mcctl doctor` "
            "(backup storage credentials) and `mcctl backup now`; once a backup has run, re-run the test "
            "with `sudo systemctl start mc-backup-check` (or Actions, server, Run workflow, backup-check).",
            details=f"mc-backup-check: {step} failed",
        )
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--max-age-hours", type=float, default=2.0)
    ap.add_argument("--scratch", type=Path, default=SCRATCH)
    args = ap.parse_args(argv)
    if notify.restore_running():
        print("a restore is running (restore.lock held): skipped")
        return 0
    try:
        check(scratch=args.scratch, max_age_hours=args.max_age_hours)
    except Failed as e:
        text = alert(e.step, e.detail)
        print(text, file=sys.stderr)
        notify.discord(text)
        return 1
    print("backup test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
