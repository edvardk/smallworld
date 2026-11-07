"""v1.2 Part B, server side: the smallworld channels in mc-brand (exact pack detection, the outdated-pack line,
the config payload), the client mod's FPS reports through mc-telemetry -> mc-agent -> the Worker.

The plugins have no Java unit tests: the rules are checked here statically, the selftest lines by
tests/e2e/brand_check.py on a real server, the Worker side in watchdog/test/experience.test.mjs."""

from __future__ import annotations

import base64
import json
import re
import shutil
import struct
import sys
import uuid
from pathlib import Path

import yaml
from conftest import REPO, example_server, make_config_repo

from mcctl import clientpack, configrepo, render, schema

sys.path.insert(0, str(REPO / "scripts"))
import telemetry

BRAND = REPO / "plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand"
TELEMETRY = REPO / "plugin/mc-telemetry/src/main/java/io/github/edvardk/mctelemetry"
CLIENT_WIRE = REPO / "client/mod/src/main/java/io/github/edvardk/smallworld/client/Wire.java"
WIRE_MD = (REPO / "client/mod/WIRE.md").read_text()
UUID = "4f2a0000-0000-4000-8000-000000001c41"
T0 = 1_793_000_000_000 // 60000 * 60000


def body(path: Path) -> str:
    return path.read_text().split("\n", 1)[1]  # without the package line


# --- the smallworld channels in mc-brand ------------------------------------------------------------------------


def test_the_plugin_uses_the_client_mods_wire_class_verbatim() -> None:
    """WIRE.md: one byte layout, shared. mc-brand carries the client's Wire.java with only its package changed."""
    assert (BRAND / "Wire.java").read_text().startswith("package io.github.edvardk.mcbrand;\n")
    assert body(BRAND / "Wire.java") == body(CLIENT_WIRE)


def test_channels_are_the_ones_wire_md_names() -> None:
    sw = (BRAND / "Smallworld.java").read_text()
    tel = (TELEMETRY / "TelemetryPlugin.java").read_text()
    for channel in ("smallworld:hello", "smallworld:fps", "smallworld:config"):
        assert f"`{channel}`" in WIRE_MD
    assert 'HELLO = "smallworld:hello"' in sw and 'CONFIG = "smallworld:config"' in sw
    assert "registerIncomingPluginChannel(plugin, HELLO, this)" in sw
    assert "registerOutgoingPluginChannel(plugin, CONFIG)" in sw
    # the FPS reports are telemetry: mc-telemetry listens itself (no deprecated metadata API between plugins)
    assert 'FPS_CHANNEL = "smallworld:fps"' in tel
    assert "registerIncomingPluginChannel(this, FPS_CHANNEL, this)" in tel
    assert "getMetadata" not in tel and "setMetadata" not in sw
    # Paper sends only on a channel the client registered: the config goes again on its registration
    assert "PlayerRegisterChannelEvent" in (BRAND / "BrandPlugin.java").read_text()


def test_pack_detection_is_exact_and_replaces_the_brand_rule() -> None:
    mp = (BRAND / "Modpack.java").read_text()
    assert "return enabled && prompt && !optedOut && !hasPack;" in mp
    assert "getClientBrandName" not in mp and "vanilla(brand)" not in mp
    assert "p.isOnline() && !smallworld.hasPack(p)" in mp  # decided when the prompt is due (the hello is in)
    sw = (BRAND / "Smallworld.java").read_text()
    assert 'TOKEN = Pattern.compile("^[A-Za-z0-9._+-]{1,64}$")' in sw  # = WIRE.md's token rule
    assert "[A-Za-z0-9._+-]{1,64}" in WIRE_MD
    # status: NONE (no pack file), CURRENT, OUTDATED (another version for that tier), UNKNOWN (tier not offered)
    for needle in ("return PackStatus.NONE;", "return PackStatus.UNKNOWN;",
                   "current.equals(pack) ? PackStatus.CURRENT : PackStatus.OUTDATED"):  # fmt: skip
        assert needle in sw
    e2e = (REPO / "tests/e2e/brand_check.py").read_text()
    for needle in (
        "modpack prompt no pack true, pack false, off false, parse true/true",
        "smallworld hello true, pack CURRENT/OUTDATED/NONE/UNKNOWN, config 010501",
    ):
        assert needle in e2e


