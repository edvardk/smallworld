"""v1.4 acceptance on the e2e-mock runner: the world map pre-render (smallworld-map, role map) and its two users in
mc-brand, on the real Purpur in the container. Runs on the runner after brand_check.py (shard telemetry-brand),
driving the box with `docker exec`:

  - the unit and timer are installed (the timer enabled for server.yml map.schedule); a run of mc-map.service
    exits 0 and prints one JSON summary per dimension: the overworld has regions and full chunks, no unknown block
    states, nothing failed;
  - tiles exist and are well-formed (docs/reference.md "Tile file": size, magic, scale, reserved, tx/tz = the
    name), meta.json has format 1 and the world folder's st_dev/st_ino; the output is 0750 mcmap:minecraft with
    0640 files, readable by the JVM's user, and mcmap can't write the world;
  - a second run renders no region (incremental: nothing changed in between, the world's chunks are saved by the
    server only when they change; the check runs `save-all flush` first so the first run saw everything);
  - mc-brand reads it: /mcbrand selftest reports the overworld's generation, and the e2e config's map room is
    live (its chunk force-loaded) with every frame spawned. The pocket map's match rate against vanilla needs a
    player holding a map; the e2e has no client, so only its line's format is checked.

    python tests/e2e/map_check.py BOX
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time

MAP_DIR = "/var/lib/mc-map"
WORLD = "/opt/minecraft/server/world"
failures: list[str] = []

# Reads every tile in the container with the box's own python3 and prints one JSON report.
TILE_SCAN = r"""
import json, os, struct, sys
root = sys.argv[1]
out = {"tiles": 0, "bad": [], "nonzero": 0}
for dim in ("minecraft/overworld", "minecraft/the_end"):
    for s in range(5):
        d = os.path.join(root, dim, f"s{s}")
        if not os.path.isdir(d):
            continue
        for name in sorted(os.listdir(d)):
            if not name.endswith(".swt"):
                continue
            data = open(os.path.join(d, name), "rb").read()
            tx, tz = (int(v) for v in name[:-4].split("."))
            ok = (len(data) == 32 + 512 * 512 and data[:8] == b"SWMAPT\x00\x01" and data[8] == s
                  and data[10:12] == b"\0\0" and struct.unpack_from("<ii", data, 12) == (tx, tz))
            out["tiles"] += 1
            out["nonzero"] += sum(1 for b in data[32:] if b)
            if not ok:
                out["bad"].append(f"{dim}/s{s}/{name}")
