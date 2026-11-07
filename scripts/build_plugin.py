#!/usr/bin/env python3
"""Reproducible build of the in-house plugins (design §8: P13 mc-telemetry, P17 mc-brand; plugin/*/build.yml).

    build_plugin.py build [--plugin NAME] [--jdk DIR] [--server-jar FILE] [--cache DIR]  # plugin/NAME/dist/
    build_plugin.py check [--plugin NAME] [--jdk DIR] [--server-jar FILE] [--cache DIR]  # rebuild + compare
    (default: every plugin in PLUGINS)

javac only (no build tool, no dependency resolution): the compile classpath is the Paper/Purpur API and its
libraries exactly as bundled in the pinned server jar (template/server.lock.yml, sha256-checked), so every
API the
plugin uses is checked against the server it runs on; the JDK is the one pinned in build.yml (sha256-checked
download, or --jdk with the same `javac -version`). The jar is stored uncompressed (deflate output differs
between
zlib builds), with sorted entries, fixed timestamps and permissions, so the same sources give the same bytes
on
any machine. `check` also compares its sha256 with the ansible/group_vars/all/main.yml pin named in build.yml
(`pin`, what Ansible verifies on deploy), and that mcctl/brandpack.py's resource-pack format is the pinned
server jar's (version.json pack_version.resource_major). CI runs `check` (ci.yml, job plugin).

v1.3: a plugin whose build.yml names `fixtures` and `fixtures_main` also compiles src/test/java (never in the
jar) and runs that class with the pinned JDK; its stdout is the fixtures file (`build` writes it, `check`
compares it), which a Python test checks independently (mc-brand: WayfindingFixtures,
tests/test_wayfinding.py).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import yaml
from lockfile import REPO_ROOT, SERVER_LOCK, download_verified, load_yaml

PLUGINS = ("mc-telemetry", "mc-brand")
GROUP_VARS = REPO_ROOT / "ansible" / "group_vars" / "all" / "main.yml"
ZIP_DATE = (1980, 1, 1, 0, 0, 0)
MANIFEST = "Manifest-Version: 1.0\r\nCreated-By: smallworld scripts/build_plugin.py\r\n\r\n"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_build(plugin: str = "mc-telemetry") -> dict[str, Any]:
    return load_yaml(REPO_ROOT / "plugin" / plugin / "build.yml")


def pinned_jar_sha256(cfg: dict[str, Any]) -> str:
    return str(yaml.safe_load(GROUP_VARS.read_text())[cfg["pin"]])


def pack_format(server: bytes) -> int:
    """The resource pack format of the server jar's Minecraft version (version.json, verified 26.3: 97)."""
    import json

    with zipfile.ZipFile(io.BytesIO(server)) as z:
        return int(json.loads(z.read("version.json"))["pack_version"]["resource_major"])


def data_version(server: bytes) -> int:
    """The data version of the server jar's Minecraft version (version.json world_version, verified 26.3:
    5023): client/pack.yml data_versions must say the same (options.txt `version:` in the client packs)."""
    import json

    with zipfile.ZipFile(io.BytesIO(server)) as z:
        return int(json.loads(z.read("version.json"))["world_version"])


def server_jar(path: str | None, cache: Path) -> bytes:
    lock = load_yaml(SERVER_LOCK)["server"]
    if path:
        data = Path(path).read_bytes()
        if sha256(data) != lock["sha256"]:
            raise SystemExit(f"{path}: sha256 differs from template/server.lock.yml ({lock['filename']})")
        return data
    cached = cache / lock["filename"]
    if cached.is_file() and sha256(cached.read_bytes()) == lock["sha256"]:
        return cached.read_bytes()
    data = download_verified(lock["url"], {"sha256": lock["sha256"]})
    cache.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(data)
    return data


