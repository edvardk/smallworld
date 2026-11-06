"""`mcctl init` / `mcctl setup`: the onboarding wizard (design §4 "mcctl init: steps", §8 P5; docs/setup.md).

Seventeen steps (`STEPS`, shown as 1-17), each idempotent and recorded in `<config repo>/.mcctl/state.json`
(status, input hash, non-secret outputs). A re-run re-validates the credentials (cheap, no prompts when `.env` already has them),
skips the steps that are done with unchanged inputs and resumes at the first one that isn't; `--from STEP`
forces a step (and everything after it) to run again. A failure prints the cause, the fix and the command to
re-run. Nothing is rolled back: every step is safe to repeat.

Secrets are only ever read with getpass (or, non-interactively, from the environment), validated live, and
written to the config repo's `.env` (mode 0600 from creation). They never appear in output, argv, logs,
state.json or git. The one-off Tailscale API key isn't stored at all.

Every outside effect goes through `Deps` (HTTP transport, subprocess runner, Terraform, bootstrap, status
ping, DNS, clock); `--mock` swaps in `mcctl.mock`'s fakes (fake provider APIs, fake gh/ssh/tailscale/
terraform; real local git), which is how the tests and the e2e-mock workflow drive the same code.
"""

from __future__ import annotations

import datetime
import importlib.util
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcctl import (
    __version__,
    configrepo,
    dashlane,
    deploy,
    doctor,
    envfile,
    galaxy,
    ghapp,
    hostnet,
    http,
    mcping,
    product,
    productsetup,
    python_problem,
    remote,
    render,
    schema,
    secrets,
    state,
    tf,
    yamledit,
)
from mcctl import bootstrap as bootstrap_mod
from mcctl import ui as ui_mod
from mcctl.providers import (
    CredentialCheck,
    ProviderError,
    cloudflare,
    discord,
    github,
    mojang,
    tailscale,
    watchdog,
)
from mcctl.ui import UI, Abort

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
STATUS_JSON = ["tailscale", "status", "--json"]
OAUTH_SCOPES = "policy_file, devices:posture_attributes, devices:core:read, auth_keys, federated_keys"
SUPPORTED_OSES = ("Debian 12", "Debian 13", "Ubuntu 24.04")
MC_BIN_DIR = "/opt/minecraft/bin"  # = ansible group_vars mc_bin_dir (tests/test_consistency.py)
MIN_MEM_KB = 3_500_000  # "RAM >= 4 GB": a 4 GB VPS reports ~3.8 GiB MemTotal
# Answers-file keys that approve something only a person may approve: a Terraform apply (also bootstrap's),
# a host-key fingerprint, a revoked token, a stored restic password. Allowed with --mock only (cli.cmd_init).
HUMAN_ONLY_ANSWERS = frozenset(
    {
        "confirm_apply",
        "host_key_matches",
        "api_key_revoked",
        "restic_password_stored",
        "delete_state_token",
        "move_product_pin",  # P15: moving the config repo's product pin (pinmove.ANSWER_KEY)
    }
)


TEMPLATE_NOTE = """# `mcctl init` writes this file from your answers. The values below are placeholders (example.com / example
# names): `mcctl validate` warns about them and `mcctl render` / any deploy refuses to run until they're
# replaced.
"""
INIT_NOTE = """# Written by `mcctl init` from your answers; edit freely (`mcctl config set PATH VALUE` keeps comments).
"""


class StepError(RuntimeError):
    """A step failed: `cause` and `fix` are shown, then the re-run command."""

    def __init__(self, cause: str, fix: str = "") -> None:
        super().__init__(cause)
        self.cause, self.fix = cause, fix


class Paused(Exception):
    """Stop here and resume later: `str(e)` is the reason, `e.resume` what to do first."""

    def __init__(self, reason: str, resume: str = "") -> None:
        super().__init__(reason)
        self.resume = resume


def _capture(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=300)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def _capture_input(argv: Sequence[str], stdin: str) -> subprocess.CompletedProcess[str]:
    """A command that reads a secret on stdin (password managers): never on argv, output captured."""
    try:
        return subprocess.run(
            list(argv), input=stdin, capture_output=True, text=True, check=False, timeout=120
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def _ssh_public_key(home: Path | None = None) -> str | None:
    ssh = (home or Path.home()) / ".ssh"
    for name in ("id_ed25519.pub", "id_ecdsa.pub", "id_rsa.pub"):
        try:
            text = (ssh / name).read_text().strip()
        except OSError:
            continue
        if text:
            return text
    return None


def _ssh_identity(home: Path | None = None) -> Path | None:
    """The private key ssh offers first (the one whose public key setup shows), if there is one."""
    ssh = (home or Path.home()) / ".ssh"
    for name in ("id_ed25519", "id_ecdsa", "id_rsa"):
        if (ssh / name).is_file() and (ssh / f"{name}.pub").is_file():
            return ssh / name
    return None


def _stream(argv: Sequence[str]) -> int:
    try:
        return subprocess.run(list(argv), check=False).returncode
    except OSError:
        return 127


def _resolve(host: str, family: int) -> set[str]:
    try:
        return {str(i[4][0]) for i in socket.getaddrinfo(host, None, family, socket.SOCK_STREAM)}
    except socket.gaierror:
        return set()


def _real_terraform(repo: configrepo.ConfigRepo, root: Path, extra: Mapping[str, str]) -> tuple[Any, Path]:
    """Render, then `terraform init` with the rendered backend; `extra` (e.g. a one-off TAILSCALE_API_KEY)
    is added to Terraform's environment only."""
    out = repo.root / ".mcctl" / "rendered"
    try:
        render.render(repo, out)
    except render.RenderError as e:
        raise StepError("server.yml still has placeholders:\n  " + "\n  ".join(e.errors)) from None
    terraform = tf.Terraform(root / "terraform", tf.environment(repo.root, dict(os.environ) | dict(extra)))
    terraform.init(out / render.BACKEND_FILE)
    return terraform, out


@dataclass
class Deps:
    transport: http.Transport = http.urllib_transport
    run: Runner = _capture  # gh, git, tailscale, ssh, ssh-keyscan, ssh-keygen (output captured)
    run_input: Callable[[Sequence[str], str], subprocess.CompletedProcess[str]] = _capture_input  # op, bw
    ssh_public_key: Callable[[], str | None] = _ssh_public_key
    ssh_identity: Callable[[], Path | None] = _ssh_identity  # its private key (passphrase / agent check)
    ssh_auth_sock: Callable[[], str | None] = lambda: os.environ.get("SSH_AUTH_SOCK")  # ssh-agent, if any
    stream: Callable[[Sequence[str]], int] = _stream  # commands that show their own progress (gh run watch)
    which: Callable[[str], str | None] = shutil.which
    now: Callable[[], datetime.datetime] = lambda: datetime.datetime.now(datetime.UTC)
    sleep: Callable[[float], None] = time.sleep
    resolve: Callable[[str, int], set[str]] = _resolve
    terraform: Callable[[configrepo.ConfigRepo, Path, Mapping[str, str]], tuple[Any, Path]] = _real_terraform
    bootstrap: Callable[[configrepo.ConfigRepo, Path, Callable[[str], str]], int] = lambda repo, root, ask: (
        bootstrap_mod.run(repo, root, ask=ask)
    )
    ping: Callable[[str, int, str], dict[str, Any]] = lambda a, p, h: mcping.status(a, p, host=h)
    probe: Callable[[str, int], str] = bootstrap_mod.probe
    known_hosts: Path = field(default_factory=lambda: Path.home() / ".ssh" / "known_hosts")
    platform: str = sys.platform
    credentials_env: Mapping[str, str] | None = None  # where credentials may come from (None: os.environ)
    os_release: Callable[[], dict[str, str]] = lambda: _os_release()
    # P10 review: GitHub's App manifest flow in the browser (code back), and the App's install page
    app_create: Callable[[ghapp.Kind, str, str, bool, Callable[[str], None]], str] = ghapp.browser_create
    # (slug, say, ask, poll) -> installation id: the redirect, the API poll, or what the user pastes
    app_install: Callable[
        [str, Callable[[str], None], Callable[[], str], Callable[[], str | None] | None], str | None
    ] = ghapp.browser_install
    app_sign: Callable[[str, bytes], bytes] = ghapp.openssl_sign  # App JWTs (resume, installation checks)
    dashlane: dashlane.Resolver = field(default_factory=lambda: dashlane.DEFAULT)  # P14: `dcli` (read-only)
    # the product's pinned galaxy collections (ansible/requirements.yml), installed if missing -> their dir
    collections: Callable[[Path], Path] = lambda root: galaxy.ensure(root)


def _os_release(path: Path = Path("/etc/os-release")) -> dict[str, str]:
    try:
        text = path.read_text()
    except OSError:
        return {}
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


@dataclass
class Options:
    mock: bool = False
    product_sha: str | None = None
    from_step: str | None = None
    seed: Path | None = None  # start server.yml from this file
    # `--reconfigure STEP`: re-run these steps and ask again what they remember (e.g. discord's "slash
    # commands too?"); without it a resumed setup never re-asks a question it has an answer for
    reconfigure: tuple[str, ...] = ()


@dataclass
class Ctx:
    root: Path
    ui: UI
    deps: Deps
    opts: Options
    product_root: Path = schema.PRODUCT_ROOT
    created: list[str] = field(default_factory=list)
    _state: state.State | None = None
    dashlane_store: bool | None = None  # P14: this run's choice (preflight); later runs read the state

    @property
    def state(self) -> state.State:
        if self._state is None:
            self._state = state.load(self.root)
        return self._state

    def outputs(self, step: str) -> dict[str, Any]:
        rec = self.state.get(step) or {}
        return dict(rec.get("outputs") or {})

    def reconfigure(self, step: str) -> bool:
        """Ask `step`'s remembered questions again? (`--reconfigure STEP`, or `--from STEP` naming it.)"""
        if step in self.opts.reconfigure:
            return True
        try:
            start = _from_index(self.opts.from_step)
        except ValueError:
            return False
        return start is not None and STEPS[start].id == step

    def env(self) -> dict[str, str]:
        """Shell + .env (the shell wins), like every mcctl command."""
        try:
            return envfile.merged(self.root, self.deps.credentials_env, self.deps.dashlane.resolve)
        except dashlane.DashlaneError as e:
            raise StepError(
                str(e), "fix the item in Dashlane (then `dcli sync`), or put the value in .env"
            ) from None
        except envfile.EnvFileError as e:
            raise StepError(str(e), "chmod 600 .env (or `mcctl doctor --fix`)") from None

    def save_env(self, **values: str) -> None:
        from mcctl import integrations

        values = {k: integrations.to_reference(self, k, v) for k, v in values.items()}  # P14: Dashlane
        envfile.write(self.root, values)
        names = ", ".join(sorted(values))
        self.ui.ok(f"saved {names} to {self.root / envfile.ENV_FILE} (mode 0600, git-ignored)")

    def repo(self) -> configrepo.ConfigRepo:
        try:
            return configrepo.load(self.root)
        except configrepo.ConfigError as e:
            raise StepError("the config repo doesn't validate:\n  " + "\n  ".join(e.errors)) from None

    # --- server.yml -----------------------------------------------------------------------------------

    @property
    def server_path(self) -> Path:
        return self.root / configrepo.SERVER_YML

    def current(self, path: str) -> Any:  # noqa: ANN401
        value = yamledit.get_value(self.server_path.read_text(), path)
        return None if value is yamledit.MISSING else value

    def set(self, values: Mapping[str, Any]) -> None:
        """Write values into server.yml (comment-preserving), each checked by the schema first."""
        text = self.server_path.read_text()
        for path, value in values.items():
            if errs := schema.field_errors(path, value):
                raise StepError("; ".join(errs))
            text = yamledit.set_value(text, path, value)
        self.server_path.write_text(text)

    # --- git ------------------------------------------------------------------------------------------

    def git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        proc = self.deps.run(["git", "-C", str(self.root), *args])
        if check and proc.returncode != 0:
            raise StepError(f"`git {' '.join(args)}` failed: {(proc.stderr or proc.stdout).strip()}")
        return proc

    def commit(self, message: str, paths: Sequence[str], *, push: bool = True) -> str:
        self.git("add", "--", *paths)
        if self.git("diff", "--cached", "--quiet", check=False).returncode != 0:
            self.git("commit", "-q", "-m", message)
            self.ui.ok(f"committed: {message}")
        sha = self.git("rev-parse", "HEAD").stdout.strip()
        if push:
            proc = self.git("push", "-q", "-u", "origin", "HEAD:main", check=False)
            if proc.returncode != 0:
                raise StepError(
                    f"`git push` failed: {(proc.stderr or proc.stdout).strip()}",
                    "check `gh auth status` (and `gh auth setup-git` for HTTPS pushes) and that you can push to the config repo",
                )
            self.ui.ok("pushed to main")
        return sha

    def gh(self, *args: str) -> subprocess.CompletedProcess[str]:
        return self.deps.run(["gh", *args])


# --- helpers ------------------------------------------------------------------------------------------


def _json(proc: subprocess.CompletedProcess[str]) -> Any:  # noqa: ANN401
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout)
    except ValueError:
        return None


def _field_check(path: str, convert: Callable[[str], Any] = str) -> Callable[[str], str | None]:
    def check(v: str) -> str | None:
        try:
            value = convert(v)
        except ValueError as e:
            return str(e)
        errs = schema.field_errors(path, value)
        return "; ".join(e.split(": ", 1)[-1] for e in errs) or None

    return check


def _int_or_none(v: str) -> int | None:
    if v.strip().lower() in ("none", "null", "off", ""):
        return None
    try:
        return int(v)
    except ValueError:
        raise ValueError("a whole number, or `none`") from None


def _credential(ctx: Ctx, check: Any, label: str) -> None:  # noqa: ANN401 - a CredentialCheck
    detail = "; ".join(check.messages)
    if check.level == "fail":
        raise StepError(f"{label}: {detail}")
    (ctx.ui.warn if check.level == "warn" else ctx.ui.ok)(f"{label}: {detail}")


def _guarded(fn: Callable[[], Any]) -> Any:  # noqa: ANN401
    """Run a provider call (a spinner meanwhile); network and provider errors become a StepError with the fix."""
    try:
        with ui_mod.stdout().spinner("waiting for the provider's API"):
            return fn()
    except http.HttpError as e:
        raise StepError(f"network error: {e}", "check your connection and re-run") from None
    except ProviderError as e:
        raise StepError(str(e), e.fix or "fix the cause above, then re-run") from None


def _local_timezone() -> str:
    tz = os.environ.get("TZ", "").lstrip(":")
    if not tz:
        try:
            target = os.readlink("/etc/localtime")
            tz = target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""
        except OSError:
            tz = ""
    return tz if tz and not schema.field_errors("host.timezone", tz) else "UTC"


def tailnet_target(ctx: Ctx, cfg: schema.ServerConfig) -> str | None:
    return bootstrap_mod.tailnet_address(cfg.host.tailscale_name, ctx.deps.run)


# --- step 0: preflight --------------------------------------------------------------------------------


def os_family(platform_name: str, release: Mapping[str, str], kernel: str = platform.release()) -> str:
    if platform_name == "darwin":
        return "macos"
    if platform_name.startswith(("win", "cygwin", "msys")):
        return "windows"
    ids = f"{release.get('ID', '')} {release.get('ID_LIKE', '')}".lower()
    if "fedora" in ids or "rhel" in ids:
        return "fedora"
    return "debian"  # Debian, Ubuntu, WSL2 Ubuntu (and the best guess for other Linux)


