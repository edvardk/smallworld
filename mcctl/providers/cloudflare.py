"""Cloudflare admin API token (design §4 init step 3, `doctor`).

Endpoints (Cloudflare API v4, checked 2026-10-31):
- `GET /user/tokens/verify` -> `result: {id, status: active|disabled|expired, expires_on?, not_before?}` for a
  user-owned token; an account-owned token is verified with `GET /accounts/{account_id}/tokens/verify`
  (same result shape). The account id comes from the zone lookup, which works with either kind.
- `GET /zones?name=<zone>` -> the zone (`status`, `account.id`); empty when the token can't see it.
- `GET /accounts/{account_id}/r2/buckets` -> needs Workers R2 Storage (Terraform: backups bucket + lock).
- `GET /accounts/{account_id}/workers/scripts` -> needs Workers Scripts Edit (the watchdog Worker, its cron
  trigger, custom domain and workers.dev switch; the custom-domain API accepts "Workers Scripts Write").
- `GET /accounts/{account_id}/d1/database` -> needs D1 Edit (the watchdog's state database; "D1 Write").
- `GET /user/tokens/permission_groups` (or `/accounts/{id}/tokens/permission_groups`) -> needs API Tokens
  Edit (Terraform's backup token and mcctl's state token).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import re
import urllib.parse

from mcctl import http
from mcctl.providers import CredentialCheck, ProviderError, parse_datetime

API = "https://api.cloudflare.com/client/v4"
CREDENTIAL = "cloudflare-admin"
R2_ENDPOINT = re.compile(r"https://([0-9a-f]{32})\.r2\.cloudflarestorage\.com/?")


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _ok(resp: http.Response) -> bool:
    data = resp.json()
    return resp.status == 200 and isinstance(data, dict) and data.get("success") is True


def _errors(resp: http.Response) -> str:
    data = resp.json()
    errs = data.get("errors") if isinstance(data, dict) else None
    if isinstance(errs, list) and errs:
        return "; ".join(f"{e.get('code')}: {e.get('message')}" for e in errs if isinstance(e, dict))
    return f"HTTP {resp.status}"


AUTH_ERROR = 9109  # "Unauthorized to access requested resource"


def _has_code(resp: http.Response, code: int) -> bool:
    data = resp.json()
    errs = data.get("errors") if isinstance(data, dict) else None
    return isinstance(errs, list) and any(isinstance(e, dict) and e.get("code") == code for e in errs)


def _zone(token: str, zone: str, transport: http.Transport) -> tuple[dict | None, http.Response]:
    resp = http.get(transport, f"{API}/zones?{urllib.parse.urlencode({'name': zone})}", _headers(token))
    zones = resp.json().get("result") if _ok(resp) else None
    return (zones[0] if isinstance(zones, list) and zones and isinstance(zones[0], dict) else None), resp


def verify(
    token: str,
    transport: http.Transport,
    now: datetime.datetime,
    *,
    account_id: str | None = None,
    account_only: bool = False,
) -> CredentialCheck:
    """The token itself: active, inside not_before/expires_on, and when it expires. A user-owned token
    verifies at /user/tokens/verify; with `account_id`, an account-owned token at
    /accounts/{account_id}/tokens/verify (facts["owner"] says which). `account_only` skips the user check."""
    check = CredentialCheck(CREDENTIAL)
    check.facts["owner"] = "user"
    if account_only and account_id:
        resp = http.get(transport, f"{API}/accounts/{account_id}/tokens/verify", _headers(token))
        if not _ok(resp):
            return check.fail(f"token rejected by /accounts/{account_id}/tokens/verify ({_errors(resp)})")
        check.facts["owner"] = "account"
        return _verified(check, resp, now)
    resp = http.get(transport, f"{API}/user/tokens/verify", _headers(token))
    if not _ok(resp) and account_id:
        user_errors = _errors(resp)
        resp = http.get(transport, f"{API}/accounts/{account_id}/tokens/verify", _headers(token))
        if not _ok(resp):
            return check.fail(
                f"token rejected by /user/tokens/verify ({user_errors}) and by "
                f"/accounts/{account_id}/tokens/verify ({_errors(resp)})"
            )
        check.facts["owner"] = "account"
    elif not _ok(resp):
        check.facts["rejected"] = True  # maybe account-owned: check() retries under the zone's account
        return check.fail(f"token rejected by /user/tokens/verify ({_errors(resp)})")
    return _verified(check, resp, now)


def _verified(check: CredentialCheck, resp: http.Response, now: datetime.datetime) -> CredentialCheck:
    result = resp.json().get("result") or {}
    if isinstance(result.get("id"), str):
        check.facts["token_id"] = result["id"]  # not secret: the dashboard lists it
    status = result.get("status")
    if status != "active":
        return check.fail(f"token status is {status!r}, not active: create a new one")
    nbf = parse_datetime(result.get("not_before"))
    if nbf is not None and nbf > now:
        return check.fail(f"token isn't valid before {nbf:%Y-%m-%d %H:%M} UTC")
    expires = parse_datetime(result.get("expires_on"))
    check.expires(expires.date() if expires else None)
    owner = "account-owned, " if check.facts["owner"] == "account" else "user-owned, "
    check.info(f"{owner}active, expires {expires:%Y-%m-%d}" if expires else f"{owner}active, no expiry date")
    return check


def check(
    token: str,
    zone: str,
    transport: http.Transport,
    *,
    now: datetime.datetime,
    s3_endpoint: str | None = None,
) -> CredentialCheck:
    """verify() plus what Terraform needs: the zone, R2, Workers, D1, token permission groups, the state
    endpoint's account."""
    result = verify(token, transport, now)
    found, resp = _zone(token, zone, transport) if result.level != "fail" else (None, None)
    if result.level == "fail":
        if not result.facts.get("rejected"):
            return result
        # Maybe an account-owned token: those verify under the account, found through the zone.
        found, resp = _zone(token, zone, transport)
        account = (found or {}).get("account") or {}
        if not isinstance(account.get("id"), str):
            return result
        result = verify(token, transport, now, account_id=account["id"])
        if result.level == "fail":
            return result
    if found is None:
        why = _errors(resp) if resp is not None and not _ok(resp) else "no such zone visible to this token"
        return result.fail(
            f"zone {zone} ({why}): add it to the account, or give the token Zone > DNS > Edit on it"
        )
    if found.get("status") != "active":
        result.warn(
            f"zone {zone} status is {found.get('status')!r} (nameservers not moved to Cloudflare yet?)"
        )
    account = (found.get("account") or {}).get("id")
    if not isinstance(account, str) or not account:
        return result.fail(f"zone {zone}: the API returned no account id")
    result.facts["account_id"] = account
    result.facts["zone_id"] = found.get("id")
    resp = http.get(transport, f"{API}/accounts/{account}/r2/buckets", _headers(token))
    if not _ok(resp):
        result.fail(f"R2 bucket list failed ({_errors(resp)}): needs Account > Workers R2 Storage > Edit")
    resp = http.get(transport, f"{API}/accounts/{account}/workers/scripts", _headers(token))
    if not _ok(resp):
        result.fail(
            f"Workers script list failed ({_errors(resp)}): needs Account > Workers Scripts > Edit (watchdog)"
        )
    resp = http.get(transport, f"{API}/accounts/{account}/d1/database", _headers(token))
    if not _ok(resp):
        result.fail(f"D1 database list failed ({_errors(resp)}): needs Account > D1 > Edit (watchdog state)")
    owner = result.facts.get("owner", "user")
    resp = http.get(transport, f"{_tokens_base(owner, account)}/permission_groups", _headers(token))
    if owner == "user" and not _ok(resp) and _has_code(resp, AUTH_ERROR):
        # /user/tokens/verify accepted it, yet it can't use the user token API: maybe account-owned after all.
        acct = verify(token, transport, now, account_id=account, account_only=True)
        if acct.level != "fail":
            result.messages = acct.messages + result.messages[1:]
            result.facts["owner"], owner = "account", "account"
            if "token_id" in acct.facts:
                result.facts["token_id"] = acct.facts["token_id"]
            result.expires(acct.expires_on)
            resp = http.get(transport, f"{_tokens_base(owner, account)}/permission_groups", _headers(token))
    if not _ok(resp):
        result.fail(
            f"token permission groups can't be listed ({_errors(resp)}): needs "
            + ("Account > Account API Tokens > Edit" if owner == "account" else "User > API Tokens > Edit")
            + " (scoped R2 tokens for Terraform state and backups)"
        )
    if s3_endpoint:
        m = R2_ENDPOINT.fullmatch(s3_endpoint.strip())
        if m is None:
            result.warn("AWS_ENDPOINT_URL_S3 isn't https://<account-id>.r2.cloudflarestorage.com")
        elif m.group(1) != account:
            result.fail(f"AWS_ENDPOINT_URL_S3 names another account than zone {zone}'s ({account})")
    if result.level != "fail":
        result.info(f"zone {zone} found; R2, Workers, D1 and token permission groups reachable")
    return result


