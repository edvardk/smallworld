"""A resumed `mcctl setup` never asks again what it already has an answer for (a live run, 2026-11-02: the
always-run Discord step re-asked "slash commands too?" on every resume). Answers are kept in server.yml
(`discord.commands`, host.*) or in the step's recorded outputs; `--reconfigure STEP` asks again. Also the host
step's IPv6 (detected over SSH, asked only when the host has none) and the passphrase/ssh-agent diagnosis."""

import io
import os
import stat
from pathlib import Path

import pytest
import yaml
from conftest import REPO, TEST_SHA

from mcctl import cli, configrepo, init, integrations, mock, ui, yamledit

ANSWERS = yaml.safe_load((REPO / "tests/e2e/answers.yml").read_text())


class Recording(dict):
    """An answers file that records every question key the wizard looks up."""

    def __init__(self, *args: object) -> None:
        super().__init__(*args)
        self.asked: list[str] = []

    def __contains__(self, key: object) -> bool:
        self.asked.append(str(key))
        return super().__contains__(key)


def run_wizard(root: Path, answers: dict, *, deps=None, reconfigure: tuple[str, ...] = ()):
    rec = Recording(answers)
    out = io.StringIO()
    talk = ui.UI(answers=rec, mock=True, env={}, out=out)
    deps = deps or mock.deps(root)
    opts = init.Options(mock=True, product_sha=TEST_SHA, reconfigure=reconfigure)
    return init.run(root, talk, deps, opts), out.getvalue(), rec.asked, deps


def test_a_resumed_setup_asks_nothing(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, asked, deps = run_wizard(root, ANSWERS)
    assert rc == 0, out
    assert "discord_commands" in asked and "host_ipv6" in asked
    rc, out, asked, _ = run_wizard(root, ANSWERS, deps=deps)
    assert rc == 0, out
    assert asked == [], f"a resume asked {asked}"
    assert "slash commands set up for server" in out


def test_the_slash_commands_no_is_remembered_in_server_yml(tmp_path) -> None:
    root = tmp_path / "c"
    no = {k: v for k, v in ANSWERS.items() if not k.startswith("discord_")} | {"discord_commands": False}
    rc, out, asked, deps = run_wizard(root, no)
    assert rc == 0, out
    assert yamledit.get_value((root / "server.yml").read_text(), "discord.commands") is False
    rc, out, asked, deps = run_wizard(root, no, deps=deps)
    assert rc == 0, out
    assert "discord_commands" not in asked and asked == []
    assert "--reconfigure discord" in out
    # --reconfigure discord: asked again, and set up
    rc, out, asked, deps = run_wizard(root, ANSWERS, deps=deps, reconfigure=("discord",))
    assert rc == 0, out
    assert "discord_commands" in asked and "discord_guild_id" in asked
    assert configrepo.load(root).server.discord.commands is True
    # an unknown step is refused before anything runs
    rc, out, _, _ = run_wizard(root, ANSWERS, deps=deps, reconfigure=("nope",))
    assert rc == 2 and "--reconfigure nope" in out


def test_an_older_state_json_answer_counts(tmp_path) -> None:
    """A state.json from before server.yml kept the "no": the discord step's outputs say commands: false."""
    root = tmp_path / "c"
    no = {k: v for k, v in ANSWERS.items() if not k.startswith("discord_")} | {"discord_commands": False}
    rc, out, _, deps = run_wizard(root, no)
    assert rc == 0, out
    text = (root / "server.yml").read_text()
    data = yaml.safe_load(text)
    data.pop("discord")
    (root / "server.yml").write_text(yaml.safe_dump(data, sort_keys=False))
    ctx = init.Ctx(root=root, ui=ui.UI(answers={}), deps=deps, opts=init.Options())
    assert ctx.outputs("discord")["commands"] is False
    assert integrations.commands_decided(ctx) is False
    assert init.Ctx(
        root=root, ui=ui.UI(answers={}), deps=deps, opts=init.Options(from_step="discord")
    ).reconfigure("discord")
    assert init.Ctx(root=root, ui=ui.UI(answers={}), deps=deps, opts=init.Options(from_step="9")).reconfigure(
        "discord"
    )
    assert not ctx.reconfigure("discord")


def test_reconfigure_is_a_setup_option() -> None:
    args = cli.build_parser().parse_args(["setup", "--reconfigure", "discord", "--reconfigure", "github"])
    assert args.reconfigure == ["discord", "github"]


def test_the_app_installation_answer_is_remembered(tmp_path) -> None:
    """Without openssl init can't check the Apps' installations itself and asks; a resume doesn't."""
    root = tmp_path / "c"
    deps = mock.deps(root)

    def no_openssl(pem: str, data: bytes) -> bytes:
        raise FileNotFoundError("openssl")

    deps.app_sign = no_openssl
    rc, out, asked, deps = run_wizard(root, ANSWERS, deps=deps)
    assert rc == 0, out
    assert "panel_app_installed" in asked and "updater_app_installed" in asked
    installs: list[str] = []
    deps.app_install = lambda slug, say, ask, poll: installs.append(slug)
    rc, out, asked, _ = run_wizard(root, ANSWERS, deps=deps)
    assert rc == 0, out
    assert asked == [] and installs == [] and "you confirmed it on an earlier run" in out


# --- the host step: IPv6 ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("3ffe:ffff:6:1234::1", None),
        ("none", None),
        ("", None),
        ("3ffe:ffff:6:1234::1/64", "without the prefix length"),
        ("3ffe:ffff:6:1234::", "the /64 prefix itself"),
        ("fd00::1", "not a public"),
        ("fe80::1", "not a public"),
        ("9.9.9.9", "not an IPv6 address"),
        ("3ffe:ffff:6:1234", "not an IPv6 address"),
    ],
)
def test_ipv6_answers_are_checked(value, error) -> None:
    got = init.ipv6_answer_error(value)
    assert (got is None) if error is None else (got is not None and error in got)


