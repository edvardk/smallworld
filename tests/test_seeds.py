"""v1.2 seed catalogue (world library): worlds/seeds.yml, scripts/seed_check.py and the verified file the panel
serves (watchdog/panel/seeds.json). The live half (booting the pinned jar, /locate over RCON) runs in ci.yml's
`seeds` job and in update-lock.yml; here: the committed file is current and honest, the locate replies parse,
the checker's bookkeeping, the CI wiring and the panel's/Worker's use of it."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

import pytest
import seed_check
import yaml
from conftest import REPO

VERIFIED = REPO / "watchdog" / "panel" / "seeds.json"
WORKFLOWS = REPO / ".github" / "workflows"


def test_the_committed_catalogue_is_verified_on_the_pinned_build() -> None:
    assert seed_check.check_problems() == []
    doc = json.loads(VERIFIED.read_text())
    lock = yaml.safe_load((REPO / "template" / "server.lock.yml").read_text())
    assert doc["minecraft_version"] == str(lock["minecraft_version"])
    assert doc["server"]["build"] == lock["server"]["build"]
    assert 10 <= len(doc["seeds"]) <= 15
    for e in doc["seeds"]:
        assert e["verified"] is True and e["claims"], e["id"]
        assert all(c["verified"] and c["distance"] <= c["within"] for c in e["claims"]), e["id"]


def test_every_catalogue_entry_cites_its_source() -> None:
    raw = yaml.safe_load((REPO / "worlds" / "seeds.yml").read_text())
    for s in raw["seeds"]:
        assert s["source"].startswith("https://"), s["id"]
        assert isinstance(s["seed"], str), s["id"]  # quoted: a 64-bit seed is no YAML float/int surprise


def test_seed_values_pass_the_worker_and_the_panel() -> None:
    worker = (REPO / "watchdog" / "worker.js").read_text()
    panel = (REPO / "watchdog" / "panel" / "panel.js").read_text()
    m = re.search(r"const WORLD_SEED = /(.*)/;", worker)
    p = re.search(r"const SEED_RE = /(.*)/;", panel)
    assert m and p
    for e in json.loads(VERIFIED.read_text())["seeds"]:
        assert re.fullmatch(m.group(1), e["seed"]) and re.fullmatch(p.group(1), e["seed"]), e["id"]


@pytest.mark.parametrize(
    ("reply", "want"),
    [
        (
            "The nearest #minecraft:village (minecraft:village_plains) is at [128, ~, 176] (206 blocks away)",
            (128, 176, 206),
        ),
        (
            "The nearest minecraft:cherry_grove is at [-1920, 70, -732] (2057 blocks away)",
            (-1920, -732, 2057),
        ),
        ("The nearest minecraft:fortress is at [-160, ~, 208] (262 blocks away)", (-160, 208, 262)),
        ('Could not find a structure of type "minecraft:mansion" nearby', None),
        ('Could not find a biome of type "minecraft:mushroom_fields" within reasonable distance', None),
    ],
)
def test_parse_locate(reply: str, want: tuple[int, int, int] | None) -> None:
    assert seed_check.parse_locate(reply) == want


def test_an_unknown_id_is_the_catalogue_s_error_not_a_failed_claim() -> None:
    with pytest.raises(seed_check.LocateError):
        seed_check.parse_locate('There is no structure with type "minecraft:abandoned_camp"')


class FakeRcon:
    def __init__(self, replies: dict[str, str]) -> None:
        self.replies, self.sent = replies, []

    def command(self, cmd: str, timeout: float | None = None) -> str:
        self.sent.append(cmd)
        return self.replies[cmd]


def test_measure_runs_the_dimension_and_compares_with_the_claim() -> None:
    rcon = FakeRcon(
        {
            "execute in minecraft:overworld positioned 400 64 -80 run locate structure #minecraft:village": "The nearest #minecraft:village (minecraft:village_plains)"
            " is at [410, ~, -60] (999 blocks away)",
            "execute in minecraft:the_nether positioned 50 64 -10 run locate structure minecraft:fortress": "The"
            " nearest"
            " minecraft:fortress is at [350, ~, -10] (300 blocks away)",
            "execute in minecraft:the_end positioned 0 64 0 run locate biome minecraft:ice_spikes": "Could not find a biome of type minecraft:ice_spikes",
        }
    )
    spawn = (400, -80)  # the Nether's origin is spawn / 8, the End's the main island
    ok = seed_check.measure(rcon, seed_check.Claim("structure", "#minecraft:village", 100), spawn)
    far = seed_check.measure(rcon, seed_check.Claim("structure", "minecraft:fortress", 250, "nether"), spawn)
    gone = seed_check.measure(rcon, seed_check.Claim("biome", "minecraft:ice_spikes", 900, "end"), spawn)
    assert ok == {
        "kind": "structure",
        "id": "#minecraft:village",
        "label": "village",
        "dimension": "overworld",
        "within": 100,
        "distance": 22,
        "at": [410, -60],
        "verified": True,
    }
    assert far["distance"] == 300 and far["verified"] is False and far["label"] == "fortress"
    assert gone["distance"] is None and gone["verified"] is False and gone["label"] == "ice spikes"
    assert len(rcon.sent) == 3


def test_the_spawn_comes_from_level_dat(tmp_path: Path) -> None:
    import gzip
    import struct

    def tag(kind: int, name: str, payload: bytes) -> bytes:
        return bytes([kind]) + struct.pack(">H", len(name)) + name.encode() + payload

    pos = tag(11, "pos", struct.pack(">i", 3) + struct.pack(">iii", 424, 68, 648))
    spawn = tag(10, "spawn", pos + b"\x00")
    level = tag(10, "", tag(10, "Data", spawn + b"\x00") + b"\x00")
    (tmp_path / "level.dat").write_bytes(gzip.compress(level))
    assert seed_check.world_spawn(tmp_path / "level.dat") == (424, 648)
    old = tag(
        10,
        "",
        tag(
            10,
            "Data",
            tag(3, "SpawnX", struct.pack(">i", -5)) + tag(3, "SpawnZ", struct.pack(">i", 7)) + b"\x00",
        )
        + b"\x00",
    )
    (tmp_path / "level.dat").write_bytes(gzip.compress(old))
    assert seed_check.world_spawn(tmp_path / "level.dat") == (-5, 7)


CATALOGUE = """\
seeds:
  - id: one
    name: One
    seed: "-42"
    description: A test seed.
    source: https://example.org/one
    claims:
      - structure: "#minecraft:village"
        within: 300
      - biome: minecraft:cherry_grove
        within: 800
        label: cherry grove
