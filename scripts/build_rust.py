#!/usr/bin/env python3
"""Reproducible build of the Rust workspace's binaries (v1.4: rust/smallworld-rcon, the drop-in for
mc_rcon.py's command line, docs/rust/smallworld-rcon.md; rust/smallworld-map, the world map pre-renderer,
docs/rust/smallworld-map.md).

    build_rust.py build    # rust/dist/<binary>-<target> for every binary and target, prints the sha256 pins
    build_rust.py check    # rebuild in a temporary directory, compare with rust/dist/ and the group_vars pins

The toolchain is the one rust/rust-toolchain.toml pins (rustup installs it; the rustc version is checked).
The targets are static musl binaries linked with the toolchain's own rust-lld (rust/.cargo/config.toml), so
nothing from the machine ends up in them. The build runs with `--locked --offline` (there are no
dependencies), a clean environment (no RUSTFLAGS, wrappers or profile overrides from outside),
SOURCE_DATE_EPOCH and `--remap-path-prefix` for every directory involved; the release profile
(rust/Cargo.toml) has codegen-units=1, LTO, panic=abort and stripped symbols. `check` builds in a different
directory than `build` did, and fails if a binary differs from the committed one, from its pin
(ansible/group_vars/all/main.yml, BINARIES' pin: what Ansible verifies before installing it) or
contains a build path. CI runs `check` (ci.yml, job rust).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
RUST_DIR = REPO_ROOT / "rust"
DIST = RUST_DIR / "dist"
GROUP_VARS = REPO_ROOT / "ansible" / "group_vars" / "all" / "main.yml"
# Each binary the workspace ships -> its sha256 pin in group_vars (keys: Debian architecture names).
BINARIES = {
    "smallworld-rcon": "mc_smallworld_rcon_sha256",
    "smallworld-map": "mc_smallworld_map_sha256",
}
PIN = BINARIES["smallworld-rcon"]
BINARY = "smallworld-rcon"
# Debian architecture name (the pin's keys, as restic_sha256's) -> Rust target.
TARGETS = {"amd64": "x86_64-unknown-linux-musl", "arm64": "aarch64-unknown-linux-musl"}
# 1980-01-01, the date the plugin jars carry (scripts/build_plugin.py ZIP_DATE). rustc embeds no time today;
# the variable is set so that a future build step that does (a build script, a tool) is reproducible too.
SOURCE_DATE_EPOCH = "315532800"
# Variables that would change what cargo or rustc produce: never taken from the caller's environment.
SCRUBBED_PREFIXES = (
    "RUSTFLAGS",
    "CARGO_ENCODED_RUSTFLAGS",
    "RUSTC_",
    "RUSTDOC",
    "CARGO_BUILD_",
    "CARGO_PROFILE_",
    "CARGO_TARGET_",
    "CARGO_INCREMENTAL",
    "CARGO_CACHE_RUSTC_INFO",
    "CARGO_UNSTABLE_",
)


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pinned_toolchain() -> str:
    return str(tomllib.loads((RUST_DIR / "rust-toolchain.toml").read_text())["toolchain"]["channel"])


def pins(pin: str = PIN) -> dict[str, str]:
    return {str(k): str(v) for k, v in (yaml.safe_load(GROUP_VARS.read_text()).get(pin) or {}).items()}


def dist_path(arch: str, binary: str = BINARY) -> Path:
    return DIST / f"{binary}-{TARGETS[arch]}"


def build_env(target_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED_PREFIXES)}
    cargo_home = Path(env.get("CARGO_HOME", Path.home() / ".cargo"))
    rustup_home = Path(env.get("RUSTUP_HOME", Path.home() / ".rustup"))
    remaps = [(RUST_DIR, "/smallworld/rust"), (target_dir, "/smallworld/target"), (cargo_home, "/cargo"),
              (rustup_home, "/rustup")]  # fmt: skip
    env["CARGO_ENCODED_RUSTFLAGS"] = "\x1f".join(f"--remap-path-prefix={src}={dst}" for src, dst in remaps)
    env["CARGO_TARGET_DIR"] = str(target_dir)
    env["CARGO_INCREMENTAL"] = "0"
    env["SOURCE_DATE_EPOCH"] = SOURCE_DATE_EPOCH
    env["LC_ALL"] = "C"
    env["TZ"] = "UTC"
    return env


def check_toolchain(env: dict[str, str]) -> None:
    want = pinned_toolchain()
    out = subprocess.run(
        ["rustc", "--version"], cwd=RUST_DIR, env=env, capture_output=True, text=True, check=True
    )
    got = out.stdout.split()[1] if out.stdout.startswith("rustc ") else out.stdout.strip()
    if got != want:
        raise SystemExit(f"rustc {got} in rust/, rust-toolchain.toml pins {want} (is rustup on PATH?)")


def build(target_dir: Path) -> dict[tuple[str, str], bytes]:
    """Every (binary, arch)'s build, built into `target_dir`."""
    env = build_env(target_dir)
    check_toolchain(env)
    out: dict[tuple[str, str], bytes] = {}
    packages = [arg for binary in BINARIES for arg in ("-p", binary)]
    for arch, target in TARGETS.items():
        subprocess.run(
            ["cargo", "build", "--release", "--locked", "--offline", "--target", target, *packages],
            cwd=RUST_DIR,
            env=env,
            check=True,
        )
        for binary in BINARIES:
            data = (target_dir / target / "release" / binary).read_bytes()
            leaks = leaked_paths(data, target_dir)
            if leaks:
                raise SystemExit(f"{target}: {binary} contains build paths: {', '.join(leaks)}")
            out[binary, arch] = data
    return out


def leaked_paths(data: bytes, target_dir: Path) -> list[str]:
    """Machine-specific directories that appear in a binary (none may)."""
    candidates = {str(REPO_ROOT), str(target_dir), str(Path.home())}
    return sorted(p for p in candidates if len(p) > 1 and p.encode() in data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("command", choices=["build", "check"])
    args = ap.parse_args(argv)
    if shutil.which("cargo") is None:
        print(
            "cargo not found: install rustup (https://rustup.rs); it reads rust/rust-toolchain.toml",
            file=sys.stderr,
        )
        return 1
    with tempfile.TemporaryDirectory(prefix="smallworld-rust-") as tmp:
        built = build(Path(tmp) / ("target-build" if args.command == "build" else "target-check"))
    problems: list[str] = []
    for (binary, arch), data in built.items():
        pin = BINARIES[binary]
        want = pins(pin)
        path = dist_path(arch, binary)
        digest = sha256(data)
        rel = path.relative_to(REPO_ROOT)
        if args.command == "build":
            DIST.mkdir(exist_ok=True)
            path.write_bytes(data)
            path.chmod(0o755)
            print(f"{rel}: {len(data)} bytes, sha256 {digest}")
            if want.get(arch) != digest:
                print(f"update ansible/group_vars/all/main.yml: {pin}.{arch}: {digest}")
            continue
        if not path.is_file() or path.read_bytes() != data:
            problems.append(f"{rel} differs from the rebuild (sha256 {digest}): run build_rust.py build")
        if want.get(arch) != digest:
            problems.append(f"{pin}.{arch} is {want.get(arch)}, the rebuild is {digest}")
        if not any(str(rel) in p or f"{pin}.{arch}" in p for p in problems):
            print(f"{rel}: reproducible, {len(data)} bytes, sha256 {digest}")
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
