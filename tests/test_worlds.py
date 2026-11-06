"""v1.2 world library: scripts/worlds.py (presets, archives, the switch sequence, the folder swap, server.yml's
world section), its hooks in backup.py/restore.py/health.py, mc-agent's world_* actions, the pending -> pull
request flow (mc_panel), `mcctl panel-pr-check`/`pr-gate` for world PRs, the schema and the render. No systemd,
no restic, no network: the outside effects are faked (worlds.Ops). What only the e2e run proves (real restic,
real systemd units and polkit, a real server generating and loading the worlds): tests/e2e/agent_check.py."""

from __future__ import annotations

import copy
import gzip
import json
import re
import struct
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import backup
import health
import mc_agent
import mc_panel
import notify
import pytest
import restore
import worlds
import yaml
from conftest import REPO

from mcctl import gate, panelpr, render, schema

KEYS = REPO / "server-config/keys/26.3/server.properties.txt"
FULL = REPO / "examples/full/server.yml"
WORKER = (REPO / "watchdog/worker.js").read_text()
PANEL_JS = (REPO / "watchdog/panel/panel.js").read_text()
CONFIRM_JS = (REPO / "watchdog/panel/confirm.js").read_text()
NOW = datetime(2026, 11, 5, 12, 0, tzinfo=UTC)


def level_dat(world: Path, dv: int) -> None:
    """A minimal gzipped NBT level.dat: {"": {"Data": {"DataVersion": dv}}}."""

    def name(s: str) -> bytes:
        return struct.pack(">H", len(s)) + s.encode()

    body = b"\x0a" + name("") + b"\x0a" + name("Data") + b"\x03" + name("DataVersion") + struct.pack(">i", dv)
    world.mkdir(parents=True, exist_ok=True)
    (world / "level.dat").write_bytes(gzip.compress(body + b"\x00\x00"))


def snap(name: str, sid: str, time: str, dv: int | None = 4671, size: int = 2 * 10**9) -> dict[str, Any]:
    tags = ["world-archive", f"world:{name}"] + ([f"dv:{dv}"] if dv is not None else [])
    return {
        "id": sid * 8,
        "short_id": sid,
        "time": time,
        "tags": tags,
        "summary": {"total_bytes_processed": size},
    }


# --- product constants: verified keys, one list everywhere -----------------------------------------------------


def test_every_world_key_exists_in_the_pinned_server_properties() -> None:
    known = set(KEYS.read_text().split())
    for table in (worlds.TYPES, worlds.PRESETS):
        for props in table.values():
            assert set(props) <= known, set(props) - known
    assert set(worlds.MANAGED_KEYS) <= known
    assert {"pvp", "spawn-monsters", "spawn-animals"}.isdisjoint(
        known
    )  # game rules in 26.3, never properties
    assert sorted(schema.WORLD_PROPERTY_KEYS) == worlds.MANAGED_KEYS


def test_types_presets_and_patterns_agree_everywhere() -> None:
    types = list(worlds.TYPES)
    presets = list(worlds.PRESETS)
    assert list(schema.WorldType.__args__) == types  # type: ignore[attr-defined]
    assert list(schema.WorldPreset.__args__) == presets  # type: ignore[attr-defined]
    assert schema.WORLD_NAME == f"^{worlds.WORLD_NAME.pattern}$" and schema.SEED == f"^{worlds.SEED.pattern}$"

    def js_list(src: str, const: str) -> list[str]:
        m = re.search(rf"const {const} = \[(.*?)\];", src, re.S)
        assert m, const
        return re.findall(r'\[?"([a-z_]+)"', m.group(1))

    assert js_list(WORKER, "WORLD_TYPES") == types and js_list(WORKER, "WORLD_PRESETS") == presets
    assert [t for t in js_list(PANEL_JS, "WORLD_TYPES") if t in worlds.TYPES] == types
    assert [p for p in js_list(PANEL_JS, "WORLD_PRESETS") if p in worlds.PRESETS] == presets
    assert f"const WORLD_NAME = /^{worlds.WORLD_NAME.pattern}$/;" in WORKER
    assert f"const WORLD_NAME_RE = /^{worlds.WORLD_NAME.pattern}$/;" in PANEL_JS


def test_flat_types_are_valid_generator_settings() -> None:
    for name, props in worlds.TYPES.items():
        gen = json.loads(props["generator-settings"])
        if props["level-type"] == "minecraft:flat":
            assert gen["layers"] and all(set(layer) == {"block", "height"} for layer in gen["layers"]), name
            assert gen["biome"].startswith("minecraft:")
        else:
            assert gen == {}, name
    assert worlds.TYPES["flat_void"]["generate-structures"] == "false"


# --- entries and server.properties -----------------------------------------------------------------------------


def test_check_entry_fills_defaults_and_refuses_bad_values() -> None:
    e = worlds.check_entry({"name": "creative-2026"})
    assert e == {"name": "creative-2026", "seed": "", "type": "default", "preset": None,
                 "border_radius": None, "pregen": None}  # fmt: skip
    for bad in ({"name": "Big"}, {"name": "-x"}, {"name": "a" * 33}, {"name": "a", "seed": "abc"},
                {"name": "a", "seed": "1" * 20}, {"name": "a", "type": "debug"}, {"name": "a", "preset": "pvp"},
                {"name": "a", "border_radius": {"minecraft:overworld": -1}},
                {"name": "a", "border_radius": {"overworld": 5}}, {"name": "a", "pregen": "yes"}, []):  # fmt: skip
        with pytest.raises(worlds.WorldError):
            worlds.check_entry(bad)


