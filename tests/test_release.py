"""Release mechanics: tag == pyproject version == mcctl.__version__, CHANGELOG section (scripts/release.py)."""

import json
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import release
import yaml
from conftest import REPO

import mcctl
from mcctl import cli


def test_versions_agree() -> None:
    assert release.pyproject_version() == release.package_version() == mcctl.__version__


def test_cli_version(capsys) -> None:
    try:
        cli.main(["--version"])
    except SystemExit as e:
        assert e.code == 0
    assert capsys.readouterr().out.strip() == f"mcctl {mcctl.__version__}"


def test_changelog_has_unreleased_section() -> None:
    assert "## [Unreleased]" in (REPO / "CHANGELOG.md").read_text()


def fake_root(tmp_path, version: str, changelog: str):
    (tmp_path / "mcctl").mkdir(parents=True)
    (tmp_path / "mcctl" / "__init__.py").write_text(f'__version__ = "{version}"\n')
    shutil.copy(REPO / "pyproject.toml", tmp_path / "pyproject.toml")
    text = (
        (tmp_path / "pyproject.toml")
        .read_text()
        .replace(f'version = "{mcctl.__version__}"', f'version = "{version}"')
    )
    (tmp_path / "pyproject.toml").write_text(text)
    (tmp_path / "CHANGELOG.md").write_text(changelog)
    return tmp_path


def test_check(tmp_path) -> None:
    log = "# Changelog\n\n## [Unreleased]\n\n## [1.2.3] - 2026-11-02\n\n- Fixed a thing.\n\n## [1.2.2] - 2026-10-01\n- old\n"
    root = fake_root(tmp_path, "1.2.3", log)
    assert release.check("v1.2.3", root) == []
    assert release.notes("1.2.3", log) == "- Fixed a thing."
    assert release.notes("1.2.3", log.replace("\n", "\r\n")) == "- Fixed a thing."  # a Windows checkout
    assert release.check("1.2.3", root) == ["tag '1.2.3' isn't vX.Y.Z"]
    errors = release.check("v1.2.4", root)
    assert len(errors) == 3  # pyproject, __version__, changelog
    empty = fake_root(tmp_path / "e", "2.0.0", "## [2.0.0] - 2026-11-02\n\n## [1.0.0] - 2026-01-01\n- x\n")
    assert release.check("v2.0.0", empty) == [
        "CHANGELOG.md has no non-empty `## [2.0.0] - YYYY-MM-DD` section"
    ]


# --- bump / tag: temp repositories with a bare "origin" (explicit identity: CI runners have none) ---

GIT_CFG = (
    "-c",
    "user.name=Release Test",
    "-c",
    "user.email=release-test@example.invalid",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
    "-c",
    "core.autocrlf=false",
)

UNRELEASED = "- Added a thing.\n"


def git(args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *GIT_CFG, *args], cwd=cwd, capture_output=True, text=True, check=False)


def must(cwd: Path, *args: str) -> str:
    res = git(args, cwd)
    assert res.returncode == 0, res.stderr
    return res.stdout.strip()


def changelog(unreleased: str = UNRELEASED) -> str:
    return (
        "# Changelog\n\nIntro mentioning `## [X.Y.Z] - YYYY-MM-DD`.\n\n## [Unreleased]\n\n"
        f"{unreleased}\n## [1.2.2] - 2026-11-07\n\n- Old fix.\n"
    )


def make_repo(tmp_path: Path, unreleased: str = UNRELEASED) -> tuple[Path, Path]:
    """A work tree at 1.2.2 whose main is pushed to a bare origin."""
    origin = tmp_path / "origin.git"
    work = tmp_path / "work"
    must(tmp_path, "init", "-q", "--bare", "--initial-branch=main", origin.as_posix())
    must(tmp_path, "init", "-q", "--initial-branch=main", work.as_posix())
    fake_root(work, "1.2.2", changelog(unreleased))
    (work / "README.md").write_text("# Smallworld\n\nVersion **1.2.2** ([CHANGELOG](CHANGELOG.md)). More.\n")
    must(work, "add", "-A")
    must(work, "commit", "-qm", "initial")
    must(work, "remote", "add", "origin", origin.as_posix())
    must(work, "push", "-q", "origin", "main")
    return work, origin


