"""Discord (design §4 init step 6; P14 slash commands).

Alerts: `GET <webhook>` returns the webhook object; nothing is posted.

Slash commands (P14), with the application's bot token (`DISCORD_BOT_TOKEN` in the config repo's .env; it
never reaches Terraform, CI, the Worker or the server). Endpoints from Discord's API docs (checked
2026-11-02):
- `GET /applications/@me` (Get Current Application): the application's `id` and `verify_key` (hex public key);
- `PUT /applications/{id}/guilds/{guild}/commands` (Bulk Overwrite Guild Application Commands): replaces ALL
  of the application's commands in that guild with COMMANDS (so a removed command disappears);
- `PATCH /applications/@me` {interactions_endpoint_url} (Edit Current Application): Discord accepts the URL
  only if it answers a signed PING (the Worker must already be deployed with the DISCORD binding);
- auth header `Authorization: Bot <token>`. The command list is the Worker's (watchdog/worker.js
  DISCORD_COMMANDS; tests/test_discord.py checks both).
"""

from __future__ import annotations

import re
import urllib.parse
from typing import Any

from mcctl import http
from mcctl.providers import CredentialCheck, ProviderError

CREDENTIAL = "discord-webhook"
BOT_CREDENTIAL = "discord-bot"
WEBHOOK = re.compile(r"https://(?:discord\.com|discordapp\.com)/api/webhooks/\d+/[A-Za-z0-9_-]+")
API = "https://discord.com/api/v10"
SNOWFLAKE = re.compile(r"[0-9]{17,20}")
PATH = "/discord/interactions"  # = watchdog/worker.js DISCORD_PATH
BOT_TOKEN_ENV = "DISCORD_BOT_TOKEN"  # noqa: S105 - a variable name

SUB_COMMAND, STRING = 1, 3
# Guild commands. default_member_permissions "0": hidden from everyone but the guild's administrators unless
# the
# guild grants them (Server Settings > Integrations); the Worker's user allowlist decides in any case.
_BASE: dict[str, Any] = {"type": 1, "default_member_permissions": "0"}
COMMANDS: list[dict[str, Any]] = [
    {**_BASE, "name": "status", "description": "Server status: up or down, players, TPS, the watchdog"},
    {**_BASE, "name": "players", "description": "Who is online"},
    {
        **_BASE,
        "name": "lag",
        "description": "Why is it lagging? The server's verdict, or one player's",
        "options": [
            {"type": STRING, "name": "player", "description": "A player who is online", "max_length": 16}
        ],
    },
    {
        **_BASE,
        "name": "perf",
        "description": "Tick times and TPS over a time range",
        "options": [
            {
                "type": STRING,
                "name": "range",
                "description": "Time range (default 24 hours)",
                "choices": [
                    {"name": "24 hours", "value": "24h"},
                    {"name": "7 days", "value": "7d"},
                    {"name": "30 days", "value": "30d"},
                ],
            }
        ],
    },
    {**_BASE, "name": "backups", "description": "Recent backups"},
    {
        **_BASE,
        "name": "backup",
        "description": "Backups",
        "options": [{"type": SUB_COMMAND, "name": "now", "description": "Start a backup now"}],
    },
    {
        **_BASE,
        "name": "whitelist",
        "description": "The whitelist",
        "options": [
            {"type": SUB_COMMAND, "name": "list", "description": "Who is whitelisted"},
            {
                "type": SUB_COMMAND,
                "name": "add",
                "description": "Whitelist a player (live now; recorded in Git by a pull request)",
                "options": [
                    {
                        "type": STRING,
                        "name": "name",
                        "description": "Java name",
                        "required": True,
                        "min_length": 3,
                        "max_length": 16,
                    }
                ],
            },
        ],
    },
    {**_BASE, "name": "rejected", "description": "Recent logins the whitelist refused"},
]


