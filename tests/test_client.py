"""v1.1: the client modpack (client/pack.yml, client.lock.yml, mcctl/clientpack.py), its lock tool and gate
(scripts/client_pack.py), the mirror publisher (mcctl/clientpublish.py), mc-agent's code hand-off, mc-brand's
/modpack and /view (static: the e2e selftest runs them), the schema and render wiring. No network: Modrinth,
the Worker and upstream downloads are fakes; the Worker's own writer is compared byte for byte through node."""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import io
import json
import os
import re
import shutil
import struct
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml
from conftest import REPO, example_server, make_config_repo

from mcctl import clientpack, clientpublish, configrepo, http, render, schema

sys.path.insert(0, str(REPO / "scripts"))
import client_pack
import mc_agent

LOCK = yaml.safe_load((REPO / "template/client.lock.yml").read_text())
DEF = clientpack.load_definition()
JAVA = REPO / "plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand"
LINK = "https://mirror.test/{}/{}"
LAUNCHERS = clientpublish.launchers


def link(sha: str, filename: str) -> str:
    return LINK.format(sha, filename)


def plans(**kw: object) -> dict[str, dict]:
    args = {"server": "Lighthouse", "address": "mc.example.com", "view_distance": 12, "resource_pack": True}
    args |= kw
    return clientpack.plans_for(LOCK, tiers=list(clientpack.TIERS), **args)  # type: ignore[arg-type]


def overrides(p: dict) -> dict[str, bytes]:
    return {o["path"].removeprefix("client-overrides/"): base64.b64decode(o["data"]) for o in p["overrides"]}


def options(p: dict) -> dict[str, str]:
    return dict(line.split(":", 1) for line in overrides(p)["options.txt"].decode().splitlines())


# --- the definition and the lock -----------------------------------------------------------------------------


def test_definition_has_four_tiers_one_recommended_and_one_shader_entry() -> None:
    assert clientpack.definition_errors(DEF) == []
    assert tuple(DEF["tiers"]) == ("low", "medium", "high", "maximum")
    assert [t for t, v in DEF["tiers"].items() if v.get("recommended")] == ["medium"]
    shader = DEF["shader"]
    # decision 2026-11-03: Complementary Unbound; its licence forbids a direct upload: never mirrored, never bundled
    assert shader["slug"] == "complementary-unbound" and shader["mirror"] is False
    assert (
        shader["tiers"] == ["maximum"] and "Modrinth" in shader["credit"] and "unmodified" in shader["credit"]
    )
    assert {m["slug"] for m in DEF["mods"]} >= {
        "fabric-api", "sodium", "lithium", "ferrite-core", "modernfix", "modmenu", "iris", "distanthorizons",
    }  # fmt: skip
    opt = {m["slug"]: m.get("tiers") for m in DEF["mods"]}
    assert opt["iris"] == opt["distanthorizons"] == ["high", "maximum"]
    assert DEF["data_versions"]["26.3"] == 5023


def test_template_lock_pins_26_3_with_hashes_and_licences() -> None:
    assert clientpack.lock_errors(LOCK, "26.3", DEF) == []
    assert LOCK["loader"]["name"] == "fabric" and "mixinextras" in LOCK["loader"]["provides"]
    by = {f["slug"]: f for f in LOCK["files"]}
    assert not by["complementary-unbound"]["mirror"]  # LicenseRef-Custom: the official URL only
    # v1.2: Xaero's two mods are All Rights Reserved: in the pack, never rehosted
    never = {"complementary-unbound", "xaeros-minimap", "xaeros-world-map"}
    assert all(not by[s]["mirror"] for s in never)
    assert all(f["mirror"] for s, f in by.items() if s not in never)
    assert by["chat-heads"]["licence"] == "MPL-2.0"  # mirrored unmodified, source link in CREDITS.txt
    pending = {p["slug"]: p["reason"] for p in LOCK["pending"]}
    assert "modernfix" in pending  # no 26.3 build yet: picked up by the weekly update job
    # v1.2: Emotecraft and its library have only betas for 26.3: releases only
    assert "releases only" in pending["emotecraft"] and "releases only" in pending["player-animation-library"]
    assert by["sodium"]["version_id"] == "bAZQdGpg"  # the 0.9.2 release (0.9.3 is beta only)
    mod = by["smallworld"]  # in-house: the product's committed jar, no upstream URL
    build = yaml.safe_load((REPO / "client/mod/build.yml").read_text())
    assert mod["source"] == "inhouse" and mod["url"] == "" and mod["mirror"] and mod["licence"] == "MIT"
    assert mod["sha256"] == build["jar_sha256"] and mod["filename"] == f"smallworld-{build['version']}.jar"
    assert all(
        set(f["tiers"]) == set(clientpack.TIERS)
        for s, f in by.items()
        if s in never - {"complementary-unbound"} | {"smallworld", "chat-heads"}
    )
    other = json.loads(json.dumps(LOCK))
    next(f for f in other["files"] if f["slug"] == "smallworld")["sha512"] = "0" * 128
    assert any("pins another build" in e for e in clientpack.lock_errors(other, "26.3", DEF))
    for f in LOCK["files"]:
        if f["slug"] == "smallworld":
            continue
        assert f["url"].startswith("https://cdn.modrinth.com/data/")
        assert re.fullmatch(r"[0-9a-f]{128}", f["sha512"]) and re.fullmatch(r"[0-9a-f]{64}", f["sha256"])
    assert clientpack.lock_errors(LOCK, "26.4", DEF)[0].startswith("client.lock.yml: pins Minecraft '26.3'")
    bad = json.loads(json.dumps(LOCK))
    bad["files"][-1]["mirror"] = True  # the shader: its licence doesn't allow it
    assert any("doesn't allow mirroring" in e for e in clientpack.lock_errors(bad, "26.3", DEF))


def test_every_tier_has_its_dependencies() -> None:
    builtin = client_pack.BUILTIN_IDS | set(LOCK["loader"]["provides"])
    for tier in clientpack.TIERS:
        files = clientpack.tier_files(LOCK, tier)
        provided = builtin | {i for f in files for i in f.get("provides", [])}
        for f in files:
            assert set(f.get("depends", [])) <= provided, (tier, f["slug"])


# --- the packs ---------------------------------------------------------------------------------------------


