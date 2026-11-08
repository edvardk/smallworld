#!/usr/bin/env python3
"""Panel actions (design §5 "Whitelist → Git", "Rejected logins", "Plugin disable", "Restore", "Stop"; P10).

Stdlib only. Imported by mc_agent.py, which runs as the ops user and calls these after a command passed its
signature, age, nonce and allowlist checks. Every privileged step is one of the fixed systemd units polkit
allows the ops user (base role, 50-mc-ops.rules); nothing here runs as root.

- Jobs: restart, stop, plugin toggles and restores run one at a time in a background thread, after an in-game
  countdown (tellraw + action bar at 5/4/3/2/1 min, 30, 10, 5..1 s), skipped when nobody is online. A job can
  be cancelled during its countdown. Its state is reported to the Worker (POST /ingest/job) for the panel.
  v1.3: one at a time across processes too (JOB_LOCK_FILE): mcctl's actions over SSH (mc_agent.py cli) run
  their job in the foreground with the same lock.
- Whitelist: RCON `whitelist add|remove` (the server resolves the UUID) -> confirmed in whitelist.json ->
  recorded in pending.json (the agent's state dir). A sync loop batches pending changes for 60 s, then commits
  the canonical players.yml to a branch `panel/<ts>` of the config repo and opens a pull request (GitHub REST
  with a short-lived token of the panel's GitHub App, minted by the Worker: contents + pull requests on the
  config repo only; the ruleset keeps main out of its reach). The config repo's workflow auto-merges it
  (whitelist-only diff). Merged -> the entries leave pending.json; closed unmerged -> reverted on the live
  server and dropped (Discord); still pending after 24 h -> dropped, with a Discord message. Deploys honour
  pending entries (players role: Git + pending adds - pending removes), so a deploy racing the PR can't undo
  a change.
- Rejected logins: "Disconnecting <name> (<address>): <reason>" lines whose reason is the whitelist
  message (Paper/Purpur 26.3: spigot.yml messages.whitelist, default "You are not whitelisted on this
  server!"; vanilla's "multiplayer.disconnect.not_whitelisted" is "You are not white-listed on this server!";
  both checked statically in the pinned jar), with the UUID from the preceding "UUID of player <name> is
  <uuid>". The last 50 (name, uuid, count, first/last seen, masked address) are kept.
- Plugins: the agent writes the disabled names to /var/lib/minecraft/plugins-disabled.json, then starts
  mc-plugins-apply.service, which runs `mc_panel.py apply-plugins` as minecraft (the owner of the server dir)
  and moves the locked jars between plugins/ and plugins/disabled/ (Paper doesn't load subdirectories); then
  a countdown restart. The plugins role keeps disabled plugins disabled across deploys.
- Restore: mc-restore@<snapshot>.service (as the ops user, restic credentials): restore.py --panel takes a
  safety snapshot first, then stops the server, fetches, applies (mc-restore-apply.service, as minecraft) and
  starts it again. The agent follows the unit and reports its steps.

    mc_panel.py apply-plugins     # mc-plugins-apply.service only (runs as minecraft)
    mc_panel.py reconcile-players DESIRED.json [--live]   # players role (as minecraft): Git + pending
    mc_panel.py alert-kept NAME...                        # players role (ops user, mc-run notify.env)
"""

from __future__ import annotations

import base64
import calendar
import contextlib
import copy
import fcntl
import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import mc_rcon
import notify
import worlds
from alerts import Alert, render

SERVER_DIR = mc_rcon.SERVER_DIR
STATE_DIR = notify.STATE_DIR  # /var/lib/minecraft: the ops user writes, everyone reads
PLUGINS_DISABLED_FILE = STATE_DIR / "plugins-disabled.json"
PLUGINS_LOCK_FILE = Path("/opt/minecraft/plugins-lock.json")  # = mc_plugins_lock_json (plugins role)
SNAPSHOTS_FILE = notify.SNAPSHOTS_FILE  # backup.py --list-snapshots writes it
PLUGINS_APPLY_UNIT = "mc-plugins-apply.service"
UNIT = "minecraft.service"
BACKUP_UNIT = "mc-backup.service"
# v1.3: one job at a time across processes: mc-agent's jobs and `mcctl` actions over SSH (mc_agent.py cli)
# both hold this lock from the countdown to the end of the work (the agent's ReadWritePaths: the state dir).
JOB_LOCK_FILE = STATE_DIR / "job.lock"
NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
SNAPSHOT_ID = re.compile(r"[0-9a-f]{8,64}")
PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")  # mcctl.schema PLUGIN_NAME, no leading dot/dash
JAR = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,199}\.jar")
COUNTDOWNS = (0, 10, 60, 300)  # seconds the panel offers (the mockup's 10 s / 60 s / 5 min, or none)
ANNOUNCE_AT = (300, 240, 180, 120, 60, 30, 10, 5, 4, 3, 2, 1)
PENDING_TTL_S = 86400  # design §5: pending whitelist entries expire after 24 h
BATCH_S = 60  # changes are batched this long before a pull request (PANEL_BATCH_S)
PR_CHECK_S = 120  # how often an open pull request's state is checked (PANEL_PR_CHECK_S)
REJECTED_KEEP = 50
RCON_WAIT_S = 300  # a restart must have RCON back within this


class ActionError(Exception):
    """An action that can't run (bad arguments, busy, a failed step). The message reaches the panel."""


def atomic_write(path: Path, text: str, mode: int = 0o644) -> None:
    """A temporary file next to `path`, created owner-only (0600) and written, then given `mode` and renamed
    over it: a reader sees the old file or the new one, never half of it, and never a wider mode mid-write."""
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_CLOEXEC, 0o600)
    with os.fdopen(fd, "w") as f:
        os.fchmod(f.fileno(), 0o600)  # an existing tmp can't keep other bits while it is written
        f.write(text)
        f.flush()
        os.fchmod(f.fileno(), mode)  # the file's intended mode (e.g. 0640 for files the JVM reads)
    os.replace(tmp, path)


def read_json(path: Path, default: Any) -> Any:  # noqa: ANN401 - whatever the file holds
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


# --- players.yml (canonical form, mcctl/schema.py dump_players) ---------------------------------------------

ITEM = re.compile(r"^  - \{(?P<body>.*)\}$")
PAIR = re.compile(r'\s*(?P<key>[a-z]+): (?P<value>"(?:[^"\\]|\\.)*"|-?\d+)\s*(?:,|$)')
SECTIONS = {"whitelist": ("name", "uuid"), "ops": ("name", "uuid", "level")}


def parse_players(text: str) -> dict[str, list[dict[str, Any]]]:
    """players.yml in its canonical form (what `mcctl validate` requires on main). Raises ValueError."""
    out: dict[str, list[dict[str, Any]]] = {}
    section = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        head = re.fullmatch(r"(whitelist|ops):( \[\])?", line)
        if head:
            section = head.group(1)
            out[section] = []
            continue
        m = ITEM.match(line)
        if not m or section is None:
            raise ValueError(f"players.yml isn't in canonical form near {line[:60]!r}")
        item: dict[str, Any] = {}
        body = m.group("body")
        pos = 0
        while pos < len(body):
            p = PAIR.match(body, pos)
            if not p or p.end() == pos:
                raise ValueError(f"players.yml: can't read {body[:60]!r}")
            item[p.group("key")] = json.loads(p.group("value"))
            pos = p.end()
        if set(item) != set(SECTIONS[section]):
            raise ValueError(f"players.yml: {section} entries have {', '.join(SECTIONS[section])}")
        out[section].append(item)
    for s in SECTIONS:
        out.setdefault(s, [])
    return out


def dump_players(players: dict[str, list[dict[str, Any]]]) -> str:
    """The canonical text (same rules as mcctl.schema.dump_players; tests compare the two)."""
    lines: list[str] = []
    for section, keys in SECTIONS.items():
        items = players.get(section) or []
        if not items:
            lines.append(f"{section}: []")
            continue
        lines.append(f"{section}:")
        for item in sorted(items, key=lambda x: (str(x["name"]).lower(), str(x["uuid"]))):
            pairs = ", ".join(f"{k}: {json.dumps(item[k])}" for k in keys)
            lines.append(f"  - {{{pairs}}}")
    return "\n".join(lines) + "\n"


