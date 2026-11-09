#!/usr/bin/env python3
"""Release helper for the product (design §1: semver tags vX.Y.Z, CHANGELOG entry per release).

    release.py ship 1.5.0 [--standalone] [--auto-merge] [--date YYYY-MM-DD]  # bump + commit + push + PR
    release.py check [v1.0.0]         # tag == pyproject == mcctl.__version__, CHANGELOG has the section
    release.py notes 1.0.0            # print that CHANGELOG section (release notes)
    release.py bump 1.3.0 [--date YYYY-MM-DD]            # set the version, date the Unreleased section
    release.py autotag [--remote origin]                 # CI (autotag.yml): does main's version need a tag?
    release.py tag [v1.3.0] [--remote origin] [--push]   # fallback: annotated tag on the tip of <remote>/main
    release.py ci-reuse --repo OWNER/REPO --sha SHA      # CI (release.yml): reuse the PR's full CI run?

To release (docs/reference.md "Releasing"): `ship X.Y.Z` on the last feature branch (it merges a freshly
fetched `<remote>/main` in if needed, never rebases), or `ship X.Y.Z --standalone` (a `release-X.Y.Z` branch
from the fetched `<remote>/main`, never from local main) when no feature PR is pending. It runs `bump`
(pyproject.toml, mcctl/__init__.py, README's version line, `## [Unreleased]` -> `## [X.Y.Z] - YYYY-MM-DD`
plus a new empty Unreleased section), commits "Release X.Y.Z", pushes, and opens the pull request (or names
the open one); `--auto-merge` turns on GitHub's auto-merge. Merging it releases: on the push to main,
autotag.yml runs `autotag` here (pyproject's version newer than every v* tag and `check` passing), creates the
annotated tag vX.Y.Z on that commit and calls release.yml (CI, `check`, the GitHub release with the notes).
`tag` is the manual fallback (a tag pushed by a person starts release.yml by itself): it refuses unless HEAD
is exactly the fetched tip of `<remote>/main`, the tree is clean, `check` passes and the tag is new, creates
the annotated tag `vX.Y.Z` and prints the push command (`--push` pushes after a typed confirmation).
`ci-reuse` lets release.yml (called by autotag.yml) skip re-running the full CI on a clean merge whose pull
request already passed it (docs/ci.md "Releases"); any doubt answers no and CI runs.
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import subprocess
import sys
import tomllib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
TAG = re.compile(r"v(\d+\.\d+\.\d+)")
VERSION = re.compile(r"\d+\.\d+\.\d+")
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
UNRELEASED = re.compile(r"^## \[Unreleased\][ \t]*\r?\n(.*?)(?=^## |\Z)", re.M | re.S)

# git argv (without "git") and the working directory -> the completed process (text, captured, never raises).
GitRunner = Callable[[Sequence[str], Path], "subprocess.CompletedProcess[str]"]


# gh argv (without "gh") and the working directory -> the completed process (same contract as GitRunner).
GhRunner = GitRunner


def run_git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def run_gh(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError as e:  # gh isn't installed
        return subprocess.CompletedProcess(["gh", *args], 127, "", str(e))


def pyproject_version(root: Path = ROOT) -> str:
    return str(tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"])


def package_version(root: Path = ROOT) -> str:
    m = re.search(r'^__version__ = "([^"]+)"$', (root / "mcctl" / "__init__.py").read_text(), re.M)
    if m is None:
        raise ValueError("mcctl/__init__.py has no __version__")
    return m.group(1)


def notes(version: str, changelog: str) -> str | None:
    """Body of the `## [version] - YYYY-MM-DD` section, or None if there is none. CRLF line endings (a
    Windows checkout, read untranslated by `_read`) count as newlines."""
    m = re.search(
        rf"^## \[{re.escape(version)}\] - \d{{4}}-\d{{2}}-\d{{2}}\n(.*?)(?=^## |\Z)",
        changelog.replace("\r\n", "\n"),
        re.M | re.S,
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
        f'  git commit -am "Release {version}"\n'
        f"  push, open a PR, let CI pass, merge it: autotag.yml tags v{version} and releases it\n"
        f"(`release.py ship {version}` does all of this in one step)"
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


# --- ship: the bump rides a pull request; autotag: CI tags the merge -------------------------------------


def remote_tags(remote: str, root: Path, git: GitRunner) -> tuple[dict[str, str], str | None]:
    """The remote's vX.Y.Z tags -> the commit each names (annotated tags peeled), or (empty, error)."""
    ls = git(["ls-remote", "--tags", remote, "refs/tags/v*"], root)
    if ls.returncode != 0:
        return {}, f"git ls-remote {remote} failed: {ls.stderr.strip()}"
    tags: dict[str, str] = {}
    for line in ls.stdout.splitlines():
        sha, _, ref = line.partition("\t")
        name = ref.removeprefix("refs/tags/")
        peeled = name.endswith("^{}")
        name = name.removesuffix("^{}")
        if TAG.fullmatch(name) and (peeled or name not in tags):
            tags[name] = sha.strip()
    return tags, None


