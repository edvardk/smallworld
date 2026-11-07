"""v1.3 phase 2: the panel's actions from the CLI over Tailscale SSH (mcctl/actions.py, the `restart`,
`console`, `worlds`, `plugins` and `players kick|ban|pardon` commands).

No SSH, no network: the ssh runners are fakes that record the command line and answer like
`scripts/mc_agent.py cli` would (tests/test_agent_cli.py tests that side). Pure logic, every OS.
"""

from __future__ import annotations

import io
import json
import shlex
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest

from mcctl import actions, cli, configrepo, ui

WORLDS = {
    "active": "main",
    "since": "2026-11-01T10:00:00Z",
    "data_version": 4671,
    "archives": [{"name": "creative", "short_id": "cccccccc", "time": "2026-11-02T10:00:00Z", "size": 5 << 20,
                  "data_version": 4671, "count": 1}],
    "library": [{"name": "main", "type": "default", "preset": "survival_smp", "seed": ""},
                {"name": "creative", "type": "flat_classic", "preset": "creative_build", "seed": "7"}],
    "deleted": ["gone"],
    "archive_bytes": 5 << 20,
    "storage_note": False,
    "pending": None,
}  # fmt: skip
PLUGINS = [
    {"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1.4.40", "enabled": True},
    {"name": "squaremap", "filename": "squaremap-1.3.jar", "version": "1.3", "enabled": False},
]


class FakeServer:
    """Records every remote command; answers `worlds`/`plugins` lists and streams action results."""

    def __init__(self, result: str = "ok: done", code: int = 0) -> None:
        self.sent: list[str] = []  # the remote command text (after `--`)
        self.result, self.code = result, code
        self.lines = [
            "countdown: countdown 60 s, 2 player(s) online",
            "running: restarting minecraft.service",
        ]

    def remote(self, argv: Sequence[str]) -> list[str]:
        assert argv[0] == "ssh" and argv[-2] == "--" and "BatchMode=yes" in argv
        self.sent.append(argv[-1])
        return shlex.split(argv[-1])

    def capture(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        words = self.remote(argv)
        data = {"worlds": WORLDS, "plugins": PLUGINS}[words[-1]]
        return subprocess.CompletedProcess(list(argv), 0, json.dumps(data) + "\n", "")

    def stream(self, argv: Sequence[str], on_line: Callable[[str], None]) -> int:
        words = self.remote(argv)
        if words[:1] == ["mc"]:
            on_line(f"ran {' '.join(words[1:])}")
            return 0
        for line in self.lines:
            on_line(line)
        on_line(f"RESULT {self.result}")
        return self.code

    def actions(self) -> list[tuple[str, str, dict[str, Any]]]:
        """(login, action, args) of each mc_agent.py cli action that was sent (lists excluded)."""
        out = []
        for text in self.sent:
            words = shlex.split(text)
            if "cli" in words and len(words) == words.index("cli") + 4:
                i = words.index("cli")
                out.append((words[i + 1], words[i + 2], json.loads(words[i + 3])))
        return out


@pytest.fixture
def server() -> FakeServer:
    return FakeServer()


@pytest.fixture
def rem(config_repo: Path, server: FakeServer) -> tuple[actions.Remote, io.StringIO]:
    buf = io.StringIO()
    r = actions.Remote(
        configrepo.load(config_repo).server,
        out=ui.Out(buf, rich=False),
        capture=server.capture,
        stream=server.stream,
        ask=lambda prompt: pytest.fail(f"asked: {prompt}"),
        login="mcctl:sam",
    )
    return r, buf


def run(rem: actions.Remote, fn: Callable[[], int]) -> int:
    return actions.guarded(rem, fn)


# --- the server-side entry point -------------------------------------------------------------------------------


def test_the_command_runs_mc_agent_cli_as_the_ops_user() -> None:
    text = actions.agent_command("mcctl:sam", "restart", {"countdown": 60, "backup": False})
    assert shlex.split(text) == [
        "sudo", "/usr/local/bin/mc-run", "-/etc/minecraft/notify.env", "/opt/minecraft/bin/mc_agent.py", "cli",
        "mcctl:sam", "restart", '{"backup":false,"countdown":60}',
    ]  # fmt: skip
    assert "systemctl" not in text  # never a generic systemctl: the agent's fixed, polkit-allowed units
    assert shlex.split(actions.agent_command("mcctl:sam", "worlds"))[-2:] == ["mcctl:sam", "worlds"]


@pytest.mark.parametrize(
    ("user", "login"),
    [("sam", "mcctl:sam"), ("DOMAIN\\Sam Smith", "mcctl:DOMAIN_Sam_Smith"), ("", "mcctl:unknown"),
     ("é", "mcctl:unknown"), ("a" * 80, "mcctl:" + "a" * 64)],
)  # fmt: skip
def test_cli_login_is_safe_for_the_audit(user: str, login: str) -> None:
    assert actions.cli_login(user) == login


def test_confirm_phrases_match_the_panel() -> None:
    assert actions.confirm_phrase("world_create", "farm") == "create farm"
    assert actions.confirm_phrase("world_switch", "farm") == "switch to farm"
    assert actions.confirm_phrase("world_delete", "farm") == "delete farm"
    assert actions.confirm_phrase("plugin", "chunky", True) == "enable chunky"
    assert actions.confirm_phrase("plugin", "chunky", False) == "disable chunky"


def test_non_terminal_confirmation_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("switch to x\n"))
    assert actions.interactive_ask("Type it: ") == ""