def test_ipv6_is_asked_only_when_the_host_has_none(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, asked, _ = run_wizard(root, ANSWERS | {"host_ipv6": "3ffe:ffff:6:1234::1"})
    assert rc == 0, out
    assert "without /64" in out and "from its prefix" not in out
    assert configrepo.load(root).server.host.ipv6 == "3ffe:ffff:6:1234::1"
    # detected: never asked
    root = tmp_path / "d"
    deps = mock.deps(root)
    deps.tools.extra_ipv6 = ["3ffe:ffff:6:99::1"]
    rc, out, asked, _ = run_wizard(root, ANSWERS, deps=deps)
    assert rc == 0, out
    assert "host_ipv6" not in asked and "IPv6 3ffe:ffff:6:99::1 detected on the host" in out
    # none: IPv4 only, and a resume doesn't ask again
    root = tmp_path / "e"
    rc, out, asked, deps = run_wizard(root, ANSWERS | {"host_ipv6": "none"})
    assert rc == 0, out
    assert configrepo.load(root).server.host.ipv6 is None
    rc, out, asked, _ = run_wizard(root, ANSWERS, deps=deps)
    assert rc == 0, out
    assert asked == []


# --- the host step: a passphrase-protected key without ssh-agent -----------------------------------------------


def fake_bin(tmp_path: Path, *, agent_lists: str = "") -> Path:
    """ssh-keygen that can't read the key without its passphrase; ssh-add whose agent holds `agent_lists`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    scripts = {
        "ssh-keygen": '#!/bin/sh\ncase "$1" in\n  -y) echo "Load key: incorrect passphrase" >&2; exit 255;;\n'
        '  -lf) echo "256 SHA256:edKeyFingerprint admin@laptop (ED25519)";;\nesac\n',
        "ssh-add": (
            f'#!/bin/sh\necho "{agent_lists}"\n'
            if agent_lists
            else '#!/bin/sh\necho "The agent has no identities."\nexit 1\n'
        ),
    }
    for name, text in scripts.items():
        (bin_dir / name).write_text(text)
        (bin_dir / name).chmod(stat.S_IRWXU)
    return bin_dir


def denied_host(root: Path, tmp_path: Path, monkeypatch, *, agent_lists: str = "", sock: str | None = "/s"):
    """mock deps whose host refuses the key; ssh-keygen -y / ssh-add are the fake binaries (real runner)."""
    monkeypatch.setenv(
        "PATH", f"{fake_bin(tmp_path, agent_lists=agent_lists)}{os.pathsep}{os.environ['PATH']}"
    )
    key = tmp_path / "id_ed25519"
    key.write_text("encrypted\n")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA admin@laptop\n")
    deps = mock.deps(root)
    real, streamed = deps.tools.__call__, []

    def tools(argv):
        if argv[0] == "ssh" and "root@9.9.9.9" in argv:
            return mock._proc(argv, 255, "", "root@9.9.9.9: Permission denied (publickey,password).")
        if argv[0] == "ssh-add" or (argv[0] == "ssh-keygen" and argv[1] in ("-y", "-lf")):
            return init._capture(argv)
        return real(argv)

    deps.run = tools
    deps.stream = lambda argv: (streamed.append(list(argv)), 0)[1]
    deps.ssh_identity = lambda: key
    deps.ssh_auth_sock = lambda: sock
    return deps, streamed


def test_a_passphrase_key_without_the_agent_is_explained(tmp_path, monkeypatch) -> None:
    root = tmp_path / "c"
    deps, streamed = denied_host(root, tmp_path, monkeypatch)
    rc, out, asked, _ = run_wizard(root, ANSWERS, deps=deps)
    assert rc == 1
    assert "has a passphrase and isn't loaded in ssh-agent" in out
    assert 'eval "$(ssh-agent -s)" && ssh-add' in out and str(tmp_path / "id_ed25519") in out
    assert "provider's panel" not in out and "ssh_copy_id" not in asked and streamed == []


def test_no_agent_at_all_is_explained(tmp_path, monkeypatch) -> None:
    root = tmp_path / "c"
    deps, streamed = denied_host(root, tmp_path, monkeypatch, sock=None)
    rc, out, _, _ = run_wizard(root, ANSWERS, deps=deps)
    assert rc == 1 and "no ssh-agent is running (SSH_AUTH_SOCK unset)" in out and streamed == []


def test_a_loaded_key_still_gets_ssh_copy_id(tmp_path, monkeypatch) -> None:
    root = tmp_path / "c"
    deps, streamed = denied_host(
        root, tmp_path, monkeypatch, agent_lists="256 SHA256:edKeyFingerprint admin@laptop (ED25519)"
    )
    rc, out, asked, _ = run_wizard(root, ANSWERS | {"ssh_copy_id": True}, deps=deps)
    assert rc == 1  # the fake host keeps refusing
    assert "ssh_copy_id" in asked and streamed and "provider's panel" in out and "ssh-agent" not in out
