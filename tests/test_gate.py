"""P10 review / P11: `mcctl pr-gate`, the config repo's required `server / gate` check, and the upstream
verification of lock updates (scripts/verify_lock_update.py). No network: git repos in tmp, faked APIs."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
import verify_lock_update as vlu
import yaml
from conftest import MOD_JAR, REPO, V141_CLIENT_LOCK

from mcctl import clientpack, gate, http

A = "3f2a0000-0000-4000-8000-000000000c41"
B = "5c090000-0000-4000-8000-00000000e212"
PLAYERS = (
    f'whitelist:\n  - {{name: "Em_K", uuid: "{A}"}}\nops:\n  - {{name: "Em_K", uuid: "{A}", level: 4}}\n'
)
ADDED = (
    f'whitelist:\n  - {{name: "Em_K", uuid: "{A}"}}\n  - {{name: "jonas_r", uuid: "{B}"}}\n'
    f'ops:\n  - {{name: "Em_K", uuid: "{A}", level: 4}}\n'
)
OLD_SHA, NEW_SHA = "a" * 40, "b" * 40
WF = "jobs:\n  server:\n    uses: edvardk/smallworld/.github/workflows/server.yml@{sha} # {v}\n    secrets: {{}}\n"
SERVER_LOCK = (REPO / "template/server.lock.yml").read_text()
PLUGINS_LOCK = (REPO / "template/plugins.lock.yml").read_text()


SERVER_YML = "name: x\ngithub:\n  updater_app: x-updater\n  panel_app: x-panel\nautomation:\n  auto_merge: builds+patch\n"
BOTS = {"panel/": "x-panel[bot]", "update/": "x-updater[bot]"}


def evaluate(*args: Any, **kw: Any) -> gate.Verdict:
    """gate.evaluate with the PR author and event sender of a well-behaved App for the head branch (the
    branch's App), unless a test passes its own."""
    bot = next((b for prefix, b in BOTS.items() if kw.get("head_ref", "").startswith(prefix)), "")
    kw.setdefault("author", bot)
    kw.setdefault("sender", bot)
    return gate.evaluate(*args, **kw)


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def commit(root: Path, files: dict[str, str]) -> str:
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    git(root, "add", ".")
    git(root, "commit", "-qm", "change")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    base = commit(tmp_path, {
        "players.yml": PLAYERS, "server.yml": SERVER_YML,
        "server.lock.yml": SERVER_LOCK, "plugins.lock.yml": PLUGINS_LOCK,
        ".github/workflows/server.yml": WF.format(sha=OLD_SHA, v="v1.2.3"),
    })  # fmt: skip
    git(tmp_path, "checkout", "-qb", "topic")
    return tmp_path, base


def tags(sha: str = NEW_SHA, annotated: bool = True) -> http.Transport:
    def transport(req: http.Request) -> http.Response:
        if req.url.endswith("/git/ref/tags/v1.2.4"):
            obj = {"type": "tag", "sha": "c" * 40} if annotated else {"type": "commit", "sha": sha}
            return http.Response(200, {}, json.dumps({"object": obj}).encode())
        if req.url.endswith("/git/tags/" + "c" * 40):
            return http.Response(200, {}, json.dumps({"object": {"type": "commit", "sha": sha}}).encode())
        return http.Response(404, {}, b'{"message": "Not Found"}')

    return transport


def no_network(req: http.Request) -> http.Response:
    raise AssertionError(f"unexpected request {req.url}")


def test_whitelist_only_passes_and_ops_or_extra_files_need_the_admin(repo) -> None:
    root, base = repo
    head = commit(root, {"players.yml": ADDED})
    v = evaluate(root, base, head, transport=no_network, head_ref="panel/1700000000")
    assert v.ok and v.policy == "whitelist"
    head2 = commit(root, {"server.yml": "name: x\nautomation:\n  auto_merge: builds+patch\nminecraft: {}\n"})
    v2 = evaluate(root, base, head2, transport=no_network, head_ref="update/lock-1")
    assert not v2.ok and v2.policy == "admin" and "needs the admin" in v2.reasons[0]


def test_lock_updates_are_verified_and_respect_auto_merge(repo) -> None:
    root, base = repo
    head = commit(root, {"server.lock.yml": SERVER_LOCK.replace("build: 2655", "build: 2656")})
    seen: list[tuple[str, str]] = []

    def verify(b: Path, h: Path) -> list[str]:
        seen.append(((b / "server.lock.yml").read_text(), (h / "server.lock.yml").read_text()))
        return []

    assert evaluate(root, base, head, transport=no_network, verify=verify, head_ref="update/lock-1").ok
    assert "2655" in seen[0][0] and "2656" in seen[0][1]
    bad = evaluate(
        root,
        base,
        head,
        transport=no_network,
        verify=lambda b, h: ["sha256 mismatch"],
        head_ref="update/lock-1",
    )
    assert bad.reasons == ["sha256 mismatch"] and bad.policy == "builds"
    # the BASE decides the level: a PR can't switch auto-merge on for itself
    git(root, "checkout", "-q", "main")
    base2 = commit(
        root,
        {
            "server.yml": "name: x\ngithub:\n  updater_app: x-updater\n  panel_app: x-panel\nautomation:\n  auto_merge: none\n"
        },
    )
    git(root, "checkout", "-qb", "topic2")
    head2 = commit(
        root, {"plugins.lock.yml": PLUGINS_LOCK.replace("1.5.3", "1.5.4"), "server.yml": "name: x\n"}
    )
    assert (
        evaluate(root, base2, head2, transport=no_network, verify=verify, head_ref="update/lock-1").policy
        == "admin"
    )
    git(root, "checkout", "-q", "main")
    git(root, "checkout", "-qb", "topic3")
    head3 = commit(root, {"plugins.lock.yml": PLUGINS_LOCK.replace("1.5.3", "1.5.4")})
    v3 = evaluate(root, base2, head3, transport=no_network, verify=verify, head_ref="update/lock-1")
    assert not v3.ok and "auto_merge is none" in v3.reasons[0]


def test_product_patch_bumps_to_a_tagged_release_pass(repo) -> None:
    root, base = repo
    head = commit(root, {".github/workflows/server.yml": WF.format(sha=NEW_SHA, v="v1.2.4")})
    assert evaluate(root, base, head, transport=tags(), head_ref="update/lock-1").ok
    assert evaluate(root, base, head, transport=tags(annotated=False), head_ref="update/lock-1").ok
    wrong = evaluate(root, base, head, transport=tags(sha="d" * 40), head_ref="update/lock-1")
    assert not wrong.ok and "doesn't point at" in wrong.reasons[0]


@pytest.mark.parametrize(
    ("text", "why"),
    [
        (WF.format(sha=NEW_SHA, v="v1.3.0"), "isn't a patch release"),
        (WF.format(sha=NEW_SHA, v="v2.0.0"), "isn't a patch release"),
        (WF.format(sha=NEW_SHA, v="v1.2.3"), "isn't a patch release"),
        (
            WF.format(sha=NEW_SHA, v="v1.2.4").replace("secrets: {}", "secrets: inherit"),
            "more than the `uses:`",
        ),
        (
            WF.format(sha=NEW_SHA, v="v1.2.4").replace("edvardk/smallworld", "evil/fork"),
            "repository changed",
        ),
        (WF.format(sha="main", v="v1.2.4"), "full 40-character"),
        (
            WF.format(sha=NEW_SHA, v="v1.2.4+fix"),
            ".github/workflows/server.yml: the pin's comment must start",
        ),
        (WF.format(sha=NEW_SHA, v="v1.2.3-4-gabcdef1"), "an untagged pin (vX.Y.Z-N-g<sha>) never does"),
    ],
)
def test_product_bumps_that_need_the_admin(repo, text: str, why: str) -> None:
    root, base = repo
    head = commit(root, {".github/workflows/server.yml": text})
    v = evaluate(root, base, head, transport=tags(), head_ref="update/lock-1")
    assert not v.ok and why in " ".join(v.reasons), v.reasons


def test_product_bumps_need_builds_plus_patch(repo) -> None:
    root, _ = repo
    git(root, "checkout", "-q", "main")
    base = commit(
        root,
        {
            "server.yml": "name: x\ngithub:\n  updater_app: x-updater\n  panel_app: x-panel\nautomation:\n  auto_merge: builds\n"
        },
    )
    git(root, "checkout", "-qb", "p")
    head = commit(root, {".github/workflows/server.yml": WF.format(sha=NEW_SHA, v="v1.2.4")})
    v = evaluate(root, base, head, transport=tags(), head_ref="update/lock-1")
    assert not v.ok and "auto_merge is builds" in v.reasons[0]


# --- v1.5.1: a patch bump that also moves the in-house client mod entries to the new release's --------------

V150_LOCK = (REPO / "template/client.lock.yml").read_text()  # v1.5.0's: mod 1.1.0


def release(files: dict[str, bytes] | None = None, sha: str = NEW_SHA) -> http.Transport:
    """tags() plus the contents API of the release at `sha` (default: v1.5.0's template lock and mod jar)."""
    files = files if files is not None else {
        "template/client.lock.yml": V150_LOCK.encode(), MOD_JAR: (REPO / MOD_JAR).read_bytes(),
    }  # fmt: skip
    tag = tags(sha)

    def transport(req: http.Request) -> http.Response:
        prefix = "https://api.github.com/repos/edvardk/smallworld/contents/"
        if req.url.startswith(prefix):
            path, _, ref = req.url.removeprefix(prefix).partition("?ref=")
            if ref == sha and path in files:
                body = {"encoding": "base64", "content": base64.b64encode(files[path]).decode()}
                return http.Response(200, {}, json.dumps(body).encode())
            return http.Response(404, {}, b'{"message": "Not Found"}')
        return tag(req)

    return transport


@pytest.fixture
def lock_repo(repo) -> tuple[Path, str]:
    """The base pins v1.2.3 with v1.4.1's client.lock.yml (the live config repo: mod 1.0.0)."""
    root, _ = repo
    git(root, "checkout", "-q", "main")
    base = commit(root, {"client.lock.yml": V141_CLIENT_LOCK.read_text()})
    git(root, "checkout", "-qb", "bump")
    return root, base


def test_a_patch_bump_with_the_releases_in_house_entries_passes(lock_repo) -> None:
    """The decision (gate docstring, SECURITY.md): the in-house jar is the product's own, committed at the tag
    the gate already verifies, so moving its entries to exactly the new release's is as safe as the pin."""
    root, base = lock_repo
    wf = WF.format(sha=NEW_SHA, v="v1.2.4")
    head = commit(root, {".github/workflows/server.yml": wf, "client.lock.yml": V150_LOCK})
    v = evaluate(root, base, head, transport=release(), head_ref="update/product-v1.2.4")
    assert v.ok and v.policy == "product", v.reasons
    # the same lock change with a minor bump, or from another branch/App: the admin
    git(root, "checkout", "-q", "main")
    git(root, "checkout", "-qb", "minor")
    minor = commit(root, {".github/workflows/server.yml": WF.format(sha=NEW_SHA, v="v1.3.0"),
                          "client.lock.yml": V150_LOCK})  # fmt: skip
    v = evaluate(root, base, minor, transport=release(), head_ref="update/product-v1.3.0")
    assert not v.ok and "isn't a patch release" in v.reasons[0]
    v = evaluate(root, base, head, transport=release(), head_ref="feature/x")
    assert not v.ok and v.policy == "admin"
    v = evaluate(root, base, head, transport=release(), head_ref="update/product-v1.2.4", author="octocat")
    assert not v.ok and "octocat" in v.reasons[0]


def test_in_house_entries_that_arent_the_releases_need_the_admin(lock_repo) -> None:
    root, base = lock_repo
    wf = WF.format(sha=NEW_SHA, v="v1.2.4")
    lock = yaml.safe_load(V150_LOCK)
    next(f for f in lock["files"] if f["slug"] == "smallworld")["sha256"] = "0" * 64  # someone's own jar
    forged = commit(root, {".github/workflows/server.yml": wf, "client.lock.yml": yaml.safe_dump(lock)})
    v = evaluate(root, base, forged, transport=release(), head_ref="update/product-v1.2.4")
    assert not v.ok and "isn't exactly the new release's entry" in " ".join(v.reasons)
    # a Modrinth entry riding along
    lock = yaml.safe_load(V150_LOCK)
    next(f for f in lock["files"] if f["slug"] == "sodium")["url"] = "https://cdn.modrinth.com/data/evil.jar"
    other = commit(root, {"client.lock.yml": yaml.safe_dump(lock, sort_keys=False)})
    v = evaluate(root, base, other, transport=release(), head_ref="update/product-v1.2.4")
    assert not v.ok and "isn't an in-house mod" in " ".join(v.reasons)
    # the release's entries are read at the tag-verified commit: the tag pointing elsewhere stops it first
    good = commit(root, {"client.lock.yml": V150_LOCK})
    v = evaluate(root, base, good, transport=release(sha="d" * 40), head_ref="update/product-v1.2.4")
    assert not v.ok and "doesn't point at" in v.reasons[0]
    # a release whose template entry doesn't match its jar, or that ships no in-house mod
    stale = release({"template/client.lock.yml": V150_LOCK.encode(), MOD_JAR: b"other"})
    v = evaluate(root, base, good, transport=stale, head_ref="update/product-v1.2.4")
    assert not v.ok and "couldn't read v1.2.4's in-house client mods" in v.reasons[0]
    v = evaluate(root, base, good, transport=release({}), head_ref="update/product-v1.2.4")
    assert not v.ok and "ships no in-house client mod" in v.reasons[0]
    assert evaluate(root, base, good, transport=release(), head_ref="update/product-v1.2.4").ok


def test_a_new_client_lock_with_a_product_bump_needs_the_admin(repo) -> None:
    root, base = repo
    head = commit(root, {".github/workflows/server.yml": WF.format(sha=NEW_SHA, v="v1.2.4"),
                         "client.lock.yml": V150_LOCK})  # fmt: skip
    v = evaluate(root, base, head, transport=release(), head_ref="update/product-v1.2.4")
    assert not v.ok and "must exist on both sides" in v.reasons[0]
    assert clientpack.TEMPLATE_LOCK == "template/client.lock.yml"


def test_cli_pr_gate(repo, capsys, tmp_path) -> None:
    from mcctl import cli

    root, base = repo
    head = commit(root, {"players.yml": ADDED})
    out = tmp_path / "out"
    assert (
        cli.main(
            [
                "pr-gate",
                "-C",
                str(root),
                "--base",
                base,
                "--head",
                head,
                "--head-ref",
                "panel/1",
                "--author",
                "x-panel[bot]",
                "--sender",
                "x-panel[bot]",
                "--github-output",
                str(out),
            ]
        )
        == 0
    )
    assert out.read_text() == "policy=whitelist\n"
    head2 = commit(root, {"README.md": "x\n"})
    assert cli.main(["pr-gate", "-C", str(root), "--base", base, "--head", head2]) == 1
    assert "needs the admin" in capsys.readouterr().err


# --- scripts/verify_lock_update.py: every changed entry against the upstream APIs (faked) -------------------


class Upstream:
    def __init__(self, files: dict[str, bytes], api: dict[str, Any]) -> None:
        self.files, self.api, self.calls = files, api, []

    def get_json(self, url: str) -> Any:
        self.calls.append(url)
        if url not in self.api:
            raise OSError(f"404 {url}")
        return self.api[url]

    def download(self, url: str, expected: dict[str, str]) -> bytes:
        data = self.files[url]
        for algo, want in expected.items():
            if hashlib.new(algo, data).hexdigest() != want:
                raise ValueError(f"{algo} mismatch for {url}")
        return data


def lock_pair(tmp_path: Path, server_change: dict | None = None, plugin_change: dict | None = None):
    base = {"server": yaml.safe_load(SERVER_LOCK), "plugins": yaml.safe_load(PLUGINS_LOCK)}
    head = copy.deepcopy(base)
    if server_change:
        head["server"]["server"].update(server_change)
    if plugin_change:
        head["plugins"]["plugins"][0].update(plugin_change)
    dirs = []
    for label, data in (("base", base), ("head", head)):
        d = tmp_path / label
        d.mkdir(parents=True)
        (d / "server.lock.yml").write_text(yaml.safe_dump(data["server"]))
        (d / "plugins.lock.yml").write_text(yaml.safe_dump(data["plugins"]))
        dirs.append(d)
    return dirs


def test_a_newer_purpur_build_is_verified_against_the_api(tmp_path, monkeypatch) -> None:
    jar = b"new purpur jar"
    md5, sha = hashlib.md5(jar).hexdigest(), hashlib.sha256(jar).hexdigest()  # noqa: S324 - Purpur's API hash
    url = "https://api.purpurmc.org/v2/purpur/26.3/2656/download"
    up = Upstream(
        {url: jar}, {"https://api.purpurmc.org/v2/purpur/26.3/2656": {"result": "SUCCESS", "md5": md5}}
    )
    monkeypatch.setattr(vlu, "get_json", up.get_json)
    monkeypatch.setattr(vlu, "download_verified", up.download)
    good = {"build": 2656, "url": url, "filename": "purpur-26.3-2656.jar", "md5": md5, "sha256": sha}
    assert vlu.verify(*lock_pair(tmp_path / "1", good)) == []
    # a foreign URL, a wrong hash, an older build, a failed CI build, a version change: refused
    for change, why in (
        ({"url": "https://evil.example/purpur.jar"}, "canonical"),
        ({"sha256": "0" * 64}, "mismatch"),
        ({"build": 2600, "url": url.replace("2656", "2600"), "filename": "purpur-26.3-2600.jar"}, "newer"),
        ({"version": "26.4"}, "version changed"),
    ):
        reasons = vlu.verify(*lock_pair(tmp_path / str(len(why)) / why[:4], good | change))
        assert reasons and why in " ".join(reasons), (change, reasons)
    up.api["https://api.purpurmc.org/v2/purpur/26.3/2656"] = {"result": "FAILURE", "md5": md5}
    assert "not a successful build" in vlu.verify(*lock_pair(tmp_path / "f", good))[0]


def test_a_newer_modrinth_release_is_verified_against_the_api(tmp_path, monkeypatch) -> None:
    jar = b"chunky 1.5.4"
    url = "https://cdn.modrinth.com/data/fALzjamp/versions/NEWID/Chunky-Bukkit-1.5.4.jar"
    version = {"project_id": "fALzjamp", "version_type": "release", "game_versions": ["26.3"], "loaders": ["paper"],
               "version_number": "1.5.4", "files": [{"url": url, "filename": "Chunky-Bukkit-1.5.4.jar",
                                                     "hashes": {"sha512": hashlib.sha512(jar).hexdigest()}}]}  # fmt: skip
    version["date_published"] = "2026-10-20T10:00:00.123456Z"
    old = {"project_id": "fALzjamp", "date_published": "2026-03-01T09:00:00Z"}
    up = Upstream({url: jar}, {"https://api.modrinth.com/v2/project/chunky": {"id": "fALzjamp"},
                               "https://api.modrinth.com/v2/version/NEWID": version,
                               "https://api.modrinth.com/v2/version/MdY6JATr": old})  # fmt: skip
    monkeypatch.setattr(vlu, "get_json", up.get_json)
    monkeypatch.setattr(vlu, "download_verified", up.download)
    good = {"version": "1.5.4", "version_id": "NEWID", "filename": "Chunky-Bukkit-1.5.4.jar", "url": url,
            "sha256": hashlib.sha256(jar).hexdigest()}  # fmt: skip
    assert vlu.verify(*lock_pair(tmp_path / "1", plugin_change=good)) == []
    version["project_id"] = "someone-else"
    assert "isn't from modrinth:chunky" in vlu.verify(*lock_pair(tmp_path / "2", plugin_change=good))[0]
    version["project_id"] = "fALzjamp"
    version["game_versions"] = ["26.4"]
    assert (
        "isn't a release for Minecraft 26.3" in vlu.verify(*lock_pair(tmp_path / "3", plugin_change=good))[0]
    )
    renamed = vlu.verify(*lock_pair(tmp_path / "4", plugin_change={"project": "evil"}))
    assert "set of plugins changed" in renamed[0]

    # p11-review: an older release (a downgrade) of the same project needs a human
    version["game_versions"] = ["26.3"]
    version["date_published"] = "2025-12-01T00:00:00Z"
    down = vlu.verify(*lock_pair(tmp_path / "5", plugin_change=good))
    assert down and "not a newer release" in down[0]


def _dirs(tmp_path: Path, base: dict, head: dict) -> tuple[Path, Path]:
    out = []
    for label, data in (("base", base), ("head", head)):
        d = tmp_path / label
        d.mkdir(parents=True)
        (d / "server.lock.yml").write_text(yaml.safe_dump(data["server"]))
        (d / "plugins.lock.yml").write_text(yaml.safe_dump(data["plugins"]))
        out.append(d)
    return out[0], out[1]


def test_a_newer_paper_build_is_verified_against_the_fill_api(tmp_path, monkeypatch) -> None:
    jar = b"paper jar 120"
    sha = hashlib.sha256(jar).hexdigest()
    url = "https://fill-data.papermc.io/v1/objects/x/paper-26.3-120.jar"
    srv = yaml.safe_load(SERVER_LOCK)
    srv["server"] = {"project": "paper", "version": "26.3", "build": 119, "filename": "paper-26.3-119.jar",
                     "url": url.replace("120", "119"), "sha256": "1" * 64}  # fmt: skip
    plugins = yaml.safe_load(PLUGINS_LOCK)
    head_srv = copy.deepcopy(srv)
    head_srv["server"].update({"build": 120, "filename": "paper-26.3-120.jar", "url": url, "sha256": sha})
    build = {"id": 120, "channel": "STABLE",
             "downloads": {"server:default": {"name": "paper-26.3-120.jar", "url": url, "checksums": {"sha256": sha}}}}  # fmt: skip
    builds_url = f"{vlu.PAPER_API}/versions/26.3/builds"
    up = Upstream({url: jar}, {builds_url: [build]})
    monkeypatch.setattr(vlu, "get_json", up.get_json)
    monkeypatch.setattr(vlu, "download_verified", up.download)
    base = {"server": srv, "plugins": plugins}
    assert vlu.verify(*_dirs(tmp_path / "ok", base, {"server": head_srv, "plugins": plugins})) == []
    build["channel"] = "BETA"
    beta = vlu.verify(*_dirs(tmp_path / "beta", base, {"server": head_srv, "plugins": plugins}))
    assert "not a STABLE build" in beta[0]
    build["channel"] = "STABLE"
    older = copy.deepcopy(head_srv)
    older["server"]["build"] = 100
    assert (
        "must be newer"
        in vlu.verify(*_dirs(tmp_path / "old", base, {"server": older, "plugins": plugins}))[0]
    )


def test_a_newer_hangar_release_is_verified_against_the_api(tmp_path, monkeypatch) -> None:
    jar = b"some hangar plugin 2.1"
    sha = hashlib.sha256(jar).hexdigest()
    url = "https://hangarcdn.papermc.io/plugins/Owner/Thing/versions/2.1/PAPER/Thing-2.1.jar"
    srv = yaml.safe_load(SERVER_LOCK)
    old_entry = {"name": "Thing", "source": "hangar", "project": "Thing", "version": "2.0",
                 "filename": "Thing-2.0.jar", "url": url.replace("2.1", "2.0"), "sha256": "2" * 64}  # fmt: skip
    new_entry = old_entry | {"version": "2.1", "filename": "Thing-2.1.jar", "url": url, "sha256": sha}
    api = f"{vlu.HANGAR_API}/projects/Thing/versions"
    v21 = {"name": "2.1", "channel": {"name": "Release"}, "platformDependencies": {"PAPER": ["26.3"]},
           "createdAt": "2026-10-01T12:00:00.5Z",
           "downloads": {"PAPER": {"downloadUrl": url, "fileInfo": {"name": "Thing-2.1.jar", "sha256Hash": sha}}}}  # fmt: skip
    up = Upstream(
        {url: jar}, {f"{api}/2.1": v21, f"{api}/2.0": {"name": "2.0", "createdAt": "2026-05-01T00:00:00Z"}}
    )
    monkeypatch.setattr(vlu, "get_json", up.get_json)
    monkeypatch.setattr(vlu, "download_verified", up.download)
    base = {"server": srv, "plugins": {"plugins": [old_entry]}}
    head = {"server": srv, "plugins": {"plugins": [new_entry]}}
    assert vlu.verify(*_dirs(tmp_path / "ok", base, head)) == []
    v21["platformDependencies"] = {"PAPER": ["26.4"]}
    assert "isn't a release for Minecraft 26.3" in vlu.verify(*_dirs(tmp_path / "mc", base, head))[0]
    v21["platformDependencies"] = {"PAPER": ["26.3"]}
    v21["createdAt"] = "2026-01-01T00:00:00Z"  # older than the base's 2.0: a downgrade
    assert "not a newer release" in vlu.verify(*_dirs(tmp_path / "down", base, head))[0]


# --- p11-review: the head branch binds the policy ----------------------------------------------------------


def test_policies_are_bound_to_the_head_branch(repo) -> None:
    root, base = repo
    head = commit(root, {"players.yml": ADDED})
    for ref in ("update/lock-1", "feature", ""):
        v = evaluate(root, base, head, transport=no_network, head_ref=ref)
        assert not v.ok and v.policy == "admin" and "panel/" in v.reasons[0]
    git(root, "checkout", "-q", "main")
    git(root, "checkout", "-qb", "locks")
    lock_head = commit(root, {"server.lock.yml": SERVER_LOCK.replace("build: 2655", "build: 2656")})
    for ref in ("panel/1700000000", "main2", ""):  # the panel App can only write panel/*: never a lock change
        v = evaluate(root, base, lock_head, transport=no_network, verify=lambda b, h: [], head_ref=ref)
        assert not v.ok and "update/" in v.reasons[0]
    assert evaluate(
        root, base, lock_head, transport=no_network, verify=lambda b, h: [], head_ref="update/lock-1"
    ).ok


def test_a_rename_is_two_changed_files(repo) -> None:
    root, base = repo
    git(root, "mv", "server.yml", "players2.yml")
    (root / "players.yml").write_text(ADDED)
    git(root, "add", ".")
    git(root, "commit", "-qm", "rename")
    head = git(root, "rev-parse", "HEAD")
    v = evaluate(root, base, head, transport=no_network, head_ref="panel/1")
    assert not v.ok and "server.yml" in v.reasons[0]
    from mcctl import panelpr

    assert "server.yml" in " ".join(panelpr.check(root, base, head))


# --- first live apply: no App bypass actor on a personal repo, so the policy is bound to the App's bot user --------


def test_policies_are_bound_to_the_apps_bot_user(repo) -> None:
    """update/* and panel/* are writable by both Apps (terraform/github.tf): the gate passes a policy only when
    the PR was opened by, and the gate's event sent by, that policy's App (slug from the BASE server.yml)."""
    root, base = repo
    git(root, "checkout", "-qb", "locks")
    lock_head = commit(root, {"server.lock.yml": SERVER_LOCK.replace("build: 2655", "build: 2656")})
    ok = {"transport": no_network, "verify": lambda b, h: [], "head_ref": "update/lock-1"}
    assert evaluate(root, base, lock_head, **ok).ok
    # the panel App opens an update/* PR, or pushes onto the updater's (it is then the sender), or a user does
    for author, sender in [
        ("x-panel[bot]", "x-panel[bot]"),
        ("x-updater[bot]", "x-panel[bot]"),
        ("octocat", "octocat"),
        ("", ""),
        ("x-updater", "x-updater"),  # a user named like the slug isn't the bot
    ]:
        v = evaluate(root, base, lock_head, author=author, sender=sender, **ok)
        assert not v.ok and v.policy == "builds" and "x-updater[bot]" in " ".join(v.reasons), (author, sender)
    git(root, "checkout", "-q", "topic")
    wl_head = commit(root, {"players.yml": ADDED})
    assert evaluate(root, base, wl_head, transport=no_network, head_ref="panel/1").ok
    v = evaluate(
        root, base, wl_head, transport=no_network, head_ref="panel/1",
        author="x-updater[bot]", sender="x-updater[bot]",
    )  # fmt: skip
    assert not v.ok and v.policy == "whitelist" and "x-panel[bot]" in v.reasons[0]


def test_no_app_slug_on_the_base_means_the_admin(repo) -> None:
    root, base = repo
    base = commit(root, {"server.yml": "name: x\nautomation:\n  auto_merge: builds+patch\n"})
    head = commit(root, {"players.yml": ADDED})
    v = evaluate(root, base, head, transport=no_network, head_ref="panel/1")
    assert not v.ok and "github.panel_app is empty on the base" in v.reasons[0]
    # a PR can't name its own App: the slug is read from the base only
    head2 = commit(root, {"server.yml": SERVER_YML, "players.yml": PLAYERS})
    v = evaluate(root, head, head2, transport=no_network, head_ref="panel/2")
    assert not v.ok


def test_the_gate_job_passes_author_and_sender() -> None:
    wf = (REPO / ".github/workflows/server.yml").read_text()
    assert "PR_AUTHOR: ${{ github.event.pull_request.user.login }}" in wf
    assert "PR_SENDER: ${{ github.event.sender.login }}" in wf
    assert '--author "$PR_AUTHOR" --sender "$PR_SENDER"' in wf
