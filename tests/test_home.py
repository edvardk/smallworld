"""When plain `mcctl` opens the dashboard (mcctl/home.py), the status probe, the remembered config repo, `-C`
precedence. The dashboard itself: tests/test_dashboard.py. No SSH, no network: the probe is replaced.
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import pytest

from mcctl import cli, home, homecfg, remote, ui
from tests.conftest import make_config_repo


def online(cfg: object = None) -> remote.Status:
    return remote.Status(
        reachable=True,
        server="active",
        players_online=2,
        players_max=20,
        tps=19.94,
        mspt=12.3,
        last_main_run={"conclusion": "success", "createdAt": "2026-11-07T10:00:00Z"},
        pending_panel_prs=[{"number": 7}],
    )


# --- when the dashboard opens -----------------------------------------------------------------------------


class FakeTTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def tty_env(**extra: str) -> dict[str, str]:
    return {"TERM": "xterm-256color", **extra}


def test_interactive_needs_both_terminals_and_no_plain_switch() -> None:
    assert home.interactive(tty_env(), FakeTTY(), FakeTTY())
    assert not home.interactive(tty_env(), io.StringIO(), FakeTTY())  # stdin not a terminal
    assert not home.interactive(tty_env(), FakeTTY(), io.StringIO())  # stdout not a terminal
    for env in ({"NO_COLOR": "1"}, {"CI": "true"}, {ui.PLAIN_ENV: "1"}, {"TERM": "dumb"}):
        assert not home.interactive(tty_env(**env), FakeTTY(), FakeTTY()), env


def test_interactive_without_textual(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "HAVE_TEXTUAL", False)
    assert not home.interactive(tty_env(), FakeTTY(), FakeTTY())


def test_bare_mcctl_without_a_terminal_is_the_usage_error_as_before(capsys: pytest.CaptureFixture) -> None:
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2
    assert "the following arguments are required: COMMAND" in capsys.readouterr().err


def test_plain_keeps_the_usage_error_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("CI", "NO_COLOR", ui.PLAIN_ENV):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("sys.stdin", FakeTTY())
    monkeypatch.setattr("sys.stdout", FakeTTY())
    assert home.wanted([]) == home.Launch(None)  # this "terminal" would open it...
    monkeypatch.setattr(home, "main", lambda launch: pytest.fail("opened the dashboard"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--plain"])  # ...but not with --plain
    assert e.value.code == 2


def test_on_a_terminal_bare_mcctl_opens_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[home.Launch] = []
    monkeypatch.setattr(home, "interactive", lambda: True)
    monkeypatch.setattr(home, "main", lambda launch: opened.append(launch) or 0)
    assert cli.main([]) == 0
    assert cli.main(["-C", "somewhere"]) == 0
    assert cli.main(["--config-dir=elsewhere"]) == 0
    assert opened == [home.Launch(None), home.Launch(Path("somewhere")), home.Launch(Path("elsewhere"))]
    with pytest.raises(SystemExit) as e:  # anything else is a command line, parsed as before
        cli.main(["-C", "x", "y"])
    assert e.value.code == 2


# --- the config repo it opens, the status probe ------------------------------------------------------


def test_initial_repo_precedence(config_repo: Path, tmp_path: Path) -> None:
    other = make_config_repo(tmp_path / "other")
    homecfg.remember(other)
    assert home._initial(home.Launch(config_repo), tmp_path) == (config_repo.resolve(), "-C")
    assert home._initial(home.Launch(None), config_repo) == (config_repo.resolve(), "current directory")
    assert home._initial(home.Launch(None), tmp_path) == (other.resolve(), "remembered")


def test_probe_budget_cache_and_late_answer(config_repo: Path) -> None:
    from mcctl import configrepo

    cfg = configrepo.load(config_repo).server
    release, calls, now = threading.Event(), [], [0.0]

    def slow(c: object) -> remote.Status:
        calls.append(1)
        release.wait(10)
        return online()

    probe = home.StatusProbe(slow, budget=0.05, ttl=30, clock=lambda: now[0])
    assert "no answer within 0.05 s" in str(probe.get(config_repo, cfg))
    release.set()
    st = probe.get(config_repo, cfg)  # the same probe, finished: no second ssh
    assert isinstance(st, remote.Status) and calls == [1]
    now[0] = 10
    assert probe.get(config_repo, cfg) is st and calls == [1]  # cached
    now[0] = 31
    assert isinstance(probe.get(config_repo, cfg), remote.Status) and calls == [1, 1]  # stale: asked again
    assert isinstance(probe.get(config_repo, cfg, force=True), remote.Status) and len(calls) == 3


def test_quiet_run_never_raises() -> None:
    proc = home.quiet_run(["mcctl-no-such-binary-xyz"])
    assert proc.returncode == 127


# --- the remembered config repo -------------------------------------------------------------------------------


def test_config_file_location(tmp_path: Path) -> None:
    xdg = {"XDG_CONFIG_HOME": str(tmp_path)}  # absolute on every OS
    assert homecfg.config_file(xdg, "linux") == tmp_path / "mcctl" / "config.toml"
    assert homecfg.config_file({}, "linux") == Path.home() / ".config" / "mcctl" / "config.toml"
    assert (
        homecfg.config_file({"XDG_CONFIG_HOME": "relative"}, "darwin").parent.parent
        == Path.home() / ".config"
    )
    win = homecfg.config_file({"APPDATA": str(Path("C:/Users/a/AppData/Roaming"))}, "win32")
    assert win == Path("C:/Users/a/AppData/Roaming") / "mcctl" / "config.toml"


def test_toml_quoting_round_trips() -> None:
    import tomllib

    odd = Path('/srv/a "quoted" \\ dir/ä')  # quotes, a backslash, non-ASCII (Windows paths have backslashes)
    data = tomllib.loads(homecfg.dumps(homecfg.Settings(current=odd, repos=[odd, Path("/b")])))
    assert data == {"current": str(odd), "repos": [str(odd), str(Path("/b"))]}


def test_remember_round_trip_and_order(tmp_path: Path) -> None:
    a, b = make_config_repo(tmp_path / "a dir"), make_config_repo(tmp_path / "b")
    homecfg.remember(a)
    homecfg.remember(b)
    homecfg.remember(a)
    s = homecfg.load()
    assert s.current == a.resolve() and s.repos == [a.resolve(), b.resolve()] and not s.problem
    assert homecfg.remembered() == a.resolve()
    text = homecfg.config_file().read_text()
    assert text.startswith("# mcctl's remembered config repos") and "TOKEN" not in text
    for i in range(homecfg.MAX_REPOS + 3):
        homecfg.remember(tmp_path / f"r{i}")
    assert len(homecfg.load().repos) == homecfg.MAX_REPOS


def test_remembered_needs_a_config_repo_that_still_exists(tmp_path: Path) -> None:
    homecfg.remember(tmp_path / "gone")
    assert homecfg.load().current == (tmp_path / "gone").resolve()
    assert homecfg.remembered() is None


def test_corrupt_file_reads_as_nothing_and_is_replaced(tmp_path: Path) -> None:
    f = homecfg.config_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("current = [unclosed\n")
    s = homecfg.load()
    assert s.current is None and s.repos == [] and str(f) in s.problem
    assert homecfg.remembered() is None
    repo = make_config_repo(tmp_path / "c")
    homecfg.remember(repo)
    assert homecfg.remembered() == repo.resolve()
    f.write_text('current = 5\nrepos = "nope"\n')  # wrong types: ignored
    assert homecfg.load().current is None and homecfg.load().repos == []


def test_xdg_config_home_is_used(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if os.name == "nt":
        monkeypatch.setenv("APPDATA", str(tmp_path / "xdg"))
    else:
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    homecfg.remember(tmp_path)
    assert (tmp_path / "xdg" / "mcctl" / "config.toml").is_file()


# --- -C precedence (cli.run) ------------------------------------------------------------------------------------


@pytest.fixture
def no_ci(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CI", raising=False)


def test_remembered_repo_when_cwd_isnt_one(
    config_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    homecfg.remember(config_repo)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert cli.run(["validate"]) == 0
    out = capsys.readouterr()
    assert f"{config_repo.resolve()}: valid" in out.out
    assert "(remembered; -C DIR for another)" in out.err


def test_cwd_config_repo_beats_the_remembered_one(
    config_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homecfg.remember(make_config_repo(tmp_path / "other"))
    seen = []
    monkeypatch.setattr(cli, "cmd_validate", lambda args: seen.append(args) or 0)
    monkeypatch.chdir(config_repo)
    assert cli.run(["validate"]) == 0
    assert seen[0].config_dir is cli.DEFAULT_DIR and seen[0].argv == ["validate"]


def test_explicit_c_wins_and_is_remembered(
    config_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_ci: None
) -> None:
    other = make_config_repo(tmp_path / "other")
    homecfg.remember(other)
    seen = []
    monkeypatch.setattr(cli, "cmd_validate", lambda args: seen.append(args) or 0)
    monkeypatch.chdir(tmp_path)
    assert cli.run(["validate", "-C", str(config_repo)]) == 0
    assert seen[0].config_dir == config_repo
    assert homecfg.load().current == config_repo.resolve()


def test_not_remembered_on_failure_in_ci_or_for_a_non_repo(
    config_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, no_ci: None
) -> None:
    monkeypatch.setattr(cli, "cmd_validate", lambda args: 1)
    assert cli.run(["validate", "-C", str(config_repo)]) == 1
    assert homecfg.load().current is None
    monkeypatch.setattr(cli, "cmd_validate", lambda args: 0)
    assert cli.run(["validate", "-C", str(tmp_path)]) == 0  # not a config repo
    assert homecfg.load().current is None
    monkeypatch.setenv("CI", "true")
    assert cli.run(["validate", "-C", str(config_repo)]) == 0
    assert homecfg.load().current is None


def test_a_reexecuted_command_gets_the_remembered_c(
    config_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    homecfg.remember(config_repo)
    monkeypatch.chdir(tmp_path)
    seen = []
    monkeypatch.setattr(cli, "cmd_lock", lambda args: seen.append(args) or 0)
    monkeypatch.setattr(cli, "cmd_logs", lambda args: seen.append(args) or 0)
    cli.run(["lock", "update", "--dry-run"])
    cli.run(["logs", "-f"])
    root = str(config_repo.resolve())
    assert seen[0].argv == ["lock", "update", "-C", root, "--dry-run"]
    assert seen[1].argv == ["logs", "-C", root, "-f"]
