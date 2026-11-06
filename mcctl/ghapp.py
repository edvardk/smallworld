"""The deployment's GitHub Apps (P10 review, P11), created with GitHub's App manifest flow.

Two Apps, each installed on the config repo only, with the least permissions their job needs:

- **panel** (cloudflare mode): Contents write + Pull requests write (+ Metadata read). mc-agent opens the
  panel's whitelist pull requests with it. Its private key goes to the Worker only (secret binding), which
  mints 1 h installation tokens narrowed to the config repo and those two permissions; the server never holds
  it.
- **updater** (P11): the same plus Workflows write. The weekly update job opens the lock and product update
  pull requests with it (a pull request opened with GITHUB_TOKEN starts no workflow, and GITHUB_TOKEN can't
  change a workflow file, which a product update must) and enables auto-merge. Its key is an Actions secret
  of the config repo that only the scheduled update job reads. It is a separate App because Workflows write
  must never sit with a key outside CI: whoever can push a workflow change to a branch can run it with the
  repository's secrets.

The config repo's rulesets (terraform/github.tf) keep both Apps away from main: changes only through pull
requests whose `server / gate` check passed (whitelist-only, verified lock updates, product patch bumps).

Manifest flow (GitHub docs "Registering a GitHub App from a manifest", REST "Create a GitHub App from a
manifest", both read 2026-11-01): the browser POSTs a form field `manifest` (JSON) to
https://github.com/settings/apps/new?state=<state> (an organization: /organizations/<org>/settings/apps/new);
GitHub redirects to the manifest's `redirect_url` with `?code=...&state=...`; `POST /app-manifests/{code}/
conversions` (no authentication; within one hour) answers 201 with `id`, `slug`, `pem`, `permissions`, ...
Installing: https://github.com/apps/<slug>/installations/new; GitHub then sends the browser to the manifest's
`setup_url` with `installation_id` and `setup_action=install`. A loopback `redirect_url` is [U] (allowed for
the browser redirect as far as the docs say; nothing restricts it).

App JWTs are signed by the Worker (WebCrypto, the panel App), by CI's `update` job (`mcctl update-prs` ->
mcctl/updates.py -> `installation_token` below, with openssl; the updater App; that job never runs the boot
test's jars, p11-review), and by `mcctl init` on the laptop, only to ask GitHub about the App it just made
(`GET /app` for the slug and permissions, `GET /repos/{repo}/installation`: is it installed, on selected
repositories? `GET /installation/repositories`: on the config repo only?), so a re-run finds an existing App
and its installation instead of making a second one. An existing App is never looked up by its public slug
(`GET /apps/{slug}`): the Apps are private, and GitHub answers 404 for them there. openssl gets the key on
stdin; without openssl init falls back to asking.

The browser half: one loopback server per run (`Loopback`) serves both the create page and the install
callback, because the manifest's `setup_url` is fixed when the App is created. If no callback arrives within a
minute (an App made by an older run points at a dead port; a browser on another machine), init polls the API
and asks for the installation id or the address-bar URL. The browser is opened by mcctl.desktop (under WSL:
the Windows browser, since Linux openers fail there).
"""

from __future__ import annotations

import base64
import html
import http.server
import json
import re
import secrets
import subprocess
import tempfile
import threading
import time
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcctl import http as mhttp
from mcctl.desktop import is_wsl, open_browser  # noqa: F401 (re-exported: ghapp.open_browser)
from mcctl.providers import CredentialCheck, ProviderError

API = "https://api.github.com"
PANEL_PERMISSIONS = {"contents": "write", "pull_requests": "write", "metadata": "read"}
UPDATER_PERMISSIONS = {
    "contents": "write",
    "pull_requests": "write",
    "workflows": "write",
    "metadata": "read",
}


@dataclass(frozen=True)
class Kind:
    key: str  # panel | updater
    permissions: Mapping[str, str]
    purpose: str

    @property
    def id_var(self) -> str:
        return f"TF_VAR_{self.key}_github_app_id"

    @property
    def key_var(self) -> str:
        return f"TF_VAR_{self.key}_github_app_private_key"