# --- init step 4: the Terraform state bucket and a token scoped to it --------------------------------
# Endpoints (Cloudflare API reference, 2026-10-31): `GET|POST /accounts/{id}/r2/buckets` (list:
# `result.buckets[].name`; create body `{name, locationHint}`), `GET /user/tokens/permission_groups` or
# `/accounts/{id}/tokens/permission_groups`, `POST /user/tokens` or `/accounts/{id}/tokens` (body `{name,
# policies: [{effect, resources, permission_groups: [{id}]}]}`; the response's `result.value` is shown once),
# `GET /user/tokens/{id}` or `/accounts/{id}/tokens/{id}`. The bucket resource key and the permission group
# name are the ones Terraform uses for the backup token (terraform/cloudflare.tf).

R2_WRITE_GROUP = "Workers R2 Storage Bucket Item Write"


class CloudflareError(ProviderError):
    pass


def _tokens_base(owner: str, account: str) -> str:
    return f"{API}/accounts/{account}/tokens" if owner == "account" else f"{API}/user/tokens"


def bucket_exists(token: str, account: str, name: str, transport: http.Transport) -> bool:
    query = urllib.parse.urlencode({"name_contains": name})
    resp = http.get(transport, f"{API}/accounts/{account}/r2/buckets?{query}", _headers(token))
    if not _ok(resp):
        raise CloudflareError(
            f"R2 bucket list failed ({_errors(resp)})", "the token needs Account > Workers R2 Storage > Edit"
        )
    buckets = (resp.json().get("result") or {}).get("buckets") or []
    return any(isinstance(b, dict) and b.get("name") == name for b in buckets)


