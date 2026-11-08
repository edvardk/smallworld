"""The dashboard (mcctl/dashboard.py): plain `mcctl` on a terminal, driven with Textual's Pilot.

Async tests run through asyncio.run (no pytest plugin). No SSH, no network, no real child: the status, the
lists, doctor and `spawn` (the child mcctl) are the fakes of tests/dashboard_fakes.py.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import subprocess
import sys
import threading
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from textual.command import CommandPalette
from textual.widgets import DataTable, Input, Static, TabbedContent

from mcctl import cli, dashboard, home, homecfg, publish, remote, restorecmd, ui
from tests import dashboard_fakes as fakes

SIZE = (120, 36)


@pytest.fixture
def repo(config_repo: Path) -> Path:
    fakes.write_players(config_repo)
    return config_repo


def drive(
    root: Path | None,
    body: Callable[[dashboard.Dashboard, Any], Awaitable[None]],
    *,
    spawn: fakes.FakeSpawn | None = None,
    cwd: Path | None = None,
    **kw: Any,
) -> dashboard.Dashboard:
    app = dashboard.Dashboard(
        home.Launch(root), services=fakes.services(spawn, **kw), cwd=cwd or Path.cwd(), refresh_s=3600
    )

    async def main() -> None:
        async with app.run_test(size=SIZE) as pilot:
            await settle(app, pilot)
            await body(app, pilot)

    asyncio.run(main())
    return app


async def settle(app: dashboard.Dashboard, pilot: Any) -> None:
    await pilot.pause(0.05)
    await app.workers.wait_for_complete()
    await pilot.pause(0.05)


def text(widget: Static) -> str:
    """What a Static shows, as plain text (a str, rich Text or a rich table)."""
    from rich.console import Console

    content = widget.content
    if isinstance(content, str):
        return content
    console = Console(width=200, file=io.StringIO(), color_system=None)
    with console.capture() as cap:
        console.print(content)
    return cap.get()


def cells(table: DataTable) -> list[list[str]]:
    return [[str(c) for c in table.get_row_at(i)] for i in range(table.row_count)]


# --- every tab renders with a fake status ----------------------------------------------------------------


def test_every_tab_renders(repo: Path) -> None:
    seen: dict[str, Any] = {}
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        seen["name"] = text(app.query_one("#name", Static))
        seen["badge"] = text(app.query_one("#badge", Static))
        seen["server"] = text(app.query_one("#card-server", dashboard.Card))
        seen["attention"] = text(app.query_one("#card-attention", dashboard.Card))
        seen["versions"] = text(app.query_one("#card-versions", dashboard.Card))
        seen["sub"] = text(app.query_one("#sub", Static))
        for i, (tab, _) in enumerate(dashboard.TABS, 1):
            if tab == "publish":
                await pilot.press("escape")  # out of the console's prompt: digits switch tabs again
            await pilot.press(str(i))
            await settle(app, pilot)
            assert app.query_one(TabbedContent).active == tab
        await pilot.press("h")  # Health: run doctor
        await settle(app, pilot)
        for tid in ("players", "worlds", "plugins", "backups", "health"):
            seen[tid] = cells(app.query_one(f"#{tid}-table", DataTable))
        seen["publish"] = list(spawn.calls)

    drive(repo, body, spawn=spawn)
    assert seen["name"] == "example" and "● ONLINE" in seen["badge"]
    assert "Smallworld v0.0.0" in seen["sub"] and "Purpur 26.3" in seen["sub"]
    assert "2/20" in seen["server"] and "Steve_Builds, alex_mines" in seen["server"]
    assert "panel PR #7" in seen["attention"]
    assert "v0.0.0 (111111111111)" in seen["versions"] and "Health > Run doctor" in seen["versions"]
    players = {row[0]: row for row in seen["players"]}
    assert set(players) == {"Steve_Builds", "alex_mines", "Notch_Jr", "RedstoneRita"}
    assert players["Steve_Builds"][2] == "level 4" and "online" in players["alex_mines"][3]
    assert [r[0] for r in seen["worlds"]] == ["survival", "creative", "skyblock", "amplified"]
    assert [r[2] for r in seen["plugins"]] == ["● enabled", "● enabled", "○ disabled"]
    assert seen["backups"][0][0] == "c3d4e5f6"  # newest first
    assert any(r[1] == "terraform plan" and "fail" in r[0] for r in seen["health"])
    root = str(repo.resolve())
    assert seen["publish"] == [["publish", "--dry-run", "-C", root]]  # the Publish tab previews on open


def test_versions_and_doctor_feed_the_overview(repo: Path) -> None:
    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        app.trigger("doctor")
        await settle(app, pilot)
        versions = text(app.query_one("#card-versions", dashboard.Card))
        assert "Worker" in versions and "v0.0.0" in versions

    drive(repo, body)


def test_the_update_notice_needs_attention(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from mcctl import notice

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        assert "Smallworld v2.0.0 is out" in text(app.query_one("#card-attention", dashboard.Card))

    drive(repo, body, update=lambda pin: notice.for_pin(pin, lambda r: "v2.0.0"))
    made: list[dashboard.Services] = []

    class Fake:
        def __init__(self, launch: home.Launch, *, services: dashboard.Services) -> None:
            made.append(services)

        def run(self) -> int:
            return 0

        def stop_children(self) -> None:
            pass

    monkeypatch.setattr(dashboard, "Dashboard", Fake)
    monkeypatch.setenv(notice.OPT_OUT_ENV, "1")
    assert dashboard.main(home.Launch(None)) == 0 and made[-1].update is None  # turned off
    monkeypatch.delenv(notice.OPT_OUT_ENV)
    assert dashboard.main(home.Launch(None)) == 0 and made[-1].update is notice.for_pin


def test_unreachable_and_failing_status(repo: Path) -> None:
    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        assert "UNREACHABLE" in text(app.query_one("#badge", Static))
        assert "unreachable (ssh x: timed out)" in text(app.query_one("#card-server", dashboard.Card))

    drive(repo, body, status=lambda cfg: remote.Status(reachable=False, error="ssh x: timed out"))

    def boom(cfg: object) -> remote.Status:
        raise RuntimeError("boom")

    async def body2(app: dashboard.Dashboard, pilot: Any) -> None:
        assert "unknown (RuntimeError: boom)" in text(app.query_one("#card-server", dashboard.Card))

    drive(repo, body2, status=boom)


# --- actions stream their output and say how they ended -------------------------------------------------------


@pytest.mark.parametrize("code", [0, 3])
def test_an_action_streams_into_its_panel_and_shows_the_verdict(repo: Path, code: int) -> None:
    spawn = fakes.FakeSpawn(codes={"restart": code})
    out: dict[str, Any] = {}

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("r")  # Overview: Restart…
        await pilot.pause(0.1)
        await pilot.click("#ok")
        await settle(app, pilot)
        panel = app.pane("overview").output
        out["state"] = text(panel.state_widget)
        out["log"] = "\n".join(str(line.text) for line in panel.log_widget.lines)
        out["code"] = panel.code

    drive(repo, body, spawn=spawn)
    assert spawn.calls == [["restart", "--countdown", "60", "-C", str(repo.resolve())]]
    assert (
        "$ mcctl restart --countdown 60" in out["log"] and "countdown 60 s: 2 player(s) warned" in out["log"]
    )
    assert out["code"] == code
    if code == 0:
        assert out["state"].startswith("✓ done: mcctl restart")
    else:
        assert out["state"].startswith("✗ failed (exit 3)") and "something failed" in out["log"]


def test_output_stays_until_cleared_and_a_busy_panel_refuses_a_second_job(repo: Path) -> None:
    spawn = fakes.FakeSpawn(hold={"logs"})

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("6")
        await settle(app, pilot)
        await pilot.press("f")  # follow: runs until stopped
        await pilot.pause(0.2)
        panel = app.pane("logs").output
        assert panel.busy and "running" in text(panel.state_widget)
        assert not app.run_cli(["logs"], panel)  # one job per panel
        await pilot.press("s")  # stop
        await settle(app, pilot)
        assert not panel.busy and spawn.stopped == ["logs"] and panel.stopped
        assert "stopped" in text(panel.state_widget)
        assert any("Saved the game" in str(line.text) for line in panel.log_widget.lines)  # still there
        await pilot.click(panel.query_one(".oclear"))
        assert not panel.log_widget.lines

    drive(repo, body, spawn=spawn)


def test_console_sends_each_line_over_rcon(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("7")
        await settle(app, pilot)
        await pilot.press(*"/say hi there", "enter")
        await settle(app, pilot)
        await pilot.press(*"list", "enter")
        await settle(app, pilot)
        await pilot.press(*"exit", "enter")
        await settle(app, pilot)
        lines = [str(line.text) for line in app.pane("console").output.log_widget.lines]
        assert "$ rcon say hi there" in lines[0] and any("There are 2 of a max" in line for line in lines)

    drive(repo, body, spawn=spawn)
    root = str(repo.resolve())
    assert spawn.calls == [["rcon", "say", "hi", "there", "-C", root], ["rcon", "list", "-C", root]]


def test_alt_digits_switch_tabs_even_from_the_console_input(repo: Path) -> None:
    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("7")
        await settle(app, pilot)
        prompt = app.query_one("#console-input", Input)
        assert app.focused is prompt
        await pilot.press("3")  # a plain digit is typed into the prompt
        assert app.query_one(TabbedContent).active == "console" and prompt.value == "3"
        await pilot.press("alt+3")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "worlds" and prompt.value == "3"
        await pilot.press("alt+1")
        await settle(app, pilot)
        assert app.query_one(TabbedContent).active == "overview"

    drive(repo, body)


def test_player_changes_pass_the_merge_answer(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("2")
        await settle(app, pilot)
        await pilot.press("o")  # op the selected player (alex_mines: online first, sorted)
        await pilot.pause(0.1)
        await pilot.click("#ok")
        await settle(app, pilot)
        await pilot.press("k")
        await pilot.pause(0.1)
        await pilot.press("tab", *"griefing", "enter")  # the name is prefilled; Tab to the reason
        await settle(app, pilot)

    drive(repo, body, spawn=spawn)
    root = str(repo.resolve())
    assert spawn.calls[0] == ["players", "op", "alex_mines", "--level", "4", "--merge", "-C", root]
    assert spawn.calls[1] == ["players", "kick", "alex_mines", "--reason", "griefing", "-C", root]


def test_publish_collects_its_answers_first(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("8")
        await settle(app, pilot)
        await pilot.press("u")
        await pilot.pause(0.1)
        await pilot.click("#ok")
        await settle(app, pilot)

    drive(repo, body, spawn=spawn)
    root = str(repo.resolve())
    assert spawn.calls[-1] == ["publish", "--merge", "--restart", "wait", "-C", root]


# --- destructive actions need the exact typed confirmation ---------------------------------------------------


def test_a_destructive_action_needs_the_exact_typed_confirmation(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("3")
        await settle(app, pilot)
        await pilot.press("down")  # creative (archived)
        await pilot.press("d")  # Delete…
        await pilot.pause(0.1)
        await pilot.click("#ok")  # the name: the selected row
        await pilot.pause(0.1)
        modal = app.screen
        assert isinstance(modal, dashboard.ConfirmModal) and modal.want == "delete creative"
        await pilot.press(*"delete skyblock", "enter")  # wrong: refused, the modal stays
        await pilot.pause(0.1)
        assert app.screen is modal and spawn.calls == []
        assert 'type exactly "delete creative"' in text(modal.query_one(".derror", Static))
        modal.query_one("#typed", Input).value = "  delete creative "
        await pilot.press("enter")
        await settle(app, pilot)

    drive(repo, body, spawn=spawn)
    assert spawn.calls == [
        ["worlds", "delete", "creative", "--confirm", "delete creative", "-C", str(repo.resolve())]
    ]


def test_cancel_or_escape_changes_nothing(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("4")
        await settle(app, pilot)
        await pilot.press("e")  # Enable… (Chunky is selected; any name)
        await pilot.pause(0.1)
        await pilot.click("#ok")
        await pilot.pause(0.1)
        assert isinstance(app.screen, dashboard.ConfirmModal) and app.screen.want == "enable Chunky"
        await pilot.press("escape")
        await pilot.pause(0.1)
        await pilot.press("d")  # Disable…: Esc at the form
        await pilot.pause(0.1)
        await pilot.press("escape")
        await settle(app, pilot)

    drive(repo, body, spawn=spawn)
    assert spawn.calls == []


def test_restore_types_restore_and_the_first_8_hex(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("5")
        await settle(app, pilot)
        await pilot.press("e")
        await pilot.pause(0.1)
        await pilot.click("#ok")
        await pilot.pause(0.1)
        assert app.screen.want == "restore c3d4e5f6"  # type: ignore[attr-defined]
        await pilot.press(*"restore c3d4e5f6", "enter")
        await settle(app, pilot)

    drive(repo, body, spawn=spawn)
    root = str(repo.resolve())
    assert spawn.calls == [["restore", "--live", "c3d4e5f6", "--confirm", "restore c3d4e5f6", "-C", root]]


def test_form_checks_refuse_bad_names_before_anything_runs(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        await pilot.press("3")
        await settle(app, pilot)
        await pilot.press("c")  # Create…
        await pilot.pause(0.1)
        await pilot.press(*"Bad Name", "enter")
        await pilot.pause(0.1)
        assert isinstance(app.screen, dashboard.FormModal)
        assert "lowercase" in text(app.screen.query_one(".derror", Static))

    drive(repo, body, spawn=spawn)
    assert spawn.calls == []


# --- the status refresh never blocks the UI ------------------------------------------------------------------


def test_a_slow_status_never_blocks_the_ui(repo: Path) -> None:
    release, calls = threading.Event(), []

    def slow(cfg: object) -> remote.Status:
        calls.append(1)
        release.wait(10)
        return fakes.sample_status()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        assert "CHECKING" in text(app.query_one("#badge", Static))
        await pilot.press("2")  # the UI answers while the probe hangs
        await pilot.pause(0.05)
        assert app.query_one(TabbedContent).active == "players"
        app.refresh_status()  # a refresh while one runs starts no second probe
        assert calls == [1] and app.probing
        release.set()
        await settle(app, pilot)
        assert "ONLINE" in text(app.query_one("#badge", Static)) and not app.probing

    app = dashboard.Dashboard(home.Launch(repo), services=fakes.services(status=slow), refresh_s=3600)

    async def main() -> None:
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause(0.1)
            await body(app, pilot)

    started = time.monotonic()
    asyncio.run(main())
    assert time.monotonic() - started < 8


def test_sparklines_collect_history(repo: Path) -> None:
    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        app.refresh_status()
        await settle(app, pilot)
        assert list(app.tps) == [19.94, 19.94] and list(app.mspt) == [12.3, 12.3]

    drive(repo, body)


# --- the command palette and the terminal ----------------------------------------------------------------------


def test_the_command_palette_lists_every_action(repo: Path) -> None:
    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        titles = [c.title for c in app.get_system_commands(app.screen)]
        for action in dashboard.ACTIONS:
            assert action.title in titles
        assert "Go to: Worlds" in titles
        await pilot.press("ctrl+p")
        await pilot.pause(0.2)
        assert isinstance(app.screen, CommandPalette)

    drive(repo, body)


def test_every_action_has_a_handler_and_names_real_commands() -> None:
    for action in dashboard.ACTIONS:
        if action.key != "switch-repo":
            assert callable(getattr(dashboard.Dashboard, "do_" + action.key.replace("-", "_"))), action.key
        assert not action.tab or action.tab in dict(dashboard.TABS)
    parser = cli.build_parser()
    for argv in (
        ["publish", "--merge", "--restart", "now"],
        ["players", "op", "x", "--level", "4", "--no-merge"],
        ["restore", "--live", "abcd1234", "--confirm", "restore abcd1234"],
        ["worlds", "switch", "x", "--countdown", "60", "--confirm", "switch to x"],
        ["logs", "--follow", "-n", "200"],
        ["secrets", "rotate", "rcon"],
    ):
        assert callable(parser.parse_args(argv).func), argv


def test_terminal_actions_run_with_the_dashboard_suspended(repo: Path) -> None:
    seen: list[list[str]] = []

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        app.trigger("term-rotate")
        await pilot.pause(0.1)
        await pilot.click("#ok")
        await settle(app, pilot)
        app.trigger("term-ssh")
        await settle(app, pilot)

    drive(repo, body, terminal=lambda argv: seen.append(list(argv)) or 0)
    root = str(repo.resolve())
    assert seen == [["secrets", "rotate", "rcon", "-C", root], ["ssh", "-C", root]]


def test_modal_output_for_commands_without_a_tab(repo: Path) -> None:
    spawn = fakes.FakeSpawn()

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        app.trigger("metrics")
        await settle(app, pilot)
        modal = app.screen
        assert isinstance(modal, dashboard.OutputModal) and modal.panel.code == 0
        await pilot.press("escape")
        await pilot.pause(0.1)
        assert not isinstance(app.screen, dashboard.OutputModal)

    drive(repo, body, spawn=spawn)
    assert spawn.calls == [["metrics", "summary", "-C", str(repo.resolve())]]


# --- config repos ----------------------------------------------------------------------------------------------


def test_without_a_config_repo_actions_say_so_and_switch_opens_one(tmp_path: Path, repo: Path) -> None:
    spawn = fakes.FakeSpawn()
    homecfg.remember(tmp_path / "gone")  # remembered but missing: not offered

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        assert app.root is None and "no config repo" in text(app.query_one("#name", Static))
        app.trigger("restart")
        await settle(app, pilot)
        assert not isinstance(app.screen, dashboard.FormModal)
        await pilot.press("ctrl+o")
        await pilot.pause(0.1)
        assert isinstance(app.screen, dashboard.RepoModal)
        await pilot.press(*str(tmp_path), "enter")  # not a config repo: refused
        await pilot.pause(0.1)
        assert "isn't a config repo" in text(app.screen.query_one(".derror", Static))
        app.screen.query_one("#path", Input).value = str(repo)
        await pilot.press("enter")
        await settle(app, pilot)
        assert app.root == repo.resolve() and app.source == "remembered"
        assert "ONLINE" in text(app.query_one("#badge", Static))

    drive(None, body, spawn=spawn, cwd=tmp_path)
    assert homecfg.load().current == repo.resolve() and spawn.calls == []


def test_a_broken_config_repo_still_opens(repo: Path) -> None:
    (repo / "server.yml").write_text("name: [not, valid\n")

    async def body(app: dashboard.Dashboard, pilot: Any) -> None:
        assert "doesn't load" in text(app.query_one("#sub", Static))
        app.trigger("restart")  # needs the server: refused
        await settle(app, pilot)
        assert not isinstance(app.screen, dashboard.FormModal)

    drive(repo, body, status=lambda cfg: pytest.fail("probed"))


# --- when it opens ---------------------------------------------------------------------------------------------


def test_without_a_terminal_or_with_plain_the_dashboard_never_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(dashboard, "main", lambda launch: pytest.fail("started the dashboard"))
    monkeypatch.setattr(home, "interactive", lambda: False)
    with pytest.raises(SystemExit) as e:
        cli.main([])
    assert e.value.code == 2


def test_home_main_opens_the_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    opened: list[home.Launch] = []
    monkeypatch.setattr(dashboard, "main", lambda launch: opened.append(launch) or 0)
    monkeypatch.setattr(home, "interactive", lambda: True)
    assert cli.main(["-C", "somewhere"]) == 0
    assert opened == [home.Launch(Path("somewhere"))]


# --- running the child mcctl -----------------------------------------------------------------------------------


def test_child_env_is_plain_quiet_and_unbuffered() -> None:
    from mcctl import notice

    env = dashboard.child_env({"PATH": "x"})
    assert env[ui.PLAIN_ENV] == "1" and env[notice.OPT_OUT_ENV] == "1" and env["PYTHONUNBUFFERED"] == "1"
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PATH"] == "x"


def test_spawn_mcctl_streams_a_real_child() -> None:
    lines: list[str] = []
    stops: list[object] = []
    code = dashboard.spawn_mcctl(["--version"], lines.append, stops.append)
    assert code == 0 and lines and lines[-1].startswith("mcctl ") and len(stops) == 1


def test_the_stopper_ends_the_child() -> None:
    extra: dict[str, Any] = (
        {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # type: ignore[attr-defined]
        if sys.platform == "win32"
        else {"start_new_session": True}
    )
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], text=True, **extra)
    dashboard._stopper(proc)()
    assert proc.wait(10) != 0


# --- the non-interactive flags the dashboard passes ------------------------------------------------------------


class TTY(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_publish_merge_and_restart_answer_up_front(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert cli.RESTART_ANSWERS == publish.RESTART_CHOICES
    ctx = cli._ctx(argparse.Namespace(config_dir=tmp_path))
    monkeypatch.setattr("sys.stdin", TTY())
    cli._conversation(ctx, argparse.Namespace(merge=None, restart=None))
    assert ctx.ui.interactive  # a terminal, nothing given: it asks
    cli._conversation(ctx, argparse.Namespace(merge=False, restart="now"))
    assert ctx.ui.answers == {"publish_merge": False, "publish_restart": "now"}
    monkeypatch.setattr("sys.stdin", io.StringIO())
    cli._conversation(ctx, argparse.Namespace(merge=None, restart=None))
    assert ctx.ui.answers == {}  # no terminal: the safe defaults, as before
    args = cli.build_parser().parse_args(["publish", "--no-merge", "--restart", "cancel"])
    assert args.merge is False and args.restart == "cancel"
    assert cli.build_parser().parse_args(["publish"]).merge is None


def test_players_merge_flag_answers_the_merge_question(
    monkeypatch: pytest.MonkeyPatch, config_repo: Path
) -> None:
    from mcctl import players

    seen = []
    monkeypatch.setattr(players, "change", lambda ctx, *a, **kw: seen.append(ctx.ui.answers) or 0)
    assert (
        cli.run(["players", "add", "Somebody", "--uuid", "0" * 8, "--no-merge", "-C", str(config_repo)]) == 0
    )
    assert cli.run(["players", "add", "Somebody", "-C", str(config_repo)]) == 0
    assert seen[0] == {"publish_merge": False}
    assert seen[1] is None or seen[1] == {}  # unchanged: asks on a terminal


def test_restore_confirm_flag(monkeypatch: pytest.MonkeyPatch, config_repo: Path) -> None:
    seen = []
    monkeypatch.setattr(
        restorecmd, "live", lambda ctx, snap, dry_run=False, confirm=None: seen.append(confirm) or 0
    )
    assert (
        cli.run(["restore", "--live", "abcd1234", "--confirm", "restore abcd1234", "-C", str(config_repo)])
        == 0
    )
    assert seen == ["restore abcd1234"]


def test_the_real_lists_go_through_the_quiet_runner(
    monkeypatch: pytest.MonkeyPatch, config_repo: Path
) -> None:
    from mcctl import configrepo

    cfg = configrepo.load(config_repo).server
    seen: list[list[str]] = []

    def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
        seen.append(list(argv))
        if "mc_agent.py" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, '[{"name": "Chunky", "enabled": true}]\n', "")
        return subprocess.CompletedProcess(argv, 255, "", "ssh: timed out")

    monkeypatch.setattr(home, "quiet_run", run)
    assert dashboard.query_server(cfg, "plugins") == [{"name": "Chunky", "enabled": True}]
    with pytest.raises(RuntimeError, match="ssh: timed out"):
        dashboard.list_snapshots(cfg)
    assert all(a[0] == "ssh" for a in seen) and "BatchMode=yes" in seen[0]
