"""scripts/notify.py: watchdog heartbeats and maintenance windows (never raise, never print secrets)."""

import json

import notify
import pytest

TOKEN = "hb-token-not-printed"


@pytest.fixture
def posts(monkeypatch):
    sent: list[tuple[str, bytes, str, str | None]] = []
    monkeypatch.setattr(
        notify, "_post", lambda url, body, ctype, token=None: sent.append((url, body, ctype, token))
    )
    monkeypatch.setenv("WATCHDOG_URL", "https://hb.mc.tests.mcctl.internal/")
    monkeypatch.setenv("HEARTBEAT_TOKEN", TOKEN)
    return sent


def test_heartbeat_paths(posts) -> None:
    assert notify.heartbeat("health", "", "ok")
    assert notify.heartbeat("backup", "start")
    assert notify.heartbeat("backup", "fail", "x" * 10000)
    assert [p[0] for p in posts] == [
        "https://hb.mc.tests.mcctl.internal/hb/health",
        "https://hb.mc.tests.mcctl.internal/hb/backup/start",
        "https://hb.mc.tests.mcctl.internal/hb/backup/fail",
    ]
    assert all(p[3] == TOKEN for p in posts) and len(posts[2][1]) == 4000
    with pytest.raises(ValueError):
        notify.heartbeat("other")


def test_heartbeat_off_without_settings(monkeypatch) -> None:
    monkeypatch.delenv("WATCHDOG_URL", raising=False)
    monkeypatch.setenv("HEARTBEAT_TOKEN", TOKEN)
    assert notify.heartbeat("health") is False


def test_heartbeat_failure_never_raises_or_leaks(monkeypatch, capsys) -> None:
    monkeypatch.setenv("WATCHDOG_URL", "https://hb.invalid")
    monkeypatch.setenv("HEARTBEAT_TOKEN", TOKEN)

    def boom(*a, **k):
        raise OSError(f"failed with {TOKEN}")

    monkeypatch.setattr(notify, "_post", boom)
    assert notify.heartbeat("health") is False
    assert TOKEN not in capsys.readouterr().err


def test_maintenance_marker_and_watchdog(posts, tmp_path) -> None:
    marker = tmp_path / "maintenance.json"
    notify.maintenance(20, "deploy\nrestart", now=1000.0, path=marker)
    assert json.loads(marker.read_text()) == {"until": 2200.0, "reason": "deploy restart"}
    assert notify.maintenance_active(now=2000.0, path=marker) == "deploy restart"
    assert notify.maintenance_active(now=2300.0, path=marker) is None
    url, body, _ctype, _token = posts[-1]
    assert url.endswith("/maintenance") and json.loads(body) == {"minutes": 20, "reason": "deploy restart"}
    notify.maintenance(0, "deploy", path=marker)
    assert not marker.exists() and json.loads(posts[-1][1])["minutes"] == 0
    notify.maintenance(99999, "x", now=0.0, path=marker)
    assert json.loads(marker.read_text())["until"] == 1440 * 60


def test_maintenance_active_tolerates_garbage(tmp_path) -> None:
    (tmp_path / "m").write_text("{not json")
    assert notify.maintenance_active(path=tmp_path / "m") is None
    assert notify.maintenance_active(path=tmp_path / "missing") is None


def test_cli(monkeypatch, tmp_path) -> None:
    seen = []
    monkeypatch.setattr(notify, "maintenance", lambda m, r: seen.append((m, r)))
    assert notify.main(["maintenance", "15", "deploy"]) == 0 and seen == [(15, "deploy")]
    assert notify.main(["maintenance", "x", "deploy"]) == 2
