#!/usr/bin/env python3
"""P16 collector A/B (design §8 "P16"): tick times with the product's G1 flags vs its Generational ZGC flags
(optionally also with the CPU split), on the real pinned server jar under the P13 bench's synthetic load.

    bench_gc.py --jdk JAVA_HOME [--server-jar FILE] [--rounds 2] [--seconds 120] [--warmup 45]
                [--entities 640] [--heap-mb 2048] [--affinity] [--out results.json]

Arms: g1 and zgc run the exact flag lists of ansible/roles/minecraft/defaults/main.yml (mc_jvm_flags_g1 /
mc_jvm_flags_zgc; only the GC log moves from /run/mc-telemetry to the arm's directory) with -Xms = -Xmx =
--heap-mb (the same heap in both: the collectors are compared, not the heap sizes). --affinity adds g1-split
and zgc-split: the JVM under `taskset -c 1-(n-1)`, CPU 0 left to the rest, as performance.cpu_affinity split
does with systemd AllowedCPUs. Each round runs every arm (alternating order: g1 zgc, zgc g1, ...), so slow
drift of the machine hits all of them. Per arm, pooled over the rounds: tick p50/p95/p99/p99.9/max (the
tickprobe plugin, every tick), GC pauses from the JVM's own log (health.gc_events: the parser the box uses;
ZGC's allocation stalls count as pauses), the JVM's CPU time per second of wall time and its peak RSS.

Report-only: the comparison never fails the run (CI runners are noisy). It fails only when an arm is
broken: the server doesn't start, it ticks less than half the expected ticks, or its GC log has no event the
product's parser recognises (that would blind health.py and mc-agent on a real box). Runs the jar (CI and
developer machines only; never the live server): online-mode off, bound to 127.0.0.1, RCON on loopback.
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

import bench_telemetry as bt
import build_plugin
import yaml
from lockfile import REPO_ROOT

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import health

ROLE_DEFAULTS = REPO_ROOT / "ansible" / "roles" / "minecraft" / "defaults" / "main.yml"
BOX_GC_LOG = "/run/mc-telemetry/gc.log"
TICKS_PER_S = 20
CLK_TCK = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100


def jvm_flags(gc: str, gc_log: str) -> list[str]:
    """The product's flag list for `gc` with the GC log redirected to `gc_log`."""
    flags = yaml.safe_load(ROLE_DEFAULTS.read_text())[f"mc_jvm_flags_{gc}"]
    return [f.replace(BOX_GC_LOG, gc_log) for f in flags]


def taskset_prefix(affinity: str, cpus: int) -> list[str]:
    """The JVM's CPUs for an arm: split = 1..n-1 (as the minecraft role's mc_jvm_cpus)."""
    if affinity == "off":
        return []
    if cpus < 3:
        raise SystemExit(f"the CPU split needs at least 3 CPUs (this machine has {cpus})")
    return ["taskset", "-c", f"1-{cpus - 1}"]


def proc_cpu_s(pid: int) -> float:
    """utime + stime of a process in seconds (/proc/<pid>/stat fields 14 and 15)."""
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / CLK_TCK


def proc_status_mb(pid: int, key: str) -> float | None:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(key + ":"):
            return round(int(line.split()[1]) / 1024, 1)
    return None


def java_pid(srv: bt.Server) -> int:
    """The JVM's pid (under taskset the Popen child execs java itself, so it is the same pid)."""
    assert srv.proc is not None
    return srv.proc.pid


def gc_summary(lines: list[str], since: float, until: float) -> dict[str, Any]:
    events = [e for e in health.gc_events(lines) if since <= e.uptime <= until]
    pauses = sorted(e.pause_ms for e in events if e.pause_ms is not None)
    return {
        "events": len(events),
        "pauses": len(pauses),
        "pause_max_ms": round(pauses[-1], 3) if pauses else 0.0,
        "pause_total_ms": round(sum(pauses), 3),
        "pause_p99_ms": round(bt.percentile(pauses, 0.99), 3) if pauses else 0.0,
        "pauses_over_20ms": sum(p >= 20.0 for p in pauses),
    }


