"""P14: Discord slash commands, the Python side. The command list setup registers is the one the Worker answers;
the provider calls (recorded responses: application, bulk overwrite, endpoint); server.yml discord.* (schema,
cross-field rules, render -> the Worker's plain-text DISCORD binding, Terraform); doctor's checks; `mcctl discord
sync`. The Worker side is watchdog/test/discord.test.mjs (signatures, allowlists, every command)."""

import json
import re

import pytest
import yaml
from conftest import REPO, Replay, make_config_repo

from mcctl import cli, doctor, init, integrations, render, schema, ui
from mcctl.providers import ProviderError, discord

WORKER = (REPO / "watchdog/worker.js").read_text()
APP, GUILD, CHANNEL, USER = (
    "1300000000000000001",
    "1300000000000000002",
    "1300000000000000003",
    "1300000000000000004",
)
KEY = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
DISCORD = {
    "commands": True,
    "application_id": APP,
    "public_key": KEY,
    "guild_id": GUILD,
    "channel_ids": [CHANNEL],
    "user_ids": [USER],
}


def js_list(name: str) -> list[str]:
    m = re.search(rf"^const {name} = \[(.*?)\];", WORKER, re.M | re.S)
    assert m, name
    return re.findall(r'"([^"]+)"', m.group(1))


def test_registered_commands_are_the_ones_the_worker_answers() -> None:
    assert [c["name"] for c in discord.COMMANDS] == js_list("DISCORD_COMMANDS")
    # the actions: exactly the two the owner approved, mapped to the agent's existing actions
    m = re.search(r"^const DISCORD_ACTIONS = \{(.*?)\};", WORKER, re.M)
    assert m and dict(re.findall(r'"([^"]+)": "([^"]+)"', m.group(1))) == {
        "backup now": "backup",
        "whitelist add": "whitelist",
    }
    assert set(js_list("COMMAND_ACTIONS")) >= {"backup", "whitelist"}
    subs = {
        c["name"]: [o["name"] for o in c.get("options", []) if o["type"] == discord.SUB_COMMAND]
        for c in discord.COMMANDS
    }
    assert subs["backup"] == ["now"] and subs["whitelist"] == ["list", "add"]
    for c in discord.COMMANDS:  # Discord's limits (Application Commands docs): names 1-32, descriptions 1-100
        assert re.fullmatch(r"[-_a-z0-9]{1,32}", c["name"]) and 1 <= len(c["description"]) <= 100
        assert c["default_member_permissions"] == "0" and c["type"] == 1
        for o in c.get("options", []):
            assert 1 <= len(o["description"]) <= 100
    assert re.search(r'^const DISCORD_PATH = "/discord/interactions";', WORKER, re.M)
    assert discord.PATH == "/discord/interactions"


def test_provider_calls_and_their_errors() -> None:
    t = Replay("discord_app_ok.json")
    app = discord.application("bot-token-x", t)
    assert (app["id"], app["public_key"], app["name"]) == (APP, KEY, "lh server")
    assert discord.register_commands("bot-token-x", APP, GUILD, t) == sorted(
        c["name"] for c in discord.COMMANDS
    )
    assert discord.set_endpoint(
        "bot-token-x", "https://hb.mc.tests.mcctl.internal/discord/interactions", t
    ).endswith("/discord/interactions")
    put = next(r for r in t.requests if r.method == "PUT")
    assert json.loads(put.body) == discord.COMMANDS
    assert all(r.headers["Authorization"] == "Bot bot-token-x" for r in t.requests)
    assert json.loads(t.requests[-1].body) == {
        "interactions_endpoint_url": "https://hb.mc.tests.mcctl.internal/discord/interactions"
    }
    bad = Replay("discord_app_errors.json")
    with pytest.raises(ProviderError, match=r"HTTP 401 \(the bot token is wrong or was reset"):
        discord.application("bot-token-x", bad)
    with pytest.raises(ProviderError, match="isn't installed in that server yet"):
        discord.register_commands("bot-token-x", APP, GUILD, bad)
    with pytest.raises(ProviderError, match="sends a signed PING first"):
        discord.set_endpoint("bot-token-x", "https://hb.x/discord/interactions", bad)
    check = discord.check_bot("bot-token-x", Replay("discord_app_ok.json"), "1399999999999999999")
    assert check.level == "fail" and "server.yml says" in check.messages[0]
    url = discord.install_url(APP, GUILD)
    assert url.startswith(
        "https://discord.com/oauth2/authorize?client_id=1300000000000000001&scope=applications.commands"
    )
    assert f"guild_id={GUILD}&disable_guild_select=true" in url


