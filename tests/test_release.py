"""Release mechanics: tag == pyproject version == mcctl.__version__, CHANGELOG section (scripts/release.py)."""

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest
import release
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
    assert "release.py tag v1.3.0" in capsys.readouterr().out
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
        path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    must(work, "commit", "-qam", "crlf")
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
