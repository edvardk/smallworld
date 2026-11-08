#!/usr/bin/env python3
"""The world library (v1.2): several worlds per server, only the active one on the box. Stdlib only.

The active world always lives in the server dir under the fixed level-name (server.yml minecraft.level_name,
e.g. world/), so backups, restores, borders and pre-generation work as before. Since Minecraft 26.x that one
folder holds every dimension (<level>/dimensions/minecraft/<dimension>/); worlds archived before 26.x have
Bukkit's split folders (world/, world_nether/, world_the_end/) and still switch in: see LAYOUT.
Every other world is a restic snapshot of the server dir in the existing R2 repository, tagged
`world-archive`, `world:<name>` and `dv:<DataVersion>`. `backup.py`'s `forget` only ever selects snapshots
tagged `auto`, so archives are never forgotten by the retention policy; only `world_delete` forgets them
(and only once the bucket lock allows: older than KEEP_WITHIN_DAYS, the rest later, see forget_due).

Each world folder has its own player data (players/data, players/advancements, players/stats since 26.x;
playerdata/, advancements/, stats/ before), so inventories, ender chests and XP are per world. The whitelist,
ops and bans are files of the server dir and are shared by every world.

    worlds.py --panel   # mc-world.service (as the ops user, restic credentials): the request the agent wrote
    worlds.py --apply      # mc-world-apply.service (as minecraft, no credentials): swap the world folders

Switch (and create, which switches to a new world): the agent's job has already warned the players
(countdown). Then, holding restore.lock (backups, self-heal and deploys wait): mark the server admin-stopped
(by=world), stop it, archive the current world (the final backup and the safety snapshot in one: it is
verified in the snapshot list before anything else happens), fetch the target archive into the staging dir
and check its level.dat DataVersion (never newer than the running server's), then mc-world-apply.service
replaces the world folders with the staged ones (or removes them: the server generates the new world from
the seed and type written into server.properties), put the marker back, start, verify (RCON answers, the
seed matches, level.dat exists) and set the world's border. A failure before the apply leaves the old world
untouched and starts it again; a failure while applying leaves the server stopped with the archive's id in
the admin-stopped marker.

Every line starting with STEP/WORLD/FAILED is read by mc-agent from the unit's journal for the panel.
"""

from __future__ import annotations

import argparse
import contextlib
import grp
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import mc_rcon
import notify

SERVER_DIR = mc_rcon.SERVER_DIR
STATE_DIR = notify.STATE_DIR  # /var/lib/minecraft: the ops user writes, everyone reads
REQUEST_FILE = STATE_DIR / "world-request.json"  # written by mc-agent, read by mc-world.service
WORLDS_FILE = notify.SNAPSHOTS_FILE.with_name("worlds.json")  # /var/lib/mc-snapshots: archives + active
LIBRARY_FILE = Path("/opt/minecraft/worlds-library.json")  # = mc_worlds_library_file (deploy: Git + pending)
STAGING_ROOT = Path("/var/lib/mc-restore")  # = mc_restore_dir (shared with restore.py, never at once)
STAGING = STAGING_ROOT / "world-staging"
MANIFEST = STAGING_ROOT / "world-manifest.json"
UNIT = "minecraft.service"
APPLY_UNIT = "mc-world-apply.service"
PANEL_UNIT = "mc-world.service"
SERVER_GROUP = "minecraft"

WORLD_NAME = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}")  # = mcctl.schema WORLD_NAME (restic tag world:<name>)
SEED = re.compile(r"(-?[0-9]{1,19})?")  # = mcctl.schema SEED ("" = random)
ARCHIVE_TAG = "world-archive"
KEEP_ARCHIVES = 2  # newest archive snapshots kept per world; older ones are forgotten (after the bucket lock)
STORAGE_NOTE_BYTES = 10 * 10**9  # R2's free storage: the panel shows a note above this
# LAYOUT. Since 26.x every dimension lives in the one world folder (level-name), level.dat and players/
# at its top: <level>/dimensions/<namespace>/<dimension>/region (vanilla LevelResource; seen in a world
# written by the pinned Purpur 26.3, which writes no <level>_nether/ or <level>_the_end/). Before 26.x
# Paper/Purpur kept a folder per Bukkit world: <level>/region, <level>_nether/DIM-1/region,
# <level>_the_end/DIM1/region. At start-up Paper's WorldFolderMigration moves such a world into the new
# layout (after a 30-second warning) and deletes the old folders; a file both layouts hold stops the start
# ("already exists"), and region files only the old layout holds would be merged in. The Bukkit world names
# (RCON, Chunky) are still <level>, <level>_nether and <level>_the_end. A switch therefore removes all three
# folders from the server dir, copies a 26.x archive's <level>/ alone (split folders without region files
# are left out and named) and a pre-26 archive's three folders (Paper migrates them), and refuses an archive
# with region files in both layouts.
LEGACY_SUFFIXES = ("_nether", "_the_end")  # pre-26 Bukkit world folders next to <level>/