def test_properties_for_writes_seed_and_type_only_for_a_new_world() -> None:
    e = worlds.check_entry({"name": "a", "seed": "-42", "type": "flat_void", "preset": "creative_build"})
    new = worlds.properties_for(e, create=True)
    assert (
        new["level-seed"] == "-42" and new["level-type"] == "minecraft:flat" and new["gamemode"] == "creative"
    )
    again = worlds.properties_for(e, create=False)
    assert "level-seed" not in again and "level-type" not in again and again["force-gamemode"] == "true"
    assert worlds.properties_for(worlds.check_entry({"name": "a"}), create=False) == {}  # preset null: as is


def test_set_properties_changes_only_its_keys(tmp_path: Path) -> None:
    props = tmp_path / "server.properties"
    props.write_text(
        "#Minecraft server properties\nmotd=Hi\\: there\nlevel-seed=1\ndifficulty=easy\nrcon.port=25575\n"
    )
    gen = worlds.TYPES["flat_classic"]["generator-settings"]
    changed = worlds.set_properties(
        props, {"level-seed": "", "difficulty": "hard", "generator-settings": gen}
    )
    assert sorted(changed) == ["difficulty", "generator-settings", "level-seed"]
    text = props.read_text()
    assert text.startswith("#Minecraft server properties\nmotd=Hi\\: there\n") and "rcon.port=25575" in text
    import mc_rcon

    read = mc_rcon.read_properties(props)
    assert (
        read["difficulty"] == "hard" and read["level-seed"] == "" and json.loads(read["generator-settings"])
    )
    assert worlds.set_properties(props, {"difficulty": "hard"}) == []  # unchanged: the file isn't rewritten


# --- the archive list (restic tags), retention -----------------------------------------------------------------


def test_archives_group_by_world_newest_first_and_skip_deleted_and_plain_snapshots() -> None:
    raw = [
        snap("main", "aaaaaaaa", "2026-11-01T10:00:00Z"),
        snap("main", "bbbbbbbb", "2026-11-03T10:00:00Z", dv=4672),
        snap("creative", "cccccccc", "2026-11-02T10:00:00Z", dv=None),
        snap("gone", "dddddddd", "2026-11-02T11:00:00Z"),
        {"id": "e" * 64, "short_id": "eeeeeeee", "time": "2026-11-04T10:00:00Z", "tags": ["auto"]},
        {"id": "f" * 64, "time": "2026-11-04T11:00:00Z", "tags": ["world:main"]},  # no world-archive tag
    ]
    out = worlds.archives_from(raw, deleted=["gone"])
    assert [a["name"] for a in out] == ["main", "creative"]
    main = out[0]
    assert main["short_id"] == "bbbbbbbb" and main["data_version"] == 4672 and main["size"] == 2 * 10**9
    assert main["snapshots"] == ["bbbbbbbb" * 8, "aaaaaaaa" * 8] and out[1]["data_version"] is None


def test_backup_forget_selects_only_auto_snapshots() -> None:
    """Archives (tag world-archive) and pre-restore snapshots are outside the retention policy."""
    assert backup.FORGET_AUTO == ["forget", "--host", "mc-server", "--tag", "auto"]
    src = (REPO / "scripts/backup.py").read_text()
    assert src.count('"forget"') == 1 and "restic(*FORGET_AUTO, *retention(" in src


def test_run_backup_adds_the_archive_tags(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backup, "world_dir", lambda: str(tmp_path))
    monkeypatch.setattr(backup, "restic", lambda *a: calls.append(a))
    monkeypatch.setattr(backup.mc_rcon, "connect", lambda: (_ for _ in ()).throw(OSError("down")))
    backup.run_backup(tag="world-archive", forget=False, extra_tags=["world:main", "dv:4671"])
    (cmd,) = calls
    assert cmd[cmd.index("--tag") + 1] == "world-archive" and "world:main" in cmd and "dv:4671" in cmd
    assert "forget" not in cmd


class FakeOps(worlds.Ops):
    """restic and systemd faked for run_panel; the apply unit runs worlds.apply() in this process."""

    def __init__(self, box: Box) -> None:
        super().__init__()
        self.box = box
        self.forgotten: list[str] = []

    def running(self) -> bool:
        return self.box.running

    def systemctl(self, *args: str) -> None:
        self.box.events.append(" ".join(args))
        verb, unit = args
        if unit == worlds.APPLY_UNIT:
            if self.box.fail_apply:
                raise subprocess.CalledProcessError(1, "systemctl")
            worlds.apply(self.box.srv)
        elif unit == worlds.UNIT:
            self.box.running = verb == "start"
            if self.box.running and not (self.box.srv / "world" / "level.dat").exists():
                level_dat(self.box.srv / "world", self.box.dv)  # the server generates the new world

    def archive(self, name: str, dv: int | None) -> str:
        self.box.events.append(f"archive {name} dv={dv} running={self.box.running}")
        if self.box.fail_archive:
            raise worlds.WorldError("restic failed")
        return "9" * 64

    def fetch(self, snapshot_id: str) -> Path:
        self.box.events.append(f"fetch {snapshot_id[:8]}")
        level_dat(worlds.STAGING / "world", self.box.archive_dv)
        (worlds.STAGING / "world" / "playerdata").mkdir()
        (worlds.STAGING / "world_nether").mkdir()
        return worlds.STAGING

    def forget(self, ids: list[str]) -> None:
        self.forgotten += ids

    def backup_now(self) -> None:
        self.box.events.append("backup-now")

    def list_snapshots(self) -> None:
        self.box.events.append("list")