def release_commit(work: Path, version: str = "1.3.0") -> None:
    assert release.bump(version, work, "2026-11-08", git) == []
    must(work, "commit", "-qam", f"release: v{version}")


def test_bump(tmp_path, capsys) -> None:
    work, _ = make_repo(tmp_path)
    assert release.main(["bump", "1.3.0", "--date", "2026-11-08"], work, git) == 0
    assert "autotag.yml tags v1.3.0" in capsys.readouterr().out
    assert release.check("v1.3.0", work) == []
    assert release.pyproject_version(work) == release.package_version(work) == "1.3.0"
    assert 'name = "smallworld-mc"\nversion = "1.3.0"\n' in (work / "pyproject.toml").read_text()
    assert (work / "README.md").read_text().startswith("# Smallworld\n\nVersion **1.3.0** ([CHANGELOG]")
    assert (work / "CHANGELOG.md").read_text() == (
        "# Changelog\n\nIntro mentioning `## [X.Y.Z] - YYYY-MM-DD`.\n\n## [Unreleased]\n\n"
        "## [1.3.0] - 2026-11-08\n\n- Added a thing.\n\n## [1.2.2] - 2026-11-07\n\n- Old fix.\n"
    )
    # Only the version line changed in pyproject.toml (formatting and comments kept).
    diff = must(work, "diff", "--numstat").splitlines()
    assert sorted(line.split("\t")[2] for line in diff) == [
        "CHANGELOG.md",
        "README.md",
        "mcctl/__init__.py",
        "pyproject.toml",
    ]
    assert all(line.split("\t")[1] == "1" for line in diff if not line.endswith("CHANGELOG.md"))


def test_bump_default_date(tmp_path) -> None:
    import datetime

    work, _ = make_repo(tmp_path)
    assert release.bump("2.0.0", work, git=git) == []
    assert f"## [2.0.0] - {datetime.date.today().isoformat()}\n" in (work / "CHANGELOG.md").read_text()