# World types -> server.properties (keys checked against server-config/keys/<version>/server.properties.txt
# by tests/test_worlds.py). level-type values are vanilla's world preset ids (data/minecraft/worldgen/
# world_preset); generator-settings is read only for minecraft:flat (FlatLevelGeneratorSettings: layers,
# biome, features, lakes). A dedicated server can't choose the biome of single_biome_surface: it is plains.
_FLAT = {"features": False, "lakes": False}
TYPES: dict[str, dict[str, str]] = {
    "default": {"level-type": "minecraft:normal", "generator-settings": "{}", "generate-structures": "true"},
    "large_biomes": {
        "level-type": "minecraft:large_biomes",
        "generator-settings": "{}",
        "generate-structures": "true",
    },
    "amplified": {
        "level-type": "minecraft:amplified",
        "generator-settings": "{}",
        "generate-structures": "true",
    },
    "single_biome": {
        "level-type": "minecraft:single_biome_surface",
        "generator-settings": "{}",
        "generate-structures": "true",
    },
    "flat_classic": {
        "level-type": "minecraft:flat",
        "generator-settings": json.dumps(
            {
                "layers": [
                    {"block": "minecraft:bedrock", "height": 1},
                    {"block": "minecraft:dirt", "height": 2},
                    {"block": "minecraft:grass_block", "height": 1},
                ],
                "biome": "minecraft:plains",
                **_FLAT,
            },
            separators=(",", ":"),
        ),
        "generate-structures": "true",
    },
    "flat_void": {
        "level-type": "minecraft:flat",
        "generator-settings": json.dumps(
            {"layers": [{"block": "minecraft:air", "height": 1}], "biome": "minecraft:the_void", **_FLAT},
            separators=(",", ":"),
        ),
        "generate-structures": "false",
    },
    "flat_redstone": {
        "level-type": "minecraft:flat",
        "generator-settings": json.dumps(
            {
                "layers": [
                    {"block": "minecraft:bedrock", "height": 1},
                    {"block": "minecraft:stone", "height": 3},
                    {"block": "minecraft:sandstone", "height": 116},
                ],
                "biome": "minecraft:desert",
                **_FLAT,
            },
            separators=(",", ":"),
        ),
        "generate-structures": "false",
    },
}

# Server presets -> server.properties. Only keys that exist in 26.3's server.properties (pvp, spawn-monsters
# and spawn-animals are game rules there, not properties): difficulty, gamemode (the default for new
# players), force-gamemode (everyone gets it at join) and hardcore (read when the world is created).
PRESETS: dict[str, dict[str, str]] = {
    "survival_smp": {
        "difficulty": "normal",
        "gamemode": "survival",
        "force-gamemode": "false",
        "hardcore": "false",
    },
    "creative_build": {
        "difficulty": "peaceful",
        "gamemode": "creative",
        "force-gamemode": "true",
        "hardcore": "false",
    },
    "hardcore": {"difficulty": "hard", "gamemode": "survival", "force-gamemode": "false", "hardcore": "true"},
    "farm_test": {
        "difficulty": "normal",
        "gamemode": "creative",
        "force-gamemode": "true",
        "hardcore": "false",
    },
    "peaceful": {
        "difficulty": "peaceful",
        "gamemode": "survival",
        "force-gamemode": "false",
        "hardcore": "false",
    },
}
DIMENSIONS = ("minecraft:overworld", "minecraft:the_nether", "minecraft:the_end")
MANAGED_KEYS = sorted(
    {k for t in TYPES.values() for k in t} | {k for p in PRESETS.values() for k in p} | {"level-seed"}
)


class WorldError(Exception):
    """A world request that can't run; the message reaches the panel."""


# --- library entries ----------------------------------------------------------------------------------------


def check_entry(raw: object) -> dict[str, Any]:
    """A library entry (server.yml world.library item, as the deploy and the agent pass it) or WorldError.
    {name, seed, type, preset (None = leave server.properties as is), border_radius ({dimension id: radius
    or None} or None = server.yml world.border_radius), pregen (bool or None = monitoring's default)}."""
    if not isinstance(raw, dict):
        raise WorldError("a world entry must be an object")
    name, seed = raw.get("name"), raw.get("seed", "")
    if not isinstance(name, str) or not WORLD_NAME.fullmatch(name):
        raise WorldError(
            "a world name is 1-32 lowercase letters, digits, _ or - (starting with a letter or digit)"
        )
    if not isinstance(seed, str) or not SEED.fullmatch(seed):
        raise WorldError("the seed must be a whole number (or empty for a random one)")
    wtype, preset = raw.get("type", "default"), raw.get("preset")
    if wtype not in TYPES:
        raise WorldError(f"type must be one of {', '.join(TYPES)}")
    if preset is not None and preset not in PRESETS:
        raise WorldError(f"preset must be one of {', '.join(PRESETS)}")
    border = raw.get("border_radius")
    if border is not None:
        if not isinstance(border, dict) or not set(border) <= set(DIMENSIONS):
            raise WorldError("border_radius maps minecraft:overworld/the_nether/the_end to a radius")
        for v in border.values():
            if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= 29999984):
                raise WorldError("a border radius is a whole number of blocks (0-29999984) or null")
    pregen = raw.get("pregen")
    if pregen is not None and not isinstance(pregen, bool):
        raise WorldError("pregen must be true, false or null")
    return {
        "name": name,
        "seed": seed,
        "type": wtype,
        "preset": preset,
        "border_radius": {d: border.get(d) for d in DIMENSIONS} if border is not None else None,
        "pregen": pregen,
    }


