#!/usr/bin/env python3
"""Tell Discord that a scheduled CI job of the config repo failed, in the alert template (scripts/alerts.py).

Usage (the reusable workflow's `if: failure()` steps):  ci_alert.py {backup-check,deploy} RUN_URL

DISCORD_WEBHOOK_URL from the environment (the config repo's optional Actions secret; empty = alerts off: the
message is only printed). Stdlib only, like the box's scripts, so the backup-check job needs no pip install.
Nothing secret goes into a message: the job name and the run's link only.
"""

from __future__ import annotations

import os
import sys

import notify
from alerts import Alert, render

KINDS = ("backup-check", "deploy")


def alert(kind: str, run_url: str) -> str:
    if kind == "backup-check":
        return render(
            Alert(
                "action",
                "The weekly backup test failed",
                impact="Players are not affected right now, but the backups may not be restorable: a crash "
                "or a mistake could lose their progress.",
                cause="The Monday check couldn't list, verify or restore the newest backup, or the newest "
                "one is older than 2 hours (the failing step in the run says which).",
                todo=f"Open the run: {run_url} . Then `mcctl doctor` (backup storage credentials) and "
                "`mcctl backup now`; once a backup has run, re-run the check (Actions, server, Run workflow, "
                "backup-check).",
                details="backup-check job failed",
            )
        )
    return render(
        Alert(
            "action",
            "A deploy failed",
            impact="This push is not (fully) applied. The server usually keeps running with what it had; if "
            "the deploy failed while restarting it or in the smoke test, players may not be able to join.",
            cause="A step of the deploy job failed (the run says which: the client pack mirror, the tailnet, "
            "Ansible, or the smoke test from the internet).",
            todo=f"Open the run: {run_url} . Fix the cause, then push again or re-run the deploy (Actions, "
            "server, Run workflow, deploy); `mcctl doctor` checks credentials and reachability.",
            details="deploy job failed",
        )
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2 or args[0] not in KINDS or not args[1].startswith("https://"):
        print(f"usage: ci_alert.py {{{','.join(KINDS)}}} RUN_URL", file=sys.stderr)
        return 2
    text = alert(args[0], args[1])
    print(text)
    if os.environ.get("DISCORD_WEBHOOK_URL"):
        notify.discord(text)
    else:
        print("DISCORD_WEBHOOK_URL is empty: not sent (alerts.discord is off)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
