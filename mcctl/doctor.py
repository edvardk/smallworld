"""`mcctl doctor [--fix]` (design §4): is this laptop + config repo ready, and when do credentials expire?

Checks, in order: tools (git, gh logged in, terraform at the tested version, tailscale up, ssh), `.env`
(present, mode 0600; `--fix` chmods it), the config repo (schema, placeholders), the product pin vs the
running mcctl, the pinned Ansible collections (`--fix` installs them), every admin credential live
(Cloudflare, state backend variables, GitHub admin PAT, product-read PAT, Tailscale OAuth client, Discord
webhook when alerts are on), `expiry.yml` (written from the checks), the domain's RDAP expiry, DNS vs
`host.*` (and a public IPv6 on the host that host.ipv6 lacks), tailnet reachability, the watchdog Worker's
freshness (`/status`), and a clean read-only `terraform plan` (no pending changes, no drift).

Every outside call goes through `Deps`, so tests run the whole doctor against recorded responses and fake
tools. Credential values are never printed.
"""

from __future__ import annotations

import datetime
import json
import os
import shutil
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from mcctl import (
    configrepo,
    deploy,
    envfile,
    expiry,
    galaxy,
    ghapp,
    hostnet,
    http,
    metrics,
    product,
    productsetup,
    remote,
    render,
    schema,
    secrets,
    state,
    tf,
    ui,
    yamlio,
)
from mcctl import dashlane as dashlane_mod
from mcctl.providers import (
    CredentialCheck,
    ProviderError,
    cloudflare,
    discord,
    github,
    rdap,
    tailscale,
    watchdog,
)

Level = Literal["ok", "warn", "fail", "skip"]
WARN_DAYS = 30
STEP = "doctor"
TOOLS = ("git", "gh", "terraform", "tailscale", "ssh")


@dataclass
class Result:
    name: str
    level: Level
    detail: str


def _resolve(host: str, family: int) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)
    except socket.gaierror:
        return set()
    return {str(info[4][0]) for info in infos}


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def _plan(repo: configrepo.ConfigRepo, product_root: Path) -> tf.Plan:
    """The read-only `terraform plan` of the "terraform plan" check (render, init, plan -lock=false)."""
    terraform, out = deploy.terraform_for(repo, product_root, purpose="plan with")
    return terraform.drift(out / render.TFVARS_FILE, tf.data_dir(repo.root) / "tfplan.doctor")


@dataclass
class Deps:
    """Everything doctor touches outside the process (tests replace each)."""

    transport: http.Transport = http.urllib_transport
    run: Callable[[Sequence[str]], subprocess.CompletedProcess[str]] = _run
    which: Callable[[str], str | None] = shutil.which
    resolve: Callable[[str, int], set[str]] = _resolve
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC)
    app_sign: Callable[[str, bytes], bytes] = ghapp.openssl_sign  # GitHub App JWTs
    dashlane: dashlane_mod.Resolver = field(default_factory=lambda: dashlane_mod.DEFAULT)  # P14: `dcli read`
    collections: Callable[[Path], Path] = lambda root: galaxy.ensure(root)  # `--fix`: install the pins
    # "terraform plan" (read-only); None: not checked (init's last step, right after its own apply)
    plan: Callable[[configrepo.ConfigRepo, Path], tf.Plan] | None = _plan


@dataclass
class Report:
    results: list[Result] = field(default_factory=list)

    def add(self, name: str, level: Level, detail: str) -> None:
        self.results.append(Result(name, level, detail))

    @property
    def failed(self) -> bool:
        return any(r.level == "fail" for r in self.results)

    def format(self) -> str:
        width = max((len(r.name) for r in self.results), default=0)
        return "\n".join(f"[{r.level:>4}] {r.name:<{width}}  {r.detail}" for r in self.results)

    def counts(self) -> str:
        n = {lv: sum(r.level == lv for r in self.results) for lv in ("ok", "warn", "fail", "skip")}
        return ", ".join(f"{v} {k}" for k, v in n.items() if v)


def show(report: Report, out: ui.Out | None = None, title: str = "mcctl doctor") -> None:
    """The report: plain, the classic `[  ok] name  detail` lines; rich, a table and the totals."""
    out = out or ui.stdout()
    if not out.rich:
        out.say(report.format())
        return
    rows = [
        [f"{'info' if r.level == 'skip' else r.level}: {r.level}", r.name, r.detail] for r in report.results
    ]
    out.table(["", "Check", "Detail"], rows, title=title, styles=("", "bold"), indent=2)
    (out.bad if report.failed else out.ok)(report.counts())


# --- tools --------------------------------------------------------------------------------------------


def terraform_version_level(found: str | None) -> tuple[Level, str]:
    if found is None:
        return "fail", "can't read `terraform version -json`"
    if found == tf.TERRAFORM_VERSION:
        return "ok", found
    want = tf.TERRAFORM_VERSION.split(".")
    have = found.split(".")
    if have[:2] == want[:2] and have[2:3] >= want[2:3]:  # versions.tf: ~> 1.17.1
        return "warn", f"{found} (the product is tested with {tf.TERRAFORM_VERSION})"
    return (
        "fail",
        f"{found}; the product needs {tf.TERRAFORM_VERSION} (versions.tf `~> {tf.TERRAFORM_VERSION}`)",
    )


def check_tools(report: Report, deps: Deps) -> None:
    for tool in TOOLS:
        path = deps.which(tool)
        if path is None:
            report.add(f"tool {tool}", "fail", "not found on PATH (install it; see the quick start)")
            continue
        level: Level = "ok"
        detail = path
        if tool == "gh":
            if deps.run(["gh", "auth", "status"]).returncode != 0:
                level, detail = "warn", "not logged in: `gh auth login` (status, private product checkout)"
        elif tool == "terraform":
            proc = deps.run(["terraform", "version", "-json"])
            try:
                found = str(json.loads(proc.stdout)["terraform_version"])
            except (ValueError, KeyError, TypeError):
                found = None
            level, detail = terraform_version_level(found)
        elif tool == "tailscale":
            proc = deps.run(["tailscale", "status", "--json"])
            try:
                backend = json.loads(proc.stdout).get("BackendState")
            except (ValueError, AttributeError):
                backend = None
            if backend != "Running":
                level, detail = "warn", f"not connected (BackendState {backend!r}): `tailscale up`"
        report.add(f"tool {tool}", level, detail)