def properties_for(entry: dict[str, Any], create: bool) -> dict[str, str]:
    """server.properties keys a switch writes. Seed and type only matter when the world is generated, so they
    are written for a new world only (an archived world keeps its own); the preset for both."""
    props: dict[str, str] = {}
    if create:
        props["level-seed"] = entry["seed"]
        props.update(TYPES[entry["type"]])
    if entry.get("preset"):
        props.update(PRESETS[entry["preset"]])
    return props


def read_library(path: Path | None = None) -> dict[str, Any]:
    """The deploy's view (server.yml world.active/library, with the panel's pending change applied)."""
    data = read_json(path or LIBRARY_FILE, {})
    entries = []
    for raw in data.get("library", []) if isinstance(data, dict) else []:
        with contextlib.suppress(WorldError):
            entries.append(check_entry(raw))
    active = data.get("active") if isinstance(data, dict) else None
    return {
        "active": active if isinstance(active, str) and WORLD_NAME.fullmatch(active) else None,
        "library": entries,
    }


def read_json(path: Path, default: Any) -> Any:  # noqa: ANN401 - whatever the file holds
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def atomic_write(path: Path, text: str, mode: int = 0o640) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    tmp.chmod(mode)
    os.replace(tmp, path)


# --- the archive list (restic tags) ------------------------------------------------------------------------


def tag_value(tags: Iterable[str], prefix: str) -> str | None:
    return next((t[len(prefix) :] for t in tags if t.startswith(prefix)), None)


def archives_from(snapshots: Iterable[dict[str, Any]], deleted: Iterable[str] = ()) -> list[dict[str, Any]]:
    """restic's snapshot list (raw JSON) -> one entry per archived world (its newest archive snapshot), newest
    first: {name, id, short_id, time, size, data_version, snapshots: [ids, newest first]}."""
    gone = set(deleted)
    by_name: dict[str, list[dict[str, Any]]] = {}
    for s in snapshots:
        tags = [str(t) for t in s.get("tags") or []]
        name = tag_value(tags, "world:")
        if ARCHIVE_TAG not in tags or not name or not WORLD_NAME.fullmatch(name) or name in gone:
            continue
        by_name.setdefault(name, []).append(s)
    out = []
    for name, snaps in by_name.items():
        snaps.sort(key=lambda x: str(x.get("time", "")), reverse=True)
        top = snaps[0]
        dv = tag_value([str(t) for t in top.get("tags") or []], "dv:")
        summary = top.get("summary") or {}
        out.append(
            {
                "name": name,
                "id": str(top.get("id", "")),
                "short_id": str(top.get("short_id") or str(top.get("id", ""))[:8]),
                "time": str(top.get("time", "")),
                "size": summary.get("total_bytes_processed")
                if isinstance(summary.get("total_bytes_processed"), int)
                else None,
                "data_version": int(dv) if dv and dv.isdigit() else None,
                "snapshots": [str(x.get("id", "")) for x in snaps],
                "times": [str(x.get("time", "")) for x in snaps],
            }
        )
    return sorted(out, key=lambda a: a["time"], reverse=True)


def load_state(path: Path | None = None) -> dict[str, Any]:
    """/var/lib/mc-snapshots/worlds.json: {active, since, data_version, archives, deleted, updated}. Written
    by this script (mc-world.service) and by backup.py --list-snapshots (mc-backup.service), as the ops user;
    mc-agent reads it for the panel. Rebuilt from restic on every backup, so a lost file only loses `active`
    (then server.yml's world.active counts) and `deleted` (those archives reappear until forgotten)."""
    data = read_json(path or WORLDS_FILE, {})
    data = data if isinstance(data, dict) else {}
    active = data.get("active")
    deleted = [
        d
        for d in data.get("deleted") or []
        if isinstance(d, dict) and WORLD_NAME.fullmatch(str(d.get("name")))
    ]
    return {
        "active": active if isinstance(active, str) and WORLD_NAME.fullmatch(active) else None,
        "since": str(data.get("since") or ""),
        "data_version": data.get("data_version") if isinstance(data.get("data_version"), int) else None,
        "archives": [a for a in data.get("archives") or [] if isinstance(a, dict)],
        "deleted": deleted,
        "updated": str(data.get("updated") or ""),
    }


def save_state(state: dict[str, Any], path: Path | None = None) -> None:
    state = dict(state, updated=datetime.now(UTC).isoformat(timespec="seconds"))
    atomic_write(path or WORLDS_FILE, json.dumps(state, sort_keys=True, indent=1) + "\n", 0o640)


def refresh_state(
    raw_snapshots: list[dict[str, Any]], data_version: int | None, path: Path | None = None
) -> None:
    """backup.py --list-snapshots: the archive list from restic's own list and the running world's version."""
    state = load_state(path)
    state["archives"] = archives_from(raw_snapshots, (d["name"] for d in state["deleted"]))
    if data_version is not None:
        state["data_version"] = data_version
    save_state(state, path)


# --- level.dat ----------------------------------------------------------------------------------------------


def data_version(world: Path) -> int | None:
    """level.dat's Data.DataVersion (the world format version the last server that saved it wrote)."""
    import world_info  # stdlib NBT reader, installed next to this script

    try:
        data = world_info.read_nbt(world / "level.dat").get("Data", {})
    except (OSError, ValueError, EOFError, KeyError, UnicodeDecodeError):
        return None
    dv = data.get("DataVersion") if isinstance(data, dict) else None
    return dv if isinstance(dv, int) else None