def server(tmp_path, **discord_fields):
    data = yaml.safe_load((make_config_repo(tmp_path / "c", renderable=True) / "server.yml").read_text())
    data["discord"] = discord_fields
    return data


def test_schema_and_render(tmp_path) -> None:
    data = server(tmp_path, **DISCORD)
    cfg = schema.load_server(data)
    tf = render.tfvars(cfg, "o/r")["discord_commands"]
    assert tf == {
        "enabled": True,
        "application_id": APP,
        "public_key": KEY,
        "guild_id": GUILD,
        "channel_ids": [CHANNEL],
        "user_ids": [USER],
        "helper_user_ids": [],
        "helper_role_id": "",
        "ephemeral": True,
    }
    off = render.tfvars(schema.load_server(server(tmp_path / "2")), "o/r")["discord_commands"]
    assert off["enabled"] is False and off["user_ids"] == []
    with pytest.raises(schema.SchemaError) as e:
        schema.load_server(server(tmp_path / "3", commands=True, guild_id=GUILD))
    errs = "\n".join(e.value.errors)
    for f in ("application_id", "public_key", "channel_ids", "user_ids"):
        assert f"discord.{f}" in errs
    with pytest.raises(schema.SchemaError, match=r"needs panel\.mode cloudflare"):
        schema.load_server(server(tmp_path / "4", **DISCORD) | {"panel": {"mode": "onserver"}})
    # ids are strings (a bare YAML number would be an int, and lose digits in JSON clients): refused
    with pytest.raises(schema.SchemaError, match=r"discord\.guild_id"):
        schema.load_server(server(tmp_path / "5", **(DISCORD | {"guild_id": int(GUILD)})))
    assert schema.field_errors("discord.public_key", "XYZ")
    schema_json = json.loads((REPO / "schema/server.schema.json").read_text())
    assert "discord" in schema_json["properties"]


def test_helper_tier_schema_and_render(tmp_path) -> None:
    """v1.0.1: discord.helper_user_ids / helper_role_id reach the Worker's DISCORD binding (ids, not secret)."""
    helper, role = "1300000000000000010", "1300000000000000020"
    cfg = schema.load_server(server(tmp_path, **DISCORD, helper_user_ids=[helper], helper_role_id=role))
    tf = render.tfvars(cfg, "o/r")["discord_commands"]
    assert (tf["helper_user_ids"], tf["helper_role_id"], tf["user_ids"]) == ([helper], role, [USER])
    for k, bad in enumerate(
        ({"helper_role_id": int(role)}, {"helper_role_id": "admins"}, {"helper_user_ids": ["12"]})
    ):
        with pytest.raises(schema.SchemaError, match=r"discord\.helper_"):
            schema.load_server(server(tmp_path / str(k), **(DISCORD | bad)))
    assert schema.field_errors("discord.helper_role_id", "nope")
    assert not schema.field_errors("discord.helper_role_id", role)
    props = json.loads((REPO / "schema/server.schema.json").read_text())["properties"]["discord"][
        "properties"
    ]
    assert {"helper_user_ids", "helper_role_id"} <= set(props)
    panel = (REPO / "terraform/panel.tf").read_text()
    assert "helper_role_id  = var.discord_commands.helper_role_id" in panel
    worker = re.search(r"^const DISCORD_HELPER_COMMANDS = \[(.*?)\];", WORKER, re.M)
    assert worker and "backup now" not in worker.group(1) and '"whitelist add"' in worker.group(1)


def test_apply_inputs_carry_the_worker_binding(tmp_path) -> None:
    """M1: setup's apply step re-runs when the slash commands are turned on (or an id changes) after it ran:
    {} while they're off (the hash every earlier state.json has), the DISCORD binding when they're on. GitHub's
    immutable OIDC subjects add `ci_oidc_subject` (the CI identity's subject changes with it)."""
    root = make_config_repo(tmp_path / "config", renderable=True)

    def inputs() -> dict:
        ctx = init.Ctx(root=root, ui=ui.UI(answers={}), deps=init.Deps(), opts=init.Options())
        return integrations.apply_inputs(ctx)

    assert inputs() == {"ci_oidc_subject": "immutable"}
    assert next(s for s in init.STEPS if s.id == "apply").inputs is init._apply_inputs
    data = yaml.safe_load((root / "server.yml").read_text())
    data["discord"] = DISCORD
    (root / "server.yml").write_text(yaml.safe_dump(data))
    on = inputs()
    assert on == {
        "ci_oidc_subject": "immutable",
        "discord": render.discord_commands(schema.load_server(data)),
    }
    assert on["discord"]["enabled"]
    data["discord"]["user_ids"] = [USER, "1300000000000000009"]
    (root / "server.yml").write_text(yaml.safe_dump(data))
    assert inputs() != on
    data["github"] = {**data.get("github", {}), "oidc_subject": "legacy"}
    data.pop("discord")
    (root / "server.yml").write_text(yaml.safe_dump(data))
    assert inputs() == {}  # name-only subjects: the hash every setup before the immutable format recorded