PANEL = Kind("panel", PANEL_PERMISSIONS, "the admin panel's whitelist pull requests")
UPDATER = Kind(
    "updater", UPDATER_PERMISSIONS, "the weekly update pull requests (lock files, product version)"
)


def manifest(kind: Kind, name: str, repository: str, redirect_url: str, setup_url: str) -> dict[str, Any]:
    """The App's manifest: private, no webhook events, exactly `kind.permissions`."""
    return {
        "name": f"{name}-{kind.key}"[:34],
        "url": f"https://github.com/{repository}",
        "description": f"mcctl: {kind.purpose} for {repository}. Install it on that repository only.",
        "hook_attributes": {"url": f"https://github.com/{repository}", "active": False},
        "redirect_url": redirect_url,
        "setup_url": setup_url,
        "setup_on_update": False,
        "public": False,
        "default_events": [],
        "default_permissions": dict(kind.permissions),
        "request_oauth_on_install": False,
    }


def new_app_url(owner: str, organization: bool, state: str) -> str:
    base = (
        f"https://github.com/organizations/{urllib.parse.quote(owner)}/settings/apps/new"
        if organization
        else "https://github.com/settings/apps/new"
    )
    return f"{base}?state={urllib.parse.quote(state)}"


def convert(code: str, transport: mhttp.Transport) -> dict[str, Any]:
    """Exchange the manifest flow's code for the App (id, slug, pem, permissions). Raises ProviderError."""
    if not code or not all(c.isalnum() or c in "-_" for c in code):
        raise ProviderError(
            "GitHub returned no usable code from the App manifest flow", "run `mcctl init` again"
        )
    resp = mhttp.send_json(
        transport,
        "POST",
        f"{API}/app-manifests/{code}/conversions",
        {},
        {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
    )
    data = resp.json()
    if resp.status != 201 or not isinstance(data, dict) or not data.get("pem") or not data.get("id"):
        raise ProviderError(
            f"GitHub refused the App manifest conversion (HTTP {resp.status})",
            "the code is valid for one hour and only once: run `mcctl init` again",
        )
    return data


def check_app(
    admin_token: str,
    slug: str,
    kind: Kind,
    transport: mhttp.Transport,
    permissions: Mapping[str, str] | None = None,
) -> CredentialCheck:
    """`GET /apps/{slug}` with the admin PAT (a private App needs authentication): exactly the permissions
    this product asks for, no more, and no webhook events. `permissions` (e.g. from the conversion) skips the
    GET."""
    check = CredentialCheck(f"github-app-{kind.key}")
    if permissions is None:
        resp = mhttp.get(
            transport,
            f"{API}/apps/{urllib.parse.quote(slug)}",
            {"Authorization": f"Bearer {admin_token}", "Accept": "application/vnd.github+json"},
        )
        data = resp.json()
        if resp.status != 200 or not isinstance(data, dict):
            return check.fail(f"GitHub App {slug} isn't visible (HTTP {resp.status})")
        permissions = data.get("permissions") or {}
        if data.get("events"):
            check.warn(f"GitHub App {slug} subscribes to webhook events it doesn't need: {data['events']}")
    want = dict(kind.permissions)
    got = dict(permissions)
    extra = {k: v for k, v in got.items() if want.get(k) != v and not (k == "metadata" and v == "read")}
    missing = {k: v for k, v in want.items() if got.get(k) != v}
    if extra:
        check.fail(
            f"GitHub App {slug} has more permissions than {kind.key} needs: {extra} (edit the App: {want})"
        )
    if missing:
        check.fail(f"GitHub App {slug} lacks {missing} (edit the App's permissions: {want})")
    if check.level != "fail":
        check.info(f"GitHub App {slug}: exactly {', '.join(f'{k} {v}' for k, v in sorted(want.items()))}")
    return check


def encode_key(pem: str) -> str:
    """The PEM as one base64 line (.env holds single-line values; Terraform base64decode()s it)."""
    return base64.b64encode(pem.encode()).decode()


def decode_key(value: str) -> str:
    text = value.strip()
    if "-----BEGIN" in text:
        return text + ("\n" if not text.endswith("\n") else "")
    return base64.b64decode(text).decode()


# --- the browser half (a loopback page and its callbacks) ---------------------------------------------------


class _Callbacks(http.server.BaseHTTPRequestHandler):
    # Browsers open speculative preconnect sockets that never send a request (seen with Chrome/Edge through
    # WSL's localhost forwarding): a socket timeout ends such a handler instead of it blocking forever.
    timeout = 5
    page = b""
    got: dict[str, dict[str, str]]  # path -> query
    event: threading.Event

    def log_message(self, *a: object) -> None:  # no request logging (the code is a one-time secret)
        pass

    def do_GET(self) -> None:
        parts = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(parts.query).items()}
        if parts.path == "/":
            body, ctype = self.page, "text/html; charset=utf-8"
        elif parts.path in ("/created", "/installed"):
            self.got[parts.path] = query
            self.event.set()
            body = (
                b"<!doctype html><meta charset=utf-8><title>mcctl</title><p>Done. Return to the terminal.</p>"
            )
            ctype = "text/html; charset=utf-8"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)


