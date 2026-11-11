"""v1.1: the optional client modpack, one `.mrpack` (Modrinth's modpack format) per tier, built offline.

Inputs: the product's client/pack.yml (mods, the shader entry, the four tiers' first-install defaults), a
config
repo's client.lock.yml (scripts/client_pack.py: pinned Modrinth versions with sha1/sha256/sha512, licences,
the
Fabric loader) and server.yml (name, address, view distance, whether the server resource pack is on).

Output per tier, written by `mcctl render` into rendered/client/:

- `<tier>.plan.json`: what the Worker needs to write the pack per download (watchdog/worker.js `packZip`): the
  modrinth.index.json fields with, per file, its upstream URL and whether we serve a mirrored copy, plus the
  override files (base64). The Worker adds a short-lived signed mirror URL in front of the upstream one, so
  every launcher download is hash-checked (sha512) and falls back to the official CDN by itself.
- `index.json`: the setup page's tier list (titles, descriptions, sizes, credits).

`mrpack(plan, link)` writes the zip exactly as the Worker does (stored entries in a fixed order, fixed dates,
no extra fields), so tests/test_client_pack.py compares the two byte for byte.

Every options.txt key and value encoding was checked in the 26.3 client jar (net.minecraft.client.Options:
values are Gson-encoded JSON; servers.dat via ServerData.write: name, ip, acceptTextures), Iris's and Distant
Horizons' config keys in their pinned jars (client/pack.yml).
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import struct
import zipfile
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
DEFINITION = PRODUCT_ROOT / "client" / "pack.yml"
LICENCES = PRODUCT_ROOT / "client" / "licenses"
LOCK_FILE = "client.lock.yml"
TIERS = ("low", "medium", "high", "maximum")
PLAN_DIR = Path("client")
INDEX_FILE = PLAN_DIR / "index.json"
# Distant Horizons' config schema version (ConfigFileHandler: a file with another `_version` is reset to the
# defaults; verified in the pinned 3.3.4 jar). A DH update that bumps it resets the tier's LOD default,
# nothing worse.
DH_CONFIG_VERSION = 5
OVERRIDES = (
    "client-overrides"  # client-only files (the Modrinth App applies them when it creates the instance)
)
PACK_ID_FILE = "config/smallworld-pack.json"  # v1.2: read by the Smallworld client mod (client/mod/WIRE.md)
SHA256 = re.compile(r"^[0-9a-f]{64}$")
FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
OPTION_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
KEY_NAME = re.compile(r"^key\.[A-Za-z0-9_.]{1,64}$")  # a KeyMapping name (v1.2)
KEY_VALUE = re.compile(r"^key\.(?:keyboard|mouse)\.[a-z0-9_.]{1,40}$")  # an InputConstants.Key name
LAUNCHER_DIR = "smallworld-{tier}"  # v1.2: the official launcher installation's game directory
MISSING_FILE = "MISSING-MODS.txt"
Link = Callable[[str, str], str]  # (sha256, filename) -> our signed mirror URL


class PackError(ValueError):
    """A definition or lock that can't produce a pack."""


# --- definition and lock ------------------------------------------------------------------------------------


def load_definition(path: Path = DEFINITION) -> dict[str, Any]:
    d = yaml.safe_load(path.read_text())
    problems = definition_errors(d)
    if problems:
        raise PackError("; ".join(problems))
    return d


