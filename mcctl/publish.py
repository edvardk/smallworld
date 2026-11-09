"""`mcctl publish` (v1.3 Part B): preview what a config change will do, validate it, then land it on main.

    mcctl publish [-C DIR] [--route pr|main|deploy] [--dry-run] [--revert] [--wait-empty] [--no-follow]

1. **Preview.** The changes against origin/main (uncommitted files, unpushed commits; `git fetch` first), the
   server.yml settings and players.yml entries they touch, then the CONSEQUENCES, judged by rendering both
   sides (`render.files`: origin/main in a temporary `git worktree`, and the working tree) and comparing the
   rendered Ansible group_vars, Terraform tfvars, backend and files:
   - the Ansible roles and server config overlays that read a changed variable (`Index`: the product's role
     files and `server-config/` templates, with the variables `group_vars/all/main.yml` and the roles'
     defaults derive from them);
   - whether Minecraft restarts: a changed variable read by a file whose change notifies the minecraft role's
     `restart minecraft` handler (`RESTART_FILES`: the config overlays, the unit, the product plugins'
     configs) or one of `RESTART_VARS` (the locks: jar, plugins, JRE; the product plugins switched on/off;
     the server icon). An approximation of what the deploy will decide (it compares the live files): it can
     say "yes" for a value the server already has, never "no" for a change the product restarts for;
   - whether Terraform changes (the tfvars, or the rendered watchdog files differ): then `mcctl apply` (its
     plan and its typed confirmation) runs before anything is merged or deployed; a changed state backend
     is refused (migrate the state by hand);
   - who is online now (`remote.STATUS_SCRIPT` over the tailnet, 20 s; "unknown" when it fails);
   - v1.5, the whitelist guard's warning: players on the live whitelist (mc-agent's inventory, read from the
     watchdog's D1 with the admin token in .env, as `mcctl doctor` does; panel.mode cloudflare) that the new
     players.yml doesn't list and the panel has no pending change for. A deploy removes those players.yml had
     before (and, with minecraft.strict_whitelist, all of them); the rest are kept, but not in Git.
2. **Validate** (as `mcctl validate`: the config repo loads and renders).
3. **Route.** `pr` (default): commit (a message that summarises the change) on `publish/<UTC timestamp>`,
   push, open the pull request, show its checks live (`gh pr checks`, polled, bounded), offer the admin's
   merge when everything but `server / gate` is green (the gate says "needs the admin" for any change a
   person publishes: the admin merges with the ruleset bypass, `gh pr merge --merge --admin`), then follow
   the deploy run of the merge commit (`gh run watch`) and show the server's status. Re-running on a
   `publish/*` branch pushes to the same pull request. `main`: commit and push to main (the admin), then
   follow the deploy. `deploy`: the emergency route, `mcctl deploy` from here (the pinned product), skipping
   CI and Git (publish it afterwards, or the next CI deploy undoes it).
4. **Restart awareness.** v1.3: outside server.yml maintenance.window the preview says the restart is deferred
   to the window (the deploy applies the files, a timer restarts in it: on an empty server, or in its last
   minutes with the countdown) and nothing is asked.
   Otherwise a restart with players online (or nobody known) asks: deploy now (the deploy's
   in-game countdown), wait until the server is empty (polled each minute, at most `--max-wait` minutes),
   or cancel. `--wait-empty` answers "wait". Without a terminal the answer is "wait", and a wait that times
   out cancels: unattended, mcctl never restarts a server with players on it.
   Unattended (no terminal on stdin), the merge question answers "no" unless `--merge` is given: the pull
   request is opened, its checks followed, and it is left for a person to merge. `--merge`/`--no-merge` and
   `--restart now|wait|cancel` answer the questions up front (scripts, the dashboard).
5. `--revert`: a pull request that reverts the last deploy's commits on main (the first-parent commits
   between the two newest runs whose deploy job succeeded, newest first, merges with `-m 1`).
6. `--dry-run`: the preview (and validation) only; `--no-follow`: open the PR or push, then stop.

`mcctl players add|remove|op|deop` land through `pull_request()` + `land()` too (a `players/*` branch; the
gate leaves whitelist changes by a person to the admin, so mcctl offers the admin's merge when green).

Everything outside goes through `ctx.deps` (git, gh, the clock, sleep, `gh run watch`) and `Tools` (the
status probe, the pinned product's `apply`/`deploy`), so tests use temporary git repos and fakes.
"""

from __future__ import annotations

import json
import re
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from mcctl import configrepo, envfile, http, init, metrics, remote, render, schema, upgrade
from mcctl.providers.cloudflare import CloudflareError
from mcctl.schema import ServerConfig

ROUTES = ("pr", "main", "deploy")
PUBLISH_PREFIX, PLAYERS_PREFIX = "publish/", "players/"
CHECKS_TIMEOUT_S = 45 * 60  # a pull request's checks (validate, gate, boot-test)
RUN_APPEAR_S = 5 * 60  # the deploy run of a merge commit shows up in `gh run list`
POLL_S = 15
WAIT_EMPTY_MINUTES = 120
WAIT_EMPTY_POLL_S = 60
STATUS_TIMEOUT_S = 20
RESTART_CHOICES = ("now", "wait", "cancel")

# Product files whose change notifies the minecraft role's `restart minecraft` handler (overlays.yml compares
# the overlays in check mode; the unit and the product plugins' settings are templates with `notify`).
RESTART_FILES = (
    "server-config/**/*.j2",
    "ansible/roles/minecraft/templates/minecraft.service.j2",
    "ansible/roles/plugins/templates/mc-brand.yml.j2",
    "ansible/roles/plugins/templates/mc-telemetry.yml.j2",
)
# Rendered variables that restart through tasks rather than templates: the server jar and the JRE (server
# lock), plugin jars (plugins lock), the product's plugins installed or removed, the server-list icon.
RESTART_VARS = frozenset(
    {"server_lock", "plugins_lock", "mc_brand_enabled", "mc_experience_enabled", "mc_brand_icon_file"}
)
DEFINITION_FILES = ("ansible/group_vars/all/main.yml", "ansible/roles/*/defaults/main.yml")
TEXT_SUFFIXES = frozenset({".yml", ".yaml", ".j2", ".py", ".json", ".xml", ".rules", ".cfg", ".conf", ".sh"})
WORD = re.compile(r"[A-Za-z_]\w*")

