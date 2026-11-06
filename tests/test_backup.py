import subprocess

import backup
import mc_rcon
import pytest


def test_save_on_even_if_restic_fails(fake_server, tmp_path, monkeypatch) -> None:
    srv = fake_server(lambda c: "")
    (tmp_path / "world").mkdir()
    monkeypatch.setattr(backup, "SERVER_DIR", str(tmp_path))

    def boom(*args: str) -> None:
        raise subprocess.CalledProcessError(1, ["restic", *args])

    monkeypatch.setattr(backup, "restic", boom)
    with pytest.raises(subprocess.CalledProcessError):
        backup.run_backup()
    assert srv.commands == ["save-off", "save-all flush", "save-on"]


def test_refuses_without_world(fake_server, tmp_path, monkeypatch) -> None:
    fake_server(lambda c: "")
    monkeypatch.setattr(backup, "SERVER_DIR", str(tmp_path))
    with pytest.raises(RuntimeError, match="no world"):
        backup.run_backup()


def test_backup_runs_without_rcon(tmp_path, monkeypatch) -> None:
    (tmp_path / "world").mkdir()
    (tmp_path / "server.properties").write_text("enable-rcon=true\nrcon.port=1\nrcon.password=x\n")
    monkeypatch.setattr(mc_rcon, "SERVER_DIR", tmp_path)
    monkeypatch.setattr(backup, "SERVER_DIR", str(tmp_path))
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backup, "restic", lambda *a: calls.append(a))
    backup.run_backup()
    assert calls[0][0] == "backup" and calls[1][0] == "forget"


def test_restic_waits_for_locks_and_keeps_lock_window(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(subprocess, "run", lambda cmd, check: calls.append(cmd))
    backup.restic("snapshots")
    assert calls == [["restic", "--retry-lock", "30m", "snapshots"]]
    i = backup.RETENTION.index("--keep-within")
    assert backup.RETENTION[i + 1] == f"{backup.KEEP_WITHIN_DAYS}d"
    assert ".restored-from" in backup.EXCLUDE_PATTERNS


def test_keep_within_comes_from_env(monkeypatch) -> None:
    # restic.env sets KEEP_WITHIN_DAYS = backups.lock_days + 1 (mc_backup_keep_within_days).
    monkeypatch.delenv("KEEP_WITHIN_DAYS", raising=False)
    assert backup.keep_within_days() == backup.KEEP_WITHIN_DAYS
    monkeypatch.setenv("KEEP_WITHIN_DAYS", "15")
    assert backup.keep_within_days() == 15
    assert backup.retention(15)[:2] == ["--keep-within", "15d"]
    monkeypatch.setenv("KEEP_WITHIN_DAYS", "1")
    with pytest.raises(ValueError, match=">= 2"):
        backup.keep_within_days()


def test_backup_and_prune_heartbeats(monkeypatch) -> None:
    beats: list[tuple[str, str]] = []
    monkeypatch.setattr(backup, "heartbeat", lambda check, status="", msg="": beats.append((check, status)))
    monkeypatch.setattr(backup, "run_backup", lambda: None)
    monkeypatch.setattr(backup, "restic", lambda *a: None)
    assert backup.main([]) == 0
    assert beats == [("backup", "start"), ("backup", "")]
    beats.clear()
    assert backup.main(["--prune"]) == 0
    assert beats == []  # a successful prune says nothing about the last backup's age

    def boom(*a: str) -> None:
        raise RuntimeError("restic exit 1")

    monkeypatch.setattr(backup, "restic", boom)
    with pytest.raises(RuntimeError):
        backup.main(["--prune"])
    assert beats == [("backup", "fail")]


def test_list_snapshots_for_the_panel(tmp_path, monkeypatch) -> None:
    import json
    import subprocess

    raw = [
        {"id": "a" * 64, "short_id": "aaaaaaaa", "time": "2026-10-30T10:00:00Z", "tags": ["auto"], "hostname": "mc",
         "paths": ["/opt/minecraft/server"], "summary": {"total_bytes_processed": 100, "data_added": 7}},
        {"id": "b" * 64, "time": "2026-10-31T10:00:00Z", "tags": None, "hostname": "mc"},
    ]  # fmt: skip
    seen: list[list[str]] = []

    def fake_run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, json.dumps(raw), "")

    monkeypatch.setattr(backup.subprocess, "run", fake_run)
    out = tmp_path / "snapshots.json"
    assert backup.list_snapshots(out) == 2
    assert seen == [["restic", "--retry-lock", "30m", "snapshots", "--json"]]
    snaps = json.loads(out.read_text())["snapshots"]
    assert [s["short_id"] for s in snaps] == ["bbbbbbbb", "aaaaaaaa"]  # newest first
    assert snaps[1] == {"id": "a" * 64, "short_id": "aaaaaaaa", "time": "2026-10-30T10:00:00Z", "tags": ["auto"],
                        "host": "mc", "size": 100, "added": 7}  # fmt: skip
    assert out.stat().st_mode & 0o777 == 0o640
    monkeypatch.setattr(backup, "SNAPSHOTS_FILE", out)
    monkeypatch.setattr(backup, "list_snapshots", lambda path=out: seen.append(["cli"]) or 0)
    assert backup.main(["--list-snapshots"]) == 0 and seen[-1] == ["cli"]


def test_the_safety_snapshot_has_its_own_tag_and_is_never_forgotten(monkeypatch, tmp_path) -> None:
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(backup, "restic", lambda *a: calls.append(a))
    monkeypatch.setattr(backup, "world_dir", lambda: str(tmp_path))
    monkeypatch.setattr(backup.mc_rcon, "connect", lambda: (_ for _ in ()).throw(OSError("down")))
    backup.run_backup(tag="pre-restore", forget=False)
    assert len(calls) == 1 and calls[0][:5] == ("backup", backup.SERVER_DIR, "--tag", "pre-restore", "--host")


def test_backup_reports_the_save_off_window(fake_server, tmp_path, monkeypatch, capsys) -> None:
    """P13 review M1: the crash-loss window (autosave off) is in the journal; forget runs after save-on."""
    srv = fake_server(lambda c: "")
    (tmp_path / "world").mkdir()
    monkeypatch.setattr(backup, "SERVER_DIR", str(tmp_path))
    order: list[str] = []
    monkeypatch.setattr(backup, "restic", lambda *a: order.append(f"{a[0]} after {srv.commands[-1]}"))
    backup.run_backup()
    assert order == ["backup after save-all flush", "forget after save-on"]
    assert "save-off window: " in capsys.readouterr().out