def test_tier_defaults_are_the_specs() -> None:
    p = plans()
    low, med, high, maxi = (options(p[t]) for t in clientpack.TIERS)
    assert all(o["version"] == "5023" for o in (low, med, high, maxi))
    assert low["renderDistance"] == "8" and low["graphicsPreset"] == '"fast"' and low["ao"] == "false"
    assert (
        low["renderClouds"] == '"false"'
        and low["particles"] == "1"
        and low["entityDistanceScaling"] == "0.75"
    )
    assert low["maxFps"] == "60" and low["enableVsync"] == "false"
    assert med["renderDistance"] == "12" and med["simulationDistance"] == "8" and med["maxFps"] == "120"
    assert med["graphicsPreset"] == '"fancy"' and med["ao"] == "true" and med["enableVsync"] == "false"
    assert high["renderDistance"] == "12" and maxi["enableVsync"] == "true" and maxi["maxFps"] == "260"
    assert options(plans(view_distance=10)["medium"])["renderDistance"] == "10"  # = the server's distance
    # only the high tiers get Iris and DH; Iris's shaders are off in High, on (Complementary) in Maximum
    names = {
        t: {f["path"].split("/")[0] + "/" + f["filename"].split("-")[0].lower() for f in p[t]["files"]}
        for t in p
    }
    assert not any("iris" in n for n in names["low"] | names["medium"])
    o_high, o_max = overrides(p["high"]), overrides(p["maximum"])
    assert b"enableShaders=false" in o_high["config/iris.properties"]
    shader = next(f["filename"] for f in LOCK["files"] if f["slug"] == "complementary-unbound")
    assert f"shaderPack={shader}".encode() in o_max["config/iris.properties"]
    assert b"lodChunkRenderDistanceRadius = 128" in o_high["config/DistantHorizons.toml"]
    assert b"lodChunkRenderDistanceRadius = 256" in o_max["config/DistantHorizons.toml"]
    assert (
        b"_version = 5" in o_max["config/DistantHorizons.toml"]
        and b"enableAutoUpdater = false" in o_max["config/DistantHorizons.toml"]
    )
    assert "config/iris.properties" not in overrides(p["medium"])
    assert not any(
        k.endswith(".txt") and "Complementary" in k for k in o_max
    )  # no shader settings file (s1.2c)
    assert any(f["path"].startswith("shaderpacks/") for f in p["maximum"]["files"])
    assert not any(f["path"].startswith("shaderpacks/") for f in p["high"]["files"])
    credits = o_max["CREDITS.txt"].decode()
    assert DEF["shader"]["credit"].split(".")[0] in credits and "Sodium" in credits
    assert {k for k in o_max if k.startswith("licenses/")} >= {"licenses/LGPL-3.0.txt", "licenses/MIT.txt"}


def parse_nbt(data: bytes) -> object:
    """Just enough NBT (compound, list, string, byte, int) to read servers.dat."""
    pos = 0

    def u16() -> int:
        nonlocal pos
        pos += 2
        return struct.unpack(">H", data[pos - 2 : pos])[0]

    def string() -> str:
        nonlocal pos
        n = u16()
        pos += n
        return data[pos - n : pos].decode()

    def payload(t: int) -> object:
        nonlocal pos
        if t == 1:
            pos += 1
            return data[pos - 1]
        if t == 3:
            pos += 4
            return struct.unpack(">i", data[pos - 4 : pos])[0]
        if t == 8:
            return string()
        if t == 9:
            et = data[pos]
            pos += 5
            return [payload(et) for _ in range(struct.unpack(">i", data[pos - 4 : pos])[0])]
        if t == 10:
            out = {}
            while data[pos] != 0:
                tt = data[pos]
                pos += 1
                key = string()  # (the right-hand side would be evaluated first)
                out[key] = payload(tt)
            pos += 1
            return out
        raise AssertionError(t)

    assert data[0] == 10
    pos = 1
    string()
    value = payload(10)
    assert pos == len(data)
    return value


def test_servers_dat_names_the_server_and_enables_its_resource_pack() -> None:
    p = plans()
    assert parse_nbt(overrides(p["medium"])["servers.dat"]) == {
        "servers": [{"name": "Lighthouse", "ip": "mc.example.com", "acceptTextures": 1}]
    }
    off = plans(resource_pack=False)
    assert parse_nbt(overrides(off["low"])["servers.dat"]) == {
        "servers": [{"name": "Lighthouse", "ip": "mc.example.com"}]
    }