def run_arm(
    jdk: Path, prep: Path, base: Path, name: str, gc: str, affinity: str, args: argparse.Namespace
) -> dict[str, Any]:
    root = base / name
    shutil.copytree(prep, root)
    (root / "bench.log").unlink(missing_ok=True)
    gc_log = root / "gc.log"
    heap = f"{args.heap_mb}M"
    jvm = [f"-Xms{heap}", f"-Xmx{heap}", *jvm_flags(gc, str(gc_log))]
    srv = bt.Server(jdk, root, jvm=jvm, prefix=taskset_prefix(affinity, os.cpu_count() or 1))
    started = time.monotonic()
    srv.start()
    startup_s = round(time.monotonic() - started, 1)
    pid = java_pid(srv)
    try:
        time.sleep(args.warmup)
        srv.cmd("tickprobe reset")
        t0, c0 = time.monotonic(), proc_cpu_s(pid)
        up0 = proc_uptime_s(pid)
        time.sleep(args.seconds)
        out = root / "ticks.txt"
        srv.cmd(f"tickprobe dump {out}")
        t1, c1 = time.monotonic(), proc_cpu_s(pid)
        rss, hwm = proc_status_mb(pid, "VmRSS"), proc_status_mb(pid, "VmHWM")
        ticks = [line.split() for line in out.read_text().splitlines() if line.strip()]
        ms = [int(t[0]) / 1000.0 for t in ticks]
    finally:
        srv.stop()
    lines = gc_log.read_text(errors="replace").splitlines() if gc_log.exists() else []
    up1 = up0 + (t1 - t0)
    shutil.rmtree(root, ignore_errors=True)
    return {
        "arm": name.split("@")[0],
        "gc": gc,
        "affinity": affinity,
        "ms": ms,
        "startup_s": startup_s,
        "cpu_cores": round((c1 - c0) / (t1 - t0), 3),
        "rss_mb": rss,
        "rss_peak_mb": hwm,
        "gc_window": gc_summary(lines, up0, up1),
        "gc_log_events": len(health.gc_events(lines)),
    }


def proc_uptime_s(pid: int) -> float:
    """Seconds since the process started (~ the JVM's -Xlog `uptime` decoration, counted from its start)."""
    start_ticks = int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])
    return float(Path("/proc/uptime").read_text().split()[0]) - start_ticks / CLK_TCK


def broken(run: dict[str, Any], seconds: int) -> str | None:
    if len(run["ms"]) < seconds * TICKS_PER_S // 2:
        return f"{run['arm']}: only {len(run['ms'])} ticks in {seconds} s"
    if run["gc_log_events"] == 0:
        return f"{run['arm']}: no GC log event the product's parser (health.gc_events) recognises"
    return None


def pooled(runs: list[dict[str, Any]], arm: str) -> dict[str, Any]:
    mine = [r for r in runs if r["arm"] == arm]
    windows = [r["gc_window"] for r in mine]
    return {
        **bt.stats([x for r in mine for x in r["ms"]]),
        "gc_pauses": sum(w["pauses"] for w in windows),
        "gc_pause_max_ms": max(w["pause_max_ms"] for w in windows),
        "gc_pause_total_ms": round(sum(w["pause_total_ms"] for w in windows), 3),
        "gc_pauses_over_20ms": sum(w["pauses_over_20ms"] for w in windows),
        "cpu_cores": round(sum(r["cpu_cores"] for r in mine) / len(mine), 3),
        "rss_peak_mb": max(r["rss_peak_mb"] or 0 for r in mine),
    }


def thp_state() -> dict[str, str]:
    out = {}
    for name in ("enabled", "shmem_enabled"):
        try:
            text = Path(f"/sys/kernel/mm/transparent_hugepage/{name}").read_text()
            out[name] = text.split("[", 1)[1].split("]", 1)[0]
        except (OSError, IndexError):
            out[name] = "?"
    return out


