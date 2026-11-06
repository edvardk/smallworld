#!/usr/bin/env python3
"""Reproducible build of the Smallworld client mod (v1.2; client/mod/build.yml, client/mod/WIRE.md).

    build_client_mod.py build [--jdk DIR] [--cache DIR]  # client/mod/dist/smallworld.jar + the wire fixtures
    build_client_mod.py check [--jdk DIR] [--cache DIR]  # rebuild + compare (jar bytes, jar_sha256, fixtures)

The same rules as scripts/build_plugin.py (whose JDK download and jar writer it reuses): javac only, every
input sha256-pinned in build.yml. Minecraft 26.x is unobfuscated, so the compile classpath is Mojang's client
jar, the Fabric Loader jar, the Fabric API modules the mod uses (extracted from the pinned fabric-api jar, the
same file the client packs ship) and the few libraries the client jar's API exposes. No Gradle, no Loom, no
remapping.

After compiling, the test class WireFixtures (client/mod/src/test/java) checks the wire format's round trips
and prints the fixtures; `build` writes them, `check` compares them with the committed file, which
tests/test_client_mod.py decodes with an independent Python implementation.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from build_plugin import _env, jar_bytes, jdk_home, sha256
from lockfile import REPO_ROOT, download_verified, load_yaml

MOD_DIR = REPO_ROOT / "client" / "mod"
BUILD_YML = MOD_DIR / "build.yml"
# Fabric-Mapping-Namespace: what Loom writes for 26.x (the pinned fabric-api jar says the same): the class
# names are the game's own, nothing to remap.
MANIFEST = (
    "Manifest-Version: 1.0\r\n"
    "Created-By: scripts/build_client_mod.py\r\n"
    "Fabric-Mapping-Namespace: official\r\n"
    "\r\n"
)
JAVAC_FLAGS = [
    "-encoding", "UTF-8",
    "-proc:none",
    "-Xlint:all,-processing,-serial,-classfile",
    "-Werror",
    "-implicit:none",
]  # fmt: skip


def load_build() -> dict[str, Any]:
    return load_yaml(BUILD_YML)


def cached(url: str, digest: str, cache: Path) -> Path:
    """A sha256-pinned download, cached under its hash (re-verified on every use)."""
    path = cache / "client-mod" / f"{digest}.jar"
    if not (path.is_file() and sha256(path.read_bytes()) == digest):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(download_verified(url, {"sha256": digest}))
    return path


def _module_of(name: str, prefix: str) -> bool:
    return name.startswith(prefix) and name[len(prefix) : len(prefix) + 1].isdigit()


def fabric_modules(fabric_api: Path, prefixes: list[str], dest: Path) -> list[Path]:
    """The jar-in-jar modules of the fabric-api jar that match `prefixes` (exactly one each)."""
    out = []
    with zipfile.ZipFile(fabric_api) as z:
        names = [n for n in z.namelist() if n.startswith("META-INF/jars/") and n.endswith(".jar")]
        for prefix in prefixes:
            hits = [n for n in names if _module_of(Path(n).name, prefix)]
            if len(hits) != 1:
                raise SystemExit(f"fabric-api module {prefix!r}: {len(hits)} jars (need exactly 1)")
            p = dest / Path(hits[0]).name
            p.write_bytes(z.read(hits[0]))
            out.append(p)
    return out


def classpath(cfg: dict[str, Any], cache: Path, dest: Path) -> list[Path]:
    mc, loader, api = cfg["minecraft"], cfg["fabric_loader"], cfg["fabric_api"]
    cp = [cached(mc["url"], mc["sha256"], cache), cached(loader["url"], loader["sha256"], cache)]
    cp += fabric_modules(cached(api["url"], api["sha256"], cache), api["modules"], dest)
    cp += [cached(lib["url"], lib["sha256"], cache) for lib in cfg["libraries"]]
    return cp


def javac(jdk: Path, cfg: dict[str, Any], sources: list[Path], cp: list[Path], out: Path) -> None:
    argv = [
        str(jdk / "bin" / "javac"),
        "--release", str(cfg["release"]),
        *JAVAC_FLAGS,
        "-d", str(out),
        "-cp", os.pathsep.join(map(str, cp)),
        *[str(s.relative_to(REPO_ROOT)) for s in sources],
    ]  # fmt: skip
    res = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, env=_env())
    if res.returncode != 0:
        sys.stderr.write(res.stdout + res.stderr)
        raise SystemExit("javac failed")


def build(jdk: Path, cfg: dict[str, Any], cache: Path) -> tuple[bytes, bytes]:
    """(the jar, the wire fixtures JSON)."""
    main = sorted((MOD_DIR / "src" / "main" / "java").rglob("*.java"))
    tests = sorted((MOD_DIR / "src" / "test" / "java").rglob("*.java"))
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for d in ("lib", "classes", "test-classes"):
            (tmp / d).mkdir()
        javac(jdk, cfg, main, classpath(cfg, cache, tmp / "lib"), tmp / "classes")
        jar = jar_bytes(tmp / "classes", MOD_DIR / "src" / "main" / "resources", MANIFEST)
        # Wire has no Minecraft types: the fixtures run on the plain JDK
        javac(jdk, cfg, tests, [tmp / "classes"], tmp / "test-classes")
        cp = os.pathsep.join([str(tmp / "classes"), str(tmp / "test-classes")])
        res = subprocess.run(
            [str(jdk / "bin" / "java"), "-cp", cp, "io.github.edvardk.smallworld.client.WireFixtures"],
            capture_output=True,
            env=_env(),
        )
        if res.returncode != 0:
            sys.stderr.write(res.stdout.decode() + res.stderr.decode())
            raise SystemExit("WireFixtures failed")
        return jar, res.stdout


def mod_json_version(jar: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(jar)) as z:
        return str(json.loads(z.read("fabric.mod.json"))["version"])


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["build", "check"])
    ap.add_argument("--jdk", help="JDK home with the pinned javac (default: download the pinned Temurin)")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "smallworld" / "build-client-mod"))
    args = ap.parse_args(argv)
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    cfg = load_build()
    jar, fixtures = build(jdk_home(args.jdk, cfg, cache), cfg, cache)
    digest = sha256(jar)
    target = REPO_ROOT / cfg["jar"]
    fixture_file = REPO_ROOT / cfg["fixtures"]
    problems = []
    if mod_json_version(jar) != str(cfg["version"]):
        problems.append(f"fabric.mod.json version != client/mod/build.yml version {cfg['version']}")
    if args.command == "build":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(jar)
        fixture_file.write_bytes(fixtures)
        print(f"{cfg['jar']}: {len(jar)} bytes, sha256 {digest}")
        if digest != cfg["jar_sha256"]:
            print(f"update client/mod/build.yml: jar_sha256: {digest}")
    else:
        if not target.is_file() or target.read_bytes() != jar:
            problems.append(f"{cfg['jar']} differs from a fresh build (sha256 {digest}): run `build`")
        if digest != cfg["jar_sha256"]:
            problems.append(f"client/mod/build.yml jar_sha256 != {digest}")
        if not fixture_file.is_file() or fixture_file.read_bytes() != fixtures:
            problems.append(f"{cfg['fixtures']} differs from WireFixtures' output: run `build`")
        if not problems:
            print(f"{cfg['jar']}: reproducible, sha256 {digest}; wire fixtures match")
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
