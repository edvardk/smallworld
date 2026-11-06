#!/usr/bin/env python3
"""May a lock update merge without a human? (P10 review / P11: the config repo's `server / gate` check.)

    verify_lock_update.py BASE_DIR HEAD_DIR   # each holds server.lock.yml and plugins.lock.yml (+
    client.lock.yml)

The weekly update job writes the lock files with update_lock.py, but the gate doesn't trust whoever pushed the
branch (the panel's GitHub App could push one too). It re-checks every changed entry against the upstream APIs
the lock tool reads, so only a real, newer build of the same server/plugin for the same Minecraft version
passes:

- server.lock.yml: minecraft_version, java_major (a Temurin bump is a product change), project and version
  unchanged; a newer build; Purpur: the canonical download URL, CI result SUCCESS, the API's md5 and the
  downloaded jar's sha256 equal the lock; Paper: the STABLE build's URL and sha256 from the Fill API.
- plugins.lock.yml: the same plugins (name, source, project) in the same order; each changed entry is a
  release of exactly that project for this Minecraft version (Modrinth: the version's project id and file;
  Hangar: the version's PAPER download and platform versions), published strictly AFTER the base's version
  (p11-review: no downgrades to an older, possibly vulnerable release; Modrinth `date_published`, Hangar
  `createdAt` of both versions from the API), and the downloaded file matches the API's hash and the lock.
- client.lock.yml (v1.1, optional): scripts/client_pack.py verify (the same entries; each changed one a newer
  Modrinth release of the same project for this version with the API's file and hashes, licence unchanged; a
  new
  or removed client lock needs a human).

Exit 0 = may merge; 1 = the reasons, one per line. Controller-side (PyYAML), like the lock tool.
"""

from __future__ import annotations

import datetime
import sys
import urllib.parse
from pathlib import Path
from typing import Any

from fetch_plugins import HANGAR_API, LOADERS, MODRINTH_API
from fetch_server_jar import PAPER_API, PURPUR_API
from lockfile import download_verified, get_json, load_yaml

SAME_SERVER = ("project", "version")


def _when(value: object) -> datetime.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=datetime.UTC)


def newer_release(name: str, new: object, old: object) -> list[str]:
    """The new version must have been published strictly after the old one (both dates from the API)."""
    n, o = _when(new), _when(old)
    if n is None or o is None or n <= o:
        return [
            f"{name}: not a newer release than the base's (published {new!r} vs {old!r}): "
            "a downgrade needs a human"
        ]
    return []


