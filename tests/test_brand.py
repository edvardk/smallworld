"""P17 branding: server.yml `branding:`, its rendering, the art (icon + resource pack) and the mc-brand plugin's
wiring (settings keys, commands, the Worker's pack route, the deploy's pins)."""

import dataclasses
import hashlib
import io
import json
import re
import sys
import zipfile

import pytest
import yaml
from conftest import REPO, example_server, make_config_repo

from mcctl import brandpack, configrepo, render, schema

sys.path.insert(0, str(REPO / "scripts"))
import brand_art

FULL = REPO / "examples/full/server.yml"
GV = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
JAVA = REPO / "plugin/mc-brand/src/main/java/io/github/edvardk/mcbrand"


def load(**branding: object) -> schema.ServerConfig:
    data = example_server()
    data["branding"] = branding
    return schema.load_server(data)


def errors(**branding: object) -> list[str]:
    with pytest.raises(schema.SchemaError) as e:
        load(**branding)
    return e.value.errors


# --- schema ------------------------------------------------------------------------------------------------------


def test_defaults_are_generic() -> None:
    cfg = schema.load_server(example_server())
    b = cfg.branding
    assert b.enabled and b.name == "Alex's server"  # derived from minecraft.motd
    assert b.rules == schema.DEFAULT_RULES and len(b.rules) == 4
    assert not any("wine" in r.lower() for r in b.rules)  # a deployment's theme, not the product's
    assert not b.drink.enabled and not b.resource_pack.enabled and b.discord_invite is None
    assert b.drink.name == "Alex's server Reserve" and b.drink.command == "drink"
    assert (b.drink.texture, b.resource_pack.glyph) == (None, None)  # the product's neutral art
    assert dataclasses.astuple(b.colors) == ("#FFFFFF", "#55AAFF", "#2E6FB0", "#C8D0D8")
    assert (b.drink.color, b.drink.lore) == ("#3A6EA5", ["Never runs dry."])
    assert b.resource_pack.prompt == "Textures for this server's items (optional)"
    assert b.resource_pack.description == "Item textures for this server's mc-brand plugin"
    assert cfg.host.timezone == schema.Host().timezone == "UTC"
    assert not b.hud.default_on and b.hud.enabled and b.hud.default_mode == "top"
    assert render.motd(cfg) == cfg.minecraft.motd  # no branding.motd: minecraft.motd as before


def test_full_example_branding() -> None:
    cfg = schema.load_server(yaml.safe_load(FULL.read_text()))
    b = cfg.branding
    assert b.name == "Lighthouse" and (
        b.colors.text,
        b.colors.accent,
        b.colors.secondary,
        b.colors.subtle,
    ) == (
        "#F4F1EA",
        "#E4572E",
        "#1D3557",
        "#A8DADC",
    )
    assert b.rules == [*schema.DEFAULT_RULES[:3], "Have fun {glyph}"]
    assert b.discord_invite == "https://discord.gg/lighthouse" and b.icon == "art/server-icon.png"
    assert b.drink.enabled and b.drink.name == "Cocoa Mug" and b.drink.command == "cocoa"
    assert b.resource_pack.enabled and b.resource_pack.description.startswith("The cocoa mug")
    assert (b.drink.texture, b.resource_pack.glyph) == ("art/cocoa-mug.png", "art/glyph.png")
    assert b.join.first_join == "Welcome to {server}, {player}! {glyph} Warm up by the lamp and stay a while"
    assert b.join.welcome_back == "Welcome back, {player} — last seen {ago} ago"
    assert render.panel_title(cfg) == "Lighthouse"
    lines = render.motd(cfg).split("\n")
    assert len(lines) == 2 and lines[0].startswith("§f§lLight§c§lhouse") and "&" not in render.motd(cfg)
    for rel, size in ((b.icon, 64), (b.drink.texture, 16), (b.resource_pack.glyph, 8)):
        w, h, _ = brand_art.decode_png((FULL.parent / rel).read_bytes())
        assert (w, h) == (size, size), rel
    plugin = render.brand_plugin(cfg)  # v1.2: mc-brand reads {glyph} and the drink's command itself
    assert plugin["rules"][-1] == "Have fun {glyph}" and "{glyph} Warm up" in plugin["join"]["first_join"]
    assert plugin["drink"]["command"] == "cocoa"


def test_nothing_restricts_farms() -> None:
    # Decision (2026-11-02): no lag/mob-farm rule, no named boss, in the defaults and the full example.
    for rules in (schema.DEFAULT_RULES, yaml.safe_load(FULL.read_text())["branding"]["rules"]):
        text = " ".join(rules)
        assert not re.search(r"(?i)\b(farms?|lag|admins?|boss|staff)\b|\bEd\b", text), text


@pytest.mark.parametrize(
    ("branding", "needle"),
    [
        ({"motd": ["a", "b", "c"]}, "at most 2 lines"),
        ({"motd": ["&4" + "x" * 60]}, "60 visible characters"),
        ({"colors": {"accent": "red"}}, "doesn't match"),
        ({"discord_invite": "http://discord.gg/abc"}, "doesn't match"),
        ({"discord_invite": "https://evil.example/abc"}, "doesn't match"),
        ({"rules": [f"rule {i}" for i in range(21)]}, "at most 20 rules"),
        ({"rules": ["line\nbreak"]}, "doesn't match"),
        ({"drink": {"lore": ["a", "b", "c", "d", "e"]}}, "at most 4 lore lines"),
        ({"drink": {"cooldown_seconds": 1}}, "must be >= 5"),
        ({"drink": {"command": "rules"}}, "one of mc-brand's own commands"),
        ({"drink": {"command": "modpack"}}, "one of mc-brand's own commands"),
        ({"drink": {"command": "Cocoa"}}, "doesn't match"),
        ({"drink": {"command": "tea-time"}}, "doesn't match"),
        ({"drink": {"command": "ab"}}, "doesn't match"),
        ({"drink": {"texture": "../mug.png"}}, "doesn't match"),
        ({"drink": {"texture": "art/mug.gif"}}, "doesn't match"),
        ({"resource_pack": {"glyph": "/etc/glyph.png"}}, "doesn't match"),
        ({"resource_pack": {"description": "two\nlines"}}, "doesn't match"),
        ({"join": {"sound": "Not A Key"}}, "doesn't match"),
        ({"icon": "../outside.png"}, "doesn't match"),
        ({"website": "https://example.com/ x"}, "doesn't match"),
        ({"rules": ["{{ lookup('pipe', 'id') }}"]}, "doesn't match"),  # would be templated by Ansible
        ({"join": {"first_join": "hi {% raw %}"}}, "doesn't match"),
        ({"motd": ["{{ x }}"]}, "Ansible would template it"),
    ],
)
def test_invalid_values_are_refused(branding: dict, needle: str) -> None:
    assert any(needle in e for e in errors(**branding)), errors(**branding)


