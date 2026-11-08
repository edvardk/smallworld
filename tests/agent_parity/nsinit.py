"""Runs inside `unshare -rm` (a user + mount namespace, root mapped to the caller): puts the harness's
directories where the agent expects its production paths, then execs the agent. Nothing outside this
process's mount namespace changes, and no production code needs a test hook.

    unshare -rm python3 nsinit.py LAYOUT.json -- AGENT ARGV...

LAYOUT.json: {"staging": dir, "roots": ["/opt", "/var/lib", "/run"], "binds": {target: host path}}.
Each root gets a tmpfs over it; its original entries come back as bind mounts (so the interpreter under /opt
or /run stays reachable), except the ones a bind target replaces. Then every bind target (a directory, or a
file such as the journald socket) is mounted from the harness's host path.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def mount(*args: str) -> None:
    subprocess.run(["mount", "-n", *args], check=True)  # -n: no /run/mount/utab


def overlay(root: str, staging: Path, replaced: set[str]) -> None:
    keep = staging / root.strip("/").replace("/", "_")
    keep.mkdir(parents=True, exist_ok=True)
    mount("--rbind", root, str(keep))
    mount("-t", "tmpfs", "-o", "mode=0755", "parity", root)
    for name in sorted(os.listdir(keep)):
        if name in replaced:
            continue
        src, dst = keep / name, Path(root) / name
        if src.is_symlink():
            dst.symlink_to(os.readlink(src))
        elif src.is_dir():
            dst.mkdir(exist_ok=True)
            mount("--rbind", str(src), str(dst))
        else:
            dst.touch()
            mount("--bind", str(src), str(dst))


def main() -> None:
    layout = json.loads(Path(sys.argv[1]).read_text())
    argv = sys.argv[sys.argv.index("--") + 1 :]
    staging = Path(layout["staging"])
    binds: dict[str, str] = layout["binds"]
    for root in layout["roots"]:
        replaced = {
            Path(t).relative_to(root).parts[0] for t in binds if Path(t).is_relative_to(root) and t != root
        }
        overlay(root, staging, replaced)
    for target, src in sorted(binds.items()):
        dst = Path(target)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if Path(src).is_dir():
            dst.mkdir(exist_ok=True)
        else:
            dst.touch(exist_ok=True)
        mount("--bind", src, target)
    os.chdir("/")
    os.execvp(argv[0], argv)  # noqa: S606 - the agent's argv, as the harness built it


if __name__ == "__main__":
    main()