def apply_entries(
    players: dict[str, list[dict[str, Any]]], entries: Iterable[dict[str, Any]]
) -> tuple[dict[str, list[dict[str, Any]]], list[str]]:
    """players.yml with the pending whitelist changes applied; returns it and the uuids that changed it.
    Ops are never touched (a removed op stays: the auto-merge refuses an ops change, so the admin decides)."""
    wl = {p["uuid"]: dict(p) for p in players.get("whitelist", [])}
    ops = {o["uuid"] for o in players.get("ops", [])}
    changed: list[str] = []
    for e in entries:
        if e["op"] == "add" and e["uuid"] not in wl:
            wl[e["uuid"]] = {"name": e["name"], "uuid": e["uuid"]}
            changed.append(e["uuid"])
        elif e["op"] == "remove" and e["uuid"] in wl and e["uuid"] not in ops:
            del wl[e["uuid"]]
            changed.append(e["uuid"])
    return {"whitelist": list(wl.values()), "ops": list(players.get("ops", []))}, changed


# --- pending whitelist changes ------------------------------------------------------------------------------


class Pending:
    """pending.json in the agent's state dir: {"entries": [{uuid, name, op, ts, login, pr}], "pr": {...},
    "error": str, "changed": ts}. The deploy (players role) reads it as root.

    v1.3: two processes write it, mc-agent (the panel's commands and the sync loop) and `mc_agent.py cli`
    (mcctl's actions over SSH), so every read-modify-write holds an flock on pending.json.lock (next to it,
    0600: only the ops user opens it) and reads the file again under it; the write is atomic (tmp + rename,
    0600). Nobody holds the lock across a network call: sync_pending works on a snapshot and merges its result
    back (merge_sync)."""

    def __init__(self, path: Path, lock_path: Path | None = None) -> None:
        self.path = path
        self.lock_path = lock_path or path.with_name(path.name + ".lock")
        self.lock = threading.Lock()  # the threads of one process (flock alone would do, this is cheaper)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Exclusive across threads and processes (an flock, released by the kernel if the process dies). Held
        for a read and a write of a small file, never across a network call, so it blocks. A lock file that
        can't be opened (no state dir: a test, a dev box) only loses the cross-process part."""
        with self.lock:
            try:
                fd: int | None = os.open(self.lock_path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
            except OSError as e:
                why = f"{type(e).__name__} (this process only)"
                print(f"pending lock {self.lock_path}: {why}", file=sys.stderr)
                fd = None
            try:
                if fd is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX)
                yield
            finally:
                if fd is not None:
                    with contextlib.suppress(OSError):
                        fcntl.flock(fd, fcntl.LOCK_UN)
                    os.close(fd)

    @contextlib.contextmanager
    def update(self) -> Iterator[dict[str, Any]]:
        """pending.json as it is now (read under the lock), saved on leaving the block if it changed."""
        with self.locked():
            data = self.load()
            before = json.dumps(data, sort_keys=True)
            yield data
            if json.dumps(data, sort_keys=True) != before:
                self.save(data)

    def load(self) -> dict[str, Any]:
        """A consistent read without the lock (writes are atomic renames); modify only inside update()."""
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        entries = [
            e for e in data.get("entries", []) if isinstance(e, dict) and UUID.fullmatch(str(e.get("uuid")))
        ]
        return {
            "entries": entries,
            "pr": data.get("pr"),
            "error": str(data.get("error") or ""),
            "changed": float(data.get("changed") or 0),
            "expired": data.get("expired") or [],
            "world": data.get("world") if isinstance(data.get("world"), dict) else None,
        }

    def save(self, data: dict[str, Any]) -> None:
        """Only under locked() (update() does both)."""
        atomic_write(self.path, json.dumps(data, sort_keys=True, indent=1), 0o600)

    def record_world(
        self, active: str, library: list[dict[str, Any]], what: str, login: str, now: float
    ) -> None:
        """v1.2: the world library as it now is on the server (world.active + world.library for server.yml).
        The latest state wins; it stays until a pull request with it is merged (a switch can't be undone by
        dropping it, so it never expires). Deploys honour it (minecraft role: worlds-library.json)."""
        with self.update() as data:
            prev = data.get("world") or {}
            data["world"] = {
                "active": active,
                "library": library,
                "what": what[:200],
                "ts": now,
                "login": login[:200],
                "pr": None,
                "open_pr": prev.get("open_pr"),
                "error": "",
            }
            data["changed"] = data["changed"] or now

    def record(self, op: str, name: str, uuid: str, login: str, now: float) -> None:
        """The latest change per player wins (one already in an open PR is replaced: superseded)."""
        with self.update() as data:
            data["entries"] = [e for e in data["entries"] if e["uuid"] != uuid]
            data["entries"].append(
                {"uuid": uuid, "name": name, "op": op, "ts": now, "login": login, "pr": None}
            )
            data["changed"] = now


def merge_sync(current: dict[str, Any], snap: dict[str, Any], done: dict[str, Any]) -> dict[str, Any]:
    """v1.3: a sync round's result (`done`, worked out from `snap` without the lock) applied to pending.json
    as it is now (`current`, read again under the lock). What changed meanwhile (record/record_world from the
    other process or a panel command) wins: an entry recorded again is kept as recorded (its own PR comes
    next), a new entry is kept; a world change keeps its new state and takes the round's view of the open
    world PR. The rest (expired, merged, rejected, put in a PR) is the round's; pr, error and expired are only
    the round's to write."""
    before = {e["uuid"]: e for e in snap["entries"]}
    after = {e["uuid"]: e for e in done["entries"]}
    entries = []
    for e in current["entries"]:
        if before.get(e["uuid"]) != e:  # new, or recorded again since the snapshot
            entries.append(e)
        elif e["uuid"] in after:
            entries.append(after[e["uuid"]])
        # else: the round dropped it (expired, merged, rejected, already in Git)
    out = dict(current, entries=entries, pr=done["pr"], error=done["error"], expired=done["expired"])
    if current["world"] == snap["world"]:
        out["world"] = done["world"]
    elif current["world"] is not None:  # a newer world state: the round's open PR (or its end) still counts
        out["world"] = dict(current["world"], open_pr=(done["world"] or {}).get("open_pr"))
    return out


# --- GitHub (contents + pulls API, stdlib) ------------------------------------------------------------------


class GitHubError(Exception):
    pass


# GitHub installation tokens: `ghs_` plus letters, digits, `_` and `.`. v1.2.2: GitHub's tokens grew past the
# old 255-character cap (390 seen live), so only the character set is strict; the length is a sanity bound.
TOKEN_RE = re.compile(r"[A-Za-z0-9_.-]{20,4096}")


def token_shape(data: Any, token: str) -> str:  # noqa: ANN401 - the Worker's JSON reply
    """Why a token was refused, never the token: the reply's keys, its length, any unexpected characters."""
    if not isinstance(data, dict):
        return f"reply is {type(data).__name__}"
    if not token:
        err = str(data.get("error") or "")[:120]
        return f"no token in the reply (keys: {', '.join(sorted(map(str, data))) or 'none'})" + (
            f": {err}" if err else ""
        )
    odd = sorted(set(re.sub(r"[A-Za-z0-9_.-]", "", token)))
    return f"{len(token)} characters" + (f", unexpected {''.join(odd[:8])!r}" if odd else "")