"""


def fake_lock(tmp_path: Path, build: int = 2655) -> Path:
    lock = tmp_path / "server.lock.yml"
    lock.write_text(
        "minecraft_version: '26.3'\njava_major: 25\nserver:\n  project: purpur\n  version: '26.3'\n"
        f"  build: {build}\n  filename: x.jar\n  url: https://example.org/x.jar\n  sha256: {'a' * 64}\n"
    )
    return lock


def results(village: int, cherry: int) -> list[dict[str, Any]]:
    return [
        {**seed_check.Claim("structure", "#minecraft:village", 300).as_dict(), "distance": village,
         "at": [0, village], "verified": village <= 300},
        {**seed_check.Claim("biome", "minecraft:cherry_grove", 800, label="cherry grove").as_dict(),
         "distance": cherry, "at": [cherry, 0], "verified": cherry <= 800},
    ]  # fmt: skip


@pytest.fixture
def fake_verify(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    cat = tmp_path / "seeds.yml"
    cat.write_text(CATALOGUE)
    out = tmp_path / "seeds.json"
    state: dict[str, Any] = {
        "village": 120,
        "cherry": 500,
        "cat": cat,
        "out": out,
        "lock": fake_lock(tmp_path),
    }

    def boot(seed: dict[str, Any], jar: bytes, java: str, timeout: float) -> tuple[Any, Any, list[str]]:
        assert seed["seed"] == "-42" and jar == b"jar"
        return results(state["village"], state["cherry"]), [8, -24], []

    monkeypatch.setattr(seed_check, "CATALOGUE", cat)
    monkeypatch.setattr(seed_check, "VERIFIED", out)
    monkeypatch.setattr(seed_check, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(seed_check, "SERVER_LOCK", state["lock"])
    monkeypatch.setattr(seed_check, "boot_and_measure", boot)
    monkeypatch.setattr(seed_check.build_plugin, "server_jar", lambda path, cache: b"jar")
    monkeypatch.setattr(seed_check, "java_binary", lambda arg, cache: "java")
    return state


def args(tmp_path: Path, compare: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        seed=None, compare=compare, java=None, cache=str(tmp_path / "cache"), timeout=60
    )


def test_verify_writes_the_file_and_compare_catches_drift(
    tmp_path: Path, fake_verify: dict[str, Any]
) -> None:
    assert seed_check.verify(args(tmp_path)) == 0
    doc = json.loads(fake_verify["out"].read_text())
    assert doc["minecraft_version"] == "26.3" and doc["server"]["build"] == 2655
    assert doc["seeds"][0]["verified"] is True and doc["seeds"][0]["claims"][0]["distance"] == 120
    assert doc["seeds"][0]["spawn"] == [8, -24]
    assert seed_check.check_problems(fake_verify["cat"], fake_verify["out"], fake_verify["lock"]) == []
    assert seed_check.verify(args(tmp_path, compare=True)) == 0  # the same result: nothing to commit
    fake_verify["village"] = 150  # a new build moved the village: the committed file is stale
    assert seed_check.verify(args(tmp_path, compare=True)) == 1
    fake_verify["village"] = 400  # ... and now beyond the claim: written, but the run fails
    assert seed_check.verify(args(tmp_path)) == 1
    problems = seed_check.check_problems(fake_verify["cat"], fake_verify["out"], fake_verify["lock"])
    assert problems == ["one: village within 300 isn't verified (400)", "one: not verified"]


def test_check_catches_a_changed_catalogue_and_another_build(
    tmp_path: Path, fake_verify: dict[str, Any]
) -> None:
    assert seed_check.verify(args(tmp_path)) == 0
    cat, out = fake_verify["cat"], fake_verify["out"]
    (tmp_path / "other").mkdir()
    other = fake_lock(tmp_path / "other", build=2700)
    assert "the lock pins 26.3 build 2700" in seed_check.check_problems(cat, out, other)[0]
    cat.write_text(CATALOGUE.replace("within: 300", "within: 200"))
    assert seed_check.check_problems(cat, out, fake_verify["lock"]) == [
        "one: the claims differ from worlds/seeds.yml"
    ]
    cat.write_text(CATALOGUE.replace("A test seed.", "Edited."))
    assert seed_check.check_problems(cat, out, fake_verify["lock"]) == [
        "one: name/seed/description/source differ from worlds/seeds.yml"
    ]
    # a hand-edited verdict is caught too
    cat.write_text(CATALOGUE)
    doc = json.loads(out.read_text())
    doc["seeds"][0]["claims"][0]["distance"] = 999
    out.write_text(json.dumps(doc))
    assert seed_check.check_problems(cat, out, fake_verify["lock"])[0].startswith("one: village within 300")


@pytest.mark.parametrize(
    ("edit", "msg"),
    [
        (('seed: "-42"', "seed: -42"), "quoted 64-bit"),
        (('seed: "-42"', 'seed: "9223372036854775808"'), "quoted 64-bit"),
        (("source: https://example.org/one", "source: http://example.org/one"), "source"),
        (("within: 300", "within: 0"), "within 1-6400"),
        (("within: 300", "within: 300\n        dimension: moon"), "dimension"),
        (('structure: "#minecraft:village"', "structure: village; stop"), "resource id"),
        (("id: one", "id: One"), "id must be"),
    ],
)
def test_the_catalogue_is_validated(tmp_path: Path, edit: tuple[str, str], msg: str) -> None:
    cat = tmp_path / "seeds.yml"
    cat.write_text(CATALOGUE.replace(*edit))
    with pytest.raises(SystemExit, match=msg):
        seed_check.load_catalogue(cat)


def test_ci_reverifies_on_catalogue_checker_and_lock_changes() -> None:
    import ci_changes

    for path in (
        "worlds/seeds.yml",
        "watchdog/panel/seeds.json",
        "scripts/seed_check.py",
        "template/server.lock.yml",
    ):
        assert ci_changes.groups([path])["seeds"], path
    ci = yaml.safe_load((WORKFLOWS / "ci.yml").read_text())
    job = ci["jobs"]["seeds"]
    assert ci["jobs"]["changes"]["outputs"]["seeds"] == "${{ steps.changes.outputs.seeds }}"
    assert job["needs"] == "changes"
    assert job["if"] == "${{ !cancelled() && needs.changes.outputs.seeds != 'false' }}"
    assert "scripts/seed_check.py verify --compare" in job["steps"][-1]["run"]
    upd = (WORKFLOWS / "update-lock.yml").read_text()
    assert "python scripts/seed_check.py verify --cache" in upd
    assert "git add template examples watchdog/panel/seeds.json" in upd


def test_the_panel_offers_the_catalogue_through_the_existing_world_create() -> None:
    panel = (REPO / "watchdog" / "panel" / "panel.js").read_text()
    worker = (REPO / "watchdog" / "worker.js").read_text()
    assert 'api("/seeds.json")' in panel
    assert "seeds\\.json" in re.search(r"const ASSET_PATHS = (/.*/);", worker).group(1)  # type: ignore[union-attr]
    assert "seeds" not in re.search(r"const CONFIRM_ASSETS = (/.*/);", worker).group(1)  # type: ignore[union-attr]
    # no new action: the dialog sends world_create with the catalogue's seed
    assert "seed_" not in re.search(r"const DESTRUCTIVE = new Set\(\[(.*)\]\);", worker).group(1)  # type: ignore[union-attr]
