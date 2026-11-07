"""`mcctl update-prs`: the weekly update task's pull requests (design §6, §1 "Upgrades", P11).

Runs in the config repo's scheduled `update` job, after `scripts/update_lock.py` rewrote the lock files in the
checkout (same Minecraft version only) and the boot test passed. It opens, with a short-lived token of the
**updater GitHub App** (mcctl/ghapp.py; its key is in the job's step env only, never on argv or in an output):

- **lock update**: the changed `server.lock.yml`/`plugins.lock.yml` on a branch `update/lock-<date>`;
  auto-merge when `automation.auto_merge` is `builds` or `builds+patch`.
- **product update**: when the product has a newer release than the pin, the workflow file's `uses:` line
  moved to that release's commit (`# vX.Y.Z`) on `update/product-vX.Y.Z`; auto-merge only for a patch release
  and `builds+patch`. A minor/major release waits for the admin (`mcctl upgrade` runs schema migrations, P12).

Auto-merge is GitHub's native one (GraphQL `enablePullRequestAutoMerge`, squash), enabled by the App, so the
merge commit is the App's and its push starts the deploy (a GITHUB_TOKEN merge wouldn't). Whether anything
merges is still decided by the required `server / gate` check (mcctl/gate.py), which re-verifies the change.
An older open update PR of the same kind with different content is closed and its branch deleted; one with
the same content is kept (nothing new is opened). Every request goes through `mcctl.http` (tests replay them).
"""

from __future__ import annotations

import base64
import datetime
import re
import urllib.parse
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mcctl import configrepo, ghapp, http
from mcctl.providers import ProviderError

API = "https://api.github.com"
LOCK_FILES = (
    configrepo.SERVER_LOCK,
    configrepo.PLUGINS_LOCK,
    configrepo.CLIENT_LOCK,
)  # v1.1: + the client pack
WORKFLOW = ".github/workflows/server.yml"
RELEASE = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