def level_name(server_dir: Path | None = None) -> str:
    props = (server_dir or SERVER_DIR) / "server.properties"
    return mc_rcon.read_properties(props).get("level-name", "world") if props.exists() else "world"


def world_dirs(root: Path, level: str) -> list[Path]:
    """Every folder a world can have in root: <level>/ (all of a 26.x world) and the pre-26 split folders."""
    return [root / level, *(root / f"{level}{s}" for s in LEGACY_SUFFIXES)]


def _real_dir(p: Path) -> bool:
    return p.is_dir() and not p.is_symlink()


def _regions(root: Path, rel: str) -> bool:
    """root/rel is a region folder with at least one region file, and no part of it under root is a symlink
    (the server can write there: never count or copy a folder elsewhere)."""
    d = root / rel
    try:
        if not d.is_dir() or d.resolve() != root.resolve() / rel:
            return False
        return any(f.is_file() and not f.is_symlink() for f in d.glob("r.*.mca"))
    except OSError:
        return False


def region_dims(root: Path, level: str) -> dict[str, list[str]]:
    """Which dimensions hold region files, per layout: {"26.x": [...], "pre-26": [...]} (dimension ids)."""
    new: list[str] = []
    base = root / level / "dimensions"
    if _real_dir(base):
        for ns in sorted(base.iterdir()):
            for dim in sorted(ns.iterdir()) if _real_dir(ns) else []:
                if _regions(root, f"{level}/dimensions/{ns.name}/{dim.name}/region"):
                    new.append(f"{ns.name}:{dim.name}")
    places = {
        "minecraft:overworld": [f"{level}/region"],
        "minecraft:the_nether": [f"{level}/DIM-1/region", f"{level}_nether/DIM-1/region"],
        "minecraft:the_end": [f"{level}/DIM1/region", f"{level}_the_end/DIM1/region"],
    }
    old = [dim for dim, rels in places.items() if any(_regions(root, rel) for rel in rels)]
    return {"26.x": new, "pre-26": old}


def check_layout(root: Path, level: str) -> tuple[str, list[Path], list[Path]]:
    """The world in root (a fetched archive): (layout "26.x" | "pre-26", the folders to copy, the folders left
    out). WorldError for one with region files in both layouts: Paper would refuse it at start-up or merge old
    chunks into it, so it is restored by hand, if at all."""
    dims = region_dims(root, level)
    if dims["26.x"] and dims["pre-26"]:
        raise WorldError(
            f"the archive has region files in both the 26.x layout ({level}/dimensions/: "
            f"{', '.join(dims['26.x'])}) and the pre-26 one ({', '.join(dims['pre-26'])}); the server would "
            "refuse or merge them at start-up, so it isn't switched in (restore it by hand)"
        )
    present = [d for d in world_dirs(root, level) if _real_dir(d)]
    legacy = [d for d in present if d != root / level]
    if dims["pre-26"] or (legacy and not _real_dir(root / level / "dimensions")):
        return "pre-26", present, []
    return "26.x", [d for d in present if d not in legacy], legacy


def version_check(target_dv: int | None, current_dv: int | None) -> str:
    """'' when fine, else the reason to refuse. An older world is allowed (it upgrades, one way: warn())."""
    if target_dv is not None and current_dv is not None and target_dv > current_dv:
        return (
            f"the archive was saved by a newer Minecraft (DataVersion {target_dv}) than this server runs "
            f"({current_dv}); a server never loads a newer world"
        )
    return ""


def version_warning(target_dv: int | None, current_dv: int | None) -> str:
    if target_dv is not None and current_dv is not None and target_dv < current_dv:
        return (
            f"the archive is from an older Minecraft (DataVersion {target_dv} < {current_dv}): it is upgraded"
            " "
            "when it loads, one way (the archive itself stays as it was)"
        )
    return ""


# --- server.properties --------------------------------------------------------------------------------------


def _escape(value: str) -> str:
    out = value.replace("\\", "\\\\")
    return ("\\" + out) if out.startswith(" ") else out


def set_properties(path: Path, changes: dict[str, str]) -> list[str]:
    """Set keys in server.properties (Java properties: `key=value`, the rest of the file untouched). Returns
    the keys that changed. The deploy's overlay doesn't manage these keys (mcctl refuses minecraft.overrides
    of them while a library is set), so the deploy never undoes a switch."""
    lines = path.read_text().splitlines() if path.exists() else []
    current = mc_rcon.read_properties(path) if path.exists() else {}
    changed = [k for k, v in changes.items() if current.get(k) != v]
    if not changed:
        return []
    seen: set[str] = set()
    out = []
    for line in lines:
        key = (
            re.split(r"(?<!\\)[=:]", line, maxsplit=1)[0].strip()
            if line.strip() and not line.lstrip().startswith(("#", "!"))
            else None
        )
        if key in changes:
            if key not in seen:
                out.append(f"{key}={_escape(changes[key])}")
                seen.add(key)
            continue
        out.append(line)
    out += [f"{k}={_escape(v)}" for k, v in changes.items() if k not in seen]
    atomic_write(path, "\n".join(out) + "\n", 0o640)
    return changed


