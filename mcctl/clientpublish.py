"""v1.1 `mcctl client-publish`: fill the pinned artifact mirror and publish the client packs' plans.

Run by the deploy job before Ansible (the reusable workflow; bearer ARTIFACT_PUBLISH_TOKEN, an Actions
secret that
never reaches the server) or by hand (`terraform output -raw artifact_publish_token` in the environment
variable).
Against the watchdog Worker (https://hb.<hostname>.<zone>):

- every pinned artifact whose licence lets us redistribute it: the server jar (server.lock.yml), the plugins
  (plugins.lock.yml; Modrinth ones whose project licence is in client/pack.yml `redistributable`), restic (the
  product's pin), and with client.enabled the client lock's `mirror: true` files. For each: HEAD
  /publish/artifacts/<sha256>/<filename>; if missing, download it from upstream, check every pinned hash
  (sha256 and, where pinned, sha512/sha1/md5), then PUT it. The Worker never overwrites an artifact.
- with client.enabled: PUT /publish/client/<tier>.json (rendered plans) and /publish/client/index.json.
- v1.2, with client.enabled and a loader profile in the client lock: per tier the official launcher's zip
  (clientpack.launcher_zip: the Fabric profile + smallworld-<tier>/ with every mirrored file), stored like any
  artifact (artifacts/<sha256>/<Server>-<Tier>.zip, immutable) and named in index.json (`launcher.sha256`,
  `size_mb`, `recipe`). A zip whose recipe (clientpack.launcher_recipe) matches the published index's and is
  still stored is reused without downloading anything; a tier whose zip can't be built is left without one
  (the page then offers the .mrpack only) and counted as failed.

A hash mismatch fails the command (an upstream file changed under a pin: never mirror it). A download or
upload
that doesn't work is reported and skipped (the server and the launchers fall back to upstream by themselves).
Every request goes through an mcctl.http transport (tests replay them); the token is only a request header.
"""

from __future__ import annotations

import hashlib
import json
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import yaml

from mcctl import clientpack, http, render
from mcctl.configrepo import ConfigRepo

TOKEN_ENV = "ARTIFACT_PUBLISH_TOKEN"  # noqa: S105 - the variable's name
GROUP_VARS = clientpack.PRODUCT_ROOT / "ansible" / "group_vars" / "all" / "main.yml"
MODRINTH_API = "https://api.modrinth.com/v2"
RESTIC_URL = "https://github.com/restic/restic/releases/download/v{v}/restic_{v}_linux_{arch}.bz2"
# An artifact PUT's idle timeout: per connect, per http.BLOCKSIZE block of the body and per read, not for the
# whole upload (http.TIMEOUT is for small API calls). If the Worker answers before reading the whole body
# (401, 413, ...), http still reads that answer, so the failure line carries its status.
UPLOAD_TIMEOUT = 120.0
UPLOAD_ATTEMPTS = 3
MAX_ZIP_BYTES = 100 * 1048576  # = the Worker's MAX_ARTIFACT_BYTES (the Workers request body limit)
BACKOFF = 5.0  # seconds, doubled per attempt


class PublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class Artifact:
    what: str
    filename: str
    url: str
    hashes: dict[str, str] = field(default_factory=dict)  # algo -> hex; sha256 always
    path: str = ""  # v1.2: an in-house file in the product checkout (no upstream URL)

    @property
    def sha256(self) -> str:
        return self.hashes["sha256"]


def _modrinth_licence(transport: http.Transport, project: str) -> str:
    resp = http.get(transport, f"{MODRINTH_API}/project/{urllib.parse.quote(project)}")
    data = resp.json() if resp.status == 200 else None
    return str(((data or {}).get("license") or {}).get("id") or "")


