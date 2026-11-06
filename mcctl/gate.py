"""`mcctl pr-gate`: the config repo's required `server / gate` check (P10 review, P11).

Whatever reaches main is deployed, and two GitHub Apps can push branches and open pull requests (the
panel's, whose key is in the Worker, and the updater, in CI). The ruleset on main (terraform/github.tf)
requires this check, pinned to GitHub Actions, and no App bypasses it; only the admin does. So the check
passes only for a change that may merge without a human, decided by the diff between the base and the head,
the head branch AND who sent it. GitHub doesn't let an App be a ruleset bypass actor on a personal account's
repository (first live apply: 422), so the "branches" ruleset leaves `panel/**` and `update/**` open to both
installed Apps. What binds a policy to its App is the pull request's author and the event's sender
(`--author` github.event.pull_request.user.login, `--sender` github.event.sender.login): both must be the
App's bot user `<slug>[bot]`, the slug from server.yml `github.panel_app` / `github.updater_app` as of the
BASE. Bot logins can't be taken by a user or another App, and a push by the other App to the branch makes
it the sender of the `synchronize` event, so the gate fails. A branch of a fork never qualifies.

- players.yml alone, from `panel/*` by the panel App: the panel's whitelist-only change
  (`mcctl panel-pr-check`: valid, canonical, ops unchanged).
- server.yml alone, from `panel/*` by the panel App, changing only `world.active` and `world.library` (v1.2
  world library, already live on the server; `mcctl panel-pr-check`: everything else parses equal, the head
  validates). A deploy never switches worlds, so this can't make the server do anything new.
- server.lock.yml and/or plugins.lock.yml alone, from `update/*` by the updater App, with
  `automation.auto_merge` builds or builds+patch: a newer build of the same server/plugins for the same
  Minecraft version, plugin versions published after the base's, every changed entry re-verified against the
  upstream APIs (scripts/verify_lock_update.py; the weekly update job's PRs).
- .github/workflows/server.yml alone, from `update/*` by the updater App, with `automation.auto_merge`
  builds+patch: only the `uses:` line, the same product repository, a patch release (vX.Y.Z -> vX.Y.Z+n)
  whose tag points at the new SHA (the product update PRs). A minor or major bump needs the admin. Caveat:
  the gate job itself runs the reusable workflow named by the PR head's `uses:` line, so for a product bump
  the new product version judges its own pull request; the tag check above is what binds it to a real
  release (SECURITY.md).

Anything else fails with "needs the admin" (who merges with the ruleset bypass). `automation.auto_merge` and
the App slugs are read from the BASE (a PR can't raise its own level or name another App).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import yaml

from mcctl import configrepo, http, panelpr, schema

Runner = Callable[..., subprocess.CompletedProcess[str]]
LOCKS = {configrepo.SERVER_LOCK, configrepo.PLUGINS_LOCK, configrepo.CLIENT_LOCK}  # v1.1: + the client pack
PANEL_PREFIX, UPDATE_PREFIX = "panel/", "update/"
WORKFLOW = ".github/workflows/server.yml"
PATCH = re.compile(r"v(\d+)\.(\d+)\.(\d+)")


@dataclass(frozen=True)
class Verdict:
    policy: str  # whitelist | worlds | builds | product | admin
    reasons: list[str]

    @property
    def ok(self) -> bool:
        return not self.reasons


def _git(root: Path, run: Runner, *args: str) -> str:
    proc = run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ValueError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def auto_merge_level(root: Path, base: str, run: Runner) -> str:
    try:
        data = yaml.safe_load(_git(root, run, "show", f"{base}:{configrepo.SERVER_YML}")) or {}
    except (ValueError, yaml.YAMLError):
        return "none"
    level = ((data.get("automation") or {}) if isinstance(data, dict) else {}).get(
        "auto_merge", "builds+patch"
    )
    return level if level in ("none", "builds", "builds+patch") else "none"


def app_slug(root: Path, base: str, run: Runner, key: str) -> str:
    """server.yml github.<key> (updater_app | panel_app) as of the base: an App slug, or "" (none)."""
    try:
        data = yaml.safe_load(_git(root, run, "show", f"{base}:{configrepo.SERVER_YML}")) or {}
    except (ValueError, yaml.YAMLError):
        return ""
    gh = (data.get("github") or {}) if isinstance(data, dict) else {}
    slug = gh.get(key) if isinstance(gh, dict) else None
    return slug if isinstance(slug, str) and re.fullmatch(schema.GH_APP_SLUG, slug) else ""


def check_app(root: Path, base: str, run: Runner, key: str, author: str, sender: str) -> list[str]:
    """The pull request was opened by, and the event that ran the gate sent by, App `key`'s bot user."""
    slug = app_slug(root, base, run, key)
    if not slug:
        return [f"server.yml github.{key} is empty on the base (no App to trust): needs the admin"]
    bot = f"{slug}[bot]"
    reasons = []
    if author != bot:
        reasons.append(
            f"the pull request was opened by {author or 'an unknown user'}, not {bot}: needs the admin"
        )
    if sender != bot:
        reasons.append(
            f"the last event (push) came from {sender or 'an unknown user'}, not {bot}: needs the admin"
        )
    return reasons


