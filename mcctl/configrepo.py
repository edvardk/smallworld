"""The config repo (design §1): what mcctl reads from it, the product pin, and the template it starts from.

A config repo holds values only; the product holds all code. Layout (relative to the config repo root):

    server.yml                      required: the deployment (mcctl/schema.py)
    server.lock.yml                 required: pinned server build (generated)
    plugins.lock.yml                required: pinned plugins (generated)
    client.lock.yml                 v1.1, required with client.enabled: the pinned client modpack (generated)
    .github/workflows/server.yml    required: ONE `uses:` call of the product's reusable workflow, pinned by
                                    commit SHA. That line is the single source of the product version.
    players.yml                     optional: whitelist + ops, canonical form
    expiry.yml                      optional: credential expiry dates written by `mcctl doctor` (P4)

`load()` validates all of it and returns the parsed pieces; `materialize()` writes a new config repo from
`template/` (used by the tests now and by `mcctl init` in P5).
"""

from __future__ import annotations

import datetime
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from mcctl import schema, yamlio

TEMPLATE_DIR = schema.PRODUCT_ROOT / "template"
EXAMPLE_DIR = schema.PRODUCT_ROOT / "examples" / "config-repo"
# The product's own GitHub repository. A config repo names it in its pinned `uses:` line, which wins;
# this is only the default for `materialize()` and for a server.yml rendered outside a config repo.
PRODUCT_REPOSITORY = "edvardk/smallworld"
REUSABLE_WORKFLOW = ".github/workflows/server.yml"  # same path in the product and in every config repo

SERVER_YML = "server.yml"
PLAYERS_YML = "players.yml"
SERVER_LOCK = "server.lock.yml"
PLUGINS_LOCK = "plugins.lock.yml"
CLIENT_LOCK = "client.lock.yml"  # = mcctl.clientpack.LOCK_FILE
EXPIRY_YML = "expiry.yml"
WORKFLOW = REUSABLE_WORKFLOW

# Template placeholders, replaced by materialize() (names, not credentials).
TEMPLATE_TOKENS = ("__PRODUCT_REPOSITORY__", "__PRODUCT_SHA__", "__PRODUCT_VERSION__")

SHA = re.compile(r"[0-9a-f]{40}")
VERSION = re.compile(r"v\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?")
_USES = re.compile(r"^\s*(?:-\s+)?uses:\s*(\S+)(?:\s+#\s*(.*?))?\s*$", re.M)
_TARGET = re.compile(r"([A-Za-z0-9-]+/[A-Za-z0-9._-]+)/" + re.escape(REUSABLE_WORKFLOW) + r"@(\S+)")
CREDENTIAL_NAME = r"^[a-z0-9][a-z0-9_-]{0,63}$"
# What a valid pin line looks like, for error messages (configrepo, gate): the comment starts with the release
# (or, for an untagged commit, `vX.Y.Z-N-g<sha>`), and any note follows after a space.
PIN_EXAMPLE = f"`uses: <owner>/<repo>/{REUSABLE_WORKFLOW}@<40-hex sha> # v1.1.0`"
PIN_NOTE_EXAMPLE = "`# v1.1.0 (upload fix)`"


