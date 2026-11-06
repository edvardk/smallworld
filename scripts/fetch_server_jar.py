#!/usr/bin/env python3
"""Resolve and verify the pinned server jar (Purpur; Paper only with --allow-paper).

    fetch_server_jar.py resolve --minecraft-version 26.3   # print lock entry for latest build
    fetch_server_jar.py verify                              # download the locked jar and check its hashes
    fetch_server_jar.py download --dest DIR                 # verify, then write the jar to DIR

Ansible downloads the jar itself (get_url + sha256 from server.lock.yml); this script is for
lock updates and CI verification.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

import yaml
from lockfile import SERVER_LOCK, digest, download_verified, get_json, load_yaml

PURPUR_API = "https://api.purpurmc.org/v2/purpur"
PAPER_API = "https://fill.papermc.io/v3/projects/paper"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"


def java_major_for(mc_version: str) -> int:
    """Java major version Mojang declares for this release (verify, don't assume)."""
    manifest = get_json(MOJANG_MANIFEST)
    for v in manifest["versions"]:
        if v["id"] == mc_version:
            return int(get_json(v["url"])["javaVersion"]["majorVersion"])
    raise LookupError(f"Minecraft {mc_version} not in Mojang manifest")


def resolve_purpur(mc_version: str) -> dict[str, Any] | None:
    """Newest Purpur build for mc_version whose CI result is SUCCESS (None if there is none)."""
    try:
        builds = get_json(f"{PURPUR_API}/{mc_version}")["builds"]
    except Exception:
        return None
    for build in sorted(builds["all"], key=int, reverse=True):
        meta = get_json(f"{PURPUR_API}/{mc_version}/{build}")
        if meta.get("result") == "SUCCESS":
            break
    else:
        return None
    url = f"{PURPUR_API}/{mc_version}/{build}/download"
    data = download_verified(url, {"md5": meta["md5"]})
    return {
        "project": "purpur",
        "version": mc_version,
        "build": int(build),
        "filename": f"purpur-{mc_version}-{build}.jar",
        "url": url,
        "sha256": digest(data, "sha256"),
        "md5": meta["md5"],
    }


def resolve_paper(mc_version: str) -> dict[str, Any] | None:
    try:
        builds = get_json(f"{PAPER_API}/versions/{mc_version}/builds")
    except Exception:
        return None
    stable = [b for b in builds if b.get("channel") == "STABLE"]
    if not stable:
        return None
    b = max(stable, key=lambda x: x["id"])
    dl = b["downloads"]["server:default"]
    sha256 = dl["checksums"]["sha256"]
    download_verified(dl["url"], {"sha256": sha256})
    return {
        "project": "paper",
        "version": mc_version,
        "build": int(b["id"]),
        "filename": dl["name"],
        "url": dl["url"],
        "sha256": sha256,
    }


def resolve(mc_version: str, allow_paper: bool = False) -> dict[str, Any]:
    entry = resolve_purpur(mc_version)
    if entry is None:
        if not allow_paper:
            raise LookupError(
                f"no successful Purpur build for {mc_version}; pass --allow-paper to fall back to Paper "
                "(Purpur-only settings in server-config/purpur.yml.j2 would then not apply)"
            )
        entry = resolve_paper(mc_version)
    if entry is None:
        raise LookupError(f"no successful Purpur or stable Paper build for {mc_version}")
    return {"minecraft_version": mc_version, "java_major": java_major_for(mc_version), "server": entry}


def verify_locked(lock_path: Path = SERVER_LOCK) -> bytes:
    server = load_yaml(lock_path)["server"]
    expected = {"sha256": server["sha256"]}
    if "md5" in server:
        expected["md5"] = server["md5"]
    return download_verified(server["url"], expected)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--minecraft-version", required=True)
    r.add_argument("--allow-paper", action="store_true", help="fall back to Paper if Purpur has no build")
    sub.add_parser("verify")
    d = sub.add_parser("download")
    d.add_argument("--dest", type=Path, required=True)
    args = p.parse_args(argv)

    if args.cmd == "resolve":
        print(yaml.safe_dump(resolve(args.minecraft_version, args.allow_paper), sort_keys=False))
    elif args.cmd == "verify":
        verify_locked()
        print("server jar OK")
    else:
        data = verify_locked()
        name = load_yaml(SERVER_LOCK)["server"]["filename"]
        args.dest.mkdir(parents=True, exist_ok=True)
        (args.dest / name).write_bytes(data)
        print(args.dest / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