def test_motd_codes_and_newline() -> None:
    cfg = load(motd=["&4&lRed &rplain", "&6gold"])
    assert render.motd(cfg) == "§4§lRed §rplain\n§6gold"
    assert render.motd(load(enabled=False, motd=["&4x"])) == cfg.minecraft.motd  # branding off: untouched


def test_json_schema_has_branding() -> None:
    js = json.loads((REPO / "schema/server.schema.json").read_text())
    props = js["properties"]["branding"]["properties"]
    assert {
        "name",
        "colors",
        "motd",
        "icon",
        "rules",
        "discord_invite",
        "join",
        "hud",
        "drink",
        "resource_pack",
    } <= set(props)
    assert {"command", "texture"} <= set(props["drink"]["properties"])
    assert {"glyph", "description"} <= set(props["resource_pack"]["properties"])
    assert props["drink"]["properties"]["command"]["not"] == {"enum": list(schema.BRAND_COMMANDS)}
    assert js == schema.json_schema()  # regenerated (python -m mcctl schema)


# --- render --------------------------------------------------------------------------------------------------------


def java_keys() -> set[str]:
    """Every key Settings.load reads (cfg.getX("k"), section("k"), <section>.getX("k"))."""
    src = (JAVA / "Settings.java").read_text()
    body = src.split("static Settings load(", 1)[1].split("\n    }\n", 1)[0]
    sections = dict(re.findall(r'ConfigurationSection (\w+) = section\(cfg, "(\w+)"\)', body))
    keys = set()
    for obj, key in re.findall(r'(\w+)\.get(?:String|Boolean|Int|StringList|MapList)\("(\w+)"', body):
        keys.add(f"{sections[obj]}.{key}" if obj in sections else key)
    # v1.4: branding.map_room is read by its own method (room.getX("k") -> map_room.k)
    room = src.split("static MapRoom.Spec mapRoom(", 1)[1].split("\n    }\n", 1)[0]
    for key in re.findall(r'room\.(?:get(?:String|Boolean|Int|IntegerList)|isInt)\("(\w+)"', room):
        keys.add(f"map_room.{key}")
    return keys


def leaf_keys(d: dict, prefix: str = "") -> set[str]:
    out = set()
    for k, v in d.items():
        out |= leaf_keys(v, f"{prefix}{k}.") if isinstance(v, dict) else {prefix + k}
    return out


def test_plugin_settings_keys_match_the_plugin() -> None:
    cfg = schema.load_server(yaml.safe_load(FULL.read_text()))
    rendered = render.brand_plugin(cfg)
    # v1.1: the /modpack hand-off paths are product constants, added by the template (not server.yml); v1.4 the
    # pre-render's directory and colours too
    product = {"client.request_dir", "client.code_file", "pocket_map.prerender_dir", "pocket_map.colors_file"}
    # v1.4: map_room carries at/offset or x/y/z (the full example uses at: spawn)
    explicit = {"map_room.x", "map_room.y", "map_room.z"}
    assert leaf_keys(rendered) | product | explicit == java_keys()
    tpl = (REPO / "ansible/roles/plugins/templates/mc-brand.yml.j2").read_text()
    assert "'request_dir': mc_client_request_dir, 'code_file': mc_client_code_file" in tpl
    assert "'pocket_map': {'prerender_dir': mc_map_dir, 'colors_file': mc_map_colors}" in tpl
    assert (
        rendered["drink"]["enabled"] is True and rendered["discord_invite"] == "https://discord.gg/lighthouse"
    )
    assert render.brand_plugin(load())["discord_invite"] == ""  # none set: empty for the plugin
    assert rendered["hud"]["tab_header"] == "Lighthouse"


def test_pack_prompt_is_a_json_text_component() -> None:
    cfg = schema.load_server(yaml.safe_load(FULL.read_text()))
    pack = render.brand_pack(cfg)
    assert pack["enabled"] is True
    assert json.loads(pack["prompt"]) == {"text": "Lighthouse textures: the cocoa mug", "color": "#E4572E"}
    assert render.brand_pack(load(enabled=False, resource_pack={"enabled": True}))["enabled"] is False


def icon_png(size: int) -> bytes:
    return brand_art.png([[(1, 2, 3, 255)] * size for _ in range(size)])


@pytest.mark.parametrize("command", ["wine", "cocoa", "tea", "drink"])
def test_drink_command_accepts_its_own_names(command: str) -> None:
    assert load(drink={"command": command}).branding.drink.command == command


def test_own_pack_art_is_checked(tmp_path) -> None:
    # v1.2: checked at render time like the icon (used byte for byte by the pack: plans/generalisation.md §5.4)
    art = {"drink": {"texture": "art/mug.png"}, "resource_pack": {"glyph": "art/glyph.png"}}
    root = make_config_repo(tmp_path / "c", example_server() | {"branding": art}, renderable=True)
    (root / "art").mkdir()
    with pytest.raises(render.RenderError, match=r"branding\.drink\.texture: art/mug\.png is not a file"):
        render.files(configrepo.load(root))
    (root / "art/mug.png").write_bytes(icon_png(16))
    with pytest.raises(
        render.RenderError, match=r"branding\.resource_pack\.glyph: art/glyph\.png is not a file"
    ):
        render.files(configrepo.load(root))
    (root / "art/glyph.png").write_bytes(icon_png(8))
    repo = configrepo.load(root)
    assert render.pack_art(repo) == {"texture": icon_png(16), "glyph": icon_png(8)}
    render.files(repo)
    (root / "art/glyph.png").write_bytes(icon_png(16))
    with pytest.raises(render.RenderError, match="16x16; the glyph needs 8x8"):
        render.files(configrepo.load(root))
    (root / "art/glyph.png").write_bytes(icon_png(8))
    (root / "art/mug.png").write_bytes(icon_png(16)[:-12] + bytes(9000))
    with pytest.raises(render.RenderError, match="at most 8192"):
        render.files(configrepo.load(root))
    (root / "art/mug.png").write_bytes(b"GIF89a" + bytes(40))
    with pytest.raises(render.RenderError, match=r"branding\.drink\.texture: art/mug\.png is not a PNG"):
        render.files(configrepo.load(root))
    assert render.pack_art(configrepo.load(make_config_repo(tmp_path / "d", renderable=True))) == {}


