import subprocess

import backup
import mc_rcon
import pytest


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch) -> None:
    """No test writes the box's backup state or sees a systemd timer start."""
    monkeypatch.setattr(backup, "STATE_FILE", tmp_path / "backup-state.json")
    for name in ("TRIGGER_UNIT", "BACKUP_GENTLE_SAVE", "BACKUP_UPLOAD_LIMIT_KIB", "BACKUP_S3_CONNECTIONS"):
        monkeypatch.delenv(name, raising=False)


def test_save_on_even_if_restic_fails(fake_server, tmp_path, monkeypatch) -> None:
    srv = fake_server(lambda c: "")
    (tmp_path / "world").mkdir()
    monkeypatch.setattr(backup, "SERVER_DIR", str(tmp_path))

    def boom(*args: str) -> None:
        raise subprocess.CalledProcessError(1, ["restic", *args])

    monkeypatch.setattr(backup, "restic", boom)
    with pytest.raises(subprocess.CalledProcessError):
        backup.run_backup()
    assert srv.commands == ["save-off", "save-all", "save-on"]


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
    assert order == ["backup after save-all", "forget after save-on"]
    assert "save-off window: " in capsys.readouterr().out


# --- v1.4: Chunky paused around save-off, the pre-generation hold ---------------------------------------------


class ChunkyServer:
    """RCON replies of a server with Chunky 1.5.3 (lang/en.json) for backup.run_backup."""

    def __init__(self, running: bool) -> None:
        self.running = running

    def __call__(self, cmd: str) -> str:
        if cmd == "chunky progress":
            return (
                "[Chunky] Task running for world. Processed: 10 chunks (1.00%), ETA: 0:01:00, Rate: 5.0 cps, "
                "Current: 0, 0"
                if self.running
                else "[Chunky] No tasks running."
            )
        if cmd == "chunky pause":
            self.running = False
            return "[Chunky] Task paused for world."
        if cmd == "chunky continue":
            self.running = True
            return "[Chunky] Task continuing for world."
        return ""


@pytest.fixture
def chunky_box(fake_server, tmp_path, monkeypatch):
    import notify

    (tmp_path / "world").mkdir()
    (tmp_path / "plugins").mkdir()
    (tmp_path / "plugins" / "Chunky-Bukkit-1.5.3.jar").write_bytes(b"")
    monkeypatch.setattr(backup, "SERVER_DIR", str(tmp_path))
    hold = tmp_path / "pregen-hold.json"
    monkeypatch.setattr(notify, "PREGEN_HOLD_FILE", hold)

    def make(running: bool, restic=None):
        chunky = ChunkyServer(running)
        srv = fake_server(chunky)
        held: list[str | None] = []

        def fake_restic(*args: str) -> None:
            if args[0] == "backup":
                held.append(notify.pregen_held())
            if restic:
                restic(*args)

        monkeypatch.setattr(backup, "restic", fake_restic)
        return srv, chunky, held, hold

    return make


def test_backup_pauses_running_pregen_around_save_off_and_continues_it(chunky_box) -> None:
    srv, chunky, held, hold = chunky_box(running=True)
    backup.run_backup()
    assert srv.commands == ["chunky progress", "chunky pause", "save-off", "save-all", "save-on",
                            "chunky continue"]  # fmt: skip
    assert held == ["backup"]  # health.py sees the hold for the whole snapshot
    assert not hold.exists() and chunky.running


def test_backup_leaves_idle_pregen_alone(chunky_box) -> None:
    srv, chunky, held, hold = chunky_box(running=False)
    backup.run_backup()
    assert srv.commands == ["chunky progress", "save-off", "save-all", "save-on"]
    assert held == [None] and not hold.exists() and not chunky.running


def test_backup_continues_pregen_and_ends_the_hold_even_when_it_fails(chunky_box) -> None:
    def boom(*args: str) -> None:
        raise subprocess.CalledProcessError(1, ["restic", *args])

    srv, chunky, held, hold = chunky_box(running=True, restic=boom)
    with pytest.raises(subprocess.CalledProcessError):
        backup.run_backup()
    assert srv.commands[-2:] == ["save-on", "chunky continue"] and chunky.running
    assert held == ["backup"] and not hold.exists()


def test_backup_without_chunky_sends_no_chunky_command(fake_server, tmp_path, monkeypatch) -> None:
    srv = fake_server(lambda c: "")
    (tmp_path / "world").mkdir()
    monkeypatch.setattr(backup, "SERVER_DIR", str(tmp_path))
    monkeypatch.setattr(backup, "restic", lambda *a: None)
    backup.run_backup()
    assert not any(c.startswith("chunky") for c in srv.commands)