def jdk_home(path: str | None, build: dict[str, Any], cache: Path) -> Path:
    want = build["jdk"]["version"].split("+")[0]
    if path:
        home = Path(path)
    else:
        pin = build["jdk"]["x64"]
        home = cache / f"jdk-{build['jdk']['version']}"
        if not (home / "bin" / "javac").is_file():
            data = download_verified(pin["url"], {"sha256": pin["sha256"]})
            tmp = Path(tempfile.mkdtemp(dir=cache))
            with tarfile.open(fileobj=io.BytesIO(data)) as tar:
                tar.extractall(tmp, filter="data")
            (top,) = [p for p in tmp.iterdir() if p.is_dir()]
            shutil.move(str(top), home)
            shutil.rmtree(tmp, ignore_errors=True)
    out = subprocess.run(
        [str(home / "bin" / "javac"), "-version"], capture_output=True, text=True, check=True, env=_env()
    )
    got = (out.stdout + out.stderr).split()[-1]
    if got != want:
        raise SystemExit(f"javac {got} at {home}, build.yml pins {want}")
    return home


def _env() -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS")
    }
    env["LC_ALL"] = "C"
    env["TZ"] = "UTC"
    return env


def classpath(server: bytes, prefixes: list[str], dest: Path) -> list[Path]:
    """The libraries under META-INF/libraries/ of the server jar that match `prefixes`, extracted to
    `dest`."""
    out = []
    with zipfile.ZipFile(io.BytesIO(server)) as z:
        names = [n for n in z.namelist() if n.startswith("META-INF/libraries/") and n.endswith(".jar")]
        for prefix in prefixes:
            hits = [n for n in names if n.removeprefix("META-INF/libraries/").startswith(prefix)]
            if len(hits) != 1:
                raise SystemExit(
                    f"classpath prefix {prefix!r}: {len(hits)} jars in the server jar (need exactly 1)"
                )
            p = dest / Path(hits[0]).name
            p.write_bytes(z.read(hits[0]))
            out.append(p)
    return out


def jar_bytes(classes: Path, resources: Path, manifest: str = MANIFEST) -> bytes:
    """A deterministic jar: manifest first, then every file sorted by name, stored, fixed date and mode."""
    files: dict[str, bytes] = {}
    for root in (classes, resources):
        for p in sorted(root.rglob("*")):
            if p.is_file():
                files[p.relative_to(root).as_posix()] = p.read_bytes()
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_STORED) as z:
        for name, data in [("META-INF/MANIFEST.MF", manifest.encode()), *sorted(files.items())]:
            info = zipfile.ZipInfo(name, date_time=ZIP_DATE)
            info.external_attr = 0o644 << 16
            info.create_system = 3
            z.writestr(info, data)
    return buf.getvalue()


def compile_only(build_cfg: dict[str, Any], cache: Path) -> list[Path]:
    """build.yml compile_only: sha256-pinned jars on javac's classpath only (cached by hash)."""
    out = []
    for dep in build_cfg.get("compile_only") or []:
        path = cache / "compile-only" / f"{dep['sha256']}.jar"
        if not (path.is_file() and sha256(path.read_bytes()) == dep["sha256"]):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(download_verified(dep["url"], {"sha256": dep["sha256"]}))
        out.append(path)
    return out


def _javac(jdk: Path, build_cfg: dict[str, Any], sources: list[Path], cp: list[Path], out: Path) -> None:
    argv = [
        str(jdk / "bin" / "javac"),
        "--release", str(build_cfg["release"]),
        "-encoding", "UTF-8",
        "-proc:none",
        "-Xlint:all,-processing,-serial,-classfile",  # missing annotation classes in the API jars
        "-Werror",
        "-implicit:none",
        "-d", str(out),
        "-cp", os.pathsep.join(map(str, cp)),
        *[str(s.relative_to(REPO_ROOT)) for s in sources],
    ]  # fmt: skip
    res = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, env=_env())
    if res.returncode != 0:
        sys.stderr.write(res.stdout + res.stderr)
        raise SystemExit("javac failed")