def test_config_payload_bytes() -> None:
    """protocol 1, interval, flags (bit 0 = HUD), each a VarInt: what the selftest prints for (5 s, HUD on)."""
    assert bytes([1, 5, 1]).hex() == "010501"
    settings = (BRAND / "Settings.java").read_text()
    assert 'clamp(client.getInt("fps_interval_seconds", 5), 2, 60)' in settings  # = the client's own clamp
    assert 'client.getBoolean("fps_hud", true)' in settings
    assert 'packs(client.getConfigurationSection("packs"))' in settings


def test_schema_and_render_of_the_fps_settings() -> None:
    data = example_server() | {"client": {"enabled": True, "fps": {"interval_seconds": 10, "hud": False}}}
    cfg = schema.load_server(data)
    client = render.brand_plugin(cfg)["client"]
    assert client["fps_interval_seconds"] == 10 and client["fps_hud"] is False
    defaults = render.brand_plugin(schema.load_server(example_server()))["client"]
    assert (defaults["fps_interval_seconds"], defaults["fps_hud"]) == (5, True)
    for bad in (1, 61):
        data = example_server() | {"client": {"fps": {"interval_seconds": bad}}}
        try:
            schema.load_server(data)
        except schema.SchemaError:
            continue
        raise AssertionError(f"interval {bad} accepted")


def test_current_pack_versions_are_rendered_from_the_client_lock(tmp_path: Path) -> None:
    data = example_server() | {"client": {"enabled": True}}
    root = make_config_repo(tmp_path / "c", data, renderable=True)
    shutil.copy(REPO / "template/client.lock.yml", root / "client.lock.yml")
    repo = configrepo.load(root)
    gv = render.ansible_vars(repo)
    packs = gv["mc_client_packs"]
    plans = render.client_plans(repo)
    assert set(packs) == set(clientpack.TIERS) and all(packs[t] == plans[t]["version_id"] for t in packs)
    # the same id the pack writes into config/smallworld-pack.json (what the mod's hello carries)
    for tier, plan in plans.items():
        pack_file = next(o for o in plan["overrides"] if o["path"].endswith(clientpack.PACK_ID_FILE))
        assert json.loads(base64.b64decode(pack_file["data"])) == {"tier": tier, "pack": packs[tier]}
        assert re.fullmatch(r"[A-Za-z0-9._+-]{1,64}", packs[tier])
    off = configrepo.load(make_config_repo(tmp_path / "off", example_server(), renderable=True))
    assert render.ansible_vars(off)["mc_client_packs"] == {}
    tpl = (REPO / "ansible/roles/plugins/templates/mc-brand.yml.j2").read_text()
    assert "'packs': mc_client_packs | default({})" in tpl


# --- the FPS reports through the telemetry path -----------------------------------------------------------------


def test_telemetry_plugin_writes_fps_only_for_players_with_the_mod() -> None:
    writer = (TELEMETRY / "Writer.java").read_text()
    assert 'if (s.fps[i] >= 0) {\n                sb.append(",\\"fps\\":").append(s.fps[i]);' in writer
    tel = (TELEMETRY / "TelemetryPlugin.java").read_text()
    assert "return Math.max(0, Math.min(10_000, value));" in tel  # = Wire.MAX_FPS
    assert "clientFps.remove(event.getPlayer().getUniqueId());" in tel


def snap(seq: int, fps: object) -> dict:
    p = {"u": UUID, "n": "Mia", "ping": 40, "brand": "fabric", "view": 12, "send": 10, "loc": "en_gb", "w": "world",
         "cx": 0, "cz": 0, "ent": 3, "glide": False, "tips": True, "join": T0 - 60000}  # fmt: skip
    if fps is not None:
        p["fps"] = fps
    raw = {"v": 1, "seq": seq, "ts": T0 + seq * 10000, "win_ms": 10000, "srv": {"tps": 20.0}, "pl": [p]}
    return telemetry.parse_snapshot(json.dumps(raw).encode())  # type: ignore[return-value]


def test_agent_minute_row_carries_average_and_lowest_fps() -> None:
    rows = telemetry.summarize_players([snap(0, 60), snap(1, 24), snap(2, None), snap(3, 12), snap(4, -1)])
    assert (rows[0]["fps"], rows[0]["fps_min"]) == (32, 12)
    assert snap(0, 99_999)["pl"][0]["fps"] == 10_000 and snap(0, "x")["pl"][0]["fps"] is None
    none = telemetry.summarize_players([snap(0, None), snap(1, None)])
    assert "fps" not in none[0] and "fps_min" not in none[0]  # no mod: the Worker keeps the F3 hint