def check_webhook(url: str, transport: http.Transport) -> CredentialCheck:
    check = CredentialCheck(CREDENTIAL)
    if not WEBHOOK.fullmatch(url.strip()):
        return check.fail("not a https://discord.com/api/webhooks/<id>/<token> URL")
    resp = http.get(transport, url.strip())
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict) or "id" not in data:
        return check.fail(f"webhook lookup failed (HTTP {resp.status}): deleted, or the URL is wrong")
    check.expires(None)
    check.info("webhook exists (nothing was posted)")
    return check


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bot {token}"}


def _fail(what: str, resp: http.Response) -> ProviderError:
    data = resp.json()
    msg = data.get("message") if isinstance(data, dict) else None
    hint = "the bot token is wrong or was reset" if resp.status == 401 else (msg or "")
    return ProviderError(f"Discord {what}: HTTP {resp.status}{f' ({hint})' if hint else ''}")


def application(token: str, transport: http.Transport) -> dict[str, str]:
    """{id, public_key, name, endpoint} of the bot token's application."""
    resp = http.get(transport, f"{API}/applications/@me", _auth(token))
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict):
        raise _fail("application lookup", resp)
    app_id, key = str(data.get("id", "")), str(data.get("verify_key", "")).lower()
    if not SNOWFLAKE.fullmatch(app_id) or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ProviderError("Discord application lookup: no id/verify_key in the answer")
    return {
        "id": app_id,
        "public_key": key,
        "name": str(data.get("name", ""))[:100],
        "endpoint": str(data.get("interactions_endpoint_url") or ""),
    }


def check_bot(token: str, transport: http.Transport, app_id: str | None = None) -> CredentialCheck:
    check = CredentialCheck(BOT_CREDENTIAL)
    try:
        app = application(token, transport)
    except ProviderError as e:
        return check.fail(str(e))
    if app_id and app["id"] != app_id:
        return check.fail(f"the token belongs to application {app['id']}, server.yml says {app_id}")
    check.expires(None)  # bot tokens don't expire (they are reset by hand)
    check.info(f"application {app['name'] or app['id']}")
    check.facts["endpoint"] = app["endpoint"]
    return check


def register_commands(token: str, app_id: str, guild_id: str, transport: http.Transport) -> list[str]:
    """Bulk-overwrite the guild's commands with COMMANDS; the names Discord now has."""
    url = f"{API}/applications/{app_id}/guilds/{guild_id}/commands"
    resp = http.send_json(transport, "PUT", url, COMMANDS, _auth(token))
    data = resp.json()
    if resp.status not in (200, 201) or not isinstance(data, list):
        if resp.status == 403 or (isinstance(data, dict) and data.get("code") == 50001):
            raise ProviderError(
                "Discord refused the guild commands (403, Missing Access): the application isn't installed "
                "in that server yet (open the install link above, then re-run)"
            )
        raise _fail("command registration", resp)
    return sorted(str(c.get("name")) for c in data if isinstance(c, dict))


def set_endpoint(token: str, url: str, transport: http.Transport) -> str:
    resp = http.send_json(
        transport, "PATCH", f"{API}/applications/@me", {"interactions_endpoint_url": url}, _auth(token)
    )
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict):
        if resp.status == 400:
            raise ProviderError(
                "Discord didn't accept the interactions endpoint (it sends a signed PING first): is the "
                "Worker "
                "deployed with discord.commands on (`mcctl apply`) and the public key in server.yml right?"
            )
        raise _fail("endpoint update", resp)
    return str(data.get("interactions_endpoint_url") or "")


def install_url(app_id: str, guild_id: str) -> str:
    """Add the application to the guild with only the commands scope (no bot user, no permissions): Discord's
    OAuth2 authorize URL with scope applications.commands, the guild pre-selected and locked."""
    query = urllib.parse.urlencode(
        {
            "client_id": app_id,
            "scope": "applications.commands",
            "guild_id": guild_id,
            "disable_guild_select": "true",
            "integration_type": "0",
        }
    )
    return f"https://discord.com/oauth2/authorize?{query}"