# --- .env ---------------------------------------------------------------------------------------------


def check_envfile(report: Report, root: Path, fix: bool) -> None:
    path = root / envfile.ENV_FILE
    if not path.is_file():
        report.add(".env", "warn", f"{path} missing: copy .env.example to .env (chmod 600) and fill it in")
        return
    problem = envfile.mode_problem(path)
    if problem and fix:
        path.chmod(0o600)
        report.add(".env", "ok", "mode fixed to 0600")
    elif problem:
        report.add(".env", "fail", problem)
    else:
        report.add(".env", "ok", "mode 0600")


def check_dashlane(report: Report, root: Path, resolver: dashlane_mod.Resolver) -> None:
    """P14: every `dl://` value in .env resolves through dcli (names and references only, never values)."""
    try:
        raw = envfile.load(root, require_private=False)
    except envfile.EnvFileError:
        return  # reported by check_envfile
    refs = [(k, v) for k, v in raw.items() if dashlane_mod.is_ref(v)]
    if not refs:
        return
    if not resolver.available():
        report.add(
            "dashlane",
            "fail",
            f"{len(refs)} .env value(s) are Dashlane references but dcli isn't installed "
            f"({dashlane_mod.INSTALL})",
        )
        return
    for key, ref, problem in resolver.check(dict(refs)):
        report.add(f"dashlane {key}", "fail" if problem else "ok", problem or f"{ref} resolves")


def check_discord_commands(
    report: Report,
    cfg: schema.ServerConfig,
    env: Mapping[str, str],
    deps: Deps,
    guarded: Callable[[str, Callable[[], CredentialCheck]], None],
) -> None:
    """P14: the bot token still belongs to discord.application_id and the endpoint is this Worker's."""
    token = env.get(discord.BOT_TOKEN_ENV)
    if not cfg.discord.commands:
        if token:
            report.add(
                "discord commands", "warn", f"{discord.BOT_TOKEN_ENV} is set but discord.commands is false"
            )
        return
    if not token:
        report.add(
            "discord commands", "warn",
            f"{discord.BOT_TOKEN_ENV} isn't set (.env): the commands work, but `mcctl discord sync` needs it",
        )  # fmt: skip
        return
    seen: list[CredentialCheck] = []

    def bot() -> CredentialCheck:
        seen.append(discord.check_bot(token, deps.transport, cfg.discord.application_id))
        return seen[-1]

    guarded("discord bot token", bot)
    if not seen or seen[-1].level == "fail":
        return
    want = render.watchdog_url(cfg) + discord.PATH
    got = seen[-1].facts.get("endpoint", "")
    if got == want:
        report.add("discord endpoint", "ok", f"interactions go to {want}")
    else:
        report.add(
            "discord endpoint", "fail",
            f"the application's interactions endpoint is {got or 'unset'}, not {want}: `mcctl discord sync`",
        )  # fmt: skip


# --- credentials --------------------------------------------------------------------------------------


def _add_check(report: Report, label: str, check: CredentialCheck) -> None:
    report.add(label, check.level, "; ".join(check.messages) or check.level)