def test_own_icon_is_rendered_next_to_the_inventory(tmp_path) -> None:
    data = example_server() | {"branding": {"icon": "art/icon.png"}}
    root = make_config_repo(tmp_path / "c", data, renderable=True)
    (root / "art").mkdir()
    (root / "art/icon.png").write_bytes(icon_png(64))
    repo = configrepo.load(root)
    files = render.files(repo)
    assert files[render.ICON_FILE] == icon_png(64)
    out = tmp_path / "out"
    render.render(repo, out)
    assert (out / render.ICON_FILE).read_bytes() == icon_png(64)
    gv = yaml.safe_load((out / render.GROUP_VARS_FILE).read_text())
    assert gv["mc_brand_icon_file"] == "files/server-icon.png"  # relative to the inventory's directory
    (root / "art/icon.png").write_bytes(icon_png(32))
    with pytest.raises(render.RenderError, match="64x64"):
        render.files(configrepo.load(root))
    (root / "art/icon.png").write_bytes(b"GIF89a" + bytes(40))
    with pytest.raises(render.RenderError, match="not a PNG"):
        render.files(configrepo.load(root))


def test_rendered_pack_is_the_config_art_byte_for_byte(tmp_path) -> None:
    """v1.2: render builds the pack from the config repo's PNGs, never re-encoded (examples/full: own icon,
    texture and glyph), names it by its sha1 and hands the sha1 to Ansible; the product's art fills the gaps."""
    root = make_config_repo(tmp_path / "full", yaml.safe_load(FULL.read_text()), renderable=True)
    (root / "art").mkdir(exist_ok=True)
    for f in (FULL.parent / "art").iterdir():
        (root / "art" / f.name).write_bytes(f.read_bytes())
    repo = configrepo.load(root)
    out = tmp_path / "out"
    render.render(repo, out)
    gv = yaml.safe_load((out / render.GROUP_VARS_FILE).read_text())
    sha1 = gv["mc_brand_pack_sha1"]
    data = (out / render.PACK_DIR / f"{sha1}.zip").read_bytes()
    assert brandpack.sha1(data) == sha1 and render.files(repo)[render.PACK_DIR / f"{sha1}.zip"] == data
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert z.read("pack.png") == (FULL.parent / "art/server-icon.png").read_bytes()
        assert (
            z.read("assets/mcbrand/textures/item/wine_glass.png")
            == (FULL.parent / "art/cocoa-mug.png").read_bytes()
        )
        assert z.read("assets/mcbrand/textures/font/wine.png") == (FULL.parent / "art/glyph.png").read_bytes()
        assert json.loads(z.read("pack.mcmeta"))["pack"]["description"].startswith("The cocoa mug")
    # the staged Worker assets: the product's panel files and exactly this pack, at the tfvars' path
    tf = json.loads((out / render.TFVARS_FILE).read_text())
    assets = out / render.WATCHDOG_ASSETS_DIR
    assert tf["watchdog_assets_dir"] == str(assets.resolve())
    assert (assets / "index.html").read_bytes() == (REPO / "watchdog/panel/index.html").read_bytes()
    assert [p.name for p in (assets / "pack").iterdir()] == [f"{sha1}.zip"]
    # a second render with other art replaces the pack (no stale zip is uploaded)
    (root / "art/glyph.png").write_bytes(icon_png(8))
    render.render(configrepo.load(root), out)
    (new,) = (assets / "pack").iterdir()
    assert (
        new.stem != sha1
        and new.stem == yaml.safe_load((out / render.GROUP_VARS_FILE).read_text())["mc_brand_pack_sha1"]
    )


def test_pack_off_or_neutral(tmp_path, config_repo) -> None:
    repo = configrepo.load(config_repo)  # the template: resource_pack off
    assert render.brand_pack_zip(repo) is None and render.ansible_vars(repo)["mc_brand_pack_sha1"] == ""
    out = tmp_path / "out"
    render.render(repo, out)
    assert not (out / render.PACK_DIR).exists() and (out / render.WATCHDOG_ASSETS_DIR / "panel.js").is_file()
    data = example_server() | {"branding": {"resource_pack": {"enabled": True}}}
    on = configrepo.load(make_config_repo(tmp_path / "c", data, renderable=True))
    assert render.brand_pack_zip(on) == neutral_pack()  # nothing of its own: the product's neutral pack
    assert render.ansible_vars(on)["mc_brand_pack_sha1"] == brandpack.sha1(neutral_pack())


def test_no_own_icon_means_the_products(config_repo) -> None:
    repo = configrepo.load(config_repo)
    assert render.ICON_FILE not in render.files(repo)
    assert render.ansible_vars(repo)["mc_brand_icon_file"] == ""


# --- art -----------------------------------------------------------------------------------------------------------


def test_art_is_reproducible_and_pinned() -> None:
    assert brand_art.main(["check"]) == 0
    assert brand_art.pins() == {"mc_brand_icon_sha256": GV["mc_brand_icon_sha256"]}
    assert hashlib.sha256(brand_art.ICON.read_bytes()).hexdigest() == GV["mc_brand_icon_sha256"]
    # v1.2: the pack is rendered from the config's art; the product neither commits nor pins one
    assert "mc_brand_pack_sha1" not in GV and not (REPO / "watchdog/panel/pack").exists()
    assert sorted(p.name for p in brandpack.ART_DIR.iterdir()) == ["drink.png", "glyph.png"]


def test_icon_and_texture_sizes() -> None:
    assert brand_art.decode_png(brand_art.ICON.read_bytes())[:2] == (64, 64)
    assert brand_art.decode_png(brand_art.TEXTURE.read_bytes())[:2] == (16, 16)
    assert brand_art.decode_png(brand_art.GLYPH.read_bytes())[:2] == (8, 8)
    assert brand_art.decode_png(brand_art.PREVIEW_ICON.read_bytes())[:2] == (512, 512)  # 8x
    assert brand_art.decode_png(brand_art.PREVIEW_DRINK.read_bytes())[:2] == (128, 128)  # 8x
    for f in (brand_art.ICON, brand_art.TEXTURE, brand_art.GLYPH):  # within render's limits (own art)
        assert f.stat().st_size <= (
            render.ICON_MAX_BYTES if f == brand_art.ICON else render.PACK_ART_MAX_BYTES
        )