class Box:
    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, *, running: bool = True) -> None:
        self.srv = tmp / "server"
        self.dv, self.archive_dv = 4671, 4671
        self.running = running
        self.fail_apply = self.fail_archive = False
        self.events: list[str] = []
        level_dat(self.srv / "world", self.dv)
        (self.srv / "world" / "playerdata").mkdir()
        (self.srv / "world_nether").mkdir()
        (self.srv / "server.properties").write_text("level-name=world\ndifficulty=easy\nmotd=x\n")
        state = tmp / "state"
        state.mkdir()
        for name, path in (("RESTORE_LOCK_FILE", "restore.lock"), ("ADMIN_STOPPED_FILE", "admin-stopped"),
                           ("MAINTENANCE_FILE", "maintenance.json")):  # fmt: skip
            monkeypatch.setattr(notify, name, state / path)
        monkeypatch.setattr(notify, "maintenance", lambda m, r: self.events.append(f"maintenance {m}"))
        monkeypatch.setattr(worlds, "WORLDS_FILE", tmp / "worlds.json")
        monkeypatch.setattr(worlds, "LIBRARY_FILE", tmp / "worlds-library.json")
        monkeypatch.setattr(worlds, "STAGING_ROOT", tmp / "mc-restore")
        monkeypatch.setattr(worlds, "STAGING", tmp / "mc-restore" / "world-staging")
        monkeypatch.setattr(worlds, "MANIFEST", tmp / "mc-restore" / "world-manifest.json")
        monkeypatch.setattr(worlds, "server_running", lambda: self.running)
        monkeypatch.setattr(worlds, "wait_rcon", lambda *a, **k: True)
        monkeypatch.setattr(worlds, "live_seed", lambda: "123")
        monkeypatch.setattr(worlds.mc_rcon, "worldborder", lambda d, r: self.events.append(f"border {d} {r}"))
        self.ops = FakeOps(self)
        self.state = state

    def archives(self, *snaps: dict[str, Any], active: str = "main") -> None:
        worlds.save_state({"active": active, "since": "", "data_version": self.dv,
                           "archives": worlds.archives_from(list(snaps)), "deleted": []})  # fmt: skip


def req(op: str, name: str, **entry: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"op": op, "name": name, "id": "c1", "login": "admin@example.com"}
    if op != "delete":
        out["entry"] = worlds.check_entry({"name": name, **entry})
    return out


def test_create_archives_first_then_swaps_and_generates(tmp_path: Path, monkeypatch, capsys) -> None:
    box = Box(tmp_path, monkeypatch)
    box.archives()
    border = {"minecraft:overworld": 1000, "minecraft:the_nether": None, "minecraft:the_end": None}
    line = worlds.run_panel(req("create", "creative", seed="123", type="flat_void", preset="creative_build",
                                border_radius=border), box.ops, box.srv)  # fmt: skip
    assert line.startswith("WORLD creative active")
    assert box.events == [
        "maintenance 180", "stop minecraft.service", "archive main dv=4671 running=False",
        "start mc-world-apply.service", "start minecraft.service", "border minecraft:overworld 1000", "backup-now",
        "maintenance 0", "list",
    ]  # fmt: skip
    props = (box.srv / "server.properties").read_text()
    assert "level-seed=123" in props and "level-type=minecraft:flat" in props and "gamemode=creative" in props
    assert "motd=x" in props  # the rest stays
    assert not (box.srv / "world" / "playerdata").exists()  # a new world: its own (empty) player data
    assert not (box.srv / "world_nether").exists()
    st = worlds.load_state()
    assert st["active"] == "creative" and st["since"]
    assert not notify.admin_stopped() and not worlds.MANIFEST.exists()
    out = capsys.readouterr().out
    assert [ln.split(":")[0] for ln in out.splitlines() if ln.startswith("STEP")] == [
        f"STEP {i}/7" for i in range(1, 8)
    ]
    assert f"ARCHIVED main {'9' * 64}" in out


def test_switch_fetches_the_archive_and_keeps_the_rest_of_the_server_dir(tmp_path, monkeypatch) -> None:
    box = Box(tmp_path, monkeypatch)
    (box.srv / "whitelist.json").write_text("[]")
    box.archives(snap("creative", "cccccccc", "2026-11-02T10:00:00Z"))
    worlds.run_panel(req("switch", "creative", preset="peaceful"), box.ops, box.srv)
    assert "fetch cccccccc" in box.events
    assert box.events.index("archive main dv=4671 running=False") < box.events.index("fetch cccccccc")
    assert (box.srv / "world" / "playerdata").is_dir() and (
        box.srv / "world_nether"
    ).is_dir()  # from the archive
    assert (box.srv / "whitelist.json").exists()  # shared by every world
    props = (box.srv / "server.properties").read_text()
    assert (
        "difficulty=peaceful" in props and "level-seed" not in props
    )  # an existing world keeps its own seed
    assert worlds.load_state()["active"] == "creative"


def test_switch_refuses_a_newer_world_before_anything_changes(tmp_path, monkeypatch) -> None:
    box = Box(tmp_path, monkeypatch)
    box.archives(snap("future", "ffffffff", "2026-11-02T10:00:00Z", dv=9999))
    with pytest.raises(worlds.WorldError, match="newer Minecraft"):
        worlds.run_panel(req("switch", "future"), box.ops, box.srv)
    assert box.events == [] and box.running