def install_hint(tool: str, family: str) -> str:
    v = tf.TERRAFORM_VERSION
    tf_exact = f"exactly {v} (the product's pin): https://releases.hashicorp.com/terraform/{v}/"
    hints = {
        "macos": {
            "git": "xcode-select --install   (or: brew install git)",
            "gh": "brew install gh, then: gh auth login",
            "terraform": f"brew install hashicorp/tap/terraform, or {tf_exact}",
            "tailscale": "install the Tailscale app (https://tailscale.com/download/mac) and log in",
            "ssh": "OpenSSH ships with macOS",
            "venv": "brew install python",
        },
        "debian": {
            "git": "sudo apt install git",
            "gh": "GitHub's apt repository: https://github.com/cli/cli/blob/trunk/docs/install_linux.md, "
            "then: gh auth login",
            "terraform": f"HashiCorp's apt repository (https://developer.hashicorp.com/terraform/install), "
            f"version {tf_exact}",
            "tailscale": "curl -fsSL https://tailscale.com/install.sh | sh, then: sudo tailscale up",
            "ssh": "sudo apt install openssh-client",
            # unversioned: whatever python3 the release ships (Ubuntu 26.04: 3.14), never python3.X-venv
            "venv": "sudo apt install python3-venv",
        },
        "fedora": {
            "git": "sudo dnf install git",
            "gh": "sudo dnf install gh, then: gh auth login",
            "terraform": f"HashiCorp's dnf repository (https://developer.hashicorp.com/terraform/install), "
            f"version {tf_exact}",
            "tailscale": "https://tailscale.com/download/linux (dnf repository), then: sudo tailscale up",
            "ssh": "sudo dnf install openssh-clients",
            "venv": "sudo dnf install python3",
        },
    }
    return hints.get(family, hints["debian"]).get(tool, "")


def venv_problem(family: str, has_ensurepip: bool | None = None) -> str | None:
    """mcctl builds the pinned product's venv with `python -m venv`, which needs ensurepip (Debian/Ubuntu split it
    into the python3-venv package)."""
    if has_ensurepip is None:
        has_ensurepip = importlib.util.find_spec("ensurepip") is not None
    if has_ensurepip:
        return None
    return f"`python -m venv` won't work here (no ensurepip). Install: {install_hint('venv', family)}"


def step_preflight(ctx: Ctx) -> dict[str, Any]:
    deps, ui = ctx.deps, ctx.ui
    family = os_family(deps.platform, deps.os_release())
    if family == "windows":
        raise StepError(
            "mcctl runs on Windows only inside WSL2",
            "install WSL2 with Ubuntu (https://learn.microsoft.com/windows/wsl/install), then run mcctl there",
        )
    problems: list[str] = []
    if problem := python_problem():
        problems.append(problem)
    else:
        ui.ok(f"python {platform.python_version()}")
    if problem := venv_problem(family):
        problems.append(problem)
    for tool in ("git", "gh", "terraform", "tailscale", "ssh"):
        if deps.which(tool) is None:
            problems.append(f"{tool} not found. Install: {install_hint(tool, family)}")
            continue
        if tool == "gh" and deps.run(["gh", "auth", "status"]).returncode != 0:
            problems.append("gh isn't logged in: gh auth login")
        elif tool == "terraform":
            data = _json(deps.run(["terraform", "version", "-json"])) or {}
            level, detail = doctor.terraform_version_level(data.get("terraform_version"))
            if level == "fail":
                problems.append(f"terraform {detail}. Install: {install_hint('terraform', family)}")
                continue
            if level == "warn":
                ui.warn(f"terraform {detail}")
                continue
        elif tool == "tailscale":
            backend = (_json(deps.run(STATUS_JSON)) or {}).get("BackendState")
            if backend != "Running":
                problems.append(f"tailscale isn't connected (BackendState {backend!r}): tailscale up")
                continue
        ui.ok(tool)
    if not problems:  # needs python + venv; the bug this prevents: bootstrap.yml can't resolve ansible.posix
        try:
            deps.collections(ctx.product_root)
            ui.ok(f"ansible collections {galaxy.describe(galaxy.pins(ctx.product_root))}")
        except galaxy.GalaxyError as e:
            problems.append(f"{e}\n    by hand: cd {ctx.product_root} && {galaxy.MANUAL}")
    if problems:
        raise StepError("missing prerequisites:\n  - " + "\n  - ".join(problems), "install them, then re-run")
    from mcctl import integrations

    return {
        "os": family,
        **integrations.preflight(ctx),
    }  # P14: Dashlane as the credential store, if dcli is set up


# --- step 1: the config repo --------------------------------------------------------------------------


def _product_pin(ctx: Ctx) -> tuple[str, str]:
    sha = ctx.opts.product_sha or product.checkout_sha(ctx.product_root)
    if sha is None or not configrepo.SHA.fullmatch(sha):
        raise StepError(
            f"can't pin the product: {ctx.product_root} isn't a clean git checkout",
            "run mcctl init from a clean checkout of a pushed product commit (or pass --product-sha SHA)",
        )
    return sha, f"v{__version__}"


def step_repo(ctx: Ctx) -> dict[str, Any]:
    ui = ctx.ui
    login = ctx.gh("api", "user", "--jq", ".login").stdout.strip() or None
    existing = configrepo.git_origin(ctx.root) if (ctx.root / ".git").exists() else None
    default = "/".join(existing) if existing else (f"{login}/mc-config" if login else None)

    def valid(v: str) -> str | None:
        parts = v.split("/")
        if len(parts) != 2:
            return "owner/name"
        errs = schema.field_errors("github.owner", parts[0]) + schema.field_errors(
            "github.config_repo", parts[1]
        )
        return "; ".join(errs) or None

    repository = ui.ask(
        "repo", "GitHub repository for this server's config (owner/name)", default=default, check=valid
    )
    owner, name = repository.split("/")
    if existing and existing != (owner, name):
        raise StepError(
            f"{ctx.root} is a clone of {'/'.join(existing)}, not {repository}", "use another -C DIR"
        )

    view = _json(ctx.gh("repo", "view", repository, "--json", "isPrivate,url"))
    if view is None:
        if ui.rich:
            ui.note(
                f"{repository} doesn't exist yet. It will be PRIVATE (the product's workflows are shared only "
                "with private repositories of your account)."
            )
        else:
            ui.info(
                f"{repository} doesn't exist yet. It will be PRIVATE (the product's workflows are shared only with"
            )
            ui.info("private repositories of your account).")
        if not ui.confirm(
            "create_repo", f"Create the private repository {repository} on GitHub?", default=True
        ):
            raise StepError(
                "the config repo is needed", f"create {repository} (private) or pick an existing one"
            )
        proc = ctx.gh(
            "repo", "create", repository, "--private", "--description", "Minecraft server config (mcctl)"
        )
        if proc.returncode != 0:
            raise StepError(f"`gh repo create` failed: {(proc.stderr or proc.stdout).strip()}")
        ui.ok(f"created github.com/{repository} (private)")
        ctx.created.append(f"GitHub repository {repository} (private)")
        view = _json(ctx.gh("repo", "view", repository, "--json", "isPrivate,url")) or {"isPrivate": True}
    elif not view.get("isPrivate"):
        raise StepError(
            f"{repository} is public: a config repo must be private",
            f"gh repo edit {repository} --visibility private --accept-visibility-change-consequences",
        )
    else:
        ui.ok(f"{repository} exists and is private")

    ctx.root.mkdir(parents=True, exist_ok=True)
    if not (ctx.root / ".git").exists():
        if any(p.name not in (".mcctl", envfile.ENV_FILE) for p in ctx.root.iterdir()):
            raise StepError(
                f"{ctx.root} isn't empty and isn't a git repository", "use an empty or new -C DIR"
            )
        ctx.git("init", "-q", "-b", "main")
        ctx.git("remote", "add", "origin", f"https://github.com/{repository}.git")
        if ctx.git("fetch", "-q", "origin", "main", check=False).returncode == 0:
            ctx.git("checkout", "-q", "-B", "main", "origin/main")

    sha, version = _product_pin(ctx)
    if ctx.opts.seed is not None and not ctx.server_path.exists():
        text = ctx.opts.seed.read_text()
        lines = text.splitlines(keepends=True)
        if lines and lines[0].startswith(
            "# yaml-language-server:"
        ):  # a path relative to the product checkout
            text = "".join(lines[1:])
        ctx.server_path.write_text(text)
        ui.ok(f"server.yml seeded from {ctx.opts.seed} (the questions keep its values as defaults)")
    with tempfile.TemporaryDirectory() as tmp:
        configrepo.materialize(
            Path(tmp) / "t", sha=sha, version=version, repository=configrepo.PRODUCT_REPOSITORY
        )
        for rel in configrepo.template_files():
            dest = ctx.root / rel
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(Path(tmp) / "t" / rel, dest)
    for ignored in (envfile.ENV_FILE, ".mcctl/"):  # admin tokens and local state never reach git
        if ctx.git("check-ignore", "-q", ignored.rstrip("/"), check=False).returncode != 0:
            with open(ctx.root / ".gitignore", "a") as fh:
                fh.write(f"{ignored}\n")
    ctx.set({"github.owner": owner, "github.config_repo": name})
    ctx.commit(f"mcctl init: config repo from the product template ({version})", ["."])
    ui.ok(
        f"{ctx.root} is the local clone; product pinned to {configrepo.PRODUCT_REPOSITORY}@{sha[:12]} ({version})"
    )
    return {
        "repository": repository,
        "url": view.get("url") or f"https://github.com/{repository}",
        "product_sha": sha,
    }


# --- step 2: questions -> server.yml ------------------------------------------------------------------


def _default(ctx: Ctx, path: str, fallback: Any = None) -> str | None:  # noqa: ANN401
    value = ctx.current(path)
    if value is not None and not schema.field_errors(path, value):
        text = str(value)
        if not re.search(schema.PLACEHOLDER_DOMAIN, text) and not re.search(schema.PLACEHOLDER_ZONE, text):
            return text if path != "name" or text != "example" else None
    return None if fallback is None else str(fallback)


def _player(ctx: Ctx, name: str) -> schema.Player:
    def lookup() -> tuple[str, str] | None:
        try:
            return mojang.uuid_for(name, ctx.deps.transport)
        except mojang.MojangError as e:
            ctx.ui.warn(str(e))
            return None

    found = _guarded(lookup)
    if found is None:
        uuid = ctx.ui.ask(
            f"uuid_{name}",
            f"Mojang doesn't know {name!r}: its UUID (dashed), or the correct name",
            check=lambda v: None if re.fullmatch(schema.UUID, v) else "a dashed lowercase UUID",
        )
        return schema.Player(name=name, uuid=uuid)
    real, uuid = found
    ctx.ui.ok(f"{real}: {uuid}")
    return schema.Player(name=real, uuid=uuid)


def step_questions(ctx: Ctx) -> dict[str, Any]:
    ui = ctx.ui
    status = _json(ctx.deps.run(STATUS_JSON)) or {}
    me = (status.get("User") or {}).get(str((status.get("Self") or {}).get("UserID")), {})
    ts_login = me.get("LoginName") if isinstance(me, dict) else None
    git_name = ctx.git("config", "user.name", check=False).stdout.strip() or None
    git_email = ctx.git("config", "user.email", check=False).stdout.strip() or None
    repo_name = str(ctx.current("github.config_repo") or "")
    slug = re.sub(r"-?(mc-)?config$", "", repo_name.lower()) or None
    slug = slug if slug and not schema.field_errors("name", slug) else None

    ui.info(
        "A few questions. Every answer is checked against the server.yml schema; Enter takes the default."
    )
    v: dict[str, Any] = {}
    v["name"] = ui.ask(
        "name",
        "Short name for this server (derives bucket/host names)",
        default=_default(ctx, "name", slug),
        check=_field_check("name"),
    )
    v["admin.name"] = ui.ask(
        "admin_name",
        "Your name (MOTD, eula.txt)",
        default=_default(ctx, "admin.name", git_name),
        check=_field_check("admin.name"),
    )
    v["admin.tailscale_login"] = ui.ask(
        "tailscale_login",
        "Your Tailscale login (user@example.com or user@github)",
        default=_default(ctx, "admin.tailscale_login", ts_login),
        check=_field_check("admin.tailscale_login"),
    )
    email_default = ts_login if ts_login and re.fullmatch(schema.EMAIL, ts_login) else git_email
    v["admin.email"] = ui.ask(
        "email",
        "Email allowed to log in to the admin panel (Cloudflare Access sends one-time codes here; "
        "not your Cloudflare account login)",
        default=_default(ctx, "admin.email", email_default),
        check=_field_check("admin.email"),
    )
    v["dns.zone"] = ui.ask(
        "zone",
        "Your domain on Cloudflare (the zone, e.g. example.org)",
        default=_default(ctx, "dns.zone"),
        check=_field_check("dns.zone"),
    )
    v["dns.hostname"] = ui.ask(
        "hostname",
        "Server hostname in that zone",
        default=_default(ctx, "dns.hostname", "mc"),
        check=_field_check("dns.hostname"),
    )
    v["host.timezone"] = ui.ask(
        "timezone",
        "Server time zone (reboot/update windows)",
        default=_default(ctx, "host.timezone", _local_timezone()),
        check=_field_check("host.timezone"),
    )
    versions = sorted(p.name for p in (ctx.product_root / "server-config" / "keys").iterdir() if p.is_dir())
    ui.info(f"Minecraft versions this product version supports: {', '.join(versions)}.")
    ui.info("This is your choice (it must be >= your world's version; changing it later is manual).")
    current_version = _default(ctx, "minecraft.version")
    v["minecraft.version"] = ui.choose(
        "minecraft_version",
        "Minecraft version",
        versions,
        default=current_version if current_version in versions else None,
    )
    ui.info("Running a server means accepting the Minecraft EULA: https://aka.ms/MinecraftEULA")
    if not ui.confirm("accept_eula", "Do you accept the Minecraft EULA?", default=False):
        raise StepError("the Minecraft EULA must be accepted to run a server", "re-run and accept it")
    v["minecraft.accept_eula"] = True
    v["world.border_radius.overworld"] = _int_or_none(
        ui.ask(
            "border_overworld",
            "World border radius, overworld (blocks, or none)",
            default=_default(ctx, "world.border_radius.overworld", 5000),
            check=_field_check("world.border_radius.overworld", _int_or_none),
        )
    )
    v["world.border_radius.nether"] = _int_or_none(
        ui.ask(
            "border_nether",
            "World border radius, nether (blocks, or none)",
            default=_default(ctx, "world.border_radius.nether", 1250),
            check=_field_check("world.border_radius.nether", _int_or_none),
        )
    )
    v["alerts.discord"] = ui.confirm(
        "discord",
        "Send alerts to a Discord channel (you'll paste a webhook URL)?",
        default=bool(ctx.current("alerts.discord")),
    )
    extra = ui.answers.get("set", {}) if ui.answers else {}
    if not isinstance(extra, dict):
        raise Abort("answers file: `set` must map dotted server.yml paths to values")
    v |= extra
    ctx.set(v)
    text = ctx.server_path.read_text()
    ctx.server_path.write_text(text.replace(TEMPLATE_NOTE, INIT_NOTE))

    players = schema.Players()
    op = ui.ask(
        "op",
        "Your Minecraft name (whitelisted + operator; empty to skip)",
        default="",
        check=lambda x: None if not x or re.fullmatch(schema.PLAYER_NAME, x) else "3-16 letters, digits or _",
    )
    others_raw = ui.ask("players", "Other players to whitelist (comma-separated, empty for none)", default="")
    names = [
        n.strip()
        for n in (others_raw if isinstance(others_raw, str) else ",".join(others_raw)).split(",")
        if n.strip()
    ]
    whitelist: dict[str, schema.Player] = {}
    ops = []
    for n in ([op] if op else []) + names:
        p = _player(ctx, n)
        whitelist[p.uuid] = p
        if n == op:
            ops.append(schema.Op(name=p.name, uuid=p.uuid, level=4))
    if whitelist:
        players = schema.Players(whitelist=list(whitelist.values()), ops=ops)
    (ctx.root / configrepo.PLAYERS_YML).write_text(schema.dump_players(players))
    repo = ctx.repo()
    for w in schema.placeholders(repo.server):
        raise StepError(w, "answer with your real values (re-run `mcctl init --from questions`)")
    ctx.commit(
        "mcctl init: server.yml and players.yml from the wizard",
        [configrepo.SERVER_YML, configrepo.PLAYERS_YML],
    )
    ui.ok(
        f"server.yml: {repo.server.name}, {repo.server.dns.hostname}.{repo.server.dns.zone}, Minecraft {repo.server.minecraft.version}"
    )
    return {"name": repo.server.name, "players": len(players.whitelist)}


# --- step 3: Cloudflare -------------------------------------------------------------------------------


# Earlier versions minted the admin token from a bootstrap token (`cloudflare_token: account-mint` or `mint`).
# Cloudflare refuses that (error 1001: a token created through the API may not manage tokens, and the admin
# token must); a run that failed there left the bootstrap token behind.
BOOTSTRAP_LEFTOVER = "mcctl {name} bootstrap"