def definition_errors(d: Any) -> list[str]:  # noqa: ANN401 - parsed YAML
    if not isinstance(d, dict) or d.get("format") != 1 or d.get("loader") != "fabric":
        return ["client/pack.yml: format 1, loader fabric"]
    out = []
    tiers = d.get("tiers") or {}
    if tuple(tiers) != TIERS:
        out.append(f"client/pack.yml tiers must be {list(TIERS)} in this order")
    if sum(1 for t in tiers.values() if t.get("recommended")) != 1:
        out.append("client/pack.yml: exactly one tier is `recommended`")
    for t in tiers.values():
        for k in t.get("options") or {}:
            if not OPTION_KEY.fullmatch(str(k)):
                out.append(f"client/pack.yml: option key {k!r}")
        mem = t.get("memory_gb")
        if not isinstance(mem, int) or isinstance(mem, bool) or not 2 <= mem <= 32:
            out.append(f"client/pack.yml tier {t.get('title')}: memory_gb (2..32) is required")
    for k, v in (d.get("keys") or {}).items():
        if not KEY_NAME.fullmatch(str(k)) or not KEY_VALUE.fullmatch(str(v)):
            out.append(f"client/pack.yml: key binding {k!r}: {v!r}")
    for m in [*d.get("mods", []), d.get("shader") or {}]:
        for t in m.get("tiers", []):
            if t not in TIERS:
                out.append(f"client/pack.yml {m.get('slug')}: unknown tier {t!r}")
        lf = m.get("licence_file")
        if lf and not (LICENCES / lf).is_file():
            out.append(f"client/pack.yml {m.get('slug')}: client/licenses/{lf} is missing")
    return out


def entry_tiers(entry: dict[str, Any]) -> list[str]:
    return list(entry.get("tiers") or TIERS)


# --- v1.2: in-house mods (client/pack.yml `inhouse`: the product's committed, reproducible jar) ----------


def inhouse_jar(entry: dict[str, Any]) -> tuple[bytes, dict[str, Any]]:
    """The jar's bytes and its build.yml; the jar must be the one build.yml pins (`jar_sha256`)."""
    spec = entry["inhouse"]
    build = yaml.safe_load((PRODUCT_ROOT / spec["build"]).read_text())
    data = (PRODUCT_ROOT / spec["jar"]).read_bytes()
    if hashlib.sha256(data).hexdigest() != build["jar_sha256"]:
        raise PackError(f"{spec['jar']} isn't the jar {spec['build']} pins (jar_sha256): rebuild it")
    return data, build


