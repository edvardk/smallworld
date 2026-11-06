import grp
import json
import os
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import backup
import health
import notify
import pytest
import restore


@pytest.fixture(autouse=True)
def markers(tmp_path: Path, monkeypatch) -> Path:
    """The /var/lib/minecraft markers and the /var/lib/mc-restore staging dir, in a temp dir."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(notify, "RESTORE_LOCK_FILE", state / "restore.lock")
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", state / "admin-stopped")
    monkeypatch.setattr(notify, "MAINTENANCE_FILE", state / "maintenance.json")
    staging_root = tmp_path / "mc-restore"
    staging_root.mkdir()
    monkeypatch.setattr(restore, "STAGING_ROOT", staging_root)
    monkeypatch.setattr(restore, "STAGING", staging_root / "staging")
    monkeypatch.setattr(restore, "MANIFEST", staging_root / "manifest.json")
    monkeypatch.setattr(restore, "SERVER_GROUP", grp.getgrgid(os.getgid()).gr_name)
    return state


def apply_in_process(monkeypatch) -> list[str]:
    """`systemctl start mc-restore-apply.service`, run in this process (the unit runs `--apply-staged`)."""
    calls: list[str] = []

    def start_apply() -> None:
        calls.append("apply")
        restore.apply_staged()

    monkeypatch.setattr(restore, "start_apply", start_apply)
    return calls


def test_restore_live_keeps_excluded_paths(tmp_path: Path, monkeypatch) -> None:
    srv = tmp_path / "server"
    (srv / "plugins/Foo").mkdir(parents=True)
    (srv / "plugins/Foo.jar").write_text("jar")
    (srv / "plugins/Foo/config.yml").write_text("old")
    (srv / "plugins/Stale").mkdir()
    (srv / "plugins/.paper-remapped").mkdir()
    (srv / "plugins/.paper-remapped/Foo.jar").write_text("remapped")
    (tmp_path / "purpur.jar").write_text("server")
    (srv / "server.jar").symlink_to(tmp_path / "purpur.jar")
    (srv / "world").mkdir()
    (srv / "world/old.dat").write_text("old")
    (srv / "world.aside").mkdir()

    def fake_restore_to(snap, target: Path, verify: bool = False) -> Path:
        (target / "world").mkdir(parents=True)
        (target / "world/new.dat").write_text("new")
        (target / "plugins/Foo").mkdir(parents=True)
        (target / "plugins/Foo/config.yml").write_text("restored")
        (target / "server.properties").write_text("level-name=world\n")
        return target

    monkeypatch.setattr(restore, "SERVER_DIR", srv)
    monkeypatch.setattr(restore, "server_running", lambda: False)
    monkeypatch.setattr(restore, "restore_to", fake_restore_to)
    calls = apply_in_process(monkeypatch)

    restore.restore_live({"id": "x", "time": "2026-10-31T10:00:00Z", "paths": ["/srv"]})
    assert calls == ["apply"]

    assert (srv / "plugins/Foo.jar").read_text() == "jar"
    assert (srv / "plugins/.paper-remapped/Foo.jar").exists()
    assert (srv / "plugins/Foo/config.yml").read_text() == "restored"
    assert not (srv / "plugins/Stale").exists()
    assert (srv / "server.jar").is_symlink()
    assert sorted(p.name for p in (srv / "world").iterdir()) == ["new.dat"]
    assert (srv / "world.aside").is_dir()
    assert (srv / "server.properties").exists()
    assert (srv / "logs").is_dir()
    assert not restore.STAGING.exists() and not restore.MANIFEST.exists()  # cleaned up


SNAP = {
    "id": "abcdef0123456789",
    "short_id": "abcdef01",
    "time": "2026-10-31T10:00:05.123456789+01:00",
    "paths": ["/opt/minecraft/server"],
}


def test_restore_live_writes_marker(tmp_path: Path, monkeypatch) -> None:
    srv = tmp_path / "server"

    def fake_restore_to(snap, target: Path, verify: bool = False) -> Path:
        (target / "world").mkdir(parents=True)
        return target

    monkeypatch.setattr(restore, "SERVER_DIR", srv)
    monkeypatch.setattr(restore, "server_running", lambda: False)
    monkeypatch.setattr(restore, "restore_to", fake_restore_to)
    apply_in_process(monkeypatch)
    restore.restore_live(SNAP)

    lines = (srv / ".restored-from").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["snapshot_id"] == SNAP["id"]
    assert rec["short_id"] == "abcdef01"
    assert rec["snapshot_time"] == SNAP["time"]
    assert rec["source_path"] == "/opt/minecraft/server"
    assert datetime.fromisoformat(rec["restored_at"]).tzinfo is not None


def test_marker_record_is_utc() -> None:
    now = datetime(2026, 10, 31, 12, 0, 0, tzinfo=timezone(timedelta(hours=1)))
    assert restore.marker_record(SNAP, now)["restored_at"] == "2026-10-31T11:00:00+00:00"


def test_snapshot_age_handles_restic_nanoseconds() -> None:
    now = datetime(2026, 10, 31, 11, 0, 5, tzinfo=UTC)  # 10:00:05+01:00 == 09:00:05Z
    assert round(restore.snapshot_age_hours(SNAP, now), 3) == 2.0


def test_check_world_needs_level_dat_and_region(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"level\.dat"):
        restore.check_world(tmp_path)
    (tmp_path / "world").mkdir()
    (tmp_path / "world/level.dat").write_bytes(b"x")
    with pytest.raises(SystemExit, match="region"):
        restore.check_world(tmp_path)
    (tmp_path / "world/region").mkdir()
    (tmp_path / "world/region/r.0.0.mca").write_bytes(b"x")
    assert "1 region file" in restore.check_world(tmp_path)


def test_check_world_follows_level_name_and_nested_layout(tmp_path: Path) -> None:
    (tmp_path / "server.properties").write_text("level-name=survival\n")
    nested = tmp_path / "survival/dimensions/minecraft/overworld/region"
    nested.mkdir(parents=True)
    (nested / "r.0.0.mca").write_bytes(b"x")
    (tmp_path / "survival/level.dat").write_bytes(b"x")
    assert restore.check_world(tmp_path).startswith("survival:")


def test_verify_refuses_stale_snapshot(tmp_path: Path, monkeypatch) -> None:
    old = SNAP | {"time": "2020-01-01T00:00:00Z"}
    monkeypatch.setattr(restore, "latest_snapshot", lambda snapshot_id=None: old)
    monkeypatch.setattr(restore, "restore_to", lambda *a, **k: pytest.fail("must not restore"))
    with pytest.raises(SystemExit, match="h old"):
        restore.verify_into(tmp_path / "r", max_age_hours=2)


def test_max_age_requires_verify_into() -> None:
    with pytest.raises(SystemExit):
        restore.main(["--if-missing", "--max-age-hours", "2"])


# --- P6 review: a restore can't race self-heal --------------------------------------------------------


def test_explicit_restore_needs_admin_stopped_or_maintenance(markers: Path, monkeypatch) -> None:
    monkeypatch.setattr(restore, "server_running", lambda: False)
    ran: list[str] = []
    monkeypatch.setattr(restore, "_restore_live", lambda snap: ran.append(snap["id"]))
    with pytest.raises(SystemExit, match="isn't admin-stopped and no maintenance window"):
        restore.restore_live(SNAP, explicit=True)
    restore.restore_live(SNAP)  # --if-missing (fresh box, nothing running yet) needs neither
    (markers / "admin-stopped").write_text("{}")
    restore.restore_live(SNAP, explicit=True)
    (markers / "admin-stopped").unlink()
    notify.maintenance(30, "restore", path=markers / "maintenance.json")
    restore.restore_live(SNAP, explicit=True)
    assert ran == [SNAP["id"]] * 3


def test_restore_refuses_while_the_server_runs(monkeypatch) -> None:
    monkeypatch.setattr(restore, "server_running", lambda: True)
    with pytest.raises(SystemExit, match="running"):
        restore.restore_live(SNAP)


def test_restore_lock_is_seen_by_health_and_backup_and_blocks_a_second_restore(
    markers: Path, monkeypatch
) -> None:
    monkeypatch.setattr(restore, "server_running", lambda: False)
    (markers / "admin-stopped").write_text("{}")
    seen: dict[str, object] = {}

    def during(snap) -> None:
        seen["health"] = notify.restore_running()
        seen["backup"] = backup.main([])
        with pytest.raises(SystemExit, match="another restore is running"):
            restore.restore_live(SNAP, explicit=True)
        svc = health.ServiceState(active_state="inactive", restore_running=notify.restore_running())
        seen["heal"] = health.decide_heal(health.State(status="down"), False, svc, 0.0).action

    monkeypatch.setattr(restore, "_restore_live", during)
    monkeypatch.setattr(backup, "heartbeat", lambda *a: pytest.fail("a skipped backup sends no heartbeat"))
    restore.restore_live(SNAP, explicit=True)
    assert seen == {"health": True, "backup": 0, "heal": "none"}
    assert notify.restore_running() is False  # released; the file staying behind means nothing
    assert (markers / "restore.lock").exists()


# --- P7 review: the ops user fetches, minecraft applies -------------------------------------------------


def test_fetch_shares_the_staged_tree_with_the_server_group(tmp_path: Path, monkeypatch) -> None:
    def fake_restore_to(snap, target: Path, verify: bool = False) -> Path:
        (target / "world").mkdir(parents=True)
        (target / "world/secret.dat").write_text("x")
        (target / "world/secret.dat").chmod(0o600)
        (target / "world").chmod(0o700)
        (target / "link").symlink_to("world/secret.dat")
        return target

    monkeypatch.setattr(restore, "restore_to", fake_restore_to)
    restore.fetch(SNAP)
    assert (restore.STAGING / "world/secret.dat").stat().st_mode & 0o777 == 0o640
    assert (restore.STAGING / "world").stat().st_mode & 0o777 == 0o750
    assert restore.read_manifest()["snapshot_id"] == SNAP["id"]
    assert restore.MANIFEST.stat().st_mode & 0o777 == 0o640


def test_apply_refuses_without_the_lock_a_stopped_server_or_a_manifest(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(restore, "SERVER_DIR", tmp_path / "server")
    monkeypatch.setattr(restore, "server_running", lambda: False)
    with pytest.raises(SystemExit, match=r"no restore holds restore\.lock"):
        restore.apply_staged()
    with restore.restore_lock():
        with pytest.raises(SystemExit, match="no usable restore manifest"):
            restore.apply_staged()
        restore.MANIFEST.write_text('{"snapshot_id": 1}')
        with pytest.raises(SystemExit, match="malformed"):
            restore.apply_staged()
        monkeypatch.setattr(restore, "server_running", lambda: True)
        with pytest.raises(SystemExit, match="running"):
            restore.apply_staged()
    assert not (tmp_path / "server").exists()


def test_copy_into_copies_symlinks_as_links_and_keeps_existing_paths(tmp_path: Path) -> None:
    src, dest = tmp_path / "src", tmp_path / "dest"
    (src / "a/b").mkdir(parents=True)
    (src / "a/b/f").write_text("new")
    (src / "keep").write_text("staged")
    (src / "ln").symlink_to("/etc/passwd")
    (dest).mkdir()
    (dest / "keep").write_text("excluded, stays")
    restore.copy_into(src, dest)
    assert (dest / "a/b/f").read_text() == "new"
    assert (dest / "keep").read_text() == "excluded, stays"
    assert (dest / "ln").is_symlink() and os.readlink(dest / "ln") == "/etc/passwd"
    assert (src / "a/b/f").exists()  # copied, not moved: the ops user cleans its staging dir up


def test_a_failed_apply_fails_the_restore_and_cleans_up(tmp_path: Path, monkeypatch) -> None:
    import subprocess

    monkeypatch.setattr(restore, "server_running", lambda: False)
    monkeypatch.setattr(restore, "restore_to", lambda snap, target, verify=False: target.mkdir(parents=True))
    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 1)

    monkeypatch.setattr(restore.subprocess, "run", run)
    with pytest.raises(SystemExit, match=r"mc-restore-apply\.service failed"):
        restore.restore_live(SNAP)
    assert calls == [["systemctl", "--no-ask-password", "start", "mc-restore-apply.service"]]
    assert not restore.STAGING.exists() and not restore.MANIFEST.exists()


def test_staging_dir_and_apply_unit_agree_with_ansible() -> None:
    import yaml
    from conftest import REPO

    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    assert f'STAGING_ROOT = Path("{gv["mc_restore_dir"]}")' in (REPO / "scripts/restore.py").read_text()
    unit = (REPO / "ansible/roles/backups/templates/mc-restore-apply.service.j2").read_text()
    assert "User=minecraft" in unit and "--apply-staged" in unit and "EnvironmentFile" not in unit
    rule = (REPO / "ansible/roles/base/templates/50-mc-ops.rules.j2").read_text()
    assert '"mc-restore-apply.service": ["start"]' in rule


# --- P10: the panel's restore (mc-restore@<id>.service: safety snapshot first) ------------------------------------


class PanelBox:
    """restic, the server unit and the apply step faked; records the order of what happened."""

    def __init__(self, tmp_path: Path, monkeypatch, *, running: bool = True, world: bool = True) -> None:
        self.srv = tmp_path / "server"
        (self.srv / "world").mkdir(parents=True) if world else self.srv.mkdir(parents=True)
        self.running = running
        self.events: list[str] = []
        self.snaps = [
            {"id": "aa" * 32, "short_id": "aaaaaaaa", "time": "2026-10-31T10:00:00Z", "paths": ["/srv"]}
        ]
        monkeypatch.setattr(restore, "SERVER_DIR", self.srv)
        monkeypatch.setattr(backup, "world_dir", lambda: str(self.srv / "world"))
        monkeypatch.setattr(restore, "server_running", lambda: self.running)
        monkeypatch.setattr(restore, "restic_json", lambda *a: list(self.snaps))
        monkeypatch.setattr(restore, "systemctl", self.systemctl)
        monkeypatch.setattr(backup, "run_backup", self.backup)
        monkeypatch.setattr(backup, "list_snapshots", lambda *a: self.events.append("list"))
        monkeypatch.setattr(restore, "fetch", lambda snap: self.events.append(f"fetch {snap['short_id']}"))
        monkeypatch.setattr(restore, "start_apply", lambda: self.events.append("apply"))
        monkeypatch.setattr(notify, "maintenance", lambda m, r: self.events.append(f"maintenance {m}"))
        self.ma = monkeypatch

    def systemctl(self, verb: str, unit: str) -> None:
        self.events.append(f"{verb} {unit}")
        self.running = verb == "start"

    def backup(self, tag: str = "auto", forget: bool = True) -> None:
        self.events.append(f"backup tag={tag} forget={forget} running={self.running}")
        self.snaps.append(
            {"id": "bb" * 32, "short_id": "bbbbbbbb", "time": "2026-10-31T11:00:00Z", "paths": ["/srv"]}
        )


def test_panel_restore_takes_a_safety_snapshot_before_anything_changes(
    tmp_path, monkeypatch, capsys, markers
) -> None:
    box = PanelBox(tmp_path, monkeypatch)
    # maintenance() is faked, so open the window the check expects by hand
    (markers / "maintenance.json").write_text(json.dumps({"until": 9e12, "reason": "test"}))
    assert restore.main(["--snapshot", "aaaaaaaa", "--panel"]) == 0
    assert box.events == [
        "maintenance 120",
        "backup tag=pre-restore forget=False running=True",  # while the server still runs (save-off/save-all)
        "stop minecraft.service",
        "fetch aaaaaaaa",
        "apply",
        "start minecraft.service",
        "maintenance 0",
        "list",
    ]
    out = capsys.readouterr().out
    assert [ln.split(":")[0] for ln in out.splitlines() if ln.startswith("STEP")] == [
        f"STEP {i}/6" for i in range(1, 7)
    ]
    assert out.strip().endswith("RESTORED aaaaaaaa (safety snapshot bbbbbbbb)")


def test_panel_restore_failures(tmp_path, monkeypatch, capsys, markers) -> None:
    (markers / "maintenance.json").write_text(json.dumps({"until": 9e12, "reason": "test"}))
    # a fetch failure: nothing changed, the server comes back
    box = PanelBox(tmp_path, monkeypatch)
    box.ma.setattr(restore, "fetch", lambda snap: (_ for _ in ()).throw(SystemExit("restic: network")))
    with pytest.raises(SystemExit):
        restore.main(["--snapshot", "aaaaaaaa", "--panel"])
    assert box.events[-3:] == ["start minecraft.service", "maintenance 0", "list"]
    assert "FAILED before anything changed" in capsys.readouterr().out and not notify.admin_stopped()
    # an apply failure: the world may be half-restored, so the server stays down (admin-stopped) with the safety id
    box2 = PanelBox(tmp_path / "2", monkeypatch)
    box2.ma.setattr(restore, "start_apply", lambda: (_ for _ in ()).throw(SystemExit("apply failed")))
    with pytest.raises(SystemExit):
        restore.main(["--snapshot", "aaaaaaaa", "--panel"])
    assert "start minecraft.service" not in box2.events
    assert notify.admin_stopped() and "restore bbbbbbbb" in notify.ADMIN_STOPPED_FILE.read_text()
    assert "FAILED while applying" in capsys.readouterr().out
    # unknown / malformed ids never reach restic's restore
    for bad in ("zz", "AAAAAAAA", "aaaa"):
        with pytest.raises(SystemExit):
            restore.restore_panel(bad)


def test_panel_restore_of_a_box_without_a_world_and_while_stopped(tmp_path, monkeypatch, markers) -> None:
    (markers / "admin-stopped").write_text("{}")
    box = PanelBox(tmp_path, monkeypatch, running=False, world=False)
    assert restore.restore_panel("aaaaaaaa").startswith("RESTORED aaaaaaaa (safety snapshot none")
    assert not any(e.startswith("backup") for e in box.events)
    assert "start minecraft.service" not in box.events  # it was admin-stopped before: stays stopped
    assert (markers / "admin-stopped").read_text() == "{}"  # the admin's own marker is put back unchanged


def test_panel_restore_marks_the_server_admin_stopped_while_it_runs(tmp_path, monkeypatch, markers) -> None:
    """P10 review: a deploy (or self-heal) during the restore must not start the server: the marker says
    by=restore from the stop until the end, and is gone again afterwards."""
    (markers / "maintenance.json").write_text(json.dumps({"until": 9e12, "reason": "test"}))
    box = PanelBox(tmp_path, monkeypatch)
    seen: list[dict] = []

    def record(snap: dict) -> None:
        seen.append(json.loads(notify.ADMIN_STOPPED_FILE.read_text()))
        box.events.append("fetch")

    box.ma.setattr(restore, "fetch", record)
    box.ma.setattr(
        restore, "start_apply", lambda: seen.append(json.loads(notify.ADMIN_STOPPED_FILE.read_text()))
    )
    real = box.systemctl

    def systemctl(verb: str, unit: str) -> None:
        if verb == "start":
            assert not notify.admin_stopped(), "the marker must be gone before the restore starts the server"
        real(verb, unit)

    box.ma.setattr(restore, "systemctl", systemctl)
    restore.restore_panel("aaaaaaaa")
    assert [m["by"] for m in seen] == ["restore", "restore"] and "in progress" in seen[0]["reason"]
    assert not notify.admin_stopped() and box.events[-3] == "start minecraft.service"
    # a failure before the apply also clears it (the world is untouched, the server comes back)
    box2 = PanelBox(tmp_path / "2", monkeypatch)
    box2.ma.setattr(restore, "fetch", lambda snap: (_ for _ in ()).throw(SystemExit("restic: network")))
    with pytest.raises(SystemExit):
        restore.restore_panel("aaaaaaaa")
    assert not notify.admin_stopped() and "start minecraft.service" in box2.events