def step_cloudflare(ctx: Ctx) -> dict[str, Any]:
    """The admin token Terraform uses: an account-owned token (it can never reach another account) the admin
    creates in the dashboard (pre-filled link plus the exact form, providers/cloudflare.admin_token_lines),
    checked live and its own policies audited (missing, extra, wider resources). It can't be minted: Cloudflare
    refuses to create a token that manages tokens through the API (error 1001, cloudflare.SUB_TOKEN_ERROR), and
    the admin token creates the R2 credentials. A CLOUDFLARE_API_TOKEN already in .env/the shell (also a
    user-owned one from an older setup) is re-validated instead. server.yml cloudflare.token_owner records who
    owns it (Terraform's R2 token follows)."""
    cfg = ctx.repo().server
    env = ctx.env()
    token = env.get("CLOUDFLARE_API_TOKEN")
    if not token:
        _account_cloudflare_token(ctx, cfg)
        token = ctx.env()["CLOUDFLARE_API_TOKEN"]
    check = _guarded(
        lambda: cloudflare.check(
            token,
            cfg.dns.zone,
            ctx.deps.transport,
            now=ctx.deps.now(),
            s3_endpoint=env.get("AWS_ENDPOINT_URL_S3"),
        )
    )
    _credential(ctx, check, "Cloudflare token")
    owner = check.facts.get("owner", "user")
    if owner == "account":
        _audit_account_token(ctx, cfg, token, check)
    if cfg.cloudflare.token_owner != owner:
        ctx.set({"cloudflare.token_owner": owner})
        ctx.commit(f"mcctl init: cloudflare.token_owner {owner}", [configrepo.SERVER_YML])
    prev = ctx.outputs("cloudflare")
    if prev.get("auto_renew") is None:
        ctx.ui.info(
            f"Domain auto-renew: if {cfg.dns.zone} is registered with Cloudflare, switch auto-renew on in the "
            "dashboard (Domain Registration > Manage domains > your domain > Auto-renew); mcctl doesn't hold a "
            "registrar permission. `mcctl doctor` and the weekly expiry check watch the expiry date."
        )
    return {
        "account_id": check.facts.get("account_id"),
        "owner": owner,
        "expires_on": check.expires_on.isoformat() if check.expires_on else None,
        "auto_renew": prev.get("auto_renew"),
    }


def _account_cloudflare_token(ctx: Ctx, cfg: schema.ServerConfig) -> None:
    ui = ctx.ui
    access = cfg.panel.mode == "cloudflare"
    old = ui.answers.get("cloudflare_token") if ui.answers else None
    if old not in (None, "account"):
        ui.warn(
            f"answers file: cloudflare_token {old!r} is gone (Cloudflare doesn't allow minting the admin "
            "token): create it by hand as below and drop the key"
        )
    if ctx.state.get("cloudflare") is not None or old not in (None, "account"):
        # A run of an older mcctl (which minted the token) may have failed after the bootstrap token was made.
        leftover = BOOTSTRAP_LEFTOVER.format(name=cfg.name)
        ui.warn(
            f"if an earlier `mcctl init` had you create a bootstrap token {leftover!r}, delete it in the "
            "dashboard (Manage Account > Account API Tokens, or My Profile > API Tokens): it can create "
            "tokens and isn't needed any more"
        )
    url = cloudflare.token_url(f"mcctl {cfg.name} (admin)", access=access)
    if ui.rich:
        _cloudflare_token_form(ctx, cfg.dns.zone, url, access)
    else:
        ui.info(f"Create mcctl's admin token by hand in the Cloudflare account that owns {cfg.dns.zone}.")
        ui.info("It is account-owned, so it can never act on another account. mcctl can't create it for you:")
        ui.info("Cloudflare doesn't let a token made through the API manage tokens, and this one must (it")
        ui.info(
            "creates the bucket-only R2 credentials). Pre-filled form (pick the account, check every row):"
        )
        ui.info(f"  {url}")
        for line in cloudflare.admin_token_lines(cfg.dns.zone, access=access):
            ui.info(line)
        ui.info(
            "Then paste it here (hidden; saved to .env with mode 0600, never printed). mcctl checks it live"
        )
        ui.info("and compares its permissions with the list above (missing, extra or wider ones).")
    token = ui.secret(
        "cloudflare_api_token", "Paste the account-owned admin token", env_name="CLOUDFLARE_API_TOKEN"
    )
    ctx.save_env(CLOUDFLARE_API_TOKEN=token)


def _cloudflare_token_form(ctx: Ctx, zone: str, url: str, access: bool) -> None:
    """The admin token form as a "Your turn" panel and a permission table (rich runs)."""
    ui = ctx.ui
    perms = cloudflare.ACCOUNT_TOKEN_PERMISSIONS + (cloudflare.ACCOUNT_TOKEN_ACCESS if access else ())
    ui.todo(
        [
            f"Open the pre-filled form and pick the account that owns {zone}: {url}",
            f"Check the permissions: exactly the {len(perms)} rows in the table below, nothing else (each row: "
            "Account/Zone, the permission, Read/Edit; the form may say Write for Edit).",
            f"Account Resources (if shown): Include > the account that owns {zone}. Zone Resources: Include > "
            f"Specific zone > {zone} (not All zones).",
            "Client IP Address Filtering: leave empty (CI runners change addresses). TTL: start today, end about "
            "a year ahead (mcctl doctor warns 30 days before it expires).",
            "Continue to summary > Create Token, then paste the token below (input hidden; saved to .env with "
            "mode 0600, never printed).",
        ],
        intro="mcctl's admin token is account-owned, so it can never act on another account. mcctl can't "
        "create it for you: Cloudflare doesn't let a token made through the API manage tokens, and this one "
        "must (it creates the bucket-only R2 credentials). Dashboard path: Manage Account > Account API Tokens "
        "> Create Token > Custom token.",
    )
    ui.table(
        ["Scope", "Permission", "Level", "Resources", "Used for"],
        [
            [
                kind,
                name,
                level,
                f"Specific zone > {zone}" if res == "zone" else f"the account that owns {zone}",
                cloudflare.PURPOSE[name],
            ]
            for kind, name, level, res in perms
        ],
        title="Token permissions",
        styles=("", "bold", "", "", "dim"),
        indent=2,
    )
    lookalikes = [
        "the two Zone rows need the FIRST dropdown set to Zone (the Account list has no DNS, Zone rows)",
        "Zone > DNS, not Zone DNS Settings, Account DNS Settings, DNS View or DNS Firewall",
        "Zone > Zone, not Zone Settings",
    ]
    if access:
        lookalikes += [
            "the combined Access: Apps and Policies, not Access: Apps or Access: Policies",
            "the combined Access: Organizations, Identity Providers, and Groups, not Access: Organizations or "
            "Access: Identity Providers",
        ]
    for text in lookalikes:
        ui.warn(f"look-alike: {text}")
    ui.note(
        "mcctl checks the token live and compares its permissions with the table (missing, extra, wider)."
    )


def _audit_account_token(ctx: Ctx, cfg: schema.ServerConfig, token: str, check: CredentialCheck) -> None:
    """Compare an account-owned token's own policies with what mcctl needs: warn on extras, wider resources or
    gaps (the functional probes in cloudflare.check already failed on a missing core permission)."""
    ui, account, token_id = ctx.ui, check.facts.get("account_id"), check.facts.get("token_id")
    if not account or not token_id:
        return
    try:
        policies = cloudflare.token_policies(token, account, token_id, ctx.deps.transport)
    except (cloudflare.CloudflareError, http.HttpError) as e:
        ui.warn(f"couldn't read the token's own permissions to audit them ({e})")
        return
    missing, extra, scope = cloudflare.audit_policies(
        policies, str(check.facts.get("zone_id") or ""), account, access=cfg.panel.mode == "cloudflare"
    )
    for name in missing:
        ui.warn(
            f"the token lacks {name!r} (see the list above): Terraform may fail; edit the token and re-run"
        )
    for name in extra:
        if row := cloudflare.lookalike(name):
            ui.warn(
                f"the token has {name!r}, a look-alike of the row mcctl needs: replace it with {row} "
                "(edit the token; see the list above)"
            )
        else:
            ui.warn(f"the token has {name!r}, which mcctl doesn't need: remove it (smaller blast radius)")
    for what in scope:
        ui.warn(f"the token reaches {what}: restrict it to {cfg.dns.zone} and its account")
    if not (missing or extra or scope):
        ui.ok(f"account-owned token: exactly the permissions mcctl needs, on {cfg.dns.zone} and its account")


# --- step 3b: Zero Trust (panel.mode cloudflare) --------------------------------------------------------------


def step_zero_trust(ctx: Ctx) -> dict[str, Any]:
    """The account's Zero Trust organization must exist before Terraform can manage the panel's Access apps;
    this step also checks the organization's global session (cloudflare.global_session_bounds: at most 30m by
    default) and sets it through the API only when it is longer; a refused write is recorded, and re-runs then
    only re-check (the fix names the dashboard setting). Signing up (team name + the Free plan) is a dashboard
    step: this detects it through the API, links there and re-checks."""
    ui, cfg = ctx.ui, ctx.repo().server
    if cfg.panel.mode != "cloudflare":
        ui.ok(f"not needed (panel.mode: {cfg.panel.mode})")
        return {"needed": False}
    token = ctx.env()["CLOUDFLARE_API_TOKEN"]
    account = ctx.outputs("cloudflare").get("account_id")
    if not account:
        raise StepError("the Cloudflare step has no account id", "re-run `mcctl init --from cloudflare`")
    transport = ctx.deps.transport
    org = _guarded(lambda: cloudflare.zero_trust_org(token, account, transport))
    for _ in range(3):
        if org is not None:
            break
        if ui.rich:
            ui.todo(
                [
                    f"Open {cloudflare.ZERO_TRUST_URL} and pick the account that owns {cfg.dns.zone}.",
                    "Choose a team name and the Free plan (Cloudflare may ask for a payment method; Free costs "
                    "nothing).",
                    "Come back here and answer the question below: mcctl re-checks through the API.",
                ],
                intro="Cloudflare Zero Trust isn't set up on this account yet (the panel's login runs on it).",
            )
        else:
            ui.info("Cloudflare Zero Trust isn't set up on this account yet (the panel's login runs on it).")
            ui.info(
                f"Open {cloudflare.ZERO_TRUST_URL} , pick your account, choose a team name and the Free plan"
            )
            ui.info("(Cloudflare may ask for a payment method; Free costs nothing). Then come back here.")
        if not ui.confirm("zero_trust_enrolled", "Done? (re-checks through the API)", default=True):
            raise Paused("waiting_for_zero_trust", f"sign up for Zero Trust at {cloudflare.ZERO_TRUST_URL}")
        org = _guarded(lambda: cloudflare.zero_trust_org(token, account, transport))
    if org is None:
        raise StepError(
            "Zero Trust still isn't set up on this account",
            f"finish the sign-up at {cloudflare.ZERO_TRUST_URL} (same account as {cfg.dns.zone}), then re-run",
        )
    ui.ok(f"Zero Trust team {org.get('auth_domain')}")
    reauth = cfg.panel.access.reauth_minutes
    target, limit = cloudflare.global_session_bounds(reauth)
    current = org.get("session_duration") or "unset"
    minutes = cloudflare.duration_minutes(org.get("session_duration"))
    fix = cloudflare.global_session_fix(reauth)
    why = (
        "it bounds how long after your last one-time PIN Access may open the confirmation host without a new "
        "one; your panel login keeps its own 24h session"
    )
    if minutes is not None and minutes <= limit:  # as the admin set it: nothing to write
        ui.ok(f"global session {current} (<= {limit}m: {why})")
        return {"needed": True, "auth_domain": org.get("auth_domain"), "global_session": current}
    if ctx.outputs("zero-trust").get("global_session") == "refused":
        # An earlier run's write was refused: re-check only (never re-write); the admin changes it by hand.
        raise StepError(
            f"the Zero Trust global session is still {current} (longer than {limit}m; {why})", fix
        )
    try:
        org = cloudflare.set_session_duration(token, account, org, f"{target}m", transport, fix=fix)
    except (cloudflare.CloudflareError, http.HttpError) as e:
        ctx.state.record("zero-trust", "failed", outputs={"global_session": "refused"})
        raise StepError(
            f"the Zero Trust global session is {current} (longer than {limit}m) and setting it to {target}m "
            f"through the API failed: {e}",
            f"{fix}; re-running only re-checks it",
        ) from None
    ui.ok(f"global session {current} -> {org.get('session_duration')} ({why})")
    return {
        "needed": True,
        "auth_domain": org.get("auth_domain"),
        "global_session": org.get("session_duration"),
    }


# --- step 4: Terraform state bucket + bucket-scoped token ---------------------------------------------


def _state_creds_work(ctx: Ctx, endpoint: str, bucket: str, key_id: str, secret: str) -> bool:
    from mcctl import sigv4

    with ctx.ui.spinner(f"checking the state credentials against bucket {bucket}"):
        for attempt in range(8):  # a new R2 token can take a few seconds to work
            status = _guarded(
                lambda: sigv4.bucket_usable(
                    endpoint, bucket, key_id, secret, ctx.deps.transport, ctx.deps.now()
                )
            )
            if status == 200:
                return True
            if attempt < 7:
                ctx.deps.sleep(5)
    return False


def _offer_token_delete(ctx: Ctx, token: str, owner: str, account: str, token_id: str) -> bool:
    """After a failed check, offer to delete the state token (so a re-run mints a new one without leaving
    an orphan behind). True if it was deleted and .env no longer names it."""
    if not ctx.ui.confirm(
        "delete_state_token",
        f"Delete the state token {token_id} through the API (a re-run then mints a fresh one)?",
        default=False,
    ):
        return False
    _guarded(lambda: cloudflare.delete_token(token, owner, account, token_id, ctx.deps.transport))
    envfile.write(ctx.root, {"AWS_ACCESS_KEY_ID": "", "AWS_SECRET_ACCESS_KEY": ""})
    ctx.ui.ok(f"deleted token {token_id} and cleared AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY in .env")
    return True


def step_state_backend(ctx: Ctx) -> dict[str, Any]:
    ui, cfg = ctx.ui, ctx.repo().server
    env = ctx.env()  # AWS_* in it come from .env only (envfile.merged)
    if ignored := envfile.shadowed(ctx.root, ctx.deps.credentials_env):
        ui.warn(
            f"IGNORING {', '.join(ignored)} from your shell: the Terraform state credentials come from "
            f"{ctx.root / envfile.ENV_FILE} only. Unset them in the shell to silence this."
        )
    cf = ctx.outputs("cloudflare")
    account, owner = cf.get("account_id"), cf.get("owner", "user")
    if not account:
        raise StepError("the Cloudflare step has no account id", "re-run `mcctl init --from cloudflare`")
    token, bucket = env["CLOUDFLARE_API_TOKEN"], cfg.state.bucket
    endpoint = f"https://{account}.r2.cloudflarestorage.com"
    transport = ctx.deps.transport
    if _guarded(lambda: cloudflare.bucket_exists(token, account, bucket, transport)):
        ui.ok(f"R2 bucket {bucket} exists")
    else:
        _guarded(lambda: cloudflare.create_bucket(token, account, bucket, cfg.backups.location, transport))
        ui.ok(f"created R2 bucket {bucket} ({cfg.backups.location})")
        ctx.created.append(f"R2 bucket {bucket} (Terraform state)")
    key_id, secret = env.get("AWS_ACCESS_KEY_ID"), env.get("AWS_SECRET_ACCESS_KEY")
    minted = False
    if (
        key_id
        and secret
        and _guarded(lambda: cloudflare.token_exists(token, owner, account, key_id, transport))
    ):
        ui.ok("state credentials in .env belong to a live token")
    else:
        minted = True
        token_id, value = _guarded(
            lambda: cloudflare.create_r2_token(token, owner, account, bucket, transport)
        )
        key_id, secret = cloudflare.r2_credentials(token_id, value)
        ctx.save_env(AWS_ACCESS_KEY_ID=key_id, AWS_SECRET_ACCESS_KEY=secret, AWS_ENDPOINT_URL_S3=endpoint)
        ui.ok(f"created an API token that can only read/write objects in {bucket}")
        ctx.created.append(f"Cloudflare API token r2-{bucket}-rw (Terraform state)")
    if env.get("AWS_ENDPOINT_URL_S3") not in (None, endpoint):
        ctx.save_env(AWS_ENDPOINT_URL_S3=endpoint)
    if not _state_creds_work(ctx, endpoint, bucket, key_id, secret):
        what = "the token mcctl just created" if minted else "the token in .env"
        deleted = _offer_token_delete(ctx, token, owner, account, key_id)
        raise StepError(
            f"the state credentials ({what}) can't use bucket {bucket} (HeadBucket and ListObjectsV2 refused)",
            "re-run: a fresh token is minted"
            if deleted
            else "re-run in a minute (new tokens can take a moment). If it persists, re-run and answer yes to "
            f"deleting token {key_id}, or delete it under "
            + ("Manage Account > Account API Tokens" if owner == "account" else "My Profile > API Tokens")
            + " and clear AWS_* in .env",
        )
    ui.ok(f"the state credentials can use bucket {bucket}")
    return {"bucket": bucket, "endpoint": endpoint}