def test_bump_keeps_crlf(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    for name in ("CHANGELOG.md", "README.md"):
        path = work / name
        # normalise first: write_text already wrote CRLF on Windows (a second replace would give \r\r\n)
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    must(work, "commit", "-qam", "crlf", "--allow-empty")  # on Windows the files were CRLF already
    assert release.bump("1.2.3", work, "2026-11-08", git) == []
    raw = (work / "CHANGELOG.md").read_bytes()
    assert b"## [Unreleased]\r\n\r\n## [1.2.3] - 2026-11-08\r\n\r\n- Added a thing." in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


@pytest.mark.parametrize(
    ("version", "date", "error"),
    [
        ("1.2.2", None, "version 1.2.2 isn't greater than the current 1.2.2"),
        ("1.2.1", None, "version 1.2.1 isn't greater than the current 1.2.2"),
        ("1.10.0", "2026-13-01", "date '2026-13-01' isn't YYYY-MM-DD"),
        ("1.3", None, "version '1.3' isn't X.Y.Z"),
        ("v1.3.0", None, "version 'v1.3.0' isn't X.Y.Z"),
    ],
)
def test_bump_refuses_version_and_date(tmp_path, version, date, error) -> None:
    work, _ = make_repo(tmp_path)
    assert release.bump(version, work, date, git) == [error]
    assert must(work, "status", "--porcelain") == ""


def test_bump_semver_not_string_order(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    assert release.bump("1.10.0", work, "2026-11-08", git) == []  # "1.10.0" < "1.2.2" as strings


def test_bump_refuses_empty_unreleased(tmp_path, capsys) -> None:
    work, _ = make_repo(tmp_path, unreleased="")
    assert release.main(["bump", "1.3.0"], work, git) == 1
    assert "`## [Unreleased]` section is empty" in capsys.readouterr().err
    assert must(work, "status", "--porcelain") == ""


def test_bump_refuses_dirty_tree(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    (work / "notes.txt").write_text("untracked\n")
    assert release.bump("1.3.0", work, None, git) == [
        "the working tree has uncommitted changes (commit or stash them first)"
    ]
    assert release.pyproject_version(work) == "1.2.2"


def test_bump_refuses_existing_section(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    log = (
        (work / "CHANGELOG.md")
        .read_text()
        .replace("## [1.2.2]", "## [1.3.0] - 2026-11-01\n\n- Stray.\n\n## [1.2.2]")
    )
    (work / "CHANGELOG.md").write_text(log)
    must(work, "commit", "-qam", "stray")
    assert release.bump("1.3.0", work, None, git) == ["CHANGELOG.md already has a `## [1.3.0]` section"]


def test_bump_refuses_missing_targets(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    (work / "README.md").write_text("# Smallworld\n\nNo version line.\n")
    must(work, "commit", "-qam", "readme")
    assert release.bump("1.3.0", work, None, git) == [
        "README.md: a line starting `Version **1.2.2**` not found"
    ]
    assert release.pyproject_version(work) == "1.2.2"  # nothing written
    assert must(work, "status", "--porcelain") == ""


def test_tag_happy_path(tmp_path, capsys) -> None:
    work, origin = make_repo(tmp_path)
    release_commit(work)
    must(work, "push", "-q", "origin", "main")
    assert release.main(["tag"], work, git) == 0
    out = capsys.readouterr().out
    assert "git push origin v1.3.0" in out
    assert must(work, "cat-file", "-t", "v1.3.0") == "tag"  # annotated
    assert must(work, "tag", "-l", "--format=%(contents:subject)", "v1.3.0") == "Smallworld v1.3.0"
    assert must(work, "rev-parse", "v1.3.0^{commit}") == must(work, "rev-parse", "HEAD")
    assert must(origin, "tag", "-l") == ""  # printed, not pushed


def test_tag_push_after_confirmation(tmp_path) -> None:
    work, origin = make_repo(tmp_path)
    release_commit(work)
    must(work, "push", "-q", "origin", "main")
    assert release.main(["tag", "v1.3.0", "--push"], work, git, ask=lambda _: "v1.3") == 1
    assert must(origin, "tag", "-l") == ""
    must(work, "tag", "-d", "v1.3.0")
    assert release.main(["tag", "v1.3.0", "--push"], work, git, ask=lambda _: "v1.3.0") == 0
    assert must(origin, "tag", "-l") == "v1.3.0"
    assert must(origin, "cat-file", "-t", "v1.3.0") == "tag"


def test_tag_refuses_branch_commit(tmp_path) -> None:
    """The v1.2.1 mistake: the release branch's commit instead of main's merge commit."""
    work, _ = make_repo(tmp_path)
    must(work, "switch", "-qc", "release")
    release_commit(work)
    must(work, "push", "-q", "origin", "release")
    must(work, "switch", "-q", "main")
    must(work, "merge", "-q", "--no-ff", "-m", "Merge release", "release")
    must(work, "push", "-q", "origin", "main")
    must(work, "switch", "-q", "release")
    name, errors = release.tag(None, work, "origin", git)
    assert name == "v1.3.0"
    assert len(errors) == 1
    assert errors[0].startswith("HEAD (") and "isn't the tip of origin/main" in errors[0]
    assert must(work, "tag", "-l") == ""
    must(work, "switch", "-q", "main")  # the merge commit: accepted
    assert release.tag(None, work, "origin", git) == ("v1.3.0", [])


def test_tag_refuses_unpushed_main(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    release_commit(work)  # on local main, not on origin/main
    _, errors = release.tag("v1.3.0", work, "origin", git)
    assert any("isn't the tip of origin/main" in e for e in errors)


def test_tag_refuses_dirty_tree(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    release_commit(work)
    must(work, "push", "-q", "origin", "main")
    (work / "README.md").write_text("edited\n")
    assert release.tag(None, work, "origin", git) == (
        "v1.3.0",
        ["the working tree has uncommitted changes (commit or stash them first)"],
    )


def test_tag_refuses_existing_tag(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    release_commit(work)
    must(work, "push", "-q", "origin", "main")
    must(work, "tag", "-a", "v1.3.0", "-m", "x")
    assert release.tag(None, work, "origin", git)[1] == ["tag v1.3.0 already exists locally"]
    must(work, "push", "-q", "origin", "v1.3.0")
    must(work, "tag", "-d", "v1.3.0")
    assert release.tag(None, work, "origin", git)[1] == ["tag v1.3.0 already exists on origin"]


def test_tag_refuses_failing_check(tmp_path, capsys) -> None:
    work, _ = make_repo(tmp_path)  # at 1.2.2 with an undated Unreleased section
    assert release.main(["tag", "v1.3.0"], work, git) == 1
    err = capsys.readouterr().err
    assert "check v1.3.0: pyproject.toml version is 1.2.2, tag says 1.3.0" in err
    assert "check v1.3.0: CHANGELOG.md has no non-empty `## [1.3.0] - YYYY-MM-DD` section" in err
    assert must(work, "tag", "-l") == ""


def test_tag_custom_remote_and_fetch_failure(tmp_path) -> None:
    work, origin = make_repo(tmp_path)
    release_commit(work)
    must(work, "remote", "add", "upstream", origin.as_posix())
    must(work, "push", "-q", "upstream", "main")
    _, errors = release.tag(None, work, "nowhere", git)
    assert len(errors) == 1 and errors[0].startswith("git fetch nowhere main failed")
    assert release.main(["tag", "--remote", "upstream"], work, git) == 0


# --- ship (v1.5): the bump rides a pull request; gh is a fake (nothing real is contacted) ------------------


class FakeGh:
    """`gh pr view|create|merge`: records each call; `open_pr` makes `pr view` find an open pull request."""

    def __init__(self, open_pr: str = "", create_rc: int = 0) -> None:
        self.open_pr, self.create_rc = open_pr, create_rc
        self.calls: list[list[str]] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        args = list(args)
        self.calls.append(args)
        if args[:2] == ["pr", "view"]:
            if self.open_pr:
                return subprocess.CompletedProcess(
                    args, 0, f'{{"url": "{self.open_pr}", "state": "OPEN"}}', ""
                )
            return subprocess.CompletedProcess(args, 1, "", "no pull requests found for branch")
        if args[:2] == ["pr", "create"]:
            out = "https://github.com/o/r/pull/41\n" if not self.create_rc else ""
            return subprocess.CompletedProcess(args, self.create_rc, out, "boom" if self.create_rc else "")
        if args[:2] == ["pr", "merge"]:
            return subprocess.CompletedProcess(args, 0, "", "")
        return subprocess.CompletedProcess(args, 1, "", f"fake gh: unexpected {args}")

    def created(self) -> list[list[str]]:
        return [c for c in self.calls if c[:2] == ["pr", "create"]]


def tag_origin(work: Path, name: str = "v1.2.2") -> None:
    must(work, "tag", "-a", name, "-m", f"Smallworld {name}")
    must(work, "push", "-q", "origin", name)


def feature_repo(tmp_path: Path, unreleased: str = UNRELEASED) -> tuple[Path, Path, str]:
    """origin/main moved on (someone else's merge) after `feature` branched off; v1.2.2 tagged."""
    work, origin = make_repo(tmp_path, unreleased)
    tag_origin(work)
    must(work, "switch", "-qc", "feature")
    (work / "feature.txt").write_text("feature\n")
    must(work, "add", "feature.txt")
    must(work, "commit", "-qm", "a feature")
    feature = must(work, "rev-parse", "HEAD")
    must(work, "switch", "-q", "main")
    (work / "other.txt").write_text("merged meanwhile\n")
    must(work, "add", "other.txt")
    must(work, "commit", "-qm", "another merge")
    must(work, "push", "-q", "origin", "main")
    must(work, "switch", "-q", "feature")
    return work, origin, feature


def test_ship_merges_main_in_bumps_commits_pushes_and_opens_the_pr(tmp_path, capsys) -> None:
    work, origin, feature = feature_repo(tmp_path)
    gh = FakeGh()
    assert release.main(["ship", "1.3.0", "--date", "2026-11-08"], work, git, gh=gh) == 0
    out = capsys.readouterr().out
    assert "merged origin/main" in out and "opened https://github.com/o/r/pull/41" in out
    # merged, never rebased: the feature commit is kept as it was, origin/main is an ancestor
    head = must(work, "rev-parse", "HEAD")
    assert must(work, "log", "-1", "--format=%s") == "Release 1.3.0"
    assert git(["merge-base", "--is-ancestor", feature, head], work).returncode == 0
    assert git(["merge-base", "--is-ancestor", must(origin, "rev-parse", "main"), head], work).returncode == 0
    assert must(work, "log", "-1", "--format=%P", "HEAD~1").count(" ") == 1  # HEAD~1 is the merge commit
    assert release.check("v1.3.0", work) == []
    assert must(origin, "rev-parse", "feature") == head  # pushed
    (create,) = gh.created()
    assert create[create.index("--head") + 1] == "feature" and create[create.index("--base") + 1] == "main"
    assert create[create.index("--title") + 1] == "Release 1.3.0"
    assert "- Added a thing." in create[create.index("--body") + 1]
    assert not [c for c in gh.calls if c[:2] == ["pr", "merge"]]


def test_ship_names_the_open_pr_and_turns_on_auto_merge(tmp_path, capsys) -> None:
    work, _, _ = feature_repo(tmp_path)
    must(work, "merge", "-q", "--no-edit", "origin/main")  # already up to date with main: no merge commit
    before = must(work, "rev-parse", "HEAD")
    gh = FakeGh(open_pr="https://github.com/o/r/pull/40")
    assert release.ship("1.3.0", work, auto_merge=True, git=git, gh=gh) == []
    out = capsys.readouterr().out
    assert "already contains origin/main" in out and "pull/40" in out and "auto-merge is on" in out
    assert must(work, "rev-parse", "HEAD~1") == before and gh.created() == []
    assert ["pr", "merge", "https://github.com/o/r/pull/40", "--auto", "--merge"] in gh.calls


def test_ship_standalone_cuts_from_the_fetched_origin_main_never_local_main(tmp_path) -> None:
    """The stale-main failure: a leftover local commit on main must not end up in the release branch."""
    work, origin = make_repo(tmp_path)
    tag_origin(work)
    other = tmp_path / "other"
    must(tmp_path, "clone", "-q", origin.as_posix(), other.as_posix())
    (other / "x.txt").write_text("x\n")
    must(other, "add", "x.txt")
    must(other, "commit", "-qm", "merged on GitHub")
    must(other, "push", "-q", "origin", "main")
    (work / "leftover.txt").write_text("stale\n")
    must(work, "add", "leftover.txt")
    must(work, "commit", "-qm", "leftover on local main")
    gh = FakeGh()
    assert release.ship("1.3.0", work, standalone=True, date="2026-11-08", git=git, gh=gh) == []
    assert must(work, "branch", "--show-current") == "release-1.3.0"
    assert must(work, "rev-parse", "HEAD~1") == must(origin, "rev-parse", "main")
    assert not (work / "leftover.txt").exists()
    assert must(origin, "log", "-1", "--format=%s", "release-1.3.0") == "Release 1.3.0"
    (create,) = gh.created()
    assert create[create.index("--head") + 1] == "release-1.3.0"


@pytest.mark.parametrize("where", ["main", "detached"])
def test_ship_refuses_main_and_detached_without_standalone(tmp_path, where) -> None:
    work, _ = make_repo(tmp_path)
    tag_origin(work)
    if where == "detached":
        must(work, "switch", "-q", "--detach", "HEAD")
    errors = release.ship("1.3.0", work, git=git, gh=FakeGh())
    assert len(errors) == 1 and "--standalone" in errors[0]
    assert release.pyproject_version(work) == "1.2.2"


def test_ship_refusals_leave_the_repository_as_it_was(tmp_path) -> None:
    work, _, _ = feature_repo(tmp_path, unreleased="")
    head = must(work, "rev-parse", "HEAD")
    gh = FakeGh()
    # empty Unreleased: found after the merge, which is undone
    errors = release.ship("1.3.0", work, git=git, gh=gh)
    assert errors == ["CHANGELOG.md's `## [Unreleased]` section is empty: nothing to release"]
    assert must(work, "rev-parse", "HEAD") == head and must(work, "status", "--porcelain") == ""
    # standalone: back where it started, the branch deleted
    assert release.ship("1.3.0", work, standalone=True, git=git, gh=gh) == errors
    assert must(work, "branch", "--show-current") == "feature"
    assert "release-1.3.0" not in must(work, "branch", "--list")
    # not newer than the latest tag; the tag exists on origin; locally
    assert release.ship("1.2.2", work, git=git, gh=gh) == [
        "tag v1.2.2 already exists on origin: pick the next version"
    ]
    assert release.ship("1.2.1", work, git=git, gh=gh) == [
        "version 1.2.1 isn't greater than the latest release tag v1.2.2"
    ]
    must(work, "tag", "v1.3.0")
    assert "already exists locally" in release.ship("1.3.0", work, git=git, gh=gh)[0]
    # a dirty tree
    (work / "notes.txt").write_text("x\n")
    assert release.ship("1.4.0", work, git=git, gh=gh) == [
        "the working tree has uncommitted changes (commit or stash them first)"
    ]
    assert gh.calls == [] and must(work, "rev-parse", "HEAD") == head


def test_ship_refuses_a_conflicting_merge(tmp_path) -> None:
    work, _, _ = feature_repo(tmp_path)
    (work / "other.txt").write_text("conflicting\n")
    must(work, "add", "other.txt")
    must(work, "commit", "-qm", "conflict")
    head = must(work, "rev-parse", "HEAD")
    (errors,) = release.ship("1.3.0", work, git=git, gh=FakeGh())
    assert "conflicts" in errors and "git merge origin/main" in errors
    assert must(work, "rev-parse", "HEAD") == head and must(work, "status", "--porcelain") == ""


# --- autotag (v1.5): what autotag.yml decides for a commit on main --------------------------------------


def test_autotag_decides_tag_release_or_nothing(tmp_path, capsys, monkeypatch) -> None:
    work, _ = make_repo(tmp_path)
    tag_origin(work)
    # v1.2.2 names HEAD: a re-run there only makes sure the GitHub release exists
    assert release.autotag(work, "origin", git) == ("release", "v1.2.2", [])
    (work / "f.txt").write_text("f\n")
    must(work, "add", "f.txt")
    must(work, "commit", "-qm", "a feature merge, no version change")
    assert release.autotag(work, "origin", git) == ("none", "v1.2.2", [])
    release_commit(work)  # the merged bump
    out = tmp_path / "gh-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert release.main(["autotag"], work, git) == 0
    assert out.read_text() == "action=tag\ntag=v1.3.0\n"
    assert capsys.readouterr().out == "action=tag\ntag=v1.3.0\n"
    must(work, "tag", "-a", "v1.3.0", "-m", "x")
    must(work, "push", "-q", "origin", "v1.3.0")
    assert release.autotag(work, "origin", git) == ("release", "v1.3.0", [])  # a re-run on the tagged commit


def test_autotag_none_for_an_older_version_and_error_for_a_broken_bump(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    tag_origin(work, "v1.4.0")
    assert release.autotag(work, "origin", git) == ("none", "v1.2.2", [])
    text = (work / "pyproject.toml").read_text().replace('version = "1.2.2"', 'version = "1.5.0"', 1)
    (work / "pyproject.toml").write_text(text)
    must(work, "commit", "-qam", "half a bump")
    action, name, errors = release.autotag(work, "origin", git)
    assert (action, name) == ("none", "v1.5.0") and len(errors) == 2  # __version__ and the changelog
    assert release.main(["autotag"], work, git) == 1
    _, errors = release.remote_tags("nowhere", work, git)
    assert errors and errors.startswith("git ls-remote nowhere failed")


def test_remote_tags_peel_annotated_tags_and_sort_by_semver(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    tag_origin(work, "v1.10.0")
    must(work, "tag", "v1.9.0")  # lightweight
    must(work, "push", "-q", "origin", "v1.9.0")
    tags, err = release.remote_tags("origin", work, git)
    head = must(work, "rev-parse", "HEAD")
    assert err is None and tags == {"v1.10.0": head, "v1.9.0": head}
    assert release.latest(tags) == "v1.10.0" and release.latest({}) is None


def test_check_defaults_to_the_pyproject_version(tmp_path) -> None:
    work, _ = make_repo(tmp_path)
    release_commit(work)
    assert release.main(["check"], work, git) == 0


# --- ci-reuse: release.yml skips the repeat CI run on a clean merge whose PR passed a full run ------------

MERGE, HEAD, MAIN = "a" * 40, "b" * 40, "c" * 40
RUN_URL = "https://github.com/o/r/actions/runs/7"


def full_jobs() -> list[dict]:
    names = [n for n in release.CI_FULL_JOBS if n != "controller"]
    names += ["controller (ubuntu-24.04, 3.12)", "controller (macos-15, 3.13)", "config-repo / deploy"]
    jobs = [{"name": n, "status": "completed", "conclusion": "success"} for n in names]
    jobs[-1]["conclusion"] = "skipped"  # the reusable server.yml's deploy never runs from the product's CI
    return jobs


class FakeApi:
    """`gh api <path>` from a dict of path -> JSON (a missing path is an API error); records the paths."""

    def __init__(self, **changes: object) -> None:
        pr = {
            "number": 41,
            "merged_at": "2026-11-08T10:00:00Z",
            "merge_commit_sha": MERGE,
            "base": {"ref": "main"},
            "head": {"sha": HEAD, "repo": {"full_name": "o/r"}},
        }
        run = {
            "id": 7,
            "head_sha": HEAD,
            "path": ".github/workflows/ci.yml",
            "status": "completed",
            "conclusion": "success",
            "created_at": "2026-11-08T09:00:00Z",
            "html_url": RUN_URL,
        }
        jobs = full_jobs()
        self.responses: dict[str, object] = {
            f"repos/o/r/commits/{MERGE}/pulls": [pr],
            f"repos/o/r/git/commits/{MERGE}": {
                "tree": {"sha": "t1"},
                "parents": [{"sha": MAIN}, {"sha": HEAD}],
            },
            f"repos/o/r/compare/{MAIN}...{HEAD}": {"behind_by": 0, "ahead_by": 3},
            f"repos/o/r/git/commits/{HEAD}": {"tree": {"sha": "t1"}, "parents": [{"sha": MAIN}]},
            f"repos/o/r/actions/workflows/ci.yml/runs?head_sha={HEAD}&per_page=100": {"workflow_runs": [run]},
            "repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100": {
                "total_count": len(jobs),
                "jobs": jobs,
            },
        }
        self.pr, self.run, self.jobs = pr, run, jobs
        self.paths: list[str] = []

    def __call__(self, args: Sequence[str], cwd: Path) -> subprocess.CompletedProcess[str]:
        args = list(args)
        assert args[0] == "api" and "GITHUB_TOKEN" not in " ".join(args)
        path = args[-1]
        self.paths.append(path)
        if path not in self.responses:
            return subprocess.CompletedProcess(args, 1, "", "HTTP 404: Not Found")
        body = self.responses[path]
        return subprocess.CompletedProcess(args, 0, body if isinstance(body, str) else json.dumps(body), "")


def test_ci_reuse_a_clean_merge_whose_head_passed_a_full_run(tmp_path, monkeypatch, capsys) -> None:
    api = FakeApi()
    reuse, why = release.ci_reuse("o/r", MERGE, gh=api)
    assert reuse is True and "#41" in why and RUN_URL in why
    # read-only GETs only (gh api without -X/-f/--input is a GET)
    assert len(api.paths) == 6
    # the CLI writes reuse=true to GITHUB_OUTPUT and always exits 0
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    assert release.main(["ci-reuse", "--repo", "o/r", "--sha", MERGE], gh=FakeApi()) == 0
    assert out.read_text() == "reuse=true\n" and capsys.readouterr().out.startswith("reuse=true: ")
    out.write_text("")
    assert release.main(["ci-reuse", "--repo", "o/r", "--sha", HEAD], gh=FakeApi()) == 0
    assert out.read_text() == "reuse=false\n"


def _no(api: FakeApi, sha: str = MERGE, repo: str = "o/r") -> str:
    reuse, why = release.ci_reuse(repo, sha, gh=api)
    assert reuse is False, why
    return why


def test_ci_reuse_refuses_anything_but_a_clean_merge() -> None:
    assert "not a repository and commit" in _no(FakeApi(), sha="HEAD")
    assert "not a repository and commit" in _no(FakeApi(), repo="o/r; rm")
    api = FakeApi()
    api.responses[f"repos/o/r/commits/{MERGE}/pulls"] = []  # a direct push to main
    assert "0 merged pull requests" in _no(api)
    api = FakeApi()
    api.pr["merged_at"] = None  # an open PR that merely contains the commit
    assert "0 merged pull requests" in _no(api)
    api = FakeApi()
    api.pr["base"] = {"ref": "v1"}
    assert "wasn't merged into main" in _no(api)
    api = FakeApi()
    api.pr["head"]["repo"] = {"full_name": "fork/r"}  # its CI ran without the repository's secrets
    assert "another repository" in _no(api)
    api = FakeApi()  # squash or rebase merge: one parent
    api.responses[f"repos/o/r/git/commits/{MERGE}"] = {"tree": {"sha": "t1"}, "parents": [{"sha": MAIN}]}
    assert "isn't a merge" in _no(api)
    api = FakeApi()  # main moved on after the PR's head: its CI didn't test what was merged
    api.responses[f"repos/o/r/compare/{MAIN}...{HEAD}"] = {"behind_by": 1, "ahead_by": 3}
    assert "didn't contain main" in _no(api)
    api = FakeApi()
    api.responses[f"repos/o/r/git/commits/{HEAD}"] = {"tree": {"sha": "t2"}}
    assert "tree differs" in _no(api)


def test_ci_reuse_needs_a_finished_passed_full_run_of_the_head() -> None:
    api = FakeApi()
    api.responses[f"repos/o/r/actions/workflows/ci.yml/runs?head_sha={HEAD}&per_page=100"] = {
        "workflow_runs": []
    }
    assert "no ci.yml run" in _no(api)
    api = FakeApi()
    api.run["head_sha"] = MAIN
    assert "no ci.yml run" in _no(api)
    api = FakeApi()
    api.run["path"] = ".github/workflows/release.yml"
    assert "no ci.yml run" in _no(api)
    api = FakeApi()
    api.run["conclusion"] = "failure"
    assert "is failure" in _no(api)
    api = FakeApi()  # a re-run still going
    api.responses[f"repos/o/r/actions/workflows/ci.yml/runs?head_sha={HEAD}&per_page=100"] = {
        "workflow_runs": [api.run, {**api.run, "id": 8, "status": "in_progress", "conclusion": None}]
    }
    assert "hasn't finished" in _no(api)
    api = FakeApi()  # the latest run decides: an older green one doesn't outweigh a newer red one
    newer = {**api.run, "id": 8, "conclusion": "failure", "created_at": "2026-11-08T09:30:00Z"}
    api.responses[f"repos/o/r/actions/workflows/ci.yml/runs?head_sha={HEAD}&per_page=100"] = {
        "workflow_runs": [api.run, newer]
    }
    assert "is failure" in _no(api)
    # a scoped pull request run (rust untouched: job skipped) or a bump-only one (no controller) isn't full
    for name, conclusion in [("rust", "skipped"), ("controller (macos-15, 3.13)", "failure")]:
        api = FakeApi()
        next(j for j in api.jobs if j["name"] == name)["conclusion"] = conclusion
        assert f"job {name} is {conclusion}" in _no(api)
    api = FakeApi()
    api.jobs[:] = [j for j in api.jobs if not j["name"].startswith("controller")]
    api.responses["repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100"] = {
        "total_count": len(api.jobs),
        "jobs": api.jobs,
    }
    assert "job controller isn't in the run" in _no(api)
    api = FakeApi()
    api.responses["repos/o/r/actions/runs/7/jobs?filter=latest&per_page=100"] = {
        "total_count": 120,
        "jobs": [],
    }
    assert "more jobs than one page" in _no(api)


def test_ci_reuse_fails_safe_on_api_trouble() -> None:
    api = FakeApi()
    del api.responses[f"repos/o/r/compare/{MAIN}...{HEAD}"]
    assert "HTTP 404" in _no(api)
    api = FakeApi()
    api.responses[f"repos/o/r/commits/{MERGE}/pulls"] = "<html>rate limited</html>"
    assert "couldn't decide (JSONDecodeError" in _no(api)
    api = FakeApi()
    api.responses[f"repos/o/r/git/commits/{MERGE}"] = ["not", "an", "object"]
    assert "couldn't decide (AttributeError" in _no(api)
    assert "couldn't decide" in _no(lambda args, cwd: subprocess.CompletedProcess(args, 0, "null", ""))


def test_ci_full_jobs_are_cis_jobs() -> None:
    """CI_FULL_JOBS names every job of ci.yml (job ids, no `name:`), config-repo by its reusable validate job."""
    ci = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    ids = set(ci["jobs"])
    assert not any("name" in job for job in ci["jobs"].values())
    server = yaml.safe_load((REPO / ".github/workflows/server.yml").read_text())
    assert "name" not in server["jobs"]["validate"]
    assert set(release.CI_FULL_JOBS) == (ids - {"config-repo"}) | {"config-repo / validate"}
