#!/usr/bin/env python3
"""Extract every valid config key for the pinned server + plugins, without running the server.

    config_keys.py --javap /path/to/jdk/bin/javap

How: download the locked Purpur jar (paperclip) and the vanilla jar it names, verify both, apply
Purpur's bsdiff patch to rebuild the real server jar (hash-checked against Purpur's manifest),
then read config defaults/keys from bundled resources and class metadata via `javap`.
Writes server-config/keys/<mc_version>/*.txt; tests/test_config_keys.py checks that every key
in server-config/*.yml.j2 exists there. Re-run after every version bump.
"""

from __future__ import annotations

import argparse
import bz2
import functools
import io
import re
import struct
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

import yaml
from lockfile import PLUGINS_LOCK, REPO_ROOT, SERVER_LOCK, digest, download_verified, http_get, load_yaml

KEYS_DIR = REPO_ROOT / "server-config" / "keys"
STRING_CONST = re.compile(r"= (?:String|Utf8)\s+(\S+)$", re.M)
STRING_LITERAL = re.compile(r"= String\s+#\d+\s+// (\S+)$", re.M)
UTF8_CONST = re.compile(r"= Utf8\s+(.+?)\s*$", re.M)
KEYLIKE = re.compile(r"^[a-z][a-z0-9_]*(?:[.-][a-z0-9_]+)*$")


def _off(b: bytes) -> int:
    y = struct.unpack("<Q", b)[0]
    return -(y & 0x7FFFFFFFFFFFFFFF) if y & (1 << 63) else y


def bspatch(old: bytes, patch: bytes) -> bytes:
    """Apply a BSDIFF40 patch (bzip2 blocks), as used by paperclip/jbsdiff."""
    if patch[:8] != b"BSDIFF40":
        raise ValueError("not a BSDIFF40 patch")
    clen, dlen, size = _off(patch[8:16]), _off(patch[16:24]), _off(patch[24:32])
    ctrl = bz2.decompress(patch[32 : 32 + clen])
    diff = bz2.decompress(patch[32 + clen : 32 + clen + dlen])
    extra = bz2.decompress(patch[32 + clen + dlen :])
    new = bytearray(size)
    op = np_ = ci = di = ei = 0
    while np_ < size:
        x, y, z = (_off(ctrl[ci + i * 8 : ci + i * 8 + 8]) for i in range(3))
        ci += 24
        seg = diff[di : di + x]
        if 0 <= op and op + x <= len(old):
            new[np_ : np_ + x] = bytes((a + b) & 0xFF for a, b in zip(seg, old[op : op + x], strict=True))
        else:
            new[np_ : np_ + x] = bytes(
                (seg[i] + (old[op + i] if 0 <= op + i < len(old) else 0)) & 0xFF for i in range(x)
            )
        di, np_, op = di + x, np_ + x, op + x
        new[np_ : np_ + y] = extra[ei : ei + y]
        ei, np_, op = ei + y, np_ + y, op + z
    return bytes(new)


def build_server_jar(paperclip: bytes) -> bytes:
    z = zipfile.ZipFile(io.BytesIO(paperclip))
    sha_vanilla, url, _ = z.read("META-INF/download-context").decode().split()
    vanilla_bundle = download_verified(url, {"sha256": sha_vanilla})
    line = next(
        ln for ln in z.read("META-INF/patches.list").decode().splitlines() if ln.startswith("versions\t")
    )
    _, orig_sha, patch_sha, out_sha, orig_path, patch_path, _out_path = line.split("\t")
    inner = zipfile.ZipFile(io.BytesIO(vanilla_bundle)).read(f"META-INF/versions/{orig_path}")
    patch = z.read(f"META-INF/versions/{patch_path}")
    if digest(inner, "sha256") != orig_sha or digest(patch, "sha256") != patch_sha:
        raise ValueError("vanilla jar or patch hash mismatch")
    out = bspatch(inner, patch)
    if digest(out, "sha256") != out_sha:
        raise ValueError("patched server jar hash mismatch")
    return out


class Javap:
    def __init__(self, javap: str, classpath: Path) -> None:
        self.javap, self.cp = javap, str(classpath)

    @functools.cache  # noqa: B019 - one instance per run
    def verbose(self, cls: str) -> str:
        return subprocess.run(
            [self.javap, "-v", "-cp", self.cp, cls], capture_output=True, text=True, check=True
        ).stdout

    def strings(self, cls: str) -> set[str]:
        return {s for s in STRING_CONST.findall(self.verbose(cls)) if KEYLIKE.match(s)}

    def string_literals(self, cls: str) -> list[str]:
        """Key-like String constants only (not the Utf8 method/field names strings() also sees)."""
        return sorted({s for s in STRING_LITERAL.findall(self.verbose(cls)) if KEYLIKE.match(s)})

    def constants(self, cls: str) -> set[str]:
        """Every string constant of a class (log messages included, spaces and all)."""
        return set(UTF8_CONST.findall(self.verbose(cls)))

    def paper_tree(self, root: str) -> list[str]:
        pkg = root.rsplit(".", 1)[0]
        keys: list[str] = []

        def kebab(name: str) -> str:
            return re.sub(r"(?<!^)([A-Z])", r"-\1", name).lower()

        def walk(cls: str, prefix: str, depth: int) -> None:
            out = self.verbose(cls)
            for m in re.finditer(
                r"\n  public (?!static)(?:final )?([\w.$<>, ?\[\]]+) (\w+);\n((?:    .*\n)*)", out
            ):
                typ, name, body = m.groups()
                nested = re.search(r'value=\["([^"]+(?:","[^"]+)*)"\]', body)
                setting = re.search(r'Setting\b[\s\S]*?value="([^"]+)"', body)
                if nested:
                    key = ".".join(nested.group(1).split('","'))
                elif setting:
                    key = setting.group(1)
                else:
                    key = kebab(name)
                if key == "version":
                    key = "_version"
                keys.append(prefix + key)
                base = typ.split("<")[0]
                if base.startswith(pkg) and "$" in base and depth < 8:
                    walk(base, prefix + key + ".", depth + 1)

        walk(root, "", 0)
        return keys


