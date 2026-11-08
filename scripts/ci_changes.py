#!/usr/bin/env python3
"""Which product CI work a change needs (ci.yml, e2e-mock.yml; docs/ci.md explains what runs when).

    ci_changes.py --event EVENT [--before SHA] [--since-days N]  # in a job: `<group>=true|false` to
                                                                 # $GITHUB_OUTPUT
    ci_changes.py --files a.py docs/x.md ...                     # offline: the groups for these paths

A pull request is diffed as GitHub tests it: its merge commit against that commit's first parent (the base
branch tip; the checkout needs fetch-depth 2). A push to a branch is diffed against `--before` (fetched here).
A schedule with `--since-days N` takes every file the commits of the last N days touched (the checkout needs
the history: fetch-depth 0), so a scheduled run of an idle repository does nothing heavy. Anything else (a
schedule without it, workflow_dispatch, a tag push through release.yml, a new branch, or any git error) runs
everything: when in doubt, the answer is "everything changed", never "nothing changed".

The groups only decide whether heavy work runs; the jobs that are required checks always run and report (a
skipped job counts as passed, a never-started workflow leaves the check pending forever).
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import subprocess
import sys
from collections.abc import Callable, Iterable

# Paths that never change what the code does. docs/images/brand-* are the branding previews that
# scripts/brand_art.py checks, so they are not docs. Everything else (tests, examples/) is code.
DOCS = ("docs/*", "*.md", "LICENSE")
NOT_DOCS = ("docs/images/brand-*",)

GROUPS: dict[str, tuple[str, ...]] = {
    # lint's heavy half (full pytest, worker tests, ansible-lint, terraform), the controller matrix
    "code": (),  # computed: any path that is not docs
    # actionlint: every workflow it lints (the product's and the config-repo template's materialized example)
    "workflows": (".github/*", "template/.github/*", "examples/config-repo/.github/*"),
    # ci.yml job plugin: build_plugin.py check + map_palette.py check (v1.4) + brand_art.py check +
    # build_client_mod.py check
    "plugin": (
        "plugin/*",
        "scripts/map_palette.py",
        "scripts/map_palette/*",
        "scripts/config_keys.py",
        "server-config/keys/*/map-*",
        "client/mod/*",
        "scripts/build_plugin.py",
        "scripts/build_client_mod.py",
        "scripts/brand_art.py",
        "mcctl/brandpack.py",
        "scripts/lockfile.py",
        "template/server.lock.yml",
        "ansible/group_vars/all/main.yml",
        "docs/images/brand-*",
        "requirements/validate.txt",
    ),
    # v1.2 ci.yml job seeds: re-verify the seed catalogue (/locate) on the pinned server build, so the
    # panel's "verified on <version>" badge stays honest (scripts/seed_check.py)
    "seeds": (
        "worlds/*",
        "watchdog/panel/seeds.json",
        "scripts/seed_check.py",
        "scripts/boot_test.py",
        "scripts/build_plugin.py",
        "scripts/lockfile.py",
        "plugin/mc-telemetry/build.yml",
        "template/server.lock.yml",
    ),
    # e2e-mock.yml job e2e, essential half: init, render, deploy twice, hardening, agent, firewall, dry runs
    "e2e": (
        "ansible/*",
        "mcctl/*",
        "scripts/*",
        "plugin/*",
        "server-config/*",
        "template/*",
        "watchdog/*",
        "requirements/*",
        "tests/e2e/*",
        "tests/fixtures/*",
        "pyproject.toml",
        ".github/workflows/e2e-mock.yml",
    ),
    # e2e-mock.yml job e2e, the heavy half too (telemetry, branding, migrate container -> container, P16
    # knobs): what those checks exercise beyond the essential path. Not e2e-mock.yml itself: Dependabot's
    # weekly action bumps touch it (the weekly scheduled run covers the heavy half).
    "e2e_full": (
        "plugin/*",
        "watchdog/*",
        "server-config/*",
        "template/server.lock.yml",
        "template/plugins.lock.yml",
        "tests/e2e/*",
        "ansible/site.yml",
        "ansible/library/mc_noatime.py",
        "ansible/roles/base/*",
        "ansible/roles/java/*",
        "ansible/roles/minecraft/*",
        "ansible/roles/plugins/*",
        "ansible/roles/backups/*",
        "mcctl/migrate.py",
        "mcctl/serverops.py",
        "mcctl/restorecmd.py",
        "scripts/backup.py",
        "scripts/restore.py",
        "scripts/telemetry.py",
        "scripts/mc_agent.py",
    ),
}


def _match(path: str, patterns: Iterable[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, p) for p in patterns)


def is_docs(path: str) -> bool:
    if _match(path, NOT_DOCS):
        return False
    if path.endswith(".md") and "/" in path and not path.startswith("docs/"):
        return False  # template/README.md (synced to the example), tests' fixtures...: checked by the tests
    return _match(path, DOCS)


def groups(files: Iterable[str] | None) -> dict[str, bool]:
    """`None` = unknown change set: every group is on."""
    if files is None:
        return dict.fromkeys(GROUPS, True)
    paths = [f for f in files if f]
    out = {name: any(_match(p, pats) for p in paths) for name, pats in GROUPS.items()}
    out["code"] = any(not is_docs(p) for p in paths)
    return out


Runner = Callable[[list[str]], str]


def _git(argv: list[str]) -> str:
    return subprocess.run(["git", *argv], capture_output=True, text=True, check=True).stdout


def changed_files(event: str, before: str, git: Runner = _git, since_days: int = 0) -> list[str] | None:
    """The changed paths, or None when they can't be known (then everything runs)."""
    try:
        if event == "schedule" and since_days > 0:
            out = git(["log", f"--since={since_days} days ago", "--name-only", "--format=", "HEAD"])
            return sorted({line for line in out.splitlines() if line})
        if event == "pull_request":
            return git(["diff", "--name-only", "HEAD^1", "HEAD"]).splitlines()
        if event == "push" and before and before.strip("0"):
            git(["fetch", "--no-tags", "--depth=1", "origin", before])
            return git(["diff", "--name-only", before, "HEAD"]).splitlines()
    except (subprocess.CalledProcessError, OSError) as e:
        print(f"::warning::cannot list the changed files ({e}): running everything")
    return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    ap.add_argument("--event", default="")
    ap.add_argument("--before", default="")
    ap.add_argument("--since-days", type=int, default=0)
    ap.add_argument("--files", nargs="*")
    args = ap.parse_args(argv)
    if args.files is not None:
        files: list[str] | None = args.files
    else:
        files = changed_files(args.event, args.before, since_days=args.since_days)
    result = groups(files)
    print("changed files:", "(unknown: everything)" if files is None else len(files))
    for f in (files or [])[:200]:
        print("  ", f)
    lines = [f"{k}={'true' if v else 'false'}" for k, v in result.items()]
    print("\n".join(lines))
    out = os.environ.get("GITHUB_OUTPUT")
    if out and args.files is None:
        with open(out, "a", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
