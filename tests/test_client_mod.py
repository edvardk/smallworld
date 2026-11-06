"""v1.2: the Smallworld client mod (client/mod/, scripts/build_client_mod.py, client/mod/WIRE.md).

The wire format is decoded here by an independent Python implementation against the fixtures the Java side
prints (WireFixtures.java, compared byte for byte by `build_client_mod.py check`); the pins agree with the
client lock and the plugin JDK; and, when the pinned inputs are cached (or SMALLWORLD_BUILD_CHECK=1 allows the
downloads), the committed jar is rebuilt and compared.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import build_client_mod
import pytest
import yaml
from conftest import REPO

MOD = REPO / "client" / "mod"
BUILD = yaml.safe_load((MOD / "build.yml").read_text())
FIXTURES = json.loads((REPO / BUILD["fixtures"]).read_text())
MAX_UTF = 64
MAX_FPS = 10_000


# --- an independent implementation of WIRE.md ---


def write_varint(value: int) -> bytes:
    v = value & 0xFFFFFFFF
    out = bytearray()
    while v & ~0x7F:
        out.append((v & 0x7F) | 0x80)
        v >>= 7
    out.append(v)
    return bytes(out)


def fit(s: str) -> str:
    while len(s.encode()) > MAX_UTF:
        s = s[:-1]
    return s


def write_utf(s: str) -> bytes:
    b = fit(s).encode()
    return write_varint(len(b)) + b


class Reader:
    def __init__(self, data: bytes) -> None:
        self.data, self.pos = data, 0

    def varint(self) -> int:
        value = 0
        for i in range(5):
            if self.pos >= len(self.data):
                raise ValueError("truncated VarInt")
            b = self.data[self.pos]
            self.pos += 1
            value |= (b & 0x7F) << (7 * i)
            if not b & 0x80:
                return value - (1 << 32) if value & (1 << 31) else value
        raise ValueError("VarInt longer than 5 bytes")

    def utf(self) -> str:
        n = self.varint()
        if not 0 <= n <= MAX_UTF or self.pos + n > len(self.data):
            raise ValueError(f"bad UTF length {n}")
        s = self.data[self.pos : self.pos + n].decode()
        self.pos += n
        return s


def encode(channel: str, f: dict[str, Any]) -> bytes:
    if channel == "hello":
        return write_varint(f["protocol"]) + b"".join(
            write_utf(f[k]) for k in ("mod_version", "tier", "pack")
        )
    if channel == "fps":
        return write_varint(max(0, min(MAX_FPS, f["fps"])))
    return b"".join(write_varint(f[k]) for k in ("protocol", "interval_seconds", "flags"))


def decode(channel: str, data: bytes) -> dict[str, Any]:
    r = Reader(data)
    if channel == "hello":
        return {"protocol": r.varint(), "mod_version": r.utf(), "tier": r.utf(), "pack": r.utf()}
    if channel == "fps":
        return {"fps": r.varint()}
    out = {"protocol": r.varint(), "interval_seconds": r.varint(), "flags": r.varint()}
    out["hud"] = bool(out["flags"] & 1)
    out["clamped_interval"] = max(2, min(60, out["interval_seconds"]))
    return out


# --- tests ---


@pytest.mark.parametrize("case", FIXTURES["cases"], ids=lambda c: f"{c['channel']}-{c['hex'][:16]}")
def test_wire_fixtures_round_trip(case: dict[str, Any]) -> None:
    data = bytes.fromhex(case["hex"])
    assert decode(case["channel"], data) == case["decoded"]
    if "input" in case:
        assert encode(case["channel"], case["input"]) == data
    # re-encoding what was decoded gives the same bytes (minus a newer peer's trailing fields)
    again = encode(case["channel"], case["decoded"])
    assert data.startswith(again)


@pytest.mark.parametrize("case", FIXTURES["malformed"], ids=lambda c: f"{c['channel']}-{c['hex'][:16]}")
def test_wire_malformed_is_refused(case: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        decode(case["channel"], bytes.fromhex(case["hex"]))


def test_wire_constants_and_doc_agree() -> None:
    wire = (MOD / "src/main/java/io/github/edvardk/smallworld/client/Wire.java").read_text()
    assert FIXTURES["protocol"] == int(re.search(r"PROTOCOL = (\d+);", wire)[1])
    assert FIXTURES["max_utf_bytes"] == MAX_UTF == int(re.search(r"MAX_UTF_BYTES = (\d+);", wire)[1])
    assert f"MAX_FPS = {MAX_FPS:_};" in wire
    doc = (MOD / "WIRE.md").read_text()
    for channel in ("smallworld:hello", "smallworld:fps", "smallworld:config"):
        assert channel in doc
    for case in FIXTURES["cases"][:1]:  # the doc's worked example is a real fixture
        assert case["hex"] in doc.replace(" ", "")
    for name in ("smallworld-pack.json", "smallworld.json"):
        assert name in doc
        assert (
            name in (MOD / "src/main/java/io/github/edvardk/smallworld/client/LocalConfig.java").read_text()
        )


def test_pins_agree_with_the_client_lock_and_the_plugin_jdk() -> None:
    lock = yaml.safe_load((REPO / "template/client.lock.yml").read_text())
    api = next(f for f in lock["files"] if f["slug"] == "fabric-api")
    assert str(BUILD["minecraft"]["version"]) == str(lock["minecraft_version"])
    assert BUILD["fabric_loader"]["version"] == lock["loader"]["version"]
    assert (BUILD["fabric_api"]["version"], BUILD["fabric_api"]["sha256"]) == (api["version"], api["sha256"])
    assert BUILD["fabric_api"]["url"] == api["url"]
    brand = yaml.safe_load((REPO / "plugin/mc-brand/build.yml").read_text())
    assert BUILD["jdk"] == brand["jdk"]
    server = yaml.safe_load((REPO / "template/server.lock.yml").read_text())
    assert int(BUILD["release"]) == int(server["java_major"])
    for dep in [BUILD["minecraft"], BUILD["fabric_loader"], BUILD["fabric_api"], *BUILD["libraries"]]:
        assert re.fullmatch(r"[0-9a-f]{64}", dep["sha256"]) and dep["url"].startswith("https://")


def test_fabric_mod_json() -> None:
    mod = json.loads((MOD / "src/main/resources/fabric.mod.json").read_text())
    assert mod["id"] == "smallworld" and mod["license"] == "MIT" and mod["environment"] == "client"
    assert mod["version"] == BUILD["version"]
    assert mod["entrypoints"]["client"] == ["io.github.edvardk.smallworld.client.SmallworldClient"]
    assert "mixins" not in mod  # networking only
    assert mod["depends"]["fabricloader"] == f">={BUILD['fabric_loader']['version']}"
    assert mod["depends"]["minecraft"] == f"~{BUILD['minecraft']['version']}"
    used = {m.rstrip("-") for m in BUILD["fabric_api"]["modules"]} - {"fabric-api-base"}
    assert used == {k for k in mod["depends"] if k.startswith("fabric-")}


def test_committed_jar_matches_its_pin_and_sources() -> None:
    jar = (REPO / BUILD["jar"]).read_bytes()
    assert hashlib.sha256(jar).hexdigest() == BUILD["jar_sha256"]
    with zipfile.ZipFile(REPO / BUILD["jar"]) as z:
        names = z.namelist()
        assert names[0] == "META-INF/MANIFEST.MF" and names[1:] == sorted(names[1:])
        assert {i.date_time for i in z.infolist()} == {(1980, 1, 1, 0, 0, 0)}
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
        assert z.read("fabric.mod.json") == (MOD / "src/main/resources/fabric.mod.json").read_bytes()
        assert b"Fabric-Mapping-Namespace: official" in z.read("META-INF/MANIFEST.MF")
        sources = {
            p.relative_to(MOD / "src/main/java").with_suffix("").as_posix()
            for p in (MOD / "src/main/java").rglob("*.java")
        }
        top = {n.removesuffix(".class") for n in names if n.endswith(".class") and "$" not in n}
        assert top == sources  # no stale classes, nothing from src/test


def _inputs_cached(cache: Path) -> bool:
    deps = [BUILD["minecraft"], BUILD["fabric_loader"], BUILD["fabric_api"], *BUILD["libraries"]]
    jdk = cache / f"jdk-{BUILD['jdk']['version']}" / "bin" / "javac"
    return jdk.is_file() and all((cache / "client-mod" / f"{d['sha256']}.jar").is_file() for d in deps)


CACHE = Path(os.environ.get("SMALLWORLD_BUILD_CACHE", Path.home() / ".cache/smallworld/build-client-mod"))


@pytest.mark.skipif(
    shutil.which("tar") is None
    or not (os.environ.get("SMALLWORLD_BUILD_CHECK") == "1" or _inputs_cached(CACHE)),
    reason="needs the pinned JDK and jars in the cache, or SMALLWORLD_BUILD_CHECK=1 to download them (CI: ci.yml "
    "job plugin runs build_client_mod.py check)",
)
def test_rebuild_is_byte_identical() -> None:
    assert build_client_mod.main(["check", "--cache", str(CACHE)]) == 0