class TokenCache:
    """Short-lived GitHub App installation tokens for the panel's pull requests (P10 review). The App's
    private key never reaches the server: the Worker holds it, mints a token narrowed to the config repo with
    Contents + Pull requests write (1 h), and hands it to this agent over its authenticated link (POST
    /agent/github-token). A token is reused until 5 minutes before it expires; nothing is written to disk."""

    def __init__(self, fetch: Callable[[], Any], clock: Callable[[], float] = time.time) -> None:
        self.fetch = fetch
        self.clock = clock
        self.token = ""
        self.expires = 0.0

    def __call__(self) -> str:
        if self.token and self.expires - self.clock() > 300:
            return self.token
        try:
            data = self.fetch()
        except Exception as e:  # the agent's IngestError, network errors: one message, never the token
            raise GitHubError(f"no GitHub token from the Worker: {str(e)[:160]}") from None
        token = str((data or {}).get("token") or "") if isinstance(data, dict) else ""
        if not TOKEN_RE.fullmatch(token):
            raise GitHubError(f"the Worker returned no usable GitHub token ({token_shape(data, token)})")
        try:
            expires = float(
                calendar.timegm(time.strptime(str(data.get("expires_at", ""))[:19], "%Y-%m-%dT%H:%M:%S"))
            )
        except ValueError:
            expires = self.clock() + 1800
        self.token, self.expires = token, expires
        return token

    def invalidate(self) -> None:
        self.token, self.expires = "", 0.0


