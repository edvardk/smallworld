#!/usr/bin/env python3
"""v1.2 seed catalogue (world library): verify every claim of worlds/seeds.yml on the pinned server build.

    seed_check.py verify [--seed ID ...] [--java JAVA] [--cache DIR]  # boot each seed, /locate, write
    seed_check.py verify --compare                                     # CI: re-verify, fail on a difference
    seed_check.py check                                                # offline: the file is current

`verify` boots the locked server jar (template/server.lock.yml, sha256-checked; the JDK pinned in
plugin/mc-telemetry/build.yml unless --java) once per seed, headless, with a throwaway world in a temporary
directory: `level-seed` set, the server and RCON bound to 127.0.0.1 on free ports, a random RCON password
that only this process knows, online-mode off, no plugins (world generation is the server jar's alone). After
"Done" (scripts/boot_test.py's boot: status ping, log scan, clean stop) the world spawn is read from
level.dat (after `save-all flush`; every locate is positioned there explicitly) and every claim is
asked over RCON: `execute in <dimension> positioned <x> 64 <z> run locate structure|biome <id>`, from the
spawn in the Overworld, from spawn / 8 in the Nether (where a portal at spawn leads) and from the main island
(0, 0) in the End. The reply "The nearest X is at [x, y, z] (N blocks away)" gives the position; the measured
distance is the horizontal one from that origin (the server's own figure is 3D for biomes); a claim is
verified when it is found within its `within` blocks. Locate searches the generator's
placement rules, so no chunks are generated for it.

The results go to watchdog/panel/seeds.json: the catalogue (names, descriptions, sources) plus each claim's
measured distance and verdict, and the Minecraft version and server build they were measured on. The panel
serves it as a static asset (the Worlds tab's create dialog: "verified on <version>" badge). `check` (run by
tests/test_seeds.py) fails when the file doesn't match worlds/seeds.yml or the pinned build, or when a claim
isn't verified: drop or fix the claim (worlds/seeds.yml) rather than ship an unverified one. The product's
CI re-verifies when the catalogue, this script or the server lock change (ci.yml job `seeds`), and the
update-lock workflow regenerates the file with every lock bump, so the badge stays honest.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import secrets
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import boot_test
import build_plugin
import mc_rcon
import world_info
from lockfile import REPO_ROOT, SERVER_LOCK, load_yaml

CATALOGUE = REPO_ROOT / "worlds" / "seeds.yml"
VERIFIED = REPO_ROOT / "watchdog" / "panel" / "seeds.json"
DIMENSIONS = {
    "overworld": "minecraft:overworld",
    "nether": "minecraft:the_nether",
    "end": "minecraft:the_end",
}
SEED_RE = re.compile(r"^-?[0-9]{1,19}$")  # the panel's and the Worker's WORLD_SEED (non-empty)
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
RESOURCE_RE = re.compile(r"^#?[a-z0-9_.-]+:[a-z0-9_./-]+$")
FOUND = re.compile(
    r"The nearest (?P<what>\S+)(?: \((?P<which>[^)]*)\))? is at "
    r"\[(?P<x>-?\d+), (?P<y>~|-?\d+), (?P<z>-?\d+)\] \((?P<d>\d+) blocks away\)"
)
LOCATE_TIMEOUT = 300.0  # a stronghold or a rare biome far away takes a while


@dataclass(frozen=True)
class Claim:
    kind: str  # "structure" | "biome"
    id: str  # minecraft:cherry_grove, #minecraft:village
    within: int
    dimension: str = "overworld"
    label: str = ""

    def origin(self, spawn: tuple[int, int]) -> tuple[int, int]:
        if self.dimension == "nether":
            return spawn[0] // 8, spawn[1] // 8
        return (0, 0) if self.dimension == "end" else spawn

    def command(self, spawn: tuple[int, int]) -> str:
        x, z = self.origin(spawn)
        return (
            f"execute in {DIMENSIONS[self.dimension]} positioned {x} 64 {z} run locate {self.kind} {self.id}"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "id": self.id,
            "label": self.label or default_label(self.id),
            "dimension": self.dimension,
            "within": self.within,
        }


def default_label(resource: str) -> str:
    """#minecraft:village -> "village", minecraft:cherry_grove -> "cherry grove"."""
    return resource.lstrip("#").split(":", 1)[1].rsplit("/", 1)[-1].replace("_", " ")


