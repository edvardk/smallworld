"""P16 review: ansible/library/mc_noatime.py against real mounts, inside the privileged e2e-mock container.

The container's root is an overlay without an fstab entry (site.yml proves that is left alone); here the module
runs against two scratch mounts with fstab entries, each twice:

- a tmpfs (`relatime`): the entry gains noatime, one `mount -o remount,noatime`, the kernel reports noatime;
- a bind mount of a directory on the overlay root, /srv (same st_dev as its parent, which `os.path.ismount` misses):
  found through mountinfo, `remount,bind,noatime` changes only that mount (its source path and / keep their
  options);

the second run of each changes nothing. Afterwards both are unmounted and /etc/fstab is put back.

    /opt/ansible/bin/python tests/e2e/noatime_check.py /src/product
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from hardening_check import expect, failures

FSTAB = Path("/etc/fstab")
TMPFS = Path("/srv/mc-noatime-tmpfs")
BIND_SRC = Path("/srv/mc-noatime-src")
BIND = Path("/srv/mc-noatime-bind")


def sh(*argv: str) -> None:
    subprocess.run(argv, check=True)


def options(mountpoint: Path) -> str | None:
    """The per-mount options of the topmost mount at mountpoint (mountinfo field 6)."""
    opts = None
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        parts = line.split()
        if parts[4] == str(mountpoint):
            opts = parts[5]
    return opts


def module(product: Path, path: Path) -> dict:
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"ANSIBLE_MODULE_ARGS": {"path": str(path)}}, f)
    try:
        out = subprocess.run(
            [sys.executable, str(product / "ansible/library/mc_noatime.py"), f.name],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        os.unlink(f.name)
    return json.loads(out.stdout)


def twice(product: Path, path: Path, mountpoint: Path, command: str, bind: bool) -> None:
    first = module(product, path / "world")
    expect(
        first.get("changed") is True and first.get("remounted") is True and first.get("live_noatime") is True,
        f"{mountpoint}: first run rewrites fstab and remounts once ({command}): {first}",
    )
    expect(first.get("mountpoint") == str(mountpoint), f"{path}/world is on {first.get('mountpoint')}")
    expect(first.get("bind") is bind, f"{mountpoint}: bind {first.get('bind')} (want {bind})")
    live = options(mountpoint) or ""
    expect("noatime" in live.split(","), f"{mountpoint} is mounted noatime now ({live})")
    second = module(product, path / "world")
    expect(
        second.get("changed") is False and second.get("remounted") is False and not second.get("warnings"),
        f"{mountpoint}: the second run changes nothing: {second}",
    )


def main(product: Path) -> int:
    saved = FSTAB.read_text()
    root_before = options(Path("/"))
    try:
        TMPFS.mkdir(parents=True, exist_ok=True)
        BIND_SRC.mkdir(parents=True, exist_ok=True)
        BIND.mkdir(parents=True, exist_ok=True)
        (BIND_SRC / "world").mkdir(exist_ok=True)
        FSTAB.write_text(
            saved
            + f"tmpfs {TMPFS} tmpfs defaults,relatime,size=16m 0 0\n"
            + f"{BIND_SRC} {BIND} none bind 0 0\n"
        )
        sh("mount", "-t", "tmpfs", "-o", "relatime,size=16m", "tmpfs", str(TMPFS))
        (TMPFS / "world").mkdir()
        sh("mount", "--bind", str(BIND_SRC), str(BIND))
        expect(not os.path.ismount(BIND), f"{BIND} is invisible to os.path.ismount (same filesystem as /)")

        twice(product, TMPFS, TMPFS, "remount,noatime", bind=False)
        twice(product, BIND, BIND, "remount,bind,noatime", bind=True)
        lines = FSTAB.read_text().splitlines()
        expect(
            f"tmpfs {TMPFS} tmpfs defaults,size=16m,noatime 0 0" in lines, "tmpfs entry: relatime -> noatime"
        )
        expect(f"{BIND_SRC} {BIND} none bind,noatime 0 0" in lines, "bind entry: bind,noatime")
        expect(FSTAB.read_text().startswith(saved), "the rest of /etc/fstab is unchanged")
        expect(options(Path("/")) == root_before, f"/ keeps its options ({options(Path('/'))})")
    finally:
        for mp in (BIND, TMPFS):
            subprocess.run(["umount", str(mp)], check=False)
        FSTAB.write_text(saved)
    if failures:
        print(f"\n{len(failures)} noatime check(s) failed")
        return 1
    print("\nall noatime checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