def test_worker_reads_fps_and_builds_the_diagnosis() -> None:
    worker = (REPO / "watchdog/worker.js").read_text()
    assert "fps: n(p.fps), fps_min: n(p.fps_min)" in worker  # cleanPlayer keeps them
    assert "fps_fair: 30" in worker and "fps_poor: 20" in worker
    assert "Server fine, your PC: ${fps} FPS" in worker
    mjs = (REPO / "watchdog/test/experience.test.mjs").read_text()
    assert "'server fine, your PC' with the measured frame rate" in mjs


def test_wire_doc_says_the_server_side_is_built() -> None:
    assert "Not built yet" not in WIRE_MD
    assert (
        yaml.safe_load((REPO / "plugin/mc-brand/src/main/resources/plugin.yml").read_text())["name"]
        == "mc-brand"
    )


# --- Xaero's maps: fair play and friends on the map -------------------------------------------------------------

XAERO = (BRAND / "XaeroMap.java").read_text()


def nbt_utf(s: str) -> bytes:
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def tracked(
    player: uuid.UUID, remove: bool, x: float = 0, y: float = 0, z: float = 0, dim: str = ""
) -> bytes:
    """An independent encoder of Xaero's packet 2 as the 26.3 jars read it (ClientboundTrackedPlayerPacket.read:
    FriendlyByteBuf.readNbt of an unnamed compound; r boolean, i via XaeroNbtUtils.getUUID = int[4] or long[2],
    x/y/z doubles, d an Identifier string), after the xaero-lib index byte (PacketHandlerFull.encodePacket)."""
    out = (
        bytes([2, 10, 1])
        + nbt_utf("r")
        + bytes([int(remove)])
        + bytes([11])
        + nbt_utf("i")
        + struct.pack(">i", 4)
    )
    out += player.bytes  # 4 big-endian ints, most significant first = UUIDUtil.uuidToIntArray
    if not remove:
        for k, v in (("x", x), ("y", y), ("z", z)):
            out += bytes([6]) + nbt_utf(k) + struct.pack(">d", v)
        out += bytes([8]) + nbt_utf("d") + nbt_utf(dim)
    return out + b"\0"


SELFTEST_UUID = uuid.UUID(int=(0x0123456789ABCDEF << 64) | 0xFEDCBA9876543210)
TRACKED_HEX = tracked(SELFTEST_UUID, False, 1.5, 64, -2.25, "minecraft:overworld").hex()
UNTRACKED_HEX = tracked(SELFTEST_UUID, True).hex()


def test_xaero_payloads_match_the_26_3_protocol() -> None:
    assert TRACKED_HEX.startswith("020a01000172000b00016900000004" + "0123456789abcdeffedcba9876543210")
    assert UNTRACKED_HEX == "020a01000172010b000169000000040123456789abcdeffedcba987654321000"
    # the selftest prints the Java encoder's bytes for the same inputs; the e2e check compares them
    e2e = re.sub(r'"\s+"', "", (REPO / "tests/e2e/brand_check.py").read_text())  # joined literals
    assert f"xaero tracked {TRACKED_HEX} untracked {UNTRACKED_HEX} handshake 0100000003" in e2e
    assert "new UUID(0x0123456789abcdefL, 0xfedcba9876543210L)" in XAERO
    assert "tracked(u, false, 1.5, 64, -2.25," in XAERO and '"minecraft:overworld")) + " untracked "' in XAERO
    for needle in ('MINIMAP = "xaerominimap:main"', 'WORLD_MAP = "xaeroworldmap:main"', "NETWORK = 3",
                   "LEVEL_ID = 0", "HANDSHAKE = 1", "TRACKED = 2", "RESET = 3"):  # fmt: skip
        assert needle in XAERO


def test_fair_play_code_is_literal_text_in_a_component() -> None:
    code = "§f§a§i§r§x§a§e§r§o"
    assert code == "§f§a§i§r§x§a§e§r§o"
    assert 'FAIR_PLAY = "\\u00a7f\\u00a7a\\u00a7i\\u00a7r\\u00a7x\\u00a7a\\u00a7e\\u00a7r\\u00a7o"' in XAERO
    assert ".append(Component.text(FAIR_PLAY))" in XAERO
    # never through legacy formatting (it would eat the section signs)
    for src in BRAND.glob("*.java"):
        assert "LegacyComponentSerializer" not in src.read_text(), src.name
    assert "fair play code true" in (REPO / "tests/e2e/brand_check.py").read_text()