Online = Callable[[ServerConfig], remote.Status]


@dataclass
class LiveWhitelist:
    """The live whitelist as mc-agent last reported it (its inventory in the watchdog's D1): `players` is None
    when unknown (`error` says why); `pending` holds the UUIDs with a pending panel change."""

    players: list[dict[str, Any]] | None = None
    pending: set[str] = field(default_factory=set)
    error: str = ""


def live_whitelist(ctx: init.Ctx, cfg: ServerConfig) -> LiveWhitelist:
    """mc-agent's inventory (whitelist + pending panel changes) from the watchdog's D1, read-only, with the
    admin token from .env (never printed). Unknown without the panel or the token, or when the read fails."""
    if cfg.panel.mode != "cloudflare":
        return LiveWhitelist(error="no panel (panel.mode isn't cloudflare)")
    try:
        token = envfile.merged(ctx.root).get("CLOUDFLARE_API_TOKEN", "")
    except envfile.EnvFileError as e:
        return LiveWhitelist(error=str(e))
    if not token:
        return LiveWhitelist(error="needs CLOUDFLARE_API_TOKEN (.env)")
    try:
        account = metrics.zone_account(token, cfg.dns.zone, ctx.deps.transport)
        db = metrics.database(token, account, f"{cfg.name}-watchdog", ctx.deps.transport)
        rows = db.query("SELECT value FROM watchdog WHERE key = 'inventory'")
        inv = json.loads(str(rows[0].get("value") or "null")) if rows else None
    except (http.HttpError, CloudflareError, ValueError, KeyError, TypeError) as e:
        return LiveWhitelist(error=f"couldn't read the watchdog's D1: {_first(e)}")
    if not isinstance(inv, dict) or not isinstance(inv.get("whitelist"), list):
        return LiveWhitelist(error="mc-agent hasn't reported the whitelist yet")
    players = [p for p in inv["whitelist"] if isinstance(p, dict) and isinstance(p.get("uuid"), str)]
    pending = {
        str(e["uuid"]).lower()
        for e in inv.get("pending") or []
        if isinstance(e, dict) and isinstance(e.get("uuid"), str)
    }
    return LiveWhitelist(players=players, pending=pending)


def whitelist_gaps(
    live: LiveWhitelist, new: schema.Players, old: schema.Players | None, strict: bool
) -> tuple[list[str], list[str]]:
    """(removed, kept): live players the new players.yml (whitelist + ops) doesn't list and the panel has no
    pending change for. A deploy removes them if players.yml listed them before (`old`: origin/main, the
    whitelist guard's "Git had them") or with strict_whitelist; it keeps the others (never in Git)."""
    git_new = {p.uuid.lower() for p in new.whitelist} | {o.uuid.lower() for o in new.ops}
    git_old = {p.uuid.lower() for p in old.whitelist} | {o.uuid.lower() for o in old.ops} if old else set()
    removed: list[str] = []
    kept: list[str] = []
    for p in live.players or []:
        uuid = str(p["uuid"]).lower()
        if uuid in git_new or uuid in live.pending:
            continue
        name = str(p.get("name") or uuid)
        (removed if strict or uuid in git_old else kept).append(name)
    return sorted(removed, key=str.lower), sorted(kept, key=str.lower)


def probe_status(cfg: ServerConfig) -> remote.Status:
    """The server's status over the tailnet (players online), bounded; never reads the terminal."""
    argv = remote.ssh_argv(cfg, remote.STATUS_SCRIPT)
    try:
        proc = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=STATUS_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return remote.Status(reachable=False, error=f"no answer within {STATUS_TIMEOUT_S} s")
    except OSError as e:
        return remote.Status(reachable=False, error=str(e))
    if proc.returncode != 0:
        why = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        return remote.Status(reachable=False, error=f"ssh {remote.target(cfg)}: {why[0]}")
    return remote.parse_status(proc.stdout)


@dataclass
class Tools:
    """What publish runs besides git and gh: the status probe, and the pinned product's commands (`apply`,
    `deploy`: re-executed from the config repo's pin, like `mcctl apply` itself)."""

    online: Online = probe_status
    product: upgrade.ProductRunner = upgrade.run_in_product
    release: upgrade.ReleaseRoot = upgrade.release_root  # v1.5.1: a release's checkout (in-house mod entries)
    whitelist: Callable[[init.Ctx, ServerConfig], LiveWhitelist] = live_whitelist
    max_wait_minutes: int = WAIT_EMPTY_MINUTES


# --- consequences ----------------------------------------------------------------------------------------


def _words(path: Path) -> set[str]:
    try:
        return set(WORD.findall(path.read_text(encoding="utf-8", errors="replace")))
    except OSError:
        return set()


def _files(base: Path, pattern: str) -> list[Path]:
    return sorted(p for p in base.glob(pattern) if p.is_file())


@dataclass(frozen=True)
class Index:
    """Which product files read which variable (by name, as a word): roles, overlays, restart sources, and
    the product's own derived variables (defaults whose value names another variable)."""

    roles: Mapping[str, frozenset[str]]
    overlays: Mapping[str, frozenset[str]]
    restart: frozenset[str]
    defs: Mapping[str, frozenset[str]]

    def closure(self, name: str) -> set[str]:
        names = {name}
        while True:
            more = {k for k, words in self.defs.items() if k not in names and words & names}
            if not more:
                return names
            names |= more


def index(product_root: Path) -> Index:
    roles = {}
    for role in sorted((product_root / "ansible" / "roles").iterdir()):
        if role.is_dir():
            words: set[str] = set()
            for f in _files(role, "**/*"):
                if f.suffix in TEXT_SUFFIXES or not f.suffix:
                    words |= _words(f)
            roles[role.name] = frozenset(words)
    overlays = {
        f.relative_to(product_root / "server-config").as_posix().removesuffix(".j2"): frozenset(_words(f))
        for f in _files(product_root / "server-config", "**/*.j2")
    }
    restart: set[str] = set()
    for pattern in RESTART_FILES:
        for f in _files(product_root, pattern):
            restart |= _words(f)
    defs: dict[str, frozenset[str]] = {}
    for pattern in DEFINITION_FILES:
        for f in _files(product_root, pattern):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
            except (OSError, yaml.YAMLError):
                continue
            if isinstance(data, dict):
                for key, value in data.items():
                    defs[str(key)] = frozenset(WORD.findall(json.dumps(value, default=str))) - {str(key)}
    return Index(roles, overlays, frozenset(restart), defs)