def inhouse_licence(data: bytes) -> str:
    """The SPDX id in the jar's fabric.mod.json (`license`)."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        return str(json.loads(z.read("fabric.mod.json")).get("license") or "")


def inhouse_filename(entry: dict[str, Any], build: dict[str, Any]) -> str:
    return f"{entry['slug']}-{build['version']}.jar"


def _inhouse_errors(f: dict[str, Any], d: dict[str, Any]) -> list[str]:
    entry = next((m for m in d["mods"] if m["slug"] == f.get("slug")), None)
    name = f.get("slug")
    if not entry or not entry.get("inhouse"):
        return [f"{LOCK_FILE} {name}: `source: inhouse` but client/pack.yml has no `inhouse` for it"]
    data, build = inhouse_jar(entry)
    want = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "sha512": hashlib.sha512(data).hexdigest(),
        "filename": inhouse_filename(entry, build),
        "url": "",
    }
    if any(f.get(k) != v for k, v in want.items()):
        return [
            f"{LOCK_FILE} {name}: pins another build than the product's {entry['inhouse']['jar']}: run the "
            "lock tool (`mcctl lock update`)"
        ]
    return []


def lock_errors(lock: Any, minecraft_version: str, definition: dict[str, Any] | None = None) -> list[str]:  # noqa: ANN401
    """client.lock.yml must pin the server's Minecraft version and every entry the definition lists (or say
    it is pending), each with its hashes; a non-redistributable licence is never mirrored."""
    d = definition or load_definition()
    if not isinstance(lock, dict) or not isinstance(lock.get("files"), list):
        return [f"{LOCK_FILE}: expected a mapping with `files:` (regenerate it with the lock tool)"]
    out = []
    if str(lock.get("minecraft_version")) != minecraft_version:
        out.append(
            f"{LOCK_FILE}: pins Minecraft {lock.get('minecraft_version')!r} but server.yml "
            "minecraft.version is "
            f"{minecraft_version!r}: run the lock tool"
        )
    loader = lock.get("loader") or {}
    if loader.get("name") != "fabric" or not re.fullmatch(r"\d+\.\d+\.\d+", str(loader.get("version", ""))):
        out.append(f"{LOCK_FILE}: loader must be fabric with a version")
    profile = loader.get("profile")
    if profile is not None:  # v1.2 (older locks have none: no launcher zip until the next lock update)
        want_id = f"fabric-loader-{loader.get('version')}-{lock.get('minecraft_version')}"
        if (
            not isinstance(profile, dict)
            or profile.get("id") != want_id
            or not SHA256.fullmatch(str(profile.get("sha256", "")))
        ):
            out.append(f"{LOCK_FILE}: loader.profile must be {want_id} with its sha256")
        else:
            try:
                profile_bytes(profile)
            except (PackError, KeyError, TypeError) as e:
                out.append(str(e))
    wanted = {m["slug"] for m in d["mods"]} | {d["shader"]["slug"]}
    have = {f.get("slug") for f in lock["files"]} | {p.get("slug") for p in lock.get("pending") or []}
    if have != wanted:
        out.append(f"{LOCK_FILE}: covers {sorted(map(str, have))}, client/pack.yml lists {sorted(wanted)}")
    allowed = set(d["redistributable"])
    for f in lock["files"]:
        name = f.get("slug")
        if not SHA256.fullmatch(str(f.get("sha256", ""))) or not re.fullmatch(
            r"[0-9a-f]{128}", str(f.get("sha512", ""))
        ):
            out.append(f"{LOCK_FILE} {name}: sha256 and sha512 required")
        if not FILENAME.fullmatch(str(f.get("filename", ""))):
            out.append(f"{LOCK_FILE} {name}: filename {f.get('filename')!r}")
        if f.get("source") == "inhouse":
            out += _inhouse_errors(f, d)
        elif not str(f.get("url", "")).startswith("https://cdn.modrinth.com/"):
            out.append(f"{LOCK_FILE} {name}: url must be on cdn.modrinth.com")
        if f.get("mirror") and f.get("licence") not in allowed:
            out.append(f"{LOCK_FILE} {name}: licence {f.get('licence')!r} doesn't allow mirroring")
    return out


# --- v1.5.1: the in-house entries move with the product pin ---------------------------------------------
# A release that ships a new in-house jar (v1.5.0: the Smallworld mod 1.1.0) changes what client.lock.yml must
# pin, and only that release knows the new hashes. So whatever moves the pin (`mcctl upgrade`/`rollout`, the
# update button, the weekly update job) also copies the target release's in-house entries into the config
# repo's client.lock.yml, in the same change, before the target's validate (live: v1.4.1 -> v1.5.0 failed
# validate, and the v1.4.1 lock tool couldn't fix it). The entries come from the release's own
# template/client.lock.yml: what its lock tool writes (tests/test_client.py keeps the two equal), as data, so
# the running product needs none of the target's code.

TEMPLATE_LOCK = f"template/{LOCK_FILE}"
Reader = Callable[[str], bytes | None]  # a product release's file by repository path (None: no such file)


def directory_reader(root: Path) -> Reader:
    """A Reader for a product checkout on disk (the target release's)."""

    def read(rel: str) -> bytes | None:
        path = root / rel
        return path.read_bytes() if path.is_file() else None

    return read


def release_inhouse(read: Reader) -> dict[str, dict[str, Any]]:
    """The `source: inhouse` entries of a product release's template/client.lock.yml, by slug, each checked
    against the jar that release ships (its `path`, sha256). {} for a release without either."""
    raw = read(TEMPLATE_LOCK)
    if raw is None:
        return {}
    try:
        lock = yaml.safe_load(raw)
    except yaml.YAMLError as e:
        raise PackError(f"the release's {TEMPLATE_LOCK} doesn't parse: {e}") from None
    files = lock.get("files") if isinstance(lock, dict) else None
    if not isinstance(files, list):
        raise PackError(f"the release's {TEMPLATE_LOCK} has no `files:` list")
    out: dict[str, dict[str, Any]] = {}
    for f in files:
        if not isinstance(f, dict) or f.get("source") != "inhouse":
            continue
        slug, path = str(f.get("slug") or ""), str(f.get("path") or "")
        safe = path and not path.startswith("/") and ".." not in Path(path).parts
        jar = read(path) if safe else None
        if jar is None:
            raise PackError(f"the release's {TEMPLATE_LOCK} {slug}: its jar {path!r} isn't in the release")
        if hashlib.sha256(jar).hexdigest() != f.get("sha256"):
            raise PackError(f"the release's {TEMPLATE_LOCK} {slug}: sha256 isn't its {path}'s")
        out[slug] = f
    return out


def _lock_files(text: str, what: str) -> dict[str, Any]:
    try:
        lock = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise PackError(f"{what} doesn't parse: {e}") from None
    if not isinstance(lock, dict) or not isinstance(lock.get("files"), list):
        raise PackError(f"{what}: expected a mapping with `files:`")
    return lock


def _inhouse(f: object) -> bool:
    return isinstance(f, dict) and f.get("source") == "inhouse"


def refresh_inhouse(text: str, entries: dict[str, dict[str, Any]]) -> tuple[str, list[str]]:
    """client.lock.yml's text with each in-house entry replaced by the release's entry of the same slug
    (`release_inhouse`); (new text, the slugs that changed). Nothing changed: the same text. Otherwise the
    lock is written as the lock tool writes it (its header comments kept). An entry the release doesn't have
    stays as it is (the target's validate says what to do); none is added or removed."""
    lock = _lock_files(text, LOCK_FILE)
    changed: list[str] = []
    files = []
    for f in lock["files"]:
        new = entries.get(str(f.get("slug"))) if _inhouse(f) else None
        if new is not None and new != f:
            changed.append(str(f.get("slug")))
            f = json.loads(json.dumps(new))  # a copy
        files.append(f)
    if not changed:
        return text, []
    lock["files"] = files
    header = []
    for line in text.splitlines(keepends=True):
        if not line.startswith("#"):
            break
        header.append(line)
    return "".join(header) + yaml.safe_dump(lock, sort_keys=False, default_flow_style=False), changed


def refresh_lock_file(config_root: Path, release: Callable[[], Reader]) -> list[str]:
    """`refresh_inhouse` on config_root/client.lock.yml with the target release's entries; `release` is
    called only when the lock has an in-house entry (it may have to fetch the release). Returns
    "<slug> <old version> -> <new version>" per changed entry ([] when nothing changed)."""
    path = config_root / LOCK_FILE
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    lock = _lock_files(text, LOCK_FILE)
    if not any(_inhouse(f) for f in lock["files"]):
        return []
    new, slugs = refresh_inhouse(text, release_inhouse(release()))
    if not slugs:
        return []
    path.write_text(new, encoding="utf-8")
    before = {str(f.get("slug")): f for f in lock["files"] if _inhouse(f)}
    after = {str(f.get("slug")): f for f in _lock_files(new, LOCK_FILE)["files"]}
    return [f"{s} {before[s].get('version')} -> {after[s].get('version')}" for s in slugs]


def inhouse_change_errors(
    base_text: str, head_text: str, entries: dict[str, dict[str, Any]] | None = None
) -> list[str]:
    """[] when head's client.lock.yml is base's with only in-house entries changed (same Minecraft version,
    loader, pending list, entries and order; every other entry identical) and, given the release's
    `entries`, each in-house entry exactly the release's; else why not."""
    try:
        base, head = _lock_files(base_text, f"the base's {LOCK_FILE}"), _lock_files(head_text, LOCK_FILE)
    except PackError as e:
        return [str(e)]
    rest = {k: v for k, v in base.items() if k != "files"}
    if rest != {k: v for k, v in head.items() if k != "files"}:
        return [f"{LOCK_FILE}: more than its in-house entries changed (Minecraft version, loader or pending)"]

    def slugs(lock: dict[str, Any]) -> list[str]:
        return [str(f.get("slug")) if isinstance(f, dict) else "" for f in lock["files"]]

    if slugs(base) != slugs(head):
        return [f"{LOCK_FILE}: the set or order of its entries changed"]
    out = []
    for old, new in zip(base["files"], head["files"], strict=True):
        name = new.get("slug") if isinstance(new, dict) else "?"
        if not (_inhouse(old) and _inhouse(new)):
            if old != new:
                out.append(f"{LOCK_FILE} {name}: changed, and it isn't an in-house mod of the product")
        elif entries is not None and new != entries.get(str(name)):
            out.append(f"{LOCK_FILE} {name}: isn't exactly the new release's entry ({TEMPLATE_LOCK})")
    return out


def tier_files(lock: dict[str, Any], tier: str) -> list[dict[str, Any]]:
    return [f for f in lock["files"] if tier in f.get("tiers", TIERS)]


# --- override files -----------------------------------------------------------------------------------------


def _gson(v: object) -> str:
    """A value as Options.save writes it (Gson.toJson of the codec's JSON): strings quoted, numbers,
    booleans."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return json.dumps(v)
    return json.dumps(str(v), ensure_ascii=False)


def options_txt(
    options: dict[str, Any], view_distance: int, data_version: int, keys: dict[str, str] | None = None
) -> str:
    """options.txt: the tier's options (Gson-encoded), then v1.2's key bindings. A binding is written as
    Options.processOptions does: `key_<KeyMapping name>:<InputConstants.Key name>`, the value raw (the
    FieldAccess.process(String, String) overload), e.g. `key_key.smoothCamera:key.keyboard.unknown`."""
    lines = [f"version:{data_version}"]
    for k, v in options.items():
        value = view_distance if v == "server" else v
        lines.append(f"{k}:{_gson(value)}")
    for name, key in (keys or {}).items():
        lines.append(f"key_{name}:{key}")
    return "\n".join(lines) + "\n"


def _nbt_string(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def servers_dat(name: str, address: str, accept_textures: bool) -> bytes:
    """Uncompressed NBT (ServerList.save: NbtIo.write): {servers: [{name, ip[, acceptTextures]}]}."""
    entry = b"\x08" + _nbt_string("name") + _nbt_string(name)
    entry += b"\x08" + _nbt_string("ip") + _nbt_string(address)
    if accept_textures:
        entry += b"\x01" + _nbt_string("acceptTextures") + b"\x01"  # ServerPackStatus ENABLED
    entry += b"\x00"
    servers = b"\x09" + _nbt_string("servers") + b"\x0a" + struct.pack(">i", 1) + entry
    return b"\x0a" + _nbt_string("") + servers + b"\x00"


def dh_toml(lod_chunks: int) -> str:
    return (
        f"_version = {DH_CONFIG_VERSION}\n\n"
        "[client]\n\tquickEnableRendering = true\n\n"
        f"[client.advanced.graphics.quality]\n\tlodChunkRenderDistanceRadius = {int(lod_chunks)}\n\n"
        # never let DH replace its own pinned jar
        "[client.advanced.autoUpdater]\n\tenableAutoUpdater = false\n\tenableSilentUpdates = false\n"
    )


def iris_properties(shader_file: str | None) -> str:
    lines = ["# Iris (written once by the server's modpack)", "disableUpdateMessage=true"]
    if shader_file:
        lines += ["enableShaders=true", f"shaderPack={shader_file}"]
    else:
        lines.append("enableShaders=false")
    return "\n".join(lines) + "\n"


def credits_txt(server: str, tier_title: str, files: list[dict[str, Any]], d: dict[str, Any]) -> str:
    out = [
        f"{server} client ({tier_title})",
        "",
        "Every file below is the unmodified release from its authors, pinned by its sha512.",
        "The licence texts are in the licenses/ folder next to this file; each mod's own copyright",
        "notice is inside its jar.",
        "",
    ]
    for f in files:
        out.append(f"- {f['name']} {f['version']} ({f['licence']}): {f['project_url']}")
        if f.get("source_url"):
            out.append(f"  source: {f['source_url']}")
    shader = d["shader"]
    if any(f["slug"] == shader["slug"] for f in files):
        out += ["", shader["credit"].strip(), f"Licence: {shader['licence_url']}"]
    return "\n".join(out) + "\n"


# --- plans and packs ----------------------------------------------------------------------------------------


def pack_filename(server: str, tier_title: str) -> str:
    base = re.sub(r"[^A-Za-z0-9]+", "", server) or "Server"
    return f"{base}-{tier_title}.mrpack"


def plan(
    d: dict[str, Any],
    lock: dict[str, Any],
    tier: str,
    *,
    server: str,
    address: str,
    view_distance: int,
    resource_pack: bool,
) -> dict[str, Any]:
    t = d["tiers"][tier]
    files = tier_files(lock, tier)
    shader = next((f for f in files if f["slug"] == d["shader"]["slug"]), None)
    overrides: list[tuple[str, bytes]] = [
        (
            "options.txt",
            options_txt(
                t["options"],
                view_distance,
                d["data_versions"][str(lock["minecraft_version"])],
                d.get("keys"),
            ).encode(),
        ),
        ("servers.dat", servers_dat(server, address, resource_pack)),
    ]
    if any(f["slug"] == "iris" for f in files):
        on = bool((t.get("iris") or {}).get("shaders")) and shader is not None
        overrides.append(
            (
                "config/iris.properties",
                iris_properties(shader["filename"] if on and shader else None).encode(),
            )
        )
    if any(f["slug"] == "distanthorizons" for f in files):
        overrides.append(
            (
                "config/DistantHorizons.toml",
                dh_toml((t.get("distant_horizons") or {}).get("lod_chunks", 128)).encode(),
            )
        )
    overrides.append(("CREDITS.txt", credits_txt(server, t["title"], files, d).encode()))
    used = sorted(
        {
            lf
            for m in [*d["mods"], d["shader"]]
            if (lf := m.get("licence_file")) and any(f["slug"] == m["slug"] for f in files)
        }
    )
    for lf in used:
        overrides.append((f"licenses/{lf}", (LICENCES / lf).read_bytes()))
    optional = {m["slug"] for m in d["mods"] if m.get("optional")}
    entries = [
        {
            "path": ("shaderpacks/" if f["kind"] == "shader" else "mods/") + f["filename"],
            "hashes": {"sha1": f["sha1"], "sha512": f["sha512"]},
            "env": {"client": "optional" if f["slug"] in optional else "required", "server": "unsupported"},
            "fileSize": int(f["size"]),
            "upstream": f["url"],
            "mirror": f["sha256"] if f.get("mirror") else None,
            "filename": f["filename"],
            "name": f["name"],
        }
        for f in files
    ]
    profile = (lock.get("loader") or {}).get("profile") or {}
    body = {
        "tier": tier,
        "title": t["title"],
        "filename": pack_filename(server, t["title"]),
        "name": f"{server} ({t['title']})",
        "summary": t["description"],
        "dependencies": {
            "minecraft": str(lock["minecraft_version"]),
            "fabric-loader": str(lock["loader"]["version"]),
        },
        "files": entries,
        "overrides": [
            {"path": f"{OVERRIDES}/{p}", "data": base64.b64encode(b).decode()} for p, b in overrides
        ],
        # v1.2: the official launcher's zip (launcher_zip; built and published by mcctl client-publish)
        "launcher": {
            "dir": LAUNCHER_DIR.format(tier=tier),
            "filename": launcher_filename(server, t["title"]),
            "profile_id": str(profile.get("id") or ""),
            "memory_gb": int(t["memory_gb"]),
            "missing": [
                {
                    "name": e["name"],
                    "filename": e["filename"],
                    "folder": e["path"].split("/")[0],
                    "url": e["upstream"],
                    "sha512": e["hashes"]["sha512"],
                    "optional": e["env"]["client"] == "optional",
                }
                for e in entries
                if not e["mirror"]
            ],
        },
    }
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    body["version_id"] = f"{lock['minecraft_version']}-{digest[:12]}"
    if any(f.get("source") == "inhouse" for f in files):
        # v1.2: what the Smallworld mod reports in smallworld:hello (client/mod/WIRE.md "Local files");
        # derived from the version id, so it isn't part of the id's digest
        pack = json.dumps({"tier": tier, "pack": body["version_id"]}, separators=(", ", ": ")) + "\n"
        body["overrides"].append(
            {"path": f"{OVERRIDES}/{PACK_ID_FILE}", "data": base64.b64encode(pack.encode()).decode()}
        )
    return body


# --- v1.2: the official Minecraft Launcher's zip ------------------------------------------------------------


def launcher_filename(server: str, tier_title: str) -> str:
    return pack_filename(server, tier_title).removesuffix(".mrpack") + ".zip"


def profile_bytes(profile: dict[str, Any]) -> bytes:
    """The Fabric launcher profile exactly as Fabric meta served it (compact JSON); its sha256 is pinned."""
    data = json.dumps(profile["json"], ensure_ascii=False, separators=(",", ":")).encode()
    if profile.get("sha256") and hashlib.sha256(data).hexdigest() != profile["sha256"]:
        raise PackError(f"{LOCK_FILE}: the loader profile doesn't match its sha256 (regenerate the lock)")
    return data


def missing_txt(p: dict[str, Any], server: str) -> str:
    la = p["launcher"]
    out = [
        f"{server} client ({p['title']}): files to add yourself",
        "",
        "Their licences don't let anyone but their authors share these files, so they aren't in this zip.",
        "Download each one from its official page (the link below) and put it into the folder shown, inside",
        f"{la['dir']} (this installation's game directory). Optional ones you may leave out.",
        "The sha512 lets you check a download (optional).",
        "",
    ]
    if not la["missing"]:
        out.append("Nothing: everything is in the zip.")
    for m in la["missing"]:
        out += [
            f"- {m['name']}{' (optional)' if m['optional'] else ''}",
            f"  file:    {m['filename']}  ->  {la['dir']}/{m['folder']}/",
            f"  get it:  {m['url']}",
            f"  sha512:  {m['sha512']}",
        ]
    return "\n".join(out) + "\n"


def launcher_manifest(p: dict[str, Any], profile: dict[str, Any], server: str) -> list[tuple[str, str]]:
    """(path in the zip, sha256 of its content) in zip order: the zip's recipe. The same recipe gives the
    same zip, so a publish that finds it already stored needs no downloads."""
    la = p["launcher"]
    pid = str(profile["id"])
    out = [(f"versions/{pid}/{pid}.json", hashlib.sha256(profile_bytes(profile)).hexdigest())]
    for f in p["files"]:
        if f["mirror"]:
            out.append((f"{la['dir']}/{f['path']}", f["mirror"]))
    for name, data in _launcher_files(p, server):
        out.append((name, hashlib.sha256(data).hexdigest()))
    return out


def _launcher_files(p: dict[str, Any], server: str) -> list[tuple[str, bytes]]:
    """The game directory's small files: the pack's overrides, then MISSING-MODS.txt."""
    la = p["launcher"]
    out = [
        (f"{la['dir']}/{o['path'].removeprefix(OVERRIDES + '/')}", base64.b64decode(o["data"]))
        for o in p["overrides"]
    ]
    out.append((f"{la['dir']}/{MISSING_FILE}", missing_txt(p, server).encode()))
    return out


def launcher_recipe(p: dict[str, Any], profile: dict[str, Any], server: str) -> str:
    text = json.dumps(launcher_manifest(p, profile, server), separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def launcher_zip(p: dict[str, Any], profile: dict[str, Any], server: str, blobs: dict[str, bytes]) -> bytes:
    """The tier's zip for the official launcher, unzipped into .minecraft by the player:

    - versions/fabric-loader-<loader>-<mc>/<same>.json: the Fabric profile (the launcher downloads the
      loader's libraries itself; no installer, no Java needed);
    - <dir>/ (smallworld-<tier>, the installation's game directory): mods/ and shaderpacks/ with every
      mirrored file, the tier's options.txt, servers.dat, config/, CREDITS.txt, licenses/, and
      MISSING-MODS.txt naming the files we may not share (official URL + sha512).

    `blobs` maps sha256 -> bytes of every mirrored file (checked here). Stored entries, fixed dates
    (zip_stored): the same plan, profile and files give the same bytes."""
    la = p["launcher"]
    pid = str(profile["id"])
    entries: list[tuple[str, bytes]] = [(f"versions/{pid}/{pid}.json", profile_bytes(profile))]
    for f in p["files"]:
        if not f["mirror"]:
            continue
        data = blobs[f["mirror"]]
        if hashlib.sha256(data).hexdigest() != f["mirror"]:
            raise PackError(f"{f['filename']}: not the pinned file")
        entries.append((f"{la['dir']}/{f['path']}", data))
    entries += _launcher_files(p, server)
    return zip_stored(entries)


def index_json(p: dict[str, Any], link: Link) -> bytes:
    """modrinth.index.json, key order and separators as watchdog/worker.js writes it (JSON.stringify)."""
    files = [
        {
            "path": f["path"],
            "hashes": f["hashes"],
            "env": f["env"],
            # an in-house file has no upstream: the mirror is its only download
            "downloads": ([link(f["mirror"], f["filename"])] if f["mirror"] else [])
            + ([f["upstream"]] if f["upstream"] else []),
            "fileSize": f["fileSize"],
        }
        for f in p["files"]
    ]
    index = {
        "formatVersion": 1,
        "game": "minecraft",
        "versionId": p["version_id"],
        "name": p["name"],
        "summary": p["summary"],
        "files": files,
        "dependencies": p["dependencies"],
    }
    return json.dumps(index, ensure_ascii=False, separators=(",", ":")).encode()


def zip_stored(entries: list[tuple[str, bytes]]) -> bytes:
    """A zip of stored (uncompressed) entries: fixed date (1980-01-01), UTF-8 names, no extra fields, 0644.
    The same bytes as watchdog/worker.js zipStored for the same entries."""
    local, central = bytearray(), bytearray()
    for name, data in entries:
        raw = name.encode("utf-8")
        crc = zlib.crc32(data) & 0xFFFFFFFF
        head = struct.pack(
            "<IHHHHHIIIHH", 0x04034B50, 10, 0x0800, 0, 0, 0x21, crc, len(data), len(data), len(raw), 0
        )
        central += struct.pack(
            "<IHHHHHHIIIHHHHHII",
            0x02014B50, 0x031E, 10, 0x0800, 0, 0, 0x21, crc, len(data), len(data), len(raw), 0, 0, 0, 0,
            0o100644 << 16, len(local),
        ) + raw  # fmt: skip
        local += head + raw + data
    end = struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(entries), len(entries), len(central), len(local), 0)
    return bytes(local + central + end)


def mrpack(p: dict[str, Any], link: Link) -> bytes:
    entries = [("modrinth.index.json", index_json(p, link))]
    entries += [(o["path"], base64.b64decode(o["data"])) for o in p["overrides"]]
    return zip_stored(entries)


def tier_size(p: dict[str, Any]) -> int:
    return sum(f["fileSize"] for f in p["files"])


def index_for_page(
    d: dict[str, Any], plans: dict[str, dict[str, Any]], lock: dict[str, Any], server: str
) -> dict[str, Any]:
    """The setup page's data (served by the Worker after the code check): no address, no secrets."""
    tiers = []
    for tier, p in plans.items():
        t = d["tiers"][tier]
        names = [f["name"] for f in tier_files(lock, tier)]
        tiers.append(
            {
                "id": tier,
                "title": t["title"],
                "description": t["description"],
                "example": t["example"],
                "recommended": bool(t.get("recommended")),
                "size_mb": round(tier_size(p) / 1e6, 1),
                "includes": names,
                "filename": p["filename"],
                "version_id": p["version_id"],
                # v1.2: the official launcher path (client-publish adds `launcher.sha256`/`size_mb` once the
                # zip is stored; without them the page offers the .mrpack only)
                "memory_gb": p["launcher"]["memory_gb"],
                "launcher": {k: p["launcher"][k] for k in ("dir", "filename", "profile_id")},
                "missing": p["launcher"]["missing"],
            }
        )
    return {
        "server": server,
        "minecraft_version": str(lock["minecraft_version"]),
        "tiers": tiers,
        "pending": [p["slug"] for p in lock.get("pending") or []],
        "shader_credit": d["shader"]["credit"].strip(),
        "shader_links": list(d["shader"].get("links") or []),
        "modrinth_app_url": str(d.get("modrinth_app_url") or ""),
    }


def plans_for(
    lock: dict[str, Any],
    *,
    server: str,
    address: str,
    view_distance: int,
    resource_pack: bool,
    tiers: list[str],
) -> dict[str, dict[str, Any]]:
    d = load_definition()
    return {
        t: plan(
            d,
            lock,
            t,
            server=server,
            address=address,
            view_distance=view_distance,
            resource_pack=resource_pack,
        )
        for t in tiers
    }
