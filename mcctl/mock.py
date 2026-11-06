"""`mcctl init --mock`: fake providers and tools, so the wizard runs end to end without touching anything real
(design §4 "Resume and failure handling", §8 P5: the tests and `.github/workflows/e2e-mock.yml`).

- `Cloud`: an `mcctl.http.Transport` answering the Cloudflare, R2 (S3), GitHub, Tailscale, Discord, Mojang,
  RDAP and watchdog-Worker endpoints the wizard uses, with state (buckets and tokens it "created"). Any other request fails.
- `Tools`: a runner for gh, tailscale, terraform (version only), ssh, ssh-keyscan and ssh-keygen. git runs for
  real in the local config repo, except for what would reach GitHub (push, fetch, clone).
- `FakeTerraform`: plan/apply/outputs with no Terraform at all (CI runs the real `terraform validate`).
- Bootstrap is the real `mcctl.bootstrap.run` with fake Terraform, playbook, SSH and port probes.

Nothing here is a credential: the fake tokens are fixed strings, the fake IDs are obviously fake.
"""

from __future__ import annotations

import base64
import datetime
import hashlib
import json
import re
import socket
import subprocess
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcctl import bootstrap, configrepo, dashlane, ghapp, hostnet, http, init, state, tf, yamledit
from mcctl.providers import cloudflare, rdap

ACCOUNT = "0" * 31 + "1"
# ui's fake value for the secret `cloudflare_api_token`: the account-owned admin token the admin "made by hand"
# (fake, like every value here); the fake API knows it with exactly the permissions mcctl asks for.
ADMIN_TOKEN = "mock-cloudflare_api_token"  # noqa: S105
ADMIN_ID = "mockadmin"
ACCOUNT_TOKEN_PREFIX = "mock-account-token-value-"  # noqa: S105 - values of the account-owned tokens the fake mints
ZONE_ID = "f" * 32
DISCORD_APP = "1300000000000000001"  # P14: the fake Discord application (its key is no key at all)
DISCORD_PUBLIC_KEY = "0" * 63 + "1"
TAILNET_IP = "100.100.1.1"
NOW = datetime.datetime(2026, 10, 31, 12, tzinfo=datetime.UTC)
# A fake App private key (not a key at all: the mock never signs anything).
MOCK_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\nbW9jay1hcHAta2V5LW5vdC1hLWtleQ==\n-----END RSA PRIVATE KEY-----\n"
)
GIT_IDENTITY = [
    "-c",
    "user.name=mcctl mock",
    "-c",
    "user.email=mock@mcctl.invalid",
    "-c",
    "commit.gpgsign=false",
]


def _ok(result: Any, status: int = 200) -> http.Response:  # noqa: ANN401
    body = {"success": True, "errors": [], "messages": [], "result": result}
    return http.Response(status, {"content-type": "application/json"}, json.dumps(body).encode())


def _json(data: Any, status: int = 200, headers: Mapping[str, str] | None = None) -> http.Response:  # noqa: ANN401
    return http.Response(
        status, {"content-type": "application/json", **(headers or {})}, json.dumps(data).encode()
    )


