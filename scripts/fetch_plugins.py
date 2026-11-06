#!/usr/bin/env python3
"""Resolve and verify plugins from Modrinth (preferred) or Hangar (fallback).

    fetch_plugins.py resolve --minecraft-version 26.3   # print lock entries for latest compatible releases
    fetch_plugins.py verify                              # download every locked plugin and check hashes
    fetch_plugins.py download --dest DIR                 # verify, then write jars to DIR

Plugins to track are listed in plugins.lock.yml (name, source, project). Ansible downloads the jars
itself with get_url + sha256 and fails the deploy on any mismatch.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
from pathlib import Path
from typing import Any

import yaml
from lockfile import PLUGINS_LOCK, digest, download_verified, get_json, load_yaml

MODRINTH_API = "https://api.modrinth.com/v2"
HANGAR_API = "https://hangar.papermc.io/api/v1"
LOADERS = ["paper", "purpur", "spigot", "bukkit"]


def resolve_modrinth(project: str, mc_version: str) -> dict[str, Any]:
    q = urllib.parse.urlencode(
        {
            "loaders": json.dumps(LOADERS),
            "game_versions": json.dumps([mc_version]),
            "include_changelog": "false",
        }
    )
    versions = [
        v for v in get_json(f"{MODRINTH_API}/project/{project}/version?{q}") if v["version_type"] == "release"
    ]
    if not versions:
        raise LookupError(f"modrinth:{project} has no release for Minecraft {mc_version}")
    v = max(versions, key=lambda x: x["date_published"])
    # Not every version marks a primary file; Modrinth then treats the first one as primary.
    f = next((f for f in v["files"] if f.get("primary")), v["files"][0])
    data = download_verified(f["url"], {"sha512": f["hashes"]["sha512"]})
    return {
        "version": v["version_number"],
        "version_id": v["id"],
        "filename": f["filename"],
        "url": f["url"],
        "sha256": digest(data, "sha256"),
    }


def resolve_hangar(project: str, mc_version: str) -> dict[str, Any]:
    q = urllib.parse.urlencode({"platform": "PAPER", "platformVersion": mc_version, "channel": "Release"})
    result = get_json(f"{HANGAR_API}/projects/{project}/versions?{q}")["result"]
    if not result:
        raise LookupError(f"hangar:{project} has no release for Minecraft {mc_version}")
    v = max(result, key=lambda x: x["createdAt"])
    dl = v["downloads"]["PAPER"]
    sha256 = dl["fileInfo"]["sha256Hash"]
    url = dl.get("downloadUrl") or dl["externalUrl"]
    download_verified(url, {"sha256": sha256})
    return {"version": v["name"], "filename": dl["fileInfo"]["name"], "url": url, "sha256": sha256}


RESOLVERS = {"modrinth": resolve_modrinth, "hangar": resolve_hangar}


def resolve(plugin: dict[str, Any], mc_version: str) -> dict[str, Any]:
    pinned = {k: plugin[k] for k in ("name", "source", "project")}
    return pinned | RESOLVERS[plugin["source"]](plugin["project"], mc_version)


def verify_locked(lock_path: Path = PLUGINS_LOCK) -> dict[str, bytes]:
    out = {}
    for p in load_yaml(lock_path)["plugins"]:
        out[p["filename"]] = download_verified(p["url"], {"sha256": p["sha256"]})
        print(f"{p['name']} {p['version']} OK")
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("resolve")
    r.add_argument("--minecraft-version", required=True)
    sub.add_parser("verify")
    d = sub.add_parser("download")
    d.add_argument("--dest", type=Path, required=True)
    args = p.parse_args(argv)

    if args.cmd == "resolve":
        plugins = load_yaml(PLUGINS_LOCK)["plugins"]
        print(
            yaml.safe_dump(
                {"plugins": [resolve(pl, args.minecraft_version) for pl in plugins]}, sort_keys=False
            )
        )
    elif args.cmd == "verify":
        verify_locked()
    else:
        args.dest.mkdir(parents=True, exist_ok=True)
        for name, data in verify_locked().items():
            (args.dest / name).write_bytes(data)
    return 0


if __name__ == "__main__":
    sys.exit(main())