# --- step 5: GitHub -----------------------------------------------------------------------------------


def pat_url(name: str, description: str, owner: str, permissions: Mapping[str, str], days: int = 366) -> str:
    """GitHub's pre-filled fine-grained PAT form (docs: managing your personal access tokens, 2026-10-31).
    Repository selection can't be pre-filled: the user picks the one repository."""
    query = {"name": name[:40], "description": description, "target_name": owner, "expires_in": str(days)}
    return "https://github.com/settings/personal-access-tokens/new?" + urllib.parse.urlencode(
        query | dict(permissions)
    )


def step_github(ctx: Ctx) -> dict[str, Any]:
    ui, repo = ctx.ui, ctx.repo()
    cfg, pin = repo.server, repo.pin
    env, transport = ctx.env(), ctx.deps.transport
    repository = f"{cfg.github.owner}/{cfg.github.config_repo}"
    out: dict[str, Any] = {}

    token = env.get("GITHUB_TOKEN")
    fresh = token is None
    if fresh:
        url = pat_url(
            f"mcctl {cfg.name} admin",
            f"Terraform: Actions secrets, environments and rulesets of {repository}",
            cfg.github.owner,
            {"secrets": "write", "environments": "write", "administration": "write", "actions": "read"},
        )
        if ui.rich:
            _pat_form(
                ctx,
                f"GitHub token for Terraform (writes {repository}'s Actions secrets and the rulesets that "
                "protect main).",
                url,
                repository,
                [
                    ("Secrets", "Read and write", "the Actions secrets CI deploys with"),
                    ("Environments", "Read and write", "the deploy/update environments that hold them"),
                    ("Administration", "Read and write", "the rulesets that protect main"),
                    ("Actions", "Read-only", "reading the environments back (GET /environments/...)"),
                    ("Metadata", "Read-only", "added automatically"),
                ],
            )
        else:
            ui.info(f"GitHub token for Terraform (writes {repository}'s Actions secrets). Open:")
            ui.info(url)
            ui.info(
                f"Repository access: Only select repositories -> {repository}. Permissions: Secrets read/write, "
                "Environments read/write, Administration read/write (the rulesets that protect main), Actions read-only"
            )
            ui.info(
                "(Metadata read is added automatically). 366 days; `mcctl doctor` warns 30 days before expiry."
            )
        token = ui.secret("github_token", "Paste the GitHub token", env_name="GITHUB_TOKEN")
    check = _guarded(lambda: github.check_admin(token, repository, transport))
    _credential(ctx, check, "GitHub admin token")
    if fresh:
        ctx.save_env(GITHUB_TOKEN=token)
    out["admin_expires_on"] = check.expires_on.isoformat() if check.expires_on else None

    public = _guarded(lambda: github.repo_is_public(pin.repository, transport))
    if public:
        ui.ok(f"{pin.repository} is public: no product-read token needed")
    else:
        prt = env.get("TF_VAR_product_read_token")
        fresh = prt is None
        if fresh:
            p_owner = pin.repository.split("/")[0]
            url = pat_url(
                f"mcctl {cfg.name} product read",
                f"CI checkout of {pin.repository}",
                p_owner,
                {"contents": "read"},
            )
            if ui.rich:
                _pat_form(
                    ctx,
                    f"The product repo {pin.repository} is private: CI needs a read-only token for it.",
                    url,
                    pin.repository,
                    [
                        ("Contents", "Read-only", "CI checks out the pinned product"),
                        ("Metadata", "Read-only", "added automatically"),
                    ],
                )
            else:
                ui.info(
                    f"The product repo {pin.repository} is private: CI needs a read-only token for it. Open:"
                )
                ui.info(url)
                ui.info(
                    f"Repository access: Only select repositories -> {pin.repository}. Permissions: Contents read."
                )
            prt = ui.secret(
                "product_read_token", "Paste the product-read token", env_name="TF_VAR_product_read_token"
            )
        check = _guarded(lambda: github.check_product_read(prt, pin.repository, pin.sha, transport))
        _credential(ctx, check, "product-read token")
        if fresh:
            ctx.save_env(TF_VAR_product_read_token=prt)
        # A private product's reusable workflow must be shared with the owner's private repositories:
        # access level `user` for a personal account, `organization` for an organization (GitHub REST).
        access = _json(ctx.gh("api", f"repos/{pin.repository}/actions/permissions/access"))
        level = (access or {}).get("access_level")
        p_owner = pin.repository.split("/")[0]
        if level in ("user", "organization", "enterprise"):
            ui.ok(f"{pin.repository} shares its workflows with your repositories ({level})")
        elif level == "none" and ui.confirm(
            "share_workflows",
            f"Allow repositories of {p_owner} to use {pin.repository}'s workflows (Settings > Actions > Access)?",
            default=True,
        ):
            kind = (_json(ctx.gh("api", f"users/{p_owner}")) or {}).get("type")
            wanted = "organization" if kind == "Organization" else "user"
            proc = ctx.gh(
                "api",
                "-X",
                "PUT",
                f"repos/{pin.repository}/actions/permissions/access",
                "-f",
                f"access_level={wanted}",
            )
            if proc.returncode != 0:
                where = "organization" if wanted == "organization" else "user"
                raise StepError(
                    "couldn't change the product's Actions access setting",
                    f"{pin.repository} > Settings > Actions > General > Access: 'Accessible from repositories owned by the {where}'",
                )
            ui.ok(f"workflow access set to the {wanted}'s private repositories")
        elif ctx.outputs("github").get("workflows_shared") is True and not ctx.reconfigure("github"):
            ui.ok(f"{pin.repository} shares its workflows (you confirmed it on an earlier run)")
            out["workflows_shared"] = True
        else:
            ui.warn(
                f"couldn't read {pin.repository}'s Actions access setting: check Settings > Actions > General > Access"
            )
            if not ui.confirm(
                "workflows_shared", "Is it 'Accessible from repositories owned by the user'?", default=False
            ):
                raise StepError(
                    "the config repo can't call the product's workflow yet",
                    "set the Access option, then re-run",
                )
            out["workflows_shared"] = True  # remembered: a resume doesn't ask again
    # P10 review: rulesets (github.protect_main) need a paid plan for a private personal repo; probe it.
    if cfg.github.protect_main:
        plan = _guarded(lambda: github.rulesets_available(token, repository, transport))
        if plan is False:
            raise StepError(
                f"{repository} can't have rulesets on this GitHub plan (private repos of personal accounts need "
                "GitHub Pro)",
                "upgrade to GitHub Pro (recommended: it keeps the panel's GitHub App away from main), or set "
                "github.protect_main: false in server.yml and read SECURITY.md 'The panel's GitHub App'",
            )
        if plan:
            ui.ok(f"{repository}: rulesets available (main gets the pull-request + checks ruleset)")
    # Security review M2: environments (github.environments) on a private personal repo need the same plan.
    if cfg.github.environments and not cfg.github.protect_main:
        plan = _guarded(lambda: github.rulesets_available(token, repository, transport))
        if plan is False:
            raise StepError(
                f"{repository} can't have deployment-branch environments on this GitHub plan (private repos of "
                "personal accounts need GitHub Pro)",
                "upgrade to GitHub Pro (recommended: it keeps the deploy secrets away from other branches), or "
                "set github.environments: false in server.yml and read SECURITY.md 'Actions secrets'",
            )
    if _guarded(lambda: github.disable_forking(token, repository, transport)):
        ui.ok(f"{repository}: forking off (a fork would carry the config repo's history)")
    else:
        ui.info(
            f"{repository}: forking setting not offered (GitHub has it for organization-owned private repos only;"
            " a private personal repo can be forked only by its collaborators)"
        )
    if _guarded(lambda: github.enable_auto_merge(token, repository, transport)):
        ui.ok(f"{repository}: auto-merge allowed, merged branches deleted (the update PRs, P11)")
    else:
        ui.warn(f"couldn't allow auto-merge on {repository}: Settings > General > Allow auto-merge")
    if cfg.panel.mode == "cloudflare":
        out |= _github_app(ctx, ghapp.PANEL, repository, token)
    out |= _github_app(ctx, ghapp.UPDATER, repository, token)
    return out


def _pat_form(
    ctx: Ctx, intro: str, url: str, repository: str, permissions: Sequence[tuple[str, str, str]]
) -> None:
    """A fine-grained PAT: "Your turn" steps and its permission table (rich runs)."""
    ui = ctx.ui
    ui.todo(
        [
            f"Open the pre-filled form (name, description, owner, 366 days, permissions): {url}",
            f"Repository access: Only select repositories > {repository} (the form can't pre-fill this).",
            "Check the permissions against the table below, then Generate token.",
            "Paste the token below (input hidden; saved to .env with mode 0600, never printed).",
        ],
        intro=intro,
    )
    ui.table(
        ["Scope", "Permission", "Level", "Resources", "Used for"],
        [["Repository", name, level, repository, why] for name, level, why in permissions],
        title="Token permissions",
        styles=("", "bold", "", "", "dim"),
        indent=2,
    )
    ui.note("366 days; `mcctl doctor` warns 30 days before it expires.")


def _github_app(ctx: Ctx, kind: ghapp.Kind, repository: str, admin_token: str) -> dict[str, Any]:
    """P10 review / P11: a GitHub App with exactly `kind.permissions`, installed on the config repo only, created
    with GitHub's App manifest flow (one click in the browser) -> TF_VAR_<kind>_github_app_id and _private_key
    (base64 PEM) in .env. Terraform hands the panel's key to the Worker only and the updater's to the Actions
    secret its update job reads. Resume-safe: an App already in .env (an earlier run, maybe one that stopped
    while installing) is found again through the API (slug, installation) instead of making a second one."""
    ui, cfg = ctx.ui, ctx.repo().server
    env, transport = ctx.env(), ctx.deps.transport
    app_id, key = env.get(kind.id_var), env.get(kind.key_var)
    prev = ctx.outputs("github")
    slug = prev.get(f"{kind.key}_app")
    if app_id and key:
        if not app_id.isdigit():
            raise StepError(f"{kind.id_var} must be the App's numeric id", f"fix it in {envfile.ENV_FILE}")
        try:
            pem = ghapp.decode_key(key)
        except ValueError:
            raise StepError(
                f"{kind.key_var} isn't a base64 PEM", "delete both lines from .env and re-run"
            ) from None
        # Checked as the App (JWT signed with the saved key): the App is private, so `GET /apps/{slug}`
        # with the admin PAT answers 404 (a live re-run, 2026-11-02).
        try:
            check = _app_api(ctx, lambda: ghapp.verify_app(app_id, pem, kind, transport, ctx.deps.app_sign))
        except ghapp.KeyRejected as e:
            raise StepError(f"{kind.key} App: {e}", e.fix) from None
        if check is _UNKNOWN:
            ui.warn(
                f"{kind.key} App {slug or app_id} (id {app_id}) exists already: not creating another one; its "
                "permissions weren't re-checked"
            )
        else:
            slug = check.facts["slug"]
            ui.ok(f"{kind.key} App {slug} (id {app_id}) exists already: not creating another one")
            _credential(ctx, check, f"{kind.key} App")
    else:
        if ui.rich:
            ui.todo(
                [
                    "Your browser opens a page from mcctl: click 'Create the GitHub App on GitHub'.",
                    "On GitHub: check the name, then Create GitHub App (private, no webhooks).",
                    f"Next, GitHub's install page: Only select repositories > {repository}, nothing else.",
                ],
                intro=f"GitHub App for {kind.purpose}: one click in your browser creates it with exactly "
                "these permissions; then you install it on the config repo only.",
            )
            ui.table(
                ["Scope", "Permission", "Level", "Resources"],
                [
                    ["Repository", name.replace("_", " ").title(), level, repository]
                    for name, level in kind.permissions.items()
                ],
                title=f"{kind.key} App permissions",
                styles=("", "bold"),
                indent=2,
            )
        else:
            ui.info(
                f"GitHub App for {kind.purpose}: one click in your browser creates it on GitHub (private, no "
                f"webhooks, permissions {', '.join(f'{k} {v}' for k, v in kind.permissions.items())}), then you "
                f"install it on {repository} only."
            )
        owner_type = (_json(ctx.gh("api", f"users/{cfg.github.owner}")) or {}).get("type")
        with ui.spinner("waiting for the browser: create the App on GitHub (Ctrl+C cancels)"):
            code = ctx.deps.app_create(kind, cfg.name, repository, owner_type == "Organization", ui.info)
        data = _guarded(lambda: ghapp.convert(code, transport))
        slug, app_id, pem = str(data.get("slug") or ""), str(data["id"]), str(data["pem"])
        _credential(
            ctx,
            ghapp.check_app(admin_token, slug, kind, transport, permissions=data.get("permissions") or {}),
            f"{kind.key} App",
        )
        ctx.save_env(**{kind.id_var: app_id, kind.key_var: ghapp.encode_key(pem)})
        ctx.created.append(f"GitHub App {slug} ({kind.purpose})")
    confirmed = _install_app(ctx, kind, repository, slug, app_id, pem)
    out = {f"{kind.key}_app": slug, f"{kind.key}_app_id": app_id}
    return out | ({f"{kind.key}_app_installed": app_id} if confirmed else {})


_UNKNOWN: Any = object()  # the API couldn't say (no openssl, GitHub down): fall back to asking


def _app_api(ctx: Ctx, call: Callable[[], Any]) -> Any:  # noqa: ANN401
    """A call as the App (a JWT signed with openssl); _UNKNOWN when it can't be made. ghapp.KeyRejected (the
    saved key isn't the App's) passes through: asking again can't help."""
    try:
        return call()
    except ghapp.KeyRejected:
        raise
    except (ProviderError, http.HttpError, OSError, subprocess.SubprocessError) as e:
        ctx.ui.warn(f"couldn't ask GitHub as the App ({e}); continuing without it")
        return _UNKNOWN