def tag_sha(repository: str, tag: str, token: str, transport: http.Transport) -> str | None:
    """The commit a tag points at (annotated tags dereferenced), via the GitHub REST git refs API."""
    headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    resp = http.get(transport, f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}", headers)
    obj = (resp.json() or {}).get("object") if resp.status == 200 and isinstance(resp.json(), dict) else None
    for _ in range(3):
        if not isinstance(obj, dict):
            return None
        if obj.get("type") == "commit":
            return str(obj.get("sha"))
        if obj.get("type") != "tag":
            return None
        resp = http.get(
            transport, f"https://api.github.com/repos/{repository}/git/tags/{obj.get('sha')}", headers
        )
        obj = (
            (resp.json() or {}).get("object")
            if resp.status == 200 and isinstance(resp.json(), dict)
            else None
        )
    return None


def check_product_bump(old_text: str, new_text: str, token: str, transport: http.Transport) -> list[str]:
    old, errs = configrepo.read_pin(old_text)
    new, errs2 = configrepo.read_pin(new_text)
    if old is None or new is None:
        return [f"{WORKFLOW}: {e}" for e in errs + errs2]
    if re.sub(r"^\s*(?:-\s+)?uses:.*$", "", old_text, flags=re.M) != re.sub(
        r"^\s*(?:-\s+)?uses:.*$", "", new_text, flags=re.M
    ):
        return [f"{WORKFLOW}: more than the `uses:` line changed"]
    if old.repository != new.repository:
        return [f"the product repository changed ({old.repository} -> {new.repository})"]
    o = PATCH.fullmatch(old.version or "")
    n = PATCH.fullmatch(new.version or "")
    if not o or not n:
        found = " -> ".join(f"`# {v}`" if v else "no comment" for v in (old.version, new.version))
        return [
            f"{WORKFLOW}: to auto-merge, the pin's comment must start with the release on both sides, "
            f"e.g. {configrepo.PIN_EXAMPLE} (found {found}); an untagged pin (vX.Y.Z-N-g<sha>) never does"
        ]
    if (o.group(1), o.group(2)) != (n.group(1), n.group(2)) or int(n.group(3)) <= int(o.group(3)):
        return [
            f"{old.version} -> {new.version} isn't a patch release: a minor or major upgrade needs the admin"
        ]
    sha = tag_sha(new.repository, str(new.version), token, transport)
    if sha != new.sha:
        got = (sha or "missing")[:12]
        return [f"tag {new.version} of {new.repository} doesn't point at {new.sha[:12]} ({got})"]
    return []


def check_locks(
    root: Path, base: str, head: str, run: Runner, verify: Callable[[Path, Path], list[str]]
) -> list[str]:
    with tempfile.TemporaryDirectory(prefix="mcctl-gate-") as d:
        dirs = {"base": Path(d) / "base", "head": Path(d) / "head"}
        for label, ref in (("base", base), ("head", head)):
            dirs[label].mkdir()
            for name in sorted(LOCKS):
                try:
                    (dirs[label] / name).write_text(_git(root, run, "show", f"{ref}:{name}"))
                except ValueError:
                    if name != configrepo.CLIENT_LOCK:  # v1.1: optional (verify: a new one needs the admin)
                        raise
        return verify(dirs["base"], dirs["head"])


