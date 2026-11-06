"""Minecraft name -> UUID for players.yml (init step 2).

`GET https://api.mojang.com/users/profiles/minecraft/<name>` -> `{"id": "<32 hex>", "name": "<name>"}`;
404 (or 204) when no such account. [V, secondary: the community API reference (wiki.vg) and client
libraries, 2026-10-31; Mojang publishes no reference.] Unauthenticated, nothing secret is sent. When it
fails the wizard asks for the UUID instead (or the player is added later from the server console).
"""

from __future__ import annotations

import re
import urllib.parse

from mcctl import http
from mcctl.providers import ProviderError

API = "https://api.mojang.com/users/profiles/minecraft"
NAME = re.compile(r"[A-Za-z0-9_]{3,16}")


class MojangError(ProviderError):
    pass


def uuid_for(name: str, transport: http.Transport) -> tuple[str, str] | None:
    """(canonical name, dashed lowercase uuid), or None if Mojang has no such player."""
    if not NAME.fullmatch(name):
        raise MojangError(f"{name!r} isn't a Minecraft name (3-16 letters, digits, _)")
    try:
        resp = http.get(transport, f"{API}/{urllib.parse.quote(name)}")
    except http.HttpError as e:
        raise MojangError(f"Mojang lookup failed: {e}") from None
    if resp.status in (204, 404):
        return None
    data = resp.json()
    raw = data.get("id") if isinstance(data, dict) else None
    if resp.status != 200 or not isinstance(raw, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", raw):
        raise MojangError(f"Mojang lookup failed (HTTP {resp.status})")
    h = raw.lower()
    return str(data.get("name") or name), f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:]}"