class ConfigError(ValueError):
    """Every problem found in a config repo, one `file: message` string each."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


@dataclass(frozen=True)
class Pin:
    """The product version a config repo runs: `uses: <repository>/.github/workflows/server.yml@<sha>`."""

    repository: str
    sha: str
    version: str | None  # from the trailing `# vX.Y.Z` comment, informational


@dataclass(frozen=True)
class ConfigRepo:
    root: Path
    server: schema.ServerConfig
    players: schema.Players
    server_lock: dict[str, Any]
    plugins_lock: dict[str, Any]
    expiry: list[dict[str, Any]]
    pin: Pin
    client_lock: dict[str, Any] | None = None  # v1.1: client.lock.yml (None: not there)


# --- the pin ------------------------------------------------------------------------------------------


def read_pin(text: str) -> tuple[Pin | None, list[str]]:
    """Parse the config repo's workflow: exactly one `uses:`, the product's reusable workflow, by SHA."""
    calls = list(_USES.finditer(text))
    if len(calls) != 1:
        return None, [
            f"must make exactly one `uses:` call (found {len(calls)}): "
            f"<owner>/<repo>/{REUSABLE_WORKFLOW}@<40-char commit sha>"
        ]
    target, comment = calls[0].group(1), calls[0].group(2)
    m = _TARGET.fullmatch(target)
    if m is None:
        return None, [f"`uses: {target}` isn't <owner>/<repo>/{REUSABLE_WORKFLOW}@<sha>"]
    repository, ref = m.group(1), m.group(2)
    if not SHA.fullmatch(ref):
        return None, [f"`@{ref}`: pin the product by its full 40-character commit SHA, not a tag or branch"]
    version = None
    if comment:
        version = comment.split()[0]
        if not VERSION.fullmatch(version):
            return None, [pin_comment_error(comment)]
    return Pin(repository, ref, version), []


def pin_comment_error(comment: str) -> str:
    """Why `# comment` isn't a valid pin comment, with an example of a valid line (no file prefix)."""
    first = comment.split()[0] if comment.split() else ""
    if (m := re.match(r"v\d+\.\d+\.\d+", first)) and first != m.group(0):
        why = f"; `{m.group(0)}` must be followed by a space, not `{first[m.end() :]}`"
    elif re.fullmatch(r"\d+\.\d+\.\d+.*", first):
        why = "; the release needs its `v`"
    else:
        why = ""
    return (
        f"the pin's comment must start with the release, e.g. {PIN_EXAMPLE} (found `# {comment}`{why}); "
        f"put any note after it: {PIN_NOTE_EXAMPLE}"
    )


UPDATE_CRON = re.compile(r"""^\s*-\s*cron:\s*["']17 4 \* \* ([0-6])["']""", re.M)
WEEKDAYS = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}


def check_update_cron(workflow: str, update_day: str) -> list[str]:
    """P11: the weekly update slot is the caller's `17 4 * * N` schedule (UTC); N = automation.update_day."""
    days = UPDATE_CRON.findall(workflow)
    want = WEEKDAYS.get(update_day[:3].title())
    if len(days) != 1:
        return [f'needs exactly one weekly update schedule `cron: "17 4 * * {want}"` (automation.update_day)']
    if int(days[0]) != want:
        return [
            f"the update schedule runs on day {days[0]}, automation.update_day is {update_day}: "
            f'use "17 4 * * {want}"'
        ]
    return []


# --- lock files and expiry.yml ------------------------------------------------------------------------


def check_locks(cfg: schema.ServerConfig, server_lock: Any, plugins_lock: Any) -> list[str]:  # noqa: ANN401
    """The lock files must pin what server.yml asks for (minecraft.version, flavour, plugins)."""
    errors: list[str] = []
    if not isinstance(server_lock, dict) or not isinstance(server_lock.get("server"), dict):
        return [f"{SERVER_LOCK}: expected a mapping with `server:` (regenerate it with the lock tool)"]
    if not isinstance(plugins_lock, dict) or not isinstance(plugins_lock.get("plugins"), list):
        return [f"{PLUGINS_LOCK}: expected a mapping with `plugins:` (regenerate it with the lock tool)"]
    server = server_lock["server"]
    if str(server_lock.get("minecraft_version")) != cfg.minecraft.version:
        errors.append(
            f"{SERVER_LOCK}: pins Minecraft {server_lock.get('minecraft_version')!r} but server.yml "
            f"minecraft.version is {cfg.minecraft.version!r}: run the lock tool for the new version"
        )
    if server.get("project") != cfg.minecraft.flavour:
        errors.append(
            f"{SERVER_LOCK}: pins {server.get('project')!r} "
            f"but minecraft.flavour is {cfg.minecraft.flavour!r}"
        )
    if not isinstance(server_lock.get("java_major"), int):
        errors.append(f"{SERVER_LOCK}: java_major must be an integer")
    entries: list[tuple[str, Any]] = [(f"{SERVER_LOCK}: server", server)]
    locked = []
    for i, p in enumerate(plugins_lock["plugins"]):
        if not isinstance(p, dict):
            errors.append(f"{PLUGINS_LOCK}: plugins[{i}]: expected a mapping")
            continue
        entries.append((f"{PLUGINS_LOCK}: plugins[{i}]", p))
        locked.append((p.get("name"), p.get("source"), p.get("project")))
    for where, e in entries:
        if not re.fullmatch(r"[0-9a-f]{64}", str(e.get("sha256", ""))):
            errors.append(f"{where}: sha256 must be 64 hex characters")
        if not str(e.get("url", "")).startswith("https://"):
            errors.append(f"{where}: url must be https://")
        if not re.fullmatch(r"[A-Za-z0-9_.+-]+\.jar", str(e.get("filename", ""))):
            errors.append(f"{where}: filename must be a plain *.jar name")
    wanted = sorted((p.name, p.source, p.project) for p in cfg.plugins)
    if sorted(locked, key=str) != sorted(wanted, key=str):
        errors.append(
            f"{PLUGINS_LOCK}: pins {sorted(locked, key=str)} but server.yml plugins are {wanted}: "
            "run the lock tool"
        )
    return errors


