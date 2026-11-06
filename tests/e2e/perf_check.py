"""P16 acceptance inside the e2e-mock container, after site.yml ran with tests/e2e/p16.yml (jvm.gc zgc,
cpu_affinity split, noatime, chunk threads, compression threshold) twice (the second run changed nothing).

- the running JVM is Generational ZGC with the product's flags (no -XX:+ZGenerational, no G1 flag) and sits on
  CPUs 1..n-1 (AllowedCPUs and the kernel's view of the process), every product helper on CPU 0;
- the GC log on tmpfs has ZGC lines that the product's parser (health.py, used by mc-health and mc-agent)
  turns into pauses and a heap figure close to the configured heap;
- THP for shmem is advise (ZGC's memfd heap), the overlays carry the knobs, and the units stay within the
  `systemd-analyze security` gate with AllowedCPUs added.

    /opt/ansible/bin/python tests/e2e/perf_check.py /src/product <heap_mb> split|off

(off: a runner with fewer than 3 CPUs, where the workflow proved the split is refused and deployed without it.)
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from hardening_check import THRESHOLDS, expect, exposure, failures, main_pid, rcon_up, show

HELPERS = (
    "mc-agent.service",
    "mc-health.service",
    "mc-backup.service",
    "mc-prune.service",
    "mc-reboot-gate.service",
)
SERVER = Path("/opt/minecraft/server")


def cpus_allowed(pid: int) -> str:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("Cpus_allowed_list:"):
            return line.split(":", 1)[1].strip()
    return "?"


def main(product: Path, heap_mb: int, affinity: str) -> int:
    sys.path.insert(0, str(product / "scripts"))
    import health

    n = os.cpu_count() or 1
    split = affinity == "split"
    jvm_cpus, helper_cpus = (f"1-{n - 1}", "0") if split else ("", "")
    expect(rcon_up(), f"the server answers RCON with ZGC (cpu_affinity {affinity})")
    pid = main_pid("minecraft.service")
    argv = Path(f"/proc/{pid}/cmdline").read_bytes().decode().split("\0")
    expect("-XX:+UseZGC" in argv, "the JVM runs ZGC")
    expect(not [a for a in argv if "ZGenerational" in a or "G1" in a], f"no ZGenerational/G1 flag: {argv}")
    expect(f"-Xmx{heap_mb}M" in argv and f"-Xms{heap_mb}M" in argv, f"heap {heap_mb} MB (Xms = Xmx)")
    allowed = show("minecraft.service", "AllowedCPUs")["AllowedCPUs"]
    expect(allowed == jvm_cpus, f"minecraft.service AllowedCPUs={allowed} (want {jvm_cpus})")
    want_list = jvm_cpus or f"0-{n - 1}"
    expect(
        cpus_allowed(pid) == want_list,
        f"the JVM's Cpus_allowed_list is {cpus_allowed(pid)} (want {want_list})",
    )
    for unit in HELPERS:
        got = show(unit, "AllowedCPUs")["AllowedCPUs"]
        expect(got == helper_cpus, f"{unit} AllowedCPUs={got} (want {helper_cpus!r})")
    for unit in ("minecraft.service", *HELPERS):
        score = exposure(unit)
        expect(score <= THRESHOLDS[unit], f"{unit}: exposure {score} <= {THRESHOLDS[unit]} with AllowedCPUs")

    # The GC log: Purpur's start-up allocates enough for ZGC's warm-up collections; wait for one.
    deadline, events = time.monotonic() + 300, []
    while time.monotonic() < deadline:
        events = health.gc_events(health.read_gc_log(SERVER))
        if any(e.pause_ms is not None for e in events) and any(e.after_mb is not None for e in events):
            break
        time.sleep(5)
    lines = health.read_gc_log(SERVER)
    expect(any("Using The Z Garbage Collector" in ln for ln in lines), "the tmpfs GC log is ZGC's")
    stats = health.parse_gc(lines, 0.0, 100.0)
    pauses = [e.pause_ms for e in health.gc_events(lines) if e.pause_ms is not None]
    expect(len(pauses) >= 3, f"health.py parses ZGC pauses ({len(pauses)}, max {stats.max_pause_ms} ms)")
    expect(stats.max_pause_ms < 100.0, f"ZGC pauses stay short (max {stats.max_pause_ms} ms)")
    heap = health.last_heap(lines)
    expect(
        heap is not None and abs(heap[1] - heap_mb) <= heap_mb * 0.03,
        f"heap after GC / capacity from the ZGC log: {heap} (capacity ~{heap_mb} MB)",
    )
    expect(stats.heap_after_pct is not None, f"post-GC heap occupancy {stats.heap_after_pct} %")

    shmem = Path("/sys/kernel/mm/transparent_hugepage/shmem_enabled").read_text()
    expect("[advise]" in shmem, f"THP shmem_enabled is advise for ZGC ({shmem.strip()})")
    props = (SERVER / "server.properties").read_text()
    expect("network-compression-threshold=512" in props.splitlines(), "network-compression-threshold=512")
    paper = (SERVER / "config" / "paper-global.yml").read_text()
    expect("worker-threads: 2" in paper, "paper-global chunk-system.worker-threads: 2")
    if failures:
        print(f"\n{len(failures)} P16 check(s) failed")
        return 1
    print("\nall P16 checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), int(sys.argv[2]), sys.argv[3]))