class Loopback:
    """One loopback HTTP server for the whole init run: the manifest's `redirect_url` (/created) and
    `setup_url` (/installed) name the same base URL, and GitHub redirects to it after the App is created and,
    later, after it is installed, so the server must outlive the create phase (it used to be one server per
    phase: the install redirect went to a closed port and init waited for nothing)."""

    def __init__(self) -> None:
        self.got: dict[str, dict[str, str]] = {}
        self.event = threading.Event()
        self.handler = type("H", (_Callbacks,), {"got": self.got, "event": self.event})
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), self.handler)
        self.srv.daemon_threads = True  # request threads never keep mcctl alive...
        self.srv.block_on_close = False  # ...nor make close() wait for an idle preconnect socket
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def show(self, page: bytes, say: Callable[[str], None], open_url: Callable[[str], Any]) -> None:
        self.handler.page = page
        url = self.base + "/"
        say(f"Opening {url} in your browser (open it yourself if nothing happens).")
        open_url(url)

    def wait(
        self,
        path: str,
        seconds: float,
        poll: Callable[[], str | None] | None = None,
        poll_every: float = 5,
    ) -> dict[str, str]:
        """The query of the first `path` callback within `seconds` ({} if none). `poll` (every `poll_every`
        seconds) may find the result another way (the installation, through the API): then
        {"installation_id": ...}."""
        deadline = time.monotonic() + seconds
        next_poll = time.monotonic() + poll_every
        while path not in self.got and time.monotonic() < deadline:
            self.event.wait(min(1, poll_every))
            self.event.clear()
            if poll is not None and time.monotonic() >= next_poll:
                next_poll = time.monotonic() + poll_every
                if found := poll():
                    return {"installation_id": found}
        return self.got.pop(path, {})

    def close(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()


_LOOPBACK: Loopback | None = None


def loopback() -> Loopback:
    """The run's loopback server (started on first use, lives until mcctl exits)."""
    global _LOOPBACK
    if _LOOPBACK is None:
        _LOOPBACK = Loopback()
    return _LOOPBACK


def browser_create(
    kind: Kind,
    name: str,
    repository: str,
    organization: bool,
    say: Callable[[str], None],
    open_url: Callable[[str], Any] = open_browser,
    timeout: float = 900,
    server: Loopback | None = None,
) -> str:
    """Run the manifest flow in the browser: a loopback page with one button that POSTs the manifest to
    GitHub; returns the code GitHub redirects back with (state checked)."""
    state = secrets.token_urlsafe(24)
    owner = repository.split("/")[0]
    lb = server or loopback()
    m = json.dumps(manifest(kind, name, repository, f"{lb.base}/created", f"{lb.base}/installed"))
    page = (
        "<!doctype html><meta charset=utf-8><title>mcctl: create a GitHub App</title>"
        f"<p>Create the GitHub App for {html.escape(kind.purpose)} ({html.escape(repository)}).</p>"
        f'<form method="post" action="{html.escape(new_app_url(owner, organization, state))}">'
        f'<input type="hidden" name="manifest" value="{html.escape(m)}">'
        "<button type=submit>Create the GitHub App on GitHub</button></form>"
    ).encode()
    lb.show(page, say, open_url)
    query = lb.wait("/created", timeout)
    if not query:
        raise ProviderError("timed out waiting for the browser", "run `mcctl init` again")
    if query.get("state") != state:
        raise ProviderError(
            "the App manifest callback had the wrong state (not from this run)", "run `mcctl init` again"
        )
    return query.get("code", "")


INSTALLATION_URL = re.compile(r"(?:installation_id=|/installations/)(\d+)")


def parse_installation(text: str) -> str | None:
    """An installation id from what the user pastes: the id itself, the address bar after installing
    (`...?installation_id=123&setup_action=install`) or the installation's settings URL
    (`.../installations/123`)."""
    text = text.strip()
    if text.isdigit():
        return text
    m = INSTALLATION_URL.search(text)
    return m.group(1) if m else None


def browser_install(
    slug: str,
    say: Callable[[str], None],
    ask: Callable[[], str],
    poll: Callable[[], str | None] | None = None,
    open_url: Callable[[str], Any] = open_browser,
    quiet: float = 60,
    timeout: float = 900,
    server: Loopback | None = None,
    poll_every: float = 5,
) -> str | None:
    """Send the browser to the App's install page; returns the installation id: from the setup redirect to the
    run's loopback server, from `poll` (the API: the App's installation on the config repo), or, after `quiet`
    seconds without either, from what the user pastes (the id or the address-bar URL; empty waits again).
    None after `timeout`."""
    install = f"https://github.com/apps/{urllib.parse.quote(slug)}/installations/new"
    lb = server or loopback()
    page = (
        "<!doctype html><meta charset=utf-8><title>mcctl: install the GitHub App</title>"
        f'<p><a href="{html.escape(install)}">Install {html.escape(slug)}</a>: choose <b>Only select '
        "repositories</b> and pick the config repo, nothing else.</p>"
    ).encode()
    lb.show(page, say, open_url)
    say(f"(or open {install} directly)")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        query = lb.wait("/installed", min(quiet, max(deadline - time.monotonic(), 0)), poll, poll_every)
        inst = query.get("installation_id", "")
        if inst.isdigit():
            return inst
        if time.monotonic() >= deadline:
            break
        say(
            "Nothing back from GitHub yet. If GitHub shows the App as installed, paste the address bar's URL "
            "or the installation id (Settings > Applications > the App > Configure: .../installations/<id>)."
        )
        if found := parse_installation(ask()):
            return found
    return None


# --- installation tokens for CI (`mcctl update-prs`): openssl signs; only the public input touches disk ----


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def jwt_input(app_id: str, now: float) -> str:
    t = int(now)
    header = b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    claims = b64url(
        json.dumps({"iat": t - 60, "exp": t + 540, "iss": str(app_id)}, separators=(",", ":")).encode()
    )
    return f"{header}.{claims}"


def openssl_sign(
    pem: str, data: bytes, run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
) -> bytes:
    """RS256 (RSASSA-PKCS1-v1_5 / SHA-256) with the system's openssl: the key goes in on stdin, never on argv
    or disk; the (public) signing input is a temporary file."""
    with tempfile.TemporaryDirectory(prefix="mcctl-jwt-") as d:
        msg = Path(d) / "input"
        msg.write_bytes(data)
        proc = run(
            ["openssl", "dgst", "-sha256", "-sign", "/dev/stdin", "-binary", str(msg)],
            input=pem.encode(),
            capture_output=True,
            check=False,
            timeout=30,
        )
    if proc.returncode != 0 or not proc.stdout:
        raise ProviderError("openssl couldn't sign the GitHub App JWT", "check the App's private key")
    return proc.stdout


def app_jwt(app_id: str, pem: str, now: float, sign: Callable[[str, bytes], bytes] = openssl_sign) -> str:
    data = jwt_input(app_id, now)
    return f"{data}.{b64url(sign(pem, data.encode()))}"


def installation_token(
    app_id: str,
    pem: str,
    repository: str,
    permissions: Mapping[str, str],
    transport: mhttp.Transport,
    now: float | None = None,
    sign: Callable[[str, bytes], bytes] = openssl_sign,
) -> dict[str, Any]:
    """A 1 h installation token for `repository` only, narrowed to `permissions` (GitHub REST: Get a
    repository installation for the authenticated app; Create an installation access token)."""
    headers = _jwt_headers(app_id, pem, sign, now)
    resp = mhttp.get(transport, f"{API}/repos/{repository}/installation", headers)
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict) or not isinstance(data.get("id"), int):
        raise ProviderError(
            f"the GitHub App isn't installed on {repository} (HTTP {resp.status})",
            "install it on the config repo (mcctl init, or the App's settings page)",
        )
    resp = mhttp.send_json(
        transport,
        "POST",
        f"{API}/app/installations/{data['id']}/access_tokens",
        {"repositories": [repository.split("/", 1)[1]], "permissions": dict(permissions)},
        headers,
    )
    token = resp.json()
    if resp.status != 201 or not isinstance(token, dict) or not isinstance(token.get("token"), str):
        raise ProviderError(
            f"GitHub refused an installation token (HTTP {resp.status})", "check the App's permissions"
        )
    return {"token": token["token"], "expires_at": str(token.get("expires_at", ""))}