def neutral_pack() -> bytes:
    return brandpack.pack_zip({}, schema.BrandingResourcePack().description)


def test_pack_contents() -> None:
    data = neutral_pack()
    assert data == neutral_pack()  # reproducible: the same art gives the same bytes (and sha1)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        assert names == sorted(names)
        assert all(i.compress_type == zipfile.ZIP_STORED for i in z.infolist())
        meta = json.loads(z.read("pack.mcmeta"))["pack"]
        assert meta["min_format"] == meta["max_format"] == brandpack.PACK_FORMAT == 97  # 26.3
        assert meta["description"] == "Item textures for this server's mc-brand plugin"
        assert z.read("pack.png") == brand_art.ICON.read_bytes()
        assert z.read("assets/mcbrand/textures/item/wine_glass.png") == brand_art.TEXTURE.read_bytes()
        assert z.read("assets/mcbrand/textures/font/wine.png") == brand_art.GLYPH.read_bytes()
        assert "pack_format" not in meta  # deprecated from format 65 on (26.3 server's PackFormat)
        potion = json.loads(z.read("assets/minecraft/items/potion.json"))["model"]
        assert (
            potion["property"] == "minecraft:custom_model_data"
            and potion["fallback"] == brandpack.VANILLA_POTION
        )
        (case,) = potion["cases"]
        assert case["when"] == brandpack.MODEL_KEY
        model = case["model"]["model"]  # mcbrand:item/wine_glass
        ns, path = model.split(":")
        assert f"assets/{ns}/models/{path}.json" in names
        tex = json.loads(z.read(f"assets/{ns}/models/{path}.json"))["textures"]["layer0"]
        assert f"assets/{tex.split(':')[0]}/textures/{tex.split(':')[1]}.png" in names
    # the plugin tags the drink with the same custom_model_data string (a frozen v1.1 id)
    assert f'LEGACY_MODEL_STRING = "{brandpack.MODEL_KEY}"' in (JAVA / "Drink.java").read_text()


def test_grid_uses_only_the_palette_and_no_gold() -> None:
    for grid, size in ((brand_art.DRINK_GRID, 16), (brand_art.GLYPH_GRID, 8)):
        assert len(grid) == size and all(len(r) == size for r in grid)
        assert set("".join(grid)) <= set(brand_art.PALETTE)
    gold = brand_art.hexc("#D4AF37")[:3]
    icon = brand_art.icon_pixels()
    assert all(p[3] == 255 for row in icon for p in row)  # opaque in the server list
    assert icon[0][0][:3] == brand_art.hexc(brand_art.ICON_BORDER)[:3]
    assert icon[32][4][:3] == brand_art.hexc(brand_art.ICON_BG)[:3]
    # neutral: no colour of a deployment's look (the v1.1 product art was one deployment's)
    for wine in ("#B03A5B", "#7B1E3A", "#561228"):
        assert brand_art.hexc(wine) not in brand_art.PALETTE.values()
    for color in [*brand_art.PALETTE.values(), *(p for row in icon for p in row)]:
        assert sum(abs(a - b) for a, b in zip(color[:3], gold, strict=True)) > 60 or color[3] == 0


def test_text_palette_has_no_gold() -> None:
    for cfg in (schema.load_server(example_server()), schema.load_server(yaml.safe_load(FULL.read_text()))):
        colors = dataclasses.asdict(cfg.branding.colors)
        assert "#D4AF37" not in {c.upper() for c in colors.values()}
        assert not re.search(r"[&§]6", render.motd(cfg))  # no gold MOTD code either


# --- plugin wiring -------------------------------------------------------------------------------------------------


def test_commands_are_declared_and_handled() -> None:
    plugin_yml = yaml.safe_load((REPO / "plugin/mc-brand/src/main/resources/plugin.yml").read_text())
    src = (JAVA / "BrandPlugin.java").read_text()
    registered = re.search(r"for \(String name : List\.of\(([^)]*)\)\)", src)
    assert registered
    names = set(re.findall(r'"(\w+)"', registered.group(1)))
    assert names == set(plugin_yml["commands"])
    for name in names:
        assert f'case "{name}" ->' in src
    for cmd in plugin_yml["commands"].values():
        assert cmd["permission"] in plugin_yml["permissions"]
    assert plugin_yml["permissions"]["mcbrand.admin"]["default"] == "op"


def test_jar_holds_the_sources_classes() -> None:
    with zipfile.ZipFile(REPO / "plugin/mc-brand/dist/mc-brand.jar") as z:
        names = set(z.namelist())
        assert z.read("plugin.yml") == (REPO / "plugin/mc-brand/src/main/resources/plugin.yml").read_bytes()
    for java in JAVA.glob("*.java"):
        assert f"io/github/edvardk/mcbrand/{java.stem}.class" in names


def test_deploy_installs_and_asserts_the_pins() -> None:
    tasks = (REPO / "ansible/roles/plugins/tasks/brand.yml").read_text()
    assert "brand_jar.stat.checksum == mc_brand_jar_sha256" in tasks
    assert "brand_icon.stat.checksum == mc_brand_icon_sha256" in tasks
    main = (REPO / "ansible/roles/plugins/tasks/main.yml").read_text()
    assert "['mc-brand.jar'] if mc_brand_enabled" in main and "import_tasks: brand.yml" in main
    props = (REPO / "server-config/server.properties.yml.j2").read_text()
    assert "mc_watchdog_url ~ '/pack/' ~ mc_brand_pack_sha1 ~ '.zip'" in props
    assert "require-resource-pack: false" in props


def test_ci_checks_the_art_and_benches_the_plugin() -> None:
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    assert "python scripts/brand_art.py check" in ci
    e2e = (REPO / ".github/workflows/e2e-mock.yml").read_text()
    assert "bench_telemetry.py --plugin mc-brand" in e2e
    assert "tests/e2e/brand_check.py" in e2e


# --- v1.0.1: the HUD, plain language, the wine glyph, the glass for everyone ----------------------------------------


