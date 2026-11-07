#!/usr/bin/env python3
"""Release helper for the product (design §1: semver tags vX.Y.Z, CHANGELOG entry per release).

    release.py check v1.0.0           # tag == pyproject == mcctl.__version__, CHANGELOG has the section
    release.py notes 1.0.0            # print that CHANGELOG section (release notes)
    release.py bump 1.3.0 [--date YYYY-MM-DD]            # set the version, date the Unreleased section
    release.py tag [v1.3.0] [--remote origin] [--push]   # annotated tag on the tip of <remote>/main

To release (docs/reference.md "Releasing"): on a clean branch, `bump X.Y.Z` (pyproject.toml,
mcctl/__init__.py, README's version line, `## [Unreleased]` -> `## [X.Y.Z] - YYYY-MM-DD` plus a new empty
Unreleased section); commit, open the PR, merge; then check out the merged main and run `tag`: it refuses
unless HEAD is exactly the fetched tip of `<remote>/main`, the tree is clean, `check` passes and the tag is
new, creates the annotated tag `vX.Y.Z` and prints the push command (`--push` pushes after a typed
confirmation). release.yml then runs CI, `check`, and creates the GitHub release with the notes.
"""

from __future__ import annotations

import argparse
import datetime
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"v(\d+\.\d+\.\d+)")
VERSION = re.compile(r"\d+\.\d+\.\d+")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
UNRELEASED = re.compile(r"^## \[Unreleased\][ \t]*\r?\n(.*?)(?=^## |\Z)", re.M | re.S)

# git argv (without "git") and the working directory -> the completed process (text, captured, never raises).
GitRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


def run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


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


def semver(version: str) -> tuple[int, int, int]:
    major, minor, patch = (int(p) for p in version.split("."))
    return major, minor, patch