def _install_app(
    ctx: Ctx, kind: ghapp.Kind, repository: str, slug: str | None, app_id: str, pem: str
) -> bool:
    """The App must be installed on the config repo, on selected repositories only. Checked as the App
    (`GET /repos/{repo}/installation`); not yet installed -> the install page, whose redirect comes back to the
    run's loopback server, while the API is polled; after a quiet minute init asks for the installation id.
    True when only the person's answer vouches for it (the API couldn't say): remembered for this App id, so
    a resume doesn't ask again."""
    ui, transport = ctx.ui, ctx.deps.transport

    def lookup() -> Any:  # noqa: ANN401
        return _app_api(
            ctx, lambda: ghapp.repo_installation(app_id, pem, repository, transport, ctx.deps.app_sign)
        )

    inst = lookup()
    remembered = ctx.outputs("github").get(f"{kind.key}_app_installed") == app_id
    if inst is _UNKNOWN and remembered and not ctx.reconfigure("github"):
        ui.ok(f"{slug or app_id} is installed on {repository} only (you confirmed it on an earlier run)")
        return True
    if inst is None or inst is _UNKNOWN:
        if not slug:
            raise StepError(
                f"the {kind.key} App (id {app_id}) isn't installed on {repository} and its slug is unknown",
                "install it from https://github.com/settings/apps (the App > Install App > only the config "
                "repo), then re-run",
            )
        ok = inst is None  # the API works: poll it while the browser installs

        def poll() -> str | None:
            found = lookup()
            return str(found["id"]) if isinstance(found, dict) else None

        def ask() -> str:
            return ui.ask(
                f"{kind.key}_app_installation",
                "Installation id or the URL GitHub showed (empty: keep waiting)",
                default="",
            )

        with ui.spinner(
            "waiting for GitHub's install redirect (Ctrl+C cancels; mcctl asks for the URL if nothing comes)"
        ):
            installation = ctx.deps.app_install(slug, ui.info, ask, poll if ok else None)
        if installation:
            ui.ok(f"{slug} installed (installation {installation})")
        inst = lookup() if ok else _UNKNOWN
    if isinstance(inst, dict):
        if inst.get("repository_selection") != "selected":
            raise StepError(
                f"{slug or app_id} is installed on ALL your repositories",
                f"GitHub > Settings > Applications > {slug or 'the App'} > Configure > Only select repositories "
                f"> {repository} only, then re-run",
            )
        repos = _app_api(
            ctx,
            lambda: ghapp.installation_repositories(app_id, pem, inst["id"], transport, ctx.deps.app_sign),
        )
        if repos is _UNKNOWN:
            ui.ok(
                f"{slug or app_id} is installed on selected repositories, {repository} among them (checked as "
                "the App)"
            )
            return False
        total, names = repos
        others = sorted(n for n in names if n.lower() != repository.lower())
        if total != 1 or others:
            raise StepError(
                f"{slug or app_id} is installed on {total} repositories"
                + (f" ({', '.join(others)} besides {repository})" if others else ""),
                f"GitHub > Settings > Applications > {slug or 'the App'} > Configure > Only select repositories "
                f"> {repository} only, then re-run",
            )
        ui.ok(f"{slug or app_id} is installed on {repository} only (checked as the App)")
        return False
    if inst is None:
        raise StepError(
            f"{slug} isn't installed on {repository} (checked as the App)",
            f"https://github.com/apps/{slug}/installations/new (Only select repositories > {repository}), then "
            "re-run",
        )
    if not ui.confirm(
        f"{kind.key}_app_installed",
        f"Is {slug} installed on {repository} only ('Only select repositories')?",
        default=True,
    ):
        raise StepError(
            f"{slug} must be installed on {repository} (and nothing else)",
            f"https://github.com/apps/{slug}/installations/new, then re-run",
        )
    return True


# --- step 5b: the product repo's settings (only for its owner) ---------------------------------------------


def step_product(ctx: Ctx) -> dict[str, Any]:
    """`mcctl product-setup` as a step: auto-merge, Dependabot security updates, Actions sharing and PR
    creation, and the CI ruleset on main of the product repo, when the person running init administers it.
    Anyone else (a deployment of someone else's product) skips it."""
    ui, pin = ctx.ui, ctx.repo().pin
    token = productsetup.token(ctx.deps.credentials_env or os.environ, ctx.deps.run, None)
    if not token:
        ui.warn("no GitHub token from `gh auth token`: skipped (run `mcctl product-setup` later)")
        return {"product": pin.repository, "applied": False}
    try:
        p = productsetup.plan(token, pin.repository, ctx.deps.transport)
    except productsetup.SetupError as e:
        ui.ok(f"{pin.repository}: skipped ({e})")
        return {"product": pin.repository, "applied": False}
    if ui.rich:
        ui.table(["", "Setting", "Now", "Wanted"], productsetup.plan_rows(p), title=pin.repository, indent=2)
    else:
        for line in productsetup.format_plan(p):
            ui.info(line)
    if not p.changes:
        ui.ok(f"{pin.repository}: settings in sync")
        return {"product": pin.repository, "applied": True}
    if not ui.confirm(
        "product_setup_apply", f"Apply these {len(p.changes)} change(s) to {pin.repository}?", default=True
    ):
        ui.warn(f"{pin.repository}: left as is (`mcctl product-setup` applies it later)")
        return {"product": pin.repository, "applied": False}
    for name in _guarded(lambda: productsetup.apply(p)):
        ui.ok(f"{pin.repository}: {name} changed")
    return {"product": pin.repository, "applied": True}


# --- step 6: Discord ----------------------------------------------------------------------------------


def step_discord(ctx: Ctx) -> dict[str, Any]:
    from mcctl import integrations

    out = _discord_alerts(ctx)
    return out | integrations.setup_commands(ctx, ctx.env())  # P14: slash commands


def step_discord_endpoint(ctx: Ctx) -> dict[str, Any]:
    from mcctl import integrations

    return integrations.step_endpoint(ctx)


def _apply_inputs(ctx: Ctx) -> dict[str, Any]:
    from mcctl import integrations

    return integrations.apply_inputs(ctx)


def _discord_endpoint_inputs(ctx: Ctx) -> dict[str, Any]:
    from mcctl import integrations

    return integrations.endpoint_inputs(ctx)


def _discord_alerts(ctx: Ctx) -> dict[str, Any]:
    ui, cfg = ctx.ui, ctx.repo().server
    env = ctx.env()
    if not cfg.alerts.discord:
        if env.get("TF_VAR_discord_webhook_url"):
            ui.warn("TF_VAR_discord_webhook_url is set but alerts.discord is false (unused)")
        ui.ok("Discord alerts off (alerts.discord: false)")
        return {"enabled": False}
    hook = env.get("TF_VAR_discord_webhook_url")
    fresh = hook is None
    if fresh:
        if ui.rich:
            ui.todo(
                [
                    "In Discord: the channel's settings > Integrations > Webhooks > New Webhook.",
                    "Copy Webhook URL, then paste it below (input hidden; it is a credential).",
                ]
            )
        else:
            ui.info("Discord: channel settings > Integrations > Webhooks > New Webhook > Copy Webhook URL.")
        hook = ui.secret("discord_webhook", "Paste the webhook URL", env_name="TF_VAR_discord_webhook_url")
    _credential(ctx, _guarded(lambda: discord.check_webhook(hook, ctx.deps.transport)), "Discord webhook")
    if fresh:
        ctx.save_env(TF_VAR_discord_webhook_url=hook)
    return {"enabled": True}


# --- step 7: Tailscale chicken-and-egg ----------------------------------------------------------------


def _plan_apply(ctx: Ctx, *, targets: Sequence[str] = (), extra: Mapping[str, str] | None = None) -> bool:
    repo = ctx.repo()
    try:
        terraform, out = ctx.deps.terraform(repo, ctx.product_root, extra or {})
        plan_file = tf.data_dir(repo.root) / tf.PLAN_FILE
        plan = terraform.plan(out / render.TFVARS_FILE, plan_file, targets=targets)
        if plan.empty:
            plan_file.unlink(missing_ok=True)
            ctx.ui.ok("Terraform: no changes")
            return True
        if not tf.confirm(plan, repo.server.name, ctx.ui.typed("confirm_apply")):
            plan_file.unlink(missing_ok=True)
            raise StepError(
                "not applied (the typed name didn't match)", "re-run and type the deployment name to apply"
            )
        try:
            terraform.apply(plan_file)
        except tf.TfError:
            in_state = terraform.state_list()
            rows = tf.apply_outcome(plan, in_state) if ctx.ui.rich else None
            if rows is not None:
                ctx.ui.table(["Result", "Resource"], rows, title="terraform apply stopped part-way", indent=2)
                ctx.ui.note(tf.failed_apply_report(plan, in_state)[-1])
            else:
                ctx.ui.bad("terraform apply failed part-way (its errors are above). What it got to:")
                for line in tf.failed_apply_report(plan, in_state):
                    ctx.ui.info("  " + line)
            raise
    except tf.TfError as e:
        where = "in the box" if ctx.ui.rich else "above"
        raise StepError(
            str(e), f"fix the errors {where}, then re-run `mcctl setup` (it resumes here)"
        ) from None
    ctx.ui.ok("Terraform applied")
    return True


def _confirm_revoked(ctx: Ctx, api_key: str | None, key_id: str | None, oauth: tuple[str, str]) -> None:
    """Make sure the one-off API access token is gone; checked through the API wherever possible."""
    ui, transport = ctx.ui, ctx.deps.transport

    def gone() -> bool | None:
        if api_key is not None:
            return not _guarded(lambda: tailscale.api_key_works(api_key, transport))
        if key_id is not None:
            exists = _guarded(lambda: tailscale.key_exists(*oauth, key_id, transport))
            return None if exists is None else not exists
        return None

    if api_key is not None and key_id is not None:
        if _guarded(lambda: tailscale.revoke_api_key(api_key, key_id, transport)):
            ui.ok(f"asked the API to revoke API token {key_id}")
    status = gone()
    if status:
        ui.ok("the one-off API access token is revoked (checked through the API)")
        return
    if ui.rich:
        ui.todo(
            [
                "Open https://login.tailscale.com/admin/settings/keys > the API access token > Revoke.",
                "Answer below: mcctl checks the revocation through the API where it can.",
            ],
            title="Your turn: (c) revoke the one-off token",
        )
    else:
        ui.info(
            "(c) Revoke the API access token now: https://login.tailscale.com/admin/settings/keys > the token > Revoke."
        )
    if not ui.confirm("api_key_revoked", "Revoked it?", default=False):
        raise StepError(
            "revoke the one-off Tailscale API token", "revoke it in the admin console, then re-run"
        )
    status = gone()
    if status is False:
        raise StepError(
            f"the API token {key_id or ''} still works: it isn't revoked yet",
            "revoke it in the admin console (Settings > Keys), then re-run",
        )
    if status is None:
        ui.warn("couldn't check the revocation through the API; relying on your answer")
    else:
        ui.ok("the one-off API access token is revoked (checked through the API)")


def step_tailscale(ctx: Ctx) -> dict[str, Any]:
    ui = ctx.ui
    env, transport = ctx.env(), ctx.deps.transport
    prev = ctx.outputs("tailscale")  # an earlier run that stopped part-way (outputs recorded below)
    cid, csecret = env.get("TAILSCALE_OAUTH_CLIENT_ID"), env.get("TAILSCALE_OAUTH_CLIENT_SECRET")
    old_client = None  # an OAuth client made with tags that can't create the keys (deleted once replaced)
    # oauth_tags is recorded before a new client is made; old_client = the one it replaces (still in .env if the
    # run stopped before the new one was saved).
    tags_ok = prev.get("oauth_tags") == list(tailscale.OAUTH_TAGS) and cid != prev.get("old_client")
    if cid and csecret and not tags_ok:
        # Clients made before the first live apply carry [tag:mc-server, tag:ci]: Tailscale then refuses both the
        # server key and the CI identity ("requested tags ... are invalid or not permitted").
        ui.warn(
            f"the OAuth client {cid} in .env was made with tags {prev.get('oauth_tags') or 'tag:mc-server, tag:ci'}:"
            f" Tailscale lets it create only keys with ALL its tags, so mcctl replaces it with one tagged"
            f" {', '.join(tailscale.OAUTH_TAGS)} (the policy makes that tag owner of tag:mc-server and tag:ci)"
        )
        old_client = cid
    elif cid and csecret:
        check = _guarded(lambda: tailscale.check_oauth(cid, csecret, transport))
        if check.level != "fail":
            _credential(ctx, check, "Tailscale OAuth client")
            if prev.get("api_key") == "unrevoked":  # stopped between the OAuth client and the revoke
                if prev.get("old_client"):
                    ui.warn(
                        f"delete the replaced OAuth client {prev['old_client']} in the admin console "
                        "(https://login.tailscale.com/admin/settings/trust-credentials > the client > Revoke)"
                    )
                _confirm_revoked(ctx, None, prev.get("api_key_id"), (cid, csecret))
            return {
                "acl_applied": True, "oauth": True, "api_key": "revoked",
                "oauth_tags": list(tailscale.OAUTH_TAGS),
            }  # fmt: skip
        ui.warn("the OAuth client in .env doesn't work: " + "; ".join(check.messages))
    if ui.rich:
        ui.todo(
            [
                "Open https://login.tailscale.com/admin/settings/keys > Generate API access token (1 day is "
                "enough).",
                "Paste it below (input hidden): it is used for this one apply and never saved.",
            ],
            intro="Tailscale chicken-and-egg: an OAuth client may only own tags that already exist in the "
            "tailnet policy, and the policy is what Terraform writes. So: (a) a one-off API access token applies "
            "the policy, (b) mcctl creates the OAuth client with it, (c) the API token is revoked.",
        )
    else:
        ui.info(
            "Tailscale chicken-and-egg: an OAuth client may only own tags that already exist in the tailnet policy,"
        )
        ui.info(
            "and the policy is what Terraform writes. So: (a) a one-off API access token applies the policy,"
        )
        ui.info("(b) mcctl creates the OAuth client with it, (c) the API token is revoked.")
        ui.info("(a) Admin console > Settings > Keys > Generate API access token (1 day is enough):")
        ui.info(
            "    https://login.tailscale.com/admin/settings/keys   (it is used for this one apply and never saved)"
        )
    api_key = ui.secret("tailscale_api_key", "Paste the API access token", env_name="TAILSCALE_API_KEY")
    _credential(ctx, _guarded(lambda: tailscale.check_api_key(api_key, transport)), "Tailscale API token")
    key_id = tailscale.api_key_id(api_key)
    # Recorded before anything uses the token, so an interrupted run still knows a token must be revoked.
    ctx.state.record(
        "tailscale", "failed",
        outputs={
            "api_key": "unrevoked", "api_key_id": key_id, "old_client": old_client,
            "oauth_tags": list(tailscale.OAUTH_TAGS),
        },
    )  # fmt: skip
    _plan_apply(ctx, targets=["tailscale_acl.this"], extra={"TAILSCALE_API_KEY": api_key})
    ctx.created.append("Tailscale policy file (tags tag:mc-terraform, tag:mc-server, tag:ci; SSH rules)")
    name = ctx.repo().server.name
    created = _guarded(lambda: tailscale.create_oauth_client(api_key, f"mcctl {name} terraform", transport))
    if created is not None:
        cid, csecret = created
        ui.ok(
            f"(b) created the OAuth client {cid} (scopes {OAUTH_SCOPES}; tag {', '.join(tailscale.OAUTH_TAGS)})"
        )
        ctx.created.append(f"Tailscale OAuth client {cid} (Terraform)")
    else:
        ui.warn("the API didn't create the OAuth client: create it in the admin console instead")
        ui.info(
            "(b) Admin console > Settings > Trust credentials > OAuth client (https://login.tailscale.com/admin/settings):"
        )
        ui.info(f"    scopes (write unless noted): {OAUTH_SCOPES}")
        ui.info(f"    tags: {', '.join(tailscale.OAUTH_TAGS)} (only this one)")
        cid = ui.secret("tailscale_oauth_client_id", "OAuth client ID", env_name="TAILSCALE_OAUTH_CLIENT_ID")
        csecret = ui.secret(
            "tailscale_oauth_client_secret", "OAuth client secret", env_name="TAILSCALE_OAUTH_CLIENT_SECRET"
        )
    _credential(
        ctx, _guarded(lambda: tailscale.check_oauth(cid, csecret, transport)), "Tailscale OAuth client"
    )
    ctx.save_env(TAILSCALE_OAUTH_CLIENT_ID=cid, TAILSCALE_OAUTH_CLIENT_SECRET=csecret)
    old_client = old_client or prev.get("old_client")
    if old_client and old_client != cid:
        if _guarded(lambda: tailscale.delete_key(api_key, old_client, transport)):
            ui.ok(f"deleted the old OAuth client {old_client}")
        else:
            ui.warn(
                f"couldn't delete the old OAuth client {old_client}: delete it in the admin console "
                "(https://login.tailscale.com/admin/settings/trust-credentials > the client > Revoke)"
            )
    _confirm_revoked(ctx, api_key, key_id, (cid, csecret))
    return {
        "acl_applied": True, "oauth": True, "api_key": "revoked", "api_key_id": key_id,
        "oauth_tags": list(tailscale.OAUTH_TAGS),
    }  # fmt: skip


# --- step 8: terraform apply --------------------------------------------------------------------------