def artifacts(repo: ConfigRepo, transport: http.Transport) -> tuple[list[Artifact], list[str]]:
    """What to mirror, and what is left out (with why)."""
    d = clientpack.load_definition()
    allowed = set(d["redistributable"])
    out: list[Artifact] = []
    skipped: list[str] = []
    s = repo.server_lock["server"]
    hashes = {"sha256": s["sha256"]} | ({"md5": s["md5"]} if s.get("md5") else {})
    out.append(Artifact(f"server {s['filename']}", s["filename"], s["url"], hashes))
    for p in repo.plugins_lock.get("plugins") or []:
        if p.get("source") != "modrinth":
            skipped.append(f"plugin {p['name']}: not on Modrinth (no licence record): upstream only")
            continue
        licence = _modrinth_licence(transport, str(p["project"]))
        if licence not in allowed:
            skipped.append(f"plugin {p['name']}: licence {licence or 'unknown'!r}: upstream only")
            continue
        out.append(Artifact(f"plugin {p['name']}", p["filename"], p["url"], {"sha256": p["sha256"]}))
    gv = yaml.safe_load(GROUP_VARS.read_text())
    for arch, sha in (gv.get("restic_sha256") or {}).items():
        v = gv["restic_version"]
        out.append(
            Artifact(
                f"restic {arch}",
                f"restic_{v}_linux_{arch}.bz2",
                RESTIC_URL.format(v=v, arch=arch),
                {"sha256": sha},
            )
        )
    if repo.server.client.enabled and repo.client_lock:
        for f in repo.client_lock["files"]:
            if not f.get("mirror"):
                skipped.append(f"client {f['slug']}: licence {f.get('licence')!r} (official URL only)")
                continue
            hashes = {"sha256": f["sha256"], "sha512": f["sha512"], "sha1": f["sha1"]}
            local = str(f.get("path") or "") if f.get("source") == "inhouse" else ""
            out.append(Artifact(f"client {f['slug']}", f["filename"], f["url"], hashes, local))
    return out, skipped


def fetch(transport: http.Transport, a: Artifact) -> http.Response:
    """The artifact's bytes: an in-house file from the product checkout, anything else from upstream."""
    if a.path:
        local = clientpack.PRODUCT_ROOT / a.path
        if not local.is_file():
            return http.Response(404, {}, b"")
        return http.Response(200, {}, local.read_bytes())
    return download(transport, a.url)


def download(transport: http.Transport, url: str, hops: int = 5) -> http.Response:
    """GET without credentials, following https redirects (GitHub release assets redirect to their CDN)."""
    resp = http.get(transport, url)
    while resp.status in (301, 302, 303, 307, 308) and hops > 0:
        nxt = urllib.parse.urljoin(url, resp.header("location") or "")
        if not nxt.startswith("https://"):
            break
        url, hops = nxt, hops - 1
        resp = http.get(transport, url)
    return resp


def verify(data: bytes, a: Artifact) -> None:
    for algo, want in a.hashes.items():
        got = hashlib.new(algo, data).hexdigest()
        if got != want:
            where = a.path or http.where(a.url)
            raise PublishError(f"{a.what}: {algo} of {where} is {got}, the lock pins {want}")


def _reason(resp: http.Response) -> str:
    """The Worker's short JSON `error` (it never echoes the token), for the failure line."""
    data = resp.json()
    text = str(data.get("error") or "") if isinstance(data, dict) else ""
    text = "".join(c for c in text if c.isprintable())[:160]
    return f" ({text})" if text else ""


def upload(
    transport: http.Transport,
    target: str,
    auth: dict[str, str],
    body: bytes,
    sleep: Callable[[float], None],
) -> str:
    """PUT one artifact, retrying timeouts and 5xx with backoff; "" when stored, else the last error."""
    headers = {**auth, "Content-Type": "application/octet-stream", "Content-Length": str(len(body))}
    error = ""
    for attempt in range(UPLOAD_ATTEMPTS):
        if attempt:
            sleep(BACKOFF * 2 ** (attempt - 1))
        try:
            put = transport(http.Request("PUT", target, headers, body, timeout=UPLOAD_TIMEOUT))
        except http.HttpError as e:
            error = str(e)
            continue
        if put.status == 200:
            return ""
        error = f"the Worker answered {put.status}{_reason(put)}"
        if put.status < 500 and put.status != 429:
            break  # a refusal: retrying won't change it
    return f"{error} (after {attempt + 1} attempt{'s' if attempt else ''})"