def test_backup_matches_healths_chunky_strings_and_the_hold_expires(tmp_path) -> None:
    import health
    import notify

    assert backup.CHUNKY_RUNNING == health.CHUNKY_RUNNING
    marker = tmp_path / "hold.json"
    notify.pregen_hold("backup", now=1000.0, path=marker)
    assert notify.pregen_held(1000.0 + 60, marker) == "backup"
    assert notify.pregen_held(1000.0 + notify.PREGEN_HOLD_MAX_S + 1, marker) is None  # a killed backup
    notify.pregen_release(marker)
    assert notify.pregen_held(1000.0, marker) is None
    notify.pregen_release(marker)  # idempotent


# --- v1.5: smooth backups (skip when idle, defer under load, gentler save, restic limits) ----------------------

S = backup.Settings()
FP = ("f" * 64, 12)
NOW = 1_800_000_000.0
IDLE = backup.Probe(reachable=True, players=0, tps=20.0, mspt_1m=5.0, mspt_5s=5.0, mspt_5s_max=9.0)
STATE = {"last_backup": NOW - 3600, "fingerprint": FP[0], "files": 12, "skips": 0}


def test_skip_only_when_idle_unchanged_and_recent() -> None:
    assert "nobody played" in (backup.skip_reason(S, STATE, IDLE, FP, NOW) or "")
    down = backup.Probe()  # server not running: nobody can be online
    assert backup.skip_reason(S, STATE, down, FP, NOW)
    for why, kw in {
        "switched off": {"settings": backup.Settings(skip_when_idle=False)},
        "no backup recorded": {"state": {}},
        "world changed": {"current": ("0" * 64, 12)},
        "someone online": {"probe": backup.Probe(reachable=True, players=1)},
        "player count unreadable": {"probe": backup.Probe(reachable=True, players=None)},
        "pre-generation running": {"probe": backup.Probe(reachable=True, players=0, pregen_running=True)},
        "last real backup too old": {"now": NOW - 3600 + backup.BACKUP_SKIP_MAX_HOURS * 3600},
    }.items():
        args = {"settings": S, "state": STATE, "probe": IDLE, "current": FP, "now": NOW} | kw
        assert (
            backup.skip_reason(args["settings"], args["state"], args["probe"], args["current"], args["now"])
            is None
        ), why


def test_busy_only_with_players_and_lag() -> None:
    calm = backup.Probe(reachable=True, players=3, tps=20.0, mspt_1m=20.0, mspt_5s=20.0, mspt_5s_max=40.0)
    assert backup.busy_reason(calm) is None
    for probe, word in (
        (backup.Probe(reachable=True, players=3, tps=16.5, mspt_1m=20.0), "TPS 16.5"),
        (backup.Probe(reachable=True, players=3, tps=19.9, mspt_1m=42.0), "MSPT 42.0"),
        (backup.Probe(reachable=True, players=3, mspt_5s=55.0, mspt_5s_max=70.0), "tick spike"),
        (backup.Probe(reachable=True, players=3, mspt_5s=20.0, mspt_5s_max=180.0), "180 ms max"),
    ):
        assert word in (backup.busy_reason(probe) or ""), probe
    lagging_empty = backup.Probe(
        reachable=True, players=0, tps=5.0, mspt_1m=200.0, mspt_5s=200.0, mspt_5s_max=900.0
    )
    assert backup.busy_reason(lagging_empty) is None  # nobody to hurt: back up (and pregen is paused for it)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


BUSY = backup.Probe(reachable=True, players=2, tps=15.0)
CALM = backup.Probe(reachable=True, players=2, tps=20.0, mspt_1m=10.0)


def test_defer_waits_in_steps_until_calm() -> None:
    clock, looks = Clock(), iter([BUSY, BUSY, CALM])
    waited = backup.defer_while_busy(S, lambda: next(looks), clock.sleep, clock)
    assert clock.sleeps == [backup.DEFER_STEP_S] * 2 and waited == 2 * backup.DEFER_STEP_S


def test_defer_gives_up_after_the_maximum_and_backs_up_anyway(capsys) -> None:
    clock = Clock()
    waited = backup.defer_while_busy(backup.Settings(max_defer_minutes=5), lambda: BUSY, clock.sleep, clock)
    assert waited == 300 and sum(clock.sleeps) == 300
    assert "backing up anyway" in capsys.readouterr().out
    for off in (backup.Settings(defer_when_busy=False), backup.Settings(max_defer_minutes=0)):
        assert backup.defer_while_busy(off, lambda: BUSY, clock.sleep, clock) == 0.0