# --- the App's own view (an App JWT signed with openssl on stdin): resume and installation discovery --------


def _jwt_headers(
    app_id: str, pem: str, sign: Callable[[str, bytes], bytes], now: float | None
) -> dict[str, str]:
    jwt = app_jwt(app_id, pem, time.time() if now is None else now, sign)
    return {
        "Authorization": f"Bearer {jwt}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def app_slug(
    app_id: str,
    pem: str,
    transport: mhttp.Transport,
    sign: Callable[[str, bytes], bytes] = openssl_sign,
    now: float | None = None,
) -> str | None:
    """`GET /app` (App JWT): the App's slug, or None if GitHub doesn't accept the id/key pair."""
    resp = mhttp.get(transport, f"{API}/app", _jwt_headers(app_id, pem, sign, now))
    data = resp.json()
    if resp.status == 200 and isinstance(data, dict) and isinstance(data.get("slug"), str):
        return data["slug"]
    return None


class KeyRejected(ProviderError):
    """GitHub doesn't accept the saved id/private-key pair (the key isn't the App's, or the App is gone)."""


def verify_app(
    app_id: str,
    pem: str,
    kind: Kind,
    transport: mhttp.Transport,
    sign: Callable[[str, bytes], bytes] = openssl_sign,
    now: float | None = None,
) -> CredentialCheck:
    """An existing App, checked as the App itself: `GET /app` with a JWT signed by the saved key (GitHub REST
    "Get the authenticated app", 2026-11-02: JWT only). The manifest makes the App private (`public: false`),
    and `GET /apps/{slug}` answers 404 for a private App to anyone but the App's own credentials (seen live
    with the admin's fine-grained PAT), so the id, slug, permissions and events come from here. facts["slug"]
    is the App's slug. Raises KeyRejected when GitHub refuses the JWT (401/404)."""
    resp = mhttp.get(transport, f"{API}/app", _jwt_headers(app_id, pem, sign, now))
    data = resp.json()
    if resp.status in (401, 403, 404):
        said = str(data.get("message", ""))[:200] if isinstance(data, dict) else ""
        raise KeyRejected(
            f"GitHub doesn't accept the saved private key for the {kind.key} App (id {app_id}; HTTP "
            f"{resp.status}{f': {said}' if said else ''}): the key doesn't belong to that App, or the App "
            "was deleted",
            "if GitHub's message is about the 'iat'/'exp' claims, this computer's clock is off: sync it and "
            "re-run. Otherwise delete the App in GitHub > Settings > Developer settings > GitHub Apps (if it "
            f"still exists), remove the {kind.id_var} and {kind.key_var} lines from the config repo's .env, "
            "then re-run",
        )
    if resp.status != 200 or not isinstance(data, dict) or not isinstance(data.get("slug"), str):
        raise ProviderError(f"GitHub didn't answer `GET /app` for the {kind.key} App (HTTP {resp.status})")
    slug = data["slug"]
    if str(data.get("id")) != str(app_id):
        raise KeyRejected(
            f"the saved private key belongs to GitHub App {slug} (id {data.get('id')}), not to "
            f"{kind.id_var}={app_id}",
            f"fix {kind.id_var} in the config repo's .env (or delete both lines and the App, then re-run)",
        )
    check = check_app("", slug, kind, transport, permissions=data.get("permissions") or {})
    if data.get("events"):
        check.warn(f"GitHub App {slug} subscribes to webhook events it doesn't need: {data['events']}")
    check.facts["slug"] = slug
    return check