def publish(
    repo: ConfigRepo,
    base_url: str,
    token: str,
    transport: http.Transport,
    say: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, int]:
    if not token:
        raise PublishError(f"{TOKEN_ENV} isn't set (an Actions secret created by `mcctl apply`)")
    auth = {"Authorization": f"Bearer {token}"}
    base = base_url.rstrip("/")
    counts = {"present": 0, "uploaded": 0, "failed": 0, "plans": 0, "launchers": 0}
    blobs: dict[str, bytes] = {}
    items, skipped = artifacts(repo, transport)
    for line in skipped:
        say(f"  skip   {line}")
    for a in items:
        target = f"{base}/publish/artifacts/{a.sha256}/{urllib.parse.quote(a.filename)}"
        try:
            head = transport(http.Request("HEAD", target, auth))
        except http.HttpError as e:
            say(f"  FAIL   {a.what}: {e} (upstream stays the source)")
            counts["failed"] += 1
            continue
        if head.status == 200:
            counts["present"] += 1
            continue
        try:
            got = fetch(transport, a)
        except http.HttpError as e:
            say(f"  FAIL   {a.what}: {e} (upstream stays the source)")
            counts["failed"] += 1
            continue
        if got.status != 200:
            where = a.path or http.where(a.url)
            say(f"  FAIL   {a.what}: {where} answered {got.status} (upstream stays the source)")
            counts["failed"] += 1
            continue
        verify(got.body, a)  # raises: never mirror a file that isn't the pinned one
        if a.what.startswith("client "):
            blobs[a.sha256] = got.body  # v1.2: the launcher zips reuse it
        error = upload(transport, target, auth, got.body, sleep)
        if error:
            say(f"  FAIL   {a.what}: {error} (upstream stays the source)")
            counts["failed"] += 1
            continue
        counts["uploaded"] += 1
        say(f"  upload {a.what} ({len(got.body) / 1e6:.1f} MB)")
    rendered = {
        rel.name.replace(".plan.json", ".json"): json.loads(content)
        for rel, content in render.files(repo).items()
        if rel.parent == clientpack.PLAN_DIR and isinstance(content, str)
    }
    zips = launchers(repo, rendered, base, auth, transport, say, sleep, blobs, items, counts)
    for t in (rendered.get("index.json") or {}).get("tiers", []):
        if t["id"] in zips:
            t["launcher"] = {**t.get("launcher", {}), **zips[t["id"]]}
    for name, data in sorted(rendered.items(), key=lambda kv: kv[0] == "index.json"):  # the index last
        resp = transport(
            http.Request(
                "PUT",
                f"{base}/publish/client/{name}",
                {**auth, "Content-Type": "application/json"},
                json.dumps(data, separators=(",", ":")).encode(),
            )
        )
        if resp.status != 200:
            raise PublishError(f"publishing client/{name}: the Worker answered {resp.status}")
        counts["plans"] += 1
    say(
        f"mirror: {counts['present']} present, {counts['uploaded']} uploaded, {counts['failed']} failed; "
        f"client plans: {counts['plans']}, launcher zips: {counts['launchers']}"
    )
    return counts


def _published_index(transport: http.Transport, base: str, auth: dict[str, str]) -> dict[str, Any]:
    """The index.json the Worker has now ({} if none or unreadable): its launcher recipes."""
    try:
        resp = transport(http.Request("GET", f"{base}/publish/client/index.json", auth))
    except http.HttpError:
        return {}
    data = resp.json() if resp.status == 200 else None
    tiers = data.get("tiers") if isinstance(data, dict) else None
    return {str(t.get("id")): t.get("launcher") or {} for t in tiers or [] if isinstance(t, dict)}


