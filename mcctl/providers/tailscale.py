"""Tailscale OAuth client used by Terraform (design §3, §4 init step 7, `doctor`).

- `POST https://api.tailscale.com/api/v2/oauth/token` with form `client_id`, `client_secret` (OAuth 2.0 client
  credentials) -> `{access_token, token_type, expires_in, scope}` (KB 1215). The access token lives one hour
  and is dropped here; OAuth clients themselves don't expire.
- `GET /api/v2/tailnet/-/acl` with that token: the policy_file scope Terraform's `tailscale_acl` needs.
- The one-off API access token (init step 7): `tskey-api-<key id>-<secret>`; `DELETE /tailnet/-/keys/{id}`
  revokes a key (whether a token may revoke itself is unverified, so mcctl tries, then checks whether the
  token still authenticates); `GET /tailnet/-/keys/{id}` with the OAuth client tells whether it still exists.
"""

from __future__ import annotations

import re

from mcctl import http
from mcctl.providers import CredentialCheck, ProviderError

API = "https://api.tailscale.com/api/v2"
CREDENTIAL = "tailscale-oauth"
# What Terraform's OAuth client needs (README "Tailscale OAuth client for Terraform"; SECURITY.md). Anything
# else the token reports is an extra scope: least privilege says remove it (design §7 "Tokens").
EXPECTED_SCOPES = frozenset(
    {"policy_file", "devices:posture_attributes", "devices:core:read", "auth_keys", "federated_keys"}
)


def extra_scopes(scope: str) -> list[str]:
    """Scopes beyond EXPECTED_SCOPES (a ':read' variant of an expected scope is not extra)."""
    extra = []
    for s in scope.replace(",", " ").split():
        read_of_expected = s.endswith(":read") and s.removesuffix(":read") in EXPECTED_SCOPES
        if s not in EXPECTED_SCOPES and not read_of_expected:
            extra.append(s)
    return sorted(extra)


def check_oauth(client_id: str, client_secret: str, transport: http.Transport) -> CredentialCheck:
    check = CredentialCheck(CREDENTIAL)
    resp = http.post_form(
        transport, f"{API}/oauth/token", {"client_id": client_id, "client_secret": client_secret}
    )
    data = resp.json()
    token = data.get("access_token") if isinstance(data, dict) else None
    if resp.status != 200 or not isinstance(token, str) or not token:
        return check.fail(f"OAuth token exchange failed (HTTP {resp.status}): check the client id/secret")
    scope = str(data.get("scope", ""))
    resp = http.get(transport, f"{API}/tailnet/-/acl", {"Authorization": f"Bearer {token}"})
    if resp.status != 200:
        return check.fail(f"can't read the tailnet policy (HTTP {resp.status}): the client needs policy_file")
    check.expires(None)  # OAuth clients don't expire
    check.info(f"token exchange and policy read work (scopes: {scope or 'n/a'}); OAuth clients don't expire")
    if extra := extra_scopes(scope):
        check.warn(
            f"the OAuth client has scopes Terraform doesn't need ({', '.join(extra)}): create one with only "
            + ", ".join(sorted(EXPECTED_SCOPES))
        )
    return check


def check_api_key(api_key: str, transport: http.Transport) -> CredentialCheck:
    """The one-off API access token for the first ACL apply (init step 7a): `GET /tailnet/-/devices` works.
    Tailscale API access tokens are sent as a Bearer token."""
    check = CredentialCheck("tailscale-api-key")
    resp = http.get(transport, f"{API}/tailnet/-/devices", {"Authorization": f"Bearer {api_key}"})
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        return check.fail(f"device list failed (HTTP {resp.status}): wrong or expired API access token")
    check.info(f"API access token works ({len(data['devices'])} devices visible)")
    return check


_API_KEY = re.compile(r"tskey-api-([A-Za-z0-9]+)-[A-Za-z0-9]+")


def api_key_id(api_key: str) -> str | None:
    """The key id inside an API access token (not secret: the admin console lists it), or None."""
    m = _API_KEY.fullmatch(api_key.strip())
    return m.group(1) if m else None


def revoke_api_key(api_key: str, key_id: str, transport: http.Transport) -> bool:
    """Ask the API to delete the token, authenticated by the token itself. True if the API accepted it."""
    req = http.Request("DELETE", f"{API}/tailnet/-/keys/{key_id}", {"Authorization": f"Bearer {api_key}"})
    return transport(req).status in (200, 204)


def api_key_works(api_key: str, transport: http.Transport) -> bool:
    """Whether the API access token still authenticates (a revoked token gets 401)."""
    resp = http.get(transport, f"{API}/tailnet/-/devices", {"Authorization": f"Bearer {api_key}"})
    return resp.status == 200


def key_exists(client_id: str, client_secret: str, key_id: str, transport: http.Transport) -> bool | None:
    """Whether key `key_id` still exists, asked with the OAuth client: True/False, or None if the API
    won't say (scope, outage)."""
    resp = http.post_form(
        transport, f"{API}/oauth/token", {"client_id": client_id, "client_secret": client_secret}
    )
    data = resp.json()
    token = data.get("access_token") if isinstance(data, dict) else None
    if resp.status != 200 or not isinstance(token, str):
        return None
    resp = http.get(transport, f"{API}/tailnet/-/keys/{key_id}", {"Authorization": f"Bearer {token}"})
    if resp.status == 404:
        return False
    return True if resp.status == 200 else None