def create_bucket(token: str, account: str, name: str, location: str, transport: http.Transport) -> None:
    body = {"name": name, "locationHint": location}
    resp = http.send_json(transport, "POST", f"{API}/accounts/{account}/r2/buckets", body, _headers(token))
    if not _ok(resp):
        raise CloudflareError(
            f"creating R2 bucket {name} failed ({_errors(resp)})",
            "the token needs Account > Workers R2 Storage > Edit; a name taken in another account needs "
            "another state.bucket in server.yml",
        )


def permission_groups(token: str, owner: str, account: str, transport: http.Transport) -> list[dict]:
    """The permission groups a token of this owner can grant ({id, name, scopes}; names only matter here)."""
    resp = http.get(transport, f"{_tokens_base(owner, account)}/permission_groups", _headers(token))
    groups = resp.json().get("result") if _ok(resp) else None
    if not isinstance(groups, list):
        raise CloudflareError(
            f"listing token permission groups failed ({_errors(resp)})",
            "the admin token needs Account > Account API Tokens > Edit (edit it in the dashboard, re-run)"
            if owner == "account"
            else "the admin token needs User > API Tokens > Edit (re-create it, update .env, re-run)",
        )
    return [g for g in groups if isinstance(g, dict)]


def permission_group_id(token: str, owner: str, account: str, name: str, transport: http.Transport) -> str:
    groups = permission_groups(token, owner, account, transport)
    ids = [g.get("id") for g in groups if isinstance(g, dict) and g.get("name") == name]
    if len(ids) != 1 or not isinstance(ids[0], str):
        raise CloudflareError(
            f"permission group {name!r} not found exactly once (found {len(ids)})",
            "Cloudflare renamed it: update mcctl (R2_WRITE_GROUP) and terraform/cloudflare.tf together",
        )
    return ids[0]