def _record_app_slugs(ctx: Ctx) -> None:
    """server.yml github.updater_app / github.panel_app = the Apps' slugs (from the github step), committed and
    pushed: `server / gate` trusts lock/product and whitelist PRs only from those Apps' bot users, read from main
    (first live apply: an App can't be a ruleset bypass actor on a personal repo; mcctl/gate.py)."""
    gh = ctx.outputs("github")
    values = {
        f"github.{key}": slug
        for key, slug in (("updater_app", gh.get("updater_app")), ("panel_app", gh.get("panel_app")))
        if isinstance(slug, str) and slug and ctx.current(f"github.{key}") != slug
    }
    if values:
        ctx.set(values)
        ctx.commit("mcctl init: GitHub App slugs for the gate", [configrepo.SERVER_YML])


def _ci_oidc_subject(ctx: Ctx) -> dict[str, Any]:
    """Set server.yml github.oidc_subject to the `sub` format GitHub issues for the config repo (GitHub API), so
    the tailnet's CI identity (terraform/tailscale.tf) trusts exactly that subject. Returns the step outputs
    `ci_oidc_subject` (what `mcctl doctor` compares with GitHub later)."""
    ui, cfg = ctx.ui, ctx.repo().server
    repository = f"{cfg.github.owner}/{cfg.github.config_repo}"
    token = ctx.env().get("GITHUB_TOKEN")
    if not token:
        raise StepError("GITHUB_TOKEN isn't set (.env)", "re-run `mcctl setup --reconfigure github`")
    info = _guarded(lambda: github.oidc_subject_info(token, repository, ctx.deps.transport))
    if info.fmt is None:
        raise StepError(info.problem, "fix it, then re-run `mcctl setup` (it resumes here)")
    if info.fmt != cfg.github.oidc_subject:
        ctx.set({"github.oidc_subject": info.fmt})
        ctx.commit(f"mcctl init: github.oidc_subject {info.fmt}", [configrepo.SERVER_YML], push=False)
    subject = info.subject(environment=secrets.DEPLOY_ENVIRONMENT if cfg.github.environments else None)
    ui.ok(f"CI identity: GitHub's OIDC sub for {repository} is {info.fmt} ({info.reason}): {subject}")
    return {"ci_oidc_subject": subject}


def step_apply(ctx: Ctx) -> dict[str, Any]:
    oidc = _ci_oidc_subject(ctx)
    return {**_apply(ctx), **oidc}


def _apply(ctx: Ctx) -> dict[str, Any]:
    ui, repo = ctx.ui, ctx.repo()
    _record_app_slugs(ctx)
    if ui.rich:
        ui.note(
            "Terraform creates: the backup bucket + bucket lock + its scoped token, the tailnet's server key "
            f"and CI identity, generated passwords, and {repo.server.github.config_repo}'s Actions secrets. "
            "Review the plan:"
        )
    else:
        ui.info(
            "Terraform creates: the backup bucket + bucket lock + its scoped token, the tailnet's server key and CI"
        )
        ui.info(
            f"identity, generated passwords, and {repo.server.github.config_repo}'s Actions secrets. Review the plan:"
        )
    if repo.server.github.environments:
        terraform, _out = ctx.deps.terraform(repo, ctx.product_root, {})
        if tf.environments_migration(True, terraform.state_list()):
            ui.info(
                "github.environments: first the environments and their secrets (the repository-level "
                "secrets stay until the next plan), then the rest."
            )
            _plan_apply(ctx, targets=[tf.ENVIRONMENT_SECRETS])
    _plan_apply(ctx)
    if _restic_to_password_manager(ctx):
        ctx.created.append(
            "Terraform resources (backups bucket, tokens, tailnet key/identity, Actions secrets)"
        )
        return {"applied": True, "restic_password": "password manager"}
    from mcctl import integrations

    # (not `. .env`: a shell would export Dashlane references, not the values; mcctl resolves them)
    commands = [integrations.show_restic_command(repo.root)]
    if ui.rich:
        ui.todo(
            [
                "In another terminal, print it with the command below (setup itself never shows it).",
                "Store it in your password manager: without it the backups can't be read.",
            ],
            intro="The restic password exists only in Terraform state now.",
        )
    else:
        ui.info("The restic password exists only in Terraform state now. Store it in your password manager:")
    ui.code(commands)
    if not ui.confirm(
        "restic_password_stored", "Stored it? Without it the backups can't be read", default=False
    ):
        raise StepError("store the restic password first", "run the command above, store it, then re-run")
    ctx.created.append("Terraform resources (backups bucket, tokens, tailnet key/identity, Actions secrets)")
    return {"applied": True}


def _restic_to_password_manager(ctx: Ctx) -> bool:
    """Offer to save the restic password with the 1Password (`op`) or Bitwarden (`bw`) CLI. The password goes
    to the CLI on stdin only (never argv, never printed). True if it was saved."""
    deps, ui = ctx.deps, ctx.ui
    manager = None
    if deps.which("op") and deps.run(["op", "whoami"]).returncode == 0:
        manager = "op"
    elif deps.which("bw"):
        status = _json(deps.run(["bw", "status"])) or {}
        if status.get("status") == "unlocked":
            manager = "bw"
        elif status:
            ui.info(
                "Bitwarden CLI found but locked: `export BW_SESSION=$(bw unlock --raw)` lets mcctl save it."
            )
    if manager is None:
        if deps.dashlane.logged_in() is not None:  # P14: Dashlane (read-only CLI: shown once, read back)
            from mcctl import integrations

            password = integrations.restic_password(ctx)
            return bool(password) and integrations.save_restic(ctx, password)
        return False
    label = {"op": "1Password (op)", "bw": "Bitwarden (bw)"}[manager]
    cfg = ctx.repo().server
    if not ui.confirm(
        "restic_password_manager",
        f"Save the restic password to {label} as 'mcctl {cfg.name} restic'?",
        default=True,
    ):
        return False
    try:
        terraform, _out = deps.terraform(ctx.repo(), ctx.product_root, {})
        password = str(terraform.outputs().get("restic_password") or "")
    except tf.TfError as e:
        ui.warn(f"couldn't read the restic password from Terraform: {e}")
        return False
    if not password:
        ui.warn("Terraform has no restic_password output")
        return False
    title = f"mcctl {cfg.name} restic"
    note = f"restic repository password for {cfg.backups.bucket} (server {cfg.dns.hostname}.{cfg.dns.zone})"
    if manager == "op":
        # 1Password CLI: `op item create -` reads an item template (JSON) from stdin (op item create docs).
        item = {
            "title": title,
            "category": "PASSWORD",
            "fields": [
                {
                    "id": "password",
                    "type": "CONCEALED",
                    "purpose": "PASSWORD",
                    "label": "password",
                    "value": password,
                },
                {
                    "id": "notesPlain",
                    "type": "STRING",
                    "purpose": "NOTES",
                    "label": "notesPlain",
                    "value": note,
                },
            ],
        }
        proc = deps.run_input(["op", "item", "create", "-"], json.dumps(item))
    else:
        # Bitwarden CLI: `bw create item` reads base64-encoded item JSON from stdin (`bw encode` output).
        import base64

        item_bw = {
            "type": 1,
            "name": title,
            "notes": note,
            "login": {"username": "restic", "password": password},
        }
        encoded = base64.b64encode(json.dumps(item_bw).encode()).decode()
        proc = deps.run_input(["bw", "create", "item"], encoded)
    if proc.returncode != 0:
        ui.warn(f"{label} didn't save it (exit {proc.returncode}); store it yourself (below)")
        return False
    ui.ok(f"restic password saved to {label} as {title!r}")
    return True


# --- step 9: the host ---------------------------------------------------------------------------------

PROBE_SCRIPT = (
    "cat /etc/os-release; echo @@; uname -m; echo @@; grep MemTotal /proc/meminfo; echo @@; "
    + hostnet.COMMAND
)


def check_host(text: str, ipv6: str | None) -> list[str]:
    """Problems (with the fix) from PROBE_SCRIPT's output."""
    parts = text.split("@@\n")
    if len(parts) < 4:
        return ["unexpected output from the host probe"]
    rel = _os_release_text(parts[0])
    problems = []
    os_name = f"{rel.get('NAME', rel.get('ID', '?'))} {rel.get('VERSION_ID', '?')}"
    ok_os = (rel.get("ID"), rel.get("VERSION_ID")) in {
        ("debian", "12"),
        ("debian", "13"),
        ("ubuntu", "24.04"),
    }
    if not ok_os:
        problems.append(
            f"OS is {os_name}; supported: {', '.join(SUPPORTED_OSES)}. Reinstall with Debian 13 in the provider's panel"
        )
    arch = parts[1].strip()
    if arch not in ("x86_64", "aarch64"):
        problems.append(f"architecture {arch}; amd64 (x86_64) or arm64 (aarch64) only")
    m = re.search(r"MemTotal:\s+(\d+)\s*kB", parts[2])
    if not m or int(m.group(1)) < MIN_MEM_KB:
        problems.append("less than 4 GB of RAM: pick a bigger plan (8 GB recommended)")
    if ipv6:
        if ipaddress.IPv6Address(ipv6) not in hostnet.configured(parts[3]):
            problems.append(
                f"{ipv6} isn't configured on the host (`ip -j -6 addr show scope global`): add it to the network "
                "config (provider docs) or set host.ipv6 to an address that is"
            )
    return problems


def _os_release_text(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"')
    return out


def _host_key(ctx: Ctx, ip: str) -> None:
    deps, ui = ctx.deps, ctx.ui
    scan = deps.run(["ssh-keyscan", "-T", "10", ip])
    lines = [ln for ln in scan.stdout.splitlines() if ln and not ln.startswith("#")]
    if not lines:
        raise StepError(
            f"no SSH host key from {ip}:22", "is the host up and port 22 open (provider firewall)?"
        )
    known = deps.run(["ssh-keygen", "-F", ip, "-f", str(deps.known_hosts)])
    if known.returncode == 0 and known.stdout.strip():
        ui.ok(f"{ip} is already in {deps.known_hosts} (ssh checks it strictly)")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".pub", delete=False) as fh:
        fh.write("\n".join(lines) + "\n")
    try:
        fps = deps.run(["ssh-keygen", "-lf", fh.name])
    finally:
        Path(fh.name).unlink(missing_ok=True)
    ui.info(f"Host key fingerprints of {ip} (compare with the provider console / first-boot log):")
    for fp in fps.stdout.splitlines():
        ui.info(f"  {fp}")
    if not ui.confirm("host_key_matches", "Do they match?", default=False):
        raise StepError("host key not confirmed", "check you have the right IP, then re-run")
    deps.known_hosts.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(deps.known_hosts, "a") as fh:
        fh.write("\n".join(lines) + "\n")
    deps.known_hosts.chmod(0o600)
    ui.ok(f"added to {deps.known_hosts}")


def step_host(ctx: Ctx) -> dict[str, Any]:
    ui, cfg = ctx.ui, ctx.repo().server
    ipv4, ipv6 = cfg.host.ipv4, cfg.host.ipv6
    if ipv4 is None:
        if ui.rich:
            ui.todo(
                [
                    "Order a VPS (this costs money): a public IPv4 (+ IPv6 if you can), Debian 13/12 or Ubuntu "
                    "24.04, amd64/arm64, >= 4 GB RAM (8 GB recommended).",
                    "Install the image with your SSH public key for root (shown below).",
                    "Give mcctl its public IPv4 below, or press Enter to stop here and resume later.",
                ],
                intro="Recommended: dedicated cores and 8 GB (e.g. a netcup root server), IPv4 + IPv6, Debian 13; "
                "docs/setup.md 'Order the server' has the steps (cores vs VPS; provider firewall: off).",
            )
        else:
            ui.info(
                "Now the machine. Any VPS with a public IPv4 (+ IPv6 if you can), Debian 13/12 or Ubuntu 24.04,"
            )
            ui.info(
                "amd64/arm64, >= 4 GB RAM (8 GB recommended), and your SSH key for root added by the provider."
            )
            ui.info(
                "Recommended: dedicated cores and 8 GB (e.g. a netcup root server), IPv4 + IPv6, Debian 13;"
            )
            ui.info("docs/setup.md 'Order the server' has the steps (cores vs VPS; provider firewall: off).")
        _show_ssh_key(ctx)
        raw = ui.ask(
            "host_ipv4",
            "The host's public IPv4 (empty: stop here and resume with `mcctl init` later)",
            default="",
            check=lambda v: None if not v else (schema.field_errors("host.ipv4", v) or [None])[0],
        )
        if not raw:
            raise Paused("waiting_for_host", "order the VPS, then give init its IPv4")
        ipv4 = raw
        ctx.set({"host.ipv4": ipv4})
        ctx.commit("mcctl init: host IPv4", [configrepo.SERVER_YML])
    # IPv6: detected on the host over SSH below (`ip -j -6 addr show scope global`) on every run while host.ipv6
    # is unset (a SLAAC address can come up after the first probe: a live run recorded none); asked only if
    # that finds none, and not again once a run got that far (the host step's outputs hold `ipv6`).
    ask_v6 = "ipv6" not in ctx.outputs("host") or ctx.reconfigure("host")
    detect_v6 = ipv6 is None
    if ctx.state.is_done("bootstrap") and ctx.outputs("host").get("ipv4") == ipv4:
        return _host_after_bootstrap(ctx, cfg, ipv4, ipv6)
    user = cfg.host.ssh_bootstrap_user
    _host_key(ctx, ipv4)
    probe = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
        f"{user}@{ipv4}",
        "--",
        PROBE_SCRIPT,
    ]
    proc = ctx.deps.run(probe)
    denied = proc.returncode != 0 and "permission denied" in (proc.stderr or "").lower()
    if denied and (agent := ssh_agent_problem(ctx)):
        # mcctl's ssh runs non-interactively (BatchMode): a passphrase-protected key works only through
        # ssh-agent. ssh-copy-id wouldn't help (the key may well be installed already: a live run).
        raise StepError(f"ssh {user}@{ipv4}: Permission denied. {agent[0]}", agent[1])
    if denied:
        # The provider mailed a root password instead of installing the key: copy the key once with it.
        # ssh-copy-id asks for the password itself (mcctl never sees it).
        ui.info(f"{user}@{ipv4} doesn't accept your SSH key yet.")
        if (
            ui.confirm(
                "ssh_copy_id",
                f"Install your public key there with ssh-copy-id (asks for {user}'s password, e.g. from the provider's email)?",
                default=True,
            )
            and ctx.deps.stream(["ssh-copy-id", "-o", "StrictHostKeyChecking=yes", f"{user}@{ipv4}"]) == 0
        ):
            proc = ctx.deps.run(probe)
    if proc.returncode != 0:
        agent = ssh_agent_problem(ctx) if "permission denied" in (proc.stderr or "").lower() else None
        raise StepError(
            f"ssh {user}@{ipv4} failed: {(proc.stderr or '').strip().splitlines()[-1:] or proc.returncode}"
            + (f". {agent[0]}" if agent else ""),
            agent[1] if agent else f"add your SSH public key for {user} in the provider's panel, then re-run",
        )
    if detect_v6:
        ipv6 = _detect_ipv6(ctx, proc.stdout, ask=ask_v6)
        if ipv6 is not None and ipv6 not in global_ipv6(proc.stdout):  # typed in: is it configured now?
            proc = ctx.deps.run(probe)
    if problems := check_host(proc.stdout, ipv6):
        raise StepError("the host isn't suitable:\n  - " + "\n  - ".join(problems))
    ui.ok(f"{user}@{ipv4}: supported OS, architecture and RAM" + (f"; {ipv6} configured" if ipv6 else ""))
    return {"ipv4": ipv4, "ipv6": ipv6}


def global_ipv6(probe_output: str) -> list[str]:
    """The host's usable public IPv6 addresses (stable first) in PROBE_SCRIPT's `ip -j -6 addr show scope
    global` part (mcctl.hostnet: no Tailscale, temporary or deprecated ones)."""
    parts = probe_output.split("@@\n")
    return hostnet.public_ipv6(parts[3]) if len(parts) >= 4 else []