# --- server.yml: the world section in canonical form (the panel's pull requests) ----------------------------


def _q(v: object) -> str:
    return json.dumps(v)


def library_line(entry: dict[str, Any]) -> str:
    """One canonical flow-style item of world.library (server.yml; mcctl.schema WorldEntry)."""
    pairs = [
        f"name: {_q(entry['name'])}",
        f"seed: {_q(entry.get('seed', ''))}",
        f"type: {_q(entry.get('type', 'default'))}",
        f"preset: {_q(entry.get('preset'))}",
    ]
    border = entry.get("border_radius")
    if border is not None:
        short = {
            "minecraft:overworld": "overworld",
            "minecraft:the_nether": "nether",
            "minecraft:the_end": "end",
        }
        inner = ", ".join(f"{short[d]}: {_q(border.get(d))}" for d in DIMENSIONS)
        pairs.append(f"border_radius: {{{inner}}}")
    if entry.get("pregen") is not None:
        pairs.append(f"pregen: {_q(entry['pregen'])}")
    return "    - {" + ", ".join(pairs) + "}"


def set_world_section(text: str, active: str, library: list[dict[str, Any]]) -> str:
    """server.yml with world.active and world.library replaced by their canonical form; every other line
    (comments included) stays as it was. Raises ValueError for a `world:` that isn't a block mapping."""
    lines = text.splitlines()
    body = [f"  active: {_q(active)}", "  library:" + ("" if library else " []")]
    body += [library_line(e) for e in sorted(library, key=lambda e: e["name"])]
    head = next((i for i, ln in enumerate(lines) if re.match(r"world:(\s|$)", ln)), None)
    if head is None:
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join([*lines, "", "world:", *body]) + "\n"
    if not re.fullmatch(r"world:\s*(#.*)?", lines[head]):
        raise ValueError(
            "server.yml: world: must be a block mapping (one key per line) for the panel to edit it"
        )
    end = head + 1
    last = head
    while end < len(lines):
        ln = lines[end]
        if ln.strip() and not ln.startswith((" ", "\t", "#")):
            break
        if ln.strip() and not ln.lstrip().startswith("#"):
            last = end
        end += 1
    block = lines[head + 1 : last + 1]
    indent = next(
        (len(ln) - len(ln.lstrip()) for ln in block if ln.strip() and not ln.lstrip().startswith("#")), 2
    )
    if indent != 2:
        raise ValueError(
            "server.yml: the world: section must be indented by two spaces for the panel to edit it"
        )
    kept: list[str] = []
    i = 0
    while i < len(block):
        ln = block[i]
        m = re.match(r"  (active|library):", ln)
        if not m:
            kept.append(ln)
            i += 1
            continue
        i += 1
        while i < len(block):  # the key's own lines: deeper, or a sequence at the same indent
            nxt = block[i]
            if nxt.strip() and not (
                nxt.startswith("   ") or nxt.startswith("  - ") or nxt.lstrip().startswith("#")
            ):
                break
            if m.group(1) == "active" and nxt.strip() and not nxt.lstrip().startswith("#"):
                raise ValueError("server.yml: world.active must be one line")
            i += 1
    while kept and not kept[-1].strip():
        kept.pop()
    return "\n".join([*lines[: head + 1], *kept, *body, *lines[last + 1 :]]) + "\n"


# --- the panel's request ------------------------------------------------------------------------------------


def read_request(path: Path | None = None) -> dict[str, Any]:
    """{op: create|switch|delete, name, entry (create/switch), id, login, ts}, as mc-agent wrote it."""
    req = read_json(path or REQUEST_FILE, None)
    if not isinstance(req, dict) or req.get("op") not in ("create", "switch", "delete"):
        raise WorldError(f"no usable world request in {path or REQUEST_FILE}")
    name = req.get("name")
    if not isinstance(name, str) or not WORLD_NAME.fullmatch(name):
        raise WorldError("the request's world name is invalid")
    out = {
        "op": req["op"],
        "name": name,
        "id": str(req.get("id", ""))[:64],
        "login": str(req.get("login", ""))[:200],
    }
    if req["op"] != "delete":
        entry = check_entry(req.get("entry"))
        if entry["name"] != name:
            raise WorldError("the request's entry is for another world")
        out["entry"] = entry
    ts = req.get("ts")
    if not isinstance(ts, int | float) or abs(time.time() - float(ts)) > 3600:
        raise WorldError("the world request is stale (older than 1 h): start it again from the panel")
    return out


def step(n: int, total: int, text: str) -> None:
    print(f"STEP {n}/{total}: {text}", flush=True)


def restic_json(*args: str) -> Any:  # noqa: ANN401 - restic JSON
    out = subprocess.run(
        ["restic", "--retry-lock", "30m", *args, "--json"],
        check=True,
        capture_output=True,
        text=True,
        timeout=1800,
    ).stdout
    return json.loads(out or "null")


def systemctl(*args: str, timeout: float = 600) -> None:
    subprocess.run(["systemctl", "--no-ask-password", *args], check=True, timeout=timeout)


def server_running() -> bool:
    return subprocess.run(["systemctl", "is-active", "--quiet", UNIT], check=False).returncode == 0