def run_verify_script(base_dir: Path, head_dir: Path) -> list[str]:
    script = schema.PRODUCT_ROOT / "scripts" / "verify_lock_update.py"
    env = {k: v for k, v in os.environ.items() if not re.search(r"TOKEN|KEY|SECRET|PASSWORD", k)}
    proc = subprocess.run(
        [sys.executable, str(script), str(base_dir), str(head_dir)],
        capture_output=True, text=True, check=False, timeout=1800, env=env,
    )  # fmt: skip
    if proc.returncode == 0:
        return []
    return [ln for ln in (proc.stdout + proc.stderr).splitlines() if ln.strip()][-10:] or [
        "lock verification failed"
    ]


def evaluate(
    root: Path,
    base: str,
    head: str,
    *,
    product_token: str = "",
    transport: http.Transport = http.urllib_transport,
    run: Runner = subprocess.run,
    verify: Callable[[Path, Path], list[str]] = run_verify_script,
    head_ref: str = "",
    author: str = "",
    sender: str = "",
) -> Verdict:
    """`head_ref`: the PR's head branch, empty for a fork's (then nothing merges without the admin); `author`:
    the PR's user login; `sender`: the login that sent the event the gate runs for."""
    try:
        changed = sorted(
            f
            for f in _git(root, run, "diff", "--no-renames", "--name-only", f"{base}...{head}").splitlines()
            if f
        )
    except ValueError as e:
        return Verdict("admin", [str(e)])
    if not changed:
        return Verdict("admin", ["the pull request changes nothing"])
    level = auto_merge_level(root, base, run)
    if changed == [configrepo.PLAYERS_YML]:
        if not head_ref.startswith(PANEL_PREFIX):
            return Verdict(
                "admin", [f"a whitelist change merges by itself only from {PANEL_PREFIX}*: needs the admin"]
            )
        if reasons := check_app(root, base, run, "panel_app", author, sender):
            return Verdict("whitelist", reasons)
        return Verdict("whitelist", panelpr.check(root, base, head, run))
    if changed == [configrepo.SERVER_YML] and head_ref.startswith(PANEL_PREFIX):
        if reasons := check_app(root, base, run, "panel_app", author, sender):
            return Verdict("worlds", reasons)
        return Verdict("worlds", panelpr.check_world(root, base, head, run))
    if (set(changed) <= LOCKS or changed == [WORKFLOW]) and not head_ref.startswith(UPDATE_PREFIX):
        return Verdict(
            "admin",
            [
                f"lock and product updates merge by themselves only from {UPDATE_PREFIX}* (the updater App): "
                "needs the admin"
            ],
        )
    if set(changed) <= LOCKS or changed == [WORKFLOW]:
        policy = "builds" if set(changed) <= LOCKS else "product"
        if reasons := check_app(root, base, run, "updater_app", author, sender):
            return Verdict(policy, reasons)
    if set(changed) <= LOCKS:
        if level == "none":
            return Verdict("builds", ["automation.auto_merge is none: lock updates need the admin"])
        try:
            return Verdict("builds", check_locks(root, base, head, run, verify))
        except ValueError as e:
            return Verdict("builds", [str(e)])
    if changed == [WORKFLOW]:
        if level != "builds+patch":
            return Verdict("product", [f"automation.auto_merge is {level}: product updates need the admin"])
        try:
            old, new = (_git(root, run, "show", f"{ref}:{WORKFLOW}") for ref in (base, head))
        except ValueError as e:
            return Verdict("product", [str(e)])
        return Verdict("product", check_product_bump(old, new, product_token, transport))
    return Verdict(
        "admin", [f"needs the admin (merge with the ruleset bypass): it changes {', '.join(changed)}"]
    )