def load_catalogue(path: Path | None = None) -> list[dict[str, Any]]:
    """The catalogue, validated; each seed's claims as Claim objects under "claims"."""
    path = path or CATALOGUE
    data = load_yaml(path) or {}
    seeds = data.get("seeds")
    if not isinstance(seeds, list) or not seeds:
        raise SystemExit(f"{path}: `seeds` must be a non-empty list")
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, s in enumerate(seeds):
        where = f"{path}: seeds[{i}]"
        if not isinstance(s, dict):
            raise SystemExit(f"{where}: not a mapping")
        extra = set(s) - {"id", "name", "seed", "description", "source", "claims"}
        if extra:
            raise SystemExit(f"{where}: unknown keys {sorted(extra)}")
        sid, seed = str(s.get("id", "")), s.get("seed")
        if not ID_RE.match(sid) or sid in seen:
            raise SystemExit(f"{where}: id must be unique, 1-40 lowercase letters, digits or -")
        seen.add(sid)
        if not isinstance(seed, str) or not SEED_RE.match(seed) or not -(2**63) <= int(seed) < 2**63:
            raise SystemExit(f"{where}: seed must be a quoted 64-bit whole number")
        for key, limit in (("name", 60), ("description", 200)):
            if not isinstance(s.get(key), str) or not 0 < len(s[key]) <= limit:
                raise SystemExit(f"{where}: {key} must be text of 1-{limit} characters")
        if not isinstance(s.get("source"), str) or not s["source"].startswith("https://"):
            raise SystemExit(f"{where}: source must be the https URL the seed was found at")
        claims = []
        for j, c in enumerate(s.get("claims") or []):
            kinds = [k for k in ("structure", "biome") if k in (c or {})]
            if len(kinds) != 1 or set(c) - {"structure", "biome", "within", "dimension", "label"}:
                raise SystemExit(f"{where}.claims[{j}]: one of structure/biome, within, [dimension, label]")
            within, dim = c.get("within"), c.get("dimension", "overworld")
            if not RESOURCE_RE.match(str(c[kinds[0]])):
                raise SystemExit(f"{where}.claims[{j}]: {c[kinds[0]]!r} isn't a resource id")
            if not isinstance(within, int) or not 0 < within <= 6400 or dim not in DIMENSIONS:
                raise SystemExit(f"{where}.claims[{j}]: within 1-6400 blocks, dimension {sorted(DIMENSIONS)}")
            claims.append(Claim(kinds[0], str(c[kinds[0]]), within, dim, str(c.get("label", ""))))
        if not claims:
            raise SystemExit(f"{where}: at least one claim")
        out.append({**s, "claims": claims})
    return out


UNKNOWN = re.compile(
    r"There is no (?:structure|biome)(?: tag)? with type|Unknown or incomplete command|Invalid"
)


class LocateError(Exception):
    """The server didn't accept the locate command (an unknown id): the catalogue is wrong, not the seed."""


def parse_locate(reply: str) -> tuple[int, int, int] | None:
    """(x, z, distance) from a locate reply, None when nothing was found within the search radius."""
    m = FOUND.search(reply)
    if m:
        return int(m["x"]), int(m["z"]), int(m["d"])
    if UNKNOWN.search(reply):
        raise LocateError(reply.strip()[:200])
    return None


def world_spawn(level_dat: Path) -> tuple[int, int]:
    """The Overworld spawn (x, z) from level.dat: Data.spawn.pos (26.x) or the older SpawnX/SpawnZ."""
    nbt = world_info.read_nbt(level_dat)
    data = nbt.get("Data", nbt)
    pos = (data.get("spawn") or {}).get("pos")
    if isinstance(pos, list) and len(pos) == 3:
        return int(pos[0]), int(pos[2])
    return int(data["SpawnX"]), int(data["SpawnZ"])


def measure(rcon: Any, claim: Claim, spawn: tuple[int, int]) -> dict[str, Any]:  # noqa: ANN401 - Rcon or a fake
    cmd = claim.command(spawn)
    reply = rcon.command(cmd, timeout=LOCATE_TIMEOUT)
    found = parse_locate(reply)
    result = claim.as_dict()
    if found is None:
        print(f"  {cmd}: not found ({reply.strip()[:160]})")
        result.update(distance=None, at=None, verified=False)
    else:
        x, z, reported = found
        # horizontal, from the origin: the server's own figure is 3D for biomes (from y=64 to the cell)
        ox, oz = claim.origin(spawn)
        dist = math.floor(math.hypot(x - ox, z - oz))
        print(f"  {cmd}: {dist} blocks at x={x} z={z} (server: {reported}; claim: within {claim.within})")
        result.update(distance=dist, at=[x, z], verified=dist <= claim.within)
    return result


