"""scripts/admin.py: the admin-stopped marker (design §5 "Stop")."""

import json
import subprocess
from pathlib import Path

import admin
import notify
import pytest


@pytest.fixture
def box(tmp_path: Path, monkeypatch) -> list[list[str]]:
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp_path / "admin-stopped")
    calls: list[list[str]] = []

    def run(argv, **kw):
        # the marker must already exist when the stop is issued (self-heal can't race the stop)
        calls.append([*argv, f"marker={notify.ADMIN_STOPPED_FILE.exists()}"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(admin.subprocess, "run", run)
    monkeypatch.setenv("SUDO_USER", "deploy")
    return calls


def test_stop_writes_the_marker_first_then_stops_and_reports(box, capsys) -> None:
    assert admin.main(["stop", "restore\nfrom backup"]) == 0
    assert box == [
        ["systemctl", "stop", "minecraft.service", "marker=True"],
        ["systemctl", "start", "--no-block", "mc-health.service", "marker=True"],
    ]
    rec = json.loads(notify.ADMIN_STOPPED_FILE.read_text())
    assert (
        rec["reason"] == "restore from backup" and rec["by"] == "deploy" and rec["since"].endswith("+00:00")
    )
    assert notify.admin_stopped()
    assert "ADMIN_STOPPED" in capsys.readouterr().out


def test_start_removes_the_marker_first(box) -> None:
    admin.main(["stop"])
    box.clear()
    assert admin.main(["start"]) == 0
    assert box[0] == ["systemctl", "start", "minecraft.service", "marker=False"]
    assert not notify.admin_stopped()


def test_status_and_usage(box, capsys) -> None:
    assert admin.main(["status"]) == 0
    assert "not admin-stopped" in capsys.readouterr().out
    assert admin.main(["reboot"]) == 2
    assert admin.main(["stop", "a", "b"]) == 2


def test_marker_path_is_what_health_reads() -> None:
    import health

    assert (
        health.ADMIN_STOPPED == Path("/var/lib/minecraft/admin-stopped") == notify.STATE_DIR / "admin-stopped"
    )


def test_start_refuses_on_a_fenced_box(box, tmp_path: Path, monkeypatch, capsys) -> None:
    """P12 review: a box `mcctl migrate` fenced never starts Minecraft (another box owns the world)."""
    monkeypatch.setattr(notify, "FENCED_FILE", tmp_path / "fenced")
    admin.main(["stop", "fenced"])
    (tmp_path / "fenced").write_text("{}")
    box.clear()
    assert admin.main(["start"]) == 3
    assert box == [] and notify.admin_stopped() and "FENCED" in capsys.readouterr().err
