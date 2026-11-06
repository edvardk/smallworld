"""P14 integrations in `mcctl setup` and their own commands: Dashlane as the credential store
(mcctl/dashlane.py) and Discord slash commands (mcctl/providers/discord.py, Worker: /discord/interactions).

Dashlane: the CLI can only read the account's data, so mcctl never writes an item. When the person chooses
Dashlane (setup's preflight, only if `dcli` is installed and set up), each credential setup would write to
.env is shown as a to-do ("add a Password item titled ... with this value"); after `dcli sync` mcctl reads the
reference back and writes the *reference* to .env only if it returns exactly the value it was given (else the
value, with a warning). The restic password (Terraform state only) is shown once for a Secure Note, read back
the same way, and its reference kept in .env as MCCTL_RESTIC_REF (checked by doctor, never resolved into a
child's environment). `mcctl secrets dashlane` lists the items for an existing .env and, with --convert, swaps
verified values for references.

Discord: the person creates the application in the developer portal (no API for that) and pastes its bot token
(kept in .env only); setup reads the application id and public key from Discord, asks for the
guild/channel/user ids, writes them to server.yml, links the install page, registers the guild commands; after
`terraform apply` (the Worker has the DISCORD binding) step discord-endpoint points the application's
interactions endpoint at the Worker. `mcctl discord sync` repeats registration + endpoint later.
"""

from __future__ import annotations

import re
import sys
from typing import Any, TextIO

from mcctl import configrepo, dashlane, envfile, render, schema, tf, yamledit
from mcctl.init import Ctx, StepError, _guarded
from mcctl.providers import discord

ITEM_SAVED = "dashlane_item_saved"


# --- Dashlane ---------------------------------------------------------------------------------------------


def dashlane_store(ctx: Ctx) -> bool:
    """Did the person choose Dashlane for credentials? (preflight: this run's answer, else the state)"""
    if ctx.dashlane_store is not None:
        return ctx.dashlane_store
    return bool(ctx.outputs("preflight").get("dashlane_store"))


def preflight(ctx: Ctx) -> dict[str, Any]:
    """Offer Dashlane when dcli is installed and logged in; the choice is kept (asked once)."""
    login = ctx.deps.dashlane.logged_in()
    if login is None:
        return {}
    ctx.ui.ok(f"dcli (Dashlane, {login})")
    prev = ctx.outputs("preflight").get("dashlane_store")
    if isinstance(prev, bool):
        choice = prev
    else:
        choice = ctx.ui.confirm(
            "dashlane_store",
            "Keep the credentials setup asks for in Dashlane (you add each item; .env keeps only dl:// "
            "references)?",
            default=True,
        )
    ctx.dashlane_store = choice  # this run (the state records it after the step)
    return {"dashlane_store": choice}


def _read_back(ctx: Ctx, ref: str, value: str) -> bool:
    resolver = ctx.deps.dashlane
    resolver.sync()  # a new item reaches the CLI on its next sync (hourly by default)
    resolver._cache.pop(ref, None)
    try:
        return resolver.read(ref) == value
    except dashlane.DashlaneError as e:
        ctx.ui.warn(str(e))
        return False


def to_reference(ctx: Ctx, key: str, value: str) -> str:
    """What to write to .env for `key`: a verified Dashlane reference, or the value itself."""
    if not value or dashlane.is_ref(value) or not dashlane_store(ctx):
        return value
    cfg = ctx.repo().server
    item = next((i for i in dashlane.items(cfg.name, {key: ""})), None)
    if item is None:  # not one of the credentials Dashlane is offered for
        return value
    ui = ctx.ui
    ui.todo(
        [
            f"In Dashlane: add a Password item titled {item.title} (any website; it's for {item.what}).",
            "Paste the same value as its password (you just pasted it here), save.",
        ],
        intro=f"{key} goes to Dashlane; .env will keep only the reference {item.ref()}.",
    )
    while ui.confirm(ITEM_SAVED, f"Saved {item.title} in Dashlane?", default=True):
        if _read_back(ctx, item.ref(), value):
            ui.ok(f"{key}: {item.ref()} reads back the same value; .env keeps the reference")
            return item.ref()
        ui.warn(f"{item.ref()} didn't read back the value (title, field or `dcli sync`?)")
        if not ui.interactive:
            break
    ui.warn(
        f"{key}: kept in .env as a value (`mcctl secrets dashlane --convert` swaps it for a reference later)"
    )
    return value