@dataclass
class Rendered:
    server: dict[str, Any]
    players: schema.Players
    group_vars: dict[str, Any]
    tfvars: dict[str, Any]
    backend: str
    inventory: str
    other: dict[str, str | bytes]


def rendered(root: Path) -> Rendered:
    """A config repo's rendered inputs (raises configrepo.ConfigError, or ValueError from render)."""
    repo = configrepo.load(root)
    files = render.files(repo)
    other = {p.as_posix(): c for p, c in files.items() if p not in render.FILES}
    gv_text = str(files[render.GROUP_VARS_FILE])
    return Rendered(
        server=yaml.safe_load((root / configrepo.SERVER_YML).read_text()) or {},
        players=repo.players,
        group_vars=yaml.safe_load(gv_text) or {},
        tfvars=json.loads(str(files[render.TFVARS_FILE])),
        backend=str(files[render.BACKEND_FILE]),
        inventory=str(files[render.INVENTORY_FILE]),
        other=other,
    )


def flatten(data: Any, prefix: str = "") -> dict[str, Any]:  # noqa: ANN401
    if isinstance(data, dict) and data:
        out: dict[str, Any] = {}
        for k, v in data.items():
            out |= flatten(v, f"{prefix}{k}.")
        return out
    return {prefix.rstrip("."): data}


def _changed(old: Mapping[str, Any], new: Mapping[str, Any]) -> list[str]:
    return sorted(k for k in set(old) | set(new) if old.get(k) != new.get(k))


def players_diff(old: schema.Players, new: schema.Players) -> list[str]:
    before = {p.uuid: p.name for p in old.whitelist}
    after = {p.uuid: p.name for p in new.whitelist}
    out = [f"+ {after[u]}" for u in after if u not in before]
    out += [f"- {before[u]}" for u in before if u not in after]
    ops_before = {o.uuid: o for o in old.ops}
    ops_after = {o.uuid: o for o in new.ops}
    for u, o in ops_after.items():
        if u not in ops_before or ops_before[u].level != o.level:
            out.append(f"op {o.name} (level {o.level})")
    out += [f"deop {o.name}" for u, o in ops_before.items() if u not in ops_after]
    return out


@dataclass
class Consequences:
    settings: list[str] = field(default_factory=list)  # server.yml paths that changed
    players: list[str] = field(default_factory=list)
    variables: list[str] = field(default_factory=list)  # rendered Ansible variables that changed
    roles: dict[str, list[str]] = field(default_factory=dict)  # role -> its changed variables
    overlays: list[str] = field(default_factory=list)
    restart: bool | None = False  # None: unknown (treated as yes)
    restart_why: list[str] = field(default_factory=list)
    terraform: list[str] = field(default_factory=list)  # tfvars keys (or watchdog files) that changed
    backend: bool = False
    unknown: str = ""  # why the consequences couldn't be judged
    old_players: schema.Players | None = None  # players.yml on origin/main (v1.5: the whitelist warning)
    deferred: str = (
        ""  # v1.3: "HH:MM-HH:MM <tz>" when a restart now would wait for server.yml maintenance.window
    )

    @property
    def restarts(self) -> bool:
        return self.restart is not False


def judge(old: Rendered, new: Rendered, idx: Index) -> Consequences:
    c = Consequences(
        settings=_changed(flatten(old.server), flatten(new.server)),
        players=players_diff(old.players, new.players),
        variables=_changed(old.group_vars, new.group_vars),
        old_players=old.players,
    )
    for var in c.variables:
        names = idx.closure(var)
        for role, words in idx.roles.items():
            if names & words:
                c.roles.setdefault(role, []).append(var)
        hit = [o for o, words in idx.overlays.items() if names & words]
        for o in hit:
            if o not in c.overlays:
                c.overlays.append(o)
        if hit:
            c.roles.setdefault("minecraft", [])
            if var not in c.roles["minecraft"]:
                c.roles["minecraft"].append(var)
        if var in RESTART_VARS:
            c.restart_why.append(f"{var} (jar, plugins, JRE or a product plugin)")
        elif names & idx.restart:
            where = ", ".join(hit) if hit else "the unit or a product plugin's settings"
            c.restart_why.append(f"{var} ({where})")
    if new.inventory != old.inventory:
        c.roles.setdefault("(every role: the inventory's host changed)", [])
        c.restart_why.append("the inventory (another host)")
    c.restart = bool(c.restart_why)
    c.terraform = _changed(old.tfvars, new.tfvars)
    watchdog = sorted(
        k
        for k in set(old.other) | set(new.other)
        if k.startswith(render.WATCHDOG_ASSETS_DIR.as_posix() + "/") and old.other.get(k) != new.other.get(k)
    )
    c.terraform += [f"{k} (watchdog asset)" for k in watchdog]
    c.backend = old.backend != new.backend
    return c


def consequences(ctx: init.Ctx, base: str, idx: Index | None = None) -> Consequences:
    """Render origin/main (a temporary worktree) and the working tree, and judge the difference."""
    idx = idx or index(schema.PRODUCT_ROOT)
    try:
        new = rendered(ctx.root)
    except (configrepo.ConfigError, ValueError, OSError) as e:
        return Consequences(restart=None, unknown=f"the working tree doesn't render ({_first(e)})")
    with tempfile.TemporaryDirectory(prefix="mcctl-publish-") as d:
        wt = Path(d) / "base"
        add = ctx.git("worktree", "add", "-q", "--detach", str(wt), base, check=False)
        if add.returncode != 0:
            return Consequences(restart=None, unknown=f"couldn't check out {base[:12]}: {add.stderr.strip()}")
        try:
            old = rendered(wt)
        except (configrepo.ConfigError, ValueError, OSError) as e:
            c = Consequences(restart=None, unknown=f"origin/main doesn't render ({_first(e)})")
            c.players = []
            return c
        finally:
            ctx.git("worktree", "remove", "--force", str(wt), check=False)
            ctx.git("worktree", "prune", check=False)
    return judge(old, new, idx)


def _first(e: BaseException) -> str:
    errors = getattr(e, "errors", None)
    return str(errors[0] if errors else e).splitlines()[0][:200]


