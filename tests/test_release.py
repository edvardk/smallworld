"""Release mechanics: tag == pyproject version == mcctl.__version__, CHANGELOG section (scripts/release.py)."""

import shutil

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