def build(jdk: Path, server: bytes, build_cfg: dict[str, Any], cache: Path) -> tuple[bytes, bytes | None]:
    """(the jar, the fixtures' output or None)."""
    plugin_dir = REPO_ROOT / "plugin" / build_cfg["name"]
    sources = sorted((plugin_dir / "src" / "main" / "java").rglob("*.java"))
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        for d in ("lib", "classes", "test-classes"):
            (tmp / d).mkdir()
        cp = classpath(server, build_cfg["classpath"], tmp / "lib") + compile_only(build_cfg, cache)
        _javac(jdk, build_cfg, sources, cp, tmp / "classes")
        jar = jar_bytes(tmp / "classes", plugin_dir / "src" / "main" / "resources")
        if not build_cfg.get("fixtures_main"):
            return jar, None
        tests = sorted((plugin_dir / "src" / "test" / "java").rglob("*.java"))
        run_cp = [tmp / "classes", *cp]
        _javac(jdk, build_cfg, tests, run_cp, tmp / "test-classes")
        res = subprocess.run(
            [
                str(jdk / "bin" / "java"),
                "-cp",
                os.pathsep.join(map(str, [tmp / "test-classes", *run_cp])),
                build_cfg["fixtures_main"],
            ],
            capture_output=True,
            env=_env(),
        )
        if res.returncode != 0:
            sys.stderr.write(res.stdout.decode() + res.stderr.decode())
            raise SystemExit(f"{build_cfg['fixtures_main']} failed")
        return jar, res.stdout


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["build", "check"])
    ap.add_argument("--plugin", choices=PLUGINS, help="one plugin (default: all)")
    ap.add_argument("--jdk", help="JDK home with the pinned javac (default: download the pinned Temurin)")
    ap.add_argument("--server-jar", help="the pinned server jar (default: download it)")
    ap.add_argument("--cache", default=str(Path.home() / ".cache" / "smallworld" / "build-plugin"))
    args = ap.parse_args(argv)
    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    lock = load_yaml(SERVER_LOCK)
    server = server_jar(args.server_jar, cache)
    problems: list[str] = []
    for name in [args.plugin] if args.plugin else list(PLUGINS):
        problems += build_one(args.command, name, args.jdk, server, cache, lock)
    if args.command == "check":
        sys.path.insert(0, str(REPO_ROOT))
        from mcctl import brandpack

        got = pack_format(server)
        if got != brandpack.PACK_FORMAT:
            problems.append(
                f"mcctl/brandpack.py PACK_FORMAT {brandpack.PACK_FORMAT} != the server jar's {got}"
            )
        # v1.1: the client packs' options.txt data version
        mc = str(lock["minecraft_version"])
        pinned = load_yaml(REPO_ROOT / "client" / "pack.yml").get("data_versions", {}).get(mc)
        if pinned != data_version(server):
            problems.append(
                f"client/pack.yml data_versions[{mc!r}] {pinned} != the server jar's {data_version(server)}"
            )
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


def build_one(
    command: str, name: str, jdk: str | None, server: bytes, cache: Path, lock: dict[str, Any]
) -> list[str]:
    cfg = load_build(name)
    if int(lock["java_major"]) != int(cfg["release"]):
        return [
            f"{name}: build.yml release {cfg['release']} != java_major {lock['java_major']} (server.lock.yml)"
        ]
    data, fixtures = build(jdk_home(jdk, cfg, cache), server, cfg, cache)
    target = REPO_ROOT / cfg["jar"]
    digest = sha256(data)
    fixture_file = REPO_ROOT / cfg["fixtures"] if fixtures is not None else None
    if command == "build":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if fixture_file is not None and fixtures is not None:
            fixture_file.write_bytes(fixtures)
        print(f"{target.relative_to(REPO_ROOT)}: {len(data)} bytes, sha256 {digest}")
        if digest != pinned_jar_sha256(cfg):
            print(f"update ansible/group_vars/all/main.yml: {cfg['pin']}: {digest}")
        return []
    problems = []
    if not target.is_file() or target.read_bytes() != data:
        problems.append(
            f"{cfg['jar']} differs from a fresh build (sha256 {digest}): "
            f"run build_plugin.py build --plugin {name}"
        )
    if digest != pinned_jar_sha256(cfg):
        problems.append(f"{cfg['pin']} in ansible/group_vars/all/main.yml != {digest}")
    if fixture_file is not None and (not fixture_file.is_file() or fixture_file.read_bytes() != fixtures):
        problems.append(f"{cfg['fixtures']} differs from {cfg['fixtures_main']}'s output: run `build`")
    if not problems:
        print(f"{cfg['jar']}: reproducible, sha256 {digest}")
    return problems


if __name__ == "__main__":
    sys.exit(main())