def check_credentials(
    report: Report, repo: configrepo.ConfigRepo, env: Mapping[str, str], deps: Deps
) -> list[CredentialCheck]:
    cfg = repo.server
    checks: list[CredentialCheck] = []

    def guarded(label: str, fn: Callable[[], CredentialCheck]) -> None:
        try:
            check = fn()
        except http.HttpError as e:
            report.add(label, "fail", f"network error: {e}")
            return
        checks.append(check)
        _add_check(report, label, check)

    if token := env.get("CLOUDFLARE_API_TOKEN"):
        guarded(
            "cloudflare token",
            lambda: cloudflare.check(
                token,
                cfg.dns.zone,
                deps.transport,
                now=deps.now(),
                s3_endpoint=env.get("AWS_ENDPOINT_URL_S3"),
            ),
        )
    else:
        report.add("cloudflare token", "fail", "CLOUDFLARE_API_TOKEN isn't set (.env)")
    account = next((c.facts.get("account_id") for c in checks if c.facts.get("account_id")), None)
    if cfg.panel.mode == "cloudflare" and token and account:
        check_zero_trust(report, cfg, token, account, deps)

    missing = [
        v for v in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3") if not env.get(v)
    ]
    if missing:
        report.add("state backend", "fail", f"{', '.join(missing)} not set (.env): the Terraform state token")
    else:
        report.add(
            "state backend", "ok", f"variables set for bucket {cfg.state.bucket} (`mcctl plan` uses them)"
        )

    repository = f"{cfg.github.owner}/{cfg.github.config_repo}"
    if token := env.get("GITHUB_TOKEN"):
        guarded("github admin token", lambda: github.check_admin(token, repository, deps.transport))
    else:
        report.add("github admin token", "fail", "GITHUB_TOKEN isn't set (.env)")

    if admin_token := env.get("GITHUB_TOKEN"):
        check_ci_oidc(report, repo, admin_token, deps)

    if token := env.get("TF_VAR_product_read_token"):
        guarded(
            "product read token",
            lambda: github.check_product_read(token, repo.pin.repository, repo.pin.sha, deps.transport),
        )
    else:
        public = github.repo_is_public(repo.pin.repository, deps.transport)
        if public:
            report.add("product read token", "ok", f"{repo.pin.repository} is public: not needed")
        else:
            report.add(
                "product read token",
                "warn",
                "TF_VAR_product_read_token isn't set: CI can't check out the product while it's private",
            )

    client_id, secret = env.get("TAILSCALE_OAUTH_CLIENT_ID"), env.get("TAILSCALE_OAUTH_CLIENT_SECRET")
    if client_id and secret:
        guarded("tailscale oauth client", lambda: tailscale.check_oauth(client_id, secret, deps.transport))
    elif env.get("TAILSCALE_API_KEY"):
        report.add("tailscale oauth client", "warn", "not set; TAILSCALE_API_KEY is (first ACL apply only)")
    else:
        report.add("tailscale oauth client", "fail", "TAILSCALE_OAUTH_CLIENT_ID/_SECRET aren't set (.env)")
    if env.get("TAILSCALE_API_KEY"):
        report.add(
            "tailscale api key",
            "warn",
            "TAILSCALE_API_KEY is set: only for the first ACL apply; keep it out of .env and revoke it "
            "afterwards",
        )

    check_discord_commands(report, cfg, env, deps, guarded)

    hook = env.get("TF_VAR_discord_webhook_url")
    if cfg.alerts.discord and hook:
        guarded("discord webhook", lambda: discord.check_webhook(hook, deps.transport))
    elif cfg.alerts.discord:
        report.add(
            "discord webhook", "fail", "alerts.discord is true but TF_VAR_discord_webhook_url isn't set"
        )
    elif hook:
        report.add("discord webhook", "warn", "TF_VAR_discord_webhook_url is set but alerts.discord is false")

    # P10 review: the rulesets that keep the GitHub Apps away from main need a plan that allows them.
    admin = env.get("GITHUB_TOKEN")
    if cfg.github.protect_main and admin:
        try:
            plan = github.rulesets_available(admin, repository, deps.transport)
        except http.HttpError as e:
            report.add("github rulesets", "fail", f"network error: {e}")
        else:
            if plan is False:
                report.add(
                    "github rulesets", "fail",
                    f"{repository} can't have rulesets on this GitHub plan (GitHub Pro for a private "
                    "personal repo): upgrade, or set github.protect_main: false (SECURITY.md: the panel App "
                    "could then push to main)",
                )  # fmt: skip
            elif plan:
                report.add(
                    "github rulesets", "ok", f"{repository} can have rulesets (main: pull requests + checks)"
                )
            else:
                report.add(
                    "github rulesets",
                    "warn",
                    "couldn't tell whether rulesets are available (Administration read?)",
                )
    elif not cfg.github.protect_main:
        report.add(
            "github rulesets", "warn",
            "github.protect_main is false: main isn't protected, so the panel's GitHub App (its key in the "
            "Worker) could push to main and deploy (SECURITY.md)",
        )  # fmt: skip

    # Security review M2: the deploy secrets in main-only environments (same plan gate as rulesets).
    if not cfg.github.environments:
        report.add(
            "github environments", "warn",
            "github.environments is false: the deploy secrets are repository-level, so any workflow run "
            "(e.g. one pushed to an update/* branch) can read them (SECURITY.md 'Actions secrets')",
        )  # fmt: skip
    elif admin and not cfg.github.protect_main:
        try:
            plan = github.rulesets_available(admin, repository, deps.transport)
        except http.HttpError as e:
            report.add("github environments", "fail", f"network error: {e}")
        else:
            if plan is False:
                report.add(
                    "github environments", "fail",
                    f"{repository} can't have deployment-branch environments on this plan: upgrade, or set "
                    "github.environments: false",
                )  # fmt: skip

    # P10 review / P11: the GitHub Apps (ids + base64 PEM keys from `mcctl init`'s manifest flow).
    slugs: dict[str, Any] = {}
    try:
        slugs = dict((state.load(repo.root).get("github") or {}).get("outputs") or {})
    except state.StateError:
        pass
    kinds = ([ghapp.PANEL] if cfg.panel.mode == "cloudflare" else []) + [ghapp.UPDATER]
    for kind in kinds:
        label = f"{kind.key} github app"
        if not (env.get(kind.id_var) and env.get(kind.key_var)):
            off = (
                "panel whitelist changes stay pending on the server"
                if kind is ghapp.PANEL
                else "no weekly update PRs"
            )
            report.add(
                label,
                "warn",
                f"{kind.id_var} / {kind.key_var} aren't set: {off} (`mcctl init --from github`)",
            )
        elif slugs.get(f"{kind.key}_app"):  # as the App (JWT): private Apps aren't visible to the admin PAT
            try:
                pem = ghapp.decode_key(env[kind.key_var])
                guarded(
                    label,
                    lambda kind=kind, pem=pem: ghapp.verify_app(
                        env[kind.id_var], pem, kind, deps.transport, deps.app_sign
                    ),
                )
            except ghapp.KeyRejected as e:
                report.add(label, "fail", f"{e} ({e.fix})")
            except ValueError:
                report.add(label, "fail", f"{kind.key_var} isn't a base64 PEM (delete both lines; re-run)")
            except (ProviderError, OSError, subprocess.SubprocessError) as e:
                report.add(label, "warn", f"couldn't ask GitHub as the App ({e}); permissions not re-checked")
        else:
            report.add(
                label, "ok", f"App {env.get(kind.id_var)} configured (permissions checked by `mcctl init`)"
            )

    # P9: the panel's GitHub login needs the OAuth app's secret (never in server.yml).
    github_idp = cfg.panel.mode == "cloudflare" and cfg.panel.access.idp == "github"
    if github_idp and not env.get("TF_VAR_panel_github_client_secret"):
        report.add(
            "panel github login",
            "fail",
            "panel.access.idp is github but TF_VAR_panel_github_client_secret (OAuth app secret) isn't set",
        )
    elif github_idp:
        report.add("panel github login", "ok", "TF_VAR_panel_github_client_secret is set")
    return checks


