#!/usr/bin/env python3
"""Skip a config repo's deploy when nothing deploy-relevant changed since the last successful one (docs/ci.md
"Keeping a config repo's minutes low").

    deploy_record.py decide --config DIR --product-sha SHA --event EVENT --record FILE [--before SHA]
        # the reusable workflow's validate job: deploy_needed=true|false, fingerprint= to $GITHUB_OUTPUT
    deploy_record.py write --config DIR --product-sha SHA --out FILE
        # the deploy job, after it succeeded: the record the next run compares against
    deploy_record.py fingerprint --config DIR --product-sha SHA

What a deploy applies is the product commit (the caller's `uses:` pin) plus the config repo's files as of the
commit it checked out. The fingerprint is a sha256 over both: the product sha and the git tree entries (mode,
blob id, path) of every file that can change what a deploy does. Only files that can't are left out:
Markdown, `.gitignore`, `.env.example`, `expiry.yml` (read by expiry-check only) and `.github/` other than the
workflow itself (which holds the pin and the secrets the deploy gets).

The record is a small JSON file the deploy job saves to the Actions cache after a successful deploy (key
`mc-deployed-<run id>-<attempt>`); the validate job restores the newest one (restore-keys `mc-deployed-`). The
deploy is needed unless every one of these holds:

- the event is not workflow_dispatch (a manual deploy always deploys);
- for a push: the push didn't touch `.github/workflows/server.yml` (a product pin change always deploys, even
  if a forged record claimed it was deployed already; that is git's answer, not the cache's);
- a record exists, has this format, the same product sha and the same fingerprint.

Anything unknown (no record, a cache miss or eviction after 7 idle days, an unreadable record, a git error)
means "deploy": in doubt, the answer is never "skip". Stdlib only (any python3 on the runner).
"""

from __future__ import annotations

import argparse
import datetime
import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

FORMAT = 1
WORKFLOW = ".github/workflows/server.yml"
# Never change what a deploy does. Everything else does (server.yml, players.yml, the locks, branding...).
IRRELEVANT = ("*.md", ".gitignore", ".env.example", "expiry.yml", "LICENSE", "LICENSE.*")
SHA = re.compile(r"[0-9a-f]{40}")

Git = Callable[[Path, list[str]], str]


def _git(cwd: Path, argv: list[str]) -> str:
    return subprocess.run(["git", "-C", str(cwd), *argv], capture_output=True, text=True, check=True).stdout


def relevant(path: str) -> bool:
    if path.startswith(".github/"):
        return path == WORKFLOW
    name = path.rsplit("/", 1)[-1]
    return not any(fnmatch.fnmatchcase(path, p) or fnmatch.fnmatchcase(name, p) for p in IRRELEVANT)


def fingerprint(config: Path, product_sha: str, git: Git = _git) -> str:
    """sha256 over the product sha and the deploy-relevant tree entries of HEAD."""
    h = hashlib.sha256(f"format {FORMAT}\nproduct {product_sha}\n".encode())
    for entry in sorted(git(config, ["ls-tree", "-r", "-z", "HEAD"]).split("\0")):
        if not entry:
            continue
        meta, path = entry.split("\t", 1)
        if relevant(path):
            h.update(f"{meta}\t{path}\n".encode())
    return h.hexdigest()


def read_record(path: Path) -> dict[str, str] | None:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("format") != FORMAT:
        return None
    if not all(isinstance(data.get(k), str) for k in ("fingerprint", "config_sha", "product_sha")):
        return None
    return data


def _changed(config: Path, since: str, git: Git) -> list[str] | None:
    try:
        out = git(config, ["diff", "--name-only", since, "HEAD"])
    except (subprocess.CalledProcessError, OSError):
        return None
    return [p for p in out.splitlines() if p and relevant(p)]


def decide(
    config: Path,
    product_sha: str,
    event: str,
    record: dict[str, str] | None,
    before: str = "",
    git: Git = _git,
) -> tuple[bool, str]:
    """(deploy needed?, why)."""
    if event == "workflow_dispatch":
        return True, "a manual deploy always deploys"
    if event == "push":
        if not (SHA.fullmatch(before) and before.strip("0")):
            return True, "a push without a usable `before` commit"
        try:
            git(config, ["diff", "--quiet", before, "HEAD", "--", WORKFLOW])
        except subprocess.CalledProcessError:
            return True, f"this push changed {WORKFLOW} (the product pin)"
        except OSError:
            return True, "git failed"
    if record is None:
        return True, "no record of a successful deploy (first deploy, or the cache entry expired)"
    if record["product_sha"] != product_sha:
        return True, f"the product pin changed ({record['product_sha'][:12]} -> {product_sha[:12]})"
    if record["fingerprint"] != fingerprint(config, product_sha, git):
        files = _changed(config, record["config_sha"], git) if SHA.fullmatch(record["config_sha"]) else None
        what = ", ".join(files[:20]) if files else "deploy-relevant files"
        return True, f"changed since the last deploy ({record['config_sha'][:12]}): {what}"
    return False, (
        f"nothing deploy-relevant changed since the last successful deploy of {record['config_sha'][:12]} "
        f"({record.get('deployed_at', '?')}); dispatch the deploy task to deploy anyway"
    )


def write_record(config: Path, product_sha: str, out: Path, git: Git = _git) -> dict[str, object]:
    record: dict[str, object] = {
        "format": FORMAT,
        "fingerprint": fingerprint(config, product_sha, git),
        "config_sha": git(config, ["rev-parse", "HEAD"]).strip(),
        "product_sha": product_sha,
        "deployed_at": datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds"),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1) + "\n")
    return record


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("decide", "write", "fingerprint"):
        p = sub.add_parser(name)
        p.add_argument("--config", type=Path, required=True)
        p.add_argument("--product-sha", required=True)
        if name == "decide":
            p.add_argument("--event", default="")
            p.add_argument("--before", default="")
            p.add_argument("--record", type=Path, required=True)
        if name == "write":
            p.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    if not SHA.fullmatch(args.product_sha):
        ap.error(f"--product-sha must be 40 hex characters, not {args.product_sha!r}")
    if args.cmd == "fingerprint":
        print(fingerprint(args.config, args.product_sha))
        return 0
    if args.cmd == "write":
        print(json.dumps(write_record(args.config, args.product_sha, args.out)))
        return 0
    try:
        needed, why = decide(args.config, args.product_sha, args.event, read_record(args.record), args.before)
        fp = fingerprint(args.config, args.product_sha)
    except (subprocess.CalledProcessError, OSError) as e:
        needed, why, fp = True, f"git failed ({e})", ""
    print(f"{'::notice::deploy skipped: ' if not needed else 'deploy: '}{why}")
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write(f"deploy_needed={'true' if needed else 'false'}\nfingerprint={fp}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
