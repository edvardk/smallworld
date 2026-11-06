"""mc-brand's optional resource pack (design §8 P17; v1.2: built by `mcctl render` from the config's art).

The pack holds the drink's item model and 16x16 texture, the 8x8 {glyph} of mc-brand's chat texts (font
mcbrand:icons, U+E001) and pack.png (the server icon). Each PNG is the config repo's own file, used byte for
byte (branding.drink.texture, branding.resource_pack.glyph, branding.icon; checked by mcctl/render.py), or
the product's neutral art (plugin/mc-brand/art/drink.png, glyph.png, dist/server-icon.png;
scripts/brand_art.py builds them). The zip is byte-reproducible: stored entries, sorted, fixed dates and
modes; its name is its sha1 (the URL the Worker serves, pack/<sha1>.zip, and server.properties
resource-pack-sha1: the client checks it). Same art + same description = the same bytes, so a client that
has the pack is never asked again.

Format (verified against the pinned 26.3 client and server jars, 2026-11-02: version.json
pack_version.resource_major 97; vanilla packs declare "min_format"/"max_format" as plain integers and
pack_format is deprecated from format 65 on): minecraft:items/potion.json becomes a `select` on
`minecraft:custom_model_data` (strings[0]): the item mc-brand gives out carries MODEL_KEY and gets the
drink's model; every other potion falls back to 26.3's own potion definition (VANILLA_POTION, copied from
the 26.3 client jar). An `item_model` component would show the missing-model cube to players without the
pack; custom_model_data keeps the plain potion look for them (the pack is optional: require-resource-pack
false).

LEGACY_* names: until v1.2 the drink was one fixed item. The custom_model_data string and the pack's paths
keep their v1.1 names (frozen; = plugin/mc-brand Drink.LEGACY_MODEL_STRING): drinks in inventories and
chests carry the string, and the same art gives a byte-identical pack.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import Mapping
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
ART_DIR = PRODUCT_ROOT / "plugin" / "mc-brand" / "art"
NEUTRAL_ART = {
    "icon": PRODUCT_ROOT / "plugin" / "mc-brand" / "dist" / "server-icon.png",
    "texture": ART_DIR / "drink.png",
    "glyph": ART_DIR / "glyph.png",
}
PACK_FORMAT = 97  # 26.3: version.json pack_version.resource_major (build_plugin.py check re-reads the jar's)
MODEL_KEY = "mcbrand:wine_glass"  # LEGACY: = Drink.LEGACY_MODEL_STRING (frozen)
LEGACY_ITEM = "wine_glass"  # assets/mcbrand/{models,textures}/item/<this> (frozen)
LEGACY_FONT_FILE = "wine.png"  # assets/mcbrand/textures/font/<this> (frozen)
GLYPH_CHAR = ""  # a private-use codepoint (= Texts.GLYPH_CHAR)
GLYPH_FONT = "mcbrand:icons"  # = Texts.ICONS
ZIP_DATE = (1980, 1, 1, 0, 0, 0)

# assets/minecraft/items/potion.json of the 26.3 client jar (sha1 e877b6a07acd633fb3bb475002175cec036e7b87).
VANILLA_POTION = {
    "type": "minecraft:model",
    "model": "minecraft:item/potion",
    "tints": [{"type": "minecraft:potion", "default": -13083194}],
}


def _json(obj: object) -> bytes:
    return (json.dumps(obj, indent=2, sort_keys=True) + "\n").encode()


def art(own: Mapping[str, bytes]) -> dict[str, bytes]:
    """The pack's three PNGs: the config repo's own (keys icon, texture, glyph), else the neutral art."""
    return {k: own[k] if k in own else path.read_bytes() for k, path in NEUTRAL_ART.items()}


def pack_files(own: Mapping[str, bytes], description: str) -> dict[str, bytes]:
    a = art(own)
    item = f"mcbrand:item/{LEGACY_ITEM}"
    potion = {
        "model": {
            "type": "minecraft:select",
            "property": "minecraft:custom_model_data",
            "index": 0,
            "cases": [{"when": MODEL_KEY, "model": {"type": "minecraft:model", "model": item}}],
            "fallback": VANILLA_POTION,
        }
    }
    return {
        "pack.mcmeta": _json(
            {"pack": {"description": description, "min_format": PACK_FORMAT, "max_format": PACK_FORMAT}}
        ),
        "pack.png": a["icon"],
        "assets/minecraft/items/potion.json": _json(potion),
        f"assets/mcbrand/models/item/{LEGACY_ITEM}.json": _json(
            {"parent": "minecraft:item/generated", "textures": {"layer0": item}}
        ),
        f"assets/mcbrand/textures/item/{LEGACY_ITEM}.png": a["texture"],
        # font provider format of the 26.3 client jar (assets/minecraft/font/include/default.json: "bitmap"
        # with file, ascent, chars; height defaults to 8): a font of its own, the default font is untouched
        "assets/mcbrand/font/icons.json": _json(
            {
                "providers": [
                    {
                        "type": "bitmap",
                        "file": f"mcbrand:font/{LEGACY_FONT_FILE}",
                        "ascent": 7,
                        "height": 8,
                        "chars": [GLYPH_CHAR],
                    }
                ]
            }
        ),
        f"assets/mcbrand/textures/font/{LEGACY_FONT_FILE}": a["glyph"],
    }


def pack_zip(own: Mapping[str, bytes], description: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in sorted(pack_files(own, description).items()):
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, data)
    return buf.getvalue()


def sha1(data: bytes) -> str:
    return hashlib.sha1(data, usedforsecurity=False).hexdigest()