def check_ci_oidc(report: Report, repo: configrepo.ConfigRepo, token: str, deps: Deps) -> None:
    """The tailnet's CI identity must trust exactly the OIDC `sub` GitHub issues for the config repo's main
    branch: server.yml github.oidc_subject must name GitHub's format, and the subject the last `mcctl setup`
    apply recorded (Terraform output ci_oidc_subject) must equal it."""
    cfg = repo.server
    repository = f"{cfg.github.owner}/{cfg.github.config_repo}"
    name = "ci oidc subject"
    try:
        info = github.oidc_subject_info(token, repository, deps.transport)
    except http.HttpError as e:
        report.add(name, "fail", f"network error: {e}")
        return
    if info.fmt is None:
        report.add(name, "fail", info.problem)
        return
    expected = info.subject(environment=secrets.DEPLOY_ENVIRONMENT if cfg.github.environments else None)
    if info.fmt != cfg.github.oidc_subject:
        report.add(
            name,
            "fail",
            f"GitHub issues {repository}'s CI tokens the {info.fmt} sub {expected!r} ({info.reason}), but "
            f"server.yml github.oidc_subject is {cfg.github.oidc_subject}: the deploy's tailnet join fails "
            f'(HTTP 403, "Cannot validate subject"). Set github.oidc_subject: {info.fmt} (or re-run '
            "`mcctl setup`), then apply",
        )
        return
    try:
        applied = ((state.load(repo.root).get("apply") or {}).get("outputs") or {}).get("ci_oidc_subject")
    except state.StateError:
        applied = None
    if applied and applied != expected:
        report.add(
            name,
            "fail",
            f"the tailnet's CI identity was applied for {applied!r}, GitHub issues {expected!r}: re-run "
            "`mcctl setup` (or `mcctl plan` + apply)",
        )
        return
    report.add(name, "ok", f"{expected} ({info.fmt}: {info.reason})")


def check_zero_trust(report: Report, cfg: schema.ServerConfig, token: str, account: str, deps: Deps) -> None:
    """The panel's Access login needs the account's Zero Trust organization; its global session should be at
    most cloudflare.global_session_bounds()[1] (SECURITY.md "Re-auth"). Read-only: it only reports."""
    try:
        org = cloudflare.zero_trust_org(token, account, deps.transport)
    except (http.HttpError, cloudflare.CloudflareError) as e:
        report.add("zero trust", "warn", f"couldn't read the organization: {e}")
        return
    if org is None:
        report.add(
            "zero trust", "fail",
            f"the account hasn't signed up for Zero Trust: {cloudflare.ZERO_TRUST_URL} (team, Free plan)",
        )  # fmt: skip
        return
    minutes = cloudflare.duration_minutes(org.get("session_duration"))
    reauth = cfg.panel.access.reauth_minutes
    _, limit = cloudflare.global_session_bounds(reauth)
    current = org.get("session_duration") or "unset"
    if minutes is not None and minutes <= limit:
        report.add("zero trust", "ok", f"{org.get('auth_domain')}, global session {current} (<= {limit}m)")
    else:
        report.add(
            "zero trust", "warn",
            f"{org.get('auth_domain')}: global session {current} > {limit}m, so Access may open the "
            f"confirmation host without a fresh one-time PIN; {cloudflare.global_session_fix(reauth)}"
            " (or `mcctl setup`, step zero-trust)",
        )  # fmt: skip


def cloudflare_permissions(root: Path, deps: Deps | None = None) -> Report:
    """`mcctl doctor --cloudflare-permissions`: the permission groups the account token form offers, as the
    API names them, for the rows mcctl needs (and the look-alikes next to them), then the admin token's own
    policies against that list with a replacement for each look-alike. Read-only; names only."""
    deps = deps or Deps()
    report = Report()
    root = root.resolve()
    try:
        repo = configrepo.load(root)
        env = envfile.merged(root)
    except (configrepo.ConfigError, envfile.EnvFileError) as e:
        report.add("config repo", "fail", str(e))
        return report
    cfg = repo.server
    token = env.get("CLOUDFLARE_API_TOKEN")
    if not token:
        report.add("cloudflare token", "fail", "CLOUDFLARE_API_TOKEN isn't set (.env)")
        return report
    try:
        check = cloudflare.check(token, cfg.dns.zone, deps.transport, now=deps.now())
        account, owner = str(check.facts.get("account_id") or ""), str(check.facts.get("owner", "user"))
        if not account:
            _add_check(report, "cloudflare token", check)
            return report
        groups = cloudflare.permission_groups(token, owner, account, deps.transport)
    except (http.HttpError, cloudflare.CloudflareError) as e:
        report.add("permission groups", "fail", str(e))
        return report
    access = cfg.panel.mode == "cloudflare"
    offered = {cloudflare.norm_group(str(g.get("name", ""))): g for g in groups}
    rows = cloudflare.ACCOUNT_TOKEN_PERMISSIONS + (cloudflare.ACCOUNT_TOKEN_ACCESS if access else ())
    for kind, name, level, _ in rows:
        api = f"{name} Write" if level == "Edit" else f"{name} {level}"
        found = offered.get(cloudflare.norm_group(api))
        label = f"{kind} > {name} > {level}"
        if found is None:
            report.add("needs", "warn", f"{label}: the form doesn't offer {api!r} on this account")
        else:
            scopes = ",".join(str(x).rsplit(".", 1)[-1] for x in found.get("scopes") or [])
            shown = f"{label}  = {found.get('name')!r}" + (f" [{scopes}]" if scopes else "")
            report.add("needs", "ok", shown)
    alike = sorted(
        str(g.get("name")) for g in groups if cloudflare.lookalike(str(g.get("name", ""))) is not None
    )
    if alike:
        report.add("look-alikes", "skip", "offered too, don't pick: " + ", ".join(alike))
    token_id = check.facts.get("token_id")
    if owner != "account" or not token_id:
        report.add("token policies", "skip", "not an account-owned token: its own policies aren't readable")
        return report
    try:
        policies = cloudflare.token_policies(token, account, str(token_id), deps.transport)
    except (http.HttpError, cloudflare.CloudflareError) as e:
        report.add("token policies", "warn", f"couldn't read them: {e}")
        return report
    missing, extra, scope = cloudflare.audit_policies(
        policies, str(check.facts.get("zone_id") or ""), account, access=access
    )
    for name in missing:
        report.add("token policies", "fail", f"lacks {name!r}")
    for name in extra:
        row = cloudflare.lookalike(name)
        report.add(
            "token policies", "warn",
            f"has {name!r}: " + (f"a look-alike, replace it with {row}" if row else "not needed, remove it"),
        )  # fmt: skip
    for what in scope:
        report.add("token policies", "warn", f"reaches {what}")
    if not (missing or extra or scope):
        report.add("token policies", "ok", "exactly the rows above, on the one zone and its account")
    return report