def test_smooth_records_a_skip_and_defers_otherwise(tmp_path) -> None:
    state = tmp_path / "state.json"
    backup.save_state(dict(STATE), state)

    class Rcon:
        def __init__(self, players: int, tps: str) -> None:
            self.replies = {"list": f"There are {players} of a max of 20 players online:", "tps": tps,
                            "mspt": "Server tick times (avg/min/max) from last 5s, 10s, 1m:\n"
                                    "◴ 5.0/1.0/9.0, 5.0/1.0/9.0, 5.0/1.0/9.0"}  # fmt: skip
            self.closed = False

        def command(self, cmd: str, timeout: float | None = None) -> str:
            return self.replies[cmd]

        def __exit__(self, *a: object) -> None:
            self.closed = True

    idle = Rcon(0, "TPS from last 5s, 1m, 5m, 15m: 20.0, 20.0, 20.0, 20.0")
    reason = backup.smooth(S, lambda: idle, now=lambda: NOW, current=lambda: FP, state_path=state)
    assert reason and "nobody played" in reason and idle.closed
    saved = backup.load_state(state)
    assert saved["skips"] == 1 and saved["last_skip"] == NOW and saved["fingerprint"] == FP[0]

    clock = Clock()
    busy = Rcon(4, "TPS from last 5s, 1m, 5m, 15m: 12.0, 12.0, 12.0, 12.0")
    out = backup.smooth(backup.Settings(max_defer_minutes=3), lambda: busy, now=lambda: NOW, sleep=clock.sleep,
                        clock=clock, current=lambda: FP, state_path=state)  # fmt: skip
    assert out is None and sum(clock.sleeps) == 180 and busy.closed
    assert backup.load_state(state)["skips"] == 1  # a deferral records nothing