def check_server(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    reasons = [
        f"server.lock.yml {k} changed ({base.get(k)!r} -> {head.get(k)!r}): that needs a human"
        for k in ("minecraft_version", "java_major")
        if base.get(k) != head.get(k)
    ]
    b, h = base.get("server") or {}, head.get("server") or {}
    reasons += [
        f"server {k} changed ({b.get(k)!r} -> {h.get(k)!r})" for k in SAME_SERVER if b.get(k) != h.get(k)
    ]
    if reasons or b == h:
        return reasons
    if not isinstance(h.get("build"), int) or not isinstance(b.get("build"), int) or h["build"] <= b["build"]:
        return [f"server build must be newer ({b.get('build')} -> {h.get('build')})"]
    version, build = h["version"], h["build"]
    if h["project"] == "purpur":
        url = f"{PURPUR_API}/{version}/{build}/download"
        if h.get("url") != url or h.get("filename") != f"purpur-{version}-{build}.jar":
            return [f"purpur {version} build {build}: url/filename aren't the canonical ones"]
        meta = get_json(f"{PURPUR_API}/{version}/{build}")
        if meta.get("result") != "SUCCESS" or meta.get("md5") != h.get("md5"):
            return [f"purpur {version} build {build}: not a successful build with that md5"]
        download_verified(url, {"md5": meta["md5"], "sha256": str(h.get("sha256"))})
        return []
    builds = get_json(f"{PAPER_API}/versions/{version}/builds")
    found = next((x for x in builds if x.get("id") == build and x.get("channel") == "STABLE"), None)
    dl = (found or {}).get("downloads", {}).get("server:default") or {}
    sha256 = (dl.get("checksums") or {}).get("sha256")
    if (
        not found
        or dl.get("url") != h.get("url")
        or sha256 != h.get("sha256")
        or dl.get("name") != h.get("filename")
    ):
        return [f"paper {version} build {build}: not a STABLE build with that url/sha256/filename"]
    download_verified(str(h["url"]), {"sha256": str(sha256)})
    return []


def check_modrinth(p: dict[str, Any], mc_version: str, old: dict[str, Any]) -> list[str]:
    project = get_json(f"{MODRINTH_API}/project/{urllib.parse.quote(str(p['project']))}")
    v = get_json(f"{MODRINTH_API}/version/{urllib.parse.quote(str(p.get('version_id', '')))}")
    name = p["name"]
    if p.get("version_id") == old.get("version_id"):
        return [f"{name}: the version id didn't change but the entry did"]
    if v.get("project_id") != project.get("id"):
        return [f"{name}: version {p.get('version_id')} isn't from modrinth:{p['project']}"]
    if v.get("version_type") != "release" or mc_version not in (v.get("game_versions") or []):
        return [f"{name}: {v.get('version_number')} isn't a release for Minecraft {mc_version}"]
    if not set(v.get("loaders") or []) & set(LOADERS) or v.get("version_number") != p.get("version"):
        return [f"{name}: loaders/version number don't match the lock"]
    f = next((f for f in v.get("files") or [] if f.get("url") == p.get("url")), None)
    if not f or f.get("filename") != p.get("filename"):
        return [f"{name}: the lock's url/filename aren't a file of that version"]
    ov = get_json(f"{MODRINTH_API}/version/{urllib.parse.quote(str(old.get('version_id', '')))}")
    if ov.get("project_id") != project.get("id"):
        return [f"{name}: the base's version {old.get('version_id')} isn't from modrinth:{p['project']}"]
    if reasons := newer_release(name, v.get("date_published"), ov.get("date_published")):
        return reasons
    download_verified(str(p["url"]), {"sha512": f["hashes"]["sha512"], "sha256": str(p.get("sha256"))})
    return []


def check_hangar(p: dict[str, Any], mc_version: str, old: dict[str, Any]) -> list[str]:
    project, version, name = str(p["project"]), str(p.get("version", "")), p["name"]
    if version == str(old.get("version", "")):
        return [f"{name}: the version didn't change but the entry did"]
    v = get_json(
        f"{HANGAR_API}/projects/{urllib.parse.quote(project)}/versions/{urllib.parse.quote(version)}"
    )
    dl = (v.get("downloads") or {}).get("PAPER") or {}
    info = dl.get("fileInfo") or {}
    url = dl.get("downloadUrl") or dl.get("externalUrl")
    platforms = (v.get("platformDependencies") or {}).get("PAPER") or []
    if (
        v.get("name") != version
        or (v.get("channel") or {}).get("name") != "Release"
        or mc_version not in platforms
    ):
        return [f"{name}: hangar:{project} {version} isn't a release for Minecraft {mc_version}"]
    if (
        url != p.get("url")
        or info.get("sha256Hash") != p.get("sha256")
        or info.get("name") != p.get("filename")
    ):
        return [f"{name}: the lock's url/sha256/filename aren't that version's PAPER download"]
    ov = get_json(
        f"{HANGAR_API}/projects/{urllib.parse.quote(project)}/versions/"
        f"{urllib.parse.quote(str(old.get('version', '')))}"
    )
    if reasons := newer_release(name, v.get("createdAt"), ov.get("createdAt")):
        return reasons
    download_verified(str(url), {"sha256": str(p["sha256"])})
    return []


def check_plugins(base: dict[str, Any], head: dict[str, Any], mc_version: str) -> list[str]:
    key = ("name", "source", "project")
    b, h = base.get("plugins") or [], head.get("plugins") or []
    if [tuple(x.get(k) for k in key) for x in b] != [tuple(x.get(k) for k in key) for x in h]:
        return ["plugins.lock.yml: the set of plugins changed (adding or removing one needs a human)"]
    reasons: list[str] = []
    for old, new in zip(b, h, strict=True):
        if old == new:
            continue
        check = (
            check_modrinth
            if new["source"] == "modrinth"
            else check_hangar
            if new["source"] == "hangar"
            else None
        )
        reasons += (
            check(new, mc_version, old) if check else [f"{new['name']}: unknown source {new['source']!r}"]
        )
    return reasons


def verify(base_dir: Path, head_dir: Path) -> list[str]:
    try:
        bs, hs = load_yaml(base_dir / "server.lock.yml"), load_yaml(head_dir / "server.lock.yml")
        bp, hp = load_yaml(base_dir / "plugins.lock.yml"), load_yaml(head_dir / "plugins.lock.yml")
        reasons = check_server(bs, hs)
        if not reasons:
            reasons = check_plugins(bp, hp, str(hs.get("minecraft_version")))
        if not reasons:
            import client_pack

            def client(d: Path) -> dict[str, Any] | None:
                return load_yaml(d / "client.lock.yml") if (d / "client.lock.yml").is_file() else None

            reasons = client_pack.verify(client(base_dir), client(head_dir))
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        return [f"couldn't verify the lock update: {type(e).__name__}: {str(e)[:200]}"]
    return reasons


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 2:
        print("usage: verify_lock_update.py BASE_DIR HEAD_DIR", file=sys.stderr)
        return 2
    reasons = verify(Path(args[0]), Path(args[1]))
    for r in reasons:
        print(r)
    if not reasons:
        print("lock update verified against the upstream APIs")
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