def yaml_paths(doc: object, prefix: str = "") -> list[str]:
    if not isinstance(doc, dict):
        return []
    out = []
    for k, v in doc.items():
        p = f"{prefix}{k}"
        out.append(p)
        out += yaml_paths(v, p + ".")
    return out


# The vanilla classes that log a line per RCON connection (server-config/log4j2.xml filters them).
RCON_LOGGERS = (
    "net.minecraft.server.rcon.thread.GenericThread",
    "net.minecraft.server.rcon.thread.RconClient",
)

# Plugins whose keys live in a settings class rather than a bundled config.yml:
# {plugin name: fully qualified class name}; its string constants are listed as keys.
PLUGIN_SETTINGS_CLASSES: dict[str, str] = {}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--javap", required=True, help="javap from a JDK matching java_major")
    args = p.parse_args(argv)

    lock = load_yaml(SERVER_LOCK)
    server = lock["server"]
    paperclip = download_verified(server["url"], {"sha256": server["sha256"]})
    out_dir = KEYS_DIR / lock["minecraft_version"]
    out_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        jar = Path(tmp) / "server.jar"
        jar.write_bytes(build_server_jar(paperclip))
        classes = Path(tmp) / "classes"
        zipfile.ZipFile(jar).extractall(classes)
        j = Javap(args.javap, classes)

        cfg = "io.papermc.paper.configuration."
        world_prefix = "world-settings.default."
        files = {
            "paper-world-defaults": j.paper_tree(cfg + "WorldConfiguration"),
            "paper-global": j.paper_tree(cfg + "GlobalConfiguration"),
            "bukkit": yaml_paths(yaml.safe_load((classes / "configurations" / "bukkit.yml").read_text())),
            "spigot": [world_prefix + s for s in j.strings("org.spigotmc.SpigotWorldConfig")]
            + sorted(j.strings("org.spigotmc.SpigotConfig")),
            "purpur": [world_prefix + s for s in j.strings("org.purpurmc.purpur.PurpurWorldConfig")]
            + sorted(j.strings("org.purpurmc.purpur.PurpurConfig")),
            "server.properties": sorted(
                j.strings("net.minecraft.server.dedicated.DedicatedServerProperties")
            ),
            # v1.2: game rule names (minecraft.gamerules, gameplay.*; applied over RCON)
            "gamerules": j.string_literals("net.minecraft.world.level.gamerules.GameRules"),
        }

        # v1.0.1: the jar's own logging config (server-config/log4j2.xml must be it + the RCON filters) and
        # the RCON thread messages those filters drop, with their logger classes (tests/test_log4j2.py).
        (out_dir / "log4j2.xml").write_bytes((classes / "log4j2.xml").read_bytes())
        rcon_log = [
            f"{cls}\t{msg}"
            for cls in RCON_LOGGERS
            for msg in sorted(j.constants(cls))
            if msg.startswith("Thread {}")
        ]
        (out_dir / "rcon-log.txt").write_text("\n".join(sorted(set(rcon_log))) + "\n")

        for plugin in load_yaml(PLUGINS_LOCK)["plugins"]:
            data = http_get(plugin["url"], timeout=300)
            if digest(data, "sha256") != plugin["sha256"]:
                raise ValueError(f"{plugin['name']} hash mismatch")
            pz = zipfile.ZipFile(io.BytesIO(data))
            keys: list[str] = []
            if "config.yml" in pz.namelist():
                keys += yaml_paths(yaml.safe_load(pz.read("config.yml")))
            if plugin["name"] in PLUGIN_SETTINGS_CLASSES:
                pdir = Path(tmp) / plugin["name"]
                pz.extractall(pdir)
                keys += Javap(args.javap, pdir).strings(PLUGIN_SETTINGS_CLASSES[plugin["name"]])
            files[f"plugin-{plugin['name']}"] = keys

    for name, keys in files.items():
        (out_dir / f"{name}.txt").write_text("\n".join(sorted(set(keys))) + "\n")
        print(f"{name}: {len(set(keys))} keys")
    return 0


if __name__ == "__main__":
    sys.exit(main())
