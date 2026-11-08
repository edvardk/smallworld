"""v1.2: the Smallworld client mod (client/mod/, scripts/build_client_mod.py, client/mod/WIRE.md).

The wire format is decoded here by an independent Python implementation against the fixtures the Java side
prints (WireFixtures.java, compared byte for byte by `build_client_mod.py check`); the pins agree with the
client lock and the plugin JDK; and, when the pinned inputs are cached (or SMALLWORLD_BUILD_CHECK=1 allows the
downloads), the committed jar is rebuilt and compared. v1.5: the client compass's channels (smallworld:compass,
smallworld:friends, the hello's capabilities) and the parity of the mod's strip with mc-brand's boss bar.
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
MAX_FRIENDS = 100
CAP_COMPASS = 1
BRAND_FIXTURES = json.loads((REPO / "plugin/mc-brand/src/test/wayfinding-fixtures.json").read_text())


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


COMPASS_FIELDS = ("flags", "max_range", "rounding", "height_threshold")


def encode(channel: str, f: dict[str, Any]) -> bytes:
    if channel == "hello":
        return (
            write_varint(f["protocol"])
            + b"".join(write_utf(f[k]) for k in ("mod_version", "tier", "pack"))
            + write_varint(f["capabilities"])
        )
    if channel == "compass":
        colors = [int(c) for c in f["colors"].split(",")]
        return b"".join(write_varint(v) for v in [*(f[k] for k in COMPASS_FIELDS), *colors])
    if channel == "friends":
        people = [p for p in f["friends"].split(";") if p]
        out = write_utf(f["dimension"]) + write_varint(len(people))
        for p in people:
            name, rest = p.split("@", 1)
            dim, xyz = rest.rsplit(":", 1)
            out += write_utf(name) + write_utf(dim) + b"".join(write_varint(int(v)) for v in xyz.split(","))
        return out
    if channel == "fps":
        return write_varint(max(0, min(MAX_FPS, f["fps"])))
    return b"".join(write_varint(f[k]) for k in ("protocol", "interval_seconds", "flags"))


def decode(channel: str, data: bytes) -> dict[str, Any]:
    r = Reader(data)
    if channel == "hello":
        out: dict[str, Any] = {
            "protocol": r.varint(),
            "mod_version": r.utf(),
            "tier": r.utf(),
            "pack": r.utf(),
        }
        out["capabilities"] = r.varint() if r.pos < len(data) else 0  # mod 1.0.0 had no capabilities
        out["compass"] = bool(out["capabilities"] & CAP_COMPASS)
        return out
    if channel == "compass":
        v = [r.varint() for _ in range(8)]
        return {
            "flags": v[0],
            "draw": bool(v[0] & 1),
            "max_range": v[1],
            "rounding": v[2],
            "height_threshold": v[3],
            "colors": ",".join(map(str, v[4:])),
        }
    if channel == "friends":
        dimension = r.utf()
        n = r.varint()
        if not 0 <= n <= MAX_FRIENDS:
            raise ValueError(f"bad friend count {n}")
        people = []
        for _ in range(n):
            name, dim = r.utf(), r.utf()
            people.append(f"{name}@{dim}:{r.varint()},{r.varint()},{r.varint()}")
        return {"dimension": dimension, "friends": ";".join(people)}
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
    if case["channel"] == "hello" and len(again) > len(data):
        again = again[:-1]  # a 1.0.0 hello: no capabilities on the wire, decoded as 0
    assert data.startswith(again)


def test_hello_capabilities_and_old_mods() -> None:
    """v1.5: the capability flag decides who draws the compass; a 1.0.0 hello (no field) has none."""
    hellos = [c for c in FIXTURES["cases"] if c["channel"] == "hello"]
    assert any(c["decoded"]["compass"] and c["decoded"]["mod_version"] == "1.1.0" for c in hellos)
    old = [c for c in hellos if "input" not in c]
    assert old and old[0]["decoded"]["capabilities"] == 0 and not old[0]["decoded"]["compass"]
    wire = (MOD / "src/main/java/io/github/edvardk/smallworld/client/SmallworldClient.java").read_text()
    assert "local.pack(), Wire.CAP_COMPASS)" in wire  # the mod always announces it


def test_client_compass_matches_the_boss_bar() -> None:
    """v1.5: for mc-brand's own fixture scenes, the mod decodes the same smallworld:friends bytes and lays out
    exactly the boss bar's strip (same items, same pixels, same focus): shared test vectors, both JVMs."""
    server = {c["what"]: c for c in BRAND_FIXTURES["client_compass"]}
    client = [c for c in FIXTURES["compass"] if c["dimension"] == "minecraft:overworld"]
    assert len(client) == len(server) >= 5
    for c in client:
        s = server[c["what"]]
        assert c["friends"] == s["friends"], c["what"]  # the vectors are mc-brand's bytes
        assert (c["placed"], c["plain"], c["focus"]) == (s["placed"], s["plain"], s["focus"]), c["what"]
        decoded = decode("friends", bytes.fromhex(c["friends"]))
        assert decoded["dimension"] == s["viewer"][3]
    # the range is applied before sending: the out-of-range player never reaches the client
    sw = next(c for c in client if "out of range" in c["what"])
    assert "Far@" not in decode("friends", bytes.fromhex(sw["friends"]))["friends"]
    # just through a portal (the message still for the overworld): the players there show as elsewhere
    portal = [c for c in FIXTURES["compass"] if c["dimension"] == "minecraft:the_nether"]
    assert portal and portal[0]["plain"].endswith(" | Alex (Overworld), Sam (Overworld)")
    assert portal[0]["focus"] == ""


def test_the_mod_draws_with_fabric_hud_api_and_no_mixins() -> None:
    src = (MOD / "src/main/java/io/github/edvardk/smallworld/client").glob("*.java")
    text = "\n".join(p.read_text() for p in src)
    assert "Mixin" not in text and "mixin" not in json.loads(
        (MOD / "src/main/resources/fabric.mod.json").read_text()
    )
    client = (MOD / "src/main/java/io/github/edvardk/smallworld/client/SmallworldClient.java").read_text()
    assert 'Identifier.fromNamespaceAndPath(Payloads.NAMESPACE, "compass"), compass::draw)' in client
    assert "VanillaHudElements.BOSS_BAR" in client
    hud = (MOD / "src/main/java/io/github/edvardk/smallworld/client/CompassHud.java").read_text()
    assert "CompassStrip.strip(" in hud and "p.exact()" in hud  # the boss bar's layout, drawn sub-pixel


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
    for channel in (
        "smallworld:hello",
        "smallworld:fps",
        "smallworld:config",
        "smallworld:compass",
        "smallworld:friends",
    ):
        assert channel in doc
    hellos = [c for c in FIXTURES["cases"] if c["channel"] == "hello"]
    for case in [c for c in hellos if "input" not in c] + hellos[
        3:4
    ]:  # the doc's worked examples are fixtures
        assert case["hex"] in doc.replace(" ", "")
    assert f"MAX_FRIENDS = {MAX_FRIENDS};" in wire and f"CAP_COMPASS = {CAP_COMPASS};" in wire
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