def test_map_is_server_yml_configurable_and_off_without_the_client() -> None:
    cfg = schema.load_server(example_server() | {"client": {"enabled": True, "map": {"players": False}}})
    assert render.brand_plugin(cfg)["map"] == {"players": False, "fair_play": True}
    assert render.brand_plugin(schema.load_server(example_server()))["map"] == {
        "players": True,
        "fair_play": True,
    }
    assert "return s.clientEnabled() && (s.mapPlayers() || s.mapFairPlay());" in XAERO
    assert "GameMode.SPECTATOR" in XAERO and "isInvisible()" in XAERO  # never reveals spectators / hiders
    assert "getPersistentDataContainer()" in XAERO  # a stable level id per world save (Xaero's map storage)


# --- vanilla social game rules -----------------------------------------------------------------------------------


def test_social_gamerules_are_the_26_3_names_and_rendered_with_defaults() -> None:
    known = set((REPO / "server-config/keys/26.3/gamerules.txt").read_text().split())
    assert {"locator_bar", "players_sleeping_percentage", "keep_inventory"} <= known
    assert "keepInventory" not in known  # 26.x renamed them all to snake_case
    gv = render.group_vars(schema.load_server(example_server()))
    assert gv["mc_gamerules"] == {"locator_bar": "true", "players_sleeping_percentage": "50"}
    data = example_server() | {"gameplay": {"locator_bar": False, "sleeping_percentage": 100}}
    data["minecraft"] = data["minecraft"] | {
        "gamerules": {"minecraft:keep_inventory": True, "respawn_radius": 0}
    }
    rules = render.group_vars(schema.load_server(data))["mc_gamerules"]
    assert rules == {"keep_inventory": "true", "locator_bar": "false", "players_sleeping_percentage": "100",
                     "respawn_radius": "0"}  # fmt: skip


def schema_errors(data: dict) -> list[str]:
    try:
        schema.load_server(data)
    except schema.SchemaError as e:
        return e.errors
    return []


def test_gamerule_names_are_checked_against_the_jar() -> None:
    for rules, needle in (
        ({"keepInventory": True}, "not a game rule of 26.3"),
        ({"locator_bar": False}, "set it with gameplay.locator_bar"),
        ({"players_sleeping_percentage": 10}, "set it with gameplay.sleeping_percentage"),
    ):
        data = example_server()
        data["minecraft"] = data["minecraft"] | {"gamerules": rules}
        errors = schema_errors(data)
        assert any(needle in e for e in errors), (rules, errors)
    data = example_server()
    data["minecraft"] = data["minecraft"] | {"gamerules": {"keep_inventory": True}}
    assert schema_errors(data) == []
    over = example_server()
    over["minecraft"] = over["minecraft"] | {"overrides": {"gamerules": {"x": 1}}}
    assert any("unknown config file" in e for e in schema_errors(over))


def test_gamerules_are_applied_idempotently_in_every_dimension(fake_server) -> None:
    import mc_rcon

    state = {("minecraft:overworld", "locator_bar"): "false", ("minecraft:the_nether", "locator_bar"): "true"}

    def handler(cmd: str) -> str:
        m = re.fullmatch(r"execute in (\S+) run gamerule (\w+)(?: (\S+))?", cmd)
        assert m, cmd
        dim, name, value = m.groups()
        if dim == "minecraft:the_end":
            return "Unknown dimension 'minecraft:the_end'"
        if value is not None:
            state[(dim, name)] = value
            return f"Game rule {name} is now set to {value}"
        return f"Game rule {name} is currently set to {state.get((dim, name), 'true')}"

    srv = fake_server(handler)
    dims = ["minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"]
    assert mc_rcon.gamerule("locator_bar", "true", dims) is True
    assert state[("minecraft:overworld", "locator_bar")] == "true"
    assert "execute in minecraft:the_nether run gamerule locator_bar true" not in srv.commands  # already set
    assert mc_rcon.gamerule("locator_bar", "true", dims) is False
    for bad in (("locator_bar", "yes"), ("Bad Name", "true"), ("x;stop", "1")):
        try:
            mc_rcon.gamerule(*bad, dims)
        except mc_rcon.RconError:
            continue
        raise AssertionError(bad)
    tasks = (REPO / "ansible/roles/minecraft/tasks/main.yml").read_text()
    assert "mc_rcon.py gamerule --name {{ item.key }} --value {{ item.value }}" in tasks
    assert "changed_when: \"'CHANGED' in mc_gamerule.stdout\"" in tasks


# --- AxGraves ------------------------------------------------------------------------------------------------------


