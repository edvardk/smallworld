"""v1.3 phase 1: the home screen (plain `mcctl` on a terminal), the remembered config repo, `-C` precedence.

The menu is driven by simulated keypresses (prompt_toolkit's pipe input, DummyOutput); no SSH, no network:
the status probe and the command functions are replaced.
"""

from __future__ import annotations

import io
import os
import threading
from pathlib import Path

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from mcctl import cli, home, homecfg, remote, ui
from tests.conftest import make_config_repo

UP, DOWN, ENTER, CTRL_C, ESC = "\x1b[A", "\x1b[B", "\r", "\x03", "\x1b"


class Scripted(home.Prompter):
    """Sends one chunk of keys before each prompt (a prompt may drop keys typed ahead of it)."""

    def __init__(self, inp: object, keys: list[str]) -> None:
        super().__init__({"input": inp, "output": DummyOutput()})
        self.inp, self.keys = inp, list(keys)
        self.selects: list[tuple[str, list]] = []

    def select(self, message: str, choices: list, default: str | None = None) -> str | None:
        self.selects.append((message, choices))
        self.inp.send_text(self.keys.pop(0))  # type: ignore[attr-defined]
        return super().select(message, choices, default)

    def text(self, message: str) -> str | None:
        self.inp.send_text(self.keys.pop(0))  # type: ignore[attr-defined]
        return super().text(message)


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


def make_home(root: Path | None, keys: list[str], inp: object, **kw: object) -> tuple[home.Home, io.StringIO]:
    buf = io.StringIO()
    h = home.Home(
        home.Launch(root),
        out=ui.Out(buf, rich=False),
        prompter=Scripted(inp, keys),
        probe=kw.pop("probe", home.StatusProbe(online)),  # type: ignore[arg-type]
        cwd=kw.pop("cwd", Path.cwd()),  # type: ignore[arg-type]
        **kw,  # type: ignore[arg-type]
    )
    return h, buf


# --- when the home screen opens -----------------------------------------------------------------------------


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