def create_r2_token(
    token: str, owner: str, account: str, bucket: str, transport: http.Transport
) -> tuple[str, str]:
    """A new API token that can read/write objects in one bucket only: (token id, token value)."""
    group = permission_group_id(token, owner, account, R2_WRITE_GROUP, transport)
    body = {
        "name": f"r2-{bucket}-rw (mcctl, Terraform state)",
        "policies": [
            {
                "effect": "allow",
                "resources": {f"com.cloudflare.edge.r2.bucket.{account}_default_{bucket}": "*"},
                "permission_groups": [{"id": group}],
            }
        ],
    }
    resp = http.send_json(transport, "POST", _tokens_base(owner, account), body, _headers(token))
    result = resp.json().get("result") if _ok(resp) else None
    if not isinstance(result, dict) or not result.get("id") or not result.get("value"):
        raise token_create_error(resp, "the state token", owner)
    return str(result["id"]), str(result["value"])


def token_create_error(resp: http.Response, what: str, owner: str) -> CloudflareError:
    """Why creating a token failed, with the fix. Error 1001: Cloudflare doesn't let a token created through
    the API hold token-management permissions (see SUB_TOKEN_ERROR)."""
    if _has_code(resp, SUB_TOKEN_ERROR):
        return CloudflareError(
            f"creating {what} failed ({_errors(resp)}): Cloudflare refuses to let a token created "
            "through the API hold permissions to manage tokens",
            "a token that manages tokens (like mcctl's admin token) must be created by hand in the "
            "dashboard: Manage Account > Account API Tokens > Create Token (docs/setup.md); the tokens "
            "mcctl and Terraform create never need that permission",
        )
    fix = (
        "the admin token needs Account > Account API Tokens > Edit (edit it in the dashboard, re-run)"
        if owner == "account"
        else "the admin token needs User > API Tokens > Edit (re-create it, update .env, re-run)"
    )
    return CloudflareError(f"creating {what} failed ({_errors(resp)})", fix)


def token_exists(token: str, owner: str, account: str, token_id: str, transport: http.Transport) -> bool:
    resp = http.get(transport, f"{_tokens_base(owner, account)}/{token_id}", _headers(token))
    return _ok(resp)


def delete_token(token: str, owner: str, account: str, token_id: str, transport: http.Transport) -> None:
    """Delete an API token mcctl created (`DELETE /user/tokens/{id}` or `/accounts/{id}/tokens/{id}`)."""
    resp = transport(http.Request("DELETE", f"{_tokens_base(owner, account)}/{token_id}", _headers(token)))
    if not _ok(resp) and resp.status != 404:
        raise CloudflareError(
            f"deleting token {token_id} failed ({_errors(resp)})",
            "delete it in the dashboard: "
            + ("Manage Account > Account API Tokens" if owner == "account" else "My Profile > API Tokens"),
        )


def r2_credentials(token_id: str, value: str) -> tuple[str, str]:
    """S3 credentials of an R2 API token: (access key id = token id, secret = sha256 hex of the value)."""
    return token_id, hashlib.sha256(value.encode()).hexdigest()


def token_url(name: str, *, access: bool = False) -> str:
    """Cloudflare's template URL that opens the account token form pre-filled (developers.cloudflare.com
    fundamentals/api/how-to/account-owned-token-template, checked 2026-11-01). Short keys from its permission
    reference: dns, zone, workers_r2, workers_scripts, d1, account_api_tokens, access (Access applications),
    access_acct (Access organizations); `type` read|edit. The URL takes no account/zone ids: the resources are
    chosen in the form (admin_token_lines says which)."""
    keys = [
        {"key": "dns", "type": "edit"},
        {"key": "zone", "type": "read"},
        {"key": "workers_r2", "type": "edit"},
        {"key": "workers_scripts", "type": "edit"},
        {"key": "d1", "type": "edit"},
        {"key": "account_api_tokens", "type": "edit"},
        *([{"key": "access", "type": "edit"}, {"key": "access_acct", "type": "edit"}] if access else []),
    ]
    groups = urllib.parse.quote(json.dumps(keys, separators=(",", ":")))
    label = urllib.parse.quote(name)
    return f"https://dash.cloudflare.com/?to=/:account/api-tokens&permissionGroupKeys={groups}&name={label}"