def test_sidebar_entries_are_never_hidden_by_the_client() -> None:
    """The v1.0.0 HUD showed only its title: vanilla hides score holders starting with '#'."""
    src = (JAVA / "Hud.java").read_text()
    m = re.search(r'static final String ENTRY_PREFIX = "([^"]*)";', src)
    assert m and not m.group(1).startswith("#") and "." in m.group(1)  # '.' can't be in a player name
    assert '"#" + i' not in src and "ENTRY_PREFIX + i" in src
    # each line on its own: shown[i] only after the write, errors logged rate-limited
    update = src.split("private void update(Board b", 1)[1].split("\n    }\n", 1)[0]
    assert (
        update.index("score.setScore(")
        < update.index("score.customName(")
        < update.index("b.shown[i] = want")
    )
    assert "catch (RuntimeException e)" in update and "b.shown[i] = null" in update


def test_hud_modes_default_top_and_backwards_compatible() -> None:
    assert schema.load_server(example_server()).branding.hud.default_mode == "top"
    assert load(hud={"default_on": True}).branding.hud.default_mode == "side"  # a v1.0.0 config
    assert load(hud={"default_on": True, "default_mode": "off"}).branding.hud.default_mode == "off"
    assert any("default_mode" in e for e in errors(hud={"default_mode": "bottom"}))
    assert render.brand_plugin(load(hud={"default_mode": "side"}))["hud"]["default_mode"] == "side"
    src = (JAVA / "Hud.java").read_text()
    assert 'new NamespacedKey(plugin, "hud")' in src
    # v1.1 (live): a v1.0.0 "HUD on" (1) follows the server's default mode, not the sidebar; 0 stays off;
    # migrated once (hud_mode written, the legacy key removed)
    assert "legacy != 0 ? Mode.SIDE" not in src
    assert "return legacy != 0 ? serverDefault : Mode.OFF;" in src
    mode_fn = src.split("Mode mode(Player p) {", 1)[1].split("\n    }\n", 1)[0]
    assert "legacyMode(legacy, def)" in mode_fn and "pdc.remove(legacyKey)" in mode_fn
    assert "pdc.set(modeKey, PersistentDataType.STRING, m.word())" in mode_fn
    assert "legacySelftest()" in src
    assert "legacy hud 1->top/side 0->off" in (REPO / "tests/e2e/brand_check.py").read_text()
    assert "p.showBossBar(bar)" in src and "p.hideBossBar(bar)" in src
    plugin = (JAVA / "BrandPlugin.java").read_text()
    assert (
        'case "hud" -> List.of("top", "side", "off", "hide", "show", "minimal", "full", "parts", "reset")'
        in plugin
    )


def test_hud_parts_are_per_player_and_filter_both_hud_modes() -> None:
    """v1.4.1: /hud hide|show <part>, /hud minimal|full, /hud parts, /hud reset; kept in the player's PDC like
    the mode, as deltas on the server's default (branding.hud.parts)."""
    src = (JAVA / "Hud.java").read_text()
    plugin = (JAVA / "BrandPlugin.java").read_text()
    enum = src.split("enum Part {", 1)[1].split(";", 1)[0]
    parts = [p.strip().lower() for p in enum.split(",")]
    assert parts == ["name", "players", "health", "tps", "mspt", "ping", "fps"] == list(schema.HUD_PARTS)
    assert 'new NamespacedKey(plugin, "hud_parts")' in src
    # stored as deltas; no key at all = the server's default
    assert "p.getPersistentDataContainer().remove(partsKey);" in src
    assert "parseDeltas(p.getPersistentDataContainer().get(partsKey, PersistentDataType.STRING))" in src
    assert "hiddenParts.remove(id);" in src  # the cache goes with the player
    # the boss bar: one condition per part, separators only between shown parts, all hidden = the name alone
    bar = src.split("boolean glyph,\n            Set<Part> hidden) {", 1)[1].split("\n    }\n", 1)[0]
    for part in ("NAME", "PLAYERS", "HEALTH", "TPS", "MSPT", "PING", "FPS"):
        assert f"!hidden.contains(Part.{part})" in bar, part
    assert "if (parts.isEmpty()) {\n            parts.add(name);" in bar
    assert "if (i > 0) {\n                b.append(sep);" in bar
    # the sidebar: every line tagged with its part; ping = the numbers next to the names
    for tag in ("Part.PLAYERS));", "Part.HEALTH));", "Part.TPS));", "Part.MSPT));", "null, Part.FPS));"):
        assert tag in src, tag
    viewer = src.split("List<Line> forViewer(", 1)[1].split("\n    }\n", 1)[0]
    assert "hidden.contains(l.part())" in viewer and "hidden.contains(Part.PING)" in viewer
    # the join grace period is unchanged: the settling bar and lines are never filtered
    assert "update(b, mine ? settlingLines : forViewer(" in src
    assert "update(b, unsettled ? all : forViewer(" in src
    settling = src.split("Component barTextSettling(", 1)[1].split("\n    }\n", 1)[0]
    assert "hidden" not in settling
    # the commands: parts, reset, hide|show <part> with a helpful message for an unknown part; tab completion
    cmd = plugin.split("private boolean hudParts(Player p, String[] args) {", 1)[1].split("\n    }\n", 1)[0]
    for case in ('case "parts" ->', 'case "reset" ->', 'case "minimal", "full" ->', 'case "hide", "show" ->'):
        assert case in cmd, case
    assert "There is no HUD part" in cmd and "Hud.Part.words()" in cmd
    assert "if (args.length > 0 && hudParts(p, args)) {" in plugin
    assert "return Hud.Part.words(); // v1.4.1" in plugin
    assert '"[top|side|off|minimal|full|parts]"' in (JAVA / "Help.java").read_text()
    usage = yaml.safe_load((REPO / "plugin/mc-brand/src/main/resources/plugin.yml").read_text())
    assert "/hud hide|show <part>" in usage["commands"]["hud"]["usage"]
    assert "/hud minimal | /hud full" in usage["commands"]["hud"]["usage"]
    e2e = (REPO / "tests/e2e/brand_check.py").read_text()
    for needle in (
        '"hud parts name,players,health,tps,mspt,ping,fps, hidden bar [ · 3 online · Smooth · 144 fps], all hidden"',
        '" name only true, sidebar true, parse PING/PLAYERS/null"',
    ):
        assert needle in e2e
    ops = (REPO / "docs/operations.md").read_text()
    assert "`/hud hide <part>`" in ops and "`/hud reset`" in ops
    assert "`/hud minimal`" in ops and "`/hud full`" in ops


# --- v1.4.1: the server's default HUD parts and the players' deltas -------------------------------------------

HUD_PARTS = list(schema.HUD_PARTS)