@dataclass
class Repo:
    """The config repo through one installation token."""

    repository: str
    token: str
    transport: http.Transport
    log: list[str] = field(default_factory=list)

    def call(self, method: str, path: str, body: object = None, ok: tuple[int, ...] = (200, 201)) -> Any:  # noqa: ANN401
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"{API}{path}"
        if body is None and method == "GET":
            resp = http.get(self.transport, url, headers)
        else:
            resp = http.send_json(self.transport, method, url, {} if body is None else body, headers)
        if resp.status not in ok:
            raise ProviderError(f"GitHub {method} {path.split('?')[0]}: HTTP {resp.status}")
        return resp.json()

    def graphql(self, query: str, variables: Mapping[str, Any]) -> Any:  # noqa: ANN401
        data = self.call("POST", "/graphql", {"query": query, "variables": dict(variables)})
        if not isinstance(data, dict) or data.get("errors"):
            msgs = [str(e.get("message", ""))[:120] for e in (data or {}).get("errors", [])]
            raise ProviderError(f"GitHub GraphQL: {'; '.join(msgs) or 'no data'}")
        return data["data"]

    def file(self, path: str, ref: str) -> tuple[str, str] | None:
        """(text, blob sha) of a file at ref, or None."""
        q = urllib.parse.quote(path)
        try:
            data = self.call("GET", f"/repos/{self.repository}/contents/{q}?ref={urllib.parse.quote(ref)}")
        except ProviderError:
            return None
        return base64.b64decode(data["content"]).decode(), str(data["sha"])

    def open_pulls(self, prefix: str) -> list[dict[str, Any]]:
        pulls = self.call("GET", f"/repos/{self.repository}/pulls?state=open&per_page=100") or []
        return [p for p in pulls if str((p.get("head") or {}).get("ref", "")).startswith(prefix)]

    def close(self, pr: dict[str, Any], why: str) -> None:
        number, branch = int(pr["number"]), str(pr["head"]["ref"])
        self.call("POST", f"/repos/{self.repository}/issues/{number}/comments", {"body": why})
        self.call("PATCH", f"/repos/{self.repository}/pulls/{number}", {"state": "closed"})
        self.call("DELETE", f"/repos/{self.repository}/git/refs/heads/{branch}", ok=(204, 200, 422))
        self.log.append(f"closed #{number} ({branch}): {why}")

    def propose(
        self,
        branch: str,
        files: Mapping[str, str],
        title: str,
        body: str,
        auto_merge: bool,
        base: str = "main",
    ) -> dict[str, Any]:
        """Branch from base, one commit per file (contents API), a pull request, and auto-merge if asked."""
        head = self.call("GET", f"/repos/{self.repository}/git/ref/heads/{base}")["object"]["sha"]
        self.call("POST", f"/repos/{self.repository}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
        for path, text in files.items():
            current = self.file(path, branch)
            self.call("PUT", f"/repos/{self.repository}/contents/{urllib.parse.quote(path)}", {
                "message": title if len(files) == 1 else f"{title} ({path})",
                "content": base64.b64encode(text.encode()).decode(),
                "branch": branch,
                **({"sha": current[1]} if current else {}),
            })  # fmt: skip
        pr = self.call("POST", f"/repos/{self.repository}/pulls",
                       {"title": title, "head": branch, "base": base, "body": body})  # fmt: skip
        self.log.append(f"opened #{pr['number']} {title}")
        if auto_merge:
            self.graphql(
                "mutation($id: ID!) { enablePullRequestAutoMerge("
                "input: {pullRequestId: $id, mergeMethod: SQUASH}) { clientMutationId } }",
                {"id": pr["node_id"]},
            )
            self.log.append(f"auto-merge on for #{pr['number']} (merges once `server / gate` passes)")
        return pr


def changed_locks(config_dir: Path, head_text: Callable[[str], str | None]) -> dict[str, str]:
    """Lock files in the checkout that differ from what main holds."""
    out = {}
    for name in LOCK_FILES:
        if not (config_dir / name).is_file():  # client.lock.yml exists only once the lock tool wrote it
            continue
        text = (config_dir / name).read_text()
        if head_text(name) != text:
            out[name] = text
    return out


def lock_summary(files: Mapping[str, str]) -> list[str]:
    lines = []
    if configrepo.SERVER_LOCK in files:
        s = (yaml.safe_load(files[configrepo.SERVER_LOCK]) or {}).get("server", {})
        lines.append(f"- server: {s.get('project')} {s.get('version')} build {s.get('build')}")
    if configrepo.PLUGINS_LOCK in files:
        for p in (yaml.safe_load(files[configrepo.PLUGINS_LOCK]) or {}).get("plugins", []):
            lines.append(f"- plugin: {p.get('name')} {p.get('version')}")
    if configrepo.CLIENT_LOCK in files:  # v1.1
        lock = yaml.safe_load(files[configrepo.CLIENT_LOCK]) or {}
        for f in lock.get("files", []):
            lines.append(f"- client: {f.get('name')} {f.get('version')}")
        for p in lock.get("pending", []):
            lines.append(f"- client: {p.get('slug')} pending ({p.get('reason')})")
    return lines


# The update pull requests read like the alerts (docs/operations.md "Alerts"): what changed, what it means for
# players, what (if anything) the admin does.
def lock_body(files: Mapping[str, str], auto_merge: bool) -> str:
    client = configrepo.CLIENT_LOCK in files
    players = (
        "Nothing to do for them. When this deploys, the server restarts once (an in-game countdown warns "
        "anyone online) and runs the newer builds: bug and security fixes, same Minecraft version."
    )
    if client:
        players += " The client pack changes too: players get the new files the next time they update it."
    todo = (
        "Nothing: the required `server / gate` check re-verifies every changed entry against the upstream "
        "APIs, then this merges by itself (automation.auto_merge) and the merge deploys."
        if auto_merge
        else "Review and merge it (auto-merge is off): the required `server / gate` check re-verifies every "
        "changed entry against the upstream APIs first, and the merge deploys."
    )
    return "\n".join([
        "**What changed:** newer builds for the same Minecraft version, boot-tested on the runner.", "",
        *lock_summary(files), "",
        f"**What it means for players:** {players}", "",
        f"**What to do:** {todo}",
    ])  # fmt: skip


def product_body(old: str, tag: str, repository: str, sha: str, patch: bool, auto_merge: bool) -> str:
    notes = f"https://github.com/{repository}/releases/tag/{tag}"
    if patch:
        kind = "A patch release (fixes only)."
        todo = (
            "Nothing: it merges by itself once `server / gate` passes (automation.auto_merge builds+patch), "
            "and the merge deploys."
            if auto_merge
            else f"Read the release notes ({notes}), then merge it; the merge deploys."
        )
    else:
        kind = "A minor/major release (new features, possibly new server.yml fields)."
        todo = (
            f"Read the release notes ({notes}), then run `mcctl upgrade -C <config> --to {tag}` (its own "
            "pull request, with the schema migrations), or merge this one as admin if the notes list none."
        )
    return "\n".join([
        f"**What changed:** the product {old} -> {tag} ({repository}@{sha[:12]}). {kind}", "",
        "**What it means for players:** usually nothing they notice; if the deploy changes a setting that "
        "needs a restart, the server restarts once with an in-game countdown.", "",
        f"**What to do:** {todo}",
    ])  # fmt: skip


def latest_tag(
    product: str, token: str, transport: http.Transport, *, timeout: float | None = None
) -> str | None:
    """The tag (vX.Y.Z) of the product's latest release (GitHub REST releases/latest; drafts and pre-releases
    excluded by GitHub), or None. `timeout`: seconds per socket operation (None: http.TIMEOUT)."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = transport(http.Request("GET", f"{API}/repos/{product}/releases/latest", headers, timeout=timeout))
    data = resp.json()
    tag = str(data.get("tag_name", "")) if isinstance(data, dict) else ""
    return tag if resp.status == 200 and RELEASE.fullmatch(tag) else None


def latest_release(product: str, token: str, transport: http.Transport) -> tuple[str, str] | None:
    """(tag, commit sha) of the product's latest release (`latest_tag`, then its commit), or None."""
    tag = latest_tag(product, token, transport)
    if tag is None:
        return None
    from mcctl import gate

    sha = gate.tag_sha(product, tag, token, transport)
    return (tag, sha) if sha else None


# A pin comment for an untagged product commit (mcctl/pinmove.py): N commits after release vX.Y.Z
DESCRIBED = re.compile(r"v(\d+)\.(\d+)\.(\d+)-(\d+)-g[0-9a-f]{7,40}")


def _order(version: str | None) -> tuple[int, int, int, int] | None:
    """Sort key of a pin comment: vX.Y.Z -> (X, Y, Z, 0); vX.Y.Z-N-g<sha> -> (X, Y, Z, N), after vX.Y.Z and
    before the next release (v0.0.0-N-g<sha>: before every release); anything else None."""
    if m := RELEASE.fullmatch(version or ""):
        return (*map(int, m.groups()), 0)  # type: ignore[return-value]
    if m := DESCRIBED.fullmatch(version or ""):
        return tuple(map(int, m.groups()))  # type: ignore[return-value]
    return None


def newer(a: str, b: str | None) -> bool:
    """Release `a` (vX.Y.Z) is newer than the pin comment `b` (a release, an untagged vX.Y.Z-N-g<sha>, or
    none/unknown: then any release is newer)."""
    if not RELEASE.fullmatch(a):
        return False
    kb = _order(b)
    ka = _order(a)
    return kb is None or (ka is not None and ka > kb)


def is_patch(old: str | None, new: str) -> bool:
    """Both plain releases, same X.Y, higher Z. An untagged pin (vX.Y.Z-N-g<sha>) is never a patch step."""
    mo, mn = RELEASE.fullmatch(old or ""), RELEASE.fullmatch(new)
    return bool(mo and mn and mo.groups()[:2] == mn.groups()[:2] and int(mn.group(3)) > int(mo.group(3)))


def bump_pin(workflow_text: str, repository: str, sha: str, version: str) -> str:
    pattern = re.compile(
        r"^(\s*(?:-\s+)?uses:\s*)"
        + re.escape(repository)
        + r"/\.github/workflows/server\.yml@[0-9a-f]{40}(\s*#.*)?$",
        re.M,
    )
    new, n = pattern.subn(
        lambda m: f"{m.group(1)}{repository}/.github/workflows/server.yml@{sha} # {version}", workflow_text
    )
    if n != 1:
        raise ProviderError("couldn't find exactly one `uses:` pin to move in the workflow file")
    return new


def run(
    config_dir: Path,
    repo: Repo,
    *,
    auto_merge: str,
    product_token: str,
    today: datetime.date,
    protected: bool = True,
) -> list[str]:
    """Open/refresh the lock and product update pull requests. Returns what happened. `protected` is
    github.protect_main: without the ruleset nothing requires `server / gate`, so auto-merge would merge at
    once; the PRs then wait for a human."""
    loaded = configrepo.load(config_dir)
    if not protected and auto_merge != "none":
        repo.log.append("github.protect_main is false: no auto-merge (no required gate check on main)")
        auto_merge = "none"
    main_text: dict[str, str | None] = {}

    def on_main(name: str) -> str | None:
        if name not in main_text:
            got = repo.file(name, "main")
            main_text[name] = got[0] if got else None
        return main_text[name]

    files = changed_locks(config_dir, on_main)
    stamp = today.strftime("%Y%m%d")
    if files:
        same = False
        for pr in repo.open_pulls("update/lock-"):
            branch = pr["head"]["ref"]
            if all((repo.file(n, branch) or ("", ""))[0] == t for n, t in files.items()):
                same = True
                repo.log.append(f"#{pr['number']} already proposes these builds")
            else:
                repo.close(pr, "Superseded by a newer weekly lock update.")
        if not same:
            merge = auto_merge in ("builds", "builds+patch")
            repo.propose(f"update/lock-{stamp}", files, "Weekly lock update", lock_body(files, merge),
                         auto_merge=merge)  # fmt: skip
    else:
        repo.log.append("lock files: already the newest builds")

    pin = loaded.pin
    release = latest_release(pin.repository, product_token, repo.transport)
    if release and newer(release[0], pin.version) and release[1] != pin.sha:
        tag, sha = release
        branch = f"update/product-{tag}"
        existing = repo.open_pulls("update/product-")
        if any(p["head"]["ref"] == branch for p in existing):
            repo.log.append(f"{branch}: already proposed")
        else:
            for pr in existing:
                repo.close(pr, f"Superseded by {tag}.")
            text = bump_pin((config_dir / WORKFLOW).read_text(), pin.repository, sha, tag)
            patch = is_patch(pin.version, tag)
            merge = patch and auto_merge == "builds+patch"
            body = product_body(pin.version or pin.sha[:12], tag, pin.repository, sha, patch, merge)
            repo.propose(branch, {WORKFLOW: text}, f"Product update to {tag}", body,
                         auto_merge=merge)  # fmt: skip
    else:
        repo.log.append(f"product: {pin.version or pin.sha[:12]} is the latest release")
    return repo.log


def main_from_env(config_dir: Path, env: Mapping[str, str], transport: http.Transport = http.urllib_transport,
                  sign: Callable[[str, bytes], bytes] = ghapp.openssl_sign,
                  today: datetime.date | None = None) -> list[str]:  # fmt: skip
    """The CLI half: the updater App from UPDATER_APP_ID / UPDATER_APP_KEY (PEM), the config repo from
    GITHUB_REPOSITORY, the product token from PRODUCT_TOKEN. Nothing is printed but what happened."""
    app_id, key, repository = (
        env.get("UPDATER_APP_ID", ""),
        env.get("UPDATER_APP_KEY", ""),
        env.get("GITHUB_REPOSITORY", ""),
    )
    if not app_id or not key:
        return [
            "no updater GitHub App (UPDATER_APP_ID/UPDATER_APP_KEY): no update pull requests (mcctl init)"
        ]
    if not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository):
        raise ProviderError("GITHUB_REPOSITORY isn't owner/repo")
    token = ghapp.installation_token(
        app_id, ghapp.decode_key(key), repository, ghapp.UPDATER_PERMISSIONS, transport, sign=sign
    )
    cfg = configrepo.load(config_dir).server
    repo = Repo(repository, token["token"], transport)
    return run(
        config_dir,
        repo,
        auto_merge=cfg.automation.auto_merge,
        protected=cfg.github.protect_main,
        product_token=env.get("PRODUCT_TOKEN", ""),
        today=today or datetime.datetime.now(datetime.UTC).date(),
    )