def check_product_repo(
    report: Report, repo: configrepo.ConfigRepo, env: Mapping[str, str], deps: Deps
) -> None:
    """`mcctl product-setup --check`, when your GitHub login administers the product repo."""
    token = productsetup.token(env, deps.run, None)
    if not token:
        report.add("product repo", "skip", "no GitHub token (gh auth login) to read its settings")
        return
    try:
        p = productsetup.plan(token, repo.pin.repository, deps.transport)
    except productsetup.SetupError as e:
        report.add("product repo", "skip", str(e))
        return
    except http.HttpError as e:
        report.add("product repo", "warn", f"network error: {e}")
        return
    if p.changes:
        names = ", ".join(c.name for c in p.changes)
        report.add("product repo", "warn", f"{repo.pin.repository}: {names} differ: `mcctl product-setup`")
    else:
        report.add("product repo", "ok", f"{repo.pin.repository}: settings in sync ({len(p.in_sync)})")


# --- expiry, DNS, tailnet -----------------------------------------------------------------------------


def check_expiry(
    report: Report, repo: configrepo.ConfigRepo, checks: list[CredentialCheck], today: datetime.date
) -> None:
    path, changed = expiry.write(repo.root, repo.expiry, checks, today)
    data = configrepo.check_expiry(yamlio.load(path.read_text()))[0]
    report.add(
        "expiry.yml", "ok", f"{'updated: commit it' if changed else 'unchanged'} ({len(data)} credentials)"
    )
    for c in data:
        exp = c["expires_on"]
        if exp is None:
            continue
        when = exp if isinstance(exp, datetime.date) else datetime.date.fromisoformat(str(exp))
        days = (when - today).days
        if days < 0:
            report.add(f"expiry {c['name']}", "fail", f"expired on {when}: create a new one")
        elif days <= WARN_DAYS:
            report.add(f"expiry {c['name']}", "warn", f"expires in {days} days ({when}): renew it")


def check_domain(
    report: Report, zone: str, deps: Deps, today: datetime.date, registrar: tuple[str, str] | None = None
) -> None:
    """Cloudflare Registrar's own record (auto-renew, expiry) when the admin token may read it; else the
    registry's RDAP expiry date. `registrar` = (token, account id)."""
    if registrar is not None:
        try:
            reg = cloudflare.registration(*registrar, zone, deps.transport)
        except (cloudflare.CloudflareError, http.HttpError):
            reg = None  # no registrar permission (the usual case): RDAP below
        if reg is not None:
            until = str(reg.get("expires_at") or "?")[:10]
            if reg.get("auto_renew") is True:
                report.add(
                    "domain expiry", "ok", f"{zone}: Cloudflare Registrar, auto-renew on (until {until})"
                )
                return
            report.add(
                "domain expiry",
                "warn",
                f"{zone}: Cloudflare Registrar, auto-renew OFF (to {until}): switch it on in the dashboard",
            )
            return
    try:
        when = rdap.expiration(zone, deps.transport)
    except (rdap.RdapError, http.HttpError) as e:
        report.add("domain expiry", "warn", f"RDAP lookup failed: {e}")
        return
    if when is None:
        report.add("domain expiry", "warn", f"{zone}: the registry publishes no expiration date")
        return
    days = (when - today).days
    level: Level = "fail" if days < 0 else "warn" if days <= WARN_DAYS else "ok"
    report.add("domain expiry", level, f"{zone} registered until {when} ({days} days)")


def check_dns(report: Report, cfg: schema.ServerConfig, deps: Deps) -> None:
    fqdn = f"{cfg.dns.hostname}.{cfg.dns.zone}"
    if cfg.host.ipv4 is None:
        report.add("dns", "skip", f"{fqdn}: host.ipv4 isn't set yet")
        return
    for label, family, want in (
        ("A", socket.AF_INET, cfg.host.ipv4),
        ("AAAA", socket.AF_INET6, cfg.host.ipv6),
    ):
        got = deps.resolve(fqdn, family)
        if want is None:
            level: Level = "warn" if got else "ok"
            detail = f"{fqdn} {label} {sorted(got)} but host.ipv6 is null" if got else f"{fqdn} {label}: none"
        elif got == {want}:
            level, detail = "ok", f"{fqdn} {label} {want}"
        else:
            level, detail = (
                "fail",
                f"{fqdn} {label} {sorted(got) or 'missing'}, server.yml says {want}: `mcctl apply`",
            )
        report.add(f"dns {label}", level, detail)


def check_tailnet(report: Report, repo: configrepo.ConfigRepo, deps: Deps) -> None:
    name = repo.server.host.tailscale_name
    try:
        bootstrapped = state.load(repo.root).is_done("bootstrap")
    except state.StateError:
        bootstrapped = False
    done = bootstrapped or repo.server.deploy.enabled
    if repo.server.host.ipv4 is None:
        report.add("tailnet", "skip", f"{name}: no host yet")
        return
    proc = deps.run(["tailscale", "ping", "--c", "1", "--timeout", "5s", name])
    if proc.returncode == 0:
        report.add("tailnet", "ok", f"{name} answers")
    else:
        hint = "" if done else " (not bootstrapped yet?)"
        report.add("tailnet", "fail" if done else "warn", f"{name} doesn't answer `tailscale ping`{hint}")


def check_watchdog(report: Report, repo: configrepo.ConfigRepo, deps: Deps) -> None:
    """The watchdog Worker's /status (no credentials): the cron runs and nothing is late or failing."""
    try:
        applied = state.load(repo.root).is_done("apply")
    except state.StateError:
        applied = False
    if not applied:
        report.add("watchdog", "skip", "not deployed yet (it comes with the first `mcctl apply`)")
        return
    url = render.watchdog_url(repo.server)
    data: dict[str, Any] | None = None
    try:
        data = watchdog.status(url, deps.transport)
        level, detail = watchdog.assess(data)
    except http.HttpError as e:
        level, detail = "fail", f"unreachable ({e})"
    report.add("watchdog", level, f"{url}: {detail}")
    if repo.server.client.enabled and data is not None:
        level, detail = watchdog.assess_client(data)
        fix = "" if level == "ok" else f" (`mcctl apply -C {repo.root}`)"
        report.add("client page", level, detail + fix)


