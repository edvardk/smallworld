"""scripts/deploy_record.py: a config repo's deploy is skipped only when nothing deploy-relevant changed since the
last successful deploy (docs/ci.md), never on a manual dispatch or a product pin change."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import deploy_record as dr
import pytest

PRODUCT = "a" * 40
OTHER = "b" * 40


def git(repo: Path, *argv: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=True).stdout


def commit(repo: Path, files: dict[str, str], msg: str = "c") -> str:
    for name, text in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", msg)
    return git(repo, "rev-parse", "HEAD").strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    r = tmp_path / "config"
    r.mkdir()
    git(r, "init", "-q", "-b", "main")
    commit(
        r,
        {
            "server.yml": "minecraft: {}\n",
            "players.yml": "ops: []\n",
            "README.md": "# config\n",
            "expiry.yml": "credentials: []\n",
            ".github/workflows/server.yml": f"uses: o/p/.github/workflows/server.yml@{PRODUCT}\n",
        },
    )
    return r


def record_of(repo: Path, tmp_path: Path, product: str = PRODUCT) -> dict[str, str]:
    dr.write_record(repo, product, tmp_path / "rec" / "record.json")
    rec = dr.read_record(tmp_path / "rec" / "record.json")
    assert rec is not None
    return rec


def test_relevant_paths() -> None:
    for path in ("server.yml", "players.yml", "plugins.lock.yml", "branding/icon.png", dr.WORKFLOW):
        assert dr.relevant(path), path
    for path in ("README.md", "docs/notes.md", "expiry.yml", ".gitignore", ".env.example", "LICENSE",
                 ".github/dependabot.yml", ".github/workflows/other.yml"):  # fmt: skip
        assert not dr.relevant(path), path


def test_fingerprint_follows_what_a_deploy_applies(repo: Path) -> None:
    base = dr.fingerprint(repo, PRODUCT)
    assert dr.fingerprint(repo, OTHER) != base  # the product commit is part of it
    commit(repo, {"README.md": "# changed\n", "expiry.yml": "credentials: [x]\n", ".github/x.yml": "a: 1\n"})
    assert dr.fingerprint(repo, PRODUCT) == base
    commit(repo, {"players.yml": "ops: [alex]\n"})
    assert dr.fingerprint(repo, PRODUCT) != base


def test_skip_only_when_nothing_relevant_changed(repo: Path, tmp_path: Path) -> None:
    rec = record_of(repo, tmp_path)
    assert rec["config_sha"] == git(repo, "rev-parse", "HEAD").strip() and rec["product_sha"] == PRODUCT
    before = rec["config_sha"]
    commit(repo, {"README.md": "# docs only\n"})
    needed, why = dr.decide(repo, PRODUCT, "push", rec, before)
    assert not needed and before[:12] in why
    assert not dr.decide(repo, PRODUCT, "schedule", rec)[0]
    # a manual dispatch always deploys
    assert dr.decide(repo, PRODUCT, "workflow_dispatch", rec, before) == (
        True,
        "a manual deploy always deploys",
    )
    # a relevant change deploys and says which file
    commit(repo, {"server.yml": "minecraft: {motd: hi}\n"})
    needed, why = dr.decide(repo, PRODUCT, "schedule", rec)
    assert needed and "server.yml" in why and "README.md" not in why


def test_a_product_pin_change_always_deploys(repo: Path, tmp_path: Path) -> None:
    rec = record_of(repo, tmp_path)
    before = rec["config_sha"]
    commit(repo, {dr.WORKFLOW: f"uses: o/p/.github/workflows/server.yml@{OTHER}\n"})
    # even a (forged) record that claims this exact state was deployed doesn't skip a push that moved the pin
    forged = record_of(repo, tmp_path, OTHER)
    needed, why = dr.decide(repo, OTHER, "push", forged, before)
    assert needed and "product pin" in why
    # the record's product sha differs: deploy (a schedule has no push range)
    assert dr.decide(repo, OTHER, "schedule", rec)[0]


@pytest.mark.parametrize("before", ["", "0" * 40, "not-a-sha", "c" * 40])
def test_a_push_without_a_usable_before_deploys(repo: Path, tmp_path: Path, before: str) -> None:
    rec = record_of(repo, tmp_path)
    assert dr.decide(repo, PRODUCT, "push", rec, before)[0]  # "c"*40 isn't in the history: git fails


def test_unknown_or_unreadable_records_deploy(repo: Path, tmp_path: Path) -> None:
    assert dr.decide(repo, PRODUCT, "schedule", None)[0]
    bad = tmp_path / "bad.json"
    for text in ("", "[]", "{", json.dumps({"format": 99, "fingerprint": "x", "config_sha": "y", "product_sha": "z"}),
                 json.dumps({"format": dr.FORMAT, "fingerprint": 1})):  # fmt: skip
        bad.write_text(text)
        assert dr.read_record(bad) is None, text
    assert dr.read_record(tmp_path / "missing.json") is None


def test_cli_writes_the_decision(repo: Path, tmp_path: Path, monkeypatch, capsys) -> None:
    out = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    rec = tmp_path / "record.json"
    assert dr.main(["write", "--config", str(repo), "--product-sha", PRODUCT, "--out", str(rec)]) == 0
    args = [
        "decide",
        "--config",
        str(repo),
        "--product-sha",
        PRODUCT,
        "--record",
        str(rec),
        "--event",
        "schedule",
    ]
    assert dr.main(args) == 0
    assert "::notice::deploy skipped" in capsys.readouterr().out
    lines = out.read_text().splitlines()
    assert lines == ["deploy_needed=false", f"fingerprint={dr.fingerprint(repo, PRODUCT)}"]
    out.write_text("")
    assert dr.main([*args[:-1], "workflow_dispatch"]) == 0
    assert out.read_text().startswith("deploy_needed=true\n")
    # a broken checkout: deploy, with an empty fingerprint
    out.write_text("")
    assert (
        dr.main(
            ["decide", "--config", str(tmp_path / "nope"), "--product-sha", PRODUCT, "--record", str(rec)]
        )
        == 0
    )
    assert out.read_text() == "deploy_needed=true\nfingerprint=\n"
    with pytest.raises(SystemExit):
        dr.main(["fingerprint", "--config", str(repo), "--product-sha", "main"])
