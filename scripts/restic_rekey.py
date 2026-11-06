#!/usr/bin/env python3
"""Make sure the restic password a deploy is about to write opens the repository (P12 review, H2).

    mc-run -/etc/minecraft/restic.env /opt/minecraft/bin/restic_rekey.py < NEW_RESTIC_ENV
 Run by the backups role before it writes restic.env, as the ops user through mc-run: the process environment
is the CURRENT restic.env (none on a fresh box), stdin the NEW one (the rendered template; Ansible passes it
as the module's stdin, never on a command line). `mcctl secrets rotate restic-key` replaces the password in
Terraform first, so any deploy that runs afterwards (CI's weekly schedule, a panel merge, a push, or mcctl's
own) gets the
new password before the repository knows it. This script closes that gap, whoever deploys:

- the new environment opens the repository                  -> NEW_OK   (nothing to do)
- the repository doesn't exist yet (restic rc 10)           -> NO_REPO  (the role runs `restic init`)
- the current environment opens it                          -> `restic key add` with the new password on
  (the new password is a key the repository doesn't know)      stdin, then checked with the new environment
                                                            -> KEY_ADDED
- neither opens it                                          -> exit 1, restic.env left as it is

The new environment is used whole (repository, R2 keys, password), so a rotated R2 token works as well. Last
line is the result word; no secret is printed or put on argv. Stdlib only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Mapping

RESTIC = "restic"
KEYS = (
    "RESTIC_REPOSITORY",
    "RESTIC_PASSWORD",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "RESTIC_CACHE_DIR",
)


def parse_env(text: str) -> dict[str, str]:
    """systemd EnvironmentFile lines KEY=VALUE (the template writes them unquoted); comments skipped."""
    env: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()
    return env


def restic_env(base: Mapping[str, str], values: Mapping[str, str]) -> dict[str, str]:
    env = {k: v for k, v in base.items() if k not in KEYS and not k.startswith("RESTIC_")}
    env.update({k: values[k] for k in KEYS if values.get(k)})
    return env


def restic(env: Mapping[str, str], *args: str, stdin: str | None = None) -> int:
    proc = subprocess.run(
        [RESTIC, *args], env=dict(env), input=stdin, capture_output=True, text=True, check=False, timeout=600
    )
    if proc.returncode not in (0, 10):
        # never restic's own text: it may name the repository URL
        why = "wrong password or no key found" if "wrong password" in (proc.stderr or "") else "another error"
        print(f"restic {' '.join(args[:2])}: rc {proc.returncode} ({why})", file=sys.stderr)
    return proc.returncode


def rekey(current: Mapping[str, str], new: Mapping[str, str], base: Mapping[str, str]) -> str:
    new_env = restic_env(base, new)
    if not new.get("RESTIC_PASSWORD") or not new.get("RESTIC_REPOSITORY"):
        raise SystemExit("the new restic environment has no repository or password")
    rc = restic(new_env, "cat", "config")
    if rc == 0:
        return "NEW_OK"
    if rc == 10:
        return "NO_REPO"
    if not current.get("RESTIC_PASSWORD"):
        raise SystemExit(
            "the new restic password doesn't open the repository and there is no current one to add it"
        )
    cur_env = restic_env(base, current)
    if restic(cur_env, "cat", "config") != 0:
        raise SystemExit("neither the new nor the current restic environment opens the repository")
    if (
        restic(
            cur_env, "key", "add", "--new-password-file", "/dev/stdin", stdin=new["RESTIC_PASSWORD"] + "\n"
        )
        != 0
    ):
        raise SystemExit("`restic key add` with the new password failed")
    if restic(new_env, "cat", "config") != 0:
        raise SystemExit("the new key was added, but the new environment still doesn't open the repository")
    return "KEY_ADDED"


def main() -> int:
    new = parse_env(sys.stdin.read())
    current = {k: os.environ[k] for k in KEYS if os.environ.get(k)}
    print(rekey(current, new, os.environ), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
