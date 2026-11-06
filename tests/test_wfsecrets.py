"""v1.2 rollout bug: a config repo's workflow that doesn't pass a secret the product needs (mc-config's missing
ARTIFACT_PUBLISH_TOKEN). mcctl/wfsecrets.py compares and edits the block; doctor, the pin move and
schema-migrate use it."""

from __future__ import annotations

import dataclasses
import subprocess
from pathlib import Path

import pytest
from conftest import REPO, example_server, make_config_repo

from mcctl import cli, configrepo, doctor, pinmove, ui, upgrade, wfsecrets

TEMPLATE = (REPO / "template" / configrepo.WORKFLOW).read_text()
PUBLISH = "      ARTIFACT_PUBLISH_TOKEN: ${{ secrets.ARTIFACT_PUBLISH_TOKEN }}\n"
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t.test"]


def without(text: str, *names: str) -> str:
    for n in names:
        line = f"      {n}: ${{{{ secrets.{n} }}}}\n"
        assert line in text
        text = text.replace(line, "")
    return text


def test_missing_and_add_keep_everything_else() -> None:
    old = without(TEMPLATE, "ARTIFACT_PUBLISH_TOKEN", "UPDATER_APP_KEY")
    assert wfsecrets.missing(old, TEMPLATE) == ["UPDATER_APP_KEY", "ARTIFACT_PUBLISH_TOKEN"]
    new = wfsecrets.add(old, ["UPDATER_APP_KEY", "ARTIFACT_PUBLISH_TOKEN"])
    assert wfsecrets.missing(new, TEMPLATE) == []
    assert new.startswith(old.rstrip("\n").rsplit("\n", 1)[0])  # only appended after the last entry
    assert new.endswith("      UPDATER_APP_KEY: ${{ secrets.UPDATER_APP_KEY }}\n" + PUBLISH)
    assert wfsecrets.missing(TEMPLATE, TEMPLATE) == [] and wfsecrets.add(TEMPLATE, []) == TEMPLATE


def test_add_inserts_inside_the_block_when_something_follows_it() -> None:
    text = (
        "on: push\njobs:\n  server:\n    uses: o/p/.github/workflows/server.yml@" + "0" * 40 + " # v1.0.0\n"
        "    secrets:\n      A: ${{ secrets.A }}\n\n      # a comment\n    with:\n      x: 1\n"
        "  other:\n    runs-on: ubuntu-24.04\n"
    )
    new = wfsecrets.add(text, ["B"])
    assert "      A: ${{ secrets.A }}\n      B: ${{ secrets.B }}\n\n      # a comment\n    with:" in new
    assert wfsecrets.passed(new) == ["A", "B"]
    bare = text.replace("    secrets:\n      A: ${{ secrets.A }}\n\n      # a comment\n", "")
    assert wfsecrets.passed(wfsecrets.add(bare, ["B"])) == ["B"]  # no block: one is created
    assert (
        wfsecrets.missing(text.replace("    secrets:\n      A", "    secrets: inherit\n      #A"), TEMPLATE)
        == []
    )
    with pytest.raises(wfsecrets.WorkflowError):
        wfsecrets.add(text, ["not a name"])
    with pytest.raises(wfsecrets.WorkflowError):
        wfsecrets.passed("jobs: {}\n")


def _config(tmp_path: Path, *, client: bool, drop: tuple[str, ...]) -> configrepo.ConfigRepo:
    data = example_server() | {"client": {"enabled": client}}
    root = make_config_repo(tmp_path / "c", data, renderable=True)
    wf = root / configrepo.WORKFLOW
    wf.write_text(without(wf.read_text(), *drop))
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "seed"]):
        subprocess.run([*GIT, "-C", str(root), *argv], check=True, capture_output=True)
    return configrepo.load(root)