print(json.dumps(out))
"""


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def sh(box: str, script: str, user: str | None = None) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "exec", box] + (["runuser", "-u", user, "--"] if user else []) + ["sh", "-c", script]
    return subprocess.run(argv, capture_output=True, text=True, timeout=600, check=False)


def rcon(box: str, command: str) -> str:
    return sh(box, f"/opt/minecraft/bin/smallworld-rcon cmd {command}", user="minecraft").stdout


def render(box: str) -> list[dict]:
    """One run of mc-map.service; its JSON summary lines (from this invocation's journal)."""
    sh(box, "systemctl reset-failed mc-map.service")
    res = sh(box, "systemctl start mc-map.service")
    expect(res.returncode == 0, f"mc-map.service runs ({res.stderr.strip()[-200:]!r})")
    inv = sh(box, "systemctl show -p InvocationID --value mc-map.service").stdout.strip()
    journal = sh(box, f"journalctl _SYSTEMD_INVOCATION_ID={inv} --no-pager -o cat").stdout
    status = sh(box, "systemctl show -p Result,ExecMainStatus mc-map.service").stdout
    expect("Result=success" in status, f"mc-map.service succeeded ({status.split()})")
    return [json.loads(line) for line in journal.splitlines() if line.startswith('{"dimension"')]


def main() -> int:
    box = sys.argv[1]
    timer = sh(
        box, "systemctl is-enabled mc-map.timer; systemctl show -p TimersCalendar --value mc-map.timer"
    )
    expect(timer.stdout.startswith("enabled"), f"mc-map.timer is enabled ({timer.stdout.split()[:4]})")
    print("  " + rcon(box, "save-all flush").strip(), flush=True)
    time.sleep(2)
    t0 = time.monotonic()
    first = render(box)
    print(f"  first run: {time.monotonic() - t0:.1f}s", flush=True)
    dims = {s["dimension"]: s for s in first}
    ow = dims.get("minecraft:overworld", {})
    expect(ow.get("regions", 0) > 0 and ow.get("chunks_full", 0) > 0, f"the overworld renders ({ow})")
    expect(all(s["unknown_states"] == 0 and s["chunks_failed"] == 0 for s in first),
           "no unknown block states, no unreadable chunks")  # fmt: skip
    expect("minecraft:the_nether" not in dims, "never the Nether")

    scan = sh(box, f"python3 -c '{TILE_SCAN}' {MAP_DIR}")
    report = json.loads(scan.stdout or "{}") if scan.returncode == 0 else {}
    expect(
        report.get("tiles", 0) >= 5 and not report.get("bad"), f"tiles exist and are well-formed ({report})"
    )
    expect(report.get("nonzero", 0) > 0, "tiles carry terrain pixels")
    meta = json.loads(sh(box, f"cat {MAP_DIR}/minecraft/overworld/meta.json").stdout or "{}")
    ident = sh(box, f"stat -c '%d %i' {WORLD}").stdout.split()
    expect(meta.get("format") == 1 and [str(meta.get("world", {}).get(k)) for k in ("dev", "ino")] == ident,
           f"meta.json: format 1, the world folder's identity ({meta.get('world')} vs {ident})")  # fmt: skip
    perms = set(
        sh(box, f"stat -c '%a %U:%G' {MAP_DIR} {MAP_DIR}/minecraft/overworld/s0/*.swt").stdout.split("\n")
    )
    expect({"750 mcmap:minecraft", "640 mcmap:minecraft"} <= perms,
           f"output 0750, tiles 0640, both mcmap:minecraft ({sorted(perms)})")  # fmt: skip
    readable = sh(box, f"head -c 8 {MAP_DIR}/minecraft/overworld/meta.json", user="minecraft")
    expect(readable.returncode == 0, "the JVM's user reads the tiles")
    write = sh(box, f"touch {WORLD}/mc-map-write-test", user="mcmap")
    expect(write.returncode != 0, "mcmap can't write the world")
    sh(box, f"rm -f {WORLD}/mc-map-write-test")

    second = render(box)
    expect(second and all(s["regions_rendered"] == 0 for s in second),
           f"a second run renders nothing ({[s.get('regions_rendered') for s in second]})")  # fmt: skip

    # mc-brand: meta.json is read at most once a minute and off the main thread.
    line, deadline = "", time.monotonic() + 90
    while time.monotonic() < deadline:
        line = next((ln for ln in rcon(box, "mcbrand selftest").splitlines() if "prerender " in ln), "")
        if re.search(r"overworld=gen\d+", line):
            break
        time.sleep(5)
    expect(
        re.search(r"prerender on \[overworld=gen\d+", line) is not None, f"mc-brand sees the tiles ({line!r})"
    )
    expect(re.search(r"match (n/a|\d+\.\d%) of \d+ px", rcon(box, "mcbrand selftest")) is not None,
           "the pocket map reports its match rate against vanilla")  # fmt: skip
    room = re.search(r"map room on (\d+)x(\d+) \w+ at \[(-?\d+) (-?\d+) (-?\d+)\]", line)
    expect(room is not None, f"the e2e config's map room is on ({line[-160:]!r})")
    if room:
        w, h, x, z = int(room.group(1)), int(room.group(2)), room.group(3), room.group(5)
        print("  " + rcon(box, f"forceload add {x} {z}").strip(), flush=True)
        live, deadline = None, time.monotonic() + 30
        while time.monotonic() < deadline:  # the chunk loads asynchronously; the room's check runs every 5 s
            live = re.search(r"live (\d+)/(\d+)", rcon(box, "mcbrand selftest"))
            if live and int(live.group(1)) == w * h:
                break
            time.sleep(1)
        expect(live is not None and int(live.group(1)) == w * h, f"every map room frame is live ({live})")
        frames = rcon(box, "execute if entity @e[type=minecraft:item_frame]")
        expect("Test passed" in frames, f"item frames exist ({frames.strip()!r})")
        rcon(box, f"forceload remove {x} {z}")
    for f in failures:
        print("FAILED:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