def installation_repositories(
    app_id: str,
    pem: str,
    installation_id: int,
    transport: mhttp.Transport,
    sign: Callable[[str, bytes], bytes] = openssl_sign,
    now: float | None = None,
) -> tuple[int, list[str]]:
    """Which repositories the installation can reach: an installation token (no `repositories` narrowing: it
    covers every repository the installation was granted; only Metadata read) and `GET /installation/
    repositories` with it (GitHub REST "Create an installation access token for an app", "List repositories
    accessible to the app installation", 2026-11-02). Returns (total_count, full names of the first 100)."""
    resp = mhttp.send_json(
        transport,
        "POST",
        f"{API}/app/installations/{int(installation_id)}/access_tokens",
        {"permissions": {"metadata": "read"}},
        _jwt_headers(app_id, pem, sign, now),
    )
    token = resp.json()
    if resp.status != 201 or not isinstance(token, dict) or not isinstance(token.get("token"), str):
        raise ProviderError(f"GitHub refused an installation token (HTTP {resp.status})")
    resp = mhttp.get(
        transport,
        f"{API}/installation/repositories?per_page=100",
        {
            "Authorization": f"Bearer {token['token']}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict) or not isinstance(data.get("total_count"), int):
        raise ProviderError(f"GitHub didn't list the installation's repositories (HTTP {resp.status})")
    names = [str(r.get("full_name")) for r in data.get("repositories") or [] if isinstance(r, dict)]
    return data["total_count"], names


def repo_installation(
    app_id: str,
    pem: str,
    repository: str,
    transport: mhttp.Transport,
    sign: Callable[[str, bytes], bytes] = openssl_sign,
    now: float | None = None,
) -> dict[str, Any] | None:
    """`GET /repos/{owner}/{repo}/installation` (App JWT; GitHub REST "Get a repository installation for the
    authenticated app", 2026-11-01): {id, repository_selection: all|selected, ...}, or None if the App isn't
    installed on the repository."""
    resp = mhttp.get(
        transport, f"{API}/repos/{repository}/installation", _jwt_headers(app_id, pem, sign, now)
    )
    data = resp.json()
    if resp.status == 200 and isinstance(data, dict) and isinstance(data.get("id"), int):
        return data
    if resp.status == 404:
        return None
    raise ProviderError(f"GitHub didn't answer the App's installation lookup (HTTP {resp.status})")