@dataclass
class GitHub:
    """The config repo through the panel's GitHub App (Contents + Pull requests on the config repo only, via
    short-lived installation tokens: TokenCache). The repository's ruleset keeps main out of reach: the App
    can only push panel/* branches and open pull requests (SECURITY.md "The panel's GitHub App")."""

    tokens: Callable[[], str]
    repo: str  # owner/name
    api: str = "https://api.github.com"
    branch: str = "main"

    def call(self, method: str, path: str, body: object = None) -> Any:  # noqa: ANN401 - GitHub JSON
        data = None if body is None else json.dumps(body).encode()
        for attempt in (1, 2):
            req = urllib.request.Request(  # noqa: S310 - https (or loopback http in tests), checked in from_env
                f"{self.api}{path}",
                data=data,
                method=method,
                headers={
                    "Authorization": f"Bearer {self.tokens()}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                    "User-Agent": "smallworld mc-agent",
                    **({"Content-Type": "application/json"} if data is not None else {}),
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=20) as resp:  # noqa: S310
                    raw = resp.read(4 << 20)
            except urllib.error.HTTPError as e:
                if e.code == 401 and attempt == 1 and isinstance(self.tokens, TokenCache):
                    self.tokens.invalidate()  # revoked or expired early: one fresh token, then give up
                    continue
                raise GitHubError(f"{method} {path.split('?')[0]}: HTTP {e.code}") from None
            except (OSError, urllib.error.URLError) as e:
                raise GitHubError(f"{method} {path.split('?')[0]}: {type(e).__name__}") from None
            return json.loads(raw or b"{}")
        raise GitHubError(f"{method} {path.split('?')[0]}: HTTP 401")

    def players_on_main(self) -> tuple[dict[str, list[dict[str, Any]]], str, str]:
        """(players.yml parsed, its blob sha, main's commit sha)."""
        ref = self.call("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}")
        head = ref["object"]["sha"]
        f = self.call("GET", f"/repos/{self.repo}/contents/players.yml?ref={head}")
        text = base64.b64decode(f["content"]).decode()
        return parse_players(text), f["sha"], head

    def open_pr(self, entries: list[dict[str, Any]], now: float) -> dict[str, Any]:
        """Commit players.yml with `entries` applied to a new branch panel/<ts> and open a PR. Returns
        {"number", "url", "branch", "changed": [uuid...]}; number None if main already has every change."""
        players, blob, head = self.players_on_main()
        new, changed = apply_entries(players, entries)
        if not changed:
            return {"number": None, "url": "", "branch": "", "changed": []}
        branch = "panel/" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
        self.call("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
        names = [f"{'+' if e['op'] == 'add' else '-'}{e['name']}" for e in entries if e["uuid"] in changed]
        message = f"panel: whitelist {' '.join(names)}"[:200]
        self.call("PUT", f"/repos/{self.repo}/contents/players.yml", {
            "message": message, "content": base64.b64encode(dump_players(new).encode()).decode(),
            "sha": blob, "branch": branch,
        })  # fmt: skip
        who = sorted({str(e.get("login") or "?") for e in entries if e["uuid"] in changed})
        body = (
            "Whitelist changes made in the admin panel (already live on the server).\n\n"
            + "\n".join(f"- {n}" for n in names)
            + f"\n\nBy: {', '.join(who)}. The workflow merges this by itself when only `whitelist` changed; "
            "closing it unmerged reverts the change on the server."
        )
        pr = self.call("POST", f"/repos/{self.repo}/pulls",
                       {"title": message, "head": branch, "base": self.branch, "body": body})  # fmt: skip
        return {"number": int(pr["number"]), "url": str(pr.get("html_url") or ""), "branch": branch,
                "changed": changed}  # fmt: skip

    def open_world_pr(self, world: dict[str, Any], now: float) -> dict[str, Any]:
        """v1.2: commit server.yml with world.active/world.library replaced (worlds.set_world_section: every
        other line stays) to a branch panel/world-<ts> and open a PR; number None if main already says so."""
        ref = self.call("GET", f"/repos/{self.repo}/git/ref/heads/{self.branch}")
        head = ref["object"]["sha"]
        f = self.call("GET", f"/repos/{self.repo}/contents/server.yml?ref={head}")
        text = base64.b64decode(f["content"]).decode()
        new = worlds.set_world_section(text, world["active"], world["library"])
        if new == text:
            return {"number": None, "url": "", "branch": ""}
        branch = "panel/world-" + time.strftime("%Y%m%d-%H%M%S", time.gmtime(now))
        self.call("POST", f"/repos/{self.repo}/git/refs", {"ref": f"refs/heads/{branch}", "sha": head})
        message = f"panel: world {world.get('what') or 'library'}"[:200]
        self.call("PUT", f"/repos/{self.repo}/contents/server.yml", {
            "message": message, "content": base64.b64encode(new.encode()).decode(), "sha": f["sha"],
            "branch": branch,
        })  # fmt: skip
        body = (
            "World library change made in the admin panel (already live on the server).\n\n"
            f"- {world.get('what') or 'library'}\n- world.active: `{world['active']}`\n"
            f"- world.library: {', '.join(e['name'] for e in world['library']) or 'empty'}\n\n"
            f"By: {world.get('login') or '?'}. Only `world.active` and `world.library` in server.yml "
            "change; the workflow merges this by itself. Closing it doesn't undo the switch: deploys keep "
            "honouring the live world until server.yml says the same (switch back in the panel to undo it)."
        )
        pr = self.call("POST", f"/repos/{self.repo}/pulls",
                       {"title": message, "head": branch, "base": self.branch, "body": body})  # fmt: skip
        return {"number": int(pr["number"]), "url": str(pr.get("html_url") or ""), "branch": branch}

    def pr_state(self, number: int) -> str:
        pr = self.call("GET", f"/repos/{self.repo}/pulls/{int(number)}")
        if pr.get("merged") or pr.get("merged_at"):
            return "merged"
        return "open" if pr.get("state") == "open" else "closed"


def github_from_env(
    env: dict[str, str] | None = None, fetch_token: Callable[[], Any] | None = None
) -> GitHub | None:
    """The config repo (CONFIG_REPO) with tokens from `fetch_token` (the agent asks the Worker), or None."""
    e = os.environ if env is None else env
    repo = e.get("CONFIG_REPO", "")
    if fetch_token is None or not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repo):
        return None
    api = (e.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    parts = urllib.parse.urlsplit(api)
    loopback = parts.scheme == "http" and parts.hostname in ("127.0.0.1", "localhost", "::1")
    if parts.scheme != "https" and not loopback:
        raise ValueError("GITHUB_API_URL must be https:// (http:// only for a loopback address)")
    branch = e.get("CONFIG_BRANCH") or "main"
    return GitHub(TokenCache(fetch_token), repo, api, branch)


# --- v1.3: the Panel -> Git self-test -----------------------------------------------------------------------
#
# Proves the panel can still open a pull request, without opening one (v1.2.2: GitHub's tokens grew to 390
# characters, TokenCache refused them, and nobody noticed for days): the Worker mints a token, a fresh
# TokenCache accepts it, the token's permissions are the ones the flow needs, and GitHub answers for the
# config repo (the repository, main's head and players.yml as open_pr reads them, the pull request list).
# Read-only: no branch, no commit, no pull request. Every error names the step; no token reaches a message
# (GitHubError carries the request line and status only, token_shape the length and odd characters).

SELFTEST_STEPS = ("token", "permissions", "repository", "main", "pulls")
SELFTEST_NEEDS = {"contents": "write", "pull_requests": "write"}  # what the Worker narrows the token to
SELFTEST_ALERT_AFTER = 2  # consecutive failures before Discord hears about it


def _no_token(text: str, token: str) -> str:
    return text.replace(token, "<token>") if token else text


def selftest(
    fetch_token: Callable[[], Any] | None,
    gh: GitHub | None,
    now: float,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    """One self-test run: {ok, ts, configured, steps: [{step, ok, detail}], error ("<step>: <why>")}."""
    out: dict[str, Any] = {"ok": False, "ts": int(now * 1000), "configured": gh is not None, "steps": [],
                           "error": ""}  # fmt: skip
    if gh is None or fetch_token is None:
        out["error"] = "configuration: no config repo for the panel (CONFIG_REPO) or no token source"
        return out
    token = ""
    reply: Any = None

    def fetch() -> Any:  # noqa: ANN401 - the Worker's JSON reply, kept for the permissions step
        nonlocal reply
        reply = fetch_token()
        return reply

    def step(name: str, ok: bool, detail: str) -> bool:
        detail = _no_token(detail, token)[:200]
        out["steps"].append({"step": name, "ok": ok, "detail": detail})
        if not ok and not out["error"]:
            out["error"] = f"{name}: {detail}"
        return ok

    cache = TokenCache(fetch, clock)  # a fresh cache: the Worker's answer is checked, not a remembered token
    try:
        token = cache()
    except GitHubError as e:
        step("token", False, str(e))
        return out
    step("token", True, f"the Worker minted a token ({len(token)} characters), TokenCache accepted it")
    perms = reply.get("permissions") if isinstance(reply, dict) else None
    if isinstance(perms, dict):
        missing = [f"{k}: {v}" for k, v in SELFTEST_NEEDS.items() if perms.get(k) != v]
        why = f"the token lacks {', '.join(missing)}" if missing else "contents + pull_requests write"
        if not step("permissions", not missing, why):
            return out
    else:
        step("permissions", True, "not reported by this Worker (checked through the repository below)")
    probe = GitHub(lambda: token, gh.repo, gh.api, gh.branch)
    try:
        repo = probe.call("GET", f"/repos/{gh.repo}")
        push = (repo.get("permissions") or {}).get("push") if isinstance(repo, dict) else None
        if push is False:
            step("repository", False, f"{gh.repo}: the token can't push (permissions.push is false)")
            return out
        step("repository", True, f"{gh.repo} readable" + (", push allowed" if push else ""))
    except (GitHubError, KeyError, TypeError, ValueError) as e:
        step("repository", False, str(e) or type(e).__name__)
        return out
    try:
        players, _, head = probe.players_on_main()
        step("main", True, f"{gh.branch} at {str(head)[:12]}, players.yml has {len(players['whitelist'])} "
             "whitelisted")  # fmt: skip
    except (GitHubError, KeyError, TypeError, ValueError) as e:
        step("main", False, str(e) or type(e).__name__)
        return out
    try:
        prs = probe.call("GET", f"/repos/{gh.repo}/pulls?state=open&per_page=1")
        if not isinstance(prs, list):
            raise GitHubError("GET pulls: not a list")
        step("pulls", True, "pull requests reachable (nothing was created)")
    except GitHubError as e:
        step("pulls", False, str(e))
        return out
    out["ok"] = True
    return out


class SelfTestState:
    """The last self-test result and the failure streak, kept in the agent's state dir (survives restarts) so
    the daily run alerts once after SELFTEST_ALERT_AFTER consecutive failures, and once when it passes
    again."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def load(self) -> dict[str, Any]:
        data = read_json(self.path, {})
        if not isinstance(data, dict):
            data = {}
        last, last_panel = data.get("last"), data.get("last_panel")
        return {
            "last": last if isinstance(last, dict) else None,
            # the last on-demand run: the once-a-minute limit counts from it, whatever ran since (v1.4 fix: a
            # daily run in between used to lift the limit)
            "last_panel": last_panel if isinstance(last_panel, dict) else None,
            "failures": int(data.get("failures") or 0),
            "alerted": data.get("alerted") is True,
        }

    def record(self, result: dict[str, Any], alert: Callable[[str], None] | None = None) -> dict[str, Any]:
        with self.lock:
            st = self.load()
            st["last"] = result
            if result.get("trigger") == "panel":
                st["last_panel"] = result
            if result.get("ok") or not result.get("configured", True):
                if st["alerted"] and alert and result.get("ok"):
                    alert(selftest_back_alert(st["failures"]))
                st["failures"], st["alerted"] = 0, False
            else:
                st["failures"] += 1
                if st["failures"] >= SELFTEST_ALERT_AFTER and not st["alerted"]:
                    if alert:
                        alert(selftest_alert(str(result.get("error") or "unknown"), st["failures"]))
                    st["alerted"] = True
            atomic_write(self.path, json.dumps(st), 0o600)
            return st

    def public(self) -> dict[str, Any] | None:
        st = self.load()
        last = st["last"]
        if not last:
            return None
        return {k: last.get(k) for k in ("ok", "ts", "configured", "error", "steps", "trigger")} | {
            "failures": st["failures"]
        }


def selftest_alert(error: str, failures: int) -> str:
    """The daily Panel -> Git self-test failed twice in a row (docs/operations.md "Alerts")."""
    return render(
        Alert(
            "warning",
            "The panel can't open pull requests on the config repo",
            impact="Whitelist and world changes made in the panel stay live but never reach Git; whitelist "
            "changes are dropped after 24 h, and the next deploy undoes them.",
            cause=f"The daily Panel -> Git self-test failed {failures} times in a row, at {error}",
            todo="Run `mcctl doctor`, and the self-test from the panel's Health tab after a fix. Token or "
            "permissions: the panel's GitHub App (installed on the config repo, Contents + Pull requests); "
            "a stale Worker: `mcctl render` + `mcctl apply`.",
            details=f"{failures} consecutive failures; last: {error}",
        )
    )


def selftest_back_alert(failures: int) -> str:
    return render(
        Alert(
            "recovered",
            "The panel can open pull requests again",
            impact="Panel whitelist and world changes reach the config repo again.",
            cause="The Panel -> Git self-test passed.",
            details=f"after {failures} failed run(s)",
        )
    )


def expired_alert(names: str) -> str:
    """Panel whitelist changes that never reached Git within 24 h (docs/operations.md "Alerts")."""
    return render(
        Alert(
            "warning",
            "Whitelist changes made in the panel were dropped",
            impact=f"Live on the server for now ({names}), but the next deploy follows players.yml in Git, "
            "so they will be undone then.",
            cause="They never reached the config repo within 24 h (no pull request could be opened or "
            "merged: the panel's GitHub App, CONFIG_REPO, or GitHub itself).",
            todo="Re-do them in the panel, or edit players.yml in the config repo and push; `mcctl doctor` "
            "checks the panel's GitHub App.",
            details=f"pending panel whitelist change(s) older than {PENDING_TTL_S // 3600} h: {names}",
        )
    )


def rejected_alert(number: int, reverted: list[str], failed: list[str]) -> str:
    """A panel whitelist pull request closed without merging: a "no" from whoever closed it."""
    todo = "Nothing if that was intended."
    if failed:
        todo += " To undo it now: deploy (`mcctl deploy`, or push to main)."
    todo += " To make the change after all: the panel again, or players.yml in the config repo."
    return render(
        Alert(
            "info",
            f"A panel whitelist change was turned down (pull request #{number} closed)",
            impact=(f"Undone on the live server with: whitelist {', '.join(reverted)}. " if reverted else "")
            + (f"Couldn't run whitelist {', '.join(failed)}: live until the next deploy. " if failed else "")
            + "The whitelist follows players.yml in Git again.",
            cause=f"Pull request #{number} was closed without merging.",
            todo=todo,
            details=f"reverted: {', '.join(reverted) or 'nothing'}; failed: {', '.join(failed) or 'nothing'}",
        )
    )


def revert_live(entries: Iterable[dict[str, Any]], rcon: Callable[[str], str]) -> tuple[list[str], list[str]]:
    """Undo panel whitelist changes on the running server (RCON); returns (reverted, failed) descriptions."""
    reverted: list[str] = []
    failed: list[str] = []
    for e in entries:
        name = str(e.get("name", ""))
        if not NAME.fullmatch(name) or e.get("op") not in ("add", "remove"):
            continue
        undo = "remove" if e["op"] == "add" else "add"
        try:
            run_ok = rcon(f"whitelist {undo} {name}")
        except (OSError, mc_rcon.RconError, KeyError, ValueError):
            run_ok = None
        (reverted if run_ok is not None else failed).append(f"{undo} {name}")
    return reverted, failed


def sync_pending(
    pending: Pending,
    gh: GitHub | None,
    now: float,
    alert: Callable[[str], None] = notify.discord,
    log: Callable[..., None] | None = None,
    batch_s: float = BATCH_S,
    pr_check_s: float = PR_CHECK_S,
    rcon: Callable[[str], str] | None = None,
) -> bool:
    """One round of the whitelist -> Git flow. Returns True if pending.json changed (push the inventory).

    A pull request closed without merging is a "no" from whoever closed it (P10 review): its changes are
    reverted on the live server (`whitelist remove` for an add, `whitelist add` for a remove), dropped from
    pending.json (so the next deploy follows Git) and announced on Discord."""
    log = log or (lambda msg, **f: None)
    run = rcon or rcon_command
    # v1.3: a snapshot under the lock, the round (GitHub, RCON, Discord) without it, then merged back into
    # pending.json as it is by then (mc_agent.py cli or a panel command may have recorded something meanwhile)
    with pending.locked():
        snap = pending.load()
    data = copy.deepcopy(snap)
    entries = data["entries"]
    # 24 h without reaching Git: drop, tell the admin (the next deploy will follow Git again)
    old = [e for e in entries if now - float(e.get("ts") or 0) > PENDING_TTL_S]
    if old:
        data["entries"] = entries = [e for e in entries if e not in old]
        gone = [{"name": e["name"], "op": e["op"], "ts": now} for e in old]
        data["expired"] = (data["expired"] + gone)[-20:]
        names = ", ".join(f"{e['op']} {e['name']}" for e in old)
        alert(expired_alert(names))
        log(f"panel whitelist entries expired: {names}", action="whitelist-expire", result="dropped")
    if gh is None:
        data["error"] = "no config repo for the panel (CONFIG_REPO): changes stay pending until they expire"
    else:
        try:
            pr = data.get("pr")
            if pr and pr.get("number") and now - float(pr.get("checked") or 0) >= pr_check_s:
                state = gh.pr_state(int(pr["number"]))
                pr["checked"] = now
                if state == "merged":
                    data["entries"] = entries = [e for e in entries if e.get("pr") != pr["number"]]
                    log(f"panel PR #{pr['number']} merged", action="whitelist-pr", result="merged")
                    data["pr"] = pr = None
                elif state == "closed":
                    number = int(pr["number"])
                    rejected = [e for e in entries if e.get("pr") == number]
                    reverted, failed = revert_live(rejected, run)
                    data["entries"] = entries = [e for e in entries if e.get("pr") != number]
                    data["error"] = (
                        f"pull request #{number} was closed without merging: reverted on the server"
                        + (
                            f" (couldn't revert {', '.join(failed)}: the next deploy follows Git)"
                            if failed
                            else ""
                        )
                    )
                    why = f"PR #{number} closed"
                    gone = [{"name": e["name"], "op": e["op"], "ts": now, "why": why} for e in rejected]
                    data["expired"] = (data["expired"] + gone)[-20:]
                    alert(rejected_alert(number, reverted, failed))
                    log(f"panel PR #{number} closed unmerged; reverted {reverted} failed {failed}",
                        action="whitelist-pr", result="reverted")  # fmt: skip
                    data["pr"] = pr = None
            fresh = [e for e in entries if e.get("pr") is None]
            if fresh and (not pr) and now - data["changed"] >= batch_s:
                made = gh.open_pr(fresh, now)
                if made["number"] is None:
                    data["entries"] = [e for e in entries if e not in fresh]  # Git already has them
                else:
                    for e in fresh:
                        e["pr"] = made["number"] if e["uuid"] in made["changed"] else 0
                    data["entries"] = [e for e in entries if e.get("pr") != 0]
                    data["pr"] = {
                        "number": made["number"],
                        "url": made["url"],
                        "branch": made["branch"],
                        "created": now,
                        "checked": now,
                    }
                    log(f"panel PR #{made['number']} opened", action="whitelist-pr", result="opened")
                data["error"] = ""
            sync_world(data, gh, now, alert, log, batch_s, pr_check_s)
        except (GitHubError, ValueError, KeyError, TypeError) as e:
            data["error"] = f"GitHub: {str(e)[:200]}"
    if data == snap:
        return False
    with pending.update() as current:
        merged = merge_sync(current, snap, data)
        changed = merged != current
        current.clear()
        current.update(merged)
    return changed


def world_rejected_alert(number: int, active: str) -> str:
    """A panel world pull request closed without merging (the switch itself stays: it is live)."""
    return render(
        Alert(
            "warning",
            f"A panel world change wasn't merged (pull request #{number} closed)",
            impact=f"The server keeps running the world {active}; deploys honour it, but server.yml in Git "
            "still names another world.",
            cause=f"Pull request #{number} (server.yml world.active/library) was closed without merging.",
            todo="Merge an equivalent change to server.yml (world.active and world.library as the panel "
            "shows them), or switch back in the panel's Worlds tab. The next world change opens a new "
            "pull request.",
            details=f"world.active on the server: {active}",
        )
    )


def sync_world(
    data: dict[str, Any],
    gh: GitHub,
    now: float,
    alert: Callable[[str], None],
    log: Callable[..., None],
    batch_s: float,
    pr_check_s: float,
) -> None:
    """v1.2: the world library -> server.yml in Git (one pull request at a time). Merged -> the pending state
    leaves pending.json unless something newer came in meanwhile (then that gets its own PR); closed -> kept
    (the live world wins), Discord, no new PR until the next world change."""
    w = data.get("world")
    if not w:
        return
    pr = w.get("open_pr")
    if pr and pr.get("number") and now - float(pr.get("checked") or 0) >= pr_check_s:
        state = gh.pr_state(int(pr["number"]))
        pr["checked"] = now
        if state == "merged":
            log(f"panel world PR #{pr['number']} merged", action="world-pr", result="merged")
            if w.get("pr") == pr["number"]:
                data["world"] = None
                return
            w["open_pr"] = None
        elif state == "closed":
            log(f"panel world PR #{pr['number']} closed unmerged", action="world-pr", result="closed")
            alert(world_rejected_alert(int(pr["number"]), str(w.get("active"))))
            w["error"] = (
                f"pull request #{pr['number']} was closed without merging; the live world still counts"
            )
            if w.get("pr") == pr["number"]:
                w["pr"] = -int(pr["number"])  # nothing new to propose until the next world change
            w["open_pr"] = None
    if w.get("pr") is None and not w.get("open_pr") and now - float(w.get("ts") or 0) >= batch_s:
        made = gh.open_world_pr(w, now)
        if made["number"] is None:
            data["world"] = None  # Git already says so
            return
        w["pr"] = made["number"]
        w["open_pr"] = {
            "number": made["number"],
            "url": made["url"],
            "branch": made["branch"],
            "created": now,
            "checked": now,
        }
        w["error"] = ""
        log(f"panel world PR #{made['number']} opened", action="world-pr", result="opened")


# --- rejected logins ----------------------------------------------------------------------------------------

# P10 review: anchored to the server's console prefix ("[HH:MM:SS INFO]: ", what Paper/Purpur print to stdout,
# which journald records), so the text right after the prefix is the logger's own message. Chat can't forge a
# line: a player's chat is logged as "<Name> text" (or "[Not Secure] <Name> text", "* Name text" for /me,
# "[Name] text" for /say), so "]: Disconnecting ..." or "]: UUID of player ..." never comes from a player,
# and a chat message can't contain a line break. ANSI colour codes (if a terminal was attached) are stripped
# first.
_PREFIX = r"^\[\d{2}:\d{2}:\d{2} INFO\]: "
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
REJECT_LINE = re.compile(
    _PREFIX + r"Disconnecting (?P<name>[A-Za-z0-9_]{1,16}) \((?P<addr>[^()]{0,80})\): (?P<reason>.*)$"
)
LOST_LINE = re.compile(
    _PREFIX + r"(?P<name>[A-Za-z0-9_]{1,16}) \((?P<addr>[^()]{0,80})\) lost connection: (?P<reason>.*)$"
)
UUID_LINE = re.compile(_PREFIX + r"UUID of player (?P<name>[A-Za-z0-9_]{1,16}) is (?P<uuid>[0-9a-f-]{36})$")
WHITELIST_REASON = re.compile(r"(?i)\bnot\s+white-?listed\b")


def mask_address(addr: str) -> str:
    """'/203.0.113.7:51234' -> '203.0.113.x'; an IPv6 address -> its /48; anything else -> ''."""
    a = addr.strip().lstrip("/")
    a = re.sub(r"^\[(.*)\](?::\d+)?$", r"\1", a)
    if a.count(":") == 1:
        a = a.split(":")[0]
    a = a.split("%")[0]
    try:
        ip = ipaddress.ip_address(a)
    except ValueError:
        return ""
    if ip.version == 4:
        return ".".join(str(ip).split(".")[:3]) + ".x"
    return str(ipaddress.ip_network(f"{ip}/48", strict=False))


class RejectedLogins:
    """The last REJECTED_KEEP whitelist rejections, in the agent's state dir (rejected.json)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()
        self.uuids: dict[str, str] = {}  # name -> uuid from "UUID of player ..." lines (in memory)
        self.recent: dict[
            str, float
        ] = {}  # name -> ts of the last counted rejection (dedupe the lost-connection line)

    def entries(self) -> list[dict[str, Any]]:
        data = read_json(self.path, [])
        if not isinstance(data, list):
            return []
        return [e for e in data if isinstance(e, dict) and NAME.fullmatch(str(e.get("name", "")))]

    def feed(self, lines: Iterable[tuple[int, str]]) -> bool:
        """(ts ms, message) lines of minecraft.service; True if the list changed."""
        hits: list[tuple[int, str, str]] = []
        for ts, raw in lines:
            msg = ANSI.sub("", raw).rstrip("\r\n")
            u = UUID_LINE.match(msg)
            if u:
                self.uuids[u.group("name").lower()] = u.group("uuid")
                if len(self.uuids) > 500:
                    self.uuids.pop(next(iter(self.uuids)))
                continue
            m = REJECT_LINE.match(msg) or LOST_LINE.match(msg)
            if not m or not WHITELIST_REASON.search(m.group("reason")):
                continue
            name = m.group("name")
            if ts - self.recent.get(name.lower(), -(10**12)) < 10000:  # its own "lost connection" line
                continue
            self.recent[name.lower()] = ts
            hits.append((ts, name, mask_address(m.group("addr"))))
        if not hits:
            return False
        with self.lock:
            by_name = {e["name"].lower(): e for e in self.entries()}
            for ts, name, ip in hits:
                e = by_name.get(name.lower())
                if e is None:
                    e = by_name[name.lower()] = {"name": name, "uuid": None, "count": 0, "first": ts}
                e.update(count=int(e.get("count", 0)) + 1, last=ts, ip=ip, name=name)
                e["uuid"] = self.uuids.get(name.lower()) or e.get("uuid")
            keep = sorted(by_name.values(), key=lambda x: -int(x.get("last", 0)))[:REJECTED_KEEP]
            atomic_write(self.path, json.dumps(keep), 0o600)
        return True

    def drop(self, name: str) -> bool:
        with self.lock:
            entries = self.entries()
            keep = [e for e in entries if e["name"].lower() != name.lower()]
            if len(keep) != len(entries):
                atomic_write(self.path, json.dumps(keep), 0o600)
                return True
        return False


# --- plugins ------------------------------------------------------------------------------------------------


def read_lock(path: Path | None = None) -> list[dict[str, str]]:
    """The deployed plugins.lock.yml (name, filename, version), written as JSON by the plugins role."""
    data = read_json(path or PLUGINS_LOCK_FILE, [])
    out = []
    for p in data if isinstance(data, list) else []:
        if (
            isinstance(p, dict)
            and PLUGIN_NAME.fullmatch(str(p.get("name")))
            and JAR.fullmatch(str(p.get("filename")))
        ):
            out.append(
                {"name": str(p["name"]), "filename": str(p["filename"]), "version": str(p.get("version", ""))}
            )
    return out


def read_disabled(path: Path | None = None) -> list[str]:
    data = read_json(path or PLUGINS_DISABLED_FILE, {})
    names = data.get("disabled", []) if isinstance(data, dict) else []
    return sorted({n for n in names if isinstance(n, str) and PLUGIN_NAME.fullmatch(n)})


def write_disabled(names: Iterable[str], login: str, path: Path | None = None) -> None:
    record = {
        "disabled": sorted(set(names)),
        "by": login[:200],
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    atomic_write(path or PLUGINS_DISABLED_FILE, json.dumps(record, sort_keys=True) + "\n", 0o644)


def apply_plugins(server_dir: Path, lock: list[dict[str, str]], disabled: list[str]) -> list[str]:
    """Move each locked jar to plugins/ (enabled) or plugins/disabled/ (disabled). Only jars named in the
    lock move; nothing is deleted or followed (symlinks are left alone). Returns what moved."""
    plugins = server_dir / "plugins"
    off_dir = plugins / "disabled"
    off_dir.mkdir(mode=0o750, exist_ok=True)
    moved = []
    for p in lock:
        on, off = plugins / p["filename"], off_dir / p["filename"]
        src, dst = (on, off) if p["name"] in disabled else (off, on)
        if src.is_file() and not src.is_symlink() and not dst.exists():
            os.replace(src, dst)
            moved.append(f"{p['name']}: {'disabled' if dst == off else 'enabled'}")
    return moved


def read_snapshots(path: Path | None = None) -> list[dict[str, Any]]:
    """restic's snapshot list as backup.py --list-snapshots saved it (newest first, trimmed)."""
    data = read_json(path or SNAPSHOTS_FILE, {})
    snaps = data.get("snapshots", []) if isinstance(data, dict) else []
    out = []
    for s in snaps[:200]:
        if isinstance(s, dict) and SNAPSHOT_ID.fullmatch(str(s.get("id", ""))):
            out.append({k: s[k] for k in ("id", "short_id", "time", "tags", "host", "size") if k in s})
    return out


# --- jobs (countdown + one long action at a time) -----------------------------------------------------------


@dataclass
class Job:
    id: str
    action: str
    label: str  # "Restart", "Server stopping", ...
    login: str
    countdown: int
    state: str = "countdown"  # countdown | running | done | failed | cancelled
    step: str = ""
    deadline: float = 0.0  # end of the countdown (epoch s)
    started: float = 0.0
    finished: float = 0.0
    players: int = 0
    cancel: threading.Event = field(default_factory=threading.Event)
    on_change: Callable[[], None] = field(default=lambda: None, repr=False)

    def set_step(self, step: str) -> None:
        self.step = step
        self.on_change()

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "action": self.action, "label": self.label, "login": self.login,
                "state": self.state, "step": self.step[:300], "countdown": self.countdown,
                "deadline": int(self.deadline * 1000), "started": int(self.started * 1000),
                "finished": int(self.finished * 1000) if self.finished else None,
                "players": self.players}  # fmt: skip


def say_text(label: str, left: int) -> str:
    when = (
        f"{left // 60} minute{'s' if left >= 120 else ''}"
        if left >= 60
        else f"{left} second{'s' if left != 1 else ''}"
    )
    return f"{label} in {when}. Find a safe spot!"


class Jobs:
    """At most one job at a time. `report` gets the job's public state whenever it changes."""

    def __init__(
        self,
        report: Callable[[dict[str, Any]], None],
        rcon: Callable[[str], str] | None = None,
        clock: Callable[[], float] = time.time,
        wait: Callable[[threading.Event, float], bool] | None = None,
        lock_path: Path | None = None,
        background: bool = True,
    ) -> None:
        self.report = report
        self.rcon = rcon or rcon_command
        self.clock = clock
        self.wait = wait or (lambda ev, s: ev.wait(s))
        self.current: Job | None = None
        self.lock = threading.Lock()
        self.lock_path = lock_path  # JOB_LOCK_FILE: shared with the other process (agent or mcctl)
        self.background = background  # False: start() runs the job before returning (mc_agent.py cli)
        self._held: int | None = None  # the lock file's descriptor while a job runs

    def _take_file_lock(self) -> None:
        """The cross-process lock (an flock, released by the kernel if the process dies). A lock file that
        can't be opened (no state dir: a test, a dev box) only loses the cross-process check."""
        if self.lock_path is None:
            return
        try:
            # only mcops takes it (mc-agent and `mc_agent.py cli`): nobody else needs to open it
            fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError as e:
            print(
                f"job lock {self.lock_path}: {type(e).__name__} (one job per process only)", file=sys.stderr
            )
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ActionError(
                "busy: another action is running (started from the panel or mcctl); wait, or cancel it there"
            ) from None
        self._held = fd

    def _release_file_lock(self) -> None:
        fd, self._held = self._held, None
        if fd is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def busy(self) -> Job | None:
        j = self.current
        return j if j and j.state in ("countdown", "running") else None

    def players(self) -> int:
        try:
            m = re.search(r"There are (\d+) of a max", mc_rcon.strip_formatting(self.rcon("list")))
        except (OSError, mc_rcon.RconError, KeyError, ValueError):
            return 0  # server down: nobody to warn
        return int(m.group(1)) if m else 0

    def announce(self, text: str) -> None:
        component = json.dumps(["", {"text": "[Server] ", "color": "gold"}, {"text": text}])
        with contextlib.suppress(OSError, mc_rcon.RconError, KeyError, ValueError):
            self.rcon(f"tellraw @a {component}")
            self.rcon(f"title @a actionbar {json.dumps({'text': text, 'color': 'gold'})}")

    def start(
        self,
        job_id: str,
        action: str,
        label: str,
        login: str,
        countdown: int,
        work: Callable[[Job], str],
        skip_if_empty: bool = True,
        background: bool | None = None,
    ) -> Job:
        if countdown not in COUNTDOWNS:
            raise ActionError(f"countdown must be one of {', '.join(map(str, COUNTDOWNS))} seconds")
        with self.lock:
            other = self.busy()
            if other:
                raise ActionError(
                    f"busy: {other.label.lower()} ({other.state}) started by {other.login}; cancel it first"
                )
            self._take_file_lock()
            job = Job(job_id, action, label, login, countdown, started=self.clock())
            job.on_change = lambda: self.report(job.public())
            self.current = job
        if self.background if background is None else background:
            threading.Thread(
                target=self._run, args=(job, work, skip_if_empty), name=f"job-{action}", daemon=True
            ).start()
        else:
            self._run(job, work, skip_if_empty)
        return job

    def _run(self, job: Job, work: Callable[[Job], str], skip_if_empty: bool) -> None:
        try:
            self._work(job, work, skip_if_empty)
        finally:
            self._release_file_lock()

    def _work(self, job: Job, work: Callable[[Job], str], skip_if_empty: bool) -> None:
        try:
            job.players = self.players() if job.countdown else 0
            left = job.countdown if (job.players or not skip_if_empty) else 0
            job.deadline = self.clock() + left
            if left:
                job.step = f"countdown {left} s, {job.players} player(s) online"
            self.report(job.public())
            while left > 0:
                if left in ANNOUNCE_AT or left == job.countdown:
                    self.announce(say_text(job.label, left))
                nxt = max((t for t in ANNOUNCE_AT if t < left), default=0)
                if self.wait(job.cancel, left - nxt):
                    self.announce(f"{job.label} cancelled.")
                    job.state, job.step, job.finished = (
                        "cancelled",
                        "cancelled during the countdown",
                        self.clock(),
                    )
                    self.report(job.public())
                    return
                left = nxt
                if left >= 10 and skip_if_empty and job.players and self.players() == 0:
                    job.step = "everyone left: no need to wait"
                    left = 0
            job.state, job.deadline = "running", self.clock()
            self.report(job.public())
            job.step = work(job) or "done"
            job.state = "done"
        except ActionError as e:
            job.state, job.step = "failed", str(e)
        except (OSError, subprocess.SubprocessError, mc_rcon.RconError, ValueError) as e:
            job.state, job.step = "failed", f"{job.step or job.action} failed: {type(e).__name__}"
        job.finished = self.clock()
        self.report(job.public())

    def cancel(self, login: str) -> str:
        job = self.busy()
        if job is None:
            raise ActionError("nothing to cancel")
        if job.state != "countdown":
            raise ActionError(f"{job.label.lower()} is already running; it can't be cancelled now")
        job.cancel.set()
        return f"{job.label.lower()} cancelled by {login}"


def rcon_command(line: str) -> str:
    with mc_rcon.connect(timeout=10.0) as r:
        return r.command(line)


def wait_for_rcon(timeout: float = RCON_WAIT_S, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, mc_rcon.RconError, KeyError, ValueError):
            rcon_command("list")
            return True
        sleep(5)
    return False


def unit_state(unit: str) -> dict[str, str]:
    out = subprocess.run(["systemctl", "show", unit, "-p", "ActiveState,Result,SubState"],
                         capture_output=True, text=True, timeout=30, check=False).stdout  # fmt: skip
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def unit_steps(unit: str, since: str) -> list[str]:
    """The STEP lines restore.py printed in this run of the unit (journal; the agent reads it)."""
    out = subprocess.run(["journalctl", "-u", unit, "--since", since, "-o", "cat", "--no-pager", "-n", "200"],
                         capture_output=True, text=True, timeout=30, check=False).stdout  # fmt: skip
    return [ln for ln in out.splitlines() if ln.startswith(("STEP ", "RESTORED", "FAILED"))]


# --- players role: Git + pending adds - pending removes (runs as minecraft during a deploy) -----------------


def desired_players(desired: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """(whitelist by uuid, ops by uuid) a deploy must leave on the server: players.yml from Git plus the
    panel's pending adds minus its pending removes (design §5 "Whitelist → Git" (4)), so a deploy that runs
    before the panel's pull request is merged can't undo a change made in the panel. Ops come from Git only
    and always stay whitelisted."""
    wl = {
        str(p["uuid"]): {"uuid": str(p["uuid"]), "name": str(p["name"])}
        for p in desired.get("whitelist") or []
    }
    ops = {str(o["uuid"]): {"uuid": str(o["uuid"]), "name": str(o["name"]), "level": int(o["level"])}
           for o in desired.get("ops") or []}  # fmt: skip
    for e in desired.get("pending") or []:
        uuid, name = str(e.get("uuid", "")), str(e.get("name", ""))
        if not UUID.fullmatch(uuid) or not NAME.fullmatch(name):
            continue
        if e.get("op") == "add":
            wl.setdefault(uuid, {"uuid": uuid, "name": name})
        elif e.get("op") == "remove" and uuid not in ops:
            wl.pop(uuid, None)
    for uuid, o in ops.items():
        wl.setdefault(uuid, {"uuid": uuid, "name": o["name"]})
    return wl, ops


# The players role's memory (v1.3 whitelist guard), next to whitelist.json in the server dir (as minecraft).
PLAYERS_STATE_FILE = "whitelist-managed.json"


@dataclass
class Reconciled:
    """What reconcile_players did: the changes (empty = nothing changed) and the live players it kept
    although neither Git nor the panel's pending adds have them (names; the UUID if the file has no valid
    name)."""

    changes: list[str] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)


def kept_alert(names: Iterable[str]) -> str:
    """A deploy kept players that are on the live whitelist but not in players.yml (v1.3 whitelist guard)."""
    shown = ", ".join(names)
    return render(
        Alert(
            "warning",
            "A deploy kept whitelisted players that aren't in players.yml",
            impact=f"They can still join ({shown}) and later deploys keep them, but players.yml doesn't list "
            "them (with `minecraft.strict_whitelist: true` a deploy would remove them).",
            cause="They were whitelisted outside Git (console, Discord or the panel while its pull request "
            "failed), and players.yml never got them.",
            todo="Keep them: `mcctl players add NAME` for each (it commits players.yml). Don't want them: "
            "remove them in the panel (Players tab).",
            details=f"on the live whitelist, not in players.yml or pending panel adds: {shown}",
        )
    )


def reconcile_players(
    desired: dict[str, Any],
    server_dir: Path,
    live: bool,
    rcon: Callable[[str], str] | None = None,
    alert: Callable[[list[str]], None] | None = None,
) -> Reconciled:
    """Make whitelist.json/ops.json match `desired` (compared by uuid, and level for ops, so the server's own
    rewrites never count as a change). With `live` (the server runs) the change is applied over RCON:
    `whitelist reload` (by UUID) and `deop` for removed operators. A new operator or a level change takes
    effect when the server next starts (it reads ops.json then): `op <name>` would resolve the *name* with
    Mojang, which could be someone else after a rename, while ops.json holds the UUID from Git.

    Whitelist guard (v1.3): a live player that Git + pending adds don't have is removed only on purpose:
    a pending panel remove, or Git had them at the last reconcile (PLAYERS_STATE_FILE: the UUIDs players.yml
    held then) and doesn't now, i.e. a commit removed them. Any other such player was never in Git
    ("unmanaged": console, Discord, an expired panel add) and is kept, listed in `kept`; `alert` is called
    once with the names whenever one is kept that the last reconcile didn't keep already. No state file yet
    (the first deploy with the guard) = nobody was in Git before: everyone not in Git is kept.
    `desired["strict"]` (server.yml minecraft.strict_whitelist): Git is the only truth, nobody is kept."""
    wl, ops = desired_players(desired)
    wl_file, ops_file = server_dir / "whitelist.json", server_dir / "ops.json"
    state_file = server_dir / PLAYERS_STATE_FILE
    cur_list = [e for e in read_json(wl_file, []) if isinstance(e, dict)]
    cur_wl = {str(e.get("uuid")).lower() for e in cur_list}
    cur_ops_list = [e for e in read_json(ops_file, []) if isinstance(e, dict)]
    cur_ops = {(str(e.get("uuid")).lower(), int(e.get("level") or 0)) for e in cur_ops_list}
    state = read_json(state_file, {})
    state = state if isinstance(state, dict) else {}
    git_before = {str(u) for u in state.get("git") or [] if isinstance(u, str)}
    warned = {str(u) for u in state.get("kept") or [] if isinstance(u, str)}
    git_now = {str(p.get("uuid")).lower() for p in desired.get("whitelist") or []} | set(ops)
    pend = desired.get("pending") or []
    removing = {str(e.get("uuid", "")).lower() for e in pend if e.get("op") == "remove"}
    result = Reconciled()
    unmanaged: list[str] = []
    if not desired.get("strict"):
        unmanaged = sorted(
            u for u in cur_wl - set(wl) if UUID.fullmatch(u) and u not in git_before and u not in removing
        )
        live_names = {str(e.get("uuid")).lower(): str(e.get("name", "")) for e in cur_list}
        for u in unmanaged:
            name = live_names.get(u, "")
            wl[u] = {"uuid": u, "name": name if NAME.fullmatch(name) else u}
        result.kept = sorted((wl[u]["name"] for u in unmanaged), key=str.lower)
        if alert and set(unmanaged) - warned:
            alert(result.kept)
    changes = result.changes
    run = rcon or rcon_command
    if set(wl) != cur_wl:
        added, removed = sorted(set(wl) - cur_wl), sorted(cur_wl - set(wl))
        changes.append(f"whitelist +{len(added)} -{len(removed)} (+{','.join(added)} -{','.join(removed)})")
        body = [wl[u] for u in sorted(wl, key=lambda u: (wl[u]["name"].lower(), u))]
        atomic_write(wl_file, json.dumps(body, indent=2) + "\n", 0o640)
        if live:
            with contextlib.suppress(OSError, mc_rcon.RconError, KeyError, ValueError):
                run("whitelist reload")
    want_ops = {(u, o["level"]) for u, o in ops.items()}
    if want_ops != cur_ops:
        before = {u for u, _ in cur_ops}
        changes.append(
            f"ops +{len(set(ops) - before)} -{len(before - set(ops))} (now {sorted(want_ops)}, "
            f"was {sorted(cur_ops)})"
        )
        if live:  # removals at once; additions and level changes when the server next starts (see above)
            names = {str(e.get("uuid")): str(e.get("name")) for e in cur_ops_list}
            for u in sorted(before - set(ops)):
                if NAME.fullmatch(names.get(u, "")):
                    with contextlib.suppress(OSError, mc_rcon.RconError, KeyError, ValueError):
                        run(f"deop {names[u]}")
        body = [{"uuid": u, "name": o["name"], "level": o["level"], "bypassesPlayerLimit": False}
                for u, o in sorted(ops.items(), key=lambda kv: (kv[1]["name"].lower(), kv[0]))]  # fmt: skip
        atomic_write(ops_file, json.dumps(body, indent=2) + "\n", 0o640)
    # only after the files are written: what Git held at this (successful) reconcile, and whom it kept
    new_state = {"git": sorted(u for u in git_now if UUID.fullmatch(u)), "kept": unmanaged}
    if {k: state.get(k) for k in new_state} != new_state:
        atomic_write(state_file, json.dumps(new_state, indent=2) + "\n", 0o640)
    return result


# --- the CLI half that runs as minecraft --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if args[:1] == ["reconcile-players"] and len(args) in (2, 3) and args[2:] in ([], ["--live"]):
        alerted: list[str] = []
        res = reconcile_players(
            json.loads(Path(args[1]).read_text()),
            SERVER_DIR,
            live=args[2:] == ["--live"],
            alert=alerted.extend,
        )
        print(f"CHANGED: {'; '.join(res.changes)}" if res.changes else "UNCHANGED")
        if res.kept:
            print(
                f"KEPT: {', '.join(res.kept)} (on the live whitelist but not in players.yml: `mcctl players "
                "add NAME` keeps them in Git, the panel removes them; minecraft.strict_whitelist drops them)"
            )
        if alerted:  # the players role sends it as the ops user (mc-run notify.env: the webhook isn't ours)
            print(f"ALERT-KEPT: {' '.join(alerted)}")
        return 0
    names = args[1:]
    if args[:1] == ["alert-kept"] and names and all(NAME.fullmatch(a) or UUID.fullmatch(a) for a in names):
        notify.discord(kept_alert(names))  # as the ops user: mc-run notify.env (players role)
        return 0
    if args != ["apply-plugins"]:
        print(
            "usage: mc_panel.py apply-plugins | reconcile-players DESIRED.json [--live] | alert-kept NAME...",
            file=sys.stderr,
        )
        return 2
    lock, disabled = read_lock(), read_disabled()
    unknown = sorted(set(disabled) - {p["name"] for p in lock})
    if unknown:
        print(f"ignoring disabled names not in the lock: {', '.join(unknown)}")
    moved = apply_plugins(SERVER_DIR, lock, disabled)
    print("; ".join(moved) or "nothing to move")
    return 0


if __name__ == "__main__":
    sys.exit(main())
