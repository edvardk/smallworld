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
    assert "CompassStrip.strip(" in src and "System.out.print(json(out, 0))" in src
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
    # v1.4.1: a turning viewer is redrawn on the next tick (was 4 ticks, 5 updates a second: laggy); still one-shot
    assert re.search(r"static final long FAST_TICKS = 1L;", compass)
    assert "if (fast == null) {" in compass  # several move events in one tick: one task, one redraw
    # the bar's name only when the rendered line differs from the last one sent (turning in place sends nothing)
    render = compass.split("private void render(Player v, BossBar bar)", 1)[1].split("\n    }\n")[0]
    assert "if (!line.equals(lines.get(v.getUniqueId())))" in render
    assert render.count("bar.name(") == 1 and render.index("bar.name(") > render.index("if (!line.equals(")
    plugin = (BRAND / "BrandPlugin.java").read_text()
    assert (
        "public void onMove(PlayerMoveEvent e)" in plugin
        and "public void onTeleport(PlayerTeleportEvent e)" in plugin
    )


def test_tick_timers_never_subtract_a_sentinel() -> None:
    """v1.4 e2e: `now - Integer.MIN_VALUE` (and Long's) overflows to a negative number, so a timer that starts at
    MIN_VALUE and fires on `now - last >= every` never fires: the pocket map never re-centred, the map room never
    painted, PreRender never read meta.json. Timers go through Wayfinding.due / PreRender.due; no field that starts
    at a MIN_VALUE sentinel is ever subtracted."""
    assert FIX["due"] == [True, False, True, True]
    for java in sorted(BRAND.glob("*.java")):
        src = java.read_text()
        for name in re.findall(r"\b(?:int|long) (\w+) = (?:Integer|Long)\.MIN_VALUE;", src):
            assert not re.search(rf"-\s*(?:\w+\.)?{name}\b", src), (  # also `now - m.checked`
                f"{java.name}: {name} starts at MIN_VALUE and is subtracted"
            )
    for java in ("PocketMap.java", "MapRoom.java"):
        src = (BRAND / java).read_text()
        assert not re.search(r"now - last\w+ >=", src), java
    assert "Wayfinding.due(now, lastRecentre, RECENTRE_TICKS)" in (BRAND / "PocketMap.java").read_text()
    assert "Wayfinding.due(now, lastTry, RETRY_TICKS)" in (BRAND / "MapRoom.java").read_text()


def test_pocket_map_centres_on_even_pixels() -> None:
    """v1.4: the pocket map's centre is a multiple of 2 * 2^scale (Wayfinding.snap(x, scale + 1)), so the dither
    parity (k1 + l1) & 1 vanilla draws with equals the tiles' (gx + gz) & 1 (docs/reference.md "The pocket map's
    centre snap"); = map_reference.pocket_centre."""
    for coord, scale, want in FIX["pocket_centre"]:
        step = 2 << scale
        assert want == math.floor(coord) // step * step
        assert (want // (1 << scale)) % 2 == 0
    pm = (BRAND / "PocketMap.java").read_text()
    assert "Wayfinding.snap(at.getX(), sc + 1)" in pm and "Wayfinding.snap(at.getZ(), sc + 1)" in pm
    assert "Wayfinding.snap(p.getX(), 1)" in pm and "Wayfinding.snap(p.getZ(), 1)" in pm
    assert "Wayfinding.snap(at.getX(), sc)" not in pm


def jdiv(a: int, b: int) -> int:
    """Java int division (towards zero)."""
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b > 0) else -q