def wait_rcon(timeout: float = 900, sleep: Callable[[float], None] = time.sleep) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError, mc_rcon.RconError, KeyError, ValueError):
            with mc_rcon.connect(timeout=10.0) as r:
                r.command("list")
            return True
        sleep(5)
    return False


SEED_REPLY = re.compile(r"Seed: \[(-?\d+)\]")


def live_seed() -> str | None:
    with contextlib.suppress(OSError, mc_rcon.RconError, KeyError, ValueError):
        with mc_rcon.connect(timeout=10.0) as r:
            m = SEED_REPLY.search(mc_rcon.strip_formatting(r.command("seed")))
            return m.group(1) if m else None
    return None


class Ops:
    """The outside effects of a world switch, replaceable in tests."""

    def __init__(self) -> None:
        import backup
        import restore

        self.backup, self.restore = backup, restore

    def running(self) -> bool:
        return server_running()

    def systemctl(self, *args: str) -> None:
        systemctl(*args)

    def archive(self, name: str, dv: int | None) -> str:
        """The final backup of the active world (the whole server dir, like any backup) tagged as its archive;
        returns the new snapshot's id once restic's own list has it."""
        before = {s["id"] for s in restic_json("snapshots") or []}
        tags = [ARCHIVE_TAG, f"world:{name}"] + ([f"dv:{dv}"] if dv is not None else [])
        self.backup.run_backup(tag=tags[0], forget=False, extra_tags=tags[1:])
        new = [s for s in restic_json("snapshots", "--tag", f"world:{name}") or [] if s["id"] not in before]
        if not new:
            raise WorldError(f"the archive of {name} isn't in restic's snapshot list: nothing was changed")
        return str(new[0]["id"])

    def fetch(self, snapshot_id: str) -> Path:
        """The archive's server dir into the staging dir, readable for group minecraft (restore.py's way)."""
        snaps = [s for s in restic_json("snapshots") or [] if s["id"] == snapshot_id]
        if not snaps:
            raise WorldError(f"archive snapshot {snapshot_id[:8]} not found")
        self.restore.restore_to(snaps[0], STAGING)
        self.restore.share_with_server(STAGING, grp.getgrnam(SERVER_GROUP).gr_gid)
        return STAGING

    def forget(self, ids: list[str]) -> None:
        if ids:
            subprocess.run(["restic", "--retry-lock", "30m", "forget", *ids], check=True, timeout=1800)

    def backup_now(self) -> None:
        """The new active world's first snapshot right away (a fresh box restores the newest non-archive)."""
        with contextlib.suppress(OSError, subprocess.SubprocessError):
            systemctl("start", "--no-block", "mc-backup.service", timeout=60)

    def list_snapshots(self) -> None:
        with contextlib.suppress(Exception):
            self.backup.list_snapshots()


def mark_stopped(reason: str) -> None:
    record = {"since": datetime.now(UTC).isoformat(timespec="seconds"), "reason": reason[:200], "by": "world"}
    atomic_write(notify.ADMIN_STOPPED_FILE, json.dumps(record, sort_keys=True) + "\n", 0o644)


def read_marker() -> str | None:
    try:
        return notify.ADMIN_STOPPED_FILE.read_text()
    except OSError:
        return None


def unmark(prior: str | None) -> None:
    if prior is None:
        notify.ADMIN_STOPPED_FILE.unlink(missing_ok=True)
    else:
        atomic_write(notify.ADMIN_STOPPED_FILE, prior, 0o644)


def write_manifest(record: dict[str, Any]) -> None:
    STAGING_ROOT.mkdir(parents=True, exist_ok=True)
    atomic_write(MANIFEST, json.dumps(record, sort_keys=True) + "\n", 0o640)


def clean_staging() -> None:
    shutil.rmtree(STAGING, ignore_errors=True)
    MANIFEST.unlink(missing_ok=True)


def keep_days() -> int:
    import backup

    return backup.keep_within_days()


def older_than(iso: str, days: int, now: datetime | None = None) -> bool:
    try:
        taken = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return False
    if taken.tzinfo is None:
        taken = taken.replace(tzinfo=UTC)
    return ((now or datetime.now(UTC)) - taken).total_seconds() > days * 86400


def forget_due(state: dict[str, Any], ops: Ops, now: datetime | None = None) -> list[str]:
    """Forget what the bucket lock allows (snapshots older than KEEP_WITHIN_DAYS): the archives of deleted
    worlds, and archive snapshots of a world beyond its newest KEEP_ARCHIVES. Returns the ids forgotten; the
    deleted entries whose snapshots are all gone leave the state."""
    days = keep_days()
    due: list[str] = []
    still: list[dict[str, Any]] = []
    for d in state["deleted"]:
        snaps = [(i, t) for i, t in zip(d.get("snapshots") or [], d.get("times") or [], strict=False)]
        old = [i for i, t in snaps if older_than(t, days, now)]
        due += old
        rest = [(i, t) for i, t in snaps if i not in old]
        if rest:
            still.append(dict(d, snapshots=[i for i, _ in rest], times=[t for _, t in rest]))
    for a in state["archives"]:
        for i, t in list(zip(a.get("snapshots") or [], a.get("times") or [], strict=False))[KEEP_ARCHIVES:]:
            if older_than(t, days, now):
                due.append(i)
    ops.forget(due)
    state["deleted"] = still
    return due