def test_switch_refuses_a_newer_level_dat_found_after_the_fetch(tmp_path, monkeypatch, capsys) -> None:
    box = Box(tmp_path, monkeypatch)
    box.archives(snap("creative", "cccccccc", "2026-11-02T10:00:00Z", dv=None))  # no dv tag (imported)
    box.archive_dv = 9999
    with pytest.raises(SystemExit):
        worlds.run_panel(req("switch", "creative"), box.ops, box.srv)
    assert "start mc-world-apply.service" not in box.events and box.events[-3] == "start minecraft.service"
    assert (box.srv / "world" / "playerdata").is_dir() and not notify.admin_stopped()
    assert "FAILED before anything changed" in capsys.readouterr().out
    assert worlds.load_state()["active"] == "main"


def test_an_older_world_is_allowed_with_a_warning(tmp_path, monkeypatch, capsys) -> None:
    box = Box(tmp_path, monkeypatch)
    box.archives(snap("old", "0ld00000", "2026-01-02T10:00:00Z", dv=4000))
    box.archive_dv = 4000
    worlds.run_panel(req("switch", "old"), box.ops, box.srv)
    assert "upgraded when it loads, one way" in capsys.readouterr().out
    assert worlds.version_warning(4000, 4671) and not worlds.version_warning(4671, 4671)


def test_a_failed_archive_leaves_the_world_and_starts_it_again(tmp_path, monkeypatch, capsys) -> None:
    box = Box(tmp_path, monkeypatch)
    box.archives()
    box.fail_archive = True
    with pytest.raises(SystemExit):
        worlds.run_panel(req("create", "new"), box.ops, box.srv)
    assert box.running and not notify.admin_stopped() and (box.srv / "world" / "playerdata").is_dir()
    assert "FAILED before anything changed (restic failed)" in capsys.readouterr().out


def test_a_failed_apply_keeps_the_server_stopped_and_names_the_archive(tmp_path, monkeypatch, capsys) -> None:
    box = Box(tmp_path, monkeypatch)
    box.archives()
    box.fail_apply = True
    with pytest.raises(SystemExit):
        worlds.run_panel(req("create", "new"), box.ops, box.srv)
    assert not box.running and notify.admin_stopped()
    assert "99999999" in notify.ADMIN_STOPPED_FILE.read_text()
    assert "FAILED while applying" in capsys.readouterr().out


def test_an_admin_stopped_server_stays_stopped(tmp_path, monkeypatch) -> None:
    box = Box(tmp_path, monkeypatch, running=False)
    notify.ADMIN_STOPPED_FILE.write_text('{"by": "panel"}')
    box.archives()
    line = worlds.run_panel(req("create", "new"), box.ops, box.srv)
    assert "server stopped" in line and "start minecraft.service" not in box.events
    assert notify.ADMIN_STOPPED_FILE.read_text() == '{"by": "panel"}'  # the admin's own marker is back


def test_request_rules(tmp_path, monkeypatch) -> None:
    box = Box(tmp_path, monkeypatch)
    box.archives(snap("creative", "cccccccc", "2026-11-02T10:00:00Z"))
    for r, why in ((req("create", "main"), "already the active"), (req("create", "creative"), "already exists"),
                   (req("switch", "nope"), "isn't an archived"), (req("delete", "main"), "active world")):  # fmt: skip
        with pytest.raises(worlds.WorldError, match=why):
            worlds.run_panel(r, box.ops, box.srv)
    assert box.events == []
    path = tmp_path / "request.json"
    path.write_text(json.dumps({"op": "create", "name": "x", "entry": {"name": "y"}, "ts": 1}))
    with pytest.raises(worlds.WorldError, match="another world"):
        worlds.read_request(path)
    path.write_text(json.dumps({"op": "rm", "name": "x"}))
    with pytest.raises(worlds.WorldError):
        worlds.read_request(path)
    import time

    path.write_text(json.dumps({"op": "delete", "name": "x", "ts": time.time() - 7200}))
    with pytest.raises(worlds.WorldError, match="stale"):
        worlds.read_request(path)


def test_delete_forgets_what_the_bucket_lock_allows_and_the_rest_later(tmp_path, monkeypatch) -> None:
    box = Box(tmp_path, monkeypatch)
    old = (NOW - timedelta(days=30)).isoformat()
    young = (NOW - timedelta(days=2)).isoformat()
    box.archives(snap("creative", "11111111", old), snap("creative", "22222222", young))
    monkeypatch.setattr(worlds, "older_than", lambda iso, days, now=None: iso == old)
    assert worlds.run_panel(req("delete", "creative"), box.ops, box.srv) == "WORLD creative deleted"
    assert box.ops.forgotten == ["11111111" * 8] and box.running
    st = worlds.load_state()
    assert st["archives"] == [] and st["deleted"][0]["snapshots"] == ["22222222" * 8]
    # a later backup forgets the rest once the lock allows
    monkeypatch.setattr(worlds, "older_than", lambda iso, days, now=None: True)
    assert worlds.forget_due(st, box.ops) == ["22222222" * 8] and st["deleted"] == []
    # and refresh_state never brings a deleted world back
    worlds.refresh_state([snap("creative", "22222222", young)], 4671)
    assert worlds.load_state()["archives"] == []


def test_forget_due_keeps_the_newest_archives_per_world(tmp_path, monkeypatch) -> None:
    box = Box(tmp_path, monkeypatch)
    times = [(NOW - timedelta(days=d)).isoformat() for d in (1, 20, 40, 60)]
    st = {
        "archives": worlds.archives_from([snap("w", f"{i}" * 8, t) for i, t in enumerate(times, 1)]),
        "deleted": [],
    }
    gone = worlds.forget_due(st, box.ops, now=NOW)
    assert gone == ["3" * 64, "4" * 64]  # beyond the newest two and past the lock window
    assert worlds.older_than(times[0], 8, NOW) is False and worlds.older_than("garbage", 8, NOW) is False