def ipv6_answer_error(value: str) -> str | None:
    """Why `value` isn't one full public IPv6 address of the host (None: it is; `none`: IPv4 only)."""
    v = value.strip()
    if v in ("", "none"):  # (an answers file's "" meant "detect it": it was, and found none)
        return None
    if "/" in v:
        return "the address only, without the prefix length (/64): e.g. 3ffe:ffff:6:1234::1"
    try:
        ip = ipaddress.IPv6Address(v)
    except ValueError:
        return "not an IPv6 address: paste the full address, e.g. 3ffe:ffff:6:1234::1"
    if not ip.is_global:
        return "not a public (global) IPv6 address: use the one from your provider's panel"
    if int(ip) & ((1 << 64) - 1) == 0:
        return "that's the /64 prefix itself, not an address in it: e.g. its ...::1"
    return (schema.field_errors("host.ipv6", str(ip)) or [None])[0]


def _detect_ipv6(ctx: Ctx, probe_output: str, *, ask: bool = True) -> str | None:
    """host.ipv6 from the host itself (`ip -j -6 addr show scope global`, PROBE_SCRIPT); asked only when the
    host has none configured, chosen when it has several (stable ones first)."""
    ui = ctx.ui
    found = global_ipv6(probe_output)
    if found:
        ipv6 = found[0] if len(found) == 1 else ui.choose("host_ipv6_pick", "Which IPv6 address?", found)
        ctx.set({"host.ipv6": ipv6})
        ctx.commit("mcctl init: host IPv6 (detected)", [configrepo.SERVER_YML])
        ui.ok(f"IPv6 {ipv6} detected on the host (host.ipv6)")
        return ipv6
    if not ask:  # answered before (IPv4 only)
        ui.info("no public IPv6 address on the host: IPv4 only (host.ipv6 unset)")
        return None
    ui.warn("no public IPv6 address is configured on the host (`ip -j -6 addr show scope global` found none)")
    ui.info(
        "If your provider gave the server an IPv6 (netcup: SCP > Network; a /64 such as 3ffe:ffff:6:1234::/64),"
    )
    ui.info("configure one address from it on the host (provider docs), then paste that full address below,")
    ui.info("e.g. 3ffe:ffff:6:1234::1, without /64. `none`: IPv4 only (add one later with `mcctl config set")
    ui.info("host.ipv6 ADDR` and re-run `mcctl setup`).")
    raw = ui.ask(
        "host_ipv6",
        "The host's full IPv6 address, without /64 (`none`: IPv4 only)",
        default="none",
        check=ipv6_answer_error,
    ).strip()
    if raw in ("", "none"):
        ui.ok("IPv4 only (host.ipv6 unset)")
        return None
    ipv6 = str(ipaddress.IPv6Address(raw))
    ctx.set({"host.ipv6": ipv6})
    ctx.commit("mcctl init: host IPv6", [configrepo.SERVER_YML])
    return ipv6


def ssh_agent_problem(ctx: Ctx) -> tuple[str, str] | None:
    """(cause, fix) when ssh can't use the key non-interactively: the key has a passphrase (`ssh-keygen -y -P ''`
    can't read it) and no ssh-agent holds it (no SSH_AUTH_SOCK, or `ssh-add -l` doesn't list it). mcctl's ssh
    runs with BatchMode, so it never asks for the passphrase."""
    key = ctx.deps.ssh_identity()
    if key is None:
        return None
    if ctx.deps.run(["ssh-keygen", "-y", "-P", "", "-f", str(key)]).returncode == 0:
        return None  # no passphrase: the agent doesn't matter
    try:
        shown = "~/" + str(key.relative_to(Path.home()))
    except ValueError:
        shown = str(key)
    fix = (
        f'in this same terminal run `eval "$(ssh-agent -s)" && ssh-add {shown}` (it asks for the passphrase '
        "once), then re-run `mcctl setup`"
    )
    if not ctx.deps.ssh_auth_sock():
        return f"Your key {shown} has a passphrase and no ssh-agent is running (SSH_AUTH_SOCK unset).", fix
    agent = ctx.deps.run(["ssh-add", "-l"])
    if agent.returncode == 0:
        fp = ctx.deps.run(["ssh-keygen", "-lf", f"{key}.pub"]).stdout.split()
        if len(fp) >= 2 and fp[1] in agent.stdout:
            return None  # loaded: something else denies the login
    return f"Your key {shown} has a passphrase and isn't loaded in ssh-agent (`ssh-add -l`).", fix


def _show_ssh_key(ctx: Ctx) -> None:
    """The public key to give the provider when installing the image (netcup SCP: the installation dialog)."""
    ui = ctx.ui
    key = ctx.deps.ssh_public_key()
    if key is None:
        ui.info("You have no SSH key yet (~/.ssh/id_ed25519.pub).")
        if ui.confirm(
            "create_ssh_key", "Create one now with ssh-keygen (it asks for a passphrase)?", default=True
        ):
            ctx.deps.stream(["ssh-keygen", "-t", "ed25519", "-f", str(Path.home() / ".ssh" / "id_ed25519")])
            key = ctx.deps.ssh_public_key()
    if key is not None:
        ui.info("Paste this public key into the provider's image installation (netcup SCP: Media > Images):")
        ui.info(f"  {key}")
        if ui.copy(key):
            ui.note("copied to your clipboard")


def _host_after_bootstrap(ctx: Ctx, cfg: schema.ServerConfig, ipv4: str, ipv6: str | None) -> dict[str, Any]:
    """The same machine, already bootstrapped (its public SSH is closed): host.ipv6 (or the tailnet name)
    changed. Probe it over the tailnet as the deploy user instead, then move the DNS records."""
    host = tailnet_target(ctx, cfg)
    ctx.ui.info(f"{ipv4} is bootstrapped (public SSH closed): checking it over the tailnet instead.")
    proc = ctx.deps.run(remote.ssh_argv(cfg, PROBE_SCRIPT, host=host))
    if proc.returncode != 0:
        raise StepError(
            f"ssh deploy@{host or cfg.host.tailscale_name} failed: "
            f"{(proc.stderr or '').strip().splitlines()[-1:] or proc.returncode}",
            f"is the server on the tailnet (`tailscale ping {cfg.host.tailscale_name}`)? Then re-run",
        )
    if ipv6 is None and global_ipv6(proc.stdout):  # host.ipv6 unset, but the host has one now
        ipv6 = _detect_ipv6(ctx, proc.stdout, ask=False)
    if problems := check_host(proc.stdout, ipv6):
        raise StepError("the host isn't suitable:\n  - " + "\n  - ".join(problems))
    ctx.ui.ok(f"{cfg.host.tailscale_name}: still suitable" + (f"; {ipv6} configured" if ipv6 else ""))
    ctx.ui.info("Updating the DNS records to match host.* (Terraform):")
    _plan_apply(ctx)
    return {"ipv4": ipv4, "ipv6": ipv6, "via": "tailnet"}


# --- step 10: bootstrap -------------------------------------------------------------------------------


def step_bootstrap(ctx: Ctx) -> dict[str, Any]:
    repo = ctx.repo()
    try:
        rc = ctx.deps.bootstrap(repo, ctx.product_root, ctx.ui.typed("confirm_apply"))
    except (deploy.DeployError, tf.TfError) as e:
        raise StepError(str(e)) from None
    if rc != 0:
        raise StepError("bootstrap didn't finish (see above)", "fix what it reports, then re-run")
    ctx.created.append(
        f"tailnet node {repo.server.host.tailscale_name} with Tailscale SSH; public SSH closed"
    )
    return {"bootstrapped": True}


# --- step 11: first deploy ----------------------------------------------------------------------------


def step_deploy(ctx: Ctx) -> dict[str, Any]:
    ui, cfg = ctx.ui, ctx.repo().server
    repository = f"{cfg.github.owner}/{cfg.github.config_repo}"
    if not cfg.deploy.enabled:
        ctx.set({"deploy.enabled": True})
    sha = ctx.commit("mcctl init: enable deploys", [configrepo.SERVER_YML])
    ui.info(f"Waiting for the deploy run of {sha[:12]} on {repository} ...")
    run_id = None
    with ui.spinner(f"waiting for GitHub to start the deploy run of {sha[:12]}"):
        run_id = _find_run(ctx, repository, sha)
    if run_id is None:
        raise StepError(
            "no workflow run started for the push",
            f"check https://github.com/{repository}/actions, then re-run",
        )
    if ui.rich:
        ui.link(f"https://github.com/{repository}/actions/runs/{run_id}", "deploy run:")
    rc = ctx.deps.stream(["gh", "run", "watch", str(run_id), "-R", repository, "--exit-status"])
    if rc != 0:
        if oidc := _tailnet_join_refused(ctx, repository, run_id):
            raise StepError(*oidc)
        raise StepError(
            f"the deploy run failed: https://github.com/{repository}/actions/runs/{run_id}",
            f"gh run view {run_id} -R {repository} --log-failed; fix, push, re-run",
        )
    ui.ok(f"deploy run {run_id} succeeded")
    return {"run_id": run_id, "sha": sha}


# The tailscale/github-action step's error when Tailscale refuses the OIDC token exchange (the federated
# identity's subject/claims don't match the token GitHub issued; the admin console says "Cannot validate
# subject").
_TAILNET_JOIN_STEP = "Join the tailnet"


def _tailnet_join_refused(ctx: Ctx, repository: str, run_id: Any) -> tuple[str, str] | None:  # noqa: ANN401
    """(cause, fix) when the deploy run failed because Tailscale refused the CI token exchange (HTTP 403)."""
    proc = ctx.gh("run", "view", str(run_id), "-R", repository, "--log-failed")
    log = proc.stdout if proc.returncode == 0 else ""
    lines = [ln for ln in log.splitlines() if _TAILNET_JOIN_STEP in ln]
    if not any("403" in ln for ln in lines):
        return None
    cfg = ctx.repo().server
    expected = ""
    if token := ctx.env().get("GITHUB_TOKEN"):
        try:
            info = github.oidc_subject_info(token, repository, ctx.deps.transport)
        except http.HttpError:
            info = None
        if info is not None and info.fmt is not None:
            env_name = secrets.DEPLOY_ENVIRONMENT if cfg.github.environments else None
            expected = (
                f" GitHub issues sub {info.subject(environment=env_name)!r} ({info.fmt}: {info.reason})."
            )
            if info.fmt != cfg.github.oidc_subject:
                ctx.set({"github.oidc_subject": info.fmt})
                ctx.commit(f"mcctl init: github.oidc_subject {info.fmt}", [configrepo.SERVER_YML], push=False)
                expected += f" server.yml github.oidc_subject is now {info.fmt}."
        elif info is not None:
            expected = f" {info.problem}."
    return (
        'Tailscale refused the CI token exchange (HTTP 403; the admin console shows "Cannot validate subject"): '
        "the tailnet's CI identity (tailscale_federated_identity.ci) doesn't match the OIDC `sub` claim of "
        f"{repository}'s token.{expected} Repositories created since 2026-07-15 get GitHub's immutable "
        "format repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:refs/heads/main.",
        "pin a product version that builds the subject from github.oidc_subject, then re-run `mcctl setup`: it "
        "plans and applies the identity's new subject (typed confirmation) and retries the deploy. "
        "`mcctl doctor` shows the subject GitHub issues (`ci oidc subject`)",
    )


def _find_run(ctx: Ctx, repository: str, sha: str) -> Any:  # noqa: ANN401 - the run's databaseId
    """The workflow run GitHub started for the push of `sha` (polled for up to 3 minutes), or None."""
    run_id = None
    for _ in range(36):
        runs = (
            _json(
                ctx.gh(
                    "run",
                    "list",
                    "-R",
                    repository,
                    "--workflow",
                    "server.yml",
                    "--branch",
                    "main",
                    "--limit",
                    "20",
                    "--json",
                    "databaseId,headSha,event",
                )
            )
            or []
        )
        found = [
            r for r in runs if r.get("headSha") == sha and r.get("event") in ("push", "workflow_dispatch")
        ]
        if found:
            run_id = found[0]["databaseId"]
            break
        ctx.deps.sleep(5)
    return run_id


# --- step 12: smoke test ------------------------------------------------------------------------------


def step_smoke(ctx: Ctx) -> dict[str, Any]:
    ui, cfg = ctx.ui, ctx.repo().server
    fqdn = f"{cfg.dns.hostname}.{cfg.dns.zone}"
    results: dict[str, str] = {}
    for family, label, want in (
        (socket.AF_INET, "IPv4", cfg.host.ipv4),
        (socket.AF_INET6, "IPv6", cfg.host.ipv6),
    ):
        if want is None:
            continue
        got = ctx.deps.resolve(fqdn, family)
        if want not in got:
            ui.warn(
                f"{fqdn} {label}: DNS says {sorted(got) or 'nothing'} (expected {want}; TTL {cfg.dns.ttl}s)"
            )
        last = ""
        for attempt in range(12):
            try:
                with ui.spinner(f"Minecraft status ping {label} {want}:25565 (try {attempt + 1}/12)"):
                    st = ctx.deps.ping(want, 25565, fqdn)
                version = (st.get("version") or {}).get("name", "?")
                players = st.get("players") or {}
                ui.ok(
                    f"{label} {want}: Minecraft {version}, {players.get('online', '?')}/{players.get('max', '?')} players"
                )
                results[label] = "up"
                break
            except (OSError, ValueError) as e:
                last = str(e)
                if ctx.deps.probe(want, 25565) == "no-route":
                    ui.warn(f"{label} {want}: unverifiable from here (no {label} route on this network)")
                    results[label] = "unverifiable"
                    break
                if attempt < 11:
                    with ui.spinner(f"{label} {want}: no answer yet ({last}); trying again"):
                        ctx.deps.sleep(15)
        else:
            raise StepError(
                f"{label} {want}:25565 doesn't answer a status ping ({last})",
                f"`mcctl logs -C {ctx.root}`; then re-run",
            )
    url = render.watchdog_url(cfg)
    with ui.spinner(f"waiting for the watchdog's first report ({url})"):
        for attempt in range(8):  # its cron runs every 5 minutes: the first run may still be ahead
            try:
                data = watchdog.status(url, ctx.deps.transport)
            except http.HttpError:
                data = None
            level, detail = watchdog.assess(data)
            if level != "fail":
                break
            if attempt < 7:
                ctx.deps.sleep(60)
    if level == "fail":
        raise StepError(
            f"watchdog {url}: {detail}",
            "check the Worker in the Cloudflare dashboard (Workers & Pages > its cron trigger), then re-run",
        )
    (ui.ok if level == "ok" else ui.warn)(f"watchdog {url}: {detail}")
    results["watchdog"] = level
    ui.info("The panel URL is checked here once the panel exists (P9).")
    return results


# --- step 13: first backup ----------------------------------------------------------------------------


def step_backup(ctx: Ctx) -> dict[str, Any]:
    ui, repo = ctx.ui, ctx.repo()
    cfg = repo.server
    host = tailnet_target(ctx, cfg)
    for what, command in (
        ("backup", remote.backup_now_command()),
        ("restore check", f"sudo mc-restic {MC_BIN_DIR}/restore.py --verify-into /var/tmp/restore-test"),
    ):
        ui.info(f"{what} on the server ...")
        with ui.spinner(f"{what} on the server"):
            proc = ctx.deps.run(remote.ssh_argv(cfg, command, host=host))
        # The verdict is the unit's result (backup) or the script's exit code (restore check); stderr is only
        # shown (first real setup: sudo's "unable to resolve host" warning there was taken for a failure).
        if what == "backup":
            ok, detail = remote.backup_outcome(proc)
        else:
            ok, detail = proc.returncode == 0, (proc.stderr or proc.stdout).strip()[-500:]
        if not ok:
            raise StepError(f"{what} failed: {detail}", f"`mcctl logs -C {ctx.root} mc-backup`; then re-run")
        if (proc.stderr or "").strip():
            ui.info(f"{what}: the server also said: {proc.stderr.strip()[-300:]}")
        ui.ok(f"{what} passed")
    if not cfg.world.expected:
        ctx.set({"world.expected": True})
        ctx.commit("mcctl init: the world is backed up (world.expected: true)", [configrepo.SERVER_YML])
    report = doctor.run(
        ctx.root,
        deps=doctor.Deps(
            transport=ctx.deps.transport,
            run=ctx.deps.run,
            which=ctx.deps.which,
            resolve=ctx.deps.resolve,
            now=ctx.deps.now,
            app_sign=ctx.deps.app_sign,
            plan=None,  # init applied just now (and the mock has no live state to plan against)
        ),
    )
    doctor.show(report, ui)
    if (ctx.root / configrepo.EXPIRY_YML).exists():
        ctx.commit("mcctl doctor: credential expiry dates", [configrepo.EXPIRY_YML])
    if report.failed:
        raise StepError("`mcctl doctor` reports failures (above)", "fix them, then re-run")
    return {"world_expected": True}