# --- mc-world.service (as the ops user) ---------------------------------------------------------------------


def run_panel(req: dict[str, Any], ops: Ops, server_dir: Path | None = None) -> str:
    """create/switch/delete: prints STEP lines, returns the WORLD result line (SystemExit on a failure)."""
    sdir = server_dir or SERVER_DIR
    state = load_state()
    library = read_library()
    active = state["active"] or library["active"] or "world"
    name = req["name"]
    if req["op"] == "delete":
        return delete_world(name, active, state, ops)
    entry = req["entry"]
    archive = next((a for a in state["archives"] if a["name"] == name), None)
    create = req["op"] == "create"
    if name == active:
        raise WorldError(f"{name} is already the active world")
    if create and archive:
        raise WorldError(f"{name} already exists (archived): switch to it, or pick another name")
    if not create and not archive:
        raise WorldError(f"{name} isn't an archived world")
    level = level_name(sdir)
    total = 7
    step(1, total, f"checking ({'new world' if create else 'archive ' + archive['short_id']} -> {name})")
    current_dv = data_version(sdir / level) or state["data_version"]
    if archive and (why := version_check(archive.get("data_version"), current_dv)):
        raise WorldError(why)
    if archive and (warn := version_warning(archive.get("data_version"), current_dv)):
        print(f"WARNING: {warn}", flush=True)
    was_stopped = notify.admin_stopped()
    prior = read_marker() if was_stopped else None
    notify.maintenance(180, f"world switch to {name}")
    applied = marked = False
    layout = "26.x"
    saved = ""
    import restore

    try:
        with restore.restore_lock():
            step(2, total, f"stopping the server (the world {active} is archived next)")
            marked = True
            mark_stopped(
                f"world switch {active} -> {name} in progress (started {datetime.now(UTC):%H:%M} UTC)"
            )
            if ops.running():
                ops.systemctl("stop", UNIT)
            if ops.running():
                raise WorldError("the server didn't stop")
            current_dv = data_version(sdir / level) or current_dv
            step(3, total, f"archiving {active} (final backup, tags {ARCHIVE_TAG} world:{active})")
            saved = ops.archive(active, current_dv)
            step(
                4,
                total,
                f"archived {active} as {saved[:8]}; "
                + ("preparing the new world" if create else f"fetching {name}"),
            )
            clean_staging()
            if archive:
                staged = ops.fetch(archive["id"])
                staged_dv = data_version(staged / level)
                if not (staged / level / "level.dat").is_file():
                    raise WorldError(f"archive {archive['short_id']} has no {level}/level.dat")
                if why := version_check(staged_dv, current_dv):
                    raise WorldError(why)
                layout, _, left_out = check_layout(staged, level)
                if layout == "pre-26":
                    found = ", ".join(d.name + "/" for d in world_dirs(staged, level) if _real_dir(d))
                    print(
                        f"WARNING: archive {archive['short_id']} has the pre-26 folder layout ({found}): the "
                        f"server moves it into {level}/dimensions/ when it starts (after a 30-second pause), "
                        "one way (the archive itself stays as it was)",
                        flush=True,
                    )
                if left_out:
                    print(
                        f"WARNING: left out {', '.join(d.name + '/' for d in left_out)}: pre-26 folders "
                        f"without region files next to a 26.x world (still in archive {archive['short_id']})",
                        flush=True,
                    )
            write_manifest(
                {
                    "name": name,
                    "level": level,
                    "create": create,
                    "properties": properties_for(entry, create),
                    "archive": saved,
                }
            )
            step(5, total, "replacing the world folders (mc-world-apply.service, as minecraft)")
            applied = True
            ops.systemctl("start", APPLY_UNIT)
        clean_staging()
        state = load_state()
        previous = state["active"] or active
        state.update(active=name, since=datetime.now(UTC).isoformat(timespec="seconds"))
        save_state(state)
        print(f"ARCHIVED {previous} {saved}", flush=True)
        unmark(prior)
        marked = False
        if was_stopped:
            step(7, total, f"done: {name} is active; the server stays stopped (it was admin-stopped before)")
            return f"WORLD {name} active (server stopped; {previous} archived as {saved[:8]})"
        how = " (generating it)" if create else ""
        if layout == "pre-26":
            how = f" (it moves the pre-26 folders into {level}/dimensions/ first: a 30-second pause)"
        step(6, total, f"starting the server with {name}{how}")
        ops.systemctl("start", UNIT)
        verify(entry, create, sdir, level)
        ops.backup_now()
        step(7, total, f"verified: {name} is running (its first backup is starting)")
    except BaseException as e:
        why = (
            str(e)
            if isinstance(e, WorldError)
            else (e.code if isinstance(e, SystemExit) else f"{type(e).__name__}: {e}")
        )
        if applied:
            if not marked:  # the switch is done; only the start or the check failed
                print(
                    f"FAILED after the switch ({why}); {name} is in place, archive of {active}: {saved[:8]}",
                    flush=True,
                )
            else:
                mark_stopped(
                    f"world switch to {name} failed while applying; restore archive {saved[:8]} or retry"
                )
                print(
                    f"FAILED while applying ({why}); the server stays stopped. "
                    f"Archive of {active}: {saved[:8]}",
                    flush=True,
                )
        else:
            clean_staging()
            if marked:
                unmark(prior)
            if not was_stopped and not ops.running():
                with contextlib.suppress(Exception):
                    ops.systemctl("start", UNIT)
            print(f"FAILED before anything changed ({why}); {active} is untouched", flush=True)
        raise SystemExit(1) from None
    finally:
        notify.maintenance(0, f"world switch to {name}")
        ops.list_snapshots()
    return f"WORLD {name} active ({active} archived as {saved[:8]})"


