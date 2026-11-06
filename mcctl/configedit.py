"""`mcctl config get|set PATH [VALUE]` (design §4 command surface; moved here from P4): one server.yml value.

`get` prints the effective value (defaults and derived values included) as JSON. `set` parses VALUE as YAML
(`10`, `true`, `null`, `"text"`, `[a, b]`), checks it against the field's schema rules, edits server.yml in
place with `mcctl.yamledit` (comments and layout kept), validates the whole config repo (cross-field rules,
lock files, ...), restores the old file if anything fails, and commits server.yml (push to deploy).

v1.2: `set client.enabled true` without a client.lock.yml writes the first one (the pinned product's
scripts/client_pack.py, which resolves client/pack.yml on Modrinth) and commits it with server.yml; v1.1
refused, and `mcctl lock update` never created one while the client was off.
"""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from mcctl import configrepo, schema, yamledit, yamlio


class ConfigEditError(RuntimeError):
    pass


# (config root, Minecraft version) -> the lock tool's result; writes root/client.lock.yml
LockWriter = Callable[[Path, str], subprocess.CompletedProcess[str]]

APPLY_NOTE = (
    "client.enabled also needs `mcctl apply` once: the Worker's client bindings and the setup page's host "
    "come from Terraform (a deploy alone leaves the page answering \"the client page isn't set up on this "
    'server yet")'
)


def client_lock_writer(product_root: Path) -> LockWriter:
    """The product's `scripts/client_pack.py lock` for a config repo (its progress lines go to stdout)."""

    def write(root: Path, minecraft_version: str) -> subprocess.CompletedProcess[str]:
        argv = [
            sys.executable,
            str(product_root / "scripts" / "client_pack.py"),
            "lock",
            "--lock-dir",
            str(root),
            "--minecraft-version",
            minecraft_version,
        ]
        return subprocess.run(argv, stderr=subprocess.PIPE, text=True, check=False)

    return write


def _load(root: Path, i_know: bool) -> configrepo.ConfigRepo:
    try:
        return configrepo.load(root, i_know=i_know)
    except configrepo.ConfigError as e:
        raise ConfigEditError("the config repo doesn't validate:\n  " + "\n  ".join(e.errors)) from None


def get(root: Path, path: str, *, i_know: bool = False) -> str:
    value: Any = dataclasses.asdict(_load(root, i_know).server)
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise ConfigEditError(f"{path}: no such setting")
        value = value[part]
    return value if isinstance(value, str) else json.dumps(value)


def parse_value(raw: str) -> Any:  # noqa: ANN401 - any YAML value
    """VALUE as YAML (numbers, true/false, null, [lists], {maps}, "quoted"); anything that reads as a plain
    string is taken verbatim, so `Hello # world` keeps its `#` instead of becoming a YAML comment."""
    text = raw.strip()
    try:
        parsed = yamlio.load(text) if text else ""
    except yaml.YAMLError:
        return text
    if isinstance(parsed, str) and not text.startswith(("'", '"')):
        return text
    return parsed


def set_and_commit(
    root: Path,
    path: str,
    raw: str,
    *,
    commit: bool = True,
    i_know: bool = False,
    lock_writer: LockWriter | None = None,
) -> str:
    root = root.resolve()
    before = _load(root, i_know)  # start from a valid repo
    value = parse_value(raw)
    if errors := schema.field_errors(path, value):
        raise ConfigEditError("; ".join(errors))
    server = root / configrepo.SERVER_YML
    old = server.read_text()
    if yamledit.get_value(old, path) == value:
        return f"{path} is already {json.dumps(value)}: nothing to do"
    try:
        new = yamledit.set_value(old, path, value)
    except yamledit.YamlEditError as e:
        raise ConfigEditError(str(e)) from None
    enables_client = path == "client.enabled" and value is True
    lock = root / configrepo.CLIENT_LOCK
    new_lock = enables_client and lock_writer is not None and not lock.is_file()
    server.write_text(new)

    def undo() -> None:
        server.write_text(old)
        if new_lock:
            lock.unlink(missing_ok=True)

    if new_lock:
        assert lock_writer is not None
        version = before.server.minecraft.version
        print(f"client.lock.yml is missing: resolving the client pack for Minecraft {version} (Modrinth) ...")
        proc = lock_writer(root, version)
        if proc.returncode != 0 or not lock.is_file():
            undo()
            raise ConfigEditError(
                "not changed: the client lock tool failed: " + (proc.stderr or "").strip()[-500:]
            )
    try:
        configrepo.load(root, i_know=i_know)
    except configrepo.ConfigError as e:
        undo()
        hint = ""
        if path == "minecraft.version":
            hint = (
                "\n  (a Minecraft version change also needs new lock files: "
                "`mcctl upgrade --minecraft-version`, docs/reference.md 'Bumping versions')"
            )
        elif enables_client and not lock.is_file():
            hint = "\n  (create the client lock first: `mcctl lock update --client`)"
        raise ConfigEditError(
            "not changed, the result doesn't validate:\n  " + "\n  ".join(e.errors) + hint
        ) from None
    files = [configrepo.SERVER_YML, *([configrepo.CLIENT_LOCK] if new_lock else [])]
    msg = f"server.yml: {path} = {json.dumps(value)}" + (" (+ a new client.lock.yml)" if new_lock else "")
    note = f"\n{APPLY_NOTE}" if enables_client and before.server.client.enabled is not True else ""
    if not commit:
        return msg + " (not committed)" + note
    for argv in (
        ["git", "-C", str(root), "add", "--", *files],
        ["git", "-C", str(root), "commit", "-q", "-m", f"mcctl config set {path}", "--", *files],
    ):
        proc = subprocess.run(argv, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise ConfigEditError(f"edited, but `{' '.join(argv[3:5])}` failed: {proc.stderr.strip()}")
    return msg + " (committed; `git push` deploys it)" + note