# --- v1.3: versions and the Panel -> Git self-test, from the watchdog's D1 ----------------------------------

SELFTEST_STALE_H = 36  # the agent runs it daily


def assess_versions(product: object, worker: object, pin: configrepo.Pin) -> tuple[Level, str]:
    """(level, detail) of the deployed product (the agent's inventory), the Worker's stamp (`mcctl render` ->
    `mcctl apply`) and the config repo's pin as it is now."""
    prod = product if isinstance(product, dict) else None
    stamp = worker if isinstance(worker, dict) else None
    pinned = f"{pin.version or '?'} ({pin.sha[:12]})"
    server = remote.product_text(prod) if prod and prod.get("version") else "unknown"
    applied = f"v{stamp.get('version')}" if stamp and stamp.get("version") else "unknown"
    problems = []
    if prod is None:
        problems.append("the server reports no version (deployed before v1.3? deploy)")
    elif (drift := remote.product_drift(prod, pin)) is not None:
        problems.append(f"server: {drift}")
    if stamp is None:
        problems.append(
            "the Worker has no version stamp: `mcctl render` + `mcctl apply` (applied before v1.3?)"
        )
    elif stamp.get("pin_sha") and stamp["pin_sha"] != pin.sha:
        problems.append(
            f"the Worker was applied for {str(stamp['pin_sha'])[:12]}, the pin is {pin.sha[:12]}: "
            "`mcctl render` + `mcctl apply`"
        )
    detail = f"server {server}, Worker {applied}, pin {pinned}"
    return ("warn", f"{detail}; " + "; ".join(problems)) if problems else ("ok", detail)


def assess_selftest(result: object, now: datetime.datetime) -> tuple[Level, str]:
    """(level, detail) of the agent's last Panel -> Git self-test (inventory `selftest`)."""
    if not isinstance(result, dict):
        return "warn", (
            "no result yet: the agent predates v1.3, or its first run is due 5 minutes after it starts"
        )
    if result.get("configured") is False:
        return "skip", "the panel has no config repo (CONFIG_REPO)"
    ts = result.get("ts")
    age_h = (now.timestamp() * 1000 - ts) / 3600000 if isinstance(ts, (int, float)) else None
    when = f"{age_h:.0f} h ago" if age_h is not None else "at an unknown time"
    if not result.get("ok"):
        streak = int(result.get("failures") or 1)
        return "fail", (
            f"failed {when} at {result.get('error') or 'an unknown step'} ({streak} in a row): panel changes "
            "can't reach the config repo. Fix it (the panel's GitHub App, a stale Worker), then run the "
            "self-test from the panel's Health tab"
        )
    if age_h is None or age_h > SELFTEST_STALE_H:
        return "warn", f"last passed {when} (it runs daily: is mc-agent running?)"
    return (
        "ok",
        f"passed {when} (the Worker's token, its permissions, the config repo's main and pull requests)",
    )


def check_panel_link(
    report: Report,
    repo: configrepo.ConfigRepo,
    token: str | None,
    account: str | None,
    deps: Deps,
) -> None:
    """v1.3: what the panel knows, read from the watchdog's D1 with the admin token (no panel login needed):
    the versions (server, Worker, pin) and the agent's last Panel -> Git self-test. Read-only."""
    if repo.server.panel.mode != "cloudflare":
        return
    try:
        applied = state.load(repo.root).is_done("apply")
    except state.StateError:
        applied = False
    if not applied:
        return  # the watchdog row says why
    if not token or not account:
        report.add("versions", "skip", "needs the Cloudflare token (see the credentials above)")
        return
    try:
        db = metrics.database(token, account, f"{repo.server.name}-watchdog", deps.transport)
        rows = db.query("SELECT key, value FROM watchdog WHERE key IN ('inventory', 'worker')")
        kv = {str(r.get("key")): json.loads(str(r.get("value") or "null")) for r in rows}
    except (http.HttpError, cloudflare.CloudflareError, ValueError) as e:
        report.add("versions", "warn", f"couldn't read the watchdog's D1: {e}")
        return
    inv = kv.get("inventory") if isinstance(kv.get("inventory"), dict) else {}
    report.add("versions", *assess_versions(inv.get("product"), kv.get("worker"), repo.pin))
    report.add("panel → git", *assess_selftest(inv.get("selftest"), deps.now()))


PLAN_VARS = ("CLOUDFLARE_API_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_ENDPOINT_URL_S3")


def check_plan(report: Report, repo: configrepo.ConfigRepo, env: Mapping[str, str], deps: Deps) -> None:
    """`terraform plan` is clean: after an apply, a plan that still changes something is either a pending
    server.yml change or drift (an attribute the provider reads back differently: a perpetual update).
    Read-only: `-lock=false`, the plan file deleted; runs only with the state credentials in .env."""
    name = "terraform plan"
    try:
        applied = state.load(repo.root).is_done("apply")
    except state.StateError:
        applied = False
    if not applied:
        report.add(name, "skip", "nothing applied yet")
        return
    if deps.plan is None:
        report.add(name, "skip", "just applied (`mcctl doctor` checks it)")
        return
    if missing := [v for v in PLAN_VARS if not env.get(v)]:
        report.add(name, "skip", f"needs {', '.join(missing)} (.env) to read the state")
        return
    if deps.which("terraform") is None:
        report.add(name, "skip", "terraform isn't installed")
        return
    root = pinned_root(repo.pin, os.environ)
    if root is None:
        report.add(name, "skip", "the pinned product isn't fetched yet (`mcctl plan` fetches it)")
        return
    try:
        plan = deps.plan(repo, root)
    except (deploy.DeployError, tf.TfError) as e:
        first = str(e).strip().splitlines()[:2]
        report.add(name, "warn", f"couldn't plan: {' '.join(first)} (`mcctl plan` shows it all)")
        return
    if plan.empty:
        report.add(name, "ok", "No changes: the live resources match the configuration")
        return
    if plan.only_relocated:
        report.add(name, "ok", f"{', '.join(plan.relocated)}: {tf.RELOCATED_NOTE}")
        return
    changed = [*plan.delete, *plan.replace, *plan.update, *plan.create]
    report.add(
        name,
        "warn",
        f"{len(changed)} pending: {', '.join(changed)}. A server.yml change not applied yet (`mcctl apply`), "
        "or drift if it shows again right after an apply (`mcctl plan` for the detail)",
    )