def verify(entry: dict[str, Any], create: bool, sdir: Path, level: str) -> None:
    """The started world answers, is the one we wanted (seed, when we know it) and gets its border."""
    if not wait_rcon():
        raise WorldError("the server didn't answer RCON within 15 minutes")
    if entry["seed"] and create:
        got = live_seed()
        if got is not None and got != entry["seed"]:
            raise WorldError(f"the new world's seed is {got}, not {entry['seed']}")
    if not (sdir / level / "level.dat").exists():
        raise WorldError(f"the server runs, but {level}/level.dat is missing")
    for dim, radius in (entry.get("border_radius") or {}).items():
        if radius is not None:
            with contextlib.suppress(OSError, mc_rcon.RconError, KeyError, ValueError):
                mc_rcon.worldborder(dim, radius)


def delete_world(name: str, active: str, state: dict[str, Any], ops: Ops) -> str:
    """Archived worlds only. Its snapshots older than the bucket lock are forgotten now, the rest by a later
    backup (forget_due); the world leaves the list at once."""
    if name == active:
        raise WorldError(f"{name} is the active world: switch to another one first")
    archive = next((a for a in state["archives"] if a["name"] == name), None)
    if archive is None:
        raise WorldError(f"{name} isn't an archived world")
    step(1, 2, f"forgetting the archive snapshots of {name} ({len(archive['snapshots'])})")
    state["deleted"] = [d for d in state["deleted"] if d["name"] != name] + [
        {
            "name": name,
            "snapshots": archive["snapshots"],
            "times": archive.get("times") or [],
            "at": datetime.now(UTC).isoformat(timespec="seconds"),
        }
    ]
    state["archives"] = [a for a in state["archives"] if a["name"] != name]
    gone = forget_due(state, ops)
    save_state(state)
    left = sum(len(d["snapshots"]) for d in state["deleted"] if d["name"] == name)
    step(
        2,
        2,
        f"{name} deleted: {len(gone)} snapshot(s) forgotten"
        + (f", {left} after the bucket lock ends" if left else ""),
    )
    ops.list_snapshots()
    return f"WORLD {name} deleted"


# --- mc-world-apply.service (as minecraft) ------------------------------------------------------------------


def apply(server_dir: Path | None = None) -> str:
    """Replace the world folders with the staged ones (or remove them for a new world) and set the world's
    server.properties keys. Refuses unless a world switch holds restore.lock, the server is stopped and the
    manifest names a valid world."""
    import restore

    sdir = server_dir or SERVER_DIR
    if not notify.restore_running():
        raise WorldError("refusing to apply: no world switch holds restore.lock")
    if server_running():
        raise WorldError("refusing to apply while minecraft.service is running")
    record = read_json(MANIFEST, None)
    if not isinstance(record, dict) or not WORLD_NAME.fullmatch(str(record.get("name"))):
        raise WorldError(f"no usable world manifest ({MANIFEST})")
    level = str(record.get("level") or "")
    if level != level_name(sdir) or not re.fullmatch(r"[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}", level):
        raise WorldError("the manifest's level-name doesn't match server.properties")
    props = record.get("properties") or {}
    if (
        not isinstance(props, dict)
        or not set(props) <= set(MANAGED_KEYS)
        or not all(isinstance(v, str) for v in props.values())
    ):
        raise WorldError("the manifest's server.properties keys aren't world keys")
    if any("\n" in v or "\r" in v for v in props.values()):
        raise WorldError("a server.properties value has a line break")
    create = record.get("create") is True
    if not create and not (
        restore.is_real_dir(STAGING / level) and (STAGING / level / "level.dat").is_file()
    ):
        raise WorldError(f"nothing staged in {STAGING}")
    copy = [] if create else check_layout(STAGING, level)[1]  # before anything is removed
    # All three, whatever the layout: a stale <level>_nether/ left next to the new world would be migrated
    # into it at start-up. Whatever they held is in the archive run_panel took first.
    for d in world_dirs(sdir, level):
        if d.is_symlink():
            d.unlink()
        elif d.exists():
            shutil.rmtree(d)
    for d in copy:
        restore.copy_into(d, sdir / d.name)
    set_properties(sdir / "server.properties", props)
    (sdir / "logs").mkdir(mode=0o750, exist_ok=True)
    return f"applied {record['name']} ({'new' if create else 'from its archive'})"


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--panel", action="store_true", help="mc-world.service: run the agent's request")
    g.add_argument("--apply", action="store_true", help="mc-world-apply.service only")
    args = p.parse_args(argv)
    try:
        if args.apply:
            print(apply(), flush=True)
            return 0
        req = read_request()
        print(run_panel(req, Ops()), flush=True)
    except WorldError as e:
        print(f"FAILED: {e}", flush=True)
        return 1
    finally:
        if args.panel:
            REQUEST_FILE.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