def save_restic(ctx: Ctx, password: str) -> bool:
    """Show the restic password once for a Dashlane Secure Note, read it back, keep the reference."""
    ui, cfg = ctx.ui, ctx.repo().server
    title, ref = dashlane.restic_title(cfg.name), dashlane.restic_ref(cfg.name)
    if not ui.confirm(
        "restic_dashlane",
        f"Show the restic password once, to save it in Dashlane as the Secure Note {title}?",
        default=True,
    ):
        return False
    ui.todo(
        [
            f"In Dashlane: add a Secure Note titled {title}.",
            "Its text: exactly the password below (nothing else), save. Then clear this screen.",
        ],
        intro="dcli can only read Dashlane, so mcctl can't save it for you. It is shown this once only.",
    )
    ui.code([password])
    while ui.confirm(ITEM_SAVED, f"Saved {title}?", default=True):
        if _read_back(ctx, ref, password):
            ctx.save_env(**{dashlane.RESTIC_REF_KEY: ref})
            ui.ok(f"{ref} reads back the restic password; `mcctl doctor` checks it from now on")
            return True
        typed = ui.ask(
            "restic_dashlane_ref",
            "Reference to the note if it differs (dl://<title or id>/<field>; empty: try again)",
            default="",
            check=lambda v: None if not v or dashlane.REF.fullmatch(v) else "dl://<title or id>/<field>",
        )
        if typed:
            ref = typed
        if not ui.interactive:
            break
    return False


def secrets_dashlane(ctx: Ctx, convert: bool) -> int:
    """`mcctl secrets dashlane [--convert]`: the items to create; --convert swaps verified values for refs."""
    ui, cfg = ctx.ui, ctx.repo().server
    raw = envfile.load(ctx.root)
    wanted = dashlane.items(cfg.name, raw)
    rows = []
    for item in wanted:
        now = raw[item.key]
        state = "reference" if dashlane.is_ref(now) else "value in .env"
        rows.append([item.key, item.what, item.title, now if dashlane.is_ref(now) else item.ref(), state])
    restic = raw.get(dashlane.RESTIC_REF_KEY)
    rows.append(
        [
            "(restic password)",
            "Terraform state only; for restores",
            dashlane.restic_title(cfg.name) + " (Secure Note)",
            restic or dashlane.restic_ref(cfg.name),
            "reference" if restic else "not recorded",
        ]
    )
    ui.table(
        ["`.env` key", "what", "Dashlane item (Password)", "reference", "now"], rows, title="Dashlane items"
    )
    ui.info("dcli can only read Dashlane items: create each one in the Dashlane app or extension (title as")
    ui.info("above, the secret in the password field); then `mcctl secrets dashlane --convert` swaps them.")
    if not convert:
        return 0
    if not ctx.deps.dashlane.available():
        raise StepError(
            "dcli isn't installed", f"install the Dashlane CLI ({dashlane.INSTALL}), log in with `dcli sync`"
        )
    ctx.deps.dashlane.sync()
    changed: dict[str, str] = {}
    for item in wanted:
        value = raw[item.key]
        if dashlane.is_ref(value):
            continue
        try:
            same = ctx.deps.dashlane.read(item.ref(), item.key) == value
        except dashlane.DashlaneError as e:
            ui.warn(str(e))
            continue
        if same:
            changed[item.key] = item.ref()
        else:
            ui.warn(f"{item.key}: {item.ref()} holds a different value; left as is")
    if changed:
        envfile.write(ctx.root, changed)
        ui.ok(f"{', '.join(sorted(changed))}: now Dashlane references in .env")
    else:
        ui.info("nothing to convert")
    return 0


# --- Discord slash commands -------------------------------------------------------------------------------

SNOWFLAKE = re.compile(schema.SNOWFLAKE)


def _id_check(v: str) -> str | None:
    return None if SNOWFLAKE.fullmatch(v.strip()) else "a Discord id: 17-20 digits (Developer Mode > Copy ID)"


def _explicit(ctx: Ctx, path: str) -> bool:
    """Is `path` written in server.yml (not just the schema's default)?"""
    return yamledit.get_value(ctx.server_path.read_text(), path) is not yamledit.MISSING


def commands_decided(ctx: Ctx) -> bool | None:
    """The slash-commands answer an earlier run gave: `discord.commands` written in server.yml (setup writes
    it either way; a hand edit counts too), else the discord step's recorded outputs (a state.json from before
    the answer was kept in server.yml). None: never answered."""
    if _explicit(ctx, "discord.commands"):
        return bool(ctx.repo().server.discord.commands)
    prev = ctx.outputs("discord").get("commands")
    return prev if isinstance(prev, bool) else None