def table(result: dict[str, Any]) -> list[str]:
    rows = [
        "| arm | ticks | mean | p50 | p99 | p99.9 | max | GC pauses | GC pause max | GC pause total | "
        "JVM CPU (cores) | peak RSS MB |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for arm, s in result["arms"].items():
        rows.append(
            f"| {arm} | {s['ticks']} | {s['mean']} | {s['p50']} | {s['p99']} | {s['p999']} | {s['max']} "
            f"| {s['gc_pauses']} | {s['gc_pause_max_ms']} | {s['gc_pause_total_ms']} | {s['cpu_cores']} "
            f"| {s['rss_peak_mb']} |"
        )
    rows += [
        "",
        f"Tick times in ms; heap {result['heap_mb']} MB in every arm; {result['rounds']} round(s) x "
        f"{result['seconds']} s after a {result['warmup']} s warm-up; {result['cpus']} CPUs; THP "
        f"enabled={result['thp']['enabled']} shmem_enabled={result['thp']['shmem_enabled']}. Report-only "
        "(fails only on a broken arm).",
        *(f"Note: {n}." for n in result["notes"]),
    ]
    return rows


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument(
        "--jdk", required=True, help="JAVA_HOME of a JDK 25 (javac for the probe, java for the server)"
    )
    ap.add_argument("--server-jar", help="the pinned server jar (default: download it, sha256-checked)")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--warmup", type=int, default=45)
    ap.add_argument("--entities", type=int, default=640)
    ap.add_argument("--heap-mb", type=int, default=2048)
    ap.add_argument("--affinity", action="store_true", help="also run both collectors with the CPU split")
    ap.add_argument("--out", default="")
    args = ap.parse_args(argv)
    jdk = Path(args.jdk)
    arms = [("g1", "off"), ("zgc", "off")]
    notes = []
    if args.affinity and (os.cpu_count() or 1) >= 3:
        arms += [("g1", "split"), ("zgc", "split")]
    elif args.affinity:  # as the deploy refuses split there: not a broken arm, just not measurable here
        notes.append(f"CPU split arms skipped: {os.cpu_count()} CPUs (the split needs at least 3)")
    cache = Path(tempfile.gettempdir()) / "bench-telemetry-cache"
    server_jar = build_plugin.server_jar(args.server_jar, cache)
    base = Path(tempfile.mkdtemp(prefix="bench-gc-"))
    runs: list[dict[str, Any]] = []
    try:
        probe = base / "tickprobe.jar"
        bt.build_probe(jdk, server_jar, probe)
        prep = bt.prepare(jdk, server_jar, base, probe, args.entities)
        for r in range(args.rounds):
            for gc, aff in arms if r % 2 == 0 else list(reversed(arms)):
                name = gc + ("-split" if aff == "split" else "")
                print(f"round {r + 1}: {name}", flush=True)
                runs.append(run_arm(jdk, prep, base, f"{name}@{r}", gc, aff, args))
    finally:
        shutil.rmtree(base, ignore_errors=True)
    names = list(dict.fromkeys(r["arm"] for r in runs))
    result: dict[str, Any] = {
        "arms": {n: pooled(runs, n) for n in names},
        "runs": [{k: v for k, v in r.items() if k != "ms"} | bt.stats(r["ms"]) for r in runs],
        "heap_mb": args.heap_mb,
        "rounds": args.rounds,
        "seconds": args.seconds,
        "warmup": args.warmup,
        "entities": args.entities,
        "cpus": os.cpu_count(),
        "thp": thp_state(),
        "notes": notes,
    }
    problems = [p for r in runs if (p := broken(r, args.seconds))]
    result["broken"] = problems
    rows = table(result)
    print("\n".join(rows))
    for p in problems:
        print("BROKEN ARM:", p)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("### Collector A/B: G1 vs Generational ZGC (P16)\n\n" + "\n".join(rows) + "\n")
            f.writelines(f"\n**Broken arm:** {p}\n" for p in problems)
    if args.out:
        Path(args.out).write_text(json.dumps(result, indent=2) + "\n")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
