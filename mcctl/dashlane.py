"""Dashlane as a secret source (P14): `.env` values may be Dashlane secret references, read with the Dashlane
CLI.

A value `dl://<id or title>[/<field>][?<transformer>]` in `.env` (or in the shell) is replaced, in mcctl's own
memory, by what `dcli read <reference>` prints. Syntax and command from Dashlane's CLI docs (cli.dashlane.com,
"Read secrets references" and "Load secrets into environment variables", checked 2026-11-02): the identifier
is the item's id (without braces) or its title (the first match wins), the field is optional (without it the
whole item is JSON), `dcli exec` replaces every environment variable that starts with `dl://`, and the CLI
only reads (Dashlane: access through the CLI "is read only", for now): mcctl can read references but never
create items, so the person creates them (`mcctl secrets dashlane` lists which) and mcctl only checks they
resolve.

Where values go: the resolved value replaces the reference in the environment mapping mcctl builds
(`envfile.merged`), which reaches Terraform/Ansible only as a child process environment. The reference is on
dcli's argv (it names an item; it isn't a secret), the value only on dcli's stdout (a pipe). Neither is ever
printed, logged or written to `.mcctl/state.json`; an error names the `.env` key and the reference, never a
value.

`dcli` asks for the master password itself when its keychain copy is off, and unlocks with biometrics on
macOS if configured. Where that prompt goes is [U] (Dashlane's docs don't say; it may be stdout, which mcctl
captures for `dcli read`: an invisible prompt and a hang). So before its first read in a process mcctl checks
the CLI is set up (`dcli accounts whoami`, else: "run `dcli sync` once") and, on a terminal, runs `dcli sync`
with the terminal as its stdin/stdout/stderr (any prompt is visible there and unlocks Dashlane for the reads
that follow); off a terminal every dcli call gets a closed stdin, so a prompt fails instead of waiting.
`mcctl doctor` checks every reference.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from mcctl.envfile import EnvFileError

PREFIX = "dl://"
REF = re.compile(r"dl://[^\s/?]+(?:/[^\s?]+)?(?:\?\S+)?")
DCLI = "dcli"
# References that stay references in the environment: checked by `mcctl doctor`, never handed to a child
# process (the restic password's Dashlane copy: Terraform state holds the live value, nothing here needs it).
RESTIC_REF_KEY = "MCCTL_RESTIC_REF"
NOT_RESOLVED = frozenset({RESTIC_REF_KEY})
INSTALL = "https://cli.dashlane.com/install"

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


def _interactive() -> bool:
    """A person at a terminal (stdin and stderr): dcli may prompt there."""
    try:
        return sys.stdin.isatty() and sys.stderr.isatty()
    except (AttributeError, ValueError):
        return False


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """dcli with stdout captured (the value). Off a terminal stdin is closed (a prompt fails, never hangs)."""
    stdin = None if _interactive() else subprocess.DEVNULL
    try:
        return subprocess.run(
            list(argv), stdout=subprocess.PIPE, stdin=stdin, text=True, check=False, timeout=180
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def _passthrough(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """dcli on the terminal (stdin, stdout, stderr inherited): its prompts and messages are visible."""
    try:
        proc = subprocess.run(list(argv), check=False, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))
    return subprocess.CompletedProcess(list(argv), proc.returncode, "", "")


def _quiet(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """dcli for a status question (never prompts: stdin closed, output captured)."""
    try:
        return subprocess.run(
            list(argv), capture_output=True, stdin=subprocess.DEVNULL, text=True, check=False, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


class DashlaneError(EnvFileError):
    """A reference didn't resolve. The message names the key and the reference, never a value. (An
    EnvFileError,
    so every command that reports a bad .env reports this too.)"""


def is_ref(value: str) -> bool:
    return value.startswith(PREFIX)


@dataclass
class Resolver:
    """Reads references through dcli, each once per process (cache in memory only)."""

    run: Runner = _run
    status: Runner = _quiet
    which: Callable[[str], str | None] = shutil.which
    terminal: Runner = _passthrough  # `dcli sync` on a terminal (a master password prompt is visible)
    interactive: Callable[[], bool] = _interactive
    _cache: dict[str, str] = field(default_factory=dict, repr=False)
    _ready: bool = field(default=False, repr=False)

    def sync(self) -> bool:
        """`dcli sync` (new items reach the CLI; unlocks Dashlane). On a terminal it runs there, so a master
        password prompt is visible; off one quietly with stdin closed (it fails instead of waiting)."""
        proc = (self.terminal if self.interactive() else self.status)([DCLI, "sync"])
        self._ready = True
        return proc.returncode == 0

    def _prepare(self) -> None:
        """Before the first `dcli read`: the CLI is set up, and on a terminal unlocked by a visible sync (a
        prompt inside a captured `dcli read` would be invisible)."""
        if self._ready:
            return
        if self.logged_in() is None:
            raise DashlaneError(
                "the Dashlane CLI (dcli) isn't logged in: run `dcli sync` once (email, code, master "
                "password), then retry"
            )
        self.sync()  # a failed sync (offline) still leaves the local copy: the reads say what's missing

    def available(self) -> bool:
        return self.which(DCLI) is not None

    def logged_in(self) -> str | None:
        """The Dashlane login dcli is set up for, or None (not installed, or no account yet).

        `dcli accounts whoami` [V: Dashlane CLI command list, via third-party docs; not on cli.dashlane.com]
        prints the account's email; it unlocks nothing."""
        if not self.available():
            return None
        proc = self.status([DCLI, "accounts", "whoami"])
        out = (proc.stdout or "").strip().splitlines()
        return out[0].strip() if proc.returncode == 0 and out and "@" in out[0] else None

    def read(self, ref: str, key: str = "") -> str:
        if not REF.fullmatch(ref):
            raise DashlaneError(
                f"{key or 'value'}: {ref!r} isn't a Dashlane reference (dl://<id or title>/<field>)"
            )
        if ref in self._cache:
            return self._cache[ref]
        if not self.available():
            raise DashlaneError(
                f"{key or ref} is a Dashlane reference but the Dashlane CLI (dcli) isn't installed: {INSTALL}"
            )
        self._prepare()
        proc = self.run([DCLI, "read", ref])
        value = proc.stdout or ""
        # one trailing line break is the CLI's, not the secret's (a value can't contain one: .env is one line)
        value = value[:-2] if value.endswith("\r\n") else value[:-1] if value.endswith("\n") else value
        if proc.returncode != 0 or not value or "\n" in value:
            why = "no such item or field" if proc.returncode != 0 else "empty or multi-line value"
            raise DashlaneError(
                f"{key or 'value'}: `dcli read {ref}` failed ({why}; exit {proc.returncode}). "
                "Check the item's title/id and field in Dashlane, `dcli sync`, then retry"
            )
        self._cache[ref] = value
        return value

    def resolve(self, env: Mapping[str, str]) -> dict[str, str]:
        """`env` with every `dl://` value replaced by the secret (like `dcli exec`)."""
        return {k: (self.read(v, k) if is_ref(v) and k not in NOT_RESOLVED else v) for k, v in env.items()}

    def resolve_lenient(self, env: Mapping[str, str]) -> dict[str, str]:
        """Like resolve(), but a reference that doesn't resolve is left out (doctor reports it on its own)."""
        out = {}
        for k, v in env.items():
            if is_ref(v) and k not in NOT_RESOLVED:
                try:
                    out[k] = self.read(v, k)
                except DashlaneError:
                    continue
            else:
                out[k] = v
        return out

    def check(self, env: Mapping[str, str]) -> list[tuple[str, str, str | None]]:
        """(key, reference, problem or None) for each reference in `env` (doctor). Values are dropped."""
        out = []
        for k, v in sorted(env.items()):
            if is_ref(v):
                try:
                    self.read(v, k)
                    out.append((k, v, None))
                except DashlaneError as e:
                    out.append((k, v, str(e)))
        return out