def deltas_model(stored: str | None) -> dict[str, bool]:
    """The spec of Hud.parseDeltas: "+part" / "-part", anything else ignored, the last one counts."""
    out: dict[str, bool] = {}
    if stored is None or len(stored) > 256:
        return out
    for w in stored.split(","):
        t = w.strip()
        name = {"online": "players"}.get(t[1:].lower(), t[1:].lower())
        if len(t) >= 2 and t[0] in "+-" and name in HUD_PARTS:
            out[name] = t[0] == "+"
    return dict(sorted(out.items(), key=lambda kv: HUD_PARTS.index(kv[0])))


def fmt(deltas: dict[str, bool]) -> str:
    return ",".join(("+" if v else "-") + k for k, v in deltas.items())


def words(parts: set[str]) -> str:
    return ",".join(p for p in HUD_PARTS if p in parts) or "none"


def effective(default: set[str], deltas: dict[str, bool]) -> set[str]:
    return {p for p in HUD_PARTS if deltas.get(p, p in default)}


def deltas_for(default: set[str], wanted: set[str]) -> dict[str, bool]:
    return {p: p in wanted for p in HUD_PARTS if (p in wanted) != (p in default)}


def choose(default: set[str], deltas: dict[str, bool], part: str, show: bool) -> dict[str, bool]:
    wanted = effective(default, deltas)
    wanted = wanted | {part} if show else wanted - {part}
    return deltas_for(default, wanted)


def test_hud_part_deltas_model_matches_the_selftest() -> None:
    """The selftest's fixed case, worked out here from the spec: the e2e test then proves the plugin agrees."""
    default = {"name", "players", "health", "tps", "fps"}
    stored = deltas_model("+mspt,-fps, bogus,+speed,ping,-NAME,+fps,-")
    assert fmt(stored) == "-name,+mspt,+fps"  # garbage ignored, the last "fps" counts
    assert words(effective(default, stored)) == "players,health,tps,mspt,fps"
    hide_fps = choose(default, stored, "fps", False)
    assert fmt(hide_fps) == "-name,+mspt,-fps"
    back = choose(default, hide_fps, "mspt", False)  # matches the default again: the delta goes
    assert fmt(back) == "-name,-fps"
    # a later server default reaches the parts the player never touched (ping), not the ones they did
    assert words(effective({"name", "players", "ping"}, back)) == "players,ping"
    minimal = deltas_for(default, {"name", "players", "health"})
    full = deltas_for(default, set(HUD_PARTS))
    assert (fmt(minimal), fmt(full)) == ("-tps,-fps", "+mspt,+ping")
    assert effective(default, minimal) == {"name", "players", "health"} and effective(default, full) == set(
        HUD_PARTS
    )
    assert words(effective(default, deltas_model(None))) == "name,players,health,tps,fps"  # /hud reset
    assert deltas_model(fmt(back)) == back
    # nothing stored that matches the default, ever; a showing part shown again is no delta
    assert choose(default, {}, "tps", True) == {} and choose(default, {}, "mspt", False) == {}
    # garbage is safe: too long, no sign, signs only, an unknown part
    for junk in ("x" * 300, "tps,mspt", "+,-,++,--", "+speed,-bogus", "", ",,,"):
        assert deltas_model(junk) == {}, junk
    needle = (
        f"deltas {fmt(stored)} -> {words(effective(default, stored))}, hide fps {fmt(hide_fps)}, hide mspt "
        f"{fmt(back)}, later default players,ping, minimal {fmt(minimal)}, full {fmt(full)}, reset "
        f"name,players,health,tps,fps, round trip true"
    )
    e2e = (REPO / "tests/e2e/brand_check.py").read_text()
    flat = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', e2e))
    assert needle in flat


def test_hud_part_deltas_in_the_plugin() -> None:
    """Hud.java keeps the deltas as the model above: the stored form, the presets, a matching choice dropped."""
    src = (JAVA / "Hud.java").read_text()
    plugin = (JAVA / "BrandPlugin.java").read_text()
    settings = (JAVA / "Settings.java").read_text()
    assert "static final Set<Part> MINIMAL = EnumSet.of(Part.NAME, Part.PLAYERS, Part.HEALTH);" in src
    parse = src.split("static Map<Part, Boolean> parseDeltas(String stored) {", 1)[1].split("\n    }\n", 1)[0]
    assert "stored.length() > MAX_STORED" in parse  # not ours: ignored
    assert "t.charAt(0) != '+' && t.charAt(0) != '-'" in parse and "if (p != null) {" in parse
    # a choice is stored as the difference to the server's default: equal = no delta
    diff = src.split("static Map<Part, Boolean> deltasFor(", 1)[1].split("\n    }\n", 1)[0]
    assert "if (wanted.contains(p) != serverDefault.contains(p)) {" in diff
    eff = src.split("static Set<Part> effective(", 1)[1].split("\n    }\n", 1)[0]
    assert "if (show == null ? serverDefault.contains(p) : show) {" in eff
    shown = src.split("void setShown(Player p, Set<Part> wanted) {", 1)[1].split("\n    }\n", 1)[0]
    assert "formatDeltas(deltasFor(s.hudParts(), wanted))" in shown
    assert "if (v.isEmpty()) {\n            p.getPersistentDataContainer().remove(partsKey);" in shown
    reset = src.split("void resetParts(Player p) {", 1)[1].split("\n    }\n", 1)[0]
    assert "p.getPersistentDataContainer().remove(partsKey);" in reset
    assert "return effective(s.hudParts(), deltas(p));" in src
    # the commands: presets, reset, show|hide through choose(); /hud parts marks the player's changes
    cmd = plugin.split("private boolean hudParts(Player p, String[] args) {", 1)[1].split("\n    }\n", 1)[0]
    assert 'hud.setShown(p, sub.equals("full") ? EnumSet.allOf(Hud.Part.class) : Hud.MINIMAL);' in cmd
    assert 'Hud.choose(def, hud.deltas(p), part, sub.equals("show"))' in cmd
    assert "hud.resetParts(p);" in cmd
    assert "boolean mine = shown.contains(part) != def.contains(part);" in cmd and '(mine ? "*" : "")' in cmd
    # the server's default: branding.hud.parts (old config: every part), in /mcbrand status
    assert (
        'hud.isList("parts") ? Hud.parseParts(hud.getStringList("parts")) : EnumSet.allOf(Hud.Part.class)'
        in settings
    )
    assert "hud parts default=%s;" in plugin and "Hud.words(s.hudParts())" in plugin
    assert '"hud default " + words(s.hudParts())' in src