# init step 7(b): the OAuth client itself, created with the one-off API access token. Tailscale API (OpenAPI
# spec at https://api.tailscale.com/api/v2?outputOpenapiSchema=true, 2026-11-01):
# `POST /tailnet/{tailnet}/keys`
# with {"keyType": "client", "scopes": [...], "tags": [...], "description": "<= 50 alphanumerics, hyphens,
# spaces"} -> Key {id, key}: `id` is the OAuth client id, `key` the client secret (shown only here). Tags are
# mandatory with `auth_keys`. A key created with a user's API access token belongs to that user.
# First live apply (2026-11-01): a client tagged [tag:mc-server, tag:ci] got "requested tags
# [tag:mc-server] are invalid or not permitted (400)" for the server key and the CI identity. Tailscale
# (tailscale/github-action troubleshooting, KB 1215 "tag owner" example): the requested tags must be ALL of
# the credential's tags or tags OWNED by its tags. So the client carries one owner tag, which the tailnet
# policy makes owner of both (tagOwners; tests/test_tailscale_policy.py).
OAUTH_TAGS = ("tag:mc-terraform",)


def create_oauth_client(api_key: str, description: str, transport: http.Transport) -> tuple[str, str] | None:
    """(client id, client secret) of a new OAuth client with exactly EXPECTED_SCOPES and OAUTH_TAGS, or None
    if the API refused (the caller then falls back to the admin console)."""
    desc = re.sub(r"[^A-Za-z0-9 -]", "-", description)[:50]
    body = {
        "keyType": "client",
        "description": desc,
        "scopes": sorted(EXPECTED_SCOPES),
        "tags": list(OAUTH_TAGS),
    }
    resp = http.send_json(
        transport, "POST", f"{API}/tailnet/-/keys", body, {"Authorization": f"Bearer {api_key}"}
    )
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict):
        return None
    cid, secret = data.get("id"), data.get("key")
    if not isinstance(cid, str) or not isinstance(secret, str) or not cid or not secret:
        return None
    return cid, secret


def delete_key(api_key: str, key_id: str, transport: http.Transport) -> bool:
    """Delete a key or OAuth client (`DELETE /tailnet/-/keys/{id}`; OAuth clients are keys of keyType client)
    with the one-off API access token. True if it's gone (deleted now, or already gone)."""
    req = http.Request("DELETE", f"{API}/tailnet/-/keys/{key_id}", {"Authorization": f"Bearer {api_key}"})
    return transport(req).status in (200, 204, 404)


# P12 `mcctl migrate` / `retire-old`: the old server's tailnet node, with a one-off API access token (the
# OAuth
# client Terraform uses has devices:core:read only, and keeps it). Tailscale API (OpenAPI spec at
# https://api.tailscale.com/api/v2?outputOpenapiSchema=true, 2026-11-01): `GET /tailnet/{tailnet}/devices`
# -> {devices: [{id, nodeId, name (FQDN), hostname, addresses, lastSeen, ...}]}; `POST /device/{id}/name`
# {"name": "<new machine name>"} renames it (its MagicDNS name follows); `DELETE /device/{id}` removes it.
# [U] the rename body's exact semantics (a bare label is assumed; the FQDN form is documented too).


def devices(api_key: str, transport: http.Transport) -> list[dict[str, object]]:
    resp = http.get(transport, f"{API}/tailnet/-/devices", {"Authorization": f"Bearer {api_key}"})
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict) or not isinstance(data.get("devices"), list):
        raise ProviderError(
            f"Tailscale device list failed (HTTP {resp.status})",
            "use a fresh API access token (admin console > Settings > Keys; 1 day expiry is enough)",
        )
    return [d for d in data["devices"] if isinstance(d, dict)]


def machine_name(device: dict[str, object]) -> str:
    """The device's MagicDNS machine name (first label of `name`), which is what `ssh <name>` resolves."""
    return str(device.get("name") or device.get("hostname") or "").split(".", 1)[0]


def find_device(api_key: str, name: str, transport: http.Transport) -> dict[str, object] | None:
    found = [d for d in devices(api_key, transport) if machine_name(d) == name]
    if len(found) > 1:
        raise ProviderError(f"more than one tailnet device is named {name!r}", "remove the stale one first")
    return found[0] if found else None


def rename_device(api_key: str, device_id: str, name: str, transport: http.Transport) -> None:
    resp = http.send_json(
        transport,
        "POST",
        f"{API}/device/{device_id}/name",
        {"name": name},
        {"Authorization": f"Bearer {api_key}"},
    )
    if resp.status not in (200, 204):
        raise ProviderError(
            f"renaming tailnet device {device_id} to {name!r} failed (HTTP {resp.status})",
            "the API access token's user must be an admin of the tailnet; or rename it in the admin console",
        )


def delete_device(api_key: str, device_id: str, transport: http.Transport) -> bool:
    """True when the device is gone (404 counts: already deleted)."""
    req = http.Request("DELETE", f"{API}/device/{device_id}", {"Authorization": f"Bearer {api_key}"})
    status = transport(req).status
    if status in (200, 204, 404):
        return True
    raise ProviderError(
        f"deleting tailnet device {device_id} failed (HTTP {status})",
        "remove it in the admin console (Machines)",
    )