def setup_commands(ctx: Ctx, env: dict[str, str]) -> dict[str, Any]:
    """Part of setup's Discord step: the application, the ids (server.yml), the install link, the commands."""
    ui = ctx.ui
    cfg = ctx.repo().server
    d = cfg.discord
    decided = commands_decided(ctx)
    if decided is not None and not ctx.reconfigure("discord"):
        want = decided  # answered on an earlier run: never re-asked on a resume (a live run, 2026-11-02)
        if not want:
            ui.ok("Discord slash commands off (`mcctl setup --reconfigure discord` to set them up)")
    else:
        want = ui.confirm(
            "discord_commands",
            "Discord slash commands too (/status, /players, /lag, /perf, /backup now, /whitelist add ...)?",
            default=d.commands,
        )
    if not want:
        if d.commands or not _explicit(ctx, "discord.commands"):  # remember the "no" (server.yml)
            ctx.set({"discord.commands": False})
            ctx.commit("mcctl setup: Discord slash commands off", [configrepo.SERVER_YML])
        return {"commands": False}
    if cfg.panel.mode != "cloudflare":
        ui.warn("Discord slash commands need panel.mode cloudflare (the Worker answers them): skipped")
        return {"commands": False}
    token = env.get(discord.BOT_TOKEN_ENV)
    fresh = token is None
    if fresh:
        ui.todo(
            [
                "Open https://discord.com/developers/applications > New Application (a name like "
                f"'{cfg.name} server'), accept the terms.",
                "Bot > Reset Token > copy it (it stays in this machine's .env; the server and the Worker "
                "never "
                "get it). No privileged intents are needed.",
                "Paste it below (input hidden; it is a credential).",
            ]
        )
        token = ui.secret("discord_bot_token", "Paste the bot token", env_name=discord.BOT_TOKEN_ENV)
    app = _guarded(lambda: discord.application(token, ctx.deps.transport))
    ui.ok(f"Discord application {app['name'] or app['id']} (id {app['id']})")
    complete = d.commands and d.guild_id and d.channel_ids and d.user_ids
    if (
        not fresh
        and complete
        and not ctx.reconfigure("discord")
        and app["id"] == d.application_id
        and app["public_key"] == d.public_key
    ):
        ui.ok(f"slash commands set up for server {d.guild_id} (change: `mcctl setup --reconfigure discord`)")
        return {"commands": True, "application_id": app["id"], "guild_id": d.guild_id}
    if fresh:
        ctx.save_env(**{discord.BOT_TOKEN_ENV: token})  # (a Dashlane reference instead, if chosen: save_env)
    ui.todo(
        [
            "In Discord: User Settings > Advanced > Developer Mode on.",
            "Right-click your server's icon > Copy Server ID; the channel for the commands > "
            "Copy Channel ID; "
            "your own name > Copy User ID.",
        ],
        intro="Only this server, this channel and you may use the commands (the Worker checks each request).",
    )
    guild = ui.ask("discord_guild_id", "Server (guild) ID", default=d.guild_id, check=_id_check).strip()
    channel = ui.ask(
        "discord_channel_id", "Channel ID", default=(d.channel_ids or [None])[0], check=_id_check
    ).strip()
    user = ui.ask(
        "discord_user_id", "Your user ID", default=(d.user_ids or [None])[0], check=_id_check
    ).strip()
    ctx.set(
        {
            "discord.commands": True,
            "discord.application_id": app["id"],
            "discord.public_key": app["public_key"],
            "discord.guild_id": guild,
            "discord.channel_ids": [channel, *[c for c in d.channel_ids if c != channel]],
            "discord.user_ids": [user, *[u for u in d.user_ids if u != user]],
        }
    )
    ctx.commit("mcctl setup: Discord slash commands (ids and the public key)", [configrepo.SERVER_YML])
    ui.todo(
        [
            "Add the application to your server (commands only, no bot user): "
            + discord.install_url(app["id"], guild)
        ],
        title="Your turn",
    )
    if not ui.confirm("discord_installed", "Added it to the server?", default=True):
        raise StepError("add the application to the server first", "open the link above, then re-run")
    names = _guarded(lambda: discord.register_commands(token, app["id"], guild, ctx.deps.transport))
    ui.ok(f"registered /{', /'.join(names)} in the server (the endpoint is set after `terraform apply`)")
    return {"commands": True, "application_id": app["id"], "guild_id": guild}


