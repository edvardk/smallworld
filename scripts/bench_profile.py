#!/usr/bin/env python3
"""Performance-profile A/B (design §8 "Performance profile"): tick times under `performance.profile:
balanced` vs `vanilla`, on the real pinned server jar, with the product's own config overlays and a
synthetic farm load.

    bench_profile.py --jdk JAVA_HOME [--server-jar FILE] [--rounds 2] [--seconds 120] [--warmup 45]
                     [--chickens 600] [--cows 320] [--heap-mb 2048] [--out results.json]

Each arm renders server-config/*.j2 from `mcctl render`'s group_vars for that profile (the same templates and
variables a deploy uses; Jinja2 with the few Ansible filters they need) and merges them into the server's own
generated configs with ansible/library/mc_config.py's merge, as a deploy does. The world: a flat world with
force-loaded chunks (they tick without players), pens of chickens (one chunk each, walled, crowded: they push
each other and lay eggs, so collisions, item merging and activation ranges all matter) and cows spread over 64
chunks. Without a player, the balanced profile's activation ranges leave most of them inactive and vanilla
ticks them all: that difference is the price of vanilla mechanics. Arms alternate (balanced vanilla, vanilla
balanced, ...). Per arm, pooled over the rounds: tick p50/p95/p99/p99.9/max (the tickprobe plugin, every
tick).

Report-only: nothing is gated (CI runners are noisy); it fails only when an arm is broken (the server doesn't
start or ticks less than half the expected ticks). Runs the jar (CI and developer machines only; never the
live server): online-mode off, bound to 127.0.0.1, RCON on loopback with a random password. The JVM runs the
product's ZGC flags (the default `jvm.gc`) with -Xms = -Xmx = --heap-mb.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import bench_gc
import bench_telemetry as bt
import build_plugin
import jinja2
import yaml
from lockfile import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ansible" / "library"))
import mc_config

from mcctl import render, schema

TICKS_PER_S = 20
OVERLAYS = {
    "bukkit.yml": "bukkit.yml.j2",
    "spigot.yml": "spigot.yml.j2",
    "purpur.yml": "purpur.yml.j2",
    "config/paper-world-defaults.yml": "paper-world-defaults.yml.j2",
}
# server.properties keys the profile decides (the bench keeps its own port/RCON/world settings)
PROPERTIES = ("view-distance", "simulation-distance", "network-compression-threshold", "sync-chunk-writes")


def profile_vars(profile: str) -> dict[str, Any]:
    data = yaml.safe_load((REPO_ROOT / "examples" / "config-repo" / "server.yml").read_text())
    data["performance"] = {"profile": profile}
    return render.group_vars(schema.load_server(data)) | {
        "mc_rcon_port": 25575,
        "mc_secret_rcon_password": "x",
    }


def _bool(v: object) -> bool:
    return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes", "on")


def render_overlay(template: str, variables: dict[str, Any]) -> dict[str, Any]:
    env = jinja2.Environment(  # noqa: S701 - YAML, not HTML (Ansible renders these without escaping too)
        loader=jinja2.FileSystemLoader(str(REPO_ROOT / "server-config")),
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        keep_trailing_newline=True,
    )
    env.filters["bool"] = _bool
    env.filters["to_json"] = json.dumps
    return yaml.safe_load(env.get_template(template).render(**variables)) or {}


def apply_profile(root: Path, profile: str) -> dict[str, Any]:
    """Merge the profile's overlays into root's generated configs; returns the server.properties values."""
    v = profile_vars(profile)
    for path, template in OVERLAYS.items():
        target = root / path
        merged = mc_config.deep_merge(yaml.safe_load(target.read_text()) or {}, render_overlay(template, v))
        target.write_text(yaml.safe_dump(merged, sort_keys=False))
    props = render_overlay("server.properties.yml.j2", v)
    return {k: props[k] for k in PROPERTIES}


class ProfileServer(bt.Server):
    def __init__(
        self, jdk: Path, root: Path, jvm: list[str], properties: dict[str, Any] | None = None
    ) -> None:
        super().__init__(jdk, root, jvm=jvm)
        self.properties = properties or {}

    def configure(self) -> None:
        super().configure()
        if self.properties:
            text = (self.root / "server.properties").read_text()
            lines = [ln for ln in text.splitlines() if ln.split("=", 1)[0] not in self.properties]
            lines += [
                f"{k}={str(v).lower() if isinstance(v, bool) else v}" for k, v in self.properties.items()
            ]
            (self.root / "server.properties").write_text("\n".join(lines) + "\n")


def prepare(jdk: Path, server_jar: bytes, base: Path, probe: Path, chickens: int, cows: int) -> Path:
    """A server dir with generated configs, a flat world, forced chunks, the pens and the herds, saved."""
    prep = base / "prep"
    (prep / "plugins").mkdir(parents=True)
    (prep / "server.jar").write_bytes(server_jar)
    shutil.copy(probe, prep / "plugins" / "tickprobe.jar")
    srv = bt.Server(jdk, prep)
    srv.start()
    try:
        srv.cmd("forceload add -64 -64 63 63")
        srv.cmd("gamerule doMobSpawning false")  # the same entities in both arms: no natural spawns
        srv.cmd("gamerule doDaylightCycle false")
        per_cow = max(1, cows // 64)
        for i in range(64):
            x, z = (i % 8) * 16 - 64 + 8, (i // 8) * 16 - 64 + 8
            for _ in range(per_cow):
                srv.cmd(f"summon minecraft:cow {x} -60 {z}")
        pens = max(1, chickens // 150)
        for p in range(pens):  # one walled chunk per pen, along z = -64..-49 (inside the forced area)
            x0, z0 = -64 + p * 16, -64
            for a, b in (((x0, z0), (x0 + 15, z0)), ((x0, z0 + 15), (x0 + 15, z0 + 15)),
                         ((x0, z0), (x0, z0 + 15)), ((x0 + 15, z0), (x0 + 15, z0 + 15))):  # fmt: skip
                srv.cmd(f"fill {a[0]} -60 {a[1]} {b[0]} -59 {b[1]} minecraft:oak_fence")
            for k in range(chickens // pens):
                srv.cmd(f"summon minecraft:chicken {x0 + 2 + k % 12} -60 {z0 + 2 + (k // 12) % 12}")
        srv.cmd("save-all flush")
    finally:
        srv.stop()
    return prep


def run_arm(jdk: Path, prep: Path, base: Path, name: str, profile: str, jvm: list[str], warmup: int,
            seconds: int) -> dict[str, Any]:  # fmt: skip
    root = base / name
    shutil.copytree(prep, root)
    (root / "bench.log").unlink(missing_ok=True)
    props = apply_profile(root, profile)
    srv = ProfileServer(jdk, root, jvm=jvm, properties=props)
    srv.start()
    try:
        time.sleep(warmup)
        srv.cmd("tickprobe reset")
        time.sleep(seconds)
        out = root / "ticks.txt"
        srv.cmd(f"tickprobe dump {out}")
        ms = [int(line.split()[0]) / 1000.0 for line in out.read_text().splitlines() if line.strip()]
        entities = srv.cmd("execute if entity @e")
    finally:
        srv.stop()
    shutil.rmtree(root / "bench", ignore_errors=True)
    if len(ms) < seconds * TICKS_PER_S / 2:
        raise SystemExit(f"{name} ({profile}) ticked only {len(ms)} times in {seconds} s: the arm is broken")
    return {"profile": profile, "ms": ms, "entities": entities.strip()}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jdk", required=True, help="JAVA_HOME of a JDK 25")
    ap.add_argument("--server-jar", help="the pinned server jar (default: download it, sha256-checked)")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--warmup", type=int, default=45)
    ap.add_argument("--chickens", type=int, default=600)
    ap.add_argument("--cows", type=int, default=320)
    ap.add_argument("--heap-mb", type=int, default=2048)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    jdk = Path(args.jdk)
    server_jar = build_plugin.server_jar(
        args.server_jar, Path(tempfile.gettempdir()) / "bench-telemetry-cache"
    )
    base = Path(tempfile.mkdtemp(prefix="bench-profile-"))
    try:
        probe = base / "tickprobe.jar"
        bt.build_probe(jdk, server_jar, probe)
        prep = prepare(jdk, server_jar, base, probe, args.chickens, args.cows)
        jvm = [
            f"-Xms{args.heap_mb}M",
            f"-Xmx{args.heap_mb}M",
            *bench_gc.jvm_flags("zgc", str(base / "gc.log")),
        ]
        order: list[str] = []
        for r in range(args.rounds):
            order += ["balanced", "vanilla"] if r % 2 == 0 else ["vanilla", "balanced"]
        runs = [
            run_arm(jdk, prep, base, f"run{i}", p, jvm, args.warmup, args.seconds)
            for i, p in enumerate(order)
        ]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    result: dict[str, Any] = {
        p: bt.stats([x for r in runs if r["profile"] == p for x in r["ms"]]) for p in ("balanced", "vanilla")
    }
    result |= {
        "rounds": [{"profile": r["profile"], "entities": r["entities"], **bt.stats(r["ms"])} for r in runs],
        "chickens": args.chickens,
        "cows": args.cows,
        "seconds": args.seconds,
        "cpus": os.cpu_count(),
    }
    table = [
        f"Synthetic farm load: {args.chickens} chickens in walled one-chunk pens + {args.cows} cows over 64 "
        f"chunks, force-loaded, no players; ZGC, {args.heap_mb} MB heap, {os.cpu_count()} CPUs.",
        "",
        "| profile | ticks | mean | p50 | p95 | p99 | p99.9 | max |",
        "|---|---|---|---|---|---|---|---|",
        *(
            f"| {p} | {s['ticks']} | {s['mean']} | {s['p50']} | {s['p95']} | {s['p99']} | {s['p999']} "
            f"| {s['max']} |"
            for p, s in ((p, result[p]) for p in ("balanced", "vanilla"))
        ),
    ]
    print("\n".join(table))
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("### Performance profile A/B (ms per tick, report-only)\n\n" + "\n".join(table) + "\n")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