def lock_info(lock_path: Path | None = None) -> dict[str, Any]:
    lock = load_yaml(lock_path or SERVER_LOCK)
    server = lock["server"]
    return {
        "minecraft_version": str(lock["minecraft_version"]),
        "server": {"project": server["project"], "build": server["build"], "sha256": server["sha256"]},
    }


def entry(seed: dict[str, Any], claims: list[dict[str, Any]], spawn: list[int] | None) -> dict[str, Any]:
    return {
        "id": seed["id"],
        "name": seed["name"],
        "seed": seed["seed"],
        "description": seed["description"],
        "source": seed["source"],
        "spawn": spawn,
        "verified": bool(claims) and all(c.get("verified") is True for c in claims),
        "claims": claims,
    }


def render(info: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    doc = {
        "generated_by": "scripts/seed_check.py verify (from worlds/seeds.yml): do not hand-edit",
        **info,
        "seeds": entries,
    }
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def boot_and_measure(
    seed: dict[str, Any], jar: bytes, java: str, timeout: float
) -> tuple[list[dict[str, Any]], list[int] | None, list[str]]:
    """One throwaway server for one seed: its spawn, every claim measured over RCON, the problems."""
    results: list[dict[str, Any]] = []
    spawn: list[int] = []
    password = secrets.token_urlsafe(24)  # only in this process and the temporary server.properties
    with tempfile.TemporaryDirectory(prefix="mc-seed-check-") as d:
        server_dir = Path(d)
        port, rcon_port = boot_test.free_port(), boot_test.free_port()
        (server_dir / "server.jar").write_bytes(jar)
        (server_dir / "eula.txt").write_text("# seed catalogue check (throwaway world)\neula=true\n")
        props = os.open(server_dir / "server.properties", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(
            props,
            f"server-ip=127.0.0.1\nserver-port={port}\nonline-mode=false\nenable-query=false\n"
            f"enable-rcon=true\nrcon.port={rcon_port}\nrcon.password={password}\nbroadcast-rcon-to-ops=false\n"
            f"level-seed={seed['seed']}\nlevel-name=seed-check\nview-distance=4\nsimulation-distance=4\n"
            "spawn-protection=0\n".encode(),
        )
        os.close(props)

        def during() -> list[str]:
            try:
                with mc_rcon.Rcon("127.0.0.1", rcon_port, password, timeout=30) as rcon:
                    rcon.command("save-all flush", timeout=120)
                    spawn.extend(world_spawn(server_dir / "seed-check" / "level.dat"))
                    print(f"  world spawn: x={spawn[0]} z={spawn[1]}")
                    for claim in seed["claims"]:
                        results.append(measure(rcon, claim, (spawn[0], spawn[1])))
            except LocateError as e:
                return [f"locate refused: {e}"]
            except (OSError, mc_rcon.RconError) as e:
                return [f"RCON: {type(e).__name__}: {e}"]
            except (KeyError, ValueError, TypeError) as e:
                return [f"level.dat: no spawn ({type(e).__name__}: {e})"]
            return []

        problems = boot_test.run(server_dir, java, port, [], timeout, during=during, heap="2G")
    return results, (spawn or None), problems


def java_binary(arg: str | None, cache: Path) -> str:
    if arg:
        return arg
    home = build_plugin.jdk_home(None, build_plugin.load_build("mc-telemetry"), cache)
    return str(home / "bin" / "java")


def verify(args: argparse.Namespace) -> int:
    catalogue = load_catalogue()
    if args.seed:
        unknown = set(args.seed) - {s["id"] for s in catalogue}
        if unknown:
            raise SystemExit(f"not in worlds/seeds.yml: {sorted(unknown)}")
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    jar = build_plugin.server_jar(None, cache)  # sha256-checked against template/server.lock.yml
    java = java_binary(args.java, cache)
    info = lock_info()
    previous: dict[str, dict[str, Any]] = {}
    if args.seed and VERIFIED.is_file():  # a partial run keeps the other seeds' results (same build only)
        old = json.loads(VERIFIED.read_text())
        if {k: old.get(k) for k in info} == info:
            previous = {e["id"]: e for e in old.get("seeds", [])}
    entries, problems = [], []
    for seed in catalogue:
        if args.seed and seed["id"] not in args.seed:
            if seed["id"] in previous:
                entries.append(previous[seed["id"]])
            continue
        print(f"{seed['id']} (seed {seed['seed']}):", flush=True)
        results, spawn, boot_problems = boot_and_measure(seed, jar, java, args.timeout)
        problems += [f"{seed['id']}: {p}" for p in boot_problems]
        if len(results) != len(seed["claims"]):
            continue  # the boot failed: no partial results in the file
        entries.append(entry(seed, results, spawn))
    text = render(info, entries)
    for e in entries:
        for c in e["claims"]:
            if not c["verified"]:
                problems.append(
                    f"{e['id']}: {c['label']} (within {c['within']}) not confirmed: "
                    + (f"measured {c['distance']} blocks" if c["distance"] is not None else "not found")
                )
    if args.compare:
        if not VERIFIED.is_file() or VERIFIED.read_text() != text:
            problems.append(
                f"{VERIFIED.relative_to(REPO_ROOT)} differs from this verification: run "
                "`python scripts/seed_check.py verify` and commit it"
            )
    else:
        VERIFIED.write_text(text)
        print(f"wrote {VERIFIED.relative_to(REPO_ROOT)}")
    for p in problems:
        print(f"SEED CHECK: {p}", file=sys.stderr)
    return 1 if problems else 0


def check_problems(
    catalogue_path: Path | None = None, verified_path: Path | None = None, lock_path: Path | None = None
) -> list[str]:
    """Offline: the verified file belongs to this catalogue and this server build, and every claim holds."""
    verified_path = verified_path or VERIFIED
    catalogue = load_catalogue(catalogue_path)
    if not verified_path.is_file():
        return [f"{verified_path.name} is missing: run `python scripts/seed_check.py verify`"]
    doc = json.loads(verified_path.read_text())
    problems: list[str] = []
    info = lock_info(lock_path)
    if {k: doc.get(k) for k in info} != info:
        problems.append(
            f"{verified_path.name} was measured on {doc.get('minecraft_version')} build "
            f"{(doc.get('server') or {}).get('build')}, the lock pins {info['minecraft_version']} build "
            f"{info['server']['build']}: run `python scripts/seed_check.py verify`"
        )
    got = {e.get("id"): e for e in doc.get("seeds", [])}
    if list(got) != [s["id"] for s in catalogue]:
        problems.append(
            f"{verified_path.name} lists {list(got)}, worlds/seeds.yml {[s['id'] for s in catalogue]}"
        )
    for seed in catalogue:
        e = got.get(seed["id"])
        if e is None:
            continue
        want = entry(seed, [c.as_dict() for c in seed["claims"]], None)
        keys = ("name", "seed", "description", "source")
        if any(e.get(k) != want[k] for k in keys):
            problems.append(f"{seed['id']}: name/seed/description/source differ from worlds/seeds.yml")
        shape = [{k: c.get(k) for k in ("kind", "id", "label", "dimension", "within")} for c in e["claims"]]
        if shape != want["claims"]:
            problems.append(f"{seed['id']}: the claims differ from worlds/seeds.yml")
            continue
        for c in e["claims"]:
            dist = c.get("distance")
            honest = c.get("verified") == (isinstance(dist, int) and dist <= c["within"])
            if not honest or not c.get("verified"):
                problems.append(f"{seed['id']}: {c['label']} within {c['within']} isn't verified ({dist})")
        sp = e.get("spawn")
        if not (isinstance(sp, list) and len(sp) == 2 and all(isinstance(v, int) for v in sp)):
            problems.append(f"{seed['id']}: no measured spawn")
        if e.get("verified") is not True:
            problems.append(f"{seed['id']}: not verified")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command", choices=["verify", "check"])
    ap.add_argument("--seed", action="append", help="verify only this catalogue id (repeatable)")
    ap.add_argument("--compare", action="store_true", help="verify: compare with the file instead of writing")
    ap.add_argument("--java", help="a java >= the lock's java_major (default: the pinned Temurin)")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "smallworld" / "build-plugin"))
    ap.add_argument("--timeout", type=float, default=900, help="seconds to reach Done, per seed")
    args = ap.parse_args(argv)
    if args.command == "verify":
        if args.compare and args.seed:
            ap.error("--compare re-verifies the whole catalogue")
        return verify(args)
    problems = check_problems()
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