# --- the runner ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    fn: Callable[[Ctx], dict[str, Any]]
    always: bool = False  # re-run every time (cheap re-validation; no prompts when .env has the values)
    inputs: Callable[[Ctx], dict[str, Any]] = lambda _ctx: {}
    why: str = ""  # what the step is for, under its header (rich runs; the id and title never change: resume)


def _tailscale_inputs(_ctx: Ctx) -> dict[str, Any]:
    return {"oauth_tags": list(tailscale.OAUTH_TAGS)}


def _host_inputs(*fields: str) -> Callable[[Ctx], dict[str, Any]]:
    """Input hash of a host step: only the host.* fields that step depends on, so changing host.ipv6 after
    bootstrap re-runs the host check (over the tailnet), deploy and smoke test, not the bootstrap (whose
    public SSH is closed by then) or the first backup."""

    def inputs(ctx: Ctx) -> dict[str, Any]:
        host = ctx.repo().server.host
        return {f: getattr(host, f) for f in fields}

    return inputs


STEPS: tuple[Step, ...] = (
    Step(
        "preflight",
        "Prerequisites",
        step_preflight,
        always=True,
        why="The tools mcctl drives from this machine: git, gh, terraform, tailscale, ssh.",
    ),
    Step(
        "repo",
        "Config repo",
        step_repo,
        why="Your deployment's own private repository: server.yml, players.yml and the workflow that deploys on push.",
    ),
    Step(
        "questions",
        "Your server (server.yml)",
        step_questions,
        why="The few facts only you know; each answer is checked against the schema.",
    ),
    Step(
        "cloudflare",
        "Cloudflare admin token (account-owned, made by hand)",
        step_cloudflare,
        always=True,
        why="Terraform manages DNS, buckets, the watchdog and the panel's login in Cloudflare with this token.",
    ),
    Step(
        "zero-trust",
        "Cloudflare Zero Trust (panel login)",
        step_zero_trust,
        always=True,
        why="The admin panel's login (one-time PINs to your email) runs on Cloudflare Zero Trust.",
    ),
    Step(
        "state-backend",
        "Terraform state bucket and token",
        step_state_backend,
        why="Terraform's state (the only secret store) lives in a private R2 bucket with a bucket-only token.",
    ),
    Step(
        "github",
        "GitHub tokens",
        step_github,
        always=True,
        why="Terraform writes the config repo's Actions secrets and rulesets; two GitHub Apps open the panel's and the update pull requests.",
    ),
    Step(
        "product",
        "Product repo settings (owner only)",
        step_product,
        why="Only for the product's owner: auto-merge, rulesets and Actions sharing on the product repo.",
    ),
    Step(
        "discord",
        "Discord alerts and slash commands",
        step_discord,
        always=True,
        why="Where the server, the watchdog and CI report problems; optionally /status, /lag, /backup now ...",
    ),
    # inputs: a change of the OAuth client's tags (first live apply) re-runs the step on an existing setup.
    Step(
        "tailscale",
        "Tailscale policy and OAuth client",
        step_tailscale,
        inputs=_tailscale_inputs,
        why="The tailnet replaces public SSH: its policy, tags and the OAuth client Terraform uses.",
    ),
    Step(
        "apply",
        "Cloud resources (terraform apply)",
        step_apply,
        inputs=_apply_inputs,  # P14: enabling the slash commands later re-runs it (the DISCORD binding)
        why="Everything in the cloud at once, after you've seen the plan and typed the deployment name.",
    ),
    # P14: after the apply (the Worker has the DISCORD binding, so it can answer Discord's verification PING)
    Step(
        "discord-endpoint",
        "Discord commands endpoint",
        step_discord_endpoint,
        inputs=_discord_endpoint_inputs,
        why="Discord sends the slash commands to the watchdog Worker, which checks every signature.",
    ),
    Step(
        "host",
        "The server machine",
        step_host,
        inputs=_host_inputs("ipv4", "ipv6"),
        why="The VPS itself: checked over SSH (OS, architecture, RAM) before anything is installed.",
    ),
    Step(
        "bootstrap",
        "Bootstrap: Tailscale, deploy user, firewall",
        step_bootstrap,
        inputs=_host_inputs("ipv4", "tailscale_name"),
        why="The host joins the tailnet, gets the deploy user and a firewall; public SSH closes.",
    ),
    Step(
        "deploy",
        "First deploy (CI)",
        step_deploy,
        inputs=_host_inputs("ipv4", "ipv6", "tailscale_name"),
        why="From now on every push deploys: this push enables deploys and waits for the workflow.",
    ),
    Step(
        "smoke",
        "Smoke test (IPv4 + IPv6)",
        step_smoke,
        inputs=_host_inputs("ipv4", "ipv6", "tailscale_name"),
        why="Players' view: the server answers a status ping on its addresses; the watchdog reports in.",
    ),
    Step(
        "backup",
        "First backup, restore check, doctor",
        step_backup,
        inputs=_host_inputs("ipv4", "tailscale_name"),
        why="A first backup and a restore check prove the world can come back; doctor re-checks everything.",
    ),
)
STEP_IDS = tuple(s.id for s in STEPS)


def _from_index(value: str | None) -> int | None:
    if value is None:
        return None
    if value.isdigit() and 1 <= int(value) <= len(STEPS):  # the number shown: [1/17]..[17/17]
        return int(value) - 1
    if value in STEP_IDS:
        return STEP_IDS.index(value)
    raise ValueError(f"--from: one of 1-{len(STEPS)} or {', '.join(STEP_IDS)}")


# What only a person can do (docs/setup.md): shown in the checklist with the step it belongs to.
HUMAN_TASKS = (
    ("cloudflare", "create the account-owned Cloudflare admin token by hand (link and exact form printed)"),
    ("zero-trust", "sign up for Cloudflare Zero Trust (team name, Free plan) if the account hasn't"),
    (
        "github",
        "create the GitHub fine-grained PATs (links pre-filled) and click through the two GitHub Apps",
    ),
    ("discord", "if you want slash commands: create the Discord application (developer portal), copy ids"),
    ("tailscale", "generate the one-off Tailscale API access token"),
    ("apply", "approve each `terraform apply` (type the deployment name)"),
    ("host", "order the VPS (costs money) and give init its IPv4"),
)


def _checklist_state(ctx: Ctx) -> state.State | None:
    try:
        return state.load(ctx.root) if (ctx.root / state.STATE_FILE).exists() else None
    except state.StateError:
        return None


def show_checklist(ctx: Ctx) -> None:
    """The checklist: plain, the classic text (checklist()); rich, a table and the tasks still ahead."""
    ui = ctx.ui
    if not ui.rich:
        ui.say(checklist(ctx))
        return
    st = _checklist_state(ctx)
    rows = []
    for i, step in enumerate(STEPS):
        status = ((st.get(step.id) if st else None) or {}).get("status") or "pending"
        level = {"done": "ok", "waiting": "warn", "failed": "fail"}.get(status)
        rows.append([str(i + 1), step.title, f"{level}: {status}" if level else status])
    ui.say()
    ui.table(["#", "Step", "Status"], rows, title="Checklist", indent=2)
    todo = [text for sid, text in HUMAN_TASKS if not (st and st.is_done(sid))]
    if todo:
        ui.todo(todo, title="Still needs you")


def checklist(ctx: Ctx) -> str:
    """Every step with its state, and the human-only tasks still ahead."""
    st = _checklist_state(ctx)
    lines = ["", "Checklist:"]
    for i, step in enumerate(STEPS):
        rec = (st.get(step.id) if st else None) or {}
        status = rec.get("status") or "pending"
        mark = {"done": "x", "waiting": "~", "failed": "!"}.get(status, " ")
        lines.append(
            f"  [{mark}] {i + 1:>2} {step.title}" + ("" if status in ("done", "pending") else f" ({status})")
        )
    todo = [text for sid, text in HUMAN_TASKS if not (st and st.is_done(sid))]
    if todo:
        lines += ["", "Still needs you:"] + [f"  - {t}" for t in todo]
    return "\n".join(lines)


NEXT_STEPS = (
    ("mcctl status -C $C", "how the server is doing"),
    ("mcctl rcon -C $C whitelist add NAME", "let a friend in"),
    ("mcctl rcon -C $C chunky start", "pre-generate the world (docs/new-world.md)"),
    ("mcctl doctor -C $C", "re-check the tokens (commit expiry.yml); renew before they expire"),
)


def _summary_rows(ctx: Ctx) -> list[tuple[str, str]] | None:
    try:
        repo = configrepo.load(ctx.root)
    except configrepo.ConfigError:
        return None
    cfg = repo.server
    repository = f"{cfg.github.owner}/{cfg.github.config_repo}"
    rows = [
        ("config repo", f"https://github.com/{repository} (local: {ctx.root})"),
        (
            "server",
            f"{cfg.dns.hostname}.{cfg.dns.zone}:25565 (IPv4 {cfg.host.ipv4}"
            + (f", IPv6 {cfg.host.ipv6})" if cfg.host.ipv6 else ")"),
        ),
        ("tailnet", f"ssh deploy@{cfg.host.tailscale_name} (Tailscale SSH; public SSH closed)"),
        (
            "Terraform state",
            f"R2 bucket {cfg.state.bucket}; backups in {cfg.backups.bucket} (hourly, bucket lock)",
        ),
        ("credentials", f"{ctx.root / envfile.ENV_FILE} (0600, names only here); expiry dates in expiry.yml"),
    ]
    rows += [("created", c) for s in STEPS for c in ctx.outputs(s.id).get("created", [])]
    return rows


def summary(ctx: Ctx) -> str:
    rows = _summary_rows(ctx)
    if rows is None:
        return ""
    lines = ["", "Done. What exists now:"] + [f"  {k:<16} {v}" for k, v in rows]
    lines += ["", "Next steps:", f"  C={ctx.root}"] + [f"  {cmd:<36} {what}" for cmd, what in NEXT_STEPS]
    lines.append(
        "  Change settings: edit server.yml (or `mcctl config set PATH VALUE`), commit, push: CI deploys it."
    )
    return "\n".join(lines)


def show_summary(ctx: Ctx) -> None:
    ui = ctx.ui
    if not ui.rich:
        ui.say(summary(ctx))
        return
    rows = _summary_rows(ctx)
    if rows is None:
        return
    ui.say()
    ui.banner("Done", ["Your Minecraft server is set up and deploys on every push to main."])
    ui.kv(rows, title="What exists now")
    expiries = expiry_rows(ctx.root)
    if expiries:
        ui.table(["Credential", "Expires", "Days left"], expiries, title="Credential expiry", indent=2)
    ui.say()
    ui.table(
        ["Command", "What it does"],
        [[cmd.replace("$C", str(ctx.root)), what] for cmd, what in NEXT_STEPS]
        + [
            [
                "edit server.yml, commit, push",
                "change settings (or `mcctl config set PATH VALUE`): CI deploys",
            ]
        ],
        title="Next steps",
        indent=2,
    )


def expiry_rows(root: Path, today: datetime.date | None = None) -> list[list[str]]:
    """expiry.yml (written by doctor) as table rows: credential, date, days left with a level."""
    from mcctl import yamlio

    try:
        data = yamlio.load((root / configrepo.EXPIRY_YML).read_text())
    except (OSError, ValueError, yamlio.yaml.YAMLError):
        return []
    today = today or datetime.datetime.now(datetime.UTC).date()
    rows = []
    for name, value in sorted((data or {}).items() if isinstance(data, dict) else []):
        when = value.get("expires_on") if isinstance(value, dict) else value
        try:
            day = datetime.date.fromisoformat(str(when)[:10])
        except ValueError:
            rows.append([str(name), str(when or "never"), "info: -"])
            continue
        left = (day - today).days
        level = "fail" if left < 0 else "warn" if left <= 30 else "ok"
        rows.append([str(name), day.isoformat(), f"{level}: {left}"])
    return rows


def run(root: Path, ui: UI, deps: Deps, opts: Options, *, product_root: Path = schema.PRODUCT_ROOT) -> int:
    with ui_mod.using(ui):
        return _run(root, ui, deps, opts, product_root=product_root)


def _deployment_name(ctx: Ctx) -> str | None:
    try:
        value = ctx.current("name") if ctx.server_path.exists() else None
    except (OSError, yamledit.YamlEditError):
        return None
    return str(value) if value and value != "example" else None


def _run(root: Path, ui: UI, deps: Deps, opts: Options, *, product_root: Path) -> int:
    root = root.expanduser().resolve()
    ctx = Ctx(root, ui, deps, opts, product_root=product_root)
    try:
        start = _from_index(opts.from_step)
    except ValueError as e:
        ui.bad(str(e))
        return 2
    if unknown := [s for s in opts.reconfigure if s not in STEP_IDS]:
        ui.bad(f"--reconfigure {', '.join(unknown)}: one of {', '.join(STEP_IDS)}")
        return 2
    done_before = 0
    if (root / state.STATE_FILE).exists():
        try:
            done_before = sum(ctx.state.is_done(s.id) for s in STEPS)
        except state.StateError as e:
            ui.bad(str(e))
            return 1
        ui.banner(
            "mcctl setup" + (f" · {name}" if (name := _deployment_name(ctx)) else ""),
            [f"Resuming in {root}: {done_before}/{len(STEPS)} steps done before."],
        )
    else:
        ui.banner(
            "mcctl setup",
            [
                f"mcctl init: set up a Minecraft server in {len(STEPS)} steps. Config repo: {root}",
                "Safe to stop at any time (Ctrl-C) and re-run: finished steps are skipped.",
            ],
        )
    total = len(STEPS)
    for i, step in enumerate(STEPS):
        forced = (start is not None and i >= start) or step.id in opts.reconfigure
        done_now = 0
        if ui.rich and (root / state.STATE_FILE).exists():
            try:
                done_now = sum(ctx.state.is_done(s.id) for s in STEPS)
            except state.StateError:
                done_now = 0
        ui.header(i + 1, total, step.title, done=done_now, name=_deployment_name(ctx))  # 1-based
        inputs: dict[str, Any] = {}
        try:
            inputs = step.inputs(ctx) if (root / configrepo.SERVER_YML).exists() and i >= 3 else {}
            if (
                not forced
                and not step.always
                and (root / state.STATE_FILE).exists()
                and ctx.state.is_done(step.id, inputs)
            ):
                ui.ok("done earlier (skipped)")
                continue
            ui.why(step.why)
            before = len(ctx.created)
            outputs = step.fn(ctx)
            if ctx.created[before:]:
                outputs = {**outputs, "created": ctx.created[before:]}
            if (
                i >= 3
            ):  # the step may have changed its own inputs (e.g. host.ipv4): record what it ran with now
                inputs = step.inputs(ctx)
        except Paused as p:
            ctx.state.record(step.id, "waiting", outputs={"reason": str(p)})
            then = p.resume or "when the host exists"
            if ui.rich:
                ui.say()
                ui.todo(
                    [then[:1].upper() + then[1:], f"Then run: mcctl setup -C {root}"],
                    title=f"Paused at {step.id} ({p})",
                )
            else:
                ui.say(f"\nPaused at {step.id} ({p}). Next: {then}; then run: mcctl setup -C {root}")
            show_checklist(ctx)
            return 0
        except (
            StepError,
            Abort,
            configrepo.ConfigError,
            yamledit.YamlEditError,
            envfile.EnvFileError,
            ProviderError,
            http.HttpError,
            OSError,
        ) as e:
            cause = e.cause if isinstance(e, StepError) else str(e)
            fix = e.fix if isinstance(e, StepError | ProviderError) else ""
            if root.exists():
                try:
                    # keep what the step recorded part-way (e.g. a Tailscale API token still to revoke)
                    ctx.state.record(step.id, "failed", inputs=inputs, outputs=ctx.outputs(step.id))
                except (OSError, state.StateError):
                    pass
            ui.fail(cause, fix, f"re-run: mcctl init -C {root}" + (" (it resumes here)" if i else ""))
            show_checklist(ctx)
            return 1
        ctx.state.record(step.id, "done", inputs=inputs, outputs=outputs)
    show_summary(ctx)
    show_checklist(ctx)
    return 0