def test_axgraves_is_locked_by_default_with_a_managed_overlay() -> None:
    lock = yaml.safe_load((REPO / "template/plugins.lock.yml").read_text())["plugins"]
    ax = next(p for p in lock if p["name"] == "AxGraves")
    assert (ax["source"], ax["project"]) == ("modrinth", "axgraves")
    assert ax["url"].startswith("https://cdn.modrinth.com/") and re.fullmatch(r"[0-9a-f]{64}", ax["sha256"])
    assert ("AxGraves", "modrinth", "axgraves") in {
        (p.name, p.source, p.project) for p in schema.load_server(example_server()).plugins
    }
    example = yaml.safe_load((REPO / "examples/config-repo/plugins.lock.yml").read_text())["plugins"]
    assert ax in example  # scripts/sync_example.py
    overlay = yaml.safe_load((REPO / "server-config/plugins/AxGraves.yml.j2").read_text())
    known = set((REPO / "server-config/keys/26.3/plugin-AxGraves.txt").read_text().split())
    assert {"despawn-time-seconds", "interact-only-own", "update-notifier.enabled"} <= known
    assert overlay["despawn-time-seconds"] == 900 and overlay["interact-only-own"] is True
    assert overlay["update-notifier"] == {"enabled": False, "on-join": False}
    e2e = (REPO / "tests/e2e/deploy_check.py").read_text()
    assert "def social(box: str)" in e2e and '"AxGraves" in plugins' in e2e


# --- spawn holograms ---------------------------------------------------------------------------------------------

HOLO = (BRAND / "Holograms.java").read_text()


def test_holograms_schema_validation_and_render() -> None:
    holo = {
        "x": 10,
        "y": 70,
        "z": -5,
        "world": "nether",
        "lines": ["Hi {server}", "{players} at {tps} {glyph}"],
    }
    cfg = schema.load_server(
        example_server() | {"branding": {"holograms": [holo, {"x": 0, "y": 64, "z": 0}]}}
    )
    out = render.brand_plugin(cfg)["holograms"]
    assert out == [
        {
            "world": "minecraft:the_nether",
            "x": 10,
            "y": 70,
            "z": -5,
            "lines": holo["lines"],
            "health": False,
            "help": False,
        },
        {
            "world": "minecraft:overworld",
            "x": 0,
            "y": 64,
            "z": 0,
            "lines": ["Welcome to {server}"],
            "health": False,
            "help": False,
        },
    ]
    assert render.brand_plugin(schema.load_server(example_server()))["holograms"] == []
    for bad, needle in (
        ({"x": 0, "y": 64, "z": 0, "lines": ["{ping}"]}, "unknown placeholder {ping}"),
        ({"x": 0, "y": 64, "z": 0, "lines": []}, "1 to 8 lines"),
        ({"x": 0, "y": 64, "z": 0, "lines": ["a"] * 9}, "1 to 8 lines"),
        ({"x": 0, "y": 400, "z": 0}, "branding.holograms[0].y"),
        ({"y": 64, "z": 0}, "branding.holograms[0]: needs x, y and z, or at: spawn"),
        ({"at": "spawn", "x": 1}, "at: spawn and x/y/z both given"),
        ({"at": "home"}, "branding.holograms[0].at"),
    ):
        errors = schema_errors(example_server() | {"branding": {"holograms": [bad]}})
        assert any(needle in e for e in errors), (bad, errors)
    many = [{"x": i, "y": 64, "z": 0} for i in range(11)]
    assert any("at most 10" in e for e in schema_errors(example_server() | {"branding": {"holograms": many}}))


def test_holograms_follow_the_research_rules() -> None:
    for needle in (
        "w.spawn(at, TextDisplay.class, d -> {",
        "d.text(text);",
        "d.setBillboard(Display.Billboard.CENTER);",
        "d.setPersistent(false);",  # never saved with the world
        "getPersistentDataContainer().set(tag, PersistentDataType.INTEGER, i)",  # the PDC tag for sweeps
        "if (!t.equals(l.plainText)) {",  # setText only when the text changed
        "d.setVisibleByDefault(visible);",  # the {glyph} variant only for players with the pack
    ):
        assert needle in HOLO, needle
    brand = (BRAND / "BrandPlugin.java").read_text()
    assert "public void onEntitiesLoad(EntitiesLoadEvent e)" in brand  # respawn on chunk/entities load
    settings = (BRAND / "Settings.java").read_text()
    assert 'holograms(cfg.getMapList("holograms"))' in settings and "x + 0.5, y + 0.5, z + 0.5" in settings
