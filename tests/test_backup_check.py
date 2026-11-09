"""scripts/backup_check.py: the weekly backup test on the server (mc-backup-check.timer, v1.2), which replaced the
config repo's Monday Actions job (still there for a manual dispatch)."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Sequence
from pathlib import Path

import backup_check as bc
import pytest
import yaml
from conftest import REPO

GiB = 1 << 30


class FakeRestic:
    def __init__(self, fail: str = "", stats: str | None = None) -> None:
        self.calls: list[list[str]] = []
        self.fail, self.stats = fail, stats

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        assert argv[:3] == ["restic", "--retry-lock", "30m"]
        cmd = argv[3]
        out = ""
        if cmd == "stats":
            out = (
                self.stats
                if self.stats is not None
                else json.dumps({"total_size": GiB, "total_file_count": 9})
            )
        elif cmd == "snapshots":
            out = "ID  Time  Host\n"
        return subprocess.CompletedProcess(argv, 1 if cmd == self.fail else 0, out, "")


def run_check(tmp_path: Path, restic: FakeRestic, *, free: int = 10 * GiB, verify=None) -> list[tuple]:
    verified: list[tuple] = []

    def default_verify(target: Path, max_age: float) -> None:
        target.mkdir(parents=True)
        (target / "level.dat").write_text("x")
        verified.append((target, max_age))

    bc.check(restic, verify or default_verify, lambda p: free, tmp_path / "scratch", max_age_hours=48)
    return verified


def test_the_whole_check_in_order_and_the_scratch_copy_is_removed(tmp_path: Path) -> None:
    restic = FakeRestic()
    verified = run_check(tmp_path, restic)
    assert [c[3] for c in restic.calls] == ["snapshots", "check", "stats"]
    assert "--read-data-subset=5%" in restic.calls[1]
    assert restic.calls[2][4:] == ["latest", "--mode", "restore-size", "--json"]
    assert verified == [(tmp_path / "scratch", 48)]
    assert not (tmp_path / "scratch").exists()


@pytest.mark.parametrize(("fail", "step"), [("snapshots", "list"), ("check", "check"), ("stats", "space")])
def test_a_failing_restic_step_names_the_step(tmp_path: Path, fail: str, step: str) -> None:
    with pytest.raises(bc.Failed) as e:
        run_check(tmp_path, FakeRestic(fail=fail))
    assert e.value.step == step and e.value.detail == f"`restic {fail}` exited 1"


@pytest.mark.parametrize("stats", ["", "[]", '{"total_files": 3}', '{"total_size": "x"}'])
def test_unexpected_stats_output(tmp_path: Path, stats: str) -> None:
    with pytest.raises(bc.Failed, match="unexpected `restic stats` output"):
        run_check(tmp_path, FakeRestic(stats=stats))


def test_no_test_restore_without_room_for_it(tmp_path: Path) -> None:
    with pytest.raises(bc.Failed) as e:
        run_check(tmp_path, FakeRestic(), free=GiB)  # needs 1 GiB * 1.1 + 256 MiB
    assert e.value.step == "space" and "1382 MiB, 1024 MiB free" in e.value.detail


def test_restore_failures_are_reported_and_cleaned_up(tmp_path: Path) -> None:
    def stale(target: Path, max_age: float) -> None:
        target.mkdir(parents=True)
        raise SystemExit("latest snapshot abc is 30.0 h old (> 2 h): backups have stopped")

    with pytest.raises(bc.Failed) as e:
        run_check(tmp_path, FakeRestic(), verify=stale)
    assert e.value.step == "restore" and "backups have stopped" in e.value.detail
    assert not (tmp_path / "scratch").exists()

    def broken(target: Path, max_age: float) -> None:
        raise subprocess.CalledProcessError(1, ["restic", "restore"])

    with pytest.raises(bc.Failed, match="CalledProcessError while restoring"):
        run_check(tmp_path, FakeRestic(), verify=broken)


def test_main_alerts_discord_and_fails_the_unit(tmp_path: Path, monkeypatch, capsys) -> None:
    sent: list[str] = []
    monkeypatch.setattr(bc.notify, "discord", sent.append)
    monkeypatch.setattr(bc.notify, "restore_running", lambda: False)

    def failing(**kw: object) -> None:
        raise bc.Failed("check", "`restic check` exited 1")

    monkeypatch.setattr(bc, "check", failing)
    assert bc.main([]) == 1
    (text,) = sent
    assert "The weekly backup test failed" in text and "checking the repository" in text
    assert "journalctl -u mc-backup-check" in text and "s3:" not in text
    monkeypatch.setattr(bc, "check", lambda **kw: None)
    sent.clear()
    assert bc.main(["--max-age-hours", "48"]) == 0 and not sent
    monkeypatch.setattr(bc.notify, "restore_running", lambda: True)
    assert bc.main([]) == 0 and "skipped" in capsys.readouterr().out


def test_the_timer_and_the_unit() -> None:
    roles = REPO / "ansible/roles/backups"
    defaults = yaml.safe_load((roles / "defaults/main.yml").read_text())
    assert defaults["backups_check_schedule"].startswith("Mon ") and defaults[
        "backups_check_schedule"
    ].endswith(" UTC")
    timer = (roles / "templates/mc-backup-check.timer.j2").read_text()
    assert "OnCalendar={{ backups_check_schedule }}" in timer and "Persistent=false" in timer
    unit = (roles / "templates/mc-backup-check.service.j2").read_text()
    assert (
        "ExecStart={{ mc_bin_dir }}/backup_check.py --max-age-hours {{ backups_check_max_age_hours }}" in unit
    )
    assert "EnvironmentFile={{ mc_etc_dir }}/restic.env" in unit and "CAP_" not in unit
    tasks = (roles / "tasks/main.yml").read_text()
    assert "- mc-backup-check.timer" in tasks and "mc-backup-check.timer]" in tasks
    env = (roles / "templates/restic.env.j2").read_text()
    assert "DISCORD_WEBHOOK_URL=" in env  # the alert's path


@pytest.mark.parametrize(
    ("schedule", "hours"), [("hourly", "2"), ("daily", "48"), ("weekly", "336"), ("*-*-* 00/6:00", "26")]
)
def test_max_age_is_two_backup_periods(schedule: str, hours: str) -> None:
    import jinja2

    expr = yaml.safe_load((REPO / "ansible/roles/backups/defaults/main.yml").read_text())
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701 - not HTML
    assert env.from_string(expr["backups_check_max_age_hours"]).render(mc_backup_schedule=schedule) == hours


def test_a_recent_skip_allows_an_older_snapshot(tmp_path: Path, monkeypatch) -> None:
    """v1.5: the timer skipped an unchanged world -> backups haven't stopped; a stale skip or none: the plain limit."""
    import json
    import time

    state = tmp_path / "backup-state.json"
    now = 1_800_000_000.0
    assert bc.allowed_age_hours(2.0, state, now) == 2.0  # no record
    state.write_text(
        json.dumps({"last_backup": now - 20 * 3600, "fingerprint": "f", "last_skip": now - 1800})
    )
    assert bc.allowed_age_hours(2.0, state, now) == 2.0 + bc.notify.BACKUP_SKIP_MAX_HOURS
    state.write_text(json.dumps({"last_backup": now - 30 * 3600, "last_skip": now - 3 * 3600}))
    assert bc.allowed_age_hours(2.0, state, now) == 2.0  # the timer stopped skipping (and backing up)
    state.write_text("{not json")
    assert bc.allowed_age_hours(2.0, state, now) == 2.0

    seen: list[float] = []
    monkeypatch.setattr(bc.notify, "restore_running", lambda: False)
    monkeypatch.setattr(bc.notify, "BACKUP_STATE_FILE", state)
    monkeypatch.setattr(bc, "check", lambda **kw: seen.append(kw["max_age_hours"]))
    state.write_text(json.dumps({"last_skip": time.time() - 60}))
    assert bc.main(["--max-age-hours", "2"]) == 0 and seen == [2.0 + bc.notify.BACKUP_SKIP_MAX_HOURS]