def apply_inputs(ctx: Ctx) -> dict[str, Any]:
    """The apply step's inputs: {} while the slash commands are off (the hash every setup before P14
    recorded), the Worker's DISCORD binding when they're on, so enabling them (or changing an id) on a setup
    whose apply is done re-runs the apply before `discord-endpoint` asks Discord to PING the Worker.
    github.oidc_subject immutable adds `ci_oidc_subject`, so a setup applied with the name-only CI subject
    re-applies the identity once (legacy keeps the old hash); github.environments adds `github_environments`.
    """
    cfg = ctx.repo().server
    # The tailnet's CI identity trusts the `sub` format of github.oidc_subject: changing it re-runs the apply.
    oidc = {} if cfg.github.oidc_subject == "legacy" else {"ci_oidc_subject": cfg.github.oidc_subject}
    if cfg.github.environments:  # security review M2: environments + the environment:deploy subject
        oidc["github_environments"] = True
    if not (cfg.discord.commands and cfg.panel.mode == "cloudflare"):
        return oidc
    return {**oidc, "discord": render.discord_commands(cfg)}


def endpoint_inputs(ctx: Ctx) -> dict[str, Any]:
    d = ctx.repo().server.discord
    return {"commands": d.commands, "app": d.application_id, "guild": d.guild_id, "key": d.public_key}


def sync(ctx: Ctx, *, register: bool = True) -> dict[str, Any]:
    """Register the commands and point the application's interactions endpoint at the Worker."""
    ui, cfg = ctx.ui, ctx.repo().server
    d = cfg.discord
    if not d.commands:
        ui.ok("Discord slash commands off (discord.commands: false)")
        return {"enabled": False}
    token = ctx.env().get(discord.BOT_TOKEN_ENV)
    if not token:
        raise StepError(
            f"{discord.BOT_TOKEN_ENV} isn't set (.env)",
            "Discord developer portal > your application > Bot > Reset Token, put it in .env, re-run",
        )
    assert d.application_id and d.guild_id
    app = _guarded(lambda: discord.application(token, ctx.deps.transport))
    if app["id"] != d.application_id or app["public_key"] != d.public_key:
        raise StepError(
            "the bot token's application doesn't match server.yml discord.application_id/public_key",
            "re-run `mcctl setup --reconfigure discord` (it reads both from Discord)",
        )
    names: list[str] = []
    if register:
        names = _guarded(
            lambda: discord.register_commands(
                token, d.application_id or "", d.guild_id or "", ctx.deps.transport
            )
        )
        ui.ok(f"commands: /{', /'.join(names)}")
    url = render.watchdog_url(cfg) + discord.PATH
    got = _guarded(lambda: discord.set_endpoint(token, url, ctx.deps.transport))
    ui.ok(f"interactions endpoint: {got or url} (Discord verified the Worker's signed PING)")
    return {"enabled": True, "endpoint": url, "commands": names}


def step_endpoint(ctx: Ctx) -> dict[str, Any]:
    return sync(ctx)


def restic_password(ctx: Ctx, *, strict: bool = False) -> str:
    """The restic password from Terraform's outputs (memory only). `.env`'s Dashlane references are resolved
    for Terraform by the same resolver as everywhere (`envfile.merged`). strict: errors raise."""
    try:
        terraform, _out = ctx.deps.terraform(ctx.repo(), ctx.product_root, {})
        return str(terraform.outputs().get("restic_password") or "")
    except tf.TfError as e:
        if strict:
            raise
        ctx.ui.warn(f"couldn't read the restic password from Terraform: {e}")
        return ""


def show_restic_command(root: object) -> str:
    """The hand-run hint that prints the restic password (instead of sourcing .env, which breaks with dl://)."""
    return f"mcctl secrets show-restic -C {root}"


def show_restic(ctx: Ctx, out: TextIO | None = None) -> int:
    """`mcctl secrets show-restic`: print the restic password once, on the terminal only (never to a pipe, a
    file or a log), so it can be copied into a password manager."""
    stream = sys.stdout if out is None else out
    if not stream.isatty():
        raise StepError(
            "stdout isn't a terminal: the restic password is only ever printed to a terminal",
            "run `mcctl secrets show-restic` in a terminal, without a pipe or redirect",
        )
    password = restic_password(ctx, strict=True)
    if not password:
        raise StepError("Terraform has no restic_password output", "run `mcctl apply` first")
    stream.write(password + "\n")
    stream.flush()
    return 0