def launchers(
    repo: ConfigRepo,
    rendered: dict[str, Any],
    base: str,
    auth: dict[str, str],
    transport: http.Transport,
    say: Callable[[str], None],
    sleep: Callable[[float], None],
    blobs: dict[str, bytes],
    items: list[Artifact],
    counts: dict[str, int],
) -> dict[str, dict[str, Any]]:
    """v1.2: build, store and describe each tier's official-launcher zip; {tier: launcher fields}."""
    if not (repo.server.client.enabled and repo.client_lock):
        return {}
    profile = (repo.client_lock.get("loader") or {}).get("profile")
    if not profile:
        say("  skip   launcher zips: the client lock has no loader profile yet (`mcctl lock update` adds it)")
        return {}
    server = str((rendered.get("index.json") or {}).get("server") or "")
    by_sha = {a.sha256: a for a in items}
    previous = _published_index(transport, base, auth)
    out: dict[str, dict[str, Any]] = {}
    for name, plan in sorted(rendered.items()):
        if name == "index.json":
            continue
        tier, filename = plan["tier"], plan["launcher"]["filename"]
        recipe = clientpack.launcher_recipe(plan, profile, server)
        prev = previous.get(tier) or {}
        if prev.get("recipe") == recipe and _is_sha256(str(prev.get("sha256", ""))):
            target = f"{base}/publish/artifacts/{prev['sha256']}/{urllib.parse.quote(filename)}"
            try:
                if transport(http.Request("HEAD", target, auth)).status == 200:
                    out[tier] = {k: prev[k] for k in ("recipe", "sha256", "size_mb") if k in prev}
                    counts["launchers"] += 1
                    continue
            except http.HttpError:
                pass
        try:
            for f in plan["files"]:
                sha = f["mirror"]
                if sha and sha not in blobs:
                    a = by_sha.get(sha)
                    got = fetch(transport, a) if a else None
                    if got is None or got.status != 200:
                        raise PublishError(f"{f['filename']} couldn't be downloaded")
                    verify(got.body, a)  # type: ignore[arg-type]
                    blobs[sha] = got.body
            data = clientpack.launcher_zip(plan, profile, server, blobs)
        except (PublishError, http.HttpError, clientpack.PackError) as e:
            say(f"  FAIL   launcher zip {tier}: {e} (the page offers the .mrpack only)")
            counts["failed"] += 1
            continue
        if len(data) > MAX_ZIP_BYTES:
            say(f"  FAIL   launcher zip {tier}: {len(data) / 1e6:.0f} MB is over the Worker's upload limit")
            counts["failed"] += 1
            continue
        sha = hashlib.sha256(data).hexdigest()
        target = f"{base}/publish/artifacts/{sha}/{urllib.parse.quote(filename)}"
        try:
            stored = transport(http.Request("HEAD", target, auth)).status == 200
        except http.HttpError:
            stored = False
        error = "" if stored else upload(transport, target, auth, data, sleep)
        if error:
            say(f"  FAIL   launcher zip {tier}: {error} (the page offers the .mrpack only)")
            counts["failed"] += 1
            continue
        out[tier] = {"recipe": recipe, "sha256": sha, "size_mb": round(len(data) / 1e6, 1)}
        counts["launchers"] += 1
        say(f"  zip    {filename} ({len(data) / 1e6:.1f} MB)")
    return out


def _is_sha256(text: str) -> bool:
    return clientpack.SHA256.fullmatch(text) is not None


def run(repo: ConfigRepo, env: dict[str, str], transport: http.Transport = http.urllib_transport) -> int:
    if repo.server.panel.mode != "cloudflare":
        print("client-publish: panel.mode isn't cloudflare (no Worker, no mirror): nothing to do")
        return 0
    if not env.get(TOKEN_ENV):
        fix = (
            f"run `mcctl apply`, then pass it in the config repo's workflow (`{TOKEN_ENV}: "
            f"${{{{ secrets.{TOKEN_ENV} }}}}` under `secrets:`; `mcctl doctor --fix` adds it)"
        )
        if repo.server.client.enabled:  # v1.2: the packs would silently go stale (mc-config, v1.1 rollout)
            print(f"::error::client-publish: {TOKEN_ENV} isn't set but client.enabled is true: {fix}")
            return 1
        # no client: only the mirror is missing, and the server's downloads fall back to upstream
        print(f"::warning::client-publish: {TOKEN_ENV} isn't set ({fix}): the artifact mirror isn't updated")
        return 0
    try:
        counts = publish(repo, render.watchdog_url(repo.server), env.get(TOKEN_ENV, ""), transport)
    except PublishError as e:
        print(f"client-publish: {e}")
        return 1
    if counts["failed"]:
        # not fatal: the server's downloads and the launchers fall back to upstream; the next deploy retries
        print(
            f"::warning::client-publish: {counts['failed']} artifact(s) weren't mirrored "
            "(upstream serves them; the next deploy retries)"
        )
    return 0
