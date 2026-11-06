"""The anonymity gate's generic half (docs/publishing.md): the files scripts/publish_fresh.py would publish
from this working tree have no session links, no emails outside reserved domains, no public IP literals
outside documentation/test ranges, the owner's name only in its allowed forms, no personal timezone default in
mcctl/schema.py or mcctl/init.py, and no references to excluded paths (`publish_fresh.generic_problems`).

The private denylist (names, domains, ids) is never in this repository; publish_fresh applies it on top.

Cleanup still to do before publishing is listed, as paths only, in tests/fixtures/publishable-known.txt:
those files may still have problems, every other file must have none, and a listed file that is clean (or
gone) fails until it is removed from the list, so the list only shrinks. It must be empty before the release
is published (publish_fresh refuses anyway).
"""

from __future__ import annotations

import subprocess
import warnings
from functools import cache

import publish_fresh
import release
from conftest import REPO

KNOWN = REPO / "tests" / "fixtures" / "publishable-known.txt"


@cache
def tree() -> tuple[dict[str, publish_fresh.Entry], dict[str, publish_fresh.Entry], tuple[str, ...]]:
    """(everything tracked, what would be published, the exclude entries), from the working tree."""
    out = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z"], capture_output=True, text=True, check=True
    )
    everything = {
        p: publish_fresh.Entry(0o100644, (REPO / p).read_bytes())
        for p in out.stdout.split("\0")
        if p and (REPO / p).is_file()
    }
    files, excludes = publish_fresh.select(everything, release.pyproject_version())
    return everything, files, tuple(excludes)


@cache
def problems() -> dict[str, list[str]]:
    everything, files, excludes = tree()
    out: dict[str, list[str]] = {}
    for hit in publish_fresh.generic_problems(files, excludes, everything):
        out.setdefault(hit.path.split("!/")[0], []).append(str(hit))
    return out


def known() -> list[str]:
    return publish_fresh.parse_excludes(KNOWN.read_text())


def test_known_list_is_paths_only() -> None:
    entries = known()
    assert entries == sorted(set(entries)), "keep tests/fixtures/publishable-known.txt sorted, no duplicates"
    tracked = tree()[0]
    assert [e for e in entries if e not in tracked] == [], (
        "entries must be exact tracked paths (no dirs/globs)"
    )


def test_no_problems_outside_the_known_list() -> None:
    allowed = set(known())
    new = [h for path, hits in sorted(problems().items()) if path not in allowed for h in hits]
    assert new == [], "not publishable (fix it, never extend the known list):\n" + "\n".join(new)
    if allowed:
        n = sum(len(problems().get(p, [])) for p in allowed)
        warnings.warn(
            f"publishable: {len(allowed)} known file(s), {n} problem(s) left to clean up", stacklevel=1
        )


def test_known_list_only_shrinks() -> None:
    files = tree()[1]
    stale = [p for p in known() if p not in files or p not in problems()]
    assert stale == [], f"clean now (or not published): remove from {KNOWN.relative_to(REPO)}: {stale}"


def test_the_export_leaves_out_the_history() -> None:
    everything, files, excludes = tree()
    assert excludes and len(files) < len(everything)
    assert publish_fresh.EXCLUDE_FILE in files and "CHANGELOG.md" in files
    changelog = files["CHANGELOG.md"].data.decode()
    assert changelog.count("\n## [") == 2  # [Unreleased] and the release
    assert changelog.rstrip().endswith(publish_fresh.changelog_line(release.pyproject_version()))