def latest(tags: dict[str, str]) -> str | None:
    """The highest vX.Y.Z (semver order), or None."""
    return max(tags, key=lambda t: semver(t[1:]), default=None)


def fetch_main(remote: str, root: Path, git: GitRunner) -> tuple[str, str | None]:
    """Fetch `<remote>/main` (never trusting local main): (its commit, error)."""
    tracking = f"refs/remotes/{remote}/main"
    fetch = git(["fetch", "--quiet", "--no-tags", remote, f"+refs/heads/main:{tracking}"], root)
    if fetch.returncode != 0:
        return "", f"git fetch {remote} main failed: {fetch.stderr.strip()}"
    return git(["rev-parse", "--verify", f"{tracking}^{{commit}}"], root).stdout.strip(), None


def _branch(root: Path, git: GitRunner) -> str | None:
    """The checked-out branch, or None on a detached HEAD."""
    res = git(["symbolic-ref", "--quiet", "--short", "HEAD"], root)
    if res.returncode != 0:
        return None
    return res.stdout.strip() or None


def ship(
    version: str,
    root: Path = ROOT,
    *,
    standalone: bool = False,
    auto_merge: bool = False,
    date: str | None = None,
    remote: str = "origin",
    git: GitRunner = run_git,
    gh: GhRunner = run_gh,
    say: Callable[[str], None] = print,
) -> list[str]:
    """Bump to `version` on the current feature branch (merged up to the fetched `<remote>/main`) or, with
    `standalone`, on a new `release-X.Y.Z` branch cut from it; commit, push, open (or name) the pull request.
    Returns errors; a refusal before the commit leaves the repository as it was."""
    if not VERSION.fullmatch(version):
        return [f"version {version!r} isn't X.Y.Z"]
    tag_name = f"v{version}"
    if (why := dirty(root, git)) is not None:
        return [why]
    branch = _branch(root, git)
    if not standalone and branch is None:
        return [
            "HEAD is detached: switch to your feature branch, or use --standalone (a release-X.Y.Z branch)"
        ]
    if not standalone and branch == "main":
        return [
            "you're on main: run this on the last feature branch (the bump rides its pull request), or use "
            "--standalone (a release-X.Y.Z branch from the fetched origin/main)"
        ]
    main_sha, err = fetch_main(remote, root, git)
    if err:
        return [err]
    tags, err = remote_tags(remote, root, git)
    if err:
        return [err]
    errors: list[str] = []
    top = latest(tags)
    if tag_name in tags:
        errors.append(f"tag {tag_name} already exists on {remote}: pick the next version")
    elif top is not None and semver(version) <= semver(top[1:]):
        errors.append(f"version {version} isn't greater than the latest release tag {top}")
    elif git(["rev-parse", "--quiet", "--verify", f"refs/tags/{tag_name}"], root).returncode == 0:
        errors.append(f"tag {tag_name} already exists locally (`git tag -d {tag_name}` if it's a leftover)")
    if errors:
        return errors

    start = git(["rev-parse", "--verify", "HEAD"], root).stdout.strip()
    origin_ref = branch or start  # where a refused standalone run goes back to
    if standalone:
        branch = f"release-{version}"
        if git(["rev-parse", "--quiet", "--verify", f"refs/heads/{branch}"], root).returncode == 0:
            return [f"branch {branch} exists already: delete it (`git branch -D {branch}`) or use it"]
        made = git(["switch", "--quiet", "--no-track", "--create", branch, main_sha], root)
        if made.returncode != 0:
            return [f"git switch -c {branch} failed: {made.stderr.strip()}"]
        say(f"on {branch}, cut from {remote}/main ({main_sha[:12]})")

        def undo() -> None:
            git(["checkout", "--quiet", origin_ref], root)
            git(["branch", "--quiet", "-D", branch], root)

    else:
        merged_in = git(["merge-base", "--is-ancestor", main_sha, "HEAD"], root).returncode == 0
        if not merged_in:
            merge = git(["merge", "--quiet", "--no-edit", f"refs/remotes/{remote}/main"], root)
            if merge.returncode != 0:
                git(["merge", "--abort"], root)
                return [
                    f"merging {remote}/main into {branch} conflicts: merge it yourself "
                    f"(`git merge {remote}/main`), commit, and run this again"
                ]
            say(f"merged {remote}/main ({main_sha[:12]}) into {branch}")
        else:
            say(f"{branch} already contains {remote}/main ({main_sha[:12]})")

        def undo() -> None:
            git(["reset", "--quiet", "--hard", start], root)

    errors = bump(version, root, date, git)
    if errors:
        undo()
        return errors
    title = f"Release {version}"
    commit = git(["commit", "--quiet", "--all", "--message", title], root)
    if commit.returncode != 0:
        undo()
        return [f"git commit failed: {(commit.stderr or commit.stdout).strip()}"]
    say(f"committed {title!r} on {branch}")
    pushed = git(["push", "--quiet", "--set-upstream", remote, f"HEAD:refs/heads/{branch}"], root)
    if pushed.returncode != 0:
        return [
            f"git push {remote} {branch} failed: {pushed.stderr.strip()} "
            f"(the commit is made: push it with `git push -u {remote} {branch}`)"
        ]
    say(f"pushed {branch}")
    view = gh(["pr", "view", branch, "--json", "url,state"], root)
    url = ""
    if view.returncode == 0:
        try:
            data = json.loads(view.stdout)
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get("state") == "OPEN":
            url = str(data.get("url") or "")
    if url:
        say(f"the open pull request carries the release now: {url}")
    else:
        body = (
            f"Releases {tag_name}: merging this tags it and creates the GitHub release (autotag.yml -> "
            f"release.yml).\n\n{notes(version, _read(root / 'CHANGELOG.md')) or ''}\n"
        )
        pr = gh(["pr", "create", "--base", "main", "--head", branch, "--title", title, "--body", body], root)
        if pr.returncode != 0:
            why = pr.stderr.strip()
            return [f"pushed {branch}, but `gh pr create` failed: {why} (open the PR on GitHub)"]
        url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
        say(f"opened {url}")
    if auto_merge:
        am = gh(["pr", "merge", url or branch, "--auto", "--merge"], root)
        if am.returncode != 0:
            return [f"`gh pr merge --auto --merge` failed: {am.stderr.strip()} (merge {url} once CI passed)"]
        say("auto-merge is on: it merges (and releases) once the required checks pass")
    else:
        say(f"merge it once CI passes: that tags {tag_name} and releases it")
    return []