def test_interactive_without_questionary(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(home, "HAVE_QUESTIONARY", False)
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
    monkeypatch.setattr(home, "main", lambda launch: pytest.fail("opened the home screen"))
    with pytest.raises(SystemExit) as e:
        cli.main(["--plain"])  # ...but not with --plain
    assert e.value.code == 2


def test_on_a_terminal_bare_mcctl_opens_the_home_screen(monkeypatch: pytest.MonkeyPatch) -> None:
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


# --- the menu -------------------------------------------------------------------------------------------------


def test_header_and_groups(config_repo: Path) -> None:
    with create_pipe_input() as inp:
        h, buf = make_home(config_repo, [UP + ENTER], inp)  # up from Status wraps to Quit
        assert h.loop() == 0
    text = buf.getvalue()
    assert "Deployment" in text and "Config repo" in text and f"{config_repo.resolve()} (-C)" in text
    assert "pinned v0.0.0 · this mcctl" in text
    assert "Server       online · 2/20 players · TPS 19.9, MSPT 12.3" in text
    assert "Last deploy  success (2026-11-07T10:00:00Z)" in text
    assert "Panel PRs    1 open" in text
    _, choices = h.prompter.selects[0]  # type: ignore[attr-defined]
    groups = [c.title.strip("─ ") for c in choices if isinstance(c, home.questionary.Separator)]
    assert groups[:9] == [
        "Server",
        "Players",
        "Worlds",
        "Config",
        "Publish",
        "Health",
        "Updates",
        "Secrets",
        "Metrics",
    ]
    disabled = {c.value: c.disabled for c in choices if getattr(c, "disabled", None)}
    assert disabled["settings"] == "v1.3, coming"
    for key in ("publish", "publish-preview", "publish-revert", "player-add", "player-op"):
        assert key not in disabled, key  # Part B: live
    for key in ("console", "restart", "worlds", "world-switch", "kick", "plugins", "restore", "backup-now"):
        assert key not in disabled, key  # phase 2: live
    assert "status" not in disabled and "quit" not in disabled


def test_status_runs_the_status_command_then_quit(config_repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen = []
    monkeypatch.setattr(cli, "cmd_status", lambda args: seen.append(args) or 0)
    monkeypatch.delenv("CI", raising=False)
    with create_pipe_input() as inp:
        h, buf = make_home(config_repo, [ENTER, UP + ENTER], inp)
        assert h.loop() == 0
    (args,) = seen
    assert args.command == "status" and args.json is False
    assert args.config_dir == config_repo.resolve()
    assert args.argv == ["status", "-C", str(config_repo.resolve())]
    assert "mcctl status: exit 0" in buf.getvalue()
    assert homecfg.load().current == config_repo.resolve()  # an explicit -C that worked is remembered


@pytest.mark.parametrize("key", [CTRL_C, ESC])
def test_ctrl_c_and_esc_quit_with_0(config_repo: Path, key: str) -> None:
    with create_pipe_input() as inp:
        h, _ = make_home(config_repo, [key], inp)
        assert h.loop() == 0


def test_a_failing_command_returns_to_the_menu(config_repo: Path) -> None:
    calls: list[list[str]] = []

    def runner(argv: list[str]) -> int:
        calls.append(argv)
        raise SystemExit(1)

    with create_pipe_input() as inp:
        # Logs (one down from Status), then Quit
        h, buf = make_home(config_repo, [DOWN + ENTER, UP + UP + ENTER], inp, runner=runner)
        assert h.loop() == 0
    assert calls == [["logs", "-C", str(config_repo.resolve())]]
    assert "mcctl logs: exit 1" in buf.getvalue()


def test_config_get_asks_for_the_setting(config_repo: Path) -> None:
    calls: list[list[str]] = []
    with create_pipe_input() as inp:
        h, _ = make_home(config_repo, ["minecraft.motd" + ENTER], inp, runner=lambda a: calls.append(a) or 0)
        h.run_item(home.ITEMS["config-get"])
    assert calls == [["config", "get", "minecraft.motd", "-C", str(config_repo.resolve())]]


def test_without_a_config_repo_items_say_so(tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        h, buf = make_home(None, [], inp, runner=lambda a: pytest.fail("ran"), cwd=tmp_path)
        h.show_header()
        assert h.run_item(home.ITEMS["status"]) is None
    assert "none: “Switch config repo”" in buf.getvalue()
    assert "no config repo yet" in buf.getvalue()


def test_switch_config_repo_by_path(config_repo: Path, tmp_path: Path) -> None:
    other = make_config_repo(tmp_path / "other")
    with create_pipe_input() as inp:
        h, _ = make_home(config_repo, [ENTER, f"{other}{ENTER}"], inp)  # default: "Enter a path…"
        assert h.switch()
    assert h.root == other.resolve() and h.source == "remembered"
    assert homecfg.load().current == other.resolve()
    with create_pipe_input() as inp:  # now it's in the picker; "Back" changes nothing
        h, _ = make_home(config_repo, [DOWN + ENTER], inp)
        assert not h.switch()
    titles = [c.title for c in h.prompter.selects[0][1]]  # type: ignore[attr-defined]
    assert titles[0] == str(other.resolve()) and titles[-1] == "Back"


def test_switch_refuses_a_folder_that_isnt_a_config_repo(config_repo: Path, tmp_path: Path) -> None:
    with create_pipe_input() as inp:
        h, buf = make_home(config_repo, [ENTER, f"{tmp_path}{ENTER}"], inp)
        assert not h.switch()
    assert h.root == config_repo.resolve() and "isn't a config repo" in buf.getvalue()


def test_initial_repo_precedence(config_repo: Path, tmp_path: Path) -> None:
    other = make_config_repo(tmp_path / "other")
    homecfg.remember(other)
    assert home._initial(home.Launch(config_repo), tmp_path) == (config_repo.resolve(), "-C")
    assert home._initial(home.Launch(None), config_repo) == (config_repo.resolve(), "current directory")
    assert home._initial(home.Launch(None), tmp_path) == (other.resolve(), "remembered")


# --- the status header ------------------------------------------------------------------------------------


def test_status_failures_read_unknown(config_repo: Path) -> None:
    def boom(cfg: object) -> remote.Status:
        raise RuntimeError("boom")

    out = ui.Out(io.StringIO(), rich=False)
    rows = dict(home.header_rows(config_repo, "-C", home.StatusProbe(boom), out))
    assert rows["Server"] == "unknown (RuntimeError: boom)"
    down = home.StatusProbe(lambda cfg: remote.Status(reachable=False, error="ssh x: timed out"))
    rows = dict(home.header_rows(config_repo, "-C", down, out))
    assert rows["Server"] == "unknown (ssh x: timed out)" and "Last deploy" in rows


def test_a_broken_config_repo_still_shows_the_header(config_repo: Path) -> None:
    (config_repo / "server.yml").write_text("name: [not, valid\n")
    out = ui.Out(io.StringIO(), rich=False)
    rows = dict(home.header_rows(config_repo, "-C", home.StatusProbe(lambda c: pytest.fail("probed")), out))
    assert rows["Deployment"].startswith("unknown (")
    assert rows["Product"].startswith("pinned v0.0.0")
    assert rows["Server"].startswith("unknown (")


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


# --- phase 2: actions with questions and typed confirmations ------------------------------------------------


def run_keys(config_repo: Path, key: str, keys: list[str]) -> tuple[list[list[str]], str]:
    calls: list[list[str]] = []
    with create_pipe_input() as inp:
        h, buf = make_home(config_repo, keys, inp, runner=lambda a: calls.append(a) or 0)
        h.run_item(home.ITEMS[key])
    return calls, buf.getvalue()


def test_world_switch_asks_name_countdown_then_the_typed_confirmation(config_repo: Path) -> None:
    calls, _ = run_keys(
        config_repo, "world-switch", ["creative" + ENTER, ENTER, "switch to creative" + ENTER]
    )
    root = str(config_repo.resolve())
    assert calls == [
        ["worlds", "switch", "creative", "--countdown", "60", "--confirm", "switch to creative", "-C", root]
    ]


@pytest.mark.parametrize("last", [ESC, ENTER])  # Esc or nothing typed at the confirmation
def test_an_unconfirmed_destructive_item_runs_nothing(config_repo: Path, last: str) -> None:
    calls, text = run_keys(config_repo, "world-delete", ["creative" + ENTER, last])
    assert calls == [] and "not confirmed: nothing was changed" in text


def test_esc_at_a_question_returns_to_the_menu(config_repo: Path) -> None:
    calls, _ = run_keys(config_repo, "world-switch", [ESC])
    assert calls == []


def test_world_create_picks_type_preset_and_countdown(config_repo: Path) -> None:
    keys = ["farm" + ENTER, DOWN + ENTER, ENTER, "-5" + ENTER, DOWN + ENTER, "create farm" + ENTER]
    calls, _ = run_keys(config_repo, "world-create", keys)
    assert calls[0][:-2] == [
        "worlds", "create", "farm", "--type", "large_biomes", "--preset", "survival_smp", "--seed", "-5",
        "--countdown", "10", "--confirm", "create farm",
    ]  # fmt: skip


def test_kick_without_a_reason_and_restart_with_a_countdown(config_repo: Path) -> None:
    root = str(config_repo.resolve())
    calls, text = run_keys(config_repo, "kick", ["jonas_r" + ENTER, ENTER])
    assert calls == [["players", "kick", "jonas_r", "-C", root]] and "$ mcctl players kick jonas_r" in text
    calls, _ = run_keys(config_repo, "ban", ["griefer_99" + ENTER, "griefing again" + ENTER])
    assert calls == [["players", "ban", "griefer_99", "--reason", "griefing again", "-C", root]]
    calls, _ = run_keys(config_repo, "restart", [UP + ENTER])  # 60 is first: up wraps to 0
    assert calls == [["restart", "--countdown", "0", "-C", root]]


def test_plugin_and_restore_items(config_repo: Path) -> None:
    root = str(config_repo.resolve())
    calls, _ = run_keys(config_repo, "plugin-disable", ["chunky" + ENTER, ENTER, "disable chunky" + ENTER])
    assert calls == [
        ["plugins", "disable", "chunky", "--countdown", "60", "--confirm", "disable chunky", "-C", root]
    ]
    calls, _ = run_keys(
        config_repo, "restore", ["abcd1234" + ENTER]
    )  # restore asks its own typed confirmation
    assert calls == [["restore", "--live", "abcd1234", "-C", root]]
    calls, _ = run_keys(config_repo, "console", [])
    assert calls == [["console", "-C", root]]


def test_every_action_item_names_a_real_command() -> None:
    parser = cli.build_parser()
    for _, items in home.MENU:
        for item in items:
            if item.coming or not item.argv:
                continue
            argv = list(item.argv) + ["x" for ask in item.asks if not ask.flag]
            if item.ask:
                argv.append("x")
            args = parser.parse_args(argv)
            assert callable(args.func), item.key
