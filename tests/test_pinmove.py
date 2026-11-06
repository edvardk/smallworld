"""P15: a config repo pinned behind the running product checkout (mcctl/pinmove.py, setup/init, doctor).

Real git in temporary repositories: a tiny "product" history (A -> B, plus an unrelated root U) whose `origin`
is the pinned product repository (B on origin/main; `git fetch` is faked), and a config repo whose workflow pins
one of its commits.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from conftest import REPO

from mcctl import cli, configrepo, doctor, gate, migrations, mock, pinmove, schema, ui, updates
from mcctl import product as product_mod

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        [*GIT, "-C", str(root), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def runner(argv):
    argv = list(argv)
    if argv[0] == "git":
        argv = [*GIT, *argv[1:]]
    if argv[0] == "gh":  # no GitHub: `gh repo view` fails, so nothing is pushed
        return subprocess.CompletedProcess(argv, 1, "", "gh: not here")
    if "fetch" in argv:  # GitHub isn't reachable from the tests: the refs set up below are the fetched state
        return subprocess.CompletedProcess(argv, 0, "", "")
    return subprocess.run(argv, capture_output=True, text=True, check=False)


@pytest.fixture
def product(tmp_path: Path) -> dict[str, object]:
    root = tmp_path / "product"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "f").write_text("a\n")
    git(root, "add", "f")
    git(root, "commit", "-q", "-m", "A: first")
    a = git(root, "rev-parse", "HEAD")
    (root / "f").write_text("b\n")
    git(root, "commit", "-q", "-am", "B: fix the setup bug")
    b = git(root, "rev-parse", "HEAD")
    git(root, "remote", "add", "origin", f"git@github.com:{configrepo.PRODUCT_REPOSITORY}.git")
    git(root, "update-ref", "refs/remotes/origin/main", b)  # B is pushed
    git(root, "checkout", "-q", "--orphan", "other")
    git(root, "commit", "-q", "-m", "U: unrelated root")
    u = git(root, "rev-parse", "HEAD")
    git(root, "checkout", "-q", "main")
    return {"root": root, "a": a, "b": b, "u": u}


def config(tmp_path: Path, sha: str) -> Path:
    root = tmp_path / "config"
    configrepo.materialize(root, sha=sha, version="v0.9.0")
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


def pin_of(root: Path) -> configrepo.Pin:
    pin, errors = configrepo.read_pin((root / configrepo.WORKFLOW).read_text())
    assert pin is not None, errors
    return pin


def talk(answer: str | None) -> ui.UI:
    return ui.UI(answers={} if answer is None else {pinmove.ANSWER_KEY: answer}, rich=False)


def test_status_relations(product) -> None:
    root, a, b, u = product["root"], product["a"], product["b"], product["u"]
    pin = configrepo.Pin("o/p", a, "v0.9.0")
    st = pinmove.status(pin, runner, root)
    assert st.relation == "stale" and st.running == b and st.behind == 1
    assert st.commits[0].endswith("B: fix the setup bug")
    assert pinmove.status(configrepo.Pin("o/p", b, None), runner, root).relation == "current"
    assert pinmove.status(configrepo.Pin("o/p", b, None), runner, root, running=a).relation == "newer"
    assert pinmove.status(configrepo.Pin("o/p", u, None), runner, root).relation == "unrelated"
    missing = pinmove.status(configrepo.Pin("o/p", "2" * 40, None), runner, root)
    assert missing.relation == "unknown" and "isn't in" in missing.reason
    (root / "f").write_text("dirty\n")
    assert pinmove.status(pin, runner, root).relation == "unknown"


def test_offer_moves_a_stale_pin_on_typed_yes(tmp_path, product, capsys) -> None:
    cfg = config(tmp_path, product["a"])
    assert pinmove.offer(cfg, talk("yes"), runner, product_root=product["root"], version="v1.0.0")
    pin = pin_of(cfg)
    assert (pin.sha, pin.version) == (product["b"], "v1.0.0")
    assert git(cfg, "status", "--porcelain") == ""  # committed
    subject = git(cfg, "log", "-1", "--format=%s")
    assert subject == f"mcctl: move the product pin v0.9.0 -> v1.0.0 ({product['b'][:12]})"
    assert git(cfg, "show", "--name-only", "--format=", "HEAD") == configrepo.WORKFLOW  # only the workflow
    out = capsys.readouterr().out
    assert "B: fix the setup bug" in out and "the commit is local" in out
    # Moved: a second offer has nothing to do
    assert not pinmove.offer(cfg, talk(None), runner, product_root=product["root"])


@pytest.mark.parametrize("answer", ["", "y", "no", "Yes please"])
def test_offer_needs_the_word_yes(tmp_path, product, answer) -> None:
    cfg = config(tmp_path, product["a"])
    before = (cfg / configrepo.WORKFLOW).read_text()
    assert not pinmove.offer(cfg, talk(answer), runner, product_root=product["root"])
    assert (cfg / configrepo.WORKFLOW).read_text() == before


def test_offer_never_moves_backwards_or_sideways(tmp_path, product, capsys) -> None:
    for sha, running in ((product["b"], product["a"]), (product["u"], None)):
        cfg = config(tmp_path / sha[:6], sha)
        assert not pinmove.offer(cfg, talk("yes"), runner, product_root=product["root"], running=running)
        assert pin_of(cfg).sha == sha
    assert "isn't on this checkout's history" in capsys.readouterr().out


def test_offer_refuses_an_unpushed_commit(tmp_path, product, capsys) -> None:
    root = product["root"]
    (root / "f").write_text("c\n")
    git(root, "commit", "-q", "-am", "C: local only")
    cfg = config(tmp_path, product["a"])
    assert not pinmove.offer(cfg, talk("yes"), runner, product_root=root)
    assert pin_of(cfg).sha == product["a"]
    assert "isn't on edvardk/smallworld's main" in capsys.readouterr().out
    assert pinmove.offer(cfg, talk("yes"), runner, product_root=root, require_published=False)
    assert pin_of(cfg).sha == git(root, "rev-parse", "HEAD")


def test_only_the_pinned_repositorys_main_counts_as_pushed(tmp_path, product, capsys) -> None:
    """P15 review: a fork, a feature branch or a stale PR ref containing the commit never makes it movable."""
    root = product["root"]
    (root / "f").write_text("c\n")
    git(root, "commit", "-q", "-am", "C: on a fork and a feature branch only")
    c = git(root, "rev-parse", "HEAD")
    git(root, "remote", "add", "fork", "https://github.com/someone/smallworld.git")
    git(root, "update-ref", "refs/remotes/fork/main", c)
    git(root, "update-ref", "refs/remotes/origin/feature", c)
    git(root, "update-ref", "refs/remotes/origin/pr/7", c)
    pin = configrepo.Pin(configrepo.PRODUCT_REPOSITORY, product["a"], "v0.9.0")
    assert "isn't on" in (pinmove.published(runner, root, pin, c) or "")
    # https and ssh forms both match; the case of owner/repo doesn't matter; a fork's URL never does
    assert pinmove.product_remote(runner, root, "EdvardK/SmallWorld") == "origin"
    git(root, "remote", "set-url", "origin", f"https://github.com/{configrepo.PRODUCT_REPOSITORY}")
    assert pinmove.product_remote(runner, root, configrepo.PRODUCT_REPOSITORY) == "origin"
    assert pinmove.product_remote(runner, root, "other/repo") is None
    git(root, "update-ref", "refs/remotes/origin/main", c)  # merged into the product's main: now it counts
    assert pinmove.published(runner, root, pin, c) is None
    # the old pin must be an ancestor of the new commit
    assert "isn't an ancestor" in (
        pinmove.published(runner, root, configrepo.Pin(pin.repository, product["u"], None), c) or ""
    )
    # no remote for the pinned repository: refused, and said why
    git(root, "remote", "remove", "origin")
    cfg = config(tmp_path, product["a"])
    assert not pinmove.offer(cfg, talk("yes"), runner, product_root=root)
    assert "no remote of" in capsys.readouterr().out


def test_a_failed_product_fetch_is_reported_not_fatal(tmp_path, product) -> None:
    def offline(argv):
        if "fetch" in argv:
            return subprocess.CompletedProcess(list(argv), 128, "", "fatal: unable to access")
        return runner(argv)

    pin = configrepo.Pin(configrepo.PRODUCT_REPOSITORY, product["a"], None)
    assert (
        pinmove.published(offline, product["root"], pin, product["b"]) is None
    )  # last fetched state is fine
    c_root = product["root"]
    (c_root / "f").write_text("c\n")
    git(c_root, "commit", "-q", "-am", "C")
    why = pinmove.published(offline, c_root, pin, git(c_root, "rev-parse", "HEAD")) or ""
    assert "`git fetch origin main` failed: fatal: unable to access" in why


def test_version_of_never_labels_an_untagged_commit_as_a_release(product) -> None:
    root, a, b = product["root"], product["a"], product["b"]
    # no release tag yet (v1.0.0 isn't tagged today): sorts before every release
    assert pinmove.version_of(runner, root, b) == f"v0.0.0-2-g{b[:12]}"
    git(root, "tag", "v1.0.0-rc1", a)  # a pre-release tag is never the base
    assert pinmove.version_of(runner, root, b) == f"v0.0.0-2-g{b[:12]}"
    git(root, "tag", "-a", "-m", "v1.0.0", "v1.0.0", a)
    assert pinmove.version_of(runner, root, a) == "v1.0.0"
    assert pinmove.version_of(runner, root, b) == f"v1.0.0-1-g{b[:12]}"
    git(root, "tag", "v1.0.1", b)  # lightweight tags count too
    assert pinmove.version_of(runner, root, b) == "v1.0.1"
    for v in (f"v0.0.0-2-g{b[:12]}", f"v1.0.0-1-g{b[:12]}"):
        assert configrepo.VERSION.fullmatch(v)  # the config repo's pin comment accepts it


def test_offer_writes_the_described_version(tmp_path, product) -> None:
    cfg = config(tmp_path, product["a"])
    assert pinmove.offer(cfg, talk("yes"), runner, product_root=product["root"])
    assert pin_of(cfg).version == f"v0.0.0-2-g{product['b'][:12]}"


def test_untagged_pins_sort_before_the_next_release_and_never_auto_merge(tmp_path) -> None:
    sha = "abcdef123456"
    assert not updates.newer("v1.0.0", f"v1.0.0-3-g{sha}")  # its own base release is older, not an update
    assert not updates.newer("v0.9.9", f"v1.0.0-3-g{sha}")
    assert updates.newer("v1.0.1", f"v1.0.0-3-g{sha}") and updates.newer("v2.0.0", f"v1.0.0-3-g{sha}")
    assert updates.newer("v0.0.1", f"v0.0.0-5-g{sha}") and updates.newer("v1.0.0", f"v0.0.0-5-g{sha}")
    assert updates.newer("v1.0.0", None) and not updates.newer("v1.0.0", "v1.0.0")
    assert not updates.newer(f"v1.0.1-1-g{sha}", "v1.0.0")  # only a release is ever proposed
    assert not updates.is_patch(f"v1.0.0-3-g{sha}", "v1.0.1")
    assert not updates.is_patch(f"v0.0.0-3-g{sha}", "v0.0.1")
    assert updates.is_patch("v1.0.0", "v1.0.1")
    # the gate (what may merge without the admin) agrees: an untagged side never auto-merges
    root = tmp_path / "c"
    configrepo.materialize(root, sha="a" * 40, version=f"v1.0.0-3-g{sha}")
    old = (root / configrepo.WORKFLOW).read_text()
    new = updates.bump_pin(old, configrepo.PRODUCT_REPOSITORY, "b" * 40, "v1.0.1")
    why = gate.check_product_bump(old, new, "", lambda req: pytest.fail("no network"))  # type: ignore[arg-type,return-value]
    assert why and "an untagged pin (vX.Y.Z-N-g<sha>) never does" in why[0]
    # migrations: X.Y.Z-N-g<sha> runs every migration after X.Y.Z (idempotent), v0.0.0-N all of them
    reg = (
        migrations.Migration("1.0.0", "a", lambda t: t),
        migrations.Migration("1.1.0", "b", lambda t: t),
        migrations.Migration("2.0.0", "c", lambda t: t),
    )
    assert [m.version for m in migrations.pending(f"v1.0.0-3-g{sha}", "1.5.0", reg)] == ["1.1.0"]
    assert [m.version for m in migrations.pending(f"v0.0.0-3-g{sha}", "2.0.0", reg)] == [
        "1.0.0",
        "1.1.0",
        "2.0.0",
    ]


def github_config(tmp_path: Path, sha: str) -> Path:
    cfg = config(tmp_path, sha)
    git(cfg, "remote", "add", "origin", "https://github.com/mcctl-tests/tests-mc-config.git")
    git(cfg, "update-ref", "refs/remotes/origin/main", "HEAD")
    return cfg


def on_github(calls: list[list[str]], *, push_rc: int = 0, fetch_rc: int = 0):
    """gh sees the config repo; `git fetch origin main` in it records origin/main as FETCH_HEAD; push faked."""

    def fake(argv):
        argv = list(argv)
        calls.append(argv)
        if argv[0] == "gh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if "push" in argv:
            return subprocess.CompletedProcess(
                argv, push_rc, "", "" if push_rc == 0 else "GH013: rule violations"
            )
        if (
            "fetch" in argv
            and argv[-2:] == ["origin", "main"]
            and "tests-mc-config" in (runner(["git", "-C", argv[2], "remote", "get-url", "origin"]).stdout)
        ):
            if fetch_rc:
                return subprocess.CompletedProcess(argv, fetch_rc, "", "fatal: could not read")
            upstream = git(Path(argv[2]), "rev-parse", "refs/remotes/origin/main")
            (Path(argv[2]) / ".git" / "FETCH_HEAD").write_text(f"{upstream}\t\tbranch 'main' of github\n")
            return subprocess.CompletedProcess(argv, 0, "", "")
        return runner(argv)

    return fake


def test_offer_pushes_exactly_one_commit_when_main_is_up_to_date(tmp_path, product, capsys) -> None:
    cfg = github_config(tmp_path, product["a"])
    calls: list[list[str]] = []
    assert pinmove.offer(cfg, talk("yes"), on_github(calls), product_root=product["root"])
    assert ["gh", "repo", "view", "mcctl-tests/tests-mc-config", "--json", "name"] in calls
    head = git(cfg, "rev-parse", "HEAD")
    assert ["git", "-C", str(cfg), "push", "-q", "origin", f"{head}:refs/heads/main"] in calls
    assert git(cfg, "rev-list", "--count", "origin/main..HEAD") == "1"
    assert "pushed to main" in capsys.readouterr().out


@pytest.mark.parametrize("case", ["branch", "ahead", "behind", "dirty", "fetch"])
def test_offer_refuses_unless_on_an_up_to_date_main(tmp_path, product, capsys, case) -> None:
    cfg = github_config(tmp_path, product["a"])
    fetch_rc = 0
    if case == "branch":
        git(cfg, "checkout", "-qb", "feature")
    elif case == "ahead":  # an unrelated local commit would be pushed along
        (cfg / "notes.txt").write_text("x\n")
        git(cfg, "add", "notes.txt")
        git(cfg, "commit", "-qm", "local only")
    elif case == "behind":
        git(cfg, "update-ref", "refs/remotes/origin/main", git(cfg, "commit-tree", "-m", "x", "HEAD^{tree}"))
    elif case == "dirty":
        wf = cfg / configrepo.WORKFLOW
        wf.write_text(wf.read_text() + "# local edit\n")
    else:
        fetch_rc = 1
    before = git(cfg, "rev-parse", "HEAD")
    calls: list[list[str]] = []
    assert not pinmove.offer(
        cfg, talk("yes"), on_github(calls, fetch_rc=fetch_rc), product_root=product["root"]
    )
    assert git(cfg, "rev-parse", "HEAD") == before and pin_of(cfg).sha == product["a"]
    assert not any("push" in c for c in calls)
    assert "git pull --rebase" in capsys.readouterr().out


def test_a_rejected_push_explains_the_pull_request_route(tmp_path, product, capsys) -> None:
    cfg = github_config(tmp_path, product["a"])
    assert pinmove.offer(cfg, talk("yes"), on_github([], push_rc=1), product_root=product["root"])
    out = " ".join(capsys.readouterr().out.split())
    assert "GH013" in out and "gh pr create --head mcctl/product-pin-" in out and "The commit is local" in out


def test_doctor_reports_a_stale_pin(tmp_path, product, monkeypatch) -> None:
    monkeypatch.delenv("MCCTL_PRODUCT_DIR")
    monkeypatch.setattr(schema, "PRODUCT_ROOT", product["root"])
    report = doctor.Report()
    repo = type("R", (), {"pin": configrepo.Pin("o/p", product["a"], "v0.9.0")})()
    doctor.check_pin(report, repo, {}, doctor.Deps(run=runner))  # type: ignore[arg-type]
    (row,) = report.results
    assert row.level == "warn" and "1 commit(s) behind" in row.detail and "doctor --fix" in row.detail


def test_init_mock_offers_the_move_before_anything_else(tmp_path, product, monkeypatch, capsys) -> None:
    """`mcctl init --mock` against a config repo pinned behind the running checkout: answers approve it."""
    monkeypatch.delenv(product_mod.ENV_OVERRIDE)
    monkeypatch.setattr(schema, "PRODUCT_ROOT", product["root"])
    cfg = config(tmp_path, product["a"])
    answers = tmp_path / "answers.yml"
    answers.write_text(f"{pinmove.ANSWER_KEY}: 'yes'\ndir: x\n")
    monkeypatch.setattr("mcctl.init.run", lambda *a, **k: 0)  # only the pre-check is under test
    rc = cli.main(["init", "-C", str(cfg), "--answers", str(answers), "--non-interactive", "--mock"])
    assert rc == 0
    assert pin_of(cfg).sha == product["b"]
    # Outside --mock an answers file can't approve it: a person types yes
    rc = cli.main(["init", "-C", str(cfg), "--answers", str(answers), "--non-interactive"])
    assert rc == 2 and pinmove.ANSWER_KEY in capsys.readouterr().err


def test_mock_deps_and_e2e_answers_know_the_key() -> None:
    assert mock.deps  # the mock's git runs real git, so the e2e exercises the real ancestry check
    assert pinmove.ANSWER_KEY in (REPO / "tests/e2e/answers.yml").read_text()


def test_no_offer_with_product_sha_or_a_product_dir_override(tmp_path, product, monkeypatch) -> None:
    """P15 review: --product-sha / MCCTL_PRODUCT_DIR mean the running code isn't the clean checkout."""
    monkeypatch.setattr(schema, "PRODUCT_ROOT", product["root"])
    cfg = config(tmp_path, product["a"])
    answers = tmp_path / "answers.yml"
    answers.write_text(f"{pinmove.ANSWER_KEY}: 'yes'\ndir: x\n")
    monkeypatch.setattr("mcctl.init.run", lambda *a, **k: 0)
    offered: list[object] = []
    monkeypatch.setattr(pinmove, "offer", lambda *a, **k: offered.append(a) or False)
    base = ["init", "-C", str(cfg), "--answers", str(answers), "--non-interactive", "--mock"]
    assert cli.main([*base, "--product-sha", product["b"]]) == 0  # MCCTL_PRODUCT_DIR set by conftest, too
    monkeypatch.delenv(product_mod.ENV_OVERRIDE)
    assert cli.main([*base, "--product-sha", product["b"]]) == 0
    assert offered == []
    assert cli.main(base) == 0 and len(offered) == 1
    # doctor --fix with the override set: the warning row is the override itself, nothing to move
    monkeypatch.setenv(product_mod.ENV_OVERRIDE, str(product["root"]))
    report = doctor.Report()
    report.add("product pin", "warn", "MCCTL_PRODUCT_DIR overrides the pinned ...")
    monkeypatch.setattr(doctor, "run", lambda *a, **k: report)
    monkeypatch.setattr(doctor, "show", lambda *a, **k: None)
    assert cli.main(["doctor", "-C", str(cfg), "--fix"]) == 0 and len(offered) == 1


@pytest.mark.parametrize("command", ["init", "doctor"])
def test_ctrl_c_or_closed_stdin_at_the_confirmation_exits_cleanly(
    tmp_path, product, monkeypatch, capsys, command
) -> None:
    monkeypatch.delenv(product_mod.ENV_OVERRIDE)
    cfg = config(tmp_path, product["a"])

    def eof(*a, **k):
        raise EOFError

    monkeypatch.setattr(pinmove, "offer", eof)
    if command == "init":
        monkeypatch.setattr("mcctl.init.run", lambda *a, **k: 0)
        answers = tmp_path / "answers.yml"
        answers.write_text("dir: x\n")
        argv = ["init", "-C", str(cfg), "--answers", str(answers), "--non-interactive", "--mock"]
    else:
        report = doctor.Report()
        report.add("product pin", "warn", "stale")
        monkeypatch.setattr(doctor, "run", lambda *a, **k: report)
        monkeypatch.setattr(doctor, "show", lambda *a, **k: None)
        argv = ["doctor", "-C", str(cfg), "--fix"]
    assert cli.main(argv) == 1
    assert f"mcctl {command}:" in capsys.readouterr().err