def autotag(
    root: Path = ROOT, remote: str = "origin", git: GitRunner = run_git
) -> tuple[str, str, list[str]]:
    """What autotag.yml does for HEAD (a commit on main): ("tag", tag, []) when pyproject's version is newer
    than every v* tag on `remote` and `check` passes; ("release", tag, []) when that tag already names HEAD
    (a re-run: the workflow makes sure the GitHub release exists); ("none", tag, []) otherwise. A new version
    that fails `check` is an error (main says "release" but can't be released)."""
    name = f"v{pyproject_version(root)}"
    tags, err = remote_tags(remote, root, git)
    if err:
        return "none", name, [err]
    head = git(["rev-parse", "--verify", "HEAD^{commit}"], root).stdout.strip()
    if name in tags:
        return ("release" if tags[name] == head else "none"), name, []
    top = latest(tags)
    if top is not None and semver(name[1:]) <= semver(top[1:]):
        return "none", name, []
    errors = [f"check {name}: {e}" for e in check(name, root)]
    return ("none" if errors else "tag"), name, errors


# ci.yml's jobs that a full run (release.yml's `full: true`) always runs. A pull request's run skips the ones
# gated on `changes` (controller, plugin, rust, seeds, actionlint) when its diff doesn't need them, and lint's
# code half with controller; a run in which every one of them ran and passed is a full run.
# tests/test_release.py keeps this equal to ci.yml's jobs. Matrix jobs are "<id> (<values>)".
CI_FULL_JOBS = (
    "changes",
    "lint",
    "controller",
    "gitleaks",
    "publication",
    "plugin",
    "rust",
    "seeds",
    "actionlint",
    "config-repo / validate",
)
CI_WORKFLOW = ".github/workflows/ci.yml"
SHA = re.compile(r"[0-9a-f]{40}")