def test_apply_refuses_without_the_lock_a_stopped_server_or_a_world_manifest(tmp_path, monkeypatch) -> None:
    box = Box(tmp_path, monkeypatch, running=False)
    with pytest.raises(worlds.WorldError, match=r"restore\.lock"):
        worlds.apply(box.srv)
    with restore.restore_lock():
        with pytest.raises(worlds.WorldError, match="manifest"):
            worlds.apply(box.srv)
        worlds.write_manifest(
            {"name": "x", "level": "world", "create": True, "properties": {"motd": "pwned"}}
        )
        with pytest.raises(worlds.WorldError, match="world keys"):
            worlds.apply(box.srv)
        worlds.write_manifest({"name": "x", "level": "other", "create": True, "properties": {}})
        with pytest.raises(worlds.WorldError, match="level-name"):
            worlds.apply(box.srv)
        worlds.write_manifest({"name": "x", "level": "world", "create": False, "properties": {}})
        with pytest.raises(worlds.WorldError, match="nothing staged"):
            worlds.apply(box.srv)
        box.running = True
        with pytest.raises(worlds.WorldError, match="running"):
            worlds.apply(box.srv)
    assert (box.srv / "world" / "level.dat").exists()  # nothing was touched


def test_data_version_reads_level_dat(tmp_path: Path) -> None:
    level_dat(tmp_path / "w", 4671)
    assert worlds.data_version(tmp_path / "w") == 4671
    assert worlds.data_version(tmp_path / "missing") is None
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "level.dat").write_bytes(b"not gzip")
    assert worlds.data_version(tmp_path / "bad") is None


# --- restore.py and health.py ----------------------------------------------------------------------------------


def test_a_fresh_box_never_restores_another_worlds_archive(monkeypatch) -> None:
    snaps = [
        {"id": "a" * 64, "time": "2026-11-01T10:00:00Z", "tags": ["auto"]},
        snap("other", "bbbbbbbb", "2026-11-02T10:00:00Z"),
    ]
    monkeypatch.setattr(restore, "restic_json", lambda *a: snaps)
    assert restore.latest_snapshot()["id"] == "a" * 64
    assert restore.latest_snapshot("bbbbbbbb")["short_id"] == "bbbbbbbb"  # an explicit id may name one


def test_the_panel_restore_refuses_another_worlds_archive(tmp_path, monkeypatch) -> None:
    Box(tmp_path, monkeypatch).archives(active="main")
    with pytest.raises(SystemExit, match="switch worlds in the panel"):
        restore.check_not_other_world(snap("other", "bbbbbbbb", "2026-11-02T10:00:00Z"))
    restore.check_not_other_world(snap("main", "cccccccc", "2026-11-02T10:00:00Z"))  # its own: fine
    restore.check_not_other_world({"id": "d" * 64, "tags": ["auto"]})


def test_pregen_starts_afresh_after_a_switch() -> None:
    sent: list[str] = []
    replies = {"list": "There are 0 of a max of 20 players online:", "chunky progress": "",
               "chunky start": "Task started in world"}  # fmt: skip

    def cmd(line: str) -> str:
        sent.append(line)
        return replies.get(line, "")

    st = health.State(pregen_done=["world"], pregen_for="main@2026-11-01")
    st, what = health.pregen_step(st, ["world"], cmd, "creative@2026-11-05")
    assert sent[0] == "chunky cancel" and what == "started (world)" and st.pregen_for == "creative@2026-11-05"
    sent.clear()
    st, _ = health.pregen_step(health.State(pregen_done=["world"]), ["world"], cmd, None)
    assert sent == []  # before any switch nothing changes


# --- mc-agent's actions ------------------------------------------------------------------------------------------


@pytest.fixture
def agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    state = tmp_path / "worlds.json"
    library = tmp_path / "worlds-library.json"
    request = tmp_path / "world-request.json"
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp_path / "admin-stopped")
    monkeypatch.setattr(
        mc_panel, "unit_steps", lambda unit, since: ["STEP 7/7: verified: creative is running"]
    )

    def systemctl(*a: str, **k: object) -> None:
        calls.append(list(a))
        if a[-1] == worlds.PANEL_UNIT:  # what mc-world.service would do to the state
            r = json.loads(request.read_text())
            st = worlds.load_state(state)
            if r["op"] == "delete":
                st["archives"] = [x for x in st["archives"] if x["name"] != r["name"]]
            else:
                st["active"] = r["name"]
            worlds.save_state(st, state)

    monkeypatch.setattr(mc_agent, "systemctl", systemctl)
    p = mc_agent.Panel(
        jobs=mc_panel.Jobs(lambda j: None, rcon=lambda line: "There are 0 of a max of 20 players online:"),
        pending=mc_panel.Pending(tmp_path / "pending.json"),
        rejected=mc_panel.RejectedLogins(tmp_path / "rejected.json"),
        snapshots=tmp_path / "snapshots.json",
        worlds_state=state,
        worlds_library=library,
        world_request=request,
        clock=lambda: 5000.0,
    )
    worlds.save_state({"active": "main", "since": "", "data_version": 4671, "deleted": [{"name": "gone", "snapshots": []}],
                       "archives": worlds.archives_from([snap("creative", "cccccccc", "2026-11-02T10:00:00Z"),
                                                         snap("future", "ffffffff", "2026-11-02T10:00:00Z", dv=9999)])},
                      state)  # fmt: skip
    library.write_text(json.dumps({"active": "main", "library": [
        {"name": "main", "seed": "", "type": "default", "preset": "survival_smp"},
        {"name": "creative", "seed": "7", "type": "flat_classic", "preset": "creative_build"}]}))  # fmt: skip
    return mc_agent.Executor(lambda: {}, None, p), p, calls, request