def check_host_ipv6(report: Report, repo: configrepo.ConfigRepo, deps: Deps) -> None:
    """host.ipv6 unset while the host has a public IPv6: no AAAA record, so IPv6-only players can't
    connect."""
    cfg = repo.server
    if cfg.host.ipv6 is not None:
        return  # check_dns compares the record with it
    try:
        bootstrapped = state.load(repo.root).is_done("bootstrap")
    except state.StateError:
        bootstrapped = False
    if cfg.host.ipv4 is None or not bootstrapped:
        report.add("host ipv6", "skip", "no bootstrapped host yet")
        return
    proc = deps.run(remote.ssh_argv(cfg, hostnet.COMMAND))
    if proc.returncode != 0:
        report.add(
            "host ipv6", "skip", f"couldn't read the host's addresses over the tailnet ({hostnet.COMMAND})"
        )
        return
    found = hostnet.public_ipv6(proc.stdout)
    if not found:
        report.add("host ipv6", "ok", "the host has no public IPv6 (IPv4 only, host.ipv6 unset)")
        return
    report.add(
        "host ipv6",
        "warn",
        f"the host has {', '.join(found)} but server.yml host.ipv6 is empty: players on IPv6-only networks "
        f"can't connect; run `mcctl config set host.ipv6 {found[0]}` + `mcctl apply`",
    )


def check_pin(
    report: Report, repo: configrepo.ConfigRepo, env: Mapping[str, str], deps: Deps | None = None
) -> None:
    from mcctl import pinmove

    pin = repo.pin
    label = f"{pin.repository}@{pin.sha[:12]}" + (f" ({pin.version})" if pin.version else "")
    if env.get(product.ENV_OVERRIDE):
        report.add("product pin", "warn", f"{product.ENV_OVERRIDE} overrides the pinned {label}")
        return
    st = pinmove.status(pin, (deps or Deps()).run)
    if st.relation == "current":
        report.add("product pin", "ok", f"this mcctl is the pinned {label}")
    elif st.relation in ("stale", "unrelated"):  # P15: a pin behind this checkout (`--fix` offers to move it)
        report.add("product pin", "warn", pinmove.describe(st))
    else:
        cache = product.cache_root(env) / pin.sha
        where = "cached" if (cache / product.READY).is_file() else "fetched on first use"
        running = st.running
        report.add(
            "product pin",
            "ok" if st.relation == "newer" else "warn",
            f"this mcctl is {running[:12] if running else 'not a clean checkout'}; "
            f"plan/apply/deploy/bootstrap run the pinned {label} ({where} in {cache})",
        )


def pinned_root(pin: configrepo.Pin, env: Mapping[str, str]) -> Path | None:
    """The product checkout plan/apply/deploy/bootstrap would run for this pin (None: not fetched yet)."""
    if override := env.get(product.ENV_OVERRIDE):
        return Path(override)
    if product.checkout_sha(schema.PRODUCT_ROOT) == pin.sha:
        return schema.PRODUCT_ROOT
    cache = product.cache_root(env) / pin.sha
    return cache / "src" if product.cache_ok(cache, pin.sha) else None


def check_collections(
    report: Report, repo: configrepo.ConfigRepo, env: Mapping[str, str], deps: Deps, fix: bool
) -> None:
    """The pinned galaxy collections (ansible/requirements.yml) that bootstrap.yml and site.yml need."""
    name = "ansible collections"
    root = pinned_root(repo.pin, env)
    if root is None:
        report.add(name, "skip", "installed with the pinned product on first use (`mcctl plan` fetches it)")
        return
    try:
        dest, want, wrong = galaxy.status(root, env)
        if wrong and fix:
            dest = deps.collections(root)
            dest, want, wrong = galaxy.status(root, env)
    except galaxy.GalaxyError as e:
        report.add(name, "fail", f"{e} (by hand: cd {root} && {galaxy.MANUAL})")
        return
    if wrong:
        report.add(
            name,
            "warn",
            f"{'; '.join(wrong)} in {dest}: deploy/bootstrap install them first (or `mcctl doctor --fix`)",
        )
    else:
        report.add(name, "ok", f"{galaxy.describe(want)} in {dest}")


def check_workflow_secrets(
    report: Report, repo: configrepo.ConfigRepo, env: Mapping[str, str], deps: Deps, fix: bool
) -> None:
    """v1.2: the config repo's workflow passes every secret the pinned product's template does (config repos
    pass them explicitly: v1.1's ARTIFACT_PUBLISH_TOKEN was missing on mc-config). `--fix` adds the missing
    lines (comments kept) and commits the workflow file; pushing it is the admin's."""
    from mcctl import wfsecrets

    name = "workflow secrets"
    product_root = pinned_root(repo.pin, env)
    if product_root is None:
        report.add(
            name, "skip", "compared with the pinned product's template once it is fetched (`mcctl plan`)"
        )
        return
    wf = repo.root / configrepo.WORKFLOW
    try:
        text = wf.read_text()
        absent = wfsecrets.missing(text, wfsecrets.template_text(product_root))
    except (OSError, wfsecrets.WorkflowError) as e:
        report.add(name, "warn", str(e))
        return
    if not absent:
        report.add(
            name, "ok", f"{configrepo.WORKFLOW} passes every secret the pinned product's template does"
        )
        return
    names = ", ".join(absent)
    publish_missing = repo.server.client.enabled and "ARTIFACT_PUBLISH_TOKEN" in absent
    level: Level = "fail" if publish_missing else "warn"
    why = " (client.enabled: the deploy fails without ARTIFACT_PUBLISH_TOKEN)" if publish_missing else ""
    if fix:
        try:
            wf.write_text(wfsecrets.add(text, absent))
        except wfsecrets.WorkflowError as e:
            report.add(name, level, f"{configrepo.WORKFLOW} doesn't pass {names}{why}; can't add them: {e}")
            return
        commit = deps.run(
            ["git", "-C", str(repo.root), "commit", "-q", "-m", f"mcctl doctor: pass {names} to the product",
             "--", configrepo.WORKFLOW]
        )  # fmt: skip
        if commit.returncode != 0:
            wf.write_text(text)
            report.add(
                name, level, f"added {names}, but `git commit` failed: {(commit.stderr or '').strip()}"
            )
            return
        report.add(
            name, "ok", f"added {names} to {configrepo.WORKFLOW} and committed it: push to main to use it"
        )
        return
    report.add(
        name,
        level,
        f"{configrepo.WORKFLOW} doesn't pass {names} to the product{why}: `mcctl doctor --fix` adds "
        + ("it" if len(absent) == 1 else "them")
        + " (one commit), or add "
        + "; ".join(f"`{wfsecrets.line(n)}`" for n in absent)
        + " under `secrets:`",
    )