def _gh_json(gh: GhRunner, path: str, root: Path) -> Any:  # noqa: ANN401 - GitHub's JSON
    proc = gh(["api", "-H", "Accept: application/vnd.github+json", path], root)
    if proc.returncode != 0:
        raise ValueError(f"gh api {path.split('?')[0]} failed: {proc.stderr.strip()[:200]}")
    return json.loads(proc.stdout)


def ci_full_run(jobs: Sequence[dict[str, Any]]) -> str | None:
    """None when the jobs of one ci.yml run (latest attempt) are a passed full run, else what is missing."""
    for want in CI_FULL_JOBS:
        mine = [j for j in jobs if j.get("name") == want or str(j.get("name", "")).startswith(f"{want} (")]
        if not mine:
            return f"job {want} isn't in the run"
        bad = [j for j in mine if j.get("status") != "completed" or j.get("conclusion") != "success"]
        if bad:
            return f"job {bad[0].get('name')} is {bad[0].get('conclusion') or bad[0].get('status')}"
    return None


def ci_reuse(repo: str, sha: str, root: Path = ROOT, gh: GhRunner = run_gh) -> tuple[bool, str]:
    """May release.yml skip its full CI run for the tagged commit `sha`? (True, why) only when `sha` is the
    merge commit of a merged pull request into main from this repository, whose second parent is the pull
    request's head and whose first parent (main before the merge) the head already contains (a clean merge:
    the merge commit's tree is the head's, checked as well), and whose head's latest ci.yml run passed with
    every job a full run has (ci_full_run). Anything else, an API error included, is (False, why): run CI."""
    try:
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", repo) or not SHA.fullmatch(sha):
            return False, f"not a repository and commit: {repo!r} {sha!r}"
        pulls = _gh_json(gh, f"repos/{repo}/commits/{sha}/pulls", root)
        merged = [
            pr
            for pr in pulls
            if isinstance(pr, dict) and pr.get("merged_at") and pr.get("merge_commit_sha") == sha
        ]
        if len(merged) != 1:
            return False, f"{sha[:12]} is the merge commit of {len(merged)} merged pull requests, not one"
        pr = merged[0]
        number, head = pr.get("number"), (pr.get("head") or {}).get("sha", "")
        if (pr.get("base") or {}).get("ref") != "main":
            return False, f"pull request #{number} wasn't merged into main"
        if ((pr.get("head") or {}).get("repo") or {}).get("full_name") != repo:
            return False, f"pull request #{number} comes from another repository"
        if not SHA.fullmatch(head):
            return False, f"pull request #{number} has no head commit"
        commit = _gh_json(gh, f"repos/{repo}/git/commits/{sha}", root)
        parents = [p.get("sha") for p in commit.get("parents", [])]
        if len(parents) != 2 or parents[1] != head:
            return False, f"{sha[:12]} isn't a merge of pull request #{number}'s head {head[:12]}"
        compare = _gh_json(gh, f"repos/{repo}/compare/{parents[0]}...{head}", root)
        if compare.get("behind_by") != 0:
            return False, f"pull request #{number}'s head didn't contain main ({parents[0][:12]}) when merged"
        head_commit = _gh_json(gh, f"repos/{repo}/git/commits/{head}", root)
        tree, head_tree = (commit.get("tree") or {}).get("sha"), (head_commit.get("tree") or {}).get("sha")
        if not tree or tree != head_tree:
            return False, f"the merge commit's tree differs from pull request #{number}'s head"
        runs = _gh_json(gh, f"repos/{repo}/actions/workflows/ci.yml/runs?head_sha={head}&per_page=100", root)
        mine = [
            r
            for r in runs.get("workflow_runs", [])
            if r.get("head_sha") == head and r.get("path", "").split("@")[0] == CI_WORKFLOW
        ]
        if not mine:
            return False, f"no ci.yml run for pull request #{number}'s head {head[:12]}"
        if any(r.get("status") != "completed" for r in mine):
            return False, f"a ci.yml run for {head[:12]} hasn't finished"
        run = max(mine, key=lambda r: (str(r.get("created_at", "")), int(r.get("id", 0))))
        if run.get("conclusion") != "success":
            return (
                False,
                f"the latest ci.yml run for {head[:12]} is {run.get('conclusion')}: {run.get('html_url')}",
            )
        data = _gh_json(gh, f"repos/{repo}/actions/runs/{run['id']}/jobs?filter=latest&per_page=100", root)
        jobs = data.get("jobs", [])
        if data.get("total_count") != len(jobs):
            return False, f"ci.yml run {run['id']} has more jobs than one page"
        if (missing := ci_full_run(jobs)) is not None:
            return False, f"ci.yml run {run.get('html_url')} wasn't a full run: {missing}"
        return True, f"pull request #{number}'s head passed a full CI run, same tree: {run.get('html_url')}"
    except (ValueError, TypeError, KeyError, AttributeError) as e:  # JSON errors are ValueErrors
        return False, f"couldn't decide ({type(e).__name__}: {e})"