def run(ex: mc_agent.Executor, action: str, args: dict[str, Any]) -> str:
    return ex.run({"id": "c1", "action": action, "args": args, "login": "admin@example.com"})


def wait_idle(p: mc_agent.Panel) -> mc_panel.Job:
    for _ in range(200):
        if not p.jobs.busy():
            break
        threading.Event().wait(0.02)
    assert p.jobs.current is not None
    return p.jobs.current


def test_agent_refuses_bad_world_requests(agent) -> None:
    ex, _, calls, _ = agent
    for action, args, why in (
        ("world_create", {"name": "Main"}, "world name"),
        ("world_create", {"name": "main"}, "already exists"),
        ("world_create", {"name": "creative"}, "already exists"),
        ("world_create", {"name": "gone"}, "deleted recently"),
        ("world_create", {"name": "x", "seed": "abc"}, "seed"),
        ("world_create", {"name": "x", "type": "debug"}, "type"),
        ("world_create", {"name": "x", "countdown": 7}, "countdown"),
        ("world_switch", {"name": "main"}, "already the active"),
        ("world_switch", {"name": "nope"}, "isn't an archived"),
        ("world_switch", {"name": "future"}, "newer Minecraft"),
        ("world_delete", {"name": "main"}, "active world"),
        ("world_delete", {"name": "nope"}, "isn't an archived"),
    ):
        with pytest.raises(mc_agent.Rejected, match=why):
            run(ex, action, args)
    assert calls == []


def test_agent_switch_writes_the_request_runs_the_unit_and_records_git(agent) -> None:
    ex, p, calls, request = agent
    out = run(ex, "world_switch", {"name": "creative", "countdown": 0})
    assert "world switch creative scheduled" in out
    job = wait_idle(p)
    assert job.state == "done" and job.step.startswith("STEP 7/7") and job.label == "World switch"
    assert calls == [["start", "mc-world.service"]]
    sent = json.loads(request.read_text())
    assert (
        sent["op"] == "switch"
        and sent["entry"]["preset"] == "creative_build"
        and sent["login"] == "admin@example.com"
    )
    pend = p.pending.load()["world"]
    assert pend["active"] == "creative" and pend["pr"] is None and pend["what"] == "switch to creative"
    assert [e["name"] for e in pend["library"]] == ["creative", "main"]


def test_agent_create_and_delete_update_the_pending_library(agent) -> None:
    ex, p, _, request = agent
    run(ex, "world_create", {"name": "farm", "seed": "-5", "type": "flat_void", "preset": "farm_test",
                             "pregen": False, "countdown": 0})  # fmt: skip
    wait_idle(p)
    assert json.loads(request.read_text())["entry"]["type"] == "flat_void"
    lib = {e["name"]: e for e in p.pending.load()["world"]["library"]}
    assert set(lib) == {"creative", "farm", "main"} and lib["farm"]["pregen"] is False
    run(ex, "world_delete", {"name": "creative"})
    wait_idle(p)
    w = p.pending.load()["world"]
    assert w["active"] == "farm" and {e["name"] for e in w["library"]} == {"farm", "main"}


def test_agent_records_nothing_when_the_switch_did_not_happen(agent, monkeypatch) -> None:
    ex, p, calls, _ = agent

    def failing(*a: str, **k: object) -> None:
        calls.append(list(a))
        raise subprocess.CalledProcessError(1, "systemctl")

    monkeypatch.setattr(mc_agent, "systemctl", failing)
    run(ex, "world_switch", {"name": "creative", "countdown": 0})
    job = wait_idle(p)
    assert job.state == "failed" and "world_switch failed" in job.step
    assert p.pending.load()["world"] is None


def test_agent_inventory_lists_the_worlds(agent) -> None:
    ex, p, _, _ = agent
    inv = mc_agent.worlds_inventory(p, ex)
    assert inv["active"] == "main" and [a["name"] for a in inv["archives"]] == ["creative", "future"]
    assert inv["archive_bytes"] == 4 * 10**9 and inv["storage_note"] is False and inv["deleted"] == ["gone"]
    assert inv["pending"] is None and {e["name"] for e in inv["library"]} == {"main", "creative"}


def test_agent_restore_refuses_another_worlds_archive(agent, tmp_path: Path) -> None:
    ex, _, _, _ = agent
    (tmp_path / "snapshots.json").write_text(json.dumps({"snapshots": [
        {"id": "c" * 64, "short_id": "cccccccc", "time": "2026-11-02T10:00:00Z", "tags": ["world-archive", "world:creative"]}]}))  # fmt: skip
    with pytest.raises(mc_agent.Rejected, match="Worlds tab"):
        run(ex, "restore", {"snapshot": "cccccccc"})


# --- pending -> pull request (mc_panel) ----------------------------------------------------------------------------


