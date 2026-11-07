"""v1.3 Part C item 8: wayfinding in mc-brand (the friend compass, the pocket map, /help, the help hologram) and
the client-mod precedence.

The pure logic is plugin/mc-brand's Wayfinding.java; WayfindingFixtures (src/test/java, run by
`scripts/build_plugin.py check` with the pinned JDK, never in the jar) prints its answers for fixed inputs to
plugin/mc-brand/src/test/wayfinding-fixtures.json. Here the arithmetic is recomputed independently in Python and
the strip's layout rules are checked on the same data; the Bukkit wiring is checked statically (the plugins have
no JVM test harness on a server), its selftest lines by tests/e2e/brand_check.py on a real Purpur.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import pytest
import yaml
from conftest import REPO, example_server

from mcctl import render, schema

BRAND = REPO / "plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand"
BUILD = yaml.safe_load((REPO / "plugin/mc-brand/build.yml").read_text())
FIX: dict[str, Any] = json.loads((REPO / BUILD["fixtures"]).read_text())
PLUGIN_YML = yaml.safe_load((REPO / "plugin/mc-brand/src/main/resources/plugin.yml").read_text())
C = FIX["strip_constants"]


# --- an independent implementation of the arithmetic -------------------------------------------------------------


def bearing(dx: float, dz: float) -> float:
    deg = math.degrees(math.atan2(dx, -dz))
    return deg + 360 if deg < 0 else deg


def facing(yaw: float) -> float:
    return (yaw + 180) % 360


def relative(b: float, f: float) -> float:
    r = (b - f) % 360
    return r - 360 if r > 180 else r


def distance(d: float, rounding: int) -> str:
    v = math.floor(d / rounding + 0.5) * rounding  # Java Math.round: half up
    return f"<{rounding}m" if v < rounding else f"{v}m"


def arrow(dy: float, threshold: int) -> str:
    return "↑" if dy > threshold else "↓" if dy < -threshold else ""


WIDTHS = {" ": 4, "`": 3, "l": 3, "<": 5, ">": 5, "f": 5, "k": 5, "@": 7, "~": 7}
WIDTHS |= dict.fromkeys("!,.:;|i'·", 2) | dict.fromkeys('"()*I[]t{}', 4)


def width(s: str) -> int:
    return sum(WIDTHS.get(ch, 6) for ch in s)


def java_int(v: float) -> int:  # (int) cast: towards zero
    return int(v)


def cursor(x: float, z: float, yaw: float, cx: int, cz: int, scale: int) -> list[Any]:
    blocks = 1 << scale
    fx, fz = (x - cx) / blocks, (z - cz) / blocks
    clamp = lambda v: max(-128, min(127, v))  # noqa: E731
    if -63 <= fx <= 63 and -63 <= fz <= 63:
        r = yaw + (-8 if yaw < 0 else 8)
        return [
            clamp(java_int(fx * 2 + 0.5)),
            clamp(java_int(fz * 2 + 0.5)),
            java_int(r * 16 / 360) & 15,
            True,
        ]
    ex = -128 if fx <= -63 else 127 if fx >= 63 else clamp(java_int(fx * 2 + 0.5))
    ez = -128 if fz <= -63 else 127 if fz >= 63 else clamp(java_int(fz * 2 + 0.5))
    return [ex, ez, 0, False]


# --- the fixtures against it ---------------------------------------------------------------------------------------


def test_fixtures_are_wired_into_the_build() -> None:
    assert BUILD["fixtures_main"] == "io.github.edvardk.mcbrand.WayfindingFixtures"
    src = (
        REPO / "plugin/mc-brand/src/test/java/io/github/edvardk/mcbrand/WayfindingFixtures.java"
    ).read_text()
    assert "Wayfinding.strip(" in src and "System.out.print(json(out, 0))" in src
    script = (REPO / "scripts/build_plugin.py").read_text()
    assert (
        'build_cfg.get("fixtures_main")' in script
        and "differs from {cfg['fixtures_main']}'s output" in script
    )
    import zipfile

    with zipfile.ZipFile(REPO / BUILD["jar"]) as z:
        assert not any("Fixtures" in n for n in z.namelist())  # tests never ship


def test_angles() -> None:
    for dx, dz, want in FIX["bearing"]:
        assert bearing(dx, dz) == pytest.approx(want, abs=1e-5)
    for yaw, want in FIX["facing"]:
        assert facing(yaw) == pytest.approx(want, abs=1e-6)
    for b, f, want in FIX["relative"]:
        assert relative(b, f) == pytest.approx(want, abs=1e-6)
    # Minecraft's yaw: 0 south, 90 west, -90 east, 180 north
    assert [facing(y) for y in (0, 90, -90, 180)] == [180, 270, 90, 0]


def test_distance_arrow_and_dimensions() -> None:
    for d, r, want in FIX["distance"]:
        assert distance(d, r) == want
    for dy, t, want in FIX["arrow"]:
        assert arrow(dy, t) == want
    names = {"minecraft:the_nether": "Nether", "minecraft:the_end": "End", "minecraft:overworld": "Overworld"}
    for key, want in FIX["dimension"]:
        assert names.get(key, key.split(":", 1)[1]) == want
    for s, want in FIX["width"]:
        assert width(s) == want


def test_map_cursors_and_snap() -> None:
    for (x, z, yaw, cx, cz, scale), want in FIX["cursor"]:
        assert cursor(x, z, yaw, cx, cz, scale) == want
    for coord, scale, want in FIX["snap"]:
        assert want == (math.floor(coord) // (1 << scale)) * (1 << scale)


@pytest.mark.parametrize("case", FIX["strip"], ids=lambda c: c["what"])
def test_strip_layout(case: dict[str, Any]) -> None:
    w, half, focus_deg, spacing = C["width_px"], C["half_fov"], C["focus_deg"], C["min_spacing_px"]
    max_range, rounding, threshold = case["config"]
    face = facing(case["yaw"])
    here = [t for t in case["targets"] if not t[4] and math.hypot(t[1], t[3]) <= max_range]
    away = sorted((t for t in case["targets"] if t[4]), key=lambda t: t[0].lower())
    here.sort(key=lambda t: (math.hypot(t[1], t[3]), t[0]))
    faced = None
    for t in here:
        r = relative(bearing(t[1], t[3]), face)
        if abs(r) <= focus_deg and (faced is None or abs(r) < abs(faced[1]) - 1e-9):
            faced = (t, r)
    if faced:
        t = faced[0]
        want = f"▼{t[0]} {distance(math.hypot(t[1], t[3]), rounding)}{arrow(t[2], threshold)}"
        assert case["focus"] == want
    else:
        assert case["focus"] == ""
    placed = case["placed"]
    pixel = lambda rel: w / 2 + rel / half * (w / 2)  # noqa: E731
    for i, (kind, text, left, wd) in enumerate(placed):
        assert wd == width(text) and 0 <= left and left + wd <= w, (kind, text)
        if i:
            prev = placed[i - 1]
            assert left >= prev[2] + prev[3] + spacing, (prev, text)  # no overlap, sorted
        if kind == "FOCUS":
            assert abs(left + wd / 2 - w / 2) <= 1  # centred on the screen
        elif 0 < left < w - wd:  # not clamped at an edge: centred on its bearing
            if kind in ("NORTH", "CARDINAL"):
                rel = relative(["N", "NE", "E", "SE", "S", "SW", "W", "NW"].index(text) * 45, face)
            elif kind == "PLAYER":
                t = next(t for t in here if text.rstrip("↑↓") == t[0])
                rel = relative(bearing(t[1], t[3]), face)
                assert abs(rel) <= half and text == t[0] + arrow(t[2], threshold)
            else:
                continue
            assert abs(left + wd / 2 - pixel(rel)) <= 1, (text, left)
    names = {p[1].rstrip("↑↓") for p in placed if p[0] == "PLAYER"}
    assert not names & {t[0] for t in case["targets"] if t[4] or math.hypot(t[1], t[3]) > max_range}
    # the line: mirror gap + strip + away suffix; the strip proper spans the full width (gap rounding <= 3 px)
    segs = case["segments"]
    seg_w = sum(width(t) + (len(t) if k == "BOLD_GAP" else 0) for k, t in segs)
    assert seg_w == case["line_width"]
    suffix = ""
    if away:
        parts = [
            f"{t[0]} ({ {'minecraft:the_nether': 'Nether', 'minecraft:the_end': 'End'}[t[4]] })" for t in away
        ]
        suffix = " | " + ", ".join(parts[:2]) + (f" +{len(away) - 2}" if len(away) > 2 else "")
        assert segs[-1] == ["AWAY", suffix]
    strip_w = seg_w - 2 * width(suffix)
    assert abs(strip_w - w) <= 3 * (len(placed) + 1), (strip_w, w)
    assert "".join(t for _, t in segs) == case["plain"]


def test_precedence_and_channel_detection() -> None:
    """The plan's three cases, selected by fake channel registrations (the client's minecraft:register list)."""
    detect = {ch: (ours, xaero) for ch, ours, xaero in FIX["channels"]}
    assert detect["smallworld:config"] == (True, False)  # the mod's receiver (client/mod WIRE.md)
    assert detect["xaerominimap:main"] == detect["xaeroworldmap:main"] == (False, True)
    assert detect["xaerobetterpvp:main"] == (False, True)  # the xaero* namespace fallback
    assert detect["notxaero:main"] == detect["minecraft:brand"] == (False, False)
    table = {(o, x): (mods, compass, tip) for o, x, mods, compass, tip in FIX["precedence"]}

    def client(channels: list[str]) -> tuple[str, bool, bool]:
        ours = any(detect[c][0] for c in channels)
        xaero = any(detect[c][1] for c in channels)
        return table[(ours, xaero)]

    assert client(["smallworld:config", "xaerominimap:main"]) == ("smallworld+xaero", False, False)
    assert client(["xaeroworldmap:main"]) == ("xaero", True, False)
    assert client(["minecraft:brand"]) == ("none", True, True)
    assert client(["smallworld:config"]) == ("smallworld", True, False)
    xaero = (BRAND / "XaeroMap.java").read_text()
    assert 'MINIMAP = "xaerominimap:main"' in xaero and 'WORLD_MAP = "xaeroworldmap:main"' in xaero


# --- the Bukkit wiring (static) -------------------------------------------------------------------------------------


def test_precedence_is_re_decided_late_and_overridden_by_the_player() -> None:
    plugin = (BRAND / "BrandPlugin.java").read_text()
    reg = plugin.split("public void onRegisterChannel(PlayerRegisterChannelEvent e)", 1)[1].split(
        "\n    }\n"
    )[0]
    assert "Wayfinding.isOurs(e.getChannel()) || Wayfinding.isXaero(e.getChannel())" in reg
    assert "compass.clientsChanged(e.getPlayer())" in reg
    hello = plugin.split("void onSmallworldHello(Player p)", 1)[1].split("\n    }\n")[0]
    assert "compass.clientsChanged(p)" in hello
    assert "p.getListeningPluginChannels().stream().anyMatch(Wayfinding::isXaero)" in plugin
    assert "Wayfinding.suggestMap(hasOurMod(p), hasXaero(p))" in plugin  # the join tip
    compass = (BRAND / "Compass.java").read_text()
    wants = compass.split("boolean wants(Player p)", 1)[1].split("\n    }\n")[0]
    assert "case ON -> true;" in wants and "case OFF -> false;" in wants  # /compass on|off always wins
    assert "Wayfinding.compassByDefault(plugin.hasOurMod(p), plugin.hasXaero(p))" in wants


def test_compass_does_no_per_tick_work() -> None:
    compass = (BRAND / "Compass.java").read_text()
    assert "runTaskTimer" not in compass  # only one-shot tasks, scheduled when someone moved
    assert "runTaskLater(plugin, this::flushFast, FAST_TICKS)" in compass
    assert "runTaskLater(plugin, this::flushSlow, SLOW_TICKS)" in compass
    assert re.search(r"static final long FAST_TICKS = 4L;", compass)
    assert "if (!line.equals(lines.get(v.getUniqueId())))" in compass  # the bar only when the text changed
    plugin = (BRAND / "BrandPlugin.java").read_text()
    assert (
        "public void onMove(PlayerMoveEvent e)" in plugin
        and "public void onTeleport(PlayerTeleportEvent e)" in plugin
    )


def test_pocket_map_is_one_per_player_and_drop_safe() -> None:
    pm = (BRAND / "PocketMap.java").read_text()
    for needle in (
        "p.getPersistentDataContainer().set(idKey, PersistentDataType.INTEGER, view.getId())",  # one view
        "e.getItemDrop().remove()",  # dropped: gone, /map is free
        "e.getDrops().removeIf(this::isPocketMap)",  # never on death
        "e.getRightClicked() instanceof ItemFrame",
        "e.getInventory().setResult(null)",  # no map cloning in the crafting grid
        'it.getType().name().endsWith("BUNDLE")',
        "public boolean isExplorerMap()",  # abstract in the pinned API
        "super(true);",  # contextual: only the owner's render changes anything
        "MapCursor.Type.PLAYER_OFF_MAP",
        "canvas.setPixelColor(x, y, null)",  # transparent: vanilla's terrain
    ):
        assert needle in pm, needle
    assert "getBasePixel(" not in pm and "setPixel(" not in pm  # the byte calls are deprecated for removal
    assert "Bukkit.createMap(" in pm and pm.count("Bukkit.createMap(") == 1


def test_help_lists_every_player_command_from_one_place() -> None:
    help_src = (BRAND / "Help.java").read_text()
    listed = set(re.findall(r'new Entry\("(\w+)"', help_src))
    assert "new Entry(s.drinkCommand()" in help_src
    commands = set(PLUGIN_YML["commands"]) - {"help", "drink"}
    assert commands == listed, commands ^ listed
    assert PLUGIN_YML["commands"]["help"]["aliases"] == ["commands"]
    for name in ("compass", "map", "help"):
        assert PLUGIN_YML["permissions"][PLUGIN_YML["commands"][name]["permission"]]["default"] is True
    assert "ClickEvent.suggestCommand(e.suggestion())" in help_src  # click types it, never runs it
    assert '"/minecraft:help"' in help_src
    plugin = (BRAND / "BrandPlugin.java").read_text()
    assert 'case "help" -> sender.sendMessage(Help.chat(s, Help.visible(s, sender)));' in plugin
    assert "new Holograms(this, s, Help.hologram(s))" in plugin  # the hologram: the same source


# --- server.yml ---------------------------------------------------------------------------------------------------


def test_defaults_and_render() -> None:
    cfg = schema.load_server(example_server())
    b = cfg.branding
    assert (b.compass.enabled, b.compass.default_on, b.compass.max_range) == (True, True, 1000)
    assert (b.compass.distance_rounding, b.compass.height_threshold, b.pocket_map.enabled) == (5, 8, True)
    out = render.brand_plugin(cfg)
    assert out["compass"] == {
        "enabled": True,
        "default_on": True,
        "max_range": 1000,
        "distance_rounding": 5,
        "height_threshold": 8,
    }
    assert out["pocket_map"] == {"enabled": True}
    settings = (BRAND / "Settings.java").read_text()
    for key, default in (("max_range", 1000), ("distance_rounding", 5), ("height_threshold", 8)):
        assert f'compass.getInt("{key}", {default})' in settings  # the plugin's fallback = the schema's


def test_spawn_help_hologram() -> None:
    data = example_server() | {"branding": {"holograms": [{"at": "spawn", "help": True}]}}
    out = render.brand_plugin(schema.load_server(data))["holograms"]
    assert out == [
        {
            "world": "minecraft:overworld",
            "at": "spawn",
            "lines": ["Welcome to {server}"],
            "health": False,
            "help": True,
        }
    ]
    settings = (BRAND / "Settings.java").read_text()
    assert 'boolean spawn = "spawn".equals(m.get("at"));' in settings
    assert 'boolean help = Boolean.TRUE.equals(m.get("help"));' in settings
    holo = (BRAND / "Holograms.java").read_text()
    assert "Location sp = w.getSpawnLocation();" in holo and "private void follow(Spec spec, Live l)" in holo


@pytest.mark.parametrize(
    ("compass", "needle"),
    [
        ({"max_range": 8}, "must be >= 16"),
        ({"distance_rounding": 0}, "must be >= 1"),
        ({"height_threshold": 100}, "must be <= 64"),
        ({"radius": 5}, "unknown key"),
    ],
)
def test_compass_values_are_checked(compass: dict, needle: str) -> None:
    with pytest.raises(schema.SchemaError) as e:
        schema.load_server(example_server() | {"branding": {"compass": compass}})
    assert any(needle in x for x in e.value.errors), e.value.errors


@pytest.mark.parametrize("name", ["compass", "map", "help", "commands"])
def test_drink_command_cannot_take_the_new_commands(name: str) -> None:
    assert schema.field_errors("branding.drink.command", name)


def test_full_example_has_a_spawn_help_board() -> None:
    cfg = schema.load_server(yaml.safe_load((REPO / "examples/full/server.yml").read_text()))
    assert any(h.at == "spawn" and h.help for h in cfg.branding.holograms)
    assert cfg.branding.compass.max_range == 2000


def test_spawn_hologram_heals_and_reports_where_it_is() -> None:
    """v1.3-next CI: after `forceload add ~ ~` the spawn help hologram never spawned ("spawned 0"): the console's
    `~ ~` is vanilla's server-wide respawn data (RconConsoleSource -> MinecraftServer.getRespawnData), the
    plugin's spawn is Bukkit's World.getSpawnLocation (Paper's per-world PaperLevelOverrides), so the e2e
    force-loaded a chunk the plugin didn't use. The plugin reports its own position and heals from the HUD task."""
    holo = (BRAND / "Holograms.java").read_text()
    tick = holo.split("void tick(Msg.Speed speed, int players)", 1)[1].split("\n    }\n")[0]
    assert "ensure(i);" in tick  # not only on EntitiesLoadEvent
    ensure = holo.split("private void ensure(int i)", 1)[1].split("\n    }\n")[0]
    assert ensure.index("if (!missing)") < ensure.index("Msg.Speed.of(")  # cheap when the display exists
    assert '", help at [" + helpAt() + "]"' in holo
    check = (REPO / "tests/e2e/brand_check.py").read_text()
    assert 'r"help at \\[(-?\\d+) (-?\\d+) (-?\\d+)"' in check
    assert 'rcon(box, f"forceload add {x} {z}")' in check and "forceload add ~ ~" not in check
    assert "time.monotonic() + 30" in check
