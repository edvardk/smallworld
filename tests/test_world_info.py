import gzip
import struct
from pathlib import Path

import world_info


def _s(v: str) -> bytes:
    b = v.encode()
    return struct.pack(">H", len(b)) + b


def test_reads_version(tmp_path: Path, capsys) -> None:
    version = b"\x08" + _s("Name") + _s("26.2") + b"\x03" + _s("Id") + struct.pack(">i", 4700) + b"\x00"
    data = b"\x0a" + _s("Data") + b"\x08" + _s("LevelName") + _s("world") + b"\x03" + _s("DataVersion")
    data += struct.pack(">i", 4700) + b"\x0a" + _s("Version") + version + b"\x00"
    root = b"\x0a" + _s("") + data + b"\x00"
    (tmp_path / "world" / "DIM-1").mkdir(parents=True)
    with gzip.open(tmp_path / "world" / "level.dat", "wb") as f:
        f.write(root)
    world_info.main([str(tmp_path)])
    out = capsys.readouterr().out
    assert "26.2 (DataVersion 4700)" in out and "vanilla DIM-1" in out