class FakeGH(mc_panel.GitHub):
    def __init__(self, server_yml: str) -> None:
        super().__init__(lambda: "t", "owner/config")
        self.files = {"server.yml": server_yml}
        self.calls: list[tuple[str, str]] = []
        self.state = "open"

    def call(self, method: str, path: str, body: object = None) -> Any:
        import base64

        self.calls.append((method, path.split("?")[0]))
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": "h" * 40}}
        if method == "GET" and "/contents/server.yml" in path:
            return {"content": base64.b64encode(self.files["server.yml"].encode()).decode(), "sha": "blob"}
        if method == "PUT":
            assert isinstance(body, dict)
            self.files["pushed"] = base64.b64decode(body["content"]).decode()
            return {}
        if path.endswith("/pulls"):
            return {"number": 7, "html_url": "https://github.com/owner/config/pull/7"}
        if "/pulls/" in path:
            return {"state": self.state, "merged": self.state == "merged"}
        return {}


def test_world_changes_become_a_server_yml_pull_request(tmp_path: Path) -> None:
    pend = mc_panel.Pending(tmp_path / "pending.json")
    gh = FakeGH(FULL.read_text())
    lib = [worlds.check_entry({"name": "main"}), worlds.check_entry({"name": "creative", "seed": "7"})]
    pend.record_world("creative", lib, "switch to creative", "admin@example.com", 1000.0)
    alerts: list[str] = []
    assert mc_panel.sync_pending(pend, gh, 1010.0, alert=alerts.append, batch_s=60) is False  # batching
    assert mc_panel.sync_pending(pend, gh, 1100.0, alert=alerts.append, batch_s=60) is True
    w = pend.load()["world"]
    assert w["pr"] == 7 and w["open_pr"]["url"].endswith("/pull/7")
    pushed = yaml.safe_load(gh.files["pushed"])
    assert pushed["world"]["active"] == "creative" and [e["name"] for e in pushed["world"]["library"]] == [
        "creative",
        "main",
    ]
    schema.load_server(pushed)
    gh.state = "merged"
    mc_panel.sync_pending(pend, gh, 1300.0, alert=alerts.append, pr_check_s=1)
    assert pend.load()["world"] is None and alerts == []


def test_a_closed_world_pr_keeps_the_live_world_and_tells_the_admin(tmp_path: Path) -> None:
    pend = mc_panel.Pending(tmp_path / "pending.json")
    gh = FakeGH(FULL.read_text())
    pend.record_world(
        "creative", [worlds.check_entry({"name": "creative"})], "create creative", "a@b", 1000.0
    )
    mc_panel.sync_pending(pend, gh, 1100.0, alert=lambda m: None, batch_s=60)
    gh.state = "closed"
    alerts: list[str] = []
    mc_panel.sync_pending(pend, gh, 1300.0, alert=alerts.append, pr_check_s=1)
    w = pend.load()["world"]
    assert w is not None and w["pr"] == -7 and "closed without merging" in w["error"] and len(alerts) == 1
    n = len(gh.calls)
    mc_panel.sync_pending(pend, gh, 1500.0, alert=alerts.append, pr_check_s=1, batch_s=60)
    assert len(gh.calls) == n  # no new PR until the next world change


# --- server.yml's world section (canonical, comment-preserving) ---------------------------------------------------


LIB = [
    worlds.check_entry({"name": "main", "preset": "survival_smp"}),
    worlds.check_entry(
        {
            "name": "creative",
            "seed": "-7",
            "type": "flat_classic",
            "preset": "creative_build",
            "pregen": False,
            "border_radius": {"minecraft:overworld": 1000},
        }
    ),
]


def without_world_library(data: dict) -> dict:
    data = copy.deepcopy(data)
    for k in ("active", "library"):
        data.get("world", {}).pop(k, None)
    if data.get("world") == {}:
        data.pop("world")
    return data


@pytest.mark.parametrize(
    "text",
    [
        FULL.read_text(),
        (REPO / "template/server.yml").read_text(),
        "name: x\nworld:\n  # borders\n  border_radius:\n    overworld: 100\n  active: old\n  library:\n  - name: old\n"
        "    seed: '1'\n\n  expected: true\nplugins: []\n",
    ],
)
def test_set_world_section_changes_only_active_and_library(text: str) -> None:
    out = worlds.set_world_section(text, "creative", LIB)
    old, new = yaml.safe_load(text), yaml.safe_load(out)
    assert without_world_library(old) == without_world_library(new)
    assert new["world"]["active"] == "creative"
    assert [e["name"] for e in new["world"]["library"]] == ["creative", "main"]
    assert new["world"]["library"][0]["border_radius"] == {"overworld": 1000, "nether": None, "end": None}
    assert worlds.set_world_section(out, "creative", LIB) == out  # canonical: idempotent
    kept = [ln for ln in text.splitlines() if ln.lstrip().startswith("#")]
    assert all(ln in out.splitlines() for ln in kept)  # every comment stays


def test_set_world_section_output_validates_and_round_trips_through_render() -> None:
    data = yaml.safe_load(worlds.set_world_section(FULL.read_text(), "creative", LIB))
    cfg = schema.load_server(data)
    lib = render.group_vars(cfg)["mc_world_library"]
    assert render.group_vars(cfg)["mc_world_active"] == "creative"
    assert [worlds.check_entry(e) for e in lib] == sorted(
        LIB, key=lambda e: e["name"]
    )  # same shape as the agent's


def test_set_world_section_refuses_what_it_cannot_edit() -> None:
    for text in ("world: {active: x}\n", "world:\n    active: x\n", "world:\n  active:\n    - x\n"):
        with pytest.raises(ValueError):
            worlds.set_world_section(text, "y", [])


# --- schema, render, Ansible ------------------------------------------------------------------------------------------