def _run(argv):  # doctor's runner, with a committer for the tests
    argv = list(argv)
    if argv[0] == "git":
        argv = [*GIT, *argv[1:]]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def test_doctor_warns_fails_with_the_client_and_fix_commits(tmp_path: Path) -> None:
    repo = _config(tmp_path, client=False, drop=("ARTIFACT_PUBLISH_TOKEN",))
    report = doctor.Report()
    doctor.check_workflow_secrets(
        report, repo, {"MCCTL_PRODUCT_DIR": str(REPO)}, doctor.Deps(run=_run), False
    )
    (row,) = report.results
    assert row.level == "warn" and "ARTIFACT_PUBLISH_TOKEN" in row.detail and "doctor --fix" in row.detail
    on = dataclasses.replace(repo, server=dataclasses.replace(
        repo.server, client=dataclasses.replace(repo.server.client, enabled=True)))  # fmt: skip
    report = doctor.Report()
    doctor.check_workflow_secrets(report, on, {"MCCTL_PRODUCT_DIR": str(REPO)}, doctor.Deps(run=_run), False)
    assert report.results[0].level == "fail" and "the deploy fails" in report.results[0].detail
    report = doctor.Report()
    doctor.check_workflow_secrets(report, on, {"MCCTL_PRODUCT_DIR": str(REPO)}, doctor.Deps(run=_run), True)
    assert report.results[0].level == "ok" and "push to main" in report.results[0].detail
    assert wfsecrets.missing((repo.root / configrepo.WORKFLOW).read_text(), TEMPLATE) == []
    show = _run(["git", "-C", str(repo.root), "show", "--name-only", "--format=%s", "HEAD"]).stdout
    assert "mcctl doctor: pass ARTIFACT_PUBLISH_TOKEN" in show and configrepo.WORKFLOW in show
    report = doctor.Report()
    doctor.check_workflow_secrets(report, on, {"MCCTL_PRODUCT_DIR": str(REPO)}, doctor.Deps(run=_run), False)
    assert report.results[0].level == "ok"


def test_schema_migrate_adds_the_secrets_the_product_passes(tmp_path: Path, capsys) -> None:
    repo = _config(tmp_path, client=False, drop=("ARTIFACT_PUBLISH_TOKEN",))
    assert upgrade.schema_migrate(repo.root, "1.1.0", REPO) == [
        f"{configrepo.WORKFLOW}: passes ARTIFACT_PUBLISH_TOKEN (the product's template does)"
    ]
    assert upgrade.schema_migrate(repo.root, "1.1.0", REPO) == []  # idempotent
    assert cli.main(["schema-migrate", "-C", str(repo.root), "--from", "1.1.0"]) == 0


def test_the_pin_move_adds_missing_secrets_in_the_same_commit(tmp_path: Path) -> None:
    product = tmp_path / "product"
    (product / "template" / ".github" / "workflows").mkdir(parents=True)
    (product / "template" / configrepo.WORKFLOW).write_text(TEMPLATE)
    (product / "f").write_text("a\n")
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "A"]):
        _run(["git", "-C", str(product), *argv])
    a = _run(["git", "-C", str(product), "rev-parse", "HEAD"]).stdout.strip()
    (product / "f").write_text("b\n")
    _run(["git", "-C", str(product), "commit", "-qam", "B"])
    b = _run(["git", "-C", str(product), "rev-parse", "HEAD"]).stdout.strip()
    cfg = tmp_path / "config"
    configrepo.materialize(cfg, sha=a, version="v1.0.0")
    wf = cfg / configrepo.WORKFLOW
    wf.write_text(without(wf.read_text(), "ARTIFACT_PUBLISH_TOKEN"))
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "init"]):
        _run(["git", "-C", str(cfg), *argv])

    def runner(argv):
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 1, "", "no GitHub")
        return _run(argv)

    talk = ui.UI(answers={pinmove.ANSWER_KEY: "yes"}, rich=False)
    assert pinmove.offer(cfg, talk, runner, product_root=product, version="v1.2.0", require_published=False)
    text = wf.read_text()
    assert f"@{b} # v1.2.0" in text and wfsecrets.missing(text, TEMPLATE) == []
    assert _run(["git", "-C", str(cfg), "status", "--porcelain"]).stdout == ""
    assert "ARTIFACT_PUBLISH_TOKEN" in _run(["git", "-C", str(cfg), "log", "-1", "--format=%b"]).stdout