# --- init step 3: the account-owned admin token, made by hand in the dashboard ----------------------------
# Cloudflare refuses to create a token holding token-management permissions (User API Tokens Edit, Account
# API Tokens Edit) through the API: `POST /user/tokens` or `/accounts/{id}/tokens` answers error 1001
# "sub-token is not allowed to have permissions to manage other tokens" (seen live 2026-11-01 on
# POST /accounts/{id}/tokens; not in the published docs). The admin token needs Account API Tokens Edit (it
# creates the bucket-only R2 credentials: init's state token, Terraform's backup token, `secrets rotate r2`),
# so it can never be minted: the admin creates it in the dashboard (Manage Account > Account API Tokens). The
# custom token builder doesn't offer User > API Tokens > Edit at all (create-via-api), so a hand-made
# user-owned admin token isn't possible either; an existing user-owned token in .env is still accepted.

SCOPE_ZONE = "com.cloudflare.api.account.zone"
SCOPE_ACCOUNT = "com.cloudflare.api.account"
SUB_TOKEN_ERROR = 1001  # "sub-token is not allowed to have permissions to manage other tokens"

# What the admin token must have, as the account token form names it (kind, permission, level, resource);
# check() probes each, audit_policies() compares the token's own policies with it. The form may say "Write"
# where this says "Edit" (the API's group names do: "DNS Write"); both mean the same.
ACCOUNT_TOKEN_PERMISSIONS = (
    ("Zone", "DNS", "Edit", "zone"),
    ("Zone", "Zone", "Read", "zone"),
    ("Account", "Workers R2 Storage", "Edit", "account"),
    ("Account", "Workers Scripts", "Edit", "account"),
    ("Account", "D1", "Edit", "account"),
    ("Account", "Account API Tokens", "Edit", "account"),
)
# panel.mode cloudflare: Access applications/policies, IdP, the organization's session
ACCOUNT_TOKEN_ACCESS = (
    ("Account", "Access: Apps and Policies", "Edit", "account"),
    ("Account", "Access: Organizations, Identity Providers, and Groups", "Edit", "account"),
)
PURPOSE = {
    "DNS": "the server's A/AAAA records and the watchdog's hostnames",
    "Zone": "find the zone and its account",
    "Workers R2 Storage": "the backup and Terraform state buckets",
    "Workers Scripts": "the watchdog Worker, its cron trigger and custom domain",
    "D1": "the watchdog's database",
    "Account API Tokens": "create the bucket-only R2 credentials: state, backups, `secrets rotate r2`",
    "Access: Apps and Policies": "the panel's Access application and policy",
    "Access: Organizations, Identity Providers, and Groups": "the panel's login: IdP, session length",
}


# Look-alikes the account token form also offers (a live account's grantable groups, listed with `mcctl doctor
# --cloudflare-permissions`, 2026-11-01): picking one of these instead of the row above was the live mistake.
# Keyed by the group name without its Read/Write suffix (lower case): the row to pick instead.
LOOKALIKES = {
    "access: apps": "Access: Apps and Policies",
    "access: policies": "Access: Apps and Policies",
    "access: organizations": "Access: Organizations, Identity Providers, and Groups",
    "access: identity providers": "Access: Organizations, Identity Providers, and Groups",
    "access: groups": "Access: Organizations, Identity Providers, and Groups",
    "dns firewall": "DNS",
    "dns view": "DNS",
    "account dns settings": "DNS",
    "zone dns settings": "DNS",
    "zone settings": "Zone",
}