def check_workflow_inputs(
    report: Report, repo: configrepo.ConfigRepo, env: Mapping[str, str], deps: Deps, fix: bool
) -> None:
    """v1.5: the config repo's manual-run form (workflow_dispatch inputs) has every input and `task` option
    the pinned product's template has (v1.5: task `product-update`, input `product_version`). A pin moved by
    the updater's pull request changes only the `uses:` line, so the form stays as it was until this adds
    them; `--fix` adds them (comments kept) and commits the workflow file; pushing it is the admin's."""
    from mcctl import wfsecrets

    name = "workflow inputs"
    product_root = pinned_root(repo.pin, env)
    if product_root is None:
        report.add(
            name, "skip", "compared with the pinned product's template once it is fetched (`mcctl plan`)"
        )
        return
    wf = repo.root / configrepo.WORKFLOW
    try:
        text = wf.read_text()
        template = wfsecrets.template_text(product_root)
        lacking = wfsecrets.missing_dispatch(text, template)
    except (OSError, wfsecrets.WorkflowError) as e:
        report.add(name, "warn", str(e))
        return
    if not lacking:
        report.add(name, "ok", f"{configrepo.WORKFLOW}'s manual run matches the pinned product's template")
        return
    what = ", ".join(lacking)
    if fix:
        try:
            wf.write_text(wfsecrets.add_dispatch(text, template))
        except wfsecrets.WorkflowError as e:
            report.add(name, "warn", f"{configrepo.WORKFLOW}'s manual run lacks {what}; can't add: {e}")
            return
        commit = deps.run(
            ["git", "-C", str(repo.root), "commit", "-q", "-m", f"mcctl doctor: the manual run gets {what}",
             "--", configrepo.WORKFLOW]
        )  # fmt: skip
        if commit.returncode != 0:
            wf.write_text(text)
            report.add(
                name, "warn", f"added {what}, but `git commit` failed: {(commit.stderr or '').strip()}"
            )
            return
        report.add(
            name, "ok", f"added {what} to {configrepo.WORKFLOW} and committed it: push to main to use it"
        )
        return
    report.add(
        name,
        "warn",
        f"{configrepo.WORKFLOW}'s manual run (Actions > Run workflow) lacks {what}, which the pinned "
        "product's template has: `mcctl doctor --fix` adds them (one commit), or copy them from the "
        "product's template/.github/workflows/server.yml",
    )


# --- the whole doctor ---------------------------------------------------------------------------------


def run(root: Path, *, fix: bool = False, deps: Deps | None = None, i_know: bool = False) -> Report:
    deps = deps or Deps()
    report = Report()
    root = root.resolve()
    check_tools(report, deps)
    check_envfile(report, root, fix)
    check_dashlane(report, root, deps.dashlane)
    try:
        if ignored := envfile.shadowed(root):
            report.add(
                "state credentials",
                "warn",
                f"ignoring {', '.join(ignored)} from your shell: the Terraform state credentials come from "
                f"{root / envfile.ENV_FILE} only (unset them to silence this)",
            )
    except envfile.EnvFileError:
        pass  # reported by check_envfile
    try:
        repo = configrepo.load(root, i_know=i_know)
    except configrepo.ConfigError as e:
        report.add("config repo", "fail", f"{len(e.errors)} error(s): " + "; ".join(e.errors))
        return report
    problems = schema.placeholders(repo.server)
    report.add("config repo", "fail" if problems else "ok", "; ".join(problems) or "valid")
    if problems:
        report.add("credentials", "skip", "fix the placeholders first (they name the zone and repo to check)")
        return report
    check_pin(report, repo, os.environ, deps)
    check_collections(report, repo, os.environ, deps, fix)
    check_workflow_secrets(report, repo, os.environ, deps, fix)
    check_workflow_inputs(report, repo, os.environ, deps, fix)
    try:
        env = envfile.merged(
            root, resolve=deps.dashlane.resolve_lenient
        )  # unresolved references: reported above
    except envfile.EnvFileError:
        env = dict(os.environ)  # the .env result above already failed; don't read a world-readable file
    checks = check_credentials(report, repo, env, deps)
    check_product_repo(report, repo, env, deps)
    today = deps.now().date()
    check_expiry(report, repo, checks, today)
    cf_token = env.get("CLOUDFLARE_API_TOKEN")
    cf_account = next((c.facts.get("account_id") for c in checks if c.facts.get("account_id")), None)
    registrar = (cf_token, str(cf_account)) if cf_token and cf_account else None
    check_domain(report, repo.server.dns.zone, deps, today, registrar)
    check_dns(report, repo.server, deps)
    check_tailnet(report, repo, deps)
    check_host_ipv6(report, repo, deps)
    check_watchdog(report, repo, deps)
    check_panel_link(report, repo, cf_token, str(cf_account) if cf_account else None, deps)
    check_plan(report, repo, env, deps)
    try:
        st = state.load(root)
        levels = {lvl: sum(r.level == lvl for r in report.results) for lvl in ("ok", "warn", "fail")}
        st.record(STEP, "failed" if report.failed else "done", outputs=levels, now=deps.now())
    except state.StateError as e:
        report.add("state", "warn", str(e))
    return report
