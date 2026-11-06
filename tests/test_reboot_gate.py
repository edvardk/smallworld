"""P11: mc-reboot-gate (scripts/reboot_gate.py): reboot for pending updates inside the window when nobody is online,
never forced, Discord after 3 days; the unit, the polkit rule and the timer."""

from __future__ import annotations

import datetime
import json
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import reboot_gate as rg
from conftest import REPO

TZ = ZoneInfo("Europe/Dublin")
WINDOW = rg.parse_window("04:30-06:00")


def at(hh: int, mm: int, day: int = 3) -> datetime.datetime:
    return datetime.datetime(2026, 11, day, hh, mm, tzinfo=TZ)


def facts(**kw: object) -> rg.Facts:
    base = rg.Facts(now=at(5, 0), required=True, boot_id="boot-a", server_active=True, players=0,
                    backup_running=False, restore_running=False, admin_stopped=False, maintenance=None)  # fmt: skip
    return replace(base, **kw)  # type: ignore[arg-type]


def test_window_parsing_and_midnight_wrap() -> None:
    assert rg.in_window(at(4, 30), WINDOW) and rg.in_window(at(5, 59), WINDOW)
    assert not rg.in_window(at(6, 0), WINDOW) and not rg.in_window(at(4, 29), WINDOW)
    wrap = rg.parse_window("23:30-01:00")
    assert (
        rg.in_window(at(23, 45), wrap) and rg.in_window(at(0, 30), wrap) and not rg.in_window(at(1, 0), wrap)
    )
    for bad in ("4:30-06:00", "24:00-01:00", "04:30", "04:30-06:60"):
        with pytest.raises(ValueError):
            rg.parse_window(bad)


def test_reboots_in_the_window_when_nobody_is_online() -> None:
    d, st = rg.decide(facts(), {}, WINDOW)
    assert d.action == "reboot" and st["rebooting"]["boot_id"] == "boot-a" and "since" in st
    # the server down counts as nobody online
    assert rg.decide(facts(server_active=False, players=0), {}, WINDOW)[0].action == "reboot"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        ({"now": at(7, 0)}, "outside the reboot window"),
        ({"players": 2}, "2 player(s) online"),
        ({"players": None}, "can't tell who is online"),
        ({"admin_stopped": True}, "admin-stopped"),
        ({"maintenance": "deploy"}, "maintenance window is open (deploy)"),
        ({"backup_running": True}, "a backup is running"),
        ({"restore_running": True}, "a restore is running"),
    ],
)
def test_never_reboots_when_blocked(change: dict, why: str) -> None:
    d, st = rg.decide(facts(**change), {}, WINDOW)
    assert d.action == "wait" and why in d.reason and "rebooting" not in st


def test_nothing_pending_clears_the_record() -> None:
    d, st = rg.decide(facts(required=False), {"since": 1.0, "warned": 2.0}, WINDOW)
    assert d.action == "none" and st == {}


def test_never_forced_with_players_online_but_warns_after_three_days_once_a_day() -> None:
    state: dict = {}
    warns = []
    t = at(5, 0, day=1)
    for half_hours in range(0, 6 * 48):  # six days, every 30 minutes, someone always online
        now = t + datetime.timedelta(minutes=30 * half_hours)
        d, state = rg.decide(facts(now=now, players=1), state, WINDOW)
        assert d.action == "wait"
        if d.warn:
            warns.append(now)
    assert [w - t for w in warns] == [
        datetime.timedelta(days=3),
        datetime.timedelta(days=4),
        datetime.timedelta(days=5),
    ]
    assert (
        "never forced"
        in rg.decide(facts(now=t + datetime.timedelta(days=9), players=1), state, WINDOW)[0].warn
    )


def test_after_the_reboot_it_says_it_is_back_and_doesnt_reboot_twice() -> None:
    _, st = rg.decide(facts(), {}, WINDOW)
    # same boot, minutes later (the reboot hasn't happened yet): wait
    d, st2 = rg.decide(facts(now=at(5, 5)), st, WINDOW)
    assert d.action == "wait" and "a moment ago" in d.reason
    # a new boot id: back; the flag is gone, nothing pending any more
    d, st3 = rg.decide(facts(now=at(5, 10), boot_id="boot-b", required=False), st2, WINDOW)
    assert d.back and d.action == "none" and st3 == {}
    # a reboot that never happened (refused) is retried after the grace
    d, _ = rg.decide(facts(now=at(5, 20)), st, WINDOW)
    assert d.action == "reboot"