def test_schema_world_library_rules() -> None:
    base = yaml.safe_load(FULL.read_text())

    def errors(world: dict, overrides: dict | None = None) -> list[str]:
        data = copy.deepcopy(base)
        data.setdefault("world", {}).update(world)
        if overrides is not None:
            data["minecraft"]["overrides"] = overrides
        try:
            schema.load_server(data)
        except schema.SchemaError as e:
            return e.errors
        return []

    assert errors({"active": "a", "library": [{"name": "a"}, {"name": "b", "seed": "5"}]}) == []
    assert any("duplicate" in e for e in errors({"active": "a", "library": [{"name": "a"}, {"name": "a"}]}))
    assert any("isn't in world.library" in e for e in errors({"active": "c", "library": [{"name": "a"}]}))
    assert errors({"active": "c"}) == []  # no library: any name (a single world)
    assert any("seed" in e for e in errors({"library": [{"name": "world", "seed": 5}]}))
    assert any("difficulty" in e for e in errors({"active": "a", "library": [{"name": "a"}]},
                                                 {"server.properties": {"difficulty": "hard"}}))  # fmt: skip


def test_units_polkit_and_ansible_wiring() -> None:
    rule = (REPO / "ansible/roles/base/templates/50-mc-ops.rules.j2").read_text()
    assert '"mc-world.service": ["start"]' in rule and '"mc-world-apply.service": ["start"]' in rule
    unit = (REPO / "ansible/roles/backups/templates/mc-world.service.j2").read_text()
    assert "User={{ mc_ops_user }}" in unit and "ExecStart={{ mc_bin_dir }}/worlds.py --panel" in unit
    assert (
        "StateDirectory=mc-snapshots" in unit
        and "ReadWritePaths={{ mc_state_dir }} {{ mc_restore_dir }}" in unit
    )
    apply = (REPO / "ansible/roles/backups/templates/mc-world-apply.service.j2").read_text()
    assert "User=minecraft" in apply and "EnvironmentFile" not in apply and "--apply" in apply
    tasks = (REPO / "ansible/roles/backups/tasks/main.yml").read_text()
    assert "- mc-world.service" in tasks and "- mc-world-apply.service" in tasks
    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    assert gv["mc_worlds_state_file"] == str(worlds.WORLDS_FILE)
    assert gv["mc_worlds_library_file"].replace("{{ mc_root }}", gv["mc_root"]) == str(worlds.LIBRARY_FILE)
    assert gv["mc_restore_dir"] == str(worlds.STAGING_ROOT)
    assert str(worlds.WORLDS_FILE.parent) == str(notify.SNAPSHOTS_FILE.parent)
    main = (REPO / "ansible/roles/minecraft/tasks/main.yml").read_text()
    assert main.index("tasks/worlds.yml") < main.index("Set world borders")


# --- mcctl panel-pr-check / pr-gate ------------------------------------------------------------------------------------


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def config(tmp_path: Path) -> tuple[Path, str]:
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    git(tmp_path, "config", "user.name", "t")
    text = FULL.read_text().replace("environments: true}", "environments: true, panel_app: x-panel}", 1)
    data = yaml.safe_load(text)
    (tmp_path / "server.yml").write_text(text)
    (tmp_path / "players.yml").write_text("whitelist: []\nops: []\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "base")
    assert data["github"].get("panel_app")
    return tmp_path, git(tmp_path, "rev-parse", "HEAD")


def head_with(root: Path, text: str) -> str:
    (root / "server.yml").write_text(text)
    git(root, "commit", "-qam", "change")
    return git(root, "rev-parse", "HEAD")


def test_a_world_only_pr_may_merge_and_anything_more_needs_the_admin(config) -> None:
    root, base = config
    text = (root / "server.yml").read_text()
    bot = yaml.safe_load(text)["github"]["panel_app"] + "[bot]"
    head = head_with(root, worlds.set_world_section(text, "creative", LIB))
    assert panelpr.check(root, base, head) == []
    v = gate.evaluate(root, base, head, head_ref="panel/world-1", author=bot, sender=bot)
    assert v.policy == "worlds" and v.ok
    assert not gate.evaluate(root, base, head, head_ref="feature/x", author=bot, sender=bot).ok
    assert not gate.evaluate(
        root, base, head, head_ref="panel/world-1", author="someone", sender="someone"
    ).ok
    sneaky = head_with(
        root, worlds.set_world_section(text, "creative", LIB).replace("hostname: mc}", "hostname: mc2}")
    )
    assert any("more than world.active" in r for r in panelpr.check(root, base, sneaky))
    bad = head_with(root, worlds.set_world_section(text, "nope", LIB))
    assert any("isn't in world.library" in r for r in panelpr.check(root, base, bad))


def test_the_worker_and_panel_keep_world_actions_on_the_confirmation_host() -> None:
    m = re.search(r"const DESTRUCTIVE = new Set\(\[(.*?)\]\);", WORKER)
    assert m and {"world_create", "world_switch", "world_delete"} <= set(
        re.findall(r'"([a-z_]+)"', m.group(1))
    )
    assert '"world_create", "world_switch", "world_delete"' in PANEL_JS.split("const DESTRUCTIVE", 1)[1][:200]
    assert '"world_create", "world_switch", "world_delete"' in CONFIRM_JS.split("const ACTIONS", 1)[1][:200]
    discord = re.search(r"^const DISCORD_ACTIONS = \{(.*?)\};", WORKER, re.M | re.S)
    assert discord and "world" not in discord.group(1)  # never from Discord