def _read(path: Path) -> str:
    """The file as-is (no newline translation), so edits keep its line endings."""
    with path.open(encoding="utf-8", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        f.write(text)


def _sub_once(pattern: str, repl: str, text: str, what: str, errors: list[str], flags: int = re.M) -> str:
    new, n = re.subn(pattern, repl, text, count=1, flags=flags)
    if n != 1:
        errors.append(f"{what} not found")
    return new


def dirty(root: Path, git: GitRunner) -> str | None:
    """Why the working tree can't be used (uncommitted changes, not a repository), or None if clean."""
    st = git(["status", "--porcelain"], root)
    if st.returncode != 0:
        return f"git status failed: {st.stderr.strip()}"
    if st.stdout.strip():
        return "the working tree has uncommitted changes (commit or stash them first)"
    return None


def bump(version: str, root: Path = ROOT, date: str | None = None, git: GitRunner = run_git) -> list[str]:
    """Set the version in pyproject.toml, mcctl/__init__.py and README.md and date CHANGELOG's Unreleased
    section. Text edits only; nothing is written unless every check and edit succeeds. Returns errors."""
    if not VERSION.fullmatch(version):
        return [f"version {version!r} isn't X.Y.Z"]
    day = date or datetime.date.today().isoformat()
    try:
        if not DATE.fullmatch(day):
            raise ValueError
        datetime.date.fromisoformat(day)
    except ValueError:
        return [f"date {day!r} isn't YYYY-MM-DD"]
    errors: list[str] = []
    current = pyproject_version(root)
    if semver(version) <= semver(current):
        errors.append(f"version {version} isn't greater than the current {current}")
    elif re.search(rf"^## \[{re.escape(version)}\]", _read(root / "CHANGELOG.md"), re.M):
        errors.append(f"CHANGELOG.md already has a `## [{version}]` section")
    if (why := dirty(root, git)) is not None:
        errors.append(why)
    paths = {
        "pyproject": root / "pyproject.toml",
        "package": root / "mcctl" / "__init__.py",
        "readme": root / "README.md",
        "changelog": root / "CHANGELOG.md",
    }
    texts = {k: _read(p) for k, p in paths.items()}
    log = texts["changelog"]
    m = UNRELEASED.search(log)
    if m is None:
        errors.append("CHANGELOG.md has no `## [Unreleased]` section")
    elif not m.group(1).strip():
        errors.append("CHANGELOG.md's `## [Unreleased]` section is empty: nothing to release")
    if errors:
        return errors
    old = re.escape(current)
    new = dict(texts)
    # The first `version = "..."` of the [project] table (no other table header in between).
    new["pyproject"] = _sub_once(
        rf'^(\[project\][ \t]*\r?\n(?:(?!\[).*\n)*?version = )"{old}"',
        rf'\g<1>"{version}"',
        texts["pyproject"],
        f'pyproject.toml: [project] version = "{current}"',
        errors,
    )
    new["package"] = _sub_once(
        rf'^__version__ = "{old}"',
        f'__version__ = "{version}"',
        texts["package"],
        f'mcctl/__init__.py: __version__ = "{current}"',
        errors,
    )
    new["readme"] = _sub_once(
        rf"^Version \*\*{old}\*\*",
        f"Version **{version}**",
        texts["readme"],
        f"README.md: a line starting `Version **{current}**`",
        errors,
    )
    nl = "\r\n" if "\r\n" in log else "\n"
    new["changelog"] = _sub_once(
        r"^## \[Unreleased\][ \t]*\r?\n",
        f"## [Unreleased]{nl}{nl}## [{version}] - {day}{nl}",
        log,
        "CHANGELOG.md: `## [Unreleased]`",
        errors,
    )
    if errors:
        return errors
    for k, path in paths.items():
        if new[k] != texts[k]:
            _write(path, new[k])
    return check(f"v{version}", root)  # a sanity check of what was written


def bump_next_steps(version: str) -> str:
    return (
        f"Bumped to {version}. Next:\n"
        f"  git diff                       # review\n"
        f'  git commit -am "release: v{version}"\n'
        f"  open a PR, let CI pass, merge it\n"
        f"  git switch main && git pull    # (or: git fetch origin && git switch --detach origin/main)\n"
        f"  python3 scripts/release.py tag v{version}"
    )


def tag(
    name: str | None = None,
    root: Path = ROOT,
    remote: str = "origin",
    git: GitRunner = run_git,
) -> tuple[str, list[str]]:
    """Create the annotated release tag on HEAD, which must be the fetched tip of `<remote>/main`.
    Returns (tag name, errors); the tag exists only when errors is empty."""
    name = name or f"v{pyproject_version(root)}"
    if TAG.fullmatch(name) is None:
        return name, [f"tag {name!r} isn't vX.Y.Z"]
    if (why := dirty(root, git)) is not None:
        return name, [why]
    tracking = f"refs/remotes/{remote}/main"
    fetch = git(["fetch", "--quiet", "--no-tags", remote, f"+refs/heads/main:{tracking}"], root)
    if fetch.returncode != 0:
        return name, [f"git fetch {remote} main failed: {fetch.stderr.strip()}"]
    head = git(["rev-parse", "--verify", "HEAD^{commit}"], root).stdout.strip()
    main = git(["rev-parse", "--verify", f"{tracking}^{{commit}}"], root).stdout.strip()
    errors: list[str] = []
    if not head or head != main:
        # The v1.2.1 mistake: a tag on a branch commit instead of main's merge commit.
        errors.append(
            f"HEAD ({head[:12] or '?'}) isn't the tip of {remote}/main ({main[:12] or '?'}): "
            f"merge the release PR, then `git fetch {remote} && git switch --detach {remote}/main` "
            "and run this again"
        )
    errors += [f"check {name}: {e}" for e in check(name, root)]
    if git(["rev-parse", "--quiet", "--verify", f"refs/tags/{name}"], root).returncode == 0:
        errors.append(f"tag {name} already exists locally")
    ls = git(["ls-remote", "--tags", remote, f"refs/tags/{name}"], root)
    if ls.returncode != 0:
        errors.append(f"git ls-remote {remote} failed: {ls.stderr.strip()}")
    elif ls.stdout.strip():
        errors.append(f"tag {name} already exists on {remote}")
    if errors:
        return name, errors
    made = git(["tag", "--annotate", name, "--message", f"Smallworld {name}"], root)
    if made.returncode != 0:
        return name, [f"git tag failed: {made.stderr.strip()}"]
    return name, []


def push(
    name: str,
    root: Path = ROOT,
    remote: str = "origin",
    git: GitRunner = run_git,
    ask: Callable[[str], str] = input,
) -> str | None:
    """Push the tag after the user types its name. Returns an error, or None once pushed."""
    try:
        typed = ask(f"Type {name} to push it to {remote} (this starts the release): ")
    except EOFError:
        typed = ""
    if typed.strip() != name:
        return f"not confirmed; the tag stays local (push it with `git push {remote} {name}`)"
    res = git(["push", remote, f"refs/tags/{name}"], root)
    if res.returncode != 0:
        return f"git push {remote} {name} failed: {res.stderr.strip()}"
    return None


def _fail(errors: Sequence[str]) -> int:
    for e in errors:
        print(f"release: {e}", file=sys.stderr)
    return 1


def main(
    argv: list[str] | None = None,
    root: Path = ROOT,
    git: GitRunner = run_git,
    ask: Callable[[str], str] = input,
) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check").add_argument("tag")
    sub.add_parser("notes").add_argument("version")
    b = sub.add_parser("bump", help="set the version and date the CHANGELOG's Unreleased section")
    b.add_argument("version", help="X.Y.Z, greater than the current version")
    b.add_argument("--date", help="the release date, YYYY-MM-DD (default: today)")
    t = sub.add_parser("tag", help="create the annotated tag vX.Y.Z on the tip of <remote>/main")
    t.add_argument("tag", nargs="?", help="vX.Y.Z (default: v<pyproject.toml version>)")
    t.add_argument(
        "--remote", default="origin", help="the remote whose main HEAD must equal (default: origin)"
    )
    t.add_argument("--push", action="store_true", help="push the tag after a typed confirmation")
    args = p.parse_args(argv)
    if args.cmd == "check":
        errors = check(args.tag, root)
        return _fail(errors) if errors else 0
    if args.cmd == "bump":
        errors = bump(args.version, root, args.date, git)
        if errors:
            return _fail(errors)
        print(bump_next_steps(args.version))
        return 0
    if args.cmd == "tag":
        name, errors = tag(args.tag, root, args.remote, git)
        if errors:
            return _fail(errors)
        print(f"Created the annotated tag {name} on HEAD (= {args.remote}/main).")
        if not args.push:
            print(f"Push it to release:\n  git push {args.remote} {name}")
            return 0
        if (err := push(name, root, args.remote, git, ask)) is not None:
            return _fail([err])
        print(f"Pushed {name}; release.yml takes it from here.")
        return 0
    text = notes(args.version, (root / "CHANGELOG.md").read_text())
    if text is None:
        return _fail([f"no CHANGELOG section for {args.version}"])
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