def test_only_the_timer_may_skip_or_defer(monkeypatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(
        backup, "heartbeat", lambda check, status="", msg="": calls.append(f"hb {status} {msg}")
    )
    monkeypatch.setattr(backup, "smooth", lambda settings: "nobody played")
    monkeypatch.setattr(backup, "run_backup", lambda: calls.append("backup"))
    monkeypatch.setattr(backup, "forget_world_archives", lambda: None)
    assert backup.main([]) == 0
    assert calls == ["hb start ", "backup", "hb  backup ok"]  # a manual start: no skip check at all
    calls.clear()
    monkeypatch.setenv("TRIGGER_UNIT", "mc-backup.timer")
    assert backup.main([]) == 0
    assert calls == ["hb  backup skipped: nobody played"]  # OK heartbeat: the watchdog's freshness holds
    calls.clear()
    monkeypatch.setattr(backup, "smooth", lambda settings: (_ for _ in ()).throw(RuntimeError("boom")))
    assert backup.main([]) == 0 and "backup" in calls  # a failing check never stops a backup


def test_a_successful_backup_records_its_fingerprint(fake_server, tmp_path, monkeypatch) -> None:
    fake_server(lambda c: "")  # server.properties in tmp_path; the state file too (the autouse fixture)
    srv = tmp_path / "srv"
    (srv / "world" / "region").mkdir(parents=True)
    (srv / "world" / "region" / "r.0.0.mca").write_bytes(b"x")
    (srv / "logs").mkdir()
    monkeypatch.setattr(backup, "SERVER_DIR", str(srv))
    monkeypatch.setattr(backup, "world_dir", lambda: str(srv / "world"))
    monkeypatch.setattr(backup, "restic", lambda *a: None)
    backup.run_backup(gentle=False)
    state = backup.load_state()
    assert state["fingerprint"] == backup.fingerprint(srv)[0] and state["skips"] == 0
    (srv / "logs" / "latest.log").write_text("excluded: no change")
    assert backup.fingerprint(srv)[0] == state["fingerprint"]
    (srv / "world" / "region" / "r.0.0.mca").write_bytes(b"xy")
    assert backup.fingerprint(srv)[0] != state["fingerprint"]
    backup.STATE_FILE.unlink()
    backup.run_backup(tag="pre-restore", forget=False, gentle=False)
    assert backup.load_state() == {}  # only `auto` snapshots count for the skip


def test_fingerprint_honours_restics_excludes(tmp_path) -> None:
    for rel in ("plugins/Chunky-Bukkit-1.5.3.jar", "plugins/Chunky/config.yml", "cache/x", "server.jar",
                "world/level.dat"):  # fmt: skip
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text("x")
    seen = [rel for rel, _ in backup._files(tmp_path)]
    assert seen == ["plugins/Chunky/config.yml", "world/level.dat"]


def test_gentle_save_waits_for_quiet_files_and_falls_back_to_flush() -> None:
    class Rcon:
        def __init__(self) -> None:
            self.commands: list[str] = []

        def command(self, cmd: str, timeout: float | None = None) -> str:
            self.commands.append(cmd)
            return ""

    r = Rcon()
    assert backup.save_world(r, gentle=True, settle=lambda: 2.5).startswith("gentle")
    assert r.commands == ["save-all"]
    r = Rcon()
    assert backup.save_world(r, gentle=True, settle=lambda: None).startswith("flush")
    assert r.commands == ["save-all", "save-all flush"]
    r = Rcon()
    assert backup.save_world(r, gentle=False) == "flush" and r.commands == ["save-all flush"]

    clock = Clock()
    clock.t = 1000.0
    writes = iter([999.5, 999.6, 1000.2, 1000.2, 1000.2, 1000.2, 1000.2, 1000.2])  # the IO threads drain
    waited = backup.wait_settled(lambda: next(writes), clock, clock.sleep, quiet_s=2.0, max_s=15.0)
    assert waited is not None and 2.0 <= waited <= 3.0
    clock.t = 0.0
    assert (
        backup.wait_settled(lambda: clock.t, clock, clock.sleep, quiet_s=2.0, max_s=15.0) is None
    )  # never quiet


def test_restic_limits_come_from_the_unit() -> None:
    assert backup.restic_limits({}) == []
    assert backup.restic_limits({"BACKUP_UPLOAD_LIMIT_KIB": "0", "BACKUP_S3_CONNECTIONS": ""}) == []
    assert backup.restic_limits({"BACKUP_UPLOAD_LIMIT_KIB": "2048", "BACKUP_S3_CONNECTIONS": "2"}) == [
        "--limit-upload", "2048", "-o", "s3.connections=2"]  # fmt: skip
    env = {"BACKUP_SKIP_WHEN_IDLE": "0", "BACKUP_DEFER_WHEN_BUSY": "1", "BACKUP_MAX_DEFER_MINUTES": "20",
           "BACKUP_GENTLE_SAVE": "false"}  # fmt: skip
    assert backup.Settings.from_env(env) == backup.Settings(False, True, 20, False)
    assert backup.Settings.from_env({}) == backup.Settings()


def test_mspt_and_tps_parsers_read_the_pinned_servers_replies() -> None:
    text = "Server tick times (avg/min/max) from last 5s, 10s, 1m:\n◴ 12.5/3.0/140.2, 11.0/3.0/90.0, 9.8/2.0/150.0"
    assert backup.parse_mspt(text) == (12.5, 140.2, 9.8)
    assert backup.parse_mspt("Unknown command") is None
    assert backup.parse_tps("TPS from last 5s, 1m, 5m, 15m: 19.5, *20.0, 20.0, 20.0") == 20.0
    assert backup.parse_tps("nope") is None


# --- v1.5.1: the live e2e of the skip path (tests/e2e/backup_skip_check.py), its logic here -----------------


def _skip_check(monkeypatch):
    import importlib
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).parent / "e2e"))
    return importlib.import_module("backup_skip_check")