def _date(v: Any) -> bool:  # noqa: ANN401
    if isinstance(v, datetime.date) and not isinstance(v, datetime.datetime):
        return True
    if isinstance(v, str):
        try:
            datetime.date.fromisoformat(v)
        except ValueError:
            return False
        return re.fullmatch(r"\d{4}-\d{2}-\d{2}", v) is not None
    return False


def check_expiry(data: Any) -> tuple[list[dict[str, Any]], list[str]]:  # noqa: ANN401
    """expiry.yml: `credentials: [{name, expires_on: date|null, checked_at: date}]`. Names and dates only."""
    if (
        not isinstance(data, dict)
        or set(data) != {"credentials"}
        or not isinstance(data["credentials"], list)
    ):
        return [], [f"{EXPIRY_YML}: expected exactly `credentials: [...]`"]
    errors: list[str] = []
    names = []
    for i, c in enumerate(data["credentials"]):
        where = f"{EXPIRY_YML}: credentials[{i}]"
        if not isinstance(c, dict) or set(c) != {"name", "expires_on", "checked_at"}:
            errors.append(f"{where}: expected exactly name, expires_on, checked_at")
            continue
        if not isinstance(c["name"], str) or not re.fullmatch(CREDENTIAL_NAME, c["name"]):
            errors.append(f"{where}.name: must match {CREDENTIAL_NAME}")
        names.append(c["name"])
        if c["expires_on"] is not None and not _date(c["expires_on"]):
            errors.append(f"{where}.expires_on: a date (YYYY-MM-DD) or null (never expires)")
        if not _date(c["checked_at"]):
            errors.append(f"{where}.checked_at: a date (YYYY-MM-DD)")
    if len(set(map(str, names))) != len(names):
        errors.append(f"{EXPIRY_YML}: duplicate credential names")
    return data["credentials"], errors


# --- loading ------------------------------------------------------------------------------------------