@dataclass
class Cloud:
    """Fake provider APIs (stateful)."""

    zone: str = "tests.mcctl.internal"
    buckets: set[str] = field(default_factory=set)
    tokens: dict[str, str] = field(default_factory=dict)  # id -> value
    revoked_ts_keys: set[str] = field(default_factory=set)
    head_bucket_status: int | None = None  # e.g. 403: an object-scoped token refused HeadBucket
    requests: list[http.Request] = field(default_factory=list)
    minted: int = 0  # Cloudflare tokens created so far (ids and values never repeat)
    deleted_values: set[str] = field(default_factory=set)  # Cloudflare tokens deleted (they stop verifying)
    admin_access: bool = (
        True  # the hand-made admin token includes the two Access rows (panel.mode cloudflare)
    )
    # account-owned tokens (values): /user/tokens/verify rejects them; their own policies by token id
    account_owned: set[str] = field(default_factory=set)
    token_policies: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    zero_trust: bool = True  # the account has signed up for Zero Trust
    zt_org: dict[str, Any] = field(
        default_factory=lambda: {
            "auth_domain": "mock-team.cloudflareaccess.com",
            "name": "mock-team",
            "session_duration": "24h",
            # a freshly created organization (live, 2026-11-01): echoing these back is refused
            "service_token_inactivity": {"enabled": False},
            "custom_pages": None,
            "created_at": "2026-11-01T10:00:00Z",
        }
    )
    zt_put_refused: bool = False  # the organization PUT fails whatever the body (the dashboard fix path)
    auto_renew: bool = False  # Cloudflare Registrar: the zone's auto-renew
    ts_clients: dict[str, str] = field(default_factory=dict)  # Tailscale OAuth clients created (id -> secret)
    ts_client_tags: dict[str, list[str]] = field(default_factory=dict)  # their tags
    gh_settings: dict[str, Any] = field(default_factory=dict)  # product-setup: repo settings by name
    # GitHub Apps made through the manifest flow (id -> slug) and their installations (slug -> (id, selection))
    gh_apps: dict[int, str] = field(default_factory=dict)
    gh_installs: dict[str, tuple[int, str]] = field(default_factory=dict)
    gh_install_repos: dict[int, list[str]] = field(default_factory=dict)  # installation -> repositories
    # P12 migrate/retire-old: the tailnet's devices ({id, name, hostname, addresses}); renames/deletes change it
    ts_devices: list[dict[str, Any]] = field(default_factory=list)
    # P14: the Discord application (its guild commands and interactions endpoint)
    discord_commands: dict[str, list[str]] = field(default_factory=dict)  # guild -> command names
    root: Path | None = None  # the config repo: the fake application's endpoint survives a resumed run there

    @property
    def discord_endpoint(self) -> str:
        path = self.root / ".mcctl" / "mock-discord.json" if self.root else None
        try:
            return str(json.loads(path.read_text())["endpoint"]) if path else ""
        except (OSError, ValueError, KeyError):
            return ""

    @property
    def worker_discord_key(self) -> str:
        """The public key in the fake Worker's DISCORD binding (FakeTerraform's last apply wrote it):
        the Worker answers Discord's verification PING only with it."""
        try:
            return (
                str(json.loads(worker_binding_file(self.root).read_text())["public_key"]) if self.root else ""
            )
        except (OSError, ValueError, KeyError, TypeError):
            return ""

    def _set_discord_endpoint(self, url: str) -> None:
        if self.root:
            (self.root / ".mcctl").mkdir(parents=True, exist_ok=True)
            (self.root / ".mcctl" / "mock-discord.json").write_text(json.dumps({"endpoint": url}))

    def __call__(self, req: http.Request) -> http.Response:
        self.requests.append(req)
        u = urllib.parse.urlsplit(req.url)
        path, query = u.path, urllib.parse.parse_qs(u.query)
        host = u.hostname or ""
        if host == "api.cloudflare.com":
            return self._cloudflare(req, path.removeprefix("/client/v4"), query)
        if host.endswith(".r2.cloudflarestorage.com"):
            bucket = path.strip("/").split("/")[0]
            auth = req.headers.get("Authorization", "")
            key_id = auth.split("Credential=", 1)[-1].split("/", 1)[0] if "Credential=" in auth else ""
            ok = bucket in self.buckets and key_id in self.tokens
            if req.method == "HEAD" and self.head_bucket_status is not None:
                return http.Response(self.head_bucket_status, {}, b"")
            return http.Response(200 if ok else 403, {}, b"")
        if host == "api.github.com":
            return self._github(req, path)
        if host == "api.tailscale.com":
            if path == "/api/v2/oauth/token":
                return _json(
                    {
                        "access_token": "mock-ts-access",
                        "token_type": "Bearer",
                        "expires_in": 3600,
                        "scope": "all",
                    }
                )
            if path == "/api/v2/tailnet/-/acl":
                return _json({"acls": []})
            bearer = req.headers.get("Authorization", "").removeprefix("Bearer ")
            api_key_id = bearer.split("-")[2] if bearer.startswith("tskey-api-") else None
            if path == "/api/v2/tailnet/-/devices":
                if api_key_id in self.revoked_ts_keys:
                    return _json({"message": "API token invalid"}, 401)
                return _json({"devices": self.ts_devices})
            dev = re.fullmatch(r"/api/v2/device/(\w+)(/name)?", path)
            if dev and api_key_id is not None:
                found = [d for d in self.ts_devices if d["id"] == dev.group(1)]
                if not found:
                    return _json({"message": "not found"}, 404)
                if req.method == "POST" and dev.group(2):
                    new = str(json.loads(req.body or b"{}").get("name", ""))
                    found[0]["name"] = f"{new}.mock.ts.net"
                    found[0]["hostname"] = new
                    return http.Response(200, {}, b"")
                if req.method == "DELETE" and not dev.group(2):
                    self.ts_devices.remove(found[0])
                    return http.Response(200, {}, b"")
            if path == "/api/v2/tailnet/-/keys" and req.method == "POST" and api_key_id is not None:
                body = json.loads(req.body or b"{}")
                if body.get("keyType") != "client" or not body.get("tags"):
                    return _json({"message": "bad request"}, 400)
                cid = f"kMOCKCLIENT{len(self.ts_clients) + 1}"
                self.ts_clients[cid] = f"tskey-client-{cid}-mockClientSecret"
                self.ts_client_tags[cid] = list(body["tags"])
                return _json(
                    {"id": cid, "key": self.ts_clients[cid], "keyType": "client", "scopes": body["scopes"]}
                )
            m = re.fullmatch(r"/api/v2/tailnet/-/keys/(\w+)", path)
            if m and req.method == "DELETE" and api_key_id is not None:
                self.revoked_ts_keys.add(m.group(1))
                return http.Response(200, {}, b"")
            if m and req.method == "GET":
                gone = m.group(1) in self.revoked_ts_keys
                return _json({"message": "not found"}, 404) if gone else _json({"id": m.group(1)})
        if host.startswith("hb.") and host.endswith(self.zone) and path == "/status":  # the watchdog Worker
            probe = {"ok": True, "last_ok": "2026-10-31T11:58:00.000Z", "failed_runs": 0, "error": ""}
            return _json(
                {
                    "ok": True,
                    "last_run": "2026-10-31T11:58:00.000Z",
                    "last_run_age_s": 120,
                    "maintenance": None,
                    "checks": {"health": {"state": "ok"}, "backup": {"state": "unknown"}},
                    "probes": {"v4": probe, "v6": probe},
                }
            )
        if host == "discord.com" and path.startswith("/api/webhooks/"):
            return _json({"id": "1", "type": 1, "name": "mock"})
        if host == "discord.com" and path.startswith("/api/v10/applications/"):
            return self._discord(req, path.removeprefix("/api/v10"))
        if host == "api.mojang.com" and path.startswith("/users/profiles/minecraft/"):
            name = path.rsplit("/", 1)[1]
            return _json({"id": hashlib.md5(name.lower().encode()).hexdigest(), "name": name})  # noqa: S324 - fake id
        if req.url == rdap.BOOTSTRAP:
            tld = self.zone.rsplit(".", 1)[1]
            return _json({"services": [[[tld], ["https://rdap.mock.invalid/"]]]})
        if host == "rdap.mock.invalid":
            return _json({"events": [{"eventAction": "expiration", "eventDate": "2028-01-01T00:00:00Z"}]})
        raise http.HttpError(f"mock: no fake for {req.method} {http.where(req.url)}")

    def _discord(self, req: http.Request, path: str) -> http.Response:
        """P14: Get/Edit Current Application, Bulk Overwrite Guild Application Commands (Bot auth)."""
        if not req.headers.get("Authorization", "").startswith("Bot mock-"):
            return _json({"message": "401: Unauthorized", "code": 0}, 401)
        app = {
            "id": DISCORD_APP,
            "name": "mock server",
            "verify_key": DISCORD_PUBLIC_KEY,
            "interactions_endpoint_url": self.discord_endpoint or None,
        }
        if path == "/applications/@me" and req.method == "GET":
            return _json(app)
        if path == "/applications/@me" and req.method == "PATCH":
            url = json.loads(req.body or b"{}").get("interactions_endpoint_url", "")
            # live: Discord PINGs it first; the Worker answers only with the DISCORD binding applied
            if not str(url).startswith("https://hb.") or self.worker_discord_key != DISCORD_PUBLIC_KEY:
                return _json(
                    {
                        "message": "Invalid Form Body",
                        "code": 50035,
                        "errors": {
                            "interactions_endpoint_url": {
                                "_errors": [
                                    {
                                        "code": "APPLICATION_INTERACTIONS_ENDPOINT_URL_INVALID",
                                        "message": "The specified interactions endpoint url could not be "
                                        "verified.",
                                    }
                                ]
                            }
                        },
                    },
                    400,
                )
            self._set_discord_endpoint(url)
            return _json(app | {"interactions_endpoint_url": url})
        m = re.fullmatch(rf"/applications/{DISCORD_APP}/guilds/(\d+)/commands", path)
        if m and req.method == "PUT":
            cmds = json.loads(req.body or b"[]")
            self.discord_commands[m.group(1)] = [c["name"] for c in cmds]
            return _json([{"id": str(i + 1), "application_id": DISCORD_APP, **c} for i, c in enumerate(cmds)])
        return _json({"message": "404: Not Found", "code": 0}, 404)

    def _cloudflare(self, req: http.Request, path: str, query: Mapping[str, list[str]]) -> http.Response:
        bearer = req.headers.get("Authorization", "").removeprefix("Bearer ")
        if bearer in self.deleted_values:
            return _json({"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]}, 401)
        if bearer.startswith(ACCOUNT_TOKEN_PREFIX) or bearer == ADMIN_TOKEN:
            self.account_owned.add(bearer)  # account-owned, also for a fresh fake (a second init run)
        if bearer == ADMIN_TOKEN and bearer not in self.tokens.values():  # made by hand in the dashboard
            self.tokens[ADMIN_ID] = ADMIN_TOKEN
            self.token_policies[ADMIN_ID] = self.admin_policies()
        own_id = next((i for i, v in self.tokens.items() if v == bearer), None)
        verify = {"id": own_id or "mock", "status": "active", "expires_on": "2027-10-01T00:00:00Z"}
        if path == "/user/tokens/verify" and bearer in self.account_owned:
            return _json({"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]}, 401)
        if path == f"/accounts/{ACCOUNT}/tokens/verify" and bearer not in self.account_owned:
            # a user-owned token doesn't verify under the account
            return _json({"success": False, "errors": [{"code": 1000, "message": "Invalid API Token"}]}, 401)
        if path == f"/accounts/{ACCOUNT}/tokens/verify" or path == "/user/tokens/verify":
            return _ok(verify)
        if path == f"/accounts/{ACCOUNT}/access/organizations":
            if not self.zero_trust:
                return _json({"success": False, "errors": [{"code": 12130, "message": "not found"}]}, 404)
            if req.method == "PUT":
                body = json.loads(req.body or b"{}")
                if problem := self.org_problem(body):  # as live: 400 code 12062 invalid_org_config
                    return _json({"success": False, "errors": [{"code": 12062, "message": problem}]}, 400)
                self.zt_org = body
            return _ok(self.zt_org)
        if path == f"/accounts/{ACCOUNT}/registrar/registrations/{self.zone}":
            reg = {"auto_renew": self.auto_renew, "expires_at": "2027-06-01T00:00:00Z", "status": "active"}
            return _ok(reg)
        if path == "/zones":
            if query.get("name") != [self.zone]:
                return _ok([])
            return _ok([{"id": ZONE_ID, "name": self.zone, "status": "active", "account": {"id": ACCOUNT}}])
        if path == f"/accounts/{ACCOUNT}/r2/buckets":
            if req.method == "POST":
                self.buckets.add(json.loads(req.body or b"{}")["name"])
                return _ok({"name": "created"})
            return _ok({"buckets": [{"name": b} for b in sorted(self.buckets)]})
        if path in (f"/accounts/{ACCOUNT}/workers/scripts", f"/accounts/{ACCOUNT}/d1/database"):
            return _ok([])
        if path.endswith("/tokens/permission_groups"):
            return _ok(self.permission_groups())
        if path in ("/user/tokens", f"/accounts/{ACCOUNT}/tokens") and req.method == "POST":
            names = {g["id"]: g["name"] for g in self.permission_groups()}
            wanted = [
                names.get(g["id"], "?")
                for pol in json.loads(req.body or b"{}").get("policies", [])
                for g in pol["permission_groups"]
            ]
            if any("API Tokens" in n for n in wanted):  # as live: no API-made token may manage tokens
                msg = "sub-token is not allowed to have permissions to manage other tokens"
                return _json({"success": False, "errors": [{"code": 1001, "message": msg}]}, 400)
            self.minted += 1
            token_id = f"mocktoken{self.minted:023d}"
            self.tokens[token_id] = f"mock-token-value-{self.minted}"
            if path.startswith("/accounts/"):  # account-owned: its own policies, with the groups' names
                self.tokens[token_id] = f"{ACCOUNT_TOKEN_PREFIX}{self.minted}"
                self.account_owned.add(self.tokens[token_id])
                self.token_policies[token_id] = [
                    {
                        **pol,
                        "permission_groups": [
                            {"id": g["id"], "name": names.get(g["id"], "?")} for g in pol["permission_groups"]
                        ],
                    }
                    for pol in json.loads(req.body or b"{}").get("policies", [])
                ]
            return _ok({"id": token_id, "value": self.tokens[token_id]})
        m = re.fullmatch(r"(?:/user|/accounts/\w+)/tokens/(\w+)", path)
        if m and req.method == "DELETE":
            if (value := self.tokens.pop(m.group(1), None)) is not None:
                self.deleted_values.add(value)
            return _ok({"id": m.group(1)})
        if m and req.method == "GET":
            return (
                _ok({"id": m.group(1), "policies": self.token_policies.get(m.group(1), [])})
                if m.group(1) in self.tokens
                else _json({"success": False, "errors": [{"code": 1003, "message": "not found"}]}, 404)
            )
        raise http.HttpError(f"mock: no fake for Cloudflare {req.method} {path}")

    @staticmethod
    def admin_groups(*, access: bool = True) -> list[tuple[str, str]]:
        """The admin token's permission groups as the API names them ("DNS Write") and their scope."""
        perms = cloudflare.ACCOUNT_TOKEN_PERMISSIONS + (cloudflare.ACCOUNT_TOKEN_ACCESS if access else ())
        return [
            (
                f"{name} Write" if level == "Edit" else f"{name} {level}",
                cloudflare.SCOPE_ZONE if res == "zone" else cloudflare.SCOPE_ACCOUNT,
            )
            for _, name, level, res in perms
        ]

    def org_problem(self, body: dict[str, Any]) -> str:
        """Why the fake refuses an organization PUT (the documented rules mcctl must satisfy), or ""."""
        if self.zt_put_refused:
            return "access.api.error.invalid_org_config"
        if not body.get("auth_domain") or not body.get("name"):  # PUT replaces: partial = refused
            return "incomplete"
        minutes = cloudflare.duration_minutes(body.get("session_duration"))
        if minutes is None or minutes < cloudflare.GLOBAL_SESSION_MIN_MINUTES:
            return "access.api.error.invalid_org_config: session_duration"
        if any(v is None for v in body.values()):
            return "access.api.error.invalid_org_config"
        sti = body.get("service_token_inactivity")
        if sti is not None and (
            not isinstance(sti, dict)
            or any(f not in sti for f in ("enabled", "inactivity_threshold_days", "action"))
            or sti.get("action") not in ("disable", "delete")
            or not 30 <= int(sti.get("inactivity_threshold_days") or 0) <= 365
        ):
            return "service_token_inactivity must include enabled, inactivity_threshold_days, and action"
        return ""

    @classmethod
    def permission_groups(cls) -> list[dict[str, Any]]:
        groups: list[dict[str, Any]] = [
            {"id": "mock-pg-r2-write", "name": cloudflare.R2_WRITE_GROUP},
            {"id": "x", "name": "Other"},
            # look-alikes the real form offers next to the rows mcctl needs (live list, 2026-11-01)
            {"id": "mock-pg-la-1", "name": "Access: Apps Write", "scopes": [cloudflare.SCOPE_ACCOUNT]},
            {"id": "mock-pg-la-2", "name": "Zone DNS Settings Write", "scopes": [cloudflare.SCOPE_ZONE]},
            {"id": "mock-pg-la-3", "name": "DNS View Read", "scopes": [cloudflare.SCOPE_ACCOUNT]},
        ]
        for name, scope in [*cls.admin_groups(), ("API Tokens Write", "com.cloudflare.api.user")]:
            slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
            groups.append({"id": f"mock-pg-{slug}", "name": name, "scopes": [scope]})
        return groups

    def admin_policies(self) -> list[dict[str, Any]]:
        """The hand-made admin token's own policies: exactly what admin_token_lines asks for."""
        ids = {g["name"]: g["id"] for g in self.permission_groups()}
        pols = []
        for scope, key in ((cloudflare.SCOPE_ZONE, ZONE_ID), (cloudflare.SCOPE_ACCOUNT, ACCOUNT)):
            groups = [n for n, s in self.admin_groups(access=self.admin_access) if s == scope]
            pols.append(
                {
                    "effect": "allow",
                    "resources": {f"{scope}.{key}": "*"},
                    "permission_groups": [{"id": ids[n], "name": n} for n in groups],
                }
            )
        return pols

    def install_app(self, slug: str, selection: str = "selected") -> str:
        """The "browser" installs the App (on the config repo; `selection` as chosen on GitHub)."""
        self.gh_installs[slug] = (4200 + len(self.gh_installs), selection)
        return str(self.gh_installs[slug][0])

    def _github(self, req: http.Request, path: str) -> http.Response:
        m = re.fullmatch(r"/app-manifests/mock-code-(panel|updater)-([a-z0-9-]+)/conversions", path)
        if m and req.method == "POST":  # the App manifest flow needs no authentication
            kind = ghapp.PANEL if m.group(1) == "panel" else ghapp.UPDATER
            slug = f"{m.group(2)}-{kind.key}"
            self.gh_apps[900 + len(slug)] = slug
            return _json(
                {
                    "id": 900 + len(slug),
                    "slug": slug,
                    "pem": MOCK_PEM,
                    "permissions": dict(kind.permissions),
                    "events": [],
                },
                201,
            )
        jwt = req.headers.get("Authorization", "").removeprefix("Bearer ").split(".")
        if len(jwt) == 3:  # as a GitHub App (init's resume and installation checks)
            claims = json.loads(base64.urlsafe_b64decode(jwt[1] + "=="))
            app = self.gh_apps.get(int(claims["iss"]))
            if app is None:
                return _json({"message": "A JSON web token could not be decoded"}, 401)
            kind = ghapp.PANEL if app.endswith("-panel") else ghapp.UPDATER
            if path == "/app":
                perms = dict(kind.permissions)
                return _json({"id": int(claims["iss"]), "slug": app, "permissions": perms, "events": []})
            m = re.fullmatch(r"/repos/([^/]+/[^/]+)/installation", path)
            if m and app in self.gh_installs:
                inst, selection = self.gh_installs[app]
                self.gh_install_repos[inst] = [m.group(1)] + (["mock/other"] if selection == "all" else [])
                return _json({"id": inst, "app_slug": app, "repository_selection": selection})
            m = re.fullmatch(r"/app/installations/(\d+)/access_tokens", path)
            if m and req.method == "POST" and int(m.group(1)) in self.gh_install_repos:
                return _json({"token": f"ghs_mock_{m.group(1)}", "expires_at": "2026-11-02T13:00:00Z"}, 201)
            return _json({"message": "Not Found"}, 404)
        bearer = req.headers.get("Authorization", "").removeprefix("Bearer ")
        if path.startswith("/installation/repositories") and bearer.startswith("ghs_mock_"):
            names = self.gh_install_repos.get(int(bearer.removeprefix("ghs_mock_")), [])
            return _json({"total_count": len(names), "repositories": [{"full_name": n} for n in names]})
        authed = "Authorization" in req.headers
        if not authed:
            return _json({"message": "Not Found"}, 404)  # a private product, seen anonymously
        expiry = {"github-authentication-token-expiration": "2027-10-31 00:00:00 UTC"}
        if re.fullmatch(r"/repos/[^/]+/[^/]+", path):
            settings = self.gh_settings.setdefault(path[7:], {})
            if req.method == "PATCH":  # repository settings (auto-merge, secret scanning)
                body = json.loads(req.body or b"{}")
                analysis = settings.setdefault("security_and_analysis", {})
                analysis.update(body.pop("security_and_analysis", {}))
                settings.update(body)
                return _json({"full_name": path[7:], **settings})
            owner, name = path[7:].split("/")
            repo = {
                "id": 800000001,
                "name": name,
                "full_name": path[7:],
                "private": True,
                "permissions": {"admin": True},
                "owner": {"login": owner, "id": 700000001, "type": "User"},
                "created_at": "2026-11-01T09:00:00Z",  # after 2026-07-15: immutable OIDC subject claims
            }
            return _json(repo | settings, headers=expiry)
        m = re.fullmatch(
            r"/repos/([^/]+/[^/]+)/(actions/permissions/access|actions/permissions/workflow|"
            r"vulnerability-alerts|automated-security-fixes|rulesets(?:/\d+)?)",
            path,
        )
        if m:
            return self._github_setting(req, self.gh_settings.setdefault(m.group(1), {}), m.group(2))
        if path.endswith("/actions/oidc/customization/sub"):
            return _json({"use_default": True})
        if path.endswith("/actions/secrets/public-key"):
            return _json({"key_id": "1", "key": "bW9jaw=="})
        if "/commits/" in path:
            return _json({"sha": path.rsplit("/", 1)[1]})
        if path.endswith("/contents/players.yml"):
            return _json({"sha": "blob", "content": "d2hpdGVsaXN0OiBbXQpvcHM6IFtdCg=="})
        if re.fullmatch(r"/repos/[^/]+/[^/]+/pulls(\?.*)?", path):
            return _json([])
        if path.endswith("/branches/main/protection"):
            return _json({"message": "Branch not protected"}, 404)
        if re.fullmatch(r"/apps/[a-z0-9-]+", path):  # the Apps are private: invisible to the admin's PAT
            return _json({"message": "Not Found"}, 404)
        raise http.HttpError(f"mock: no fake for GitHub {req.method} {path}")

    def _github_setting(self, req: http.Request, st: dict[str, Any], what: str) -> http.Response:
        body = json.loads(req.body or b"null")
        if what == "actions/permissions/access":
            if req.method == "PUT":
                st["access_level"] = body["access_level"]
                return http.Response(204, {}, b"")
            return _json({"access_level": st.get("access_level", "none")})
        if what == "actions/permissions/workflow":
            if req.method == "PUT":
                st["workflow"] = body
                return http.Response(204, {}, b"")
            return _json(
                st.get(
                    "workflow",
                    {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False},
                )
            )
        if what in ("vulnerability-alerts", "automated-security-fixes"):
            if req.method == "PUT":
                st[what] = True
                return http.Response(204, {}, b"")
            if what == "vulnerability-alerts":
                return http.Response(204 if st.get(what) else 404, {}, b"")
            return _json({"enabled": bool(st.get(what)), "paused": False})
        rulesets = st.setdefault("rulesets", {})
        if what == "rulesets" and req.method == "POST":
            rid = 100 + len(rulesets)
            rulesets[rid] = {"id": rid, **body}
            return _json(rulesets[rid], 201)
        if what == "rulesets":
            return _json([{"id": r["id"], "name": r["name"]} for r in rulesets.values()])
        rid = int(what.rsplit("/", 1)[1])
        if req.method == "PUT":
            rulesets[rid] = {"id": rid, **body}
        return _json(rulesets[rid])


def _proc(argv: Sequence[str], rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, out, err)


@dataclass
class Tools:
    """Fake gh / tailscale / terraform / ssh tools; real local git (identity added, GitHub unreachable)."""

    root: Path
    login: str = "mock-user"
    tailscale_login: str = "mock-user@github"
    access_level: str = "user"  # the product's Actions access setting (`none` until shared)
    product_owner_type: str = "User"  # GitHub account type of the product's owner
    created: set[str] = field(default_factory=set)
    calls: list[list[str]] = field(default_factory=list)
    # P12: what the fake server says (restic snapshots/keys, the restore marker, the live restore's result)
    snapshots: list[dict[str, Any]] = field(
        default_factory=lambda: [
            {
                "id": "a1b2c3d4" + "0" * 56,
                "short_id": "a1b2c3d4",
                "time": "2026-10-31T11:00:00Z",
                "tags": ["auto"],
                "paths": ["/opt/minecraft/server"],
            }
        ]
    )
    restored_from: str | None = None  # None: the latest snapshot's id
    restic_keys: list[dict[str, Any]] = field(
        default_factory=lambda: [{"id": "01d0" + "0" * 60, "current": True}]
    )
    stdin_seen: list[str] = field(default_factory=list)  # what run_input got (tests check it never leaks)
    extra_ipv6: list[str] = field(default_factory=list)  # more addresses the probed host has (a new machine)
    # P12 review: a backup (fence, pre-upgrade) adds a snapshot; server scripts containing one of these fail
    backup_appends: bool = False
    fail_scripts: list[str] = field(default_factory=list)
    # `mcctl backup now` / init's first backup: the unit's properties, and what the server says on stderr
    # (the real first setup: sudo warned "unable to resolve host" on every call)
    backup_result: str = "Result=success\nExecMainStatus=0"
    backup_stderr: str = ""
    # the config repo's origin/main (a sha): set by a push, read by `git fetch origin main` (None: unreachable)
    origin_main: str | None = None
    pre_upgrade: dict[str, Any] | None = None  # /var/lib/minecraft/pre-upgrade.json

    def _server(self) -> dict[str, Any]:
        try:
            text = (self.root / configrepo.SERVER_YML).read_text()
        except OSError:
            return {"host": {}, "name": None}
        host = yamledit.get_value(text, "host")
        name = yamledit.get_value(text, "name")
        return {"host": host if isinstance(host, dict) else {}, "name": name}

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        tool = argv[0]
        if tool == "git":
            if "push" in argv[1:4] and argv[-1] == "HEAD:main" and argv[1] == "-C":
                head = subprocess.run(
                    ["git", "-C", argv[2], "rev-parse", "HEAD"], capture_output=True, text=True
                )
                self.origin_main = head.stdout.strip() or self.origin_main
            if (
                "fetch" in argv[1:4]
                and argv[1] == "-C"
                and self.origin_main
                and argv[-2:] == ["origin", "main"]
            ):
                (Path(argv[2]) / ".git" / "FETCH_HEAD").write_text(
                    f"{self.origin_main}\t\tbranch 'main' of mock\n"
                )
                return _proc(argv, 0)
            if any(a in ("push", "fetch", "clone", "ls-remote") for a in argv[1:4]):
                return _proc(argv, 0 if "push" in argv else 1, "", "mock: GitHub isn't reachable")
            return subprocess.run(
                ["git", *GIT_IDENTITY, *argv[1:]], capture_output=True, text=True, check=False
            )
        if tool == "gh":
            return self._gh(argv)
        if tool == "tailscale" and argv[1:3] == ["status", "--json"]:
            srv = self._server()
            name = srv["host"].get("tailscale_name") or srv.get("name") or "mock"
            status = {
                "BackendState": "Running",
                "Self": {"UserID": 1, "DNSName": "laptop.mock.ts.net."},
                "User": {"1": {"LoginName": self.tailscale_login}},
                "Peer": {"nodekey:mock": {"DNSName": f"{name}.mock.ts.net.", "TailscaleIPs": [TAILNET_IP]}},
            }
            return _proc(argv, 0, json.dumps(status))
        if tool == "tailscale" and argv[1:2] == ["ping"]:
            return _proc(argv, 0, f"pong from {argv[-1]} ({TAILNET_IP}) via DERP(mock) in 10ms\n")
        if tool == "terraform" and argv[1:] == ["version", "-json"]:
            return _proc(argv, 0, json.dumps({"terraform_version": tf.TERRAFORM_VERSION}))
        if tool == "ssh-keyscan":
            return _proc(
                argv,
                0,
                f"{argv[-1]} ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMOCKmockMOCKmockMOCKmockMOCKmockMOCKmock\n",
            )
        if tool == "ssh-keygen":
            if "-F" in argv:
                return _proc(argv, 1)
            return _proc(argv, 0, "256 SHA256:MOCKmockMOCKmockMOCKmockMOCKmockMOCKmockMOCK (ED25519)\n")
        if tool == "ssh":
            if argv[-1] == init.PROBE_SCRIPT:  # as root over public SSH, or as deploy over the tailnet
                v6 = self._ip_json()
                out = (
                    'ID=debian\nVERSION_ID="13"\nNAME="Debian GNU/Linux"\n@@\nx86_64\n@@\nMemTotal:        8131452 kB\n@@\n'
                    + v6
                )
                return _proc(argv, 0, out)
            if argv[-1] == hostnet.COMMAND:  # mcctl doctor's "host ipv6" check
                return _proc(argv, 0, self._ip_json())
            return self._server_script(argv)
        if tool == "op" and argv[1:] in (["whoami"], ["item", "create", "-"]):
            return _proc(argv, 0, "{}")
        if tool == "bw" and argv[1:] == ["status"]:
            return _proc(argv, 0, json.dumps({"status": "unlocked"}))
        if tool == "bw" and argv[1:] == ["create", "item"]:
            return _proc(argv, 0, "{}")
        if tool in ("ssh-copy-id",):
            return _proc(argv, 0)
        return _proc(argv, 127, "", f"mock: unexpected command {tool}")

    def _ip_json(self) -> str:
        """`ip -j -6 addr show scope global` on the mock host: host.ipv6 plus extra_ipv6 (mcctl.hostnet)."""
        ipv6 = self._server()["host"].get("ipv6")
        addrs = [*([ipv6] if ipv6 else []), *self.extra_ipv6]
        info = [
            {
                "family": "inet6",
                "local": a,
                "prefixlen": 64,
                "scope": "global",
                "valid_life_time": 4294967295,
                "preferred_life_time": 4294967295,
            }
            for a in addrs
        ]
        return json.dumps([{"ifindex": 2, "ifname": "eth0", "addr_info": info}] if addrs else []) + "\n"

    def _server_script(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        """P12: the server side of migrate/restore/rotate (serverops' scripts), by their section markers."""
        script, sec = argv[-1], "@@mcctl:"
        out, err = "", ""
        if any(f in script for f in self.fail_scripts):
            return _proc(argv, 1, "", "mock: the server script failed")
        # remote.backup_now_command (init step 17, `mcctl backup now`)
        if f"{sec}backup" in script and "systemctl start mc-backup.service" in script:
            out += f"{sec}backup\nStartExit=0\n{self.backup_result}\nConditionResult=yes\n{sec}journal\nsnapshot saved\n"
            err = self.backup_stderr
        if self.backup_appends and "systemctl start mc-backup.service" in script:
            n = len(self.snapshots)
            sid = f"{n:08x}" + "e" * 56
            when = f"2026-11-01T{n:02d}:00:00Z"
            self.snapshots.append({"id": sid, "short_id": sid[:8], "time": when, "tags": ["auto"]})
        if f"{sec}snapshots" in script:
            out += f"{sec}snapshots\n{json.dumps(self.snapshots)}\n"
        if f"{sec}restored" in script:
            sid = self.restored_from or (self.snapshots[-1]["id"] if self.snapshots else "")
            out += f"{sec}restored\n" + json.dumps({"snapshot_id": sid, "short_id": sid[:8]}) + "\n"
        if f"{sec}preupgrade" in script:
            out += f"{sec}preupgrade\n" + (json.dumps(self.pre_upgrade) if self.pre_upgrade else "") + "\n"
        if f"{sec}keys" in script:
            out += f"{sec}keys\n{json.dumps(self.restic_keys)}\n"
        if f"{sec}journal" in script:
            out += f"{sec}journal\nSTEP 2/6 safety snapshot\nRESTORED\n{sec}result\nsuccess\n"
        if "restic cat config" in script:
            out += "REPO_OK\n"
        if script.startswith("mc list"):
            out += "There are 0 of a max of 20 players online:\n"
        if f"{sec}end" in script:
            out += f"{sec}end\n"
        return _proc(argv, 0, out, err)

    def run_input(self, argv: Sequence[str], stdin: str) -> subprocess.CompletedProcess[str]:
        self.stdin_seen.append(stdin)
        if "restic key add" in argv[-1]:
            self.restic_keys = [{"id": k["id"], "current": False} for k in self.restic_keys]
            self.restic_keys.append({"id": "9e70" + "0" * 60, "current": True})
        return self(argv)

    def _gh(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        args = argv[1:]
        if args[:2] == ["pr", "create"]:
            head = args[args.index("--head") + 1]
            return _proc(argv, 0, f"https://github.com/mock/pull/{abs(hash(head)) % 1000}\n")
        if args[:2] == ["auth", "status"]:
            return _proc(argv)
        if args[:2] == ["auth", "token"]:
            return _proc(argv, 0, "mock-gh-oauth-token\n")
        if args[:2] == ["api", "user"]:
            return _proc(argv, 0, self.login + "\n")
        if args[:2] == ["repo", "view"]:
            repo = args[2]
            if repo not in self.created:
                return _proc(argv, 1, "", "GraphQL: Could not resolve to a Repository")
            return _proc(argv, 0, json.dumps({"isPrivate": True, "url": f"https://github.com/{repo}"}))
        if args[:2] == ["repo", "create"]:
            self.created.add(args[2])
            return _proc(argv, 0, f"https://github.com/{args[2]}\n")
        if args[:1] == ["api"] and "/actions/permissions/access" in " ".join(args):
            if "PUT" in args:
                self.access_level = args[-1].removeprefix("access_level=")
                return _proc(argv)
            return _proc(argv, 0, json.dumps({"access_level": self.access_level}))
        if args[:1] == ["api"] and args[1].startswith("users/"):
            return _proc(argv, 0, json.dumps({"login": args[1][6:], "type": self.product_owner_type}))
        if args[:2] == ["run", "list"]:
            head = subprocess.run(
                ["git", "-C", str(self.root), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
            return _proc(argv, 0, json.dumps([{"databaseId": 4242, "headSha": head, "event": "push"}]))
        return _proc(argv, 1, "", f"mock: unexpected gh {' '.join(args[:2])}")


def worker_binding_file(root: Path) -> Path:
    """Where the fake Worker's applied DISCORD binding lives (it survives a resumed run, like Cloudflare)."""
    return root / ".mcctl" / "mock-worker.json"


class FakeTerraform:
    """Duck-types mcctl.tf.Terraform for the wizard: a plan with changes, an apply that only records the
    Worker's DISCORD binding (`root` set: the fake Discord verifies the endpoint against it)."""

    def __init__(self, log: list[str], fail_apply: list[str] | None = None) -> None:
        self.log = log
        self.fail_apply = fail_apply  # addresses that fail on the next apply (the rest are created)
        self.state: list[str] = []
        self._planned: list[str] = []
        self._var_file: Path | None = None
        self._targets: list[str] = []
        self.root: Path | None = None

    def plan(
        self, var_file: Path, out: Path, *, targets: Sequence[str] = (), replaces: Sequence[str] = ()
    ) -> tf.Plan:
        tf.private_file(out)
        self._var_file, self._targets = var_file, list(targets)
        self.log.append(
            "plan " + " ".join([*(f"-target={t}" for t in targets), *(f"-replace={r}" for r in replaces)])
        )
        if targets:
            plan = tf.Plan(create=list(targets))
        else:
            plan = tf.Plan(
                create=["cloudflare_r2_bucket.backups", "github_actions_secret.this"], replace=list(replaces)
            )
        self._planned = plan.create + plan.replace
        return plan

    def apply(self, plan_file: Path) -> None:
        plan_file.unlink(missing_ok=True)
        self.log.append("apply")
        failing = [a for a in self.fail_apply or [] if a in self._planned]
        self.state += [a for a in self._planned if a not in failing and a not in self.state]
        if failing:
            self.fail_apply = None  # the cause gets fixed before the re-run
            raise tf.TfError("`terraform apply` failed (see above). Fix it and run the command again.")
        self._apply_worker()

    def _apply_worker(self) -> None:
        if self.root is None or self._var_file is None:
            return
        if self._targets and "cloudflare_workers_script.watchdog" not in self._targets:
            return
        try:
            tfvars = json.loads(self._var_file.read_text())
        except (OSError, ValueError):
            return
        d = tfvars.get("discord_commands") or {}
        on = bool(d.get("enabled")) and tfvars.get("panel_mode", "cloudflare") == "cloudflare"
        path = worker_binding_file(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"public_key": d.get("public_key") if on else None}))

    def state_list(self) -> list[str]:
        return list(self.state)

    def outputs(self) -> dict[str, Any]:
        return {
            bootstrap.KEY_OUTPUT: "mock-tskey-auth",
            "restic_password": "mock-restic-password-not-a-secret",
        }


def _remember_apps(cloud: Cloud, root: Path) -> None:
    """GitHub keeps the Apps between runs: a fresh fake for a config repo whose state.json records them (an
    earlier run made and installed them) knows them, so a re-run verifies them as the App."""
    try:
        outputs = (state.load(root.expanduser().resolve()).get("github") or {}).get("outputs") or {}
    except (OSError, ValueError, state.StateError):
        return
    for key in ("panel", "updater"):
        slug, app_id = outputs.get(f"{key}_app"), str(outputs.get(f"{key}_app_id") or "")
        if isinstance(slug, str) and slug and app_id.isdigit():
            cloud.gh_apps[int(app_id)] = slug
            cloud.install_app(slug)


def deps(
    root: Path,
    *,
    login: str = "mock-user",
    cloud: Cloud | None = None,
    tf_log: list[str] | None = None,
    fake_tf: FakeTerraform | None = None,
) -> init.Deps:
    """init.Deps with every outside effect faked (the state of the fakes lives in the returned objects)."""
    if cloud is None:
        cloud = Cloud()
        _remember_apps(cloud, root)
    if cloud.root is None:
        cloud.root = root.expanduser().resolve()
    tools = Tools(root.expanduser().resolve(), login=login)
    tf_log = [] if tf_log is None else tf_log

    def terraform(
        repo: configrepo.ConfigRepo, product_root: Path, extra: Mapping[str, str]
    ) -> tuple[Any, Path]:
        out = repo.root / ".mcctl" / "rendered"
        from mcctl import render

        render.render(repo, out)  # the real renderer: placeholders and rendering errors still fail
        t = fake_tf or FakeTerraform(tf_log)
        t.root = repo.root
        return t, out

    def run_bootstrap(repo: configrepo.ConfigRepo, product_root: Path, ask: Callable[[str], str]) -> int:
        probes: dict[str, list[str]] = {}

        def prober(address: str, port: int) -> bootstrap.Probe:
            seen = probes.setdefault(address, [])
            seen.append(address)
            return "open" if len(seen) == 1 else "no-answer"  # open before the playbook, closed after

        return bootstrap.run(
            repo,
            product_root,
            ask=ask,
            runner=tools,
            prober=prober,
            terraform_factory=lambda: terraform(repo, product_root, {}),
            playbook=lambda argv, cwd, env: 0,
            pinned_collections=lambda root: root / ".mcctl" / "mock-collections",  # no ansible-galaxy
        )

    def ping(address: str, port: int, host: str) -> dict[str, Any]:
        return {"version": {"name": "Purpur 26.3 (mock)"}, "players": {"online": 0, "max": 20}}

    def resolve(host: str, family: int) -> set[str]:
        srv = tools._server()["host"]
        want = srv.get("ipv6") if family == socket.AF_INET6 else srv.get("ipv4")
        return {str(want)} if want else set()

    d = init.Deps(
        transport=cloud,
        run=tools,
        stream=lambda argv: 0,
        which=lambda name: None if name in ("op", "bw") else f"/mock/bin/{name}",  # no password manager
        now=lambda: NOW,
        sleep=lambda _s: None,
        resolve=resolve,
        terraform=terraform,
        bootstrap=run_bootstrap,
        ping=ping,
        probe=lambda a, p: "open",
        known_hosts=root.expanduser().resolve() / ".mcctl" / "mock-known_hosts",
        platform="linux",
        credentials_env={"PATH": "/usr/bin:/bin"},  # never the real shell's credentials
        os_release=lambda: {"ID": "debian"},
        # GitHub's App manifest flow: the "browser" returns a code the fake conversion endpoint knows
        app_create=lambda kind, name, repository, org, say: f"mock-code-{kind.key}-{name}",
        app_install=lambda slug, say, ask, poll: cloud.install_app(slug),
        app_sign=lambda pem, data: b"mock-signature",
        run_input=tools.run_input,
        dashlane=dashlane.Resolver(which=lambda _name: None),  # P14: no Dashlane CLI (like op/bw)
        ssh_public_key=lambda: "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMOCKmockMOCKmock mcctl-mock",
        ssh_identity=lambda: None,  # no private key to inspect (tests set one with fake ssh-keygen/ssh-add)
        ssh_auth_sock=lambda: None,
        collections=lambda product_root: product_root / ".mcctl" / "mock-collections",  # no ansible-galaxy
    )
    d.cloud, d.tools, d.tf_log = cloud, tools, tf_log  # type: ignore[attr-defined] - handles for tests
    return d
