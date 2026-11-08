#!/usr/bin/env python3
"""Reproducible build of the Rust workspace's binaries (v1.4): rust/smallworld-rcon (the drop-in for
mc_rcon.py's command line; docs/rust/smallworld-rcon.md), rust/smallworld-map (the world map pre-renderer;
docs/rust/smallworld-map.md) and rust/smallworld-agent (the port of mc_agent.py;
rust/smallworld-agent/PORTING.md).

    build_rust.py build    # rust/dist/<binary>-<target> for every binary and target, prints the sha256 pins
    build_rust.py check    # rebuild in a temporary directory, compare with rust/dist/ and the group_vars pins

The toolchain is the one rust/rust-toolchain.toml pins (rustup installs it; the rustc version is checked).
The targets are static musl binaries linked with the toolchain's own rust-lld (rust/.cargo/config.toml), so
nothing from the machine's libraries ends up in them. smallworld-agent's TLS provider, ring, compiles C and
assembly: with the pinned clang (CLANG, its version checked; plain clang with `--target`, no musl sysroot or
cross-gcc) and llvm-ar of the same release. The crates come from crates.io as Cargo.lock pins them
(`cargo fetch --locked` first; the build itself runs `--locked --offline`; smallworld-rcon and smallworld-map
have none), in a clean environment (no RUSTFLAGS, CC/CFLAGS, wrappers or profile overrides from outside),
with SOURCE_DATE_EPOCH and `--remap-path-prefix` for every directory involved; the release profiles
(rust/Cargo.toml) have codegen-units=1, LTO and stripped symbols. `check` builds in a different directory
than `build` did, and fails if a binary differs from the committed one, from its pin
(ansible/group_vars/all/main.yml, BINARIES' pin: what Ansible verifies before installing it) or contains a
build path. CI runs `check` (ci.yml, job rust).
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
# binary -> (its group_vars pin (keys: Debian architecture names), the cargo profile it is built with)
BINARIES = {
    "smallworld-rcon": ("mc_smallworld_rcon_sha256", "release"),
    "smallworld-map": ("mc_smallworld_map_sha256", "release"),
    "smallworld-agent": ("mc_smallworld_agent_sha256", "release-agent"),
}
PIN = BINARIES["smallworld-rcon"][0]  # the first pin (kept for callers that predate the agent)
BINARY = "smallworld-rcon"
# The C compiler for ring (smallworld-agent's TLS provider): Ubuntu 24.04's clang 18 (the CI runners' and the
# dev container's). Its version is part of the build's inputs: another clang may emit other objects, so the
# version is checked, never just whatever `clang` is on PATH.
CLANG = "clang-18"
CLANG_VERSION = "18.1.3"
LLVM_AR = "llvm-ar-18"
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
    "CC_",
    "CFLAGS",
    "CXX",
    "AR_",
    "TARGET_",
    "HOST_",
    "CRATE_CC_NO_DEFAULTS",
)
SCRUBBED_EXACT = ("CC", "AR")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def pinned_toolchain() -> str:
    return str(tomllib.loads((RUST_DIR / "rust-toolchain.toml").read_text())["toolchain"]["channel"])


def pins(pin: str = PIN) -> dict[str, str]:
    return {str(k): str(v) for k, v in (yaml.safe_load(GROUP_VARS.read_text()).get(pin) or {}).items()}


def dist_path(arch: str, binary: str = BINARY) -> Path:
    return DIST / f"{binary}-{TARGETS[arch]}"


def build_env(target_dir: Path) -> dict[str, str]:
    env = {
        k: v for k, v in os.environ.items() if not k.startswith(SCRUBBED_PREFIXES) and k not in SCRUBBED_EXACT
    }
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
    for target in TARGETS.values():
        var = target.replace("-", "_")
        env[f"CC_{var}"] = CLANG
        env[f"AR_{var}"] = LLVM_AR
        env[f"CFLAGS_{var}"] = f"--target={target}"
    return env


def check_clang(env: dict[str, str]) -> None:
    """The pinned clang (ring's C and assembly): its exact version, or no build."""
    try:
        out = subprocess.run([CLANG, "--version"], env=env, capture_output=True, text=True, check=True).stdout
    except (OSError, subprocess.CalledProcessError):
        raise SystemExit(
            f"{CLANG} not found: install clang {CLANG_VERSION} (Ubuntu 24.04: clang-18)"
        ) from None
    first = out.splitlines()[0] if out else ""
    if f"clang version {CLANG_VERSION}" not in first:
        raise SystemExit(f"{CLANG} is {first!r}; scripts/build_rust.py pins clang {CLANG_VERSION}")
    if shutil.which(LLVM_AR, path=env.get("PATH")) is None:
        raise SystemExit(f"{LLVM_AR} not found (Ubuntu 24.04: apt install llvm-18)")


def check_toolchain(env: dict[str, str]) -> None:
    want = pinned_toolchain()
    out = subprocess.run(
        ["rustc", "--version"], cwd=RUST_DIR, env=env, capture_output=True, text=True, check=True
    )
    got = out.stdout.split()[1] if out.stdout.startswith("rustc ") else out.stdout.strip()
    if got != want:
        raise SystemExit(f"rustc {got} in rust/, rust-toolchain.toml pins {want} (is rustup on PATH?)")


def build(target_dir: Path) -> dict[tuple[str, str], bytes]:
    """Every (binary, arch)'s bytes, built into `target_dir`."""
    env = build_env(target_dir)
    check_toolchain(env)
    check_clang(env)
    subprocess.run(["cargo", "fetch", "--locked"], cwd=RUST_DIR, env=env, check=True)
    out: dict[tuple[str, str], bytes] = {}
    for binary, (_, profile) in BINARIES.items():
        for arch, target in TARGETS.items():
            argv = ["cargo", "build", "--profile", profile, "--locked", "--offline", "--target", target]
            argv += ["-p", binary]
            subprocess.run(argv, cwd=RUST_DIR, env=env, check=True)
            data = (target_dir / target / profile / binary).read_bytes()
            leaks = leaked_paths(data, target_dir)
            if leaks:
                raise SystemExit(f"{binary} {target}: the binary contains build paths: {', '.join(leaks)}")
            out[(binary, arch)] = data
    return out


def leaked_paths(data: bytes, target_dir: Path) -> list[str]:
    """Machine-specific directories that appear in a binary (none may)."""
    cargo_home = os.environ.get("CARGO_HOME", str(Path.home() / ".cargo"))
    candidates = {str(REPO_ROOT), str(target_dir), str(Path.home()), cargo_home}
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
        pin = BINARIES[binary][0]
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
        before = len(problems)
        if not path.is_file() or path.read_bytes() != data:
            problems.append(f"{rel} differs from the rebuild (sha256 {digest}): run build_rust.py build")
        if want.get(arch) != digest:
            problems.append(f"{pin}.{arch} is {want.get(arch)}, the rebuild is {digest}")
        if len(problems) == before:
            print(f"{rel}: reproducible, {len(data)} bytes, sha256 {digest}")
    for p in problems:
        print(p, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