def test_terraform_binds_discord_config_without_secrets() -> None:
    panel = (REPO / "terraform/panel.tf").read_text()
    variables = (REPO / "terraform/variables.tf").read_text()
    assert 'variable "discord_commands"' in variables
    assert '{ type = "plain_text", name = "DISCORD", text = jsonencode(local.discord_config) }' in panel
    assert "discord_commands = local.cloudflare_mode && var.discord_commands.enabled" in panel
    for tf_file in (REPO / "terraform").glob("*.tf"):
        assert "DISCORD_BOT_TOKEN" not in tf_file.read_text() and "bot_token" not in tf_file.read_text()


def test_doctor_checks_the_bot_token_and_the_endpoint(tmp_path, monkeypatch) -> None:
    from test_mcctl_doctor import ALL_OK, SECRETS, deps, levels, write_env

    for k in [*SECRETS, "TAILSCALE_API_KEY", "TF_VAR_discord_webhook_url", "GH_TOKEN", "DISCORD_BOT_TOKEN"]:
        monkeypatch.delenv(k, raising=False)
    root = make_config_repo(tmp_path / "config", renderable=True)
    data = yaml.safe_load((root / "server.yml").read_text())
    data["discord"] = DISCORD
    (root / "server.yml").write_text(yaml.safe_dump(data))
    write_env(root)
    got = levels(doctor.run(root, deps=deps()))
    assert got["discord commands"] == "warn"  # no bot token: the commands work, sync can't run
    write_env(root, SECRETS | {"DISCORD_BOT_TOKEN": "bot-token-x"})
    report = doctor.run(root, deps=deps(*ALL_OK, "discord_app_ok.json"))
    got = levels(report)
    assert got["discord bot token"] == "ok" and got["discord endpoint"] == "ok", report.format()
    assert "bot-token-x" not in report.format()
    data["dns"]["hostname"] = "other"
    (root / "server.yml").write_text(yaml.safe_dump(data))
    report = doctor.run(root, deps=deps(*ALL_OK, "discord_app_ok.json"))
    assert levels(report)["discord endpoint"] == "fail" and "mcctl discord sync" in report.format()


def test_discord_sync_command(tmp_path, monkeypatch) -> None:
    root = make_config_repo(tmp_path / "config", renderable=True)
    data = yaml.safe_load((root / "server.yml").read_text())
    data["discord"] = DISCORD
    (root / "server.yml").write_text(yaml.safe_dump(data))
    (root / ".env").write_text("DISCORD_BOT_TOKEN=bot-token-x\n")
    (root / ".env").chmod(0o600)
    transport = Replay("discord_app_ok.json")
    ctx = init.Ctx(
        root=root,
        ui=ui.UI(answers={}),
        deps=init.Deps(transport=transport, credentials_env={}),
        opts=init.Options(),
    )
    out = integrations.sync(ctx)
    assert out == {
        "enabled": True,
        "endpoint": "https://hb.mc.tests.mcctl.internal/discord/interactions",
        "commands": sorted(c["name"] for c in discord.COMMANDS),
    }
    # a token of another application, or a changed public key: refused (re-run setup's Discord step)
    data["discord"]["public_key"] = "0" * 64
    (root / "server.yml").write_text(yaml.safe_dump(data))
    with pytest.raises(init.StepError, match=r"doesn't match server\.yml"):
        integrations.sync(ctx)
    # the CLI wiring
    args = cli.build_parser().parse_args(["discord", "sync", "-C", str(root), "--endpoint-only"])
    assert args.func is cli.cmd_discord and args.endpoint_only
    args = cli.build_parser().parse_args(["secrets", "dashlane", "-C", str(root), "--convert"])
    assert args.func is cli.cmd_secrets and args.convert