def test_hud_parts_schema() -> None:
    assert load().branding.hud.parts == HUD_PARTS
    assert load(hud={"parts": ["name", "online", "health"]}).branding.hud.parts == [
        "name",
        "online",
        "health",
    ]
    assert load(hud={"parts": []}).branding.hud.parts == []
    (e,) = errors(hud={"parts": ["name", "speed"]})
    assert e == (
        "branding.hud.parts: unknown HUD part 'speed' (did you mean 'tps'?); the parts are name, players, health, "
        "tps, mspt, ping, fps ('online' = players)"
    )
    (e,) = errors(hud={"parts": ["helth"]})
    assert "unknown HUD part 'helth' (did you mean 'health'?)" in e
    (e,) = errors(hud={"parts": ["TPS"]})
    assert "unknown HUD part 'TPS' (did you mean 'tps'?)" in e
    (e,) = errors(hud={"parts": ["players", "online"]})
    assert e == "branding.hud.parts: 'players' is listed twice ('online' is players)"
    (e,) = errors(hud={"parts": [3]})  # the item type reports it; the check skips it
    assert e.startswith("branding.hud.parts[0]: expected a string, got 3")
    assert schema.field_errors("branding.hud.parts", ["name", "bogus"])
    assert not schema.field_errors("branding.hud.parts", ["name", "fps"])
    js = json.loads((REPO / "schema/server.schema.json").read_text())
    prop = js["properties"]["branding"]["properties"]["hud"]["properties"]["parts"]
    assert prop["items"]["enum"] == [*HUD_PARTS, "online"] and prop["uniqueItems"] is True
    assert prop["default"] == HUD_PARTS


def test_hud_parts_render() -> None:
    def parts(hud: dict[str, object] | None = None, fps_hud: bool = True) -> list[str]:
        data = example_server() | {"client": {"enabled": True, "fps": {"hud": fps_hud}}}
        if hud is not None:
            data["branding"] = {"hud": hud}
        return render.brand_plugin(schema.load_server(data))["hud"]["parts"]

    assert parts() == HUD_PARTS  # today's look
    # canonical names in the plugin's order, "online" = players
    assert parts({"parts": ["fps", "online", "name"]}) == ["name", "players", "fps"]
    assert parts({"parts": []}) == []
    # client.fps.hud false: fps out of the server's default
    assert parts(fps_hud=False) == [p for p in HUD_PARTS if p != "fps"]
    assert parts({"parts": ["name", "fps"]}, fps_hud=False) == ["name"]
    full = yaml.safe_load(FULL.read_text())
    assert full["branding"]["hud"]["parts"] == ["name", "players", "health", "tps", "ping", "fps"]
    golden = json.loads((REPO / "tests/fixtures/full-render.json").read_text())
    assert golden["group_vars"]["mc_brand"]["hud"]["parts"] == full["branding"]["hud"]["parts"]
    assert "fps_hud" not in golden["group_vars"]["mc_brand"]["client"]


def test_hud_parts_docs() -> None:
    ops = (REPO / "docs/operations.md").read_text()
    row = next(ln for ln in ops.splitlines() if ln.startswith("| `branding.hud.parts` |"))
    for word in ("/hud minimal", "/hud full", "/hud reset", "client.fps.hud: false", *HUD_PARTS):
        assert word in row, word
    assert "+fps,-mspt" in ops
    features = (REPO / "docs/features.md").read_text()
    assert "`branding.hud.parts`" in features and "`/hud minimal`" in features
    # 1.4.1's own section: later releases have their own headings
    changelog = (REPO / "CHANGELOG.md").read_text().split("## [1.4.0]", 1)[0].split("## [1.4.1]", 1)[1]
    assert "`branding.hud.parts`" in changelog and "`/hud minimal`" in changelog
    for heading in ("### Added", "### Fixed", "### Security"):
        assert changelog.count(heading) <= 1, heading
    doc = schema.BrandingHud.__dataclass_fields__["parts"].metadata["doc"]
    assert "/hud reset" in doc and "client.fps.hud" in doc


def java_const(text: str, name: str) -> str:
    m = re.search(
        rf'static final (?:String|double|int) {name} = ((?:"(?:[^"\\]|\\.)*"\s*\+?\s*)+|[^;"]+);', text
    )
    assert m, name
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1))) or m.group(1).strip()


def test_plain_language_is_one_catalogue_shared_by_both_plugins_and_the_worker() -> None:
    msg = (JAVA / "Msg.java").read_text()
    tel = (
        REPO / "plugin/mc-telemetry/src/main/java/io/github/edvardk/mctelemetry/TelemetryPlugin.java"
    ).read_text()
    for name in ("TPS_HOVER", "MSPT_HOVER", "PING_HOVER", "FPS_TIP", "SMOOTH_P95_MS", "SMOOTH_TPS", "BUDGET_MS",
                 "PING_FINE_MS"):  # fmt: skip
        assert java_const(msg, name) == java_const(tel, name), name
    assert java_const(msg, "TPS_HOVER").startswith("TPS (ticks per second): how many game updates")
    assert java_const(msg, "MSPT_HOVER").startswith("Tick time (MSPT): milliseconds each update takes.")
    fps = java_const(msg, "FPS_TIP")
    assert fps.startswith("The server and your connection look fine") and "Press F3" in fps
    worker = (REPO / "watchdog/worker.js").read_text()
    assert f'fps: () => ({{ text: "{fps}"' in worker
    assert (float(java_const(msg, "SMOOTH_P95_MS")), float(java_const(msg, "SMOOTH_TPS"))) == (35.0, 19.5)
    # the numbers keep their scale everywhere players see them
    assert '"%.1f/20 TPS"' in msg and '"%.1f of 50 ms"' in msg and '"Smooth"' in msg and '"A bit busy"' in msg
    assert '"FPS: press F3"' in msg and "Msg.FPS_HINT" in (JAVA / "Hud.java").read_text()
    assert "ms per tick" not in tel and '"%.1f/20 TPS"' in tel