def git_origin(directory: Path) -> tuple[str, str] | None:
    """(owner, repo) of the directory's GitHub `origin` remote, if any."""
    try:
        url = subprocess.run(
            ["git", "-C", str(directory), "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return schema.parse_github_remote(url)


def _read_yaml(path: Path, errors: list[str], name: str) -> Any:  # noqa: ANN401
    try:
        return yamlio.load(path.read_text())
    except FileNotFoundError:
        errors.append(f"{name}: missing")
    except yaml.YAMLError as e:
        errors.append(f"{name}: invalid YAML: {e}")
    return None


def load_server_file(path: Path, *, i_know: bool = False) -> schema.ServerConfig:
    """Load one server.yml (github.* derived from the directory's `origin` remote when not set)."""
    errors: list[str] = []
    data = _read_yaml(path, errors, path.name)
    if errors:
        raise ConfigError(errors)
    github = data.get("github") if isinstance(data, dict) else None
    needs_git = not isinstance(github, dict) or not {"owner", "config_repo"} <= set(github)
    origin = git_origin(path.resolve().parent) if needs_git else None
    try:
        return schema.load_server(data, git_origin=origin, i_know=i_know)
    except schema.SchemaError as e:
        raise ConfigError([f"{path.name}: {err}" for err in e.errors]) from None


def players_errors(text: str) -> tuple[schema.Players | None, list[str]]:
    """Schema rules, then canonical form (design §2)."""
    try:
        players = schema.load_players(yamlio.load(text))
    except yaml.YAMLError as e:
        return None, [f"invalid YAML: {e}"]
    except schema.SchemaError as e:
        return None, e.errors
    canonical = schema.dump_players(players)
    if text != canonical:
        return None, ["not in canonical form (sorted, one flow mapping per entry); expected:\n" + canonical]
    return players, []


def load(root: Path, *, i_know: bool = False) -> ConfigRepo:
    """Validate a whole config repo; raises ConfigError listing every problem found."""
    root = root.resolve()
    errors: list[str] = []
    try:
        server = load_server_file(root / SERVER_YML, i_know=i_know)
    except ConfigError as e:
        errors += e.errors
        server = None

    players = schema.Players()
    if (root / PLAYERS_YML).is_file():
        parsed, perr = players_errors((root / PLAYERS_YML).read_text())
        errors += [f"{PLAYERS_YML}: {e}" for e in perr]
        players = parsed or players

    server_lock = _read_yaml(root / SERVER_LOCK, errors, SERVER_LOCK)
    plugins_lock = _read_yaml(root / PLUGINS_LOCK, errors, PLUGINS_LOCK)
    if server is not None and server_lock is not None and plugins_lock is not None:
        errors += check_locks(server, server_lock, plugins_lock)

    client_lock = None
    if (root / CLIENT_LOCK).is_file():
        client_lock = _read_yaml(root / CLIENT_LOCK, errors, CLIENT_LOCK)
    if server is not None and server.client.enabled:
        from mcctl import clientpack

        if client_lock is None and not (root / CLIENT_LOCK).is_file():
            fix = "`mcctl config set client.enabled true` writes it (or `mcctl lock update --client`)"
            errors.append(f"{CLIENT_LOCK}: missing (client.enabled needs it: {fix})")
        elif client_lock is not None:
            errors += clientpack.lock_errors(client_lock, server.minecraft.version)

    expiry: list[dict[str, Any]] = []
    if (root / EXPIRY_YML).is_file():
        data = _read_yaml(root / EXPIRY_YML, errors, EXPIRY_YML)
        if data is not None:
            expiry, eerr = check_expiry(data)
            errors += eerr

    pin = None
    try:
        workflow = (root / WORKFLOW).read_text()
    except FileNotFoundError:
        errors.append(f"{WORKFLOW}: missing (the pinned call of the product's reusable workflow)")
    else:
        pin, perr = read_pin(workflow)
        errors += [f"{WORKFLOW}: {e}" for e in perr]
        if server is not None:
            errors += [f"{WORKFLOW}: {e}" for e in check_update_cron(workflow, server.automation.update_day)]

    if errors or server is None or pin is None:
        raise ConfigError(errors)
    return ConfigRepo(root, server, players, server_lock, plugins_lock, expiry, pin, client_lock)


# --- the template -------------------------------------------------------------------------------------


def template_files() -> list[Path]:
    """Files of template/, relative to it (sorted)."""
    return sorted(
        p.relative_to(TEMPLATE_DIR)
        for p in TEMPLATE_DIR.rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    )


def materialize(
    dest: Path, *, sha: str, version: str, repository: str = PRODUCT_REPOSITORY, overwrite: bool = False
) -> list[Path]:
    """Write a config repo from template/ into dest, pinned to the product commit `sha` (`version` comment).

    Refuses a non-empty dest unless overwrite is set (then only template files are rewritten).
    """
    if not SHA.fullmatch(sha):
        raise ValueError(f"not a 40-character commit SHA: {sha!r}")
    if not VERSION.fullmatch(version):
        raise ValueError(f"not a version (vX.Y.Z): {version!r}")
    if dest.exists() and any(dest.iterdir()) and not overwrite:
        raise FileExistsError(f"{dest} isn't empty")
    written = []
    for rel in template_files():
        text = (TEMPLATE_DIR / rel).read_text()
        for token, value in zip(TEMPLATE_TOKENS, (repository, sha, version), strict=True):
            text = text.replace(token, value)
        out = dest / rel
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text)
        written.append(out)
    return written
