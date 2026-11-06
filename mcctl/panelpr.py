"""`mcctl panel-pr-check`: may the config repo's workflow auto-merge a panel pull request? (design §5
"Whitelist → Git" (3), P10; v1.2 world library.)

mc-agent records the panel's whitelist changes (already live on the server) as a pull request from a branch
`panel/<ts>`. The reusable workflow merges it with GITHUB_TOKEN (so no deploy runs: the change is live
already) only if this check passes: the diff between the base and the head touches players.yml and nothing
else, both versions are valid and canonical, the ops are unchanged, and the whitelist did change. Anything
else (an ops change, a second file, a hand edit that isn't canonical) stays an ordinary pull request.

v1.2: the world library's pull request (a switch, create or delete, already live on the server) touches
server.yml alone and in it only `world.active` and `world.library` (compared parsed: every other value is
equal on both sides), and the head validates.
"""

from __future__ import annotations

import copy
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from mcctl import configrepo, schema

Runner = Callable[..., subprocess.CompletedProcess[str]]


def _git(root: Path, run: Runner, *args: str) -> str:
    proc = run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ValueError(f"git {' '.join(args[:2])} failed: {proc.stderr.strip()[:200]}")
    return proc.stdout


def check(root: Path, base: str, head: str = "HEAD", run: Runner = subprocess.run) -> list[str]:
    """Reasons not to auto-merge (empty: merge)."""
    try:
        changed = [
            f
            for f in _git(root, run, "diff", "--no-renames", "--name-only", f"{base}...{head}").splitlines()
            if f
        ]
    except ValueError as e:
        return [str(e)]
    if changed == [configrepo.SERVER_YML]:
        return check_world(root, base, head, run)
    if changed != [configrepo.PLAYERS_YML]:
        return [
            f"the diff must touch only {configrepo.PLAYERS_YML} (or only server.yml's world library); it "
            f"touches {', '.join(changed) or 'nothing'}"
        ]
    versions: dict[str, schema.Players] = {}
    errors: list[str] = []
    for label, ref in (("base", base), ("head", head)):
        try:
            text = _git(root, run, "show", f"{ref}:{configrepo.PLAYERS_YML}")
        except ValueError as e:
            errors.append(f"{label}: {e}")
            continue
        players, problems = configrepo.players_errors(text)
        errors += [f"{label} {configrepo.PLAYERS_YML}: {p}" for p in problems]
        if players is not None:
            versions[label] = players
    if errors:
        return errors
    old, new = versions["base"], versions["head"]
    if old.ops != new.ops:
        return ["the ops changed: operators are changed by a person, not by the panel"]
    if old.whitelist == new.whitelist:
        return ["nothing changed in the whitelist"]
    return []


WORLD_KEYS = ("active", "library")


def _without_world_library(data: Any) -> Any:  # noqa: ANN401 - parsed YAML
    out = copy.deepcopy(data)
    world = out.get("world") if isinstance(out, dict) else None
    if isinstance(world, dict):
        for key in WORLD_KEYS:
            world.pop(key, None)
        if not world:
            out.pop("world")
    return out


def check_world(root: Path, base: str, head: str = "HEAD", run: Runner = subprocess.run) -> list[str]:
    """v1.2: server.yml changes only world.active / world.library, and the head is a valid server.yml."""
    docs: dict[str, Any] = {}
    for label, ref in (("base", base), ("head", head)):
        try:
            docs[label] = yaml.safe_load(_git(root, run, "show", f"{ref}:{configrepo.SERVER_YML}"))
        except (ValueError, yaml.YAMLError) as e:
            return [f"{label} {configrepo.SERVER_YML}: {str(e)[:200]}"]
    old, new = docs["base"], docs["head"]
    if not isinstance(old, dict) or not isinstance(new, dict):
        return [f"{configrepo.SERVER_YML} must be a mapping on both sides"]
    if _without_world_library(old) != _without_world_library(new):
        return [f"{configrepo.SERVER_YML}: more than world.active and world.library changed: needs the admin"]
    world = lambda d: {k: (d.get("world") or {}).get(k) for k in WORLD_KEYS}  # noqa: E731
    if world(old) == world(new):
        return ["nothing changed in the world library"]
    try:
        schema.load_server(new, git_origin=("owner", "config"), i_know=True)
    except schema.SchemaError as e:
        return [f"head {configrepo.SERVER_YML}: {err}" for err in e.errors[:10]]
    return []