def test_glyph_font_in_the_pack_and_only_for_players_with_it() -> None:
    with zipfile.ZipFile(io.BytesIO(neutral_pack())) as z:
        font = json.loads(z.read("assets/mcbrand/font/icons.json"))
        (prov,) = font["providers"]
        assert prov["type"] == "bitmap" and prov["chars"] == ["\ue001"] and prov["height"] == 8
        assert (
            prov["file"] == "mcbrand:font/wine.png"
            and "assets/mcbrand/textures/font/wine.png" in z.namelist()
        )
        w, h, _ = brand_art.decode_png(z.read("assets/mcbrand/textures/font/wine.png"))
        assert (w, h) == (8, 8)
    assert brandpack.GLYPH_FONT == "mcbrand:icons" and brandpack.GLYPH_CHAR == "\ue001"
    texts = (JAVA / "Texts.java").read_text()
    assert 'Key.key("mcbrand", "icons")' in texts and 'GLYPH_CHAR = "\\uE001"' in texts
    # {glyph}; the v1.1 forms ({wine}, U+1F377) still render as the glyph for hand-written configs
    assert 'all.put("glyph", glyph(glyph));' in texts
    assert '.replace(LEGACY_GLYPH_EMOJI, "{glyph}").replace(LEGACY_GLYPH, "{glyph}")' in texts
    plugin = (JAVA / "BrandPlugin.java").read_text()
    assert "getResourcePackStatus() == PlayerResourcePackStatusEvent.Status.SUCCESSFULLY_LOADED" in plugin
    assert "public void onPack(PlayerResourcePackStatusEvent e)" in plugin
    # no raw emoji left in what the product or the full example ship: {glyph} renders per player
    for f in (FULL, REPO / "template/server.yml", REPO / "mcctl/schema.py"):
        assert "\U0001f377" not in f.read_text() and "\\U0001F377" not in f.read_text(), f


def test_drink_for_everyone_who_never_had_one() -> None:
    plugin = (JAVA / "BrandPlugin.java").read_text()
    assert "!has && glass.neverIssued(p) && s.giveOnFirstJoin()" in plugin
    assert "first && !has" not in plugin
    glass = (JAVA / "Drink.java").read_text()
    assert "!p.getPersistentDataContainer().has(serialKey, PersistentDataType.INTEGER)" in glass


def test_drink_command_is_drink_plus_the_configured_name() -> None:
    """v1.2: plugin.yml declares /drink (no static aliases: a jar can't know the deployment's name); a
    branding.drink.command other than drink is registered in the CommandMap with /drink's permission."""
    plugin_yml = yaml.safe_load((REPO / "plugin/mc-brand/src/main/resources/plugin.yml").read_text())
    assert "aliases" not in plugin_yml["commands"]["drink"]
    assert plugin_yml["commands"]["drink"]["permission"] == "mcbrand.drink"
    assert plugin_yml["permissions"]["mcbrand.drink"]["default"] is True
    assert not any("wine" in k for k in (*plugin_yml["commands"], *plugin_yml["permissions"]))
    plugin = (JAVA / "BrandPlugin.java").read_text()
    reg = plugin.split("private void registerDrinkAlias() {", 1)[1].split("\n    }\n", 1)[0]
    assert 'if (name.equals("drink"))' in reg and 'alias.setPermission("mcbrand.drink");' in reg
    assert "Bukkit.getCommandMap()" in reg and "map.register(name, " in reg and "drink(sender);" in reg
    assert "catch (RuntimeException | LinkageError e)" in reg  # a failure leaves /drink working
    assert "removeIf(c -> c == gone)" in plugin  # onDisable takes the alias out again (a reload)
    settings = (JAVA / "Settings.java").read_text()
    assert 'command(drink.getString("command"))' in settings and 'v.matches("[a-z]{3,16}")' in settings
    assert schema.BrandingDrink.__dataclass_fields__["command"].metadata["pattern"] == "^[a-z]{3,16}$"
    # the drink's messages name it; nothing in the plugin says "wine" but its frozen v1.1 ids
    for java in JAVA.glob("*.java"):
        for line in java.read_text().splitlines():
            if "wine" in line.lower():
                assert "LEGACY" in line or line.lstrip().startswith(("*", "/")), (java.name, line)
    e2e = (REPO / "tests/e2e/brand_check.py").read_text()
    assert "drink command /" in e2e


def test_join_grace_period_and_smoothed_health() -> None:
    """v1.1 (decision): a joining player sees "Settling in…" (no health word, colour or FPS hint) until settle_seconds
    passed and two ping samples agree; everyone else's health word needs two bad readings before it gets worse
    (one join's chunk-load burst no longer flips it). The e2e selftest runs both rules on fixed inputs."""
    assert schema.load_server(example_server()).branding.hud.settle_seconds == 10
    assert any("settle_seconds" in e for e in errors(hud={"settle_seconds": 2}))
    assert any("settle_seconds" in e for e in errors(hud={"settle_seconds": 31}))
    assert render.brand_plugin(load(hud={"settle_seconds": 15}))["hud"]["settle_seconds"] == 15
    settings = (JAVA / "Settings.java").read_text()
    assert 'clamp(hud.getInt("settle_seconds", 10), 3, 30)' in settings
    msg = (JAVA / "Msg.java").read_text()
    assert "static final int DOWNGRADE_SAMPLES = 2;" in msg
    assert "if (raw.ordinal() <= shown.ordinal())" in msg  # better (or equal): at once
    assert "return Math.abs(ping - previousPing) <= Math.max(30, previousPing / 2);" in msg
    assert "if (sinceJoinMs < settleMs)" in msg and "previousPing <= 0 || ping <= 0" in msg
    hud = (JAVA / "Hud.java").read_text()
    tick = hud.split("    void tick() {", 1)[1].split("\n    }\n", 1)[0]
    assert "smoother.update(speed.health())" in tick and "speed.health()" not in tick.replace(
        "smoother.update(speed.health())", ""
    )
    assert "updateBar(p, bar, speed, health, online.size(), mine)" in tick
    assert "footerSettling(server, online.size())" in tick
    join = hud.split("    void join(Player p) {", 1)[1].split("\n    }\n", 1)[0]
    assert "settling.put(p.getUniqueId()" in join
    lines = hud.split("List<Line> lines(Server server, Msg.Speed speed, Msg.Health h", 1)[1].split(
        "\n    }\n", 1
    )[0]
    assert "if (forSettling)" in lines and lines.index("if (forSettling)") < lines.index("Msg.FPS_HINT")
    e2e = (REPO / "tests/e2e/brand_check.py").read_text()
    assert "smoothing Smooth/Laggy/Smooth" in e2e and "settle false/false/false/true" in e2e