# --- restart -----------------------------------------------------------------------------------------------------


def test_restart_streams_the_countdown_and_reports(rem, server: FakeServer) -> None:
    r, buf = rem
    assert run(r, lambda: actions.restart(r, 60, backup=True)) == 0
    assert server.actions() == [
        ("mcctl:sam", "restart", {"countdown": 60, "backup": True, "skip_if_empty": True})
    ]
    text = buf.getvalue()
    assert "countdown 60 s, 2 player(s) online" in text and "restarting minecraft.service" in text
    assert "done" in text


def test_restart_wait_counts_down_even_when_empty(rem, server: FakeServer) -> None:
    r, _ = rem
    assert run(r, lambda: actions.restart(r, 10, wait=True)) == 0
    assert server.actions()[0][2]["skip_if_empty"] is False


def test_restart_refuses_a_countdown_the_panel_doesnt_offer(rem, server: FakeServer) -> None:
    r, buf = rem
    assert run(r, lambda: actions.restart(r, 7)) == 2
    assert server.sent == [] and "--countdown must be one of 0, 10, 60, 300" in buf.getvalue()


@pytest.mark.parametrize(
    ("result", "code"), [("refused: restart: the server is admin-stopped", 1), ("ok: x", 255)]
)
def test_a_refusal_or_ssh_failure_is_exit_1(config_repo: Path, result: str, code: int) -> None:
    srv = FakeServer(result, code)
    buf = io.StringIO()
    r = actions.Remote(configrepo.load(config_repo).server, out=ui.Out(buf, rich=False), capture=srv.capture,
                       stream=srv.stream, login="mcctl:sam")  # fmt: skip
    assert actions.restart(r) == 1
    assert ("admin-stopped" in buf.getvalue()) == (code == 1)


def test_no_result_line_is_a_failure(rem, server: FakeServer) -> None:
    r, buf = rem
    r.stream = lambda argv, on_line: 255
    assert actions.restart(r) == 1
    assert "no result from the server (ssh exit 255)" in buf.getvalue()


# --- console -----------------------------------------------------------------------------------------------------


def test_console_one_shot_is_rcon(rem, server: FakeServer) -> None:
    r, buf = rem
    assert actions.console(r, ["whitelist", "add", "Em_K"]) == 0
    assert server.sent == ["mc whitelist add Em_K"] and "ran whitelist add Em_K" in buf.getvalue()


def test_console_prompt_sends_each_line_until_exit(rem, server: FakeServer) -> None:
    r, _ = rem
    lines = iter(["list", "", "/say hello there", "exit", "never"])
    assert actions.console(r, [], read=lambda prompt: next(lines)) == 0
    assert server.sent == ["mc list", "mc 'say hello there'"]
    eof = iter(["tps"])

    def read(prompt: str) -> str:
        try:
            return next(eof)
        except StopIteration:
            raise EOFError from None

    assert actions.console(r, [], read=read) == 0 and server.sent[-1] == "mc tps"


# --- worlds ------------------------------------------------------------------------------------------------------


def test_worlds_list(rem, server: FakeServer) -> None:
    r, buf = rem
    assert run(r, lambda: actions.worlds_list(r)) == 0
    text = buf.getvalue()
    assert "main" in text and "creative" in text and "5.0 MB" in text and "flat_classic" in text
    assert "recently deleted" in text and "gone" in text
    assert server.actions() == []  # read-only