def test_the_e2e_skip_check_judges_what_backup_py_really_does(
    fake_server, tmp_path, monkeypatch, capsys
) -> None:
    """The e2e round's verdicts, fed with scripts/backup.py's own output, heartbeats and state: a timer run on
    an idle, unchanged server is a recorded skip with one OK heartbeat; after a world file changed, the next
    timer run backs up (start + ok, recorded, skips reset)."""
    check = _skip_check(monkeypatch)
    fake_server(lambda c: "There are 0 of a max of 20 players online:" if c == "list" else "")
    srv = tmp_path / "srv"  # not tmp_path: the state file lives there (the autouse fixture)
    (srv / "world" / "region").mkdir(parents=True)
    (srv / "world" / "region" / "r.0.0.mca").write_bytes(b"x")
    monkeypatch.setattr(backup, "SERVER_DIR", str(srv))
    monkeypatch.setattr(backup, "world_dir", lambda: str(srv / "world"))
    monkeypatch.setattr(backup, "restic", lambda *a: print("snapshot 1a2b3c4d saved"))
    monkeypatch.setattr(backup, "forget_world_archives", lambda: None)
    hits: list = []

    def heartbeat(name: str, status: str = "", message: str = "") -> bool:  # what notify.py posts
        hits.append(
            check.Hit(f"/hb/{name}" + (f"/{status}" if status else ""), message, f"Bearer {check.TOKEN}")
        )
        return True

    monkeypatch.setattr(backup, "heartbeat", heartbeat)
    for line in check.env_file("https://127.0.0.1:1", tmp_path / "ca.pem").splitlines():
        name, value = line.split("=", 1)
        if name in ("TRIGGER_UNIT", "BACKUP_SKIP_WHEN_IDLE", "BACKUP_DEFER_WHEN_BUSY"):
            monkeypatch.setenv(name, value)
    backup.record_backup(backup.fingerprint(), backup.time.time() - 600)  # the last backup, 10 min ago
    capsys.readouterr()

    before = backup.load_state()
    assert backup.main([]) == 0
    journal = capsys.readouterr().out
    assert check.outcome(journal) == "skipped" and check.finished(journal), journal
    assert check.skip_problems(journal, hits, before, backup.load_state(), 0) == []
    assert check.skip_problems(journal, hits, before, backup.load_state(), 1) == [
        "a skipped run made 1 snapshot(s)"
    ]

    (srv / "world" / "e2e-backup-skip.txt").write_text("changed")
    before, hits[:] = backup.load_state(), []
    assert backup.main([]) == 0
    journal = capsys.readouterr().out
    assert check.outcome(journal) == "backed-up" and check.finished(journal)
    assert check.backup_problems(journal, hits, before, backup.load_state(), 1) == []
    # and the verdicts catch the opposite case each way
    assert check.skip_problems(journal, hits, before, backup.load_state(), 1)
    assert check.backup_problems("backup skipped: x", [], before, before, 0)


def test_the_e2e_skip_check_drop_in_and_verdicts(monkeypatch) -> None:
    check = _skip_check(monkeypatch)
    env = dict(
        line.split("=", 1) for line in check.env_file("https://127.0.0.1:4443", check.CERT).splitlines()
    )
    assert env["TRIGGER_UNIT"] == backup.TIMER_UNIT  # what backup.scheduled() looks for
    assert backup.scheduled(env) and backup.Settings.from_env(env) == backup.Settings(defer_when_busy=False)
    assert env["WATCHDOG_URL"].startswith("https://127.0.0.1:")  # notify.py posts to https only
    assert env["SSL_CERT_FILE"] == str(check.CERT) and env["HEARTBEAT_TOKEN"] == check.TOKEN
    # a second EnvironmentFile (read after restic.env: it wins), nothing else changed in the unit
    assert check.dropin() == f"[Service]\nEnvironmentFile={check.ENV_FILE}\n"
    assert str(check.DROPIN).startswith("/run/systemd/system/mc-backup.service.d/")
    assert not str(check.ETC).startswith("/etc/minecraft")  # InaccessiblePaths in the backup unit
    assert check.outcome("Traceback (most recent call last):") == "failed"
    assert check.outcome("x\n(start failed: Job failed)") == "failed"
    assert not check.finished("save: gentle (save-all, files quiet after 0.6 s)")
    assert check.level_name("motd=x\nlevel-name=atlas\n") == "atlas" and check.level_name("") == "world"
    assert check.ATTEMPTS >= 3


def test_the_e2e_watchdog_mock_takes_notify_pys_https_heartbeat(tmp_path, monkeypatch) -> None:
    """The mock's self-signed certificate, trusted through SSL_CERT_FILE, carries notify.py's real POST."""
    import shutil

    import notify

    if shutil.which("openssl") is None:
        pytest.skip("no openssl")
    check = _skip_check(monkeypatch)
    monkeypatch.setattr(check, "ETC", tmp_path / "etc")
    monkeypatch.setattr(check, "CERT", tmp_path / "etc" / "watchdog.pem")
    monkeypatch.setattr(check, "KEY", tmp_path / "etc" / "watchdog.key")
    check.make_cert()
    mock = check.Watchdog()
    import urllib.request

    for name in (
        "HTTPS_PROXY",
        "https_proxy",
        "ALL_PROXY",
        "all_proxy",
    ):  # straight to 127.0.0.1, as in the box
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(urllib.request, "_opener", None)  # no opener another test installed
    with mock.serving():
        monkeypatch.setenv("SSL_CERT_FILE", str(check.CERT))
        monkeypatch.setenv("WATCHDOG_URL", f"https://127.0.0.1:{mock.port}")
        monkeypatch.setenv("HEARTBEAT_TOKEN", check.TOKEN)
        assert notify.heartbeat("backup", "", "backup skipped: nobody played")
    assert mock.hits == [check.Hit("/hb/backup", "backup skipped: nobody played", f"Bearer {check.TOKEN}")]