def admin_token_lines(zone: str, *, access: bool) -> list[str]:
    """The exact account token form, step by step (printed by init; docs/setup.md says the same)."""
    perms = ACCOUNT_TOKEN_PERMISSIONS + (ACCOUNT_TOKEN_ACCESS if access else ())
    lines = [
        f"  Cloudflare dashboard > the account that owns {zone} > Manage Account > Account API Tokens",
        "  > Create Token > Custom token (Get started). Token name: anything, e.g. the one in the link.",
        f"  Permissions ({len(perms)} rows, nothing else). Each row has three dropdowns: Account/Zone, the",
        "  permission (exact name below), Read/Edit (the form may say Write for Edit):",
    ]
    lines += [f"    {kind} > {name} > {level}   ({PURPOSE[name]})" for kind, name, level, _ in perms]
    lines += [
        "  Look-alikes the form also offers; don't pick these:",
        "   - the two Zone rows need the FIRST dropdown set to Zone (the Account list has no DNS, Zone rows)",
        "   - Zone > DNS, not Zone DNS Settings, Account DNS Settings, DNS View or DNS Firewall",
        "   - Zone > Zone, not Zone Settings",
    ]
    if access:
        lines += [
            "   - the combined Access: Apps and Policies, not Access: Apps or Access: Policies",
            "   - the combined Access: Organizations, Identity Providers, and Groups,",
            "   - not Access: Organizations or Access: Identity Providers",
        ]
    lines += [
        f"  Account Resources (if the form shows them): Include > the account that owns {zone}",
        f"  Zone Resources: Include > Specific zone > {zone}   (not All zones)",
        "  Client IP Address Filtering: leave empty (CI runners have changing addresses)",
        "  TTL: start today, end about a year ahead (mcctl doctor warns 30 days before it expires)",
        "  > Continue to summary > Create Token",
    ]
    return lines


def norm_group(name: str) -> str:
    n = re.sub(r"\s+(write|edit)$", " write", name.strip().lower())
    return n.removeprefix("account ")  # "Account API Tokens Write" = "API Tokens Write"


def lookalike(name: str) -> str | None:
    """For a permission group name (API spelling, e.g. "Access: Apps Write"): the admin token row it was
    probably meant to be ("Account > Access: Apps and Policies > Edit"), or None if it isn't a look-alike."""
    base = re.sub(r"\s+(read|write|edit)$", "", name.strip().lower())
    instead = LOOKALIKES.get(base)
    if instead is None:
        return None
    row = next(r for r in ACCOUNT_TOKEN_PERMISSIONS + ACCOUNT_TOKEN_ACCESS if r[1] == instead)
    return f"{row[0]} > {row[1]} > {row[2]}"


def token_policies(token: str, account: str, token_id: str, transport: http.Transport) -> list[dict]:
    """An account-owned token's own policies (`GET /accounts/{id}/tokens/{token_id}`; API Tokens Read)."""
    resp = http.get(transport, f"{API}/accounts/{account}/tokens/{token_id}", _headers(token))
    if not _ok(resp):
        raise CloudflareError(f"reading token {token_id} failed ({_errors(resp)})")
    pols = (resp.json().get("result") or {}).get("policies")
    return pols if isinstance(pols, list) else []


def _resource_keys(resources: object) -> list[str]:
    if not isinstance(resources, dict):
        return []
    keys = []
    for k, v in resources.items():
        keys.append(str(k))
        keys += _resource_keys(v)
    return keys