def test_world_switch_with_the_typed_confirmation(rem, server: FakeServer) -> None:
    r, _ = rem
    assert (
        run(r, lambda: actions.world_switch(r, "creative", countdown=10, confirm="switch to creative")) == 0
    )
    assert server.actions() == [
        ("mcctl:sam", "world_switch", {"name": "creative", "countdown": 10, "confirm": "switch to creative"})
    ]


def test_world_switch_asks_when_no_confirm_is_given(rem, server: FakeServer) -> None:
    r, _ = rem
    asked: list[str] = []
    r.ask = lambda prompt: asked.append(prompt) or "switch to creative"
    assert run(r, lambda: actions.world_switch(r, "creative")) == 0
    assert asked == ["Type `switch to creative` to switch: "] and len(server.actions()) == 1


@pytest.mark.parametrize("typed", ["switch creative", "", "SWITCH TO CREATIVE"])
def test_a_wrong_confirmation_sends_nothing(rem, server: FakeServer, typed: str) -> None:
    r, buf = rem
    assert run(r, lambda: actions.world_switch(r, "creative", confirm=typed)) == 2
    assert server.actions() == [] and 'type exactly "switch to creative"' in buf.getvalue()


@pytest.mark.parametrize(
    ("fn", "why"),
    [
        (lambda r: actions.world_switch(r, "nope", confirm="switch to nope"), "unknown world nope"),
        (lambda r: actions.world_switch(r, "main", confirm="switch to main"), "already the active world"),
        (lambda r: actions.world_switch(r, "Bad Name", confirm="x"), "a world name is"),
        (lambda r: actions.world_delete(r, "main", confirm="delete main"), "is the active world"),
        (lambda r: actions.world_delete(r, "nope", confirm="delete nope"), "unknown world nope"),
        (lambda r: actions.world_create(r, "creative", confirm="create creative"), "already exists"),
        (lambda r: actions.world_create(r, "gone", confirm="create gone"), "deleted recently"),
        (lambda r: actions.world_create(r, "farm", seed="abc", confirm="create farm"), "seed"),
        (lambda r: actions.world_create(r, "farm", wtype="debug", confirm="create farm"), "--type"),
        (lambda r: actions.world_create(r, "farm", countdown=5, confirm="create farm"), "--countdown"),
    ],
)
def test_world_refusals(rem, server: FakeServer, fn: Callable[[actions.Remote], int], why: str) -> None:
    r, buf = rem
    assert run(r, lambda: fn(r)) == 2
    assert server.actions() == [] and why in buf.getvalue()


def test_world_create_and_delete(rem, server: FakeServer) -> None:
    r, _ = rem
    assert run(r, lambda: actions.world_create(r, "farm", seed="-5", wtype="flat_void", preset="farm_test",
                                               pregen=False, countdown=0, confirm="create farm")) == 0  # fmt: skip
    assert run(r, lambda: actions.world_delete(r, "creative", confirm="delete creative")) == 0
    assert server.actions() == [
        ("mcctl:sam", "world_create", {"name": "farm", "seed": "-5", "type": "flat_void", "preset": "farm_test",
                                       "pregen": False, "countdown": 0, "confirm": "create farm"}),
        ("mcctl:sam", "world_delete", {"name": "creative", "confirm": "delete creative"}),
    ]  # fmt: skip


def test_world_types_and_presets_follow_the_schema() -> None:
    assert "flat_void" in actions.WORLD_TYPES and "survival_smp" in actions.WORLD_PRESETS


# --- plugins -----------------------------------------------------------------------------------------------------


def test_plugins_list(rem) -> None:
    r, buf = rem
    assert run(r, lambda: actions.plugins_list(r)) == 0
    assert "chunky" in buf.getvalue() and "disabled" in buf.getvalue()


def test_plugin_disable_and_enable(rem, server: FakeServer) -> None:
    r, _ = rem
    assert run(r, lambda: actions.plugin_toggle(r, "chunky", False, confirm="disable chunky")) == 0
    assert (
        run(r, lambda: actions.plugin_toggle(r, "squaremap", True, countdown=0, confirm="enable squaremap"))
        == 0
    )
    assert server.actions() == [
        ("mcctl:sam", "plugin", {"name": "chunky", "enabled": False, "countdown": 60, "confirm": "disable chunky"}),
        ("mcctl:sam", "plugin", {"name": "squaremap", "enabled": True, "countdown": 0, "confirm": "enable squaremap"}),
    ]  # fmt: skip