def _fail(errors: Sequence[str]) -> int:
    for e in errors:
        print(f"release: {e}", file=sys.stderr)
    return 1


def main(
    argv: list[str] | None = None,
    root: Path = ROOT,
    git: GitRunner = run_git,
    ask: Callable[[str], str] = input,
    gh: GhRunner = run_gh,
) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("ship", help="bump, commit, push and open the release PR (merging it releases)")
    s.add_argument("version", help="X.Y.Z, greater than the latest v* tag")
    s.add_argument(
        "--standalone",
        action="store_true",
        help="no pending feature PR: a release-X.Y.Z branch from the fetched <remote>/main",
    )
    s.add_argument("--auto-merge", action="store_true", help="auto-merge (gh pr merge --auto --merge)")
    s.add_argument("--date", help="the release date, YYYY-MM-DD (default: today)")
    s.add_argument("--remote", default="origin", help="the remote to fetch main from and push to")
    sub.add_parser("check").add_argument("tag", nargs="?", help="vX.Y.Z (default: v<pyproject.toml version>)")
    a = sub.add_parser("autotag", help="CI (autotag.yml): action=tag|release|none and tag= for HEAD")
    a.add_argument("--remote", default="origin", help="the remote whose v* tags count (default: origin)")
    sub.add_parser("notes").add_argument("version")
    b = sub.add_parser("bump", help="set the version and date the CHANGELOG's Unreleased section")
    b.add_argument("version", help="X.Y.Z, greater than the current version")
    b.add_argument("--date", help="the release date, YYYY-MM-DD (default: today)")
    t = sub.add_parser(
        "tag", help="fallback (autotag.yml tags merges): the annotated tag vX.Y.Z on the tip of <remote>/main"
    )
    t.add_argument("tag", nargs="?", help="vX.Y.Z (default: v<pyproject.toml version>)")
    t.add_argument(
        "--remote", default="origin", help="the remote whose main HEAD must equal (default: origin)"
    )
    t.add_argument("--push", action="store_true", help="push the tag after a typed confirmation")
    r = sub.add_parser("ci-reuse", help="CI (release.yml): reuse=true|false, may the full CI run be skipped?")
    r.add_argument("--repo", required=True, help="OWNER/REPO (github.repository)")
    r.add_argument("--sha", required=True, help="the tagged commit")
    args = p.parse_args(argv)
    if args.cmd == "ship":
        errors = ship(
            args.version,
            root,
            standalone=args.standalone,
            auto_merge=args.auto_merge,
            date=args.date,
            remote=args.remote,
            git=git,
            gh=gh,
        )
        return _fail(errors) if errors else 0
    if args.cmd == "check":
        errors = check(args.tag or f"v{pyproject_version(root)}", root)
        return _fail(errors) if errors else 0
    if args.cmd == "autotag":
        action, name, errors = autotag(root, args.remote, git)
        lines = [f"action={action}", f"tag={name}"]
        print("\n".join(lines))
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return _fail(errors) if errors else 0
    if args.cmd == "ci-reuse":
        reuse, why = ci_reuse(args.repo, args.sha, root, gh)
        lines = [f"reuse={'true' if reuse else 'false'}"]
        print(f"{lines[0]}: {why}")
        if out := os.environ.get("GITHUB_OUTPUT"):
            with open(out, "a", encoding="utf-8") as fh:
                fh.write("\n".join(lines) + "\n")
        return 0  # a "no" is an answer, not a failure: release.yml runs CI
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