def test_prerender_window_and_shading() -> None:
    """v1.4: PreRender's window coordinates (gx = cx / 2^s + k1 - 64, tiles of 512) and vanilla's shading."""
    g1, g2, g3, t1, t2, t3, low, lowest, first, early, late = FIX["prerender"]
    # meta.json's re-read: due at once the first time, then once a minute (a sentinel compared by subtraction
    # overflowed and the first read never happened: the v1.4 e2e)
    assert (first, early, late) == (True, False, True)
    assert [g1, g2, g3] == [jdiv(-64, 1) + 0 - 64, jdiv(128, 2) + 127 - 64, jdiv(-4096, 16) + 64 - 64]
    assert [t1, t2, t3] == [-1 // 512, 511 // 512, 512 // 512]

    def shade(rgb: int, m: int) -> int:
        return sum(((rgb >> sh & 255) * m // 255) << sh for sh in (16, 8, 0))

    assert low == shade(0x7FB238, 180) == (89 << 16 | 125 << 8 | 39)  # GRASS, LOW: MapPalette's 89,125,39
    assert lowest == shade(0x7FB238, 135)
    pre = (BRAND / "PreRender.java").read_text()
    for needle in (
        "World.Environment.NETHER",  # never the Nether
        'Files.getAttribute(world, "unix:dev")',  # world identity (a switch: no stale map)
        "runTaskAsynchronously",  # file reads off the main thread
        "MAX_TILES = 16",  # 16 x 256 KB = 4 MB
        "data.length != SIZE",  # a tile is checked before use
        "due(now, m.checked)",
    ):
        assert needle in pre, needle
    assert "= Long.MIN_VALUE" not in pre  # no sentinel compared by subtraction
    pm = (BRAND / "PocketMap.java").read_text()
    assert "prerender.window(" in pm and "private boolean fill(" in pm and "private void sample(" in pm


def test_map_room_geometry() -> None:
    """v1.4: MapRoom's slots (bottom centre anchor, the viewer's right per facing, rows from the top), vanilla's
    map grid centres and the border fit, recomputed independently."""
    right = {"south": (1, 0), "north": (-1, 0), "east": (0, -1), "west": (0, 1)}
    *walls, (_, centres), (_, border) = FIX["map_room"]
    for facing, slots in walls:
        rx, rz = right[facing]
        for i, got in enumerate(slots):
            col, top = i % 4, i // 4
            off = col - (4 - 1) // 2
            assert got == [col, top, 10 + rx * off, 64 + (3 - 1 - top), -20 + rz * off]
    for sx, sz, scale, gx, gz, first, last in centres:
        w = 128 << scale

        def grid(c: int, w: int = w) -> int:
            return (c + 64) // w * w + w // 2 - 64

        assert (gx, gz) == (grid(sx), grid(sz))
        assert first == [grid(sx) - 1 * w, grid(sz) - 1 * w]  # width 4, height 3: column 0 is one to the west
        assert last == [grid(sx) + 2 * w, grid(sz) + 1 * w]
        assert gx % (2 << scale) == 0 or w // 2 - 64 < 0  # vanilla centres are even pixels too
    for radius, width, height, bpp, scale, first, last in border:
        px = 128 * min(width, height)
        assert bpp == max(1, -(-2 * radius // px))
        assert scale == min(4, bpp.bit_length() - 1)
        size = width * 128
        assert first == (0 - size // 2) * bpp + bpp // 2 and last == (size - 1 - size // 2) * bpp + bpp // 2
        assert first <= -radius + bpp and last >= radius - 2 * bpp  # the border fits the wall


def test_map_room_is_fixed_invulnerable_and_idempotent() -> None:
    room = (BRAND / "MapRoom.java").read_text()
    for needle in (
        "fr.setFixed(true)",
        "fr.setInvulnerable(true)",
        "fr.setPersistent(false)",  # never saved with the world: no duplicates, a changed config moves it
        "fr.getPersistentDataContainer().set(tag, PersistentDataType.STRING, id)",
        "v.setLocked(true)",  # vanilla never draws on them
        "super(false);",  # non-contextual: one picture for everyone
        "public void onBreak(HangingBreakEvent e)",
        "public void onDamage(EntityDamageEvent e)",
        "public void onInteract(PlayerInteractEntityEvent e)",
        "PersistentDataType.INTEGER_ARRAY",  # map ids kept in the world: no new map_N.dat per start
        "if (ours(e) && !current(e))",  # the sweep
    ):
        assert needle in room, needle
    assert room.count("Bukkit.createMap(") == 1
    plugin = (BRAND / "BrandPlugin.java").read_text()
    assert "mapRoom.loaded(e.getEntities())" in plugin and "mapRoom.selftest()" in plugin


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
    assert out["pocket_map"] == {"enabled": True, "prerender": True}  # v1.4: map.enabled defaults to true
    assert out["map_room"]["enabled"] is False  # v1.4: off unless configured
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


# --- v1.5: the client compass (the mod draws it; client/mod/WIRE.md "The client compass") --------------------------


def _mode(want: bool, capable: bool, pending: bool) -> str:
    if not want:
        return "NONE"
    return "CLIENT" if capable else "WAIT" if pending else "SERVER"


def _capability(caps: int | None, ours: bool, channels: bool) -> str:
    if caps is None:
        return "UNKNOWN" if ours else "NO"
    if not caps & 1:
        return "NO"
    return "YES" if channels else "UNKNOWN"


def test_client_compass_decision_tables() -> None:
    """Who draws: a mod client with the capability gets the client compass, everyone else the boss bar."""
    for key, mode in FIX["compass_mode"].items():
        want, capable, pending = (v == "true" for v in key.split("/"))
        assert mode == _mode(want, capable, pending), key
    for key, cap in FIX["capability"].items():
        c, ours, channels = key.split("/")
        assert cap == _capability(None if c == "null" else int(c), ours == "true", channels == "true"), key
    # the plan's cases: vanilla -> boss bar, mod 1.0.0 -> boss bar, mod 1.1.0 -> the mod draws
    assert FIX["capability"]["null/false/false"] == "NO"  # vanilla: no hello, no channels
    assert FIX["capability"]["0/true/true"] == "NO"  # mod 1.0.0: hello without the capability
    assert FIX["capability"]["1/true/true"] == "YES"  # mod 1.1.0
    assert FIX["compass_mode"]["true/false/false"] == "SERVER"
    assert FIX["compass_mode"]["true/true/false"] == "CLIENT"
    assert FIX["compass_mode"]["false/true/false"] == "NONE"  # /compass off wins for the mod too


def _friends(hexstr: str) -> tuple[str, list[tuple[str, str, int, int, int]]]:
    from test_client_mod import Reader

    r = Reader(bytes.fromhex(hexstr))
    dim, n = r.utf(), r.varint()
    return dim, [(r.utf(), r.utf(), r.varint(), r.varint(), r.varint()) for _ in range(n)]


def test_friends_payload_is_what_the_boss_bar_shows() -> None:
    """The positions the mod gets: in range only, in 1/16 block, nearest first, elsewhere by name (no position)."""
    for case in FIX["client_compass"]:
        vx, _vy, vz, world = case["viewer"]
        dim, friends = _friends(case["friends"])
        assert dim == world
        here = [f for f in friends if f[1] == ""]
        away = [f for f in friends if f[1] != ""]
        assert friends == here + away and [f[0] for f in away] == sorted(f[0] for f in away)
        assert all(f[2:] == (0, 0, 0) for f in away)
        dists = [math.hypot(f[2] / 16 - vx, f[4] / 16 - vz) for f in here]
        assert dists == sorted(dists) and all(d <= case["config"][0] for d in dists)
        # every player the boss bar places is in the payload
        names = {f[0] for f in friends}
        for kind, text, *_ in case["placed"]:
            if kind in ("PLAYER", "FOCUS"):
                assert text.lstrip("▼").split(" ")[0].rstrip("↑↓") in names
    dim, friends = _friends(FIX["selftest_friends"])
    assert [f[0] for f in friends] == ["Alex", "Sam", "Kim"]  # "Far" is out of range


def test_selftest_prints_the_client_compass_fixtures() -> None:
    """tests/e2e/brand_check.py looks for the same payload bytes on a real Purpur (the selftest line)."""
    e2e = (REPO / "tests/e2e/brand_check.py").read_text().replace('"\n            "', "")
    assert f"friends {FIX['selftest_friends']}, drawn " in e2e
    compass = (BRAND / "Compass.java").read_text()
    assert 'Wayfinding.friends("minecraft:overworld", 0.5, 0.5, 1000, Wayfinding.SELFTEST_SCENE)' in compass
    fixtures = (
        REPO / "plugin/mc-brand/src/test/java/io/github/edvardk/mcbrand/WayfindingFixtures.java"
    ).read_text()
    assert '"minecraft:overworld", 0.5, 0.5, 1000, Wayfinding.SELFTEST_SCENE)' in fixtures