def test_main_reboots_through_the_target_with_announcements(tmp_path, monkeypatch) -> None:
    calls: list[list[str]] = []
    said: list[str] = []
    windows: list[tuple[int, str]] = []
    monkeypatch.setenv("REBOOT_WINDOW", "04:30-06:00")
    monkeypatch.setenv("REBOOT_TZ", "Europe/Dublin")
    monkeypatch.setenv("REBOOT_TARGET", "reboot.target")
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setattr(rg, "gather", lambda tz: facts())
    monkeypatch.setattr(rg.subprocess, "run", lambda argv, **kw: calls.append(argv))
    monkeypatch.setattr(rg.notify, "discord", said.append)
    monkeypatch.setattr(rg.notify, "maintenance", lambda m, r: windows.append((m, r)))
    assert rg.main([]) == 0
    assert calls == [["systemctl", "--no-ask-password", "start", "--no-block", "--job-mode=replace-irreversibly",
                      "reboot.target"]]  # fmt: skip
    assert windows == [(20, "reboot for security updates")] and "Rebooting the server host" in said[0]
    state = json.loads((tmp_path / "state.json").read_text())
    assert state["rebooting"]["boot_id"] == "boot-a"
    # after the boot: "back", the window closed (the server runs), no second reboot
    calls.clear()
    said.clear()
    monkeypatch.setattr(rg, "gather", lambda tz: facts(boot_id="boot-b", required=False))
    assert rg.main([]) == 0
    assert calls == [] and "is back after its reboot" in said[0] and windows[-1] == (0, "reboot")
    # --status changes nothing
    assert rg.main(["--status"]) == 0 and calls == []
    monkeypatch.setenv("REBOOT_TARGET", "../../evil")
    with pytest.raises(SystemExit):
        rg.main([])


def test_unit_timer_polkit_and_render_agree() -> None:
    unit = (REPO / "ansible/roles/autoreboot/templates/mc-reboot-gate.service.j2").read_text()
    assert "User={{ mc_ops_user }}" in unit and "REBOOT_TARGET={{ mc_reboot_target }}" in unit
    assert "REBOOT_WINDOW={{ mc_reboot_window }}" in unit and "REBOOT_TZ={{ timezone }}" in unit
    assert "ReadWritePaths={{ mc_state_dir }}" in unit and "MemoryDenyWriteExecute=true" in unit
    timer = (REPO / "ansible/roles/autoreboot/templates/mc-reboot-gate.timer.j2").read_text()
    assert "OnCalendar=*-*-* *:00,30:00" in timer and "OnBootSec=3min" in timer
    rule = (REPO / "ansible/roles/base/templates/50-mc-ops.rules.j2").read_text()
    assert '"{{ mc_reboot_target }}": ["start"]' in rule
    assert "Never reboot automatically" in (REPO / "ansible/roles/base/tasks/main.yml").read_text()
    assert Path(rg.__file__).name in (REPO / "ansible/roles/base/tasks/main.yml").read_text()


def test_gather_treats_a_fenced_box_as_admin_stopped(monkeypatch, tmp_path) -> None:
    """P12 review: the reboot gate stands down on a box `mcctl migrate` fenced."""
    import notify

    monkeypatch.setattr(rg, "unit_active", lambda unit: False)
    monkeypatch.setattr(rg, "players_online", lambda active: 0)
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp_path / "admin-stopped")
    monkeypatch.setattr(notify, "MAINTENANCE_FILE", tmp_path / "maintenance.json")
    monkeypatch.setattr(notify, "RESTORE_LOCK_FILE", tmp_path / "restore.lock")
    monkeypatch.setattr(notify, "FENCED_FILE", tmp_path / "fenced")
    assert not rg.gather(ZoneInfo("UTC")).admin_stopped
    (tmp_path / "fenced").write_text("{}")
    assert rg.gather(ZoneInfo("UTC")).admin_stopped