def audit_policies(
    policies: list[dict], zone_id: str, account_id: str, *, access: bool
) -> tuple[list[str], list[str], list[str]]:
    """(missing, extra, scope) findings for an account-owned admin token against ACCOUNT_TOKEN_PERMISSIONS:
    permission groups it lacks, groups it has beyond them, and resources wider than the one zone/account."""
    wanted = {norm_group(f"{name} {level}") for _, name, level, _ in ACCOUNT_TOKEN_PERMISSIONS} | (
        {norm_group(f"{name} {level}") for _, name, level, _ in ACCOUNT_TOKEN_ACCESS} if access else set()
    )
    have: set[str] = set()
    scope: list[str] = []
    for pol in policies:
        if not isinstance(pol, dict) or pol.get("effect", "allow") != "allow":
            continue
        for g in pol.get("permission_groups") or []:
            if isinstance(g, dict) and g.get("name"):
                have.add(norm_group(str(g["name"])))
        for key in _resource_keys(pol.get("resources")):
            if key == f"{SCOPE_ZONE}.*" or key.endswith(".zone.*"):
                scope.append("all zones of the account (restrict it to the one zone)")
            elif key.startswith(f"{SCOPE_ZONE}.") and key != f"{SCOPE_ZONE}.{zone_id}":
                scope.append(f"another zone ({key.rsplit('.', 1)[1]})")
            elif (
                key.startswith(f"{SCOPE_ACCOUNT}.")
                and not key.startswith(SCOPE_ZONE)
                and (key != f"{SCOPE_ACCOUNT}.{account_id}")
            ):
                scope.append(f"another account ({key.rsplit('.', 1)[1]})")
    missing = sorted(wanted - have)
    return missing, sorted(have - wanted), sorted(set(scope))


# --- Registrar registration (doctor: expiry and auto-renew, when the admin token may read it) -------------
# Cloudflare API reference (2026-11-01): `GET /accounts/{id}/registrar/registrations/{domain}` -> `result`
# {auto_renew, expires_at, status}; a domain that isn't registered with Cloudflare answers 4xx "Domain not
# found" (code 10000). Account-owned tokens can't use Registrar, so auto-renew is switched on in the
# dashboard; doctor falls back to RDAP when this is refused.


def registration(token: str, account: str, domain: str, transport: http.Transport) -> dict | None:
    """The domain's Cloudflare Registrar registration, or None if it isn't registered with Cloudflare."""
    url = f"{API}/accounts/{account}/registrar/registrations/{urllib.parse.quote(domain)}"
    resp = http.get(transport, url, _headers(token))
    if _ok(resp) and isinstance(resp.json().get("result"), dict):
        return resp.json()["result"]
    if 400 <= resp.status < 500 and resp.status not in (401, 403):
        return None
    raise CloudflareError(f"reading the registration of {domain} failed ({_errors(resp)})")


# --- Zero Trust organization (init step "zero-trust", doctor) -----------------------------------------------
# `GET /accounts/{id}/access/organizations` -> `result` {auth_domain, name, session_duration, ...} (Cloudflare
# API reference, 2026-11-01). Creating the organization (POST) is documented, but Cloudflare's guides make the
# Zero Trust sign-up (plan selection, Free included, which may ask for a payment method) a dashboard step, so
# mcctl only detects it and links there, then checks its session_duration (set only when too long, below);
# Terraform only reads it (terraform/panel.tf data source).

ZERO_TRUST_URL = "https://one.dash.cloudflare.com/"


def zero_trust_org(token: str, account: str, transport: http.Transport) -> dict | None:
    """The account's Zero Trust organization, or None when the account hasn't signed up for Zero Trust."""
    resp = http.get(transport, f"{API}/accounts/{account}/access/organizations", _headers(token))
    result = resp.json().get("result") if _ok(resp) else None
    if isinstance(result, dict) and result.get("auth_domain"):
        return result
    if resp.status in (401, 403) and "auth" in _errors(resp).lower():
        raise CloudflareError(
            f"reading the Zero Trust organization failed ({_errors(resp)})",
            "the admin token needs Account > Access: Organizations, Identity Providers, and Groups > Edit",
        )
    return None


# The global session. Cloudflare docs (cloudflare-one/identity/users/session-management, 2026-11-01): the
# global session is 15 minutes to one month (default 24h); "when the application token expires, Cloudflare
# will automatically issue a new application token if the global token is still valid". So it doesn't
# shorten any application's own session (the panel keeps its 24h token); it bounds how long after the last
# real login (one-time PIN) Access may hand out a confirmation-app token silently. The Worker refuses
# confirmation tokens older than reauth_minutes, so a confirmed action means a PIN at most global +
# reauth_minutes ago
# (SECURITY.md "Re-auth"). mcctl wants it at most global_session_bounds()[1] and, only when it is longer, sets
# it to the target (an earlier target of reauth_minutes = 10m was below Cloudflare's minimum).
GLOBAL_SESSION_MIN_MINUTES = 15
GLOBAL_SESSION_DASHBOARD = (
    "Cloudflare dashboard > Zero Trust > Access controls > Access settings > "
    "Set your global session duration > Edit"
)


