#!/usr/bin/env python3
"""Report the Minecraft version a world was last saved with (e.g. before importing or restoring one).

    world_info.py /path/to/server-dir [--level-name world]

Reads <dir>/<level>/level.dat (gzipped NBT) and prints Version.Name, DataVersion and the dimension
layout (Bukkit-style world_nether/world_the_end vs vanilla DIM-1/DIM1). Stdlib only.
"""

from __future__ import annotations

import argparse
import gzip
import struct
import sys
from pathlib import Path
from typing import Any, BinaryIO


def _read(f: BinaryIO, fmt: str) -> Any:  # noqa: ANN401
    size = struct.calcsize(fmt)
    return struct.unpack(fmt, f.read(size))[0]


def _string(f: BinaryIO) -> str:
    return f.read(_read(f, ">H")).decode("utf-8", errors="replace")


def _payload(f: BinaryIO, tag: int) -> Any:  # noqa: ANN401
    if tag == 1:
        return _read(f, ">b")
    if tag == 2:
        return _read(f, ">h")
    if tag == 3:
        return _read(f, ">i")
    if tag == 4:
        return _read(f, ">q")
    if tag == 5:
        return _read(f, ">f")
    if tag == 6:
        return _read(f, ">d")
    if tag == 7:
        return f.read(_read(f, ">i"))
    if tag == 8:
        return _string(f)
    if tag == 9:
        inner, n = _read(f, ">b"), _read(f, ">i")
        return [_payload(f, inner) for _ in range(n)]
    if tag == 10:
        out = {}
        while (t := _read(f, ">b")) != 0:
            name = _string(f)
            out[name] = _payload(f, t)
        return out
    if tag == 11:
        return [_read(f, ">i") for _ in range(_read(f, ">i"))]
    if tag == 12:
        return [_read(f, ">q") for _ in range(_read(f, ">i"))]
    raise ValueError(f"unknown NBT tag {tag}")


def read_nbt(path: Path) -> dict[str, Any]:
    with gzip.open(path, "rb") as f:
        tag = _read(f, ">b")  # type: ignore[arg-type]
        if tag != 10:
            raise ValueError("level.dat root is not a compound")
        _string(f)  # type: ignore[arg-type]
        return _payload(f, 10)  # type: ignore[arg-type]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("server_dir", type=Path)
    p.add_argument("--level-name", default="world")
    args = p.parse_args(argv)

    world = args.server_dir / args.level_name
    data = read_nbt(world / "level.dat")["Data"]
    version = data.get("Version", {})
    print(f"level name : {data.get('LevelName')}")
    print(f"version    : {version.get('Name')} (DataVersion {data.get('DataVersion')})")
    bukkit = [
        d
        for d in (f"{args.level_name}_nether", f"{args.level_name}_the_end")
        if (args.server_dir / d).is_dir()
    ]
    vanilla = [d for d in ("DIM-1", "DIM1") if (world / d).is_dir()]
    layout = [f"bukkit {', '.join(bukkit)}"] if bukkit else []
    layout += [f"vanilla {', '.join(vanilla)}"] if vanilla else []
    print(f"layout     : {'; '.join(layout) or 'overworld only'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