# --- git -------------------------------------------------------------------------------------------------


@dataclass
class Changes:
    base: str  # origin/main's sha
    branch: str
    files: list[str]  # differ from origin/main (committed, uncommitted, untracked)
    uncommitted: list[str]
    unpushed: list[str]  # "<sha> <subject>"


def current_branch(ctx: init.Ctx) -> str:
    proc = ctx.git("symbolic-ref", "-q", "--short", "HEAD", check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        raise init.StepError(
            "the config repo is on a detached HEAD", "`git switch main` (or your publish/* branch) first"
        )
    return proc.stdout.strip()


def fetch_base(ctx: init.Ctx) -> str:
    fetched = ctx.git("fetch", "-q", "origin", "main", check=False)
    if fetched.returncode != 0:
        ctx.ui.warn(f"`git fetch origin main` failed ({(fetched.stderr or '').strip()[:200]}): old main")
    proc = ctx.git("rev-parse", "--verify", "-q", "origin/main", check=False)
    if proc.returncode != 0:
        raise init.StepError("the config repo has no origin/main", "`git push -u origin main` once")
    return proc.stdout.strip()


def _status_paths(porcelain: str) -> list[str]:
    out = []
    for line in porcelain.splitlines():
        if len(line) > 3:
            path = line[3:].strip()
            out.append(path.split(" -> ")[-1].strip('"'))
    return sorted(set(out))


def changes(ctx: init.Ctx, base: str) -> Changes:
    branch = current_branch(ctx)
    uncommitted = _status_paths(ctx.git("status", "--porcelain").stdout)
    unpushed = [ln for ln in ctx.git("log", "--format=%h %s", f"{base}..HEAD").stdout.splitlines() if ln]
    tracked = [f for f in ctx.git("diff", "--name-only", base).stdout.splitlines() if f]
    files = sorted(set(tracked) | set(uncommitted))
    return Changes(base, branch, files, uncommitted, unpushed)


def summary(ch: Changes, c: Consequences) -> str:
    """A one-line description of the change (commit message, PR title)."""
    parts = []
    if configrepo.SERVER_YML in ch.files:
        shown = ", ".join(c.settings[:3]) + (f", +{len(c.settings) - 3} more" if len(c.settings) > 3 else "")
        parts.append(f"server.yml ({shown})" if shown else "server.yml")
    if configrepo.PLAYERS_YML in ch.files:
        parts.append(f"players.yml ({', '.join(c.players[:3])})" if c.players else "players.yml")
    rest = [f for f in ch.files if f not in (configrepo.SERVER_YML, configrepo.PLAYERS_YML)]
    if rest:
        parts.append(", ".join(rest[:3]) + (f" +{len(rest) - 3} more" if len(rest) > 3 else ""))
    return "mcctl publish: " + ("; ".join(parts) or "no file changes")


def stamp(ctx: init.Ctx) -> str:
    return ctx.deps.now().strftime("%Y%m%d-%H%M%S")


def repository(cfg: ServerConfig) -> str:
    return f"{cfg.github.owner}/{cfg.github.config_repo}"


# --- preview ---------------------------------------------------------------------------------------------


def online_text(st: remote.Status | None) -> str:
    if st is None:
        return "not asked"
    if not st.reachable or st.players_online is None:
        return f"unknown ({st.error or 'no answer'})"
    who = f" ({', '.join(st.players)})" if st.players else ""
    return f"{st.players_online}{who}"


def show_whitelist(ctx: init.Ctx, live: LiveWhitelist | None, gaps: tuple[list[str], list[str]]) -> None:
    """v1.5: the whitelist guard's warning in the preview (see the module docstring)."""
    ui = ctx.ui
    if live is None:
        return
    if live.players is None:
        ui.say(f"  live whitelist: unknown ({live.error})")
        return
    removed, kept = gaps
    if removed:
        ui.warn(
            f"live whitelist: a deploy removes {', '.join(removed)}: on the live whitelist, not in "
            "players.yml and no pending panel change. Keep them: `mcctl players add NAME` (or put them back "
            "in players.yml)"
        )
    if kept:
        ui.warn(
            f"live whitelist: {', '.join(kept)} aren't in players.yml (and no pending panel change): the "
            "whitelist guard keeps them, but Git doesn't have them. Keep them: `mcctl players add NAME`; "
            "don't want them: remove them in the panel"
        )
    if not removed and not kept:
        ui.say(f"  live whitelist: {len(live.players)} player(s), all in players.yml or pending in the panel")


def show_preview(ctx: init.Ctx, ch: Changes, c: Consequences, st: remote.Status | None) -> None:
    ui = ctx.ui
    ui.section(f"Changes against origin/main ({ch.base[:12]}), on {ch.branch}")
    if ch.uncommitted:
        ui.say(f"  uncommitted: {', '.join(ch.uncommitted)}")
    for line in ch.unpushed:
        ui.say(f"  unpushed commit: {line}")
    ui.say(f"  files: {', '.join(ch.files)}")
    if c.settings:
        ui.say(f"  server.yml: {', '.join(c.settings)}")
    if c.players:
        ui.say(f"  players.yml: {', '.join(c.players)}")
    ui.section("Consequences")
    if c.unknown:
        ui.warn(f"unknown: {c.unknown}")
    roles = [f"{r} ({', '.join(v)})" if v else r for r, v in sorted(c.roles.items())]
    ui.say(f"  Ansible roles: {'; '.join(roles) if roles else 'none'}")
    ui.say(f"  server config overlays: {', '.join(c.overlays) if c.overlays else 'none'}")
    if c.restart is None:
        ui.warn("Minecraft restart: unknown (treated as yes)")
    elif c.restart:
        ui.warn(f"Minecraft restart: yes ({'; '.join(c.restart_why)})")
    else:
        ui.say("  Minecraft restart: no")
    if c.restarts and c.deferred:
        ui.say(
            f"  restart deferred to the maintenance window {c.deferred}: a deploy now applies the files and "
            "the server restarts in the window: once it is empty, or in its last minutes with the countdown "
            "(`mcctl deploy --force-restart` restarts at once)"
        )
    if c.backend:
        ui.warn("Terraform state backend (server.yml state.*) changes: migrate the state by hand first")
    if c.terraform:
        ui.warn(
            f"Terraform: yes ({', '.join(c.terraform)}): `mcctl apply` (plan, typed confirmation) runs "
            "before the deploy"
        )
    else:
        ui.say("  Terraform: no change")
    ui.say(f"  players online now: {online_text(st)}")


# --- the steps ------------------------------------------------------------------------------------------


def validate(ctx: init.Ctx) -> configrepo.ConfigRepo:
    repo = ctx.repo()
    try:
        render.files(repo)
    except ValueError as e:
        raise init.StepError(f"the config repo doesn't render: {e}") from None
    ctx.ui.ok(f"valid (product {repo.pin.repository}@{repo.pin.sha[:12]})")
    return repo


def terraform_first(ctx: init.Ctx, repo: configrepo.ConfigRepo, c: Consequences, tools: Tools) -> bool:
    if not c.terraform:
        return True
    ctx.ui.section("Terraform first: `mcctl apply` (its plan, then the deployment name typed)")
    if tools.product(repo.pin, ["apply", "-C", str(ctx.root)]) == 0:
        ctx.ui.ok("Terraform applied")
        return True
    return ctx.ui.confirm(
        "publish_without_apply",
        "`mcctl apply` applied nothing (no changes, or not confirmed). Go on without it?",
        default=False,
    )


def restart_ok(
    ctx: init.Ctx, cfg: ServerConfig, c: Consequences, tools: Tools, *, wait_empty: bool = False
) -> bool:
    """True: go on (no restart, nobody online, `now`, or the server became empty); False: cancelled."""
    if not c.restarts:
        return True
    ui = ctx.ui
    if c.deferred:  # v1.3: nobody is disconnected now; the deploy leaves the restart for the window
        ui.info(f"the restart waits for the maintenance window ({c.deferred}): nobody is disconnected now")
        return True
    st = tools.online(cfg)
    n = st.players_online if st.reachable else None
    if n == 0:
        return True
    who = f"{n} player(s) online" if n is not None else f"players online unknown ({st.error or 'no answer'})"
    ui.warn(f"the deploy restarts Minecraft and there are {who}")
    if wait_empty:
        choice = "wait"
    else:
        choice = ui.choose(
            "publish_restart",
            "Deploy now, wait until the server is empty, or cancel?",
            RESTART_CHOICES,
            default="wait",
            descriptions={
                "now": "the deploy's in-game countdown, then the restart",
                "wait": f"check every minute, at most {tools.max_wait_minutes} min",
                "cancel": "nothing is merged, pushed or deployed",
            },
        )
    if choice == "now":
        return True
    if choice == "cancel":
        return False
    return wait_until_empty(ctx, cfg, tools)


def wait_until_empty(ctx: init.Ctx, cfg: ServerConfig, tools: Tools) -> bool:
    ui = ctx.ui
    deadline = ctx.deps.now().timestamp() + tools.max_wait_minutes * 60
    while True:
        st = tools.online(cfg)
        if st.reachable and st.players_online == 0:
            ui.ok("the server is empty: going on")
            return True
        if ctx.deps.now().timestamp() >= deadline:
            ui.warn(f"still not empty after {tools.max_wait_minutes} min ({online_text(st)} online)")
            if not ui.interactive:
                return False  # unattended: never restart on players
            late = ui.choose("publish_late", "Deploy now or cancel?", ("now", "cancel"), default="cancel")
            return late == "now"
        ui.info(f"waiting for an empty server: {online_text(st)} online")
        ctx.deps.sleep(WAIT_EMPTY_POLL_S)


def need_gh(ctx: init.Ctx, why: str) -> None:
    if ctx.deps.which("gh") is None:
        raise init.StepError(
            f"`gh` (GitHub CLI) not found: {why}", "install it and `gh auth login`, or use `--route main`"
        )


def pull_request(ctx: init.Ctx, cfg: ServerConfig, branch: str, title: str, body: str) -> str:
    """Push `branch` and open its pull request (or find the open one): its URL."""
    proc = ctx.git("push", "-q", "-u", "origin", f"{branch}:{branch}", check=False)
    if proc.returncode != 0:
        raise init.StepError(
            f"`git push` of {branch} failed: {(proc.stderr or '').strip()}",
            "check `gh auth status` (and `gh auth setup-git`) and that you can push to the config repo",
        )
    repo = repository(cfg)
    view = ctx.gh("pr", "view", branch, "-R", repo, "--json", "url,state")
    try:
        found = json.loads(view.stdout) if view.returncode == 0 else None
    except ValueError:
        found = None
    if isinstance(found, dict) and found.get("state") == "OPEN" and found.get("url"):
        ctx.ui.ok(f"pushed to the open pull request {found['url']}")
        return str(found["url"])
    pr = ctx.gh(
        "pr", "create", "-R", repo, "--base", "main", "--head", branch, "--title", title, "--body", body
    )
    if pr.returncode != 0:
        raise init.StepError(f"pushed {branch}, but `gh pr create` failed: {(pr.stderr or '').strip()}")
    url = pr.stdout.strip().splitlines()[-1] if pr.stdout.strip() else ""
    ctx.ui.ok(f"pull request: {url}")
    return url


def _is_gate(check: Mapping[str, Any]) -> bool:
    name = str(check.get("name", ""))
    return name == "gate" or name.endswith("/ gate")


def wait_checks(ctx: init.Ctx, url: str) -> str:
    """Poll `gh pr checks` until every check but the gate finished: pass | fail | timeout."""
    ui = ctx.ui
    deadline = ctx.deps.now().timestamp() + CHECKS_TIMEOUT_S
    shown: dict[str, str] = {}
    ui.section("Checks")
    while True:
        proc = ctx.gh("pr", "checks", url, "--json", "name,bucket,state,link")
        try:
            items = json.loads(proc.stdout) if proc.stdout.strip() else None
        except ValueError:
            items = None
        if isinstance(items, list) and items:
            for chk in items:
                name, bucket = str(chk.get("name", "?")), str(chk.get("bucket", "pending"))
                if shown.get(name) == bucket:
                    continue
                shown[name] = bucket
                if _is_gate(chk) and bucket == "fail":
                    ui.say(f"  {name}: needs the admin (expected: you merge it)")
                elif bucket == "pass":
                    ui.ok(f"{name}: passed")
                elif bucket in ("fail", "cancel"):
                    ui.bad(f"{name}: {bucket} {chk.get('link', '')}".rstrip())
                else:
                    ui.say(f"  {name}: {bucket}")
            others = [c for c in items if not _is_gate(c)]
            if any(c.get("bucket") in ("fail", "cancel") for c in others):
                return "fail"
            if others and not any(c.get("bucket") == "pending" for c in items):
                return "pass"
        if ctx.deps.now().timestamp() >= deadline:
            ui.warn(f"the checks didn't finish within {CHECKS_TIMEOUT_S // 60} min: {url}")
            return "timeout"
        ctx.deps.sleep(POLL_S)


def merge_commit(ctx: init.Ctx, url: str) -> str:
    proc = ctx.gh("pr", "view", url, "--json", "mergeCommit,state")
    try:
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except ValueError:
        data = {}
    commit = (data or {}).get("mergeCommit") or {}
    return str(commit.get("oid", "")) if isinstance(commit, dict) else ""


def sync_main(ctx: init.Ctx) -> None:
    """Back on main, fast-forwarded to origin/main (the merge)."""
    ctx.git("checkout", "-q", "main", check=False)
    ctx.git("fetch", "-q", "origin", "main", check=False)
    if ctx.git("merge", "-q", "--ff-only", "origin/main", check=False).returncode != 0:
        ctx.ui.warn("local main didn't fast-forward to origin/main: `git pull` it yourself")


def follow_deploy(ctx: init.Ctx, cfg: ServerConfig, sha: str, tools: Tools) -> int:
    """Find the workflow run of `sha` on main, watch it, then say whether it deployed and show the status."""
    ui = ctx.ui
    repo = repository(cfg)
    deadline = ctx.deps.now().timestamp() + RUN_APPEAR_S
    ui.section(f"Deploy of {sha[:12]}")
    run: dict[str, Any] | None = None
    while run is None:
        proc = ctx.gh(
            "run", "list", "-R", repo, "--workflow", "server.yml", "--branch", "main", "--commit", sha,
            "--limit", "5", "--json", "databaseId,status,conclusion,url,event",
        )  # fmt: skip
        try:
            runs = json.loads(proc.stdout) if proc.returncode == 0 else []
        except ValueError:
            runs = []
        runs = [r for r in runs if isinstance(r, dict) and r.get("event") in ("push", "workflow_dispatch")]
        if runs:
            run = runs[0]
            break
        if ctx.deps.now().timestamp() >= deadline:
            ui.warn(f"no workflow run for {sha[:12]} after {RUN_APPEAR_S // 60} min: see the Actions tab")
            return 1
        ctx.deps.sleep(POLL_S)
    run_id = str(run.get("databaseId"))
    ui.say(f"  run {run_id}: {run.get('url', '')}")
    rc = ctx.deps.stream(["gh", "run", "watch", run_id, "-R", repo, "--exit-status"])
    view = ctx.gh("run", "view", run_id, "-R", repo, "--json", "conclusion,jobs")
    try:
        data = json.loads(view.stdout) if view.returncode == 0 else {}
    except ValueError:
        data = {}
    jobs = [j for j in (data or {}).get("jobs") or [] if isinstance(j, dict)]
    deploy = next((j for j in jobs if str(j.get("name", "")).split("/")[-1].strip() == "deploy"), None)
    verdict = (deploy or {}).get("conclusion") or "unknown"
    if rc == 0 and verdict == "success":
        ui.ok("deployed")
    elif rc == 0 and verdict == "skipped":
        ui.say("  the deploy job was skipped (nothing deploy-relevant changed, or deploy.enabled is false)")
    elif rc == 0:
        ui.say(f"  workflow run passed (deploy job: {verdict})")
    else:
        ui.bad(f"the workflow run failed (deploy job: {verdict}): {run.get('url', '')}")
    status_summary(ctx, cfg, tools)
    return 0 if rc == 0 else 1


def status_summary(ctx: init.Ctx, cfg: ServerConfig, tools: Tools) -> None:
    st = tools.online(cfg)
    if not st.reachable:
        ctx.ui.warn(f"status: unknown ({st.error})")
        return
    ctx.ui.say(
        f"  status: {st.server or 'unknown'}, players {online_text(st)}"
        + (f", TPS {st.tps:.1f}" if st.tps is not None else "")
        + ("; `mcctl doctor` for the full check" if st.server != "active" else "")
    )


def land(
    ctx: init.Ctx,
    cfg: ServerConfig,
    url: str,
    tools: Tools,
    *,
    before_merge: Callable[[], bool] = lambda: True,
    follow: bool = True,
) -> int:
    """A pull request's checks, the admin's merge when green, the deploy of the merge commit."""
    ui = ctx.ui
    if not follow:
        ui.say(f"  not followed (--no-follow): merge {url} once its checks passed; that deploys it")
        return 0
    verdict = wait_checks(ctx, url)
    if verdict != "pass":
        ui.bad(
            "not merged: a check failed" if verdict == "fail" else "not merged: the checks are still running"
        )
        ui.say(f"  fix it and run `mcctl publish` again on this branch (same pull request): {url}")
        return 1
    # At a terminal the suggested answer is yes; unattended (no terminal, no --merge) it is no: the pull
    # request is opened and left for a person, never merged (and deployed) by default.
    if not ui.confirm("publish_merge", "Checks passed. Merge it now (it deploys)?", default=ui.interactive):
        ui.say(f"  not merged: {url} waits for you (merging it deploys)")
        return 0
    if not before_merge():
        ui.say(f"  not merged (cancelled): {url} waits for you")
        return 0
    merged = ctx.gh("pr", "merge", url, "--merge", "--admin")
    if merged.returncode != 0:
        why = (merged.stderr or "").strip()
        raise init.StepError(f"`gh pr merge` failed: {why}", f"merge {url} on GitHub")
    ui.ok("merged")
    sha = merge_commit(ctx, url)
    sync_main(ctx)
    if not sha:
        ui.warn("couldn't read the merge commit: follow the deploy in the Actions tab")
        return 0
    return follow_deploy(ctx, cfg, sha, tools)


# --- routes ---------------------------------------------------------------------------------------------


def _route_pr(
    ctx: init.Ctx,
    repo: configrepo.ConfigRepo,
    ch: Changes,
    c: Consequences,
    tools: Tools,
    *,
    title: str,
    body: str,
    wait_empty: bool,
    follow: bool,
) -> int:
    branch = ch.branch
    if not branch.startswith(PUBLISH_PREFIX):
        branch = f"{PUBLISH_PREFIX}{stamp(ctx)}"
        ctx.git("checkout", "-q", "-b", branch)
    if ch.uncommitted:
        ctx.git("add", "-A")
        ctx.git("commit", "-q", "-m", title)
        ctx.ui.ok(f"committed on {branch}: {title}")
    url = pull_request(ctx, repo.server, branch, title, body)

    def before_merge() -> bool:
        return terraform_first(ctx, repo, c, tools) and restart_ok(
            ctx, repo.server, c, tools, wait_empty=wait_empty
        )

    return land(ctx, repo.server, url, tools, before_merge=before_merge, follow=follow)


def _route_main(
    ctx: init.Ctx,
    repo: configrepo.ConfigRepo,
    ch: Changes,
    c: Consequences,
    tools: Tools,
    *,
    title: str,
    wait_empty: bool,
    follow: bool,
) -> int:
    if ch.branch != "main":
        raise init.StepError(f"--route main pushes main, but this is {ch.branch}", "`git switch main` first")
    if not terraform_first(ctx, repo, c, tools):
        return 1
    if not restart_ok(ctx, repo.server, c, tools, wait_empty=wait_empty):
        ctx.ui.say("  cancelled: nothing was pushed")
        return 0
    sha = ctx.commit(title, ["."], push=True)
    if not follow or ctx.deps.which("gh") is None:
        ctx.ui.say("  pushed; the deploy runs in the config repo's Actions tab")
        return 0
    return follow_deploy(ctx, repo.server, sha, tools)


def _route_deploy(
    ctx: init.Ctx, repo: configrepo.ConfigRepo, c: Consequences, tools: Tools, *, wait_empty: bool
) -> int:
    ui = ctx.ui
    ui.warn(
        "EMERGENCY route: `mcctl deploy` from this machine, skipping CI (validate, gate, boot test) and Git"
    )
    if not terraform_first(ctx, repo, c, tools):
        return 1
    if not restart_ok(ctx, repo.server, c, tools, wait_empty=wait_empty):
        ui.say("  cancelled: nothing was deployed")
        return 0
    rc = tools.product(repo.pin, ["deploy", "-C", str(ctx.root)])
    ui.warn("main doesn't have this change: publish it (`mcctl publish`), or the next CI deploy undoes it")
    return rc


def run(
    ctx: init.Ctx,
    *,
    route: str = "pr",
    dry_run: bool = False,
    revert: bool = False,
    wait_empty: bool = False,
    follow: bool = True,
    message: str | None = None,
    tools: Tools | None = None,
    idx: Index | None = None,
) -> int:
    tools = tools or Tools()
    if route not in ROUTES:
        raise init.StepError(f"--route must be one of {', '.join(ROUTES)}")
    if revert:
        if route != "pr":
            raise init.StepError("--revert always opens a pull request (leave out --route)")
        return run_revert(ctx, dry_run=dry_run, wait_empty=wait_empty, follow=follow, tools=tools, idx=idx)
    current_branch(ctx)
    if route == "pr" and not dry_run:
        need_gh(ctx, "the pull request route needs it")
    base = fetch_base(ctx)
    ch = changes(ctx, base)
    if not ch.files:
        ctx.ui.ok("nothing to publish: the config repo matches origin/main")
        return 0
    if ch.branch not in ("main",) and not ch.branch.startswith(PUBLISH_PREFIX):
        raise init.StepError(
            f"publish works from main or a {PUBLISH_PREFIX}* branch, not {ch.branch}",
            "`git switch main` first",
        )
    return _publish(ctx, ch, route=route, dry_run=dry_run, wait_empty=wait_empty, follow=follow,
                    message=message, tools=tools, idx=idx)  # fmt: skip


def _publish(
    ctx: init.Ctx,
    ch: Changes,
    *,
    route: str,
    dry_run: bool,
    wait_empty: bool,
    follow: bool,
    message: str | None,
    tools: Tools,
    idx: Index | None,
    body_extra: str = "",
) -> int:
    ui = ctx.ui
    c = consequences(ctx, ch.base, idx)
    st: remote.Status | None = None
    live: LiveWhitelist | None = None
    gaps: tuple[list[str], list[str]] = ([], [])
    try:
        loaded = configrepo.load(ctx.root)
        cfg = loaded.server
        c.deferred = schema.restart_deferred(cfg, ctx.deps.now()) or ""  # the window the change deploys with
        st = tools.online(cfg)
        live = tools.whitelist(ctx, cfg)
        gaps = whitelist_gaps(live, loaded.players, c.old_players, cfg.minecraft.strict_whitelist)
    except configrepo.ConfigError:
        st = None
    show_preview(ctx, ch, c, st)
    show_whitelist(ctx, live, gaps)
    ui.section("Validate")
    repo = validate(ctx)
    if c.backend:
        raise init.StepError(
            "server.yml state.* changes the Terraform state backend: not published",
            "migrate the state by hand (terraform init -migrate-state), then publish",
        )
    title = message or summary(ch, c)
    if dry_run:
        ui.say(f"(dry run: nothing was committed, pushed or deployed; route {route} would land: {title})")
        return 0
    if route == "deploy":
        return _route_deploy(ctx, repo, c, tools, wait_empty=wait_empty)
    if route == "main":
        return _route_main(ctx, repo, ch, c, tools, title=title, wait_empty=wait_empty, follow=follow)
    body = "\n".join(
        [
            f"Published with `mcctl publish` from {ch.branch}.",
            "",
            *([f"- server.yml: {', '.join(c.settings)}"] if c.settings else []),
            *([f"- players.yml: {', '.join(c.players)}"] if c.players else []),
            f"- files: {', '.join(ch.files)}",
            f"- Ansible roles: {', '.join(sorted(c.roles)) or 'none'}",
            f"- Minecraft restart: {'yes' if c.restart else 'unknown' if c.restart is None else 'no'}"
            + (
                f" (deferred to the maintenance window {c.deferred} if deployed now)"
                if c.restarts and c.deferred
                else ""
            ),
            f"- Terraform: {'yes (applied before the merge)' if c.terraform else 'no change'}",
            "",
            "`server / gate` leaves a change published by a person to the admin's merge.",
            *([body_extra] if body_extra else []),
        ]
    )
    return _route_pr(ctx, repo, ch, c, tools, title=title, body=body, wait_empty=wait_empty, follow=follow)


# --- revert ---------------------------------------------------------------------------------------------


def last_deploys(ctx: init.Ctx, cfg: ServerConfig, want: int = 2) -> list[str]:
    """Head SHAs of the newest workflow runs on main whose deploy job succeeded (newest first)."""
    repo = repository(cfg)
    proc = ctx.gh(
        "run", "list", "-R", repo, "--workflow", "server.yml", "--branch", "main", "--status", "success",
        "--limit", "50", "--json", "databaseId,headSha,event",
    )  # fmt: skip
    try:
        runs = json.loads(proc.stdout) if proc.returncode == 0 else None
    except ValueError:
        runs = None
    if runs is None:
        raise init.StepError(f"`gh run list` failed: {(proc.stderr or '').strip()}", "check `gh auth status`")
    out: list[str] = []
    for r in runs:
        if not isinstance(r, dict) or r.get("headSha") in out:
            continue
        view = ctx.gh("run", "view", str(r.get("databaseId")), "-R", repo, "--json", "jobs")
        try:
            jobs = (json.loads(view.stdout) or {}).get("jobs") or [] if view.returncode == 0 else []
        except ValueError:
            jobs = []
        if any(
            isinstance(j, dict)
            and str(j.get("name", "")).split("/")[-1].strip() == "deploy"
            and j.get("conclusion") == "success"
            for j in jobs
        ):
            out.append(str(r.get("headSha")))
            if len(out) >= want:
                break
    return out


def run_revert(
    ctx: init.Ctx,
    *,
    dry_run: bool,
    wait_empty: bool,
    follow: bool,
    tools: Tools,
    idx: Index | None = None,
) -> int:
    ui = ctx.ui
    start = current_branch(ctx)
    need_gh(ctx, "--revert finds the last deploy with it")
    if ctx.git("status", "--porcelain").stdout.strip():
        raise init.StepError("the config repo has uncommitted changes", "commit, publish or stash them first")
    cfg = ctx.repo().server
    base = fetch_base(ctx)
    deploys = last_deploys(ctx, cfg)
    if not deploys:
        raise init.StepError("no successful deploy found on main (`gh run list`): nothing to revert")
    head = deploys[0]
    if ctx.git("merge-base", "--is-ancestor", head, base, check=False).returncode != 0:
        raise init.StepError(f"the last deploy's commit {head[:12]} isn't on origin/main", "fetch and retry")
    rng = f"{deploys[1]}..{head}" if len(deploys) > 1 else f"{head}^!"
    commits = [c for c in ctx.git("rev-list", "--first-parent", rng).stdout.split() if c]
    if not commits:
        raise init.StepError(f"the last deploy ({head[:12]}) changed nothing since the one before")
    ui.section(f"Revert the last deploy ({head[:12]})" + ("" if len(deploys) > 1 else ": the only one found"))
    for line in ctx.git("log", "--no-walk=unsorted", "--format=  %h %s", *commits).stdout.splitlines():
        ui.say(line)
    if base != head:
        ui.warn(f"main moved on since that deploy ({base[:12]}): the revert may conflict with newer commits")
    if dry_run:
        ui.say(f"(dry run: a pull request would revert {len(commits)} commit(s); nothing was changed)")
        return 0
    branch = f"{PUBLISH_PREFIX}revert-{stamp(ctx)}"
    ctx.git("checkout", "-q", "-b", branch, base)
    for sha in commits:
        parents = ctx.git("rev-list", "--parents", "-n", "1", sha).stdout.split()
        mainline = ["-m", "1"] if len(parents) > 2 else []
        proc = ctx.git("revert", "--no-edit", *mainline, sha, check=False)
        if proc.returncode != 0:
            ctx.git("revert", "--abort", check=False)
            ctx.git("checkout", "-q", "-f", start, check=False)
            ctx.git("branch", "-q", "-D", branch, check=False)
            raise init.StepError(
                f"`git revert {sha[:12]}` conflicts: {(proc.stderr or proc.stdout).strip()[:300]}",
                "revert it by hand on a branch and publish that",
            )
    ch = changes(ctx, base)
    if not ch.files:
        ctx.git("checkout", "-q", "-f", start, check=False)
        ctx.git("branch", "-q", "-D", branch, check=False)
        ui.ok("the revert changes nothing on main today: nothing to publish")
        return 0
    return _publish(
        ctx, ch, route="pr", dry_run=False, wait_empty=wait_empty, follow=follow,
        message=f"mcctl publish --revert: undo the deploy of {head[:12]}", tools=tools, idx=idx,
        body_extra=f"Reverts {len(commits)} commit(s) of the deploy of {head} (`mcctl publish --revert`).",
    )  # fmt: skip


# --- players.yml (mcctl players) --------------------------------------------------------------------------


def players_pr(
    ctx: init.Ctx, title: str, slug: str, *, follow: bool = True, tools: Tools | None = None
) -> int:
    """players.yml (already written and validated) on a `players/*` branch: commit, push, pull request, then
    the admin's merge when green (`land`). A whitelist change never restarts the server."""
    tools = tools or Tools()
    need_gh(ctx, "players changes go through a pull request (`--push`: straight to main)")
    start = current_branch(ctx)
    if start != "main":
        raise init.StepError(f"players opens its pull request from main, not {start}", "`git switch main`")
    cfg = ctx.repo().server
    branch = f"{PLAYERS_PREFIX}{re.sub(r'[^a-z0-9-]+', '-', slug.lower()).strip('-')}-{stamp(ctx)}"
    ctx.git("checkout", "-q", "-b", branch)
    try:
        ctx.git("add", "--", configrepo.PLAYERS_YML)
        ctx.git("commit", "-q", "-m", title)
        body = (
            f"`{title}`: players.yml only (the deploy's players role applies it live, no restart)."
            "\n\n`server / gate` merges whitelist-only changes by itself only from the panel App; this one "
            "waits for the admin's merge (`mcctl` offers it when the checks are green)."
        )
        url = pull_request(ctx, cfg, branch, title, body)
    except init.StepError:
        ctx.git("checkout", "-q", "-f", start, check=False)
        ctx.git("branch", "-q", "-D", branch, check=False)
        raise
    ctx.git("checkout", "-q", start)
    return land(ctx, cfg, url, tools, follow=follow)