@pytest.mark.parametrize(
    ("name", "enabled", "confirm", "why"),
    [("worldedit", False, "disable worldedit", "isn't in the deployed plugins.lock.yml"),
     ("chunky", True, "enable chunky", "already enabled"),
     ("chunky", False, "disable", 'type exactly "disable chunky"'),
     ("../x", False, "disable ../x", "not a plugin name")],
)  # fmt: skip
def test_plugin_refusals(rem, server: FakeServer, name: str, enabled: bool, confirm: str, why: str) -> None:
    r, buf = rem
    assert run(r, lambda: actions.plugin_toggle(r, name, enabled, confirm=confirm)) == 2
    assert server.actions() == [] and why in buf.getvalue()


# --- players: live moderation ---------------------------------------------------------------------------------


def test_kick_ban_pardon(rem, server: FakeServer) -> None:
    r, _ = rem
    assert actions.moderate(r, "kick", "jonas_r", "brb\nnow") == 0
    assert actions.moderate(r, "ban", "griefer_99") == 0
    assert actions.moderate(r, "pardon", "griefer_99", "ignored") == 0
    assert [a[1:] for a in server.actions()] == [
        ("moderate", {"verb": "kick", "target": "jonas_r", "reason": "brb now"}),
        ("moderate", {"verb": "ban", "target": "griefer_99"}),
        ("moderate", {"verb": "pardon", "target": "griefer_99"}),
    ]


@pytest.mark.parametrize(("verb", "target"), [("kick", "@a"), ("op", "Em_K"), ("ban", "x")])
def test_moderation_refusals(rem, server: FakeServer, verb: str, target: str) -> None:
    r, _ = rem
    assert run(r, lambda: actions.moderate(r, verb, target)) == 2
    assert server.sent == []


# --- the commands (argparse) ----------------------------------------------------------------------------------


@pytest.fixture
def wired(config_repo: Path, server: FakeServer, monkeypatch: pytest.MonkeyPatch) -> Callable[..., int]:
    monkeypatch.delenv("CI", raising=False)

    def remote(args: object) -> actions.Remote:
        return actions.Remote(configrepo.load(config_repo).server, out=ui.Out(io.StringIO(), rich=False),
                              capture=server.capture, stream=server.stream, ask=lambda p: "",
                              login="mcctl:sam")  # fmt: skip

    monkeypatch.setattr(cli, "_remote", remote)
    return lambda *argv: cli.run([*argv, "-C", str(config_repo)])


def test_commands_parse_and_reach_the_server(
    wired: Callable[..., int], server: FakeServer, config_repo: Path
) -> None:
    assert wired("restart", "--countdown", "300", "--backup") == 0
    assert wired("worlds", "switch", "creative", "--countdown", "0", "--confirm", "switch to creative") == 0
    assert (
        wired("worlds", "create", "farm", "--type", "amplified", "--no-pregen", "--confirm", "create farm")
        == 0
    )
    assert wired("worlds", "delete", "creative", "--confirm", "delete creative") == 0
    assert wired("worlds", "list") == 0
    assert wired("plugins", "disable", "chunky", "--confirm", "disable chunky") == 0
    assert wired("plugins", "list") == 0
    assert wired("players", "ban", "griefer_99", "--reason", "griefing") == 0
    assert cli.run(["console", "-C", str(config_repo), "list"]) == 0  # its words are the server's (like rcon)
    sent = [
        (a, args.get("name") or args.get("target") or args.get("countdown"))
        for _, a, args in server.actions()
    ]
    assert sent == [("restart", 300), ("world_switch", "creative"), ("world_create", "farm"),
                    ("world_delete", "creative"), ("plugin", "chunky"), ("moderate", "griefer_99")]  # fmt: skip
    assert server.actions()[2][2]["pregen"] is False and server.sent[-1] == "mc list"


def test_without_a_confirmation_off_a_terminal_the_command_refuses(
    wired: Callable[..., int], server: FakeServer
) -> None:
    assert wired("worlds", "switch", "creative") == 2
    assert wired("plugins", "enable", "squaremap") == 2
    assert server.actions() == []


def test_players_kick_is_live_only_never_players_yml(
    wired: Callable[..., int], server: FakeServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcctl import players

    monkeypatch.setattr(players, "change", lambda *a, **k: pytest.fail("edited players.yml"))
    assert wired("players", "kick", "jonas_r") == 0
    assert wired("players", "kick") == 2  # a name is required
    assert [a[1] for a in server.actions()] == ["moderate"]


def test_console_words_are_verbatim() -> None:
    assert cli.strip_plain(["console", "say", "--plain"]) == (["console", "say", "--plain"], False)
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["restart", "--countdown", "soon"])
