"""P10: `mcctl panel-pr-check`, the gate before the config repo's workflow merges a panel pull request by itself
(design §5 "Whitelist → Git" (3)): players.yml only, valid and canonical, ops unchanged, whitelist changed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from mcctl import cli, panelpr

A = "3f2a0000-0000-4000-8000-000000000c41"
B = "5c090000-0000-4000-8000-00000000e212"
BASE = f'whitelist:\n  - {{name: "Em_K", uuid: "{A}"}}\nops:\n  - {{name: "Em_K", uuid: "{A}", level: 4}}\n'
ADDED = (
    f'whitelist:\n  - {{name: "Em_K", uuid: "{A}"}}\n  - {{name: "jonas_r", uuid: "{B}"}}\n'
    f'ops:\n  - {{name: "Em_K", uuid: "{A}", level: 4}}\n'
)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "players.yml").write_text(BASE)
    (tmp_path / "server.yml").write_text("name: x\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    base = git(tmp_path, "rev-parse", "HEAD")
    git(tmp_path, "checkout", "-qb", "panel/1")
    return tmp_path, base


def commit(root: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (root / name).write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-qm", "change")
    return git(root, "rev-parse", "HEAD")


def test_a_whitelist_only_change_may_merge(repo) -> None:
    root, base = repo
    head = commit(root, {"players.yml": ADDED})
    assert panelpr.check(root, base, head) == []
    assert cli.main(["panel-pr-check", "-C", str(root), "--base", base, "--head", head]) == 0


@pytest.mark.parametrize(
    ("files", "why"),
    [
        ({"players.yml": ADDED, "server.yml": "name: y\n"}, "only players.yml"),
        ({"server.yml": "name: y\n"}, "more than world.active and world.library"),
        ({"players.yml": ADDED.replace("level: 4", "level: 3")}, "the ops changed"),
        ({"players.yml": ADDED.replace("  - {name: \"jonas_r\"", "  - {name: \"zz\"")}, "players.yml"),
        ({"players.yml": f'whitelist:\n  - {{name: "jonas_r", uuid: "{B}"}}\n  - {{name: "Em_K", uuid: "{A}"}}\n'
                         f'ops:\n  - {{name: "Em_K", uuid: "{A}", level: 4}}\n'}, "canonical"),
        ({"players.yml": BASE + "# comment\n"}, "canonical"),
    ],
)  # fmt: skip
def test_anything_else_is_left_for_a_human(repo, files: dict[str, str], why: str, capsys) -> None:
    root, base = repo
    head = commit(root, files)
    reasons = panelpr.check(root, base, head)
    assert reasons and any(why in r for r in reasons), reasons
    assert cli.main(["panel-pr-check", "-C", str(root), "--base", base, "--head", head]) == 1
    assert "left for a human" in capsys.readouterr().err


def test_no_change_and_unknown_commits(repo) -> None:
    root, base = repo
    assert panelpr.check(root, base, base) == [
        "the diff must touch only players.yml (or only server.yml's world library); it touches nothing"
    ]
    assert panelpr.check(root, "0" * 40, "HEAD")[0].startswith("git diff")
