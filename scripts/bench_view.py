#!/usr/bin/env python3
"""v1.1 bench: the tick-time cost of `/view 16` (design docs/design/client-v1.1.md §7). Report-only.

    bench_view.py --jdk JAVA_HOME [--server-jar FILE] [--bots 4] [--rounds 2] [--seconds 120] [--warmup 45]
                  [--out results.json]

The real pinned server jar with mc-brand (its /view and the health guard as deployed: max 16, check every 5 s)
and the bench-only tickprobe plugin, server view distance 12 and simulation distance 8 a flat world.
`--bots` headless offline players (scripts/bench_bots.py; client render distance 16) spawn and walk outwards
in different directions at walking speed, so the server keeps loading, generating and sending new chunks, as
exploring players make it do. Arm A: the bots stay at the server's 12. Arm B: each bot types `/view 16` on
joining; mc-brand raises it while the server is Smooth (and may lower it under load: `mcbrand status` says how
many were raised and lowered). Arms alternate (A B B A ...); per arm, pooled over the rounds: tick
p50/p95/p99/p99.9/max, the chunks the bots received and the guard's counts.

Report-only: it fails only when an arm is broken (the server doesn't start, a bot can't join, or the server
ticks less than half the expected ticks). Runs the jar (CI and developer machines only; never the live
server): online-mode off, bound to 127.0.0.1, RCON on loopback with a random password.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import bench_bots
import bench_telemetry as bt
import build_plugin

TICKS_PER_S = 20
BRAND_CONFIG = """\
name: Bench
hud: {enabled: true, default_mode: "off", interval_seconds: 5, tab_list: true}
client: {enabled: false}
view: {enabled: true, max: 16, check_seconds: 5}
"""


class ViewServer(bt.Server):
    def configure(self) -> None:
        super().configure()
        props = (self.root / "server.properties").read_text()
        props = props.replace("view-distance=6", "view-distance=12")
        props = props.replace("simulation-distance=6", "simulation-distance=8")
        (self.root / "server.properties").write_text(props + "white-list=false\nenforce-whitelist=false\n")


def prepare(jdk: Path, server_jar: bytes, base: Path, probe: Path) -> tuple[Path, dict[str, Any], int]:
    prep = base / "prep"
    (prep / "plugins" / "mc-brand").mkdir(parents=True)
    (prep / "server.jar").write_bytes(server_jar)
    shutil.copy(probe, prep / "plugins" / "tickprobe.jar")
    shutil.copy(bt.BRAND_JAR, prep / "plugins" / "mc-brand.jar")
    (prep / "plugins" / "mc-brand" / "config.yml").write_text(BRAND_CONFIG)
    ids = bench_bots.packet_ids(jdk, prep / "server.jar", base / "reports")
    srv = ViewServer(jdk, prep)
    srv.start()  # the world, once (both arms start from this copy)
    srv.stop()
    return prep, ids, bench_bots.protocol_version(prep / "server.jar")


def run_arm(jdk: Path, prep: Path, base: Path, name: str, view16: bool, ids: dict, protocol: int, bots: int,
            warmup: int, seconds: int) -> dict[str, Any]:  # fmt: skip
    root = base / name
    shutil.copytree(prep, root)
    (root / "bench.log").unlink(missing_ok=True)
    srv = ViewServer(jdk, root)
    srv.start()
    directions = [(math.cos(2 * math.pi * i / bots), math.sin(2 * math.pi * i / bots)) for i in range(bots)]
    players = [
        bench_bots.Bot(
            ids, srv.port, f"bot{i}", d, 16, command="view 16" if view16 else "", protocol=protocol
        )
        for i, d in enumerate(directions)
    ]
    try:
        bench_bots.join_all(players)
        time.sleep(warmup)
        srv.cmd("tickprobe reset")
        chunks0 = sum(b.chunks for b in players)
        time.sleep(seconds)
        out = root / "ticks.txt"
        srv.cmd(f"tickprobe dump {out}")
        ms = [int(line.split()[0]) / 1000.0 for line in out.read_text().splitlines() if line.strip()]
        chunks = sum(b.chunks for b in players) - chunks0
        status = srv.cmd("mcbrand status")
        errors = [f"{b.name}: {b.error}" for b in players if b.error]
    finally:
        for b in players:
            b.stop.set()
        srv.stop()
    shutil.rmtree(root / "bench", ignore_errors=True)
    if errors:
        raise SystemExit(f"{name}: bots dropped out: {errors}")
    if len(ms) < seconds * TICKS_PER_S / 2:
        raise SystemExit(f"{name} ticked only {len(ms)} times in {seconds} s: the arm is broken")
    m = re.search(r"view checks=(\d+) raised=(\d+) \(now (\d+)\) lowered=(\d+)", status)
    keys = ("checks", "raised", "raised_now", "lowered")
    guard = dict(zip(keys, map(int, m.groups()), strict=True)) if m else {}
    return {"arm": "B" if view16 else "A", "ms": ms, "chunks": chunks, "guard": guard}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--jdk", required=True, help="JAVA_HOME of a JDK 25")
    ap.add_argument("--server-jar", help="the pinned server jar (default: download it, sha256-checked)")
    ap.add_argument("--bots", type=int, default=4)
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--warmup", type=int, default=45)
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    jdk = Path(args.jdk)
    cache = Path(tempfile.gettempdir()) / "bench-telemetry-cache"
    server_jar = build_plugin.server_jar(args.server_jar, cache)
    base = Path(tempfile.mkdtemp(prefix="bench-view-"))
    try:
        probe = base / "tickprobe.jar"
        bt.build_probe(jdk, server_jar, probe)
        prep, ids, protocol = prepare(jdk, server_jar, base, probe)
        order: list[bool] = []
        for r in range(args.rounds):
            order += [False, True] if r % 2 == 0 else [True, False]
        runs = [
            run_arm(jdk, prep, base, f"run{i}", v, ids, protocol, args.bots, args.warmup, args.seconds)
            for i, v in enumerate(order)
        ]
    finally:
        shutil.rmtree(base, ignore_errors=True)
    a = [x for r in runs if r["arm"] == "A" for x in r["ms"]]
    b = [x for r in runs if r["arm"] == "B" for x in r["ms"]]
    result = {
        "A_view12": bt.stats(a),
        "B_view16": bt.stats(b),
        "rounds": [
            {"arm": r["arm"], **bt.stats(r["ms"]), "chunks": r["chunks"], "guard": r["guard"]} for r in runs
        ],
        "bots": args.bots,
        "seconds": args.seconds,
        "cpus": os.cpu_count(),
    }
    table = [
        "| arm | ticks | mean | p50 | p95 | p99 | p99.9 | max | chunks sent |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for label, key, arm in (("A: view 12", "A_view12", "A"), ("B: /view 16", "B_view16", "B")):
        s = result[key]
        chunks = sum(r["chunks"] for r in runs if r["arm"] == arm)
        table.append(
            f"| {label} | {s['ticks']} | {s['mean']} | {s['p50']} | {s['p95']} | {s['p99']} | {s['p999']} "
            f"| {s['max']} | {chunks} |"
        )
    guard = [r["guard"] for r in runs if r["arm"] == "B"]
    summary = "\n".join(
        [
            f"### /view 16 bench ({args.bots} walking bots, {args.rounds} rounds x {args.seconds} s, "
            "report-only)",
            "",
            *table,
            "",
            f"Guard in arm B (per run): {guard}",
        ]
    )
    print(summary)
    if path := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(path, "a") as f:
            f.write(summary + "\n")
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