def test_pack_is_reproducible_and_a_valid_mrpack() -> None:
    a, b = plans(), plans()
    assert a == b
    for tier, p in a.items():
        data = clientpack.mrpack(p, link)
        assert data == clientpack.mrpack(b[tier], link)
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            assert z.testzip() is None
            names = z.namelist()
            assert names[0] == "modrinth.index.json" and all(
                n.startswith("client-overrides/") for n in names[1:]
            )
            # v1.2: the Smallworld mod's pack id (client/mod/WIRE.md)
            assert json.loads(z.read("client-overrides/config/smallworld-pack.json")) == {
                "tier": tier,
                "pack": p["version_id"],
            }
            assert all(
                i.compress_type == zipfile.ZIP_STORED and i.date_time == (1980, 1, 1, 0, 0, 0)
                for i in z.infolist()
            )
            index = json.loads(z.read("modrinth.index.json"))
        assert index["formatVersion"] == 1 and index["game"] == "minecraft"
        assert index["dependencies"] == {"minecraft": "26.3", "fabric-loader": LOCK["loader"]["version"]}
        for f in index["files"]:
            locked = next(x for x in LOCK["files"] if x["filename"] in f["path"])
            assert f["hashes"] == {"sha1": locked["sha1"], "sha512": locked["sha512"]}
            want = ([link(locked["sha256"], locked["filename"])] if locked["mirror"] else []) + [
                locked["url"]
            ]
            if not locked["url"]:  # in-house: the mirror is the only download
                want = want[:-1]
            assert f["downloads"] == want, tier
            optional = locked["slug"] in {
                "xaeros-minimap",
                "xaeros-world-map",
            }  # v1.2: a player may skip the map
            assert f["env"] == {"client": "optional" if optional else "required", "server": "unsupported"}
    assert a["medium"]["filename"] == "Lighthouse-Medium.mrpack"
    assert a["low"]["version_id"] != a["high"]["version_id"]


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed")
def test_the_workers_pack_writer_gives_the_same_bytes(tmp_path: Path) -> None:
    p = plans()["maximum"]
    (tmp_path / "plan.json").write_text(json.dumps(p))
    script = (
        "import w from './watchdog/worker.js';import {readFileSync,writeFileSync} from 'node:fs';"
        f"const p=JSON.parse(readFileSync({json.dumps(str(tmp_path / 'plan.json'))}));"
        "const z=await w._internals.packZip(p,async(s,f)=>`https://mirror.test/${s}/${f}`);"
        f"writeFileSync({json.dumps(str(tmp_path / 'out.mrpack'))},z);"
    )
    proc = subprocess.run(
        ["node", "--import", "./watchdog/test/register.mjs", "--input-type=module", "-e", script],
        cwd=REPO, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out.mrpack").read_bytes() == clientpack.mrpack(p, link)


# --- v1.2: the official launcher's zip ------------------------------------------------------------------------


def fake_blobs(p: dict) -> dict[str, bytes]:
    return {f["mirror"]: f"jar {f['filename']}".encode() for f in p["files"] if f["mirror"]}


def faked(p: dict, blobs: dict[str, bytes]) -> dict:
    """The plan with each mirrored file's sha256 replaced by its fake blob's."""
    q = json.loads(json.dumps(p))
    for f in q["files"]:
        if f["mirror"]:
            f["mirror"] = hashlib.sha256(blobs[f["mirror"]]).hexdigest()
    return q


def test_launcher_zip_is_reproducible_and_installs_like_the_launcher_expects() -> None:
    profile = LOCK["loader"]["profile"]
    assert profile["id"] == f"fabric-loader-{LOCK['loader']['version']}-26.3"
    raw = clientpack.profile_bytes(profile)
    assert hashlib.sha256(raw).hexdigest() == profile["sha256"]
    assert json.loads(raw)["inheritsFrom"] == "26.3" and json.loads(raw)["mainClass"].startswith(
        "net.fabricmc"
    )
    for tier, p in plans().items():
        blobs = fake_blobs(p)
        q = faked(p, {k: v for k, v in blobs.items()})
        by_sha = {hashlib.sha256(v).hexdigest(): v for v in blobs.values()}
        data = clientpack.launcher_zip(q, profile, "Lighthouse", by_sha)
        assert data == clientpack.launcher_zip(q, profile, "Lighthouse", by_sha)  # same bytes every time
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            assert z.testzip() is None
            names = z.namelist()
            assert {i.date_time for i in z.infolist()} == {(1980, 1, 1, 0, 0, 0)}
            pid = profile["id"]
            assert names[0] == f"versions/{pid}/{pid}.json" and z.read(names[0]) == raw
            root = f"smallworld-{tier}/"
            assert all(n.startswith(root) for n in names[1:])
            mods = {n.removeprefix(root + "mods/") for n in names if n.startswith(root + "mods/")}
            mirrored = {f["filename"] for f in p["files"] if f["mirror"] and f["path"].startswith("mods/")}
            assert mods == mirrored and "smallworld-1.1.0.jar" in mods
            assert not any("xaero" in n for n in names)  # All Rights Reserved: never in the zip
            opts = z.read(root + "options.txt").decode()
            assert "key_key.smoothCamera:key.keyboard.unknown" in opts.splitlines()  # F8 unbound
            assert json.loads(z.read(root + "config/smallworld-pack.json"))["tier"] == tier
            assert root + "servers.dat" in names and root + "CREDITS.txt" in names
            missing = z.read(root + clientpack.MISSING_FILE).decode()
            for f in p["files"]:
                if not f["mirror"]:
                    assert f["upstream"] in missing and f["hashes"]["sha512"] in missing
                    assert f"{root}{f['path'].split('/')[0]}/" in missing
            assert "Xaero's Minimap (optional)" in missing
        assert [n for n, _ in clientpack.launcher_manifest(q, profile, "Lighthouse")] == names
    shaders = plans()["maximum"]["launcher"]["missing"]
    assert any(
        m["folder"] == "shaderpacks" and not m["optional"] for m in shaders
    )  # Complementary: link only
    assert plans()["medium"]["launcher"]["filename"] == "Lighthouse-Medium.zip"
    assert [plans()[t]["launcher"]["memory_gb"] for t in clientpack.TIERS] == [4, 6, 6, 8]
    with pytest.raises(clientpack.PackError):
        clientpack.profile_bytes(profile | {"sha256": "0" * 64})
    bad = json.loads(json.dumps(LOCK))
    bad["loader"]["profile"]["id"] = "fabric-loader-0.0.1-26.3"
    assert any("loader.profile" in e for e in clientpack.lock_errors(bad, "26.3", DEF))
    old = json.loads(json.dumps(LOCK))
    del old["loader"]["profile"]  # a v1.1 lock: still valid (no launcher zip until the next lock update)
    assert clientpack.lock_errors(old, "26.3", DEF) == []


# --- schema, config repo, render -----------------------------------------------------------------------------


def load(**client: object) -> schema.ServerConfig:
    data = example_server()
    data["client"] = client
    return schema.load_server(data)


def test_schema_defaults_and_cross_checks() -> None:
    c = schema.load_server(example_server()).client
    assert not c.enabled and c.hostname == "play" and c.prompt and c.tiers == list(clientpack.TIERS)
    assert c.view.enabled and c.view.max == 16
    assert load(enabled=True).client.enabled
    data = example_server() | {"client": {"enabled": True}, "panel": {"mode": "off"}}
    with pytest.raises(schema.SchemaError) as e:
        schema.load_server(data)
    assert any("needs panel.mode cloudflare" in x for x in e.value.errors)
    data = example_server() | {"client": {"enabled": True, "hostname": "panel"}}
    with pytest.raises(schema.SchemaError) as e:
        schema.load_server(data)
    assert any("client.hostname" in x for x in e.value.errors)
    with pytest.raises(schema.SchemaError):
        load(tiers=[])
    with pytest.raises(schema.SchemaError):
        load(view={"max": 33})
    full = schema.load_server(yaml.safe_load((REPO / "examples/full/server.yml").read_text()))
    assert full.client.enabled and full.performance.view_distance == 12


def test_config_repo_needs_a_client_lock_when_enabled(tmp_path: Path) -> None:
    data = example_server() | {"client": {"enabled": True}}
    root = make_config_repo(tmp_path / "c", data, renderable=True)
    (root / "client.lock.yml").unlink()
    with pytest.raises(configrepo.ConfigError) as e:
        configrepo.load(root)
    assert any("client.lock.yml: missing" in x for x in e.value.errors)
    shutil.copy(REPO / "template/client.lock.yml", root / "client.lock.yml")
    repo = configrepo.load(root)
    assert repo.client_lock == LOCK
    files = render.files(repo)
    assert {str(k) for k in files if str(k).startswith("client/")} == {
        "client/index.json",
        *(f"client/{t}.plan.json" for t in clientpack.TIERS),
    }
    page = json.loads(files[clientpack.INDEX_FILE])
    assert [t["id"] for t in page["tiers"]] == list(clientpack.TIERS) and page["pending"] == [
        "modernfix",
        "emotecraft",
        "player-animation-library",
    ]
    assert "mc.example" not in json.dumps(page)  # the page's data never carries the server's address
    gv = render.group_vars(repo.server)
    assert (
        gv["mc_client_enabled"] and gv["mc_brand"]["client"]["url"] == f"https://play.{repo.server.dns.zone}/"
    )
    assert gv["mc_brand"]["view"] == {"enabled": True, "max": 16, "check_seconds": 5}
    tf = render.tfvars(repo.server, "o/r")
    assert tf["client_enabled"] and tf["client_hostname"] == "play"
    # off: no plans
    off = dataclasses.replace(repo, server=dataclasses.replace(repo.server, client=schema.Client()))
    assert not any(str(k).startswith("client/") for k in render.files(off))


# --- the lock tool and the gate ------------------------------------------------------------------------------


def jar(meta: dict, nested: dict[str, dict] | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("fabric.mod.json", json.dumps(meta))
        for name, m in (nested or {}).items():
            inner = io.BytesIO()
            with zipfile.ZipFile(inner, "w") as zi:
                zi.writestr("fabric.mod.json", json.dumps(m))
            z.writestr(name, inner.getvalue())
    return buf.getvalue()


def test_fabric_meta_reads_nested_jars_and_drop_unmet_marks_pending() -> None:
    data = jar(
        {"id": "fabric-api", "jars": [{"file": "META-INF/jars/a.jar"}], "depends": {"fabricloader": "*"}},
        {"META-INF/jars/a.jar": {"id": "fabric-rendering-v1", "provides": ["x"]}},
    )
    meta = client_pack.fabric_meta(data)
    assert meta == {
        "mod_id": "fabric-api",
        "provides": ["fabric-api", "fabric-rendering-v1", "x"],
        "depends": ["fabricloader"],
    }
    assert client_pack.fabric_meta(b"not a zip") == {}
    files = [
        {"slug": "a", "tiers": ["low", "high"], "provides": ["a"], "depends": []},
        {"slug": "b", "tiers": ["high"], "provides": ["b"], "depends": ["a", "c"]},
        {"slug": "d", "tiers": ["low", "high"], "provides": ["d"], "depends": ["b"]},
    ]
    pending: list[dict] = []
    client_pack.drop_unmet(files, pending)
    assert [f["slug"] for f in files] == ["a"]  # b needs c (nowhere); then d needs b
    assert pending == [{"slug": "b", "reason": "needs c"}, {"slug": "d", "reason": "needs b"}]


def test_resolve_takes_the_newest_release_and_marks_missing_builds_pending(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    d = {
        "data_versions": {"26.3": 5023},
        "redistributable": ["MIT"],
        "mods": [{"slug": "good", "name": "Good"}, {"slug": "none", "name": "None"}],
        "shader": {"slug": "shady", "name": "Shady", "mirror": False, "tiers": ["maximum"]},
    }
    blob = jar({"id": "good", "depends": {"fabricloader": "*", "minecraft": "26.3"}})
    sha = {a: hashlib.new(a, blob).hexdigest() for a in ("sha1", "sha512")}

    def file(name: str) -> dict:
        return {
            "primary": True,
            "url": f"https://cdn.modrinth.com/data/x/{name}",
            "filename": name,
            "hashes": sha,
        }

    versions = {
        "good": [
            {"id": "old", "version_type": "release", "version_number": "1", "date_published": "2026-01-01", "files": [file("g1.jar")]},
            {"id": "new", "version_type": "release", "version_number": "2", "date_published": "2026-02-01", "files": [file("g2.jar")]},
            {"id": "beta", "version_type": "beta", "version_number": "3", "date_published": "2026-03-01", "files": [file("g3.jar")]},
        ],
        "none": [{"id": "b", "version_type": "beta", "version_number": "9", "date_published": "2026-03-01", "files": []}],
        "shady": [{"id": "s", "version_type": "release", "version_number": "r1", "date_published": "2026-03-01", "files": [file("s.zip")]}],
    }  # fmt: skip

    def get_json(url: str) -> object:
        if url.startswith(client_pack.FABRIC_META):
            return [
                {"loader": {"version": "0.20.2", "stable": False}},
                {"loader": {"version": "0.20.1", "stable": True}},
            ]
        slug = url.split("/project/")[1].split("/")[0].split("?")[0]
        if "/version?" in url:
            return versions[slug]
        return {
            "id": slug.upper(),
            "project_type": "mod",
            "license": {"id": "MIT" if slug == "good" else "LicenseRef-Custom"},
        }

    monkeypatch.setattr(client_pack, "get_json", get_json)
    monkeypatch.setattr(client_pack, "download_verified", lambda url, hashes: blob)
    monkeypatch.setattr(client_pack, "loader_provides", lambda v: ["fabricloader", "mixinextras"])
    profile = {"id": "fabric-loader-0.20.1-26.3", "url": "u", "sha256": "0" * 64, "json": {}}
    monkeypatch.setattr(client_pack, "loader_profile", lambda mc, v: profile)
    lock = client_pack.resolve("26.3", d)
    assert lock["loader"] == {
        "name": "fabric",
        "version": "0.20.1",
        "provides": ["fabricloader", "mixinextras"],
        "profile": profile,
    }
    good, shady = lock["files"]
    assert (
        good["version_id"] == "new" and good["mirror"] and good["sha256"] == hashlib.sha256(blob).hexdigest()
    )
    assert shady["kind"] == "shader" and not shady["mirror"] and shady["tiers"] == ["maximum"]
    assert lock["pending"] == [
        {
            "slug": "none",
            "reason": "only beta builds for Minecraft 26.3 + fabric on Modrinth (newest 9); the packs take "
            "releases only",
        }
    ]
    with pytest.raises(LookupError):
        client_pack.resolve("26.4", d)


def test_gate_verification_of_client_locks(monkeypatch: pytest.MonkeyPatch) -> None:
    assert client_pack.verify(None, None) == []
    assert "new" in client_pack.verify(None, LOCK)[0]
    assert "removed" in client_pack.verify(LOCK, None)[0]
    assert client_pack.verify(LOCK, LOCK) == []
    fewer = json.loads(json.dumps(LOCK))
    fewer["pending"] = []
    assert "set of entries changed" in client_pack.verify(LOCK, fewer)[0]
    # v1.2: the in-house entry is checked against the product's jar, never upstream
    mod = next(f for f in LOCK["files"] if f["slug"] == "smallworld")
    entry = next(m for m in DEF["mods"] if m["slug"] == "smallworld")
    assert client_pack.inhouse_entry(entry, DEF) == mod
    assert client_pack.check_entry(mod, None, "26.3", DEF) == []
    assert "isn't the product's" in client_pack.check_entry(mod | {"size": 1}, mod, "26.3", DEF)[0]
    # a changed entry is re-checked upstream: here the API says the version isn't a release
    head = json.loads(json.dumps(LOCK))
    head["files"][1]["version_id"] = "zzz"
    calls: list[str] = []

    def get_json(url: str) -> dict:
        calls.append(url)
        if "/version/" in url:
            return {
                "project_id": head["files"][1]["project_id"],
                "version_type": "beta",
                "game_versions": ["26.3"],
            }
        return {"id": head["files"][1]["project_id"], "license": {"id": head["files"][1]["licence"]}}

    monkeypatch.setattr(client_pack, "get_json", get_json)
    reasons = client_pack.verify(LOCK, head)
    assert len(reasons) == 1 and "isn't a release" in reasons[0]
    assert all("/version/zzz" in u or "/project/" in u for u in calls)


def test_update_lock_writes_the_client_lock_only_where_wanted(tmp_path: Path) -> None:
    import update_lock

    assert not update_lock.client_enabled(tmp_path)
    (tmp_path / "server.yml").write_text("client: {enabled: true}\n")
    assert update_lock.client_enabled(tmp_path)
    (tmp_path / "server.yml").write_text("client: {enabled: false}\n")
    assert not update_lock.client_enabled(tmp_path)


def test_update_lock_client_flag_creates_a_first_client_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # v1.2 rollout bug: with client.enabled false and no lock, `lock update` never wrote one, and
    # `config set client.enabled true` refused without it. --client breaks the loop.
    import fetch_plugins
    import fetch_server_jar
    import update_lock

    for name in ("server.lock.yml", "plugins.lock.yml"):
        shutil.copy(REPO / "template" / name, tmp_path / name)
    server = yaml.safe_load((REPO / "template/server.lock.yml").read_text())
    monkeypatch.setattr(fetch_server_jar, "resolve", lambda v, paper: server)
    monkeypatch.setattr(fetch_plugins, "resolve", lambda pl, v: pl)
    monkeypatch.setattr(client_pack, "resolve", lambda v: LOCK)
    assert update_lock.main(["--lock-dir", str(tmp_path)]) == 0
    assert not (tmp_path / "client.lock.yml").exists()  # off and no lock: still not written
    assert update_lock.main(["--lock-dir", str(tmp_path), "--client"]) == 0
    assert yaml.safe_load((tmp_path / "client.lock.yml").read_text()) == LOCK
    assert "client: " in capsys.readouterr().out


def _client_ready_repo(tmp_path: Path) -> Path:
    """A committed config repo that may turn the client on, but has no client.lock.yml yet."""
    root = make_config_repo(tmp_path / "c", example_server(), renderable=True)
    (root / "client.lock.yml").unlink(missing_ok=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    for argv in (["init", "-q"], ["add", "-A"], ["commit", "-qm", "seed"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True, env=os.environ | env)
    return root


def test_config_set_client_enabled_writes_and_commits_the_first_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from mcctl import configedit

    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.setenv(k, "t")
    for k in ("GIT_AUTHOR_EMAIL", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(k, "t@t")
    root = _client_ready_repo(tmp_path)
    calls: list[tuple[Path, str]] = []

    def writer(r: Path, version: str) -> subprocess.CompletedProcess[str]:
        calls.append((r, version))
        shutil.copy(REPO / "template/client.lock.yml", r / "client.lock.yml")
        return subprocess.CompletedProcess([], 0, "", "")

    # without a writer: refused, and the hint names the command that makes the lock
    with pytest.raises(configedit.ConfigEditError, match=r"mcctl lock update --client"):
        configedit.set_and_commit(root, "client.enabled", "true")
    msg = configedit.set_and_commit(root, "client.enabled", "true", lock_writer=writer)
    assert calls == [(root.resolve(), configrepo.load(root).server.minecraft.version)]
    assert "new client.lock.yml" in msg and "mcctl apply" in msg
    show = subprocess.run(["git", "-C", str(root), "show", "--name-only", "--format=%s", "HEAD"],
                          capture_output=True, text=True, check=True).stdout.split()  # fmt: skip
    assert "server.yml" in show and "client.lock.yml" in show
    assert configrepo.load(root).server.client.enabled
    # already on: nothing to do, no second lock run
    assert "nothing to do" in configedit.set_and_commit(root, "client.enabled", "true", lock_writer=writer)
    assert len(calls) == 1


def test_config_set_client_enabled_restores_everything_when_the_lock_tool_fails(tmp_path: Path) -> None:
    from mcctl import configedit

    root = _client_ready_repo(tmp_path)
    before = (root / "server.yml").read_text()

    def writer(r: Path, version: str) -> subprocess.CompletedProcess[str]:
        (r / "client.lock.yml").write_text("half: written\n")
        return subprocess.CompletedProcess([], 1, "", "modrinth: 503")

    with pytest.raises(configedit.ConfigEditError, match="modrinth: 503"):
        configedit.set_and_commit(root, "client.enabled", "true", lock_writer=writer)
    assert (root / "server.yml").read_text() == before and not (root / "client.lock.yml").exists()


# --- the mirror publisher ------------------------------------------------------------------------------------


class FakeTransport:
    def __init__(self, upstream: dict[str, bytes], present: set[str] = frozenset()) -> None:  # type: ignore[assignment]
        self.upstream = upstream
        self.present = set(present)
        self.requests: list[http.Request] = []

    def __call__(self, req: http.Request) -> http.Response:
        self.requests.append(req)
        if req.url.startswith("https://api.modrinth.com/v2/project/"):
            return http.Response(200, {}, json.dumps({"license": {"id": "GPL-3.0-only"}}).encode())
        if "/publish/artifacts/" in req.url:
            if req.method == "HEAD":
                return http.Response(200 if req.url in self.present else 404, {}, b"")
            assert req.headers["Authorization"] == "Bearer tok"
            self.present.add(req.url)
            return http.Response(200, {}, b'{"ok":true}')
        if "/publish/client/" in req.url:
            return http.Response(200, {}, b'{"ok":true}')
        if req.url in self.upstream:
            assert "Authorization" not in req.headers  # never a credential to an upstream host
            return http.Response(200, {}, self.upstream[req.url])
        if req.url.startswith("https://github.com/restic/"):
            return http.Response(302, {"location": req.url.replace("github.com", "objects.example")}, b"")
        return http.Response(404, {}, b"")


def test_publish_mirrors_verified_artifacts_and_the_plans(tmp_path: Path) -> None:
    data = example_server() | {"client": {"enabled": True}}
    root = make_config_repo(tmp_path / "c", data, renderable=True)
    repo = configrepo.load(root)
    items, skipped = clientpublish.artifacts(repo, FakeTransport({}))
    whats = [a.what for a in items]
    assert whats[0].startswith("server ") and any(w.startswith("restic ") for w in whats)
    assert any("complementary-unbound" in s for s in skipped)  # licence: the official URL only
    assert any(
        s.startswith("plugin Chunky: licence 'GPL-3.0-only'") for s in skipped
    )  # not on the allow-list
    # one client mod's real bytes aren't here: give each artifact bytes whose hashes we then pin
    upstream: dict[str, bytes] = {}
    patched = []
    for a in items:
        if a.path:  # in-house: the product's real jar, read from the checkout
            patched.append(a)
            continue
        blob = a.what.encode()
        upstream[a.url.replace("github.com", "objects.example") if "restic" in a.what else a.url] = blob
        patched.append(dataclasses.replace(a, hashes={k: hashlib.new(k, blob).hexdigest() for k in a.hashes}))
    t = FakeTransport(upstream)
    lines: list[str] = []
    orig = clientpublish.artifacts
    try:
        clientpublish.artifacts = lambda repo, transport: (patched, skipped)  # type: ignore[assignment]
        clientpublish.launchers = lambda *a, **k: {}  # type: ignore[assignment] # (its own test below)
        counts = clientpublish.publish(repo, "https://hb.example", "tok", t, lines.append)
        assert counts == {"present": 0, "uploaded": len(patched), "failed": 0, "plans": 5, "launchers": 0}
        again = clientpublish.publish(repo, "https://hb.example", "tok", t, lines.append)
        assert again["present"] == len(patched) and again["uploaded"] == 0  # HEAD 200: nothing re-uploaded
        # a file that isn't the pinned one is never mirrored, and the run fails
        bad = [dataclasses.replace(patched[0], hashes={"sha256": "0" * 64})]
        clientpublish.artifacts = lambda repo, transport: (bad, [])  # type: ignore[assignment]
        with pytest.raises(clientpublish.PublishError, match="sha256"):
            clientpublish.publish(repo, "https://hb.example", "tok2", FakeTransport(upstream), lines.append)
    finally:
        clientpublish.artifacts = orig  # type: ignore[assignment]
        clientpublish.launchers = LAUNCHERS  # type: ignore[assignment]
    with pytest.raises(clientpublish.PublishError):
        clientpublish.publish(repo, "https://hb.example", "", t, lines.append)
    # v1.2: with the client on, a workflow that doesn't pass the secret fails the deploy (v1.1 only warned and
    # the packs went stale); without the client it is a warning (only the mirror is missing)
    assert repo.server.client.enabled
    assert clientpublish.run(repo, {}, t) == 1
    off = dataclasses.replace(repo, server=dataclasses.replace(repo.server, client=dataclasses.replace(
        repo.server.client, enabled=False)))  # fmt: skip
    assert clientpublish.run(off, {}, t) == 0


# --- mc-agent's code hand-off --------------------------------------------------------------------------------


UUID = "0f8fad5b-d9cb-469f-a165-70867728950e"
NONCE = "0123456789abcdef0123456789abcdef"


def agent_settings(tmp_path: Path) -> mc_agent.Settings:
    s = mc_agent.Settings.from_env(
        {
            "INGEST_URL": "https://hb.example",
            "AGENT_TOKEN": "t" * 40,
            "COMMAND_HMAC_KEY": "k" * 40,
            "CLIENT": "1",
            "CLIENT_REQUEST_DIR": str(tmp_path / "req"),
            "CLIENT_CODE_FILE": str(tmp_path / "codes.tsv"),
            "STATE_DIRECTORY": str(tmp_path / "state"),
        }
    )
    (tmp_path / "req").mkdir()
    return s


def request_file(tmp_path: Path, uuid: str = UUID, nonce: str = NONCE, name: str = "Steve") -> None:
    (tmp_path / "req" / f"{uuid}.req").write_text(f"mc-client-request 1\n{uuid}\t{name}\t{nonce}\t1\n")


def test_agent_answers_a_request_once_and_never_logs_the_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = agent_settings(tmp_path)
    assert s.client and s.client_code_file == tmp_path / "codes.tsv"
    posts: list[tuple[str, dict]] = []
    audits: list[tuple[str, dict]] = []
    monkeypatch.setattr(mc_agent, "audit", lambda msg, **f: audits.append((msg, f)))

    def post(path: str, body: dict) -> dict:
        posts.append((path, body))
        return {"code": "123456", "expires_ms": 99}

    codes = mc_agent.ClientCodes(s, post, clock=lambda: 1000.0)
    request_file(tmp_path)
    request_file(tmp_path, uuid="not-a-uuid")  # ignored (name doesn't match its uuid / bad uuid)
    (tmp_path / "req" / "x.req").write_text("garbage")
    assert codes.scan() == 1
    assert posts == [("/agent/client-code", {"uuid": UUID, "name": "Steve"})]
    text = (tmp_path / "codes.tsv").read_text()
    assert text == f"mc-client-codes 1\nC\t{UUID}\t{NONCE}\t123456\t99\n"
    assert oct(os.stat(tmp_path / "codes.tsv").st_mode & 0o777) == "0o640"
    assert codes.scan() == 0 and len(posts) == 1  # the same (uuid, nonce) again: answered already
    assert audits == [("client code issued", {"player": "Steve", "uuid": UUID})]
    assert "123456" not in json.dumps(audits)


def test_agent_reports_refusals_and_caps_its_requests(tmp_path: Path) -> None:
    s = agent_settings(tmp_path)

    def refuse(path: str, body: dict) -> dict:
        raise mc_agent.IngestError("POST /agent/client-code: HTTP 429", 429)

    codes = mc_agent.ClientCodes(s, refuse, clock=lambda: 1000.0)
    request_file(tmp_path)
    codes.scan()
    assert f"E\t{UUID}\t{NONCE}\t{mc_agent.CLIENT_REASONS[429]}" in (tmp_path / "codes.tsv").read_text()
    calls = []
    capped = mc_agent.ClientCodes(
        s, lambda p, b: calls.append(1) or {"code": "1", "expires_ms": 1}, clock=lambda: 1.0
    )
    capped.sent = [1.0] * mc_agent.CLIENT_PER_HOUR
    request_file(tmp_path, nonce="f" * 32)
    capped.scan()
    assert not calls  # over the hourly cap: refused locally
    assert "odd answer" not in (tmp_path / "codes.tsv").read_text()
    assert mc_agent.parse_client_request(f"mc-client-request 1\n{UUID}\tSte ve\t{NONCE}\t1\n") is None


def test_agent_logs_why_the_worker_refused_a_code(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """v1.1 rollout: a Worker without the client (no `mcctl apply`) answered 404 and only the code file said why.
    The journal (stderr) gets one plain line per distinct cause, with the fix, never the token."""
    monkeypatch.setattr(mc_agent, "WARN", mc_agent.Warnings())
    s = agent_settings(tmp_path)
    status = {"code": 404}

    def refuse(path: str, body: dict) -> dict:
        raise mc_agent.IngestError(f"POST {path}: HTTP {status['code']}", status["code"])

    codes = mc_agent.ClientCodes(s, refuse, clock=lambda: 1000.0)
    for i in range(3):  # the same cause again: no new line
        request_file(tmp_path, nonce=f"{i:032x}")
        codes.scan()
    err = capsys.readouterr().err.splitlines()
    assert err == [
        "client: /modpack code request failed (POST /agent/client-code: HTTP 404): the Worker has no client set "
        "up (client.enabled needs `mcctl apply`; `mcctl doctor` checks it); players see: the client page isn't "
        "set up on this server yet (retrying quietly)"
    ]
    assert mc_agent.CLIENT_REASONS[404] in (tmp_path / "codes.tsv").read_text()  # the player still sees it
    status["code"] = 502  # a different cause: one more line
    request_file(tmp_path, nonce="e" * 32)
    codes.scan()
    err = capsys.readouterr().err
    assert "HTTP 502): the Worker is unreachable or failing" in err and s.token not in err


# --- mc-brand: /modpack, the join prompt, /view (static; the e2e selftest runs the rules) -------------------------


def test_modpack_and_view_in_the_plugin() -> None:
    plugin = yaml.safe_load((REPO / "plugin/mc-brand/src/main/resources/plugin.yml").read_text())
    assert {"modpack", "view"} <= set(plugin["commands"])
    assert plugin["permissions"]["mcbrand.view"]["default"] is True  # decision: every player may use /view
    mp = (JAVA / "Modpack.java").read_text()
    assert "return enabled && prompt && !optedOut && !hasPack;" in mp  # v1.2: exact
    assert "runTaskAsynchronously(plugin, () -> writeRequest(id, req))" in mp  # file IO off the main thread
    assert "runTaskTimerAsynchronously(plugin, this::poll" in mp
    assert "ATOMIC_MOVE" in mp and "Files.deleteIfExists" in mp
    assert "HttpClient" not in mp and "URLConnection" not in mp  # never talks to the Worker
    assert mc_agent.CLIENT_NONCE.pattern == "^[0-9a-f]{32}$" and 'Pattern.compile("^[0-9a-f]{32}$")' in mp
    view = (JAVA / "View.java").read_text()
    assert "static final int LOWER_AFTER = 2;" in view and "static final int RAISE_AFTER = 6;" in view
    assert "p.setViewDistance(-1)" in view and "p.setSendViewDistance(n)" in view
    assert 'return smooth && !holding ? "raise" : "wait";' in view
    assert "PersistentDataType.INTEGER" in view  # persisted per UUID (the player's own data)
    brand = (JAVA / "BrandPlugin.java").read_text()
    assert "runTaskTimer(this, view::check, every, every)" in brand
    e2e = (REPO / "tests/e2e/brand_check.py").read_text()
    for needle in ("modpack prompt no pack true, pack false, off false, parse true/true",
                   "view decide raise/wait/lower/invalid, guard r-l-----r"):  # fmt: skip
        assert needle in e2e
    # the plugin's request format is what the agent parses
    assert '"mc-client-request 1\\n" + id + "\\t" + name + "\\t" + nonce' in mp
    assert mc_agent.parse_client_request(f"mc-client-request 1\n{UUID}\tSteve\t{NONCE}\t5\n") == (
        UUID,
        "Steve",
        NONCE,
    )


def test_bench_bots_codec() -> None:
    import bench_bots

    for n in (0, 1, 127, 128, 300, 2**31 - 1, -1):
        assert bench_bots.read_varint(bench_bots.varint(n))[0] == n
    assert bench_bots.varint(300) == b"\xac\x02" and bench_bots.string("ab") == b"\x02ab"
    bot = bench_bots.Bot({}, 1, "b", (1.0, 0.0), 16)
    info = bot.client_information()
    assert info.startswith(b"\x05en_us\x10") and len(info) == 6 + 1 + 1 + 1 + 1 + 1 + 1 + 1 + 1


def test_publish_retries_uploads_and_never_blocks_the_deploy_on_one(tmp_path: Path) -> None:
    data = example_server() | {"client": {"enabled": True}}
    repo = configrepo.load(make_config_repo(tmp_path / "c", data, renderable=True))
    blob = b"restic"
    a = clientpublish.Artifact(
        "restic amd64", "r.bz2", "https://up.example/r.bz2", {"sha256": hashlib.sha256(blob).hexdigest()}
    )

    class Flaky(FakeTransport):
        def __init__(self, fails: int) -> None:
            super().__init__({a.url: blob})
            self.fails = fails

        def __call__(self, req: http.Request) -> http.Response:
            if req.method == "PUT" and "/publish/artifacts/" in req.url:
                assert req.timeout == clientpublish.UPLOAD_TIMEOUT
                if self.fails:
                    self.fails -= 1
                    raise http.HttpError("hb.example/publish/artifacts/...: The write operation timed out")
            return super().__call__(req)

    waits: list[float] = []
    orig = clientpublish.artifacts
    try:
        clientpublish.artifacts = lambda repo, transport: ([a], [])  # type: ignore[assignment]
        clientpublish.launchers = lambda *a, **k: {}  # type: ignore[assignment]
        counts = clientpublish.publish(
            repo, "https://hb.example", "tok", Flaky(2), lambda _: None, waits.append
        )
        assert counts["uploaded"] == 1 and counts["failed"] == 0 and waits == [5.0, 10.0]
        lines: list[str] = []
        counts = clientpublish.publish(
            repo, "https://hb.example", "tok", Flaky(9), lines.append, lambda _: None
        )
        assert counts["failed"] == 1 and counts["plans"] == 5  # the plans still go out
        assert any("timed out (after 3 attempts)" in line for line in lines)
    finally:
        clientpublish.artifacts = orig  # type: ignore[assignment]
        clientpublish.launchers = LAUNCHERS  # type: ignore[assignment]


class _Server:
    """A loopback http.server (no TLS) in a thread, standing in for the Worker's PUT endpoint."""

    def __init__(self, handler: type[BaseHTTPRequestHandler]) -> None:
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/publish/artifacts/{'a' * 64}/r.bz2"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def _direct(timeout: float) -> http.Transport:
    opener = http.build_opener(proxies={})  # loopback: no proxy

    def transport(req: http.Request) -> http.Response:
        assert req.timeout == clientpublish.UPLOAD_TIMEOUT
        return http.send(dataclasses.replace(req, timeout=timeout), opener)

    return transport


def test_upload_reports_the_workers_answer_when_it_stops_reading() -> None:
    """The restic stall: the Worker answered (413 here) without reading the body; the client used to block
    writing until its timeout and report "The write operation timed out", hiding the answer."""
    release = threading.Event()

    class Refuse(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            body = json.dumps({"error": "Content-Length over 100 MB"}).encode()
            self.send_response(413)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            self.wfile.flush()
            release.wait(10)  # headers read, body never: the client's writes stall, the answer is waiting

        def log_message(self, *args: object) -> None:
            pass

    blob = b"\0" * (48 << 20)  # more than the loopback socket buffers hold
    with _Server(Refuse) as srv:
        try:
            error = clientpublish.upload(
                _direct(0.5), srv.url, {"Authorization": "Bearer t"}, blob, lambda _: None
            )
        finally:
            release.set()
    assert "413" in error and "Content-Length over 100 MB" in error, error
    assert "timed out" not in error
    assert error.endswith("(after 1 attempt)")  # a refusal isn't retried


def test_upload_timeout_is_an_idle_timeout_not_a_cap_on_the_whole_upload() -> None:
    got: list[int] = []

    class Slow(BaseHTTPRequestHandler):
        def do_PUT(self) -> None:
            left = int(self.headers["Content-Length"])
            while left:
                chunk = self.rfile.read(min(left, 1 << 20))
                left -= len(chunk)
                got.append(len(chunk))
                time.sleep(0.4)  # a slow link: each pause well inside the timeout
            body = b'{"ok":true,"existed":false}'
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    blob = b"\1" * (16 << 20)
    with _Server(Slow) as srv:
        start = time.monotonic()
        error = clientpublish.upload(_direct(4.0), srv.url, {}, blob, lambda _: None)
        took = time.monotonic() - start
    assert error == ""
    assert sum(got) == len(blob)
    # longer than the timeout in total, yet never idle that long (wide margins for a busy CI box)
    assert took > 4.0


# --- v1.2: the launcher zips through the publisher -----------------------------------------------------------


class ZipWorker(FakeTransport):
    """FakeTransport + what the Worker stores: uploaded artifacts and the published plans (GET for the index)."""

    def __init__(self, upstream: dict[str, bytes]) -> None:
        super().__init__(upstream)
        self.stored: dict[str, bytes] = {}
        self.plans: dict[str, dict] = {}

    def __call__(self, req: http.Request) -> http.Response:
        if "/publish/artifacts/" in req.url and req.method == "PUT":
            self.stored[req.url] = req.body or b""
        if "/publish/client/" in req.url:
            name = req.url.rsplit("/", 1)[1]
            self.requests.append(req)
            if req.method == "GET":
                return http.Response(
                    200 if name in self.plans else 404, {}, json.dumps(self.plans.get(name, {})).encode()
                )
            self.plans[name] = json.loads(req.body or b"{}")
            return http.Response(200, {}, b'{"ok":true}')
        return super().__call__(req)


def test_publish_builds_stores_and_reuses_the_launcher_zips(tmp_path: Path) -> None:
    data = example_server() | {"client": {"enabled": True}}
    root = make_config_repo(tmp_path / "c", data, renderable=True)
    lock = json.loads(json.dumps(LOCK))
    upstream: dict[str, bytes] = {}
    for f in lock["files"]:  # every Modrinth file gets small fake bytes, pinned in the lock
        if f.get("source") == "inhouse":
            continue
        blob = f"jar {f['slug']}".encode()
        upstream[f["url"]] = blob
        f.update(
            {a: hashlib.new(a, blob).hexdigest() for a in ("sha1", "sha256", "sha512")} | {"size": len(blob)}
        )
    (root / "client.lock.yml").write_text(yaml.safe_dump(lock, sort_keys=False))
    repo = configrepo.load(root)
    t = ZipWorker(upstream)
    real = clientpublish.artifacts

    def client_only(repo: configrepo.ConfigRepo, transport: http.Transport) -> tuple[list, list]:
        items, skipped = real(repo, transport)
        return [a for a in items if a.what.startswith("client ")], skipped

    orig = clientpublish.artifacts
    try:
        clientpublish.artifacts = client_only  # type: ignore[assignment]
        lines: list[str] = []
        counts = clientpublish.publish(repo, "https://hb.example", "tok", t, lines.append)
        assert counts["launchers"] == 4 and counts["failed"] == 0, lines
        index = t.plans["index.json"]
        for tier in index["tiers"]:
            la = tier["launcher"]
            assert (
                la["dir"] == f"smallworld-{tier['id']}"
                and la["profile_id"] == LOCK["loader"]["profile"]["id"]
            )
            url = f"https://hb.example/publish/artifacts/{la['sha256']}/{la['filename']}"
            zipped = t.stored[url]
            assert hashlib.sha256(zipped).hexdigest() == la["sha256"] and la["size_mb"] >= 0
            jar = (REPO / "client/mod/dist/smallworld.jar").read_bytes()
            with zipfile.ZipFile(io.BytesIO(zipped)) as z:
                mods = [z.read(n) for n in z.namelist() if "/mods/" in n]
            assert jar in mods and set(mods) <= set(upstream.values()) | {jar}  # the pinned bytes
            assert {m["name"] for m in tier["missing"]} >= {"Xaero's Minimap", "Xaero's World Map"}
        # the next deploy: same recipes, zips still stored: nothing downloaded, nothing re-uploaded
        before = len(t.stored)
        t.requests.clear()
        again = clientpublish.publish(repo, "https://hb.example", "tok", t, lines.append)
        assert again["launchers"] == 4 and len(t.stored) == before
        assert not any(r.url in upstream for r in t.requests)
        assert t.plans["index.json"]["tiers"] == index["tiers"]
    finally:
        clientpublish.artifacts = orig  # type: ignore[assignment]
    # a v1.1 lock without the loader profile: no zips, the page offers the .mrpack only
    del lock["loader"]["profile"]
    (root / "client.lock.yml").write_text(yaml.safe_dump(lock, sort_keys=False))
    out: list[str] = []
    assert (
        clientpublish.launchers(configrepo.load(root), {}, "", {}, t, out.append, lambda _: None, {}, [], {})
        == {}
    )
    assert "no loader profile" in out[0]