DEFAULT = Resolver()


# --- which items to create (`mcctl secrets dashlane`) -----------------------------------------------------


@dataclass(frozen=True)
class Item:
    key: str  # the .env variable
    what: str  # what it is (shown)
    title: str  # the Dashlane item title mcctl suggests

    def ref(self) -> str:
        # a Password item's secret is its `password` field [U: field name per Dashlane's item JSON (`dcli p -o
        # json` shows "password"); the docs show only <field>]; `mcctl doctor` proves each reference resolves
        return f"{PREFIX}{self.title}/password"


# The laptop's admin credentials (design §3; mcctl init writes them to .env). AWS_* are the Terraform state
# token.
CREDENTIALS: tuple[tuple[str, str], ...] = (
    ("CLOUDFLARE_API_TOKEN", "Cloudflare admin token"),
    ("AWS_ACCESS_KEY_ID", "Terraform state token: key id"),
    ("AWS_SECRET_ACCESS_KEY", "Terraform state token: secret"),
    ("GITHUB_TOKEN", "GitHub admin PAT"),
    ("TF_VAR_product_read_token", "GitHub product-read PAT"),
    ("TAILSCALE_OAUTH_CLIENT_ID", "Tailscale OAuth client id"),
    ("TAILSCALE_OAUTH_CLIENT_SECRET", "Tailscale OAuth client secret"),
    ("TF_VAR_discord_webhook_url", "Discord alerts webhook"),
    ("DISCORD_BOT_TOKEN", "Discord bot token (slash commands setup)"),
    ("TF_VAR_panel_github_client_secret", "GitHub OAuth client secret (panel idp github)"),
    ("TF_VAR_panel_github_app_private_key", "panel GitHub App private key"),
    ("TF_VAR_updater_github_app_private_key", "updater GitHub App private key"),
)


def items(deployment: str, present: Mapping[str, str]) -> list[Item]:
    """One suggested item per credential this .env has (by key; values are never looked at here)."""
    return [
        Item(key, what, f"mcctl-{deployment}-{key.lower().replace('_', '-')}")
        for key, what in CREDENTIALS
        if key in present
    ]


def restic_title(deployment: str) -> str:
    return f"mcctl-{deployment}-restic"


def restic_ref(deployment: str) -> str:
    # a Secure Note's text [U: field name `content` per the note's JSON (`dcli note -o json`); the docs show
    # only
    # <field>]: setup reads it back once and compares, `mcctl doctor` checks it still resolves
    return f"{PREFIX}{restic_title(deployment)}/content"