def global_session_bounds(reauth_minutes: int) -> tuple[int, int]:
    """(target, limit) in minutes for the organization's global session: mcctl sets the target when the
    current value exceeds the limit (or isn't a duration); anything up to the limit is accepted as set."""
    target = max(GLOBAL_SESSION_MIN_MINUTES, reauth_minutes)
    return target, max(2 * GLOBAL_SESSION_MIN_MINUTES, 2 * reauth_minutes)


def global_session_fix(reauth_minutes: int) -> str:
    target, limit = global_session_bounds(reauth_minutes)
    shown = f"{target // 60} hour(s)" if target % 60 == 0 and target >= 60 else f"{target} minutes"
    return f"{GLOBAL_SESSION_DASHBOARD}: choose {shown} (anything up to {limit} minutes is accepted)"


# `PUT /accounts/{id}/access/organizations` (Cloudflare API reference, "Update your Zero Trust organization";
# no PATCH exists) replaces the object, so mcctl sends back the organization as GET returned it with only
# session_duration changed. Live (2026-11-01, a freshly created organization) the API refused echoed objects
# with 400 code 12062 (access.api.error.invalid_org_config; "service_token_inactivity must include enabled,
# inactivity_threshold_days, and action"), so the body is cleaned first: timestamps dropped, nulls, empty
# strings and empty objects dropped (every top-level field is optional), and service_token_inactivity (whose
# three fields are required when it is sent; threshold 30-365 days, action disable|delete) sent only when it
# is enabled and complete: a disabled one is the default, so leaving it out loses nothing.
_ORG_READ_ONLY = ("created_at", "updated_at")
_TOKEN_INACTIVITY_FIELDS = ("enabled", "inactivity_threshold_days", "action")


def _pruned(obj: dict) -> dict:
    out = {k: _pruned(v) if isinstance(v, dict) else v for k, v in obj.items()}
    return {k: v for k, v in out.items() if v is not None and v != "" and v != {}}


def session_body(org: dict, duration: str) -> dict:
    """The PUT body for setting session_duration on the organization `org` (as GET returned it)."""
    body = _pruned({k: v for k, v in org.items() if k not in _ORG_READ_ONLY})
    sti = body.get("service_token_inactivity")
    if not (
        isinstance(sti, dict)
        and sti.get("enabled") is True
        and all(f in sti for f in _TOKEN_INACTIVITY_FIELDS)
    ):
        body.pop("service_token_inactivity", None)
    return body | {"session_duration": duration}


def set_session_duration(
    token: str, account: str, org: dict, duration: str, transport: http.Transport, *, fix: str = ""
) -> dict:
    """PUT the organization `org` (as GET returned it) with session_duration = `duration`; the result."""
    resp = http.send_json(
        transport,
        "PUT",
        f"{API}/accounts/{account}/access/organizations",
        session_body(org, duration),
        _headers(token),
    )
    result = resp.json().get("result") if _ok(resp) else None
    if not isinstance(result, dict):
        raise CloudflareError(
            f"setting the Zero Trust global session to {duration} failed ({_errors(resp)})",
            fix or f"{GLOBAL_SESSION_DASHBOARD}: set it there",
        )
    return result


def duration_minutes(value: object) -> float | None:
    """Go-style duration ("24h", "10m", "2h45m", "300ms") in minutes; None if it isn't one."""
    if not isinstance(value, str) or not value:
        return None
    parts = re.findall(r"([0-9.]+)(ms|us|µs|ns|h|m|s)", value)
    if not parts or "".join(a + b for a, b in parts) != value:
        return None
    scale = {"h": 60.0, "m": 1.0, "s": 1 / 60, "ms": 1 / 60000, "us": 0.0, "µs": 0.0, "ns": 0.0}
    return sum(float(n) * scale[u] for n, u in parts)
