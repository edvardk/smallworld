#!/usr/bin/env python3
"""Release checks for the product (design §1: semver tags vX.Y.Z, CHANGELOG entry per release).

    release.py check v1.0.0    # tag == pyproject version == mcctl.__version__, CHANGELOG has the section
    release.py notes 1.0.0     # print that CHANGELOG section (release notes)

To release: bump the version in pyproject.toml and mcctl/__init__.py, rename `## [Unreleased]` to
`## [X.Y.Z] - YYYY-MM-DD` (and start a new Unreleased section), merge, then push the tag `vX.Y.Z` on that
commit. release.yml runs CI, this check, and creates the GitHub release with the notes.
"""

from __future__ import annotations

import argparse
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"v(\d+\.\d+\.\d+)")


def pyproject_version(root: Path = ROOT) -> str:
    return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])


def package_version(root: Path = ROOT) -> str:
    m = re.search(r'^__version__ = "([^"]+)"$', (root / "mcctl" / "__init__.py").read_text(), re.M)
    if m is None:
        raise ValueError("mcctl/__init__.py has no __version__")
    return m.group(1)


def notes(version: str, changelog: str) -> str | None:
    """Body of the `## [version] - YYYY-MM-DD` section, or None if there is none."""
    m = re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)", changelog, re.M | re.S
    )
    return m.group(1).strip() if m else None


def check(tag: str, root: Path = ROOT) -> list[str]:
    m = TAG.fullmatch(tag)
    if m is None:
        return [f"tag {tag!r} isn't vX.Y.Z"]
    version = m.group(1)
    errors = []
    if (pv := pyproject_version(root)) != version:
        errors.append(f"pyproject.toml version is {pv}, tag says {version}")
    if (mv := package_version(root)) != version:
        errors.append(f"mcctl.__version__ is {mv}, tag says {version}")
    if not notes(version, (root / "CHANGELOG.md").read_text()):
        errors.append(f"CHANGELOG.md has no non-empty `## [{version}] - YYYY-MM-DD` section")
    return errors


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").add_argument("tag")
    sub.add_parser("notes").add_argument("version")
    args = p.parse_args(argv)
    if args.cmd == "check":
        errors = check(args.tag)
        for e in errors:
            print(f"release: {e}", file=sys.stderr)
        return 1 if errors else 0
    text = notes(args.version, (ROOT / "CHANGELOG.md").read_text())
    if text is None:
        print(f"release: no CHANGELOG section for {args.version}", file=sys.stderr)
        return 1
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
