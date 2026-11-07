"""v1.3 maintenance window (server.yml maintenance.window): a deploy outside it stages the change and leaves the
Minecraft restart to scripts/restart_window.py (the unit's ExecStartPre applies it at any start; the timer restarts
at the window's start). The script, the deploy's decision (site.yml), the role's wiring and the units."""

from __future__ import annotations

import datetime
import os
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import restart_window as rw
import yaml
from conftest import REPO

from mcctl import schema

TZ = ZoneInfo("Europe/Dublin")
WINDOW = "04:00-07:00"


def at(hh: int, mm: int) -> datetime.datetime:
    return datetime.datetime(2026, 11, 3, hh, mm, tzinfo=TZ)


def facts(**kw: object) -> rw.Facts:
    base = rw.Facts(now=at(4, 0), pending=True, server_active=True, admin_stopped=False, restore_running=False,
                    backup_running=False, maintenance=None)  # fmt: skip
    return replace(base, **kw)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("window", "inside", "outside"),
    [("04:00-07:00", ["04:00", "06:59"], ["03:59", "07:00", "12:00"]),
     ("23:00-01:30", ["23:00", "00:00", "01:29"], ["01:30", "22:59", "12:00"])],
)  # fmt: skip
def test_window_rule_is_the_same_in_the_script_and_mcctl(window: str, inside: list, outside: list) -> None:
    for t, want in [(t, True) for t in inside] + [(t, False) for t in outside]:
        hh, mm = map(int, t.split(":"))
        assert rw.in_window(window, at(hh, mm)) is want, t
        assert schema.in_window(window, datetime.time(hh, mm)) is want, t
    for bad in ("4:00-07:00", "24:00-01:00", "04:00"):
        with pytest.raises(ValueError):
            rw.parse_window(bad)


def test_the_timer_restarts_only_inside_the_window_with_nothing_in_the_way() -> None:
    assert rw.decide(facts(), WINDOW)[0] == "restart"
    assert rw.decide(facts(pending=False), WINDOW) == ("none", "no restart is pending")
    for kw, why in [
        ({"now": at(7, 0)}, "outside the maintenance window"),
        ({"admin_stopped": True}, "admin-stopped"),
        ({"server_active": False}, "isn't running"),
        ({"maintenance": "deploy"}, "maintenance window is open"),
        ({"restore_running": True}, "restore"),
        ({"backup_running": True}, "backup"),
    ]:
        action, reason = rw.decide(facts(**kw), WINDOW)
        assert action == "wait" and why in reason, (kw, reason)


def test_inside_the_window_it_waits_for_an_empty_server_until_the_last_minutes() -> None:
    """Maintainer decision (v1.3): empty -> now; players online or unknown (RCON unreachable) -> wait, until the
    window's last force_after_minutes_before_end minutes, then the countdown restart; 0 -> never forced."""
    assert rw.decide(facts(players=0), WINDOW, 30) == (
        "restart",
        f"inside the maintenance window ({WINDOW}), nobody online",
    )
    for players, who in ((3, "3 player(s) online"), (None, "unknown (RCON unreachable)")):
        action, why = rw.decide(facts(now=at(5, 0), players=players), WINDOW, 30)
        assert action == "wait" and who in why and "last 30 min" in why, why
        action, why = rw.decide(facts(now=at(6, 29), players=players), WINDOW, 30)
        assert action == "wait", why  # 31 min left
        action, why = rw.decide(facts(now=at(6, 30), players=players), WINDOW, 30)
        assert action == "restart" and "only 30 min" in why and "countdown" in why, why
        assert rw.decide(facts(now=at(6, 59), players=players), WINDOW, 30)[0] == "restart"
        action, why = rw.decide(facts(now=at(6, 59), players=players), WINDOW, 0)
        assert action == "wait" and "never forced" in why, why
    # a short window crossing midnight: 23:50-00:20, force 20 -> from 00:00
    short = "23:50-00:20"
    assert rw.minutes_left(short, at(23, 50)) == 30 and rw.minutes_left(short, at(0, 0)) == 20
    assert rw.decide(facts(now=at(23, 55), players=2), short, 20)[0] == "wait"
    assert rw.decide(facts(now=at(0, 0), players=2), short, 20)[0] == "restart"
    assert rw.decide(facts(now=at(0, 20), players=2), short, 20)[0] == "wait"  # outside again


def test_players_online_reads_rcon_list(monkeypatch: pytest.MonkeyPatch) -> None:
    class Rcon:
        def __init__(self, reply: str) -> None:
            self.reply = reply

        def __enter__(self) -> Rcon:
            return self

        def __exit__(self, *a: object) -> None:
            return None

        def command(self, cmd: str) -> str:
            assert cmd == "list"
            return self.reply

    monkeypatch.setattr(
        rw.mc_rcon, "connect", lambda timeout: Rcon("There are 2 of a max of 20 players online: a, b")
    )
    assert rw.players_online(True) == 2 and rw.players_online(False) == 0

    def down(timeout: float) -> None:
        raise OSError("refused")

    monkeypatch.setattr(rw.mc_rcon, "connect", down)
    assert rw.players_online(True) is None


def test_apply_copies_the_staged_files_and_empties_the_pending_dir(tmp_path: Path) -> None:
    pending, server = tmp_path / "pending", tmp_path / "server"
    (pending / "files" / "config").mkdir(parents=True)
    (pending / "files" / "server.properties").write_text("view-distance=8\n")
    (pending / "files" / "config" / "paper-global.yml").write_text("a: 1\n")
    (pending / "files" / "link").symlink_to("/etc/passwd")  # never followed
    (pending / "pending.json").write_text('{"since": "2026-11-03T12:00:00Z"}')
    server.mkdir()
    (server / "server.properties").write_text("view-distance=10\n")
    (server / "server.properties").chmod(0o600)
    assert rw.apply(pending, server) == ["config/paper-global.yml", "server.properties"]
    assert (server / "server.properties").read_text() == "view-distance=8\n"
    assert (server / "server.properties").stat().st_mode & 0o777 == 0o600  # the live file's mode kept
    assert (server / "config" / "paper-global.yml").stat().st_mode & 0o777 == 0o640
    assert not (server / "link").exists() and not (pending / "files").exists()
    assert not (pending / "pending.json").exists() and pending.is_dir()
    assert rw.apply(pending, server) == []  # nothing pending: a plain start


def test_main_dispatch(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    sent: list[str] = []
    monkeypatch.setattr(rw.notify, "discord", sent.append)
    assert rw.main(["deferred", WINDOW, "Europe/Dublin"]) == 0
    assert "04:00-07:00 Europe/Dublin" in sent[0] and "--force-restart" in sent[0]
    assert rw.main(["bogus"]) == 2
    monkeypatch.setenv("RESTART_WINDOW", WINDOW)
    monkeypatch.setenv("RESTART_TZ", "Europe/Dublin")
    monkeypatch.setattr(rw, "gather", lambda tz: facts(pending=False))
    monkeypatch.setattr(rw, "restart", lambda *a: pytest.fail("restarted"))
    assert rw.main(["--status"]) == 0 and "none: no restart is pending" in capsys.readouterr().out
    monkeypatch.setattr(rw, "gather", lambda tz: facts())
    assert rw.main(["--status"]) == 0 and "restart: inside" in capsys.readouterr().out  # --status never acts
    calls: list[tuple] = []
    monkeypatch.setattr(rw, "restart", lambda *a: calls.append(a) or 0)
    assert rw.main([]) == 0 and calls == [(WINDOW, "Europe/Dublin", 60)]
    # the deploy's question: the same rule from its arguments, never acting
    capsys.readouterr()
    monkeypatch.setattr(rw, "gather", lambda tz: facts(now=at(5, 0), players=4))
    monkeypatch.setattr(rw, "restart", lambda *a: pytest.fail("the deploy's decide restarted"))
    assert rw.main(["decide", WINDOW, "Europe/Dublin", "30"]) == 0
    assert capsys.readouterr().out.startswith("wait: 4 player(s) online")
    monkeypatch.setattr(rw, "gather", lambda tz: facts(now=at(6, 45), players=4))
    assert rw.main(["decide", WINDOW, "Europe/Dublin", "30"]) == 0
    assert capsys.readouterr().out.startswith("restart:")


def test_restart_opens_and_closes_maintenance_and_reports(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(rw, "PENDING_DIR", tmp_path)
    (tmp_path / "pending.json").write_text('{"since": "2026-11-03T12:00:00Z"}')
    log: list[object] = []
    monkeypatch.setattr(rw.notify, "maintenance", lambda m, r: log.append(("maintenance", m)))
    monkeypatch.setattr(rw.notify, "discord", lambda msg: log.append(("discord", msg)))
    monkeypatch.setattr(rw.mc_rcon, "countdown", lambda s: log.append(("countdown", s)))

    def systemctl(argv: list[str], **kw: object) -> None:
        log.append(("systemctl", argv[-2:]))
        (tmp_path / "pending.json").unlink()  # ExecStartPre applied it

    monkeypatch.setattr(rw.subprocess, "run", systemctl)
    monkeypatch.setattr(rw, "wait_for_rcon", lambda: True)
    assert rw.restart(WINDOW, "Europe/Dublin", 60) == 0
    assert [e[0] for e in log] == ["maintenance", "countdown", "systemctl", "maintenance", "discord"]
    assert log[2] == ("systemctl", ["restart", "minecraft.service"]) and log[3] == ("maintenance", 0)
    assert "2026-11-03T12:00:00Z" in str(log[4][1])


def test_server_yml_maintenance_field() -> None:
    from test_mcctl_render import full_data

    data = full_data()
    cfg = schema.load_server(data)
    assert cfg.maintenance.window == "04:00-07:00" and schema.maintenance_timezone(cfg) == "Europe/Dublin"
    london = ZoneInfo("Europe/Dublin")
    inside = datetime.datetime(2026, 11, 3, 5, 0, tzinfo=london)
    assert schema.restart_deferred(cfg, inside) is None
    assert schema.restart_deferred(cfg, inside.replace(hour=12)) == "04:00-07:00 Europe/Dublin"
    data["maintenance"] = {"window": "23:00-01:00"}  # timezone: host.timezone
    cfg = schema.load_server(data)
    assert schema.maintenance_timezone(cfg) == cfg.host.timezone
    assert schema.restart_deferred(cfg, datetime.datetime(2026, 11, 3, 0, 30, tzinfo=datetime.UTC)) is None
    data.pop("maintenance")
    assert schema.restart_deferred(schema.load_server(data), inside.replace(hour=12)) is None
    short = {"window": "23:50-00:20", "force_after_minutes_before_end": 20}  # a 30-min window still works
    assert schema.load_server(data | {"maintenance": short}).maintenance.force_after_minutes_before_end == 20
    assert schema.window_minutes("23:50-00:20") == 30 and schema.window_minutes("04:00-07:00") == 180
    assert (
        schema.load_server(
            data | {"maintenance": {"window": "04:00-07:00"}}
        ).maintenance.force_after_minutes_before_end
        == 30
    )
    for bad, why in [({"window": "04:00-04:30"}, "shorter than the window \\(30 min\\)"),
                     ({"window": "23:50-00:20", "force_after_minutes_before_end": 30}, "shorter than the window"),
                     ({"window": "04:00-07:00", "force_after_minutes_before_end": -1}, "force_after"),
                     ({"window": "4:00-07:00"}, "maintenance.window"), ({"window": "05:00-05:00"}, "same"),
                     ({"timezone": "Mars/Base"}, "time zone")]:  # fmt: skip
        data["maintenance"] = bad
        with pytest.raises(schema.SchemaError, match=why):
            schema.load_server(data)


# --- the deploy's decision (site.yml pre_tasks, run on localhost) ------------------------------------------


def hhmm(delta_min: int) -> str:
    t = datetime.datetime.now(datetime.UTC) + datetime.timedelta(minutes=delta_min)
    return f"{t.hour:02d}:{t.minute:02d}"


@pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
@pytest.mark.parametrize(
    ("window", "force", "defer"),
    [
        (lambda: f"{hhmm(-60)}-{hhmm(60)}", "", False),  # inside (crosses midnight at some hours of the day)
        (lambda: f"{hhmm(120)}-{hhmm(180)}", "", True),  # outside
        (lambda: f"{hhmm(120)}-{hhmm(180)}", "1", False),  # outside, forced
        (lambda: "", "", False),  # no window
    ],
)
def test_site_decides_whether_a_restart_waits(tmp_path: Path, window, force: str, defer: bool) -> None:
    site = yaml.safe_load((REPO / "ansible/site.yml").read_text())[0]
    names = ("Is this deploy forced to restart now?", "Read the clock in the maintenance window's time zone",
             "Read the maintenance window's bounds and the time",
             "Decide whether a restart has to wait for the maintenance window")  # fmt: skip
    picked = [t for t in site["pre_tasks"] if t["name"] in names]
    assert [t["name"] for t in picked] == list(names)
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "vars": {"mc_maintenance_window": window(), "mc_maintenance_timezone": "UTC"},
             "tasks": [*picked, {"ansible.builtin.assert": {"that": f"(mc_restart_defer | bool) == {defer}"}}]}]  # fmt: skip
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    env = {k: v for k, v in os.environ.items() if k != "MC_FORCE_RESTART"} | {"ANSIBLE_NOCOLOR": "1"}
    proc = subprocess.run(
        ["ansible-playbook", str(tmp_path / "play.yml"), "-i", "localhost,"],
        cwd=REPO / "ansible", env=env | ({"MC_FORCE_RESTART": force} if force else {}),
        capture_output=True, text=True, timeout=120, check=False,
    )  # fmt: skip
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "changed=0" in proc.stdout[proc.stdout.rfind("PLAY RECAP") :]


# --- the role's wiring ----------------------------------------------------------------------------------


def tasks(name: str) -> list[dict]:
    return yaml.safe_load((REPO / f"ansible/roles/minecraft/tasks/{name}").read_text())


def test_restart_handler_defers_or_restarts_and_discards_stale_staging() -> None:
    handler = yaml.safe_load((REPO / "ansible/roles/minecraft/handlers/main.yml").read_text())[0]
    assert "mc_restart_defer" in handler["vars"]["mc_restart_deferred"]
    assert "mc_active.status.ActiveState" in handler["vars"]["mc_restart_deferred"]  # only a running server
    restart = tasks("restart.yml")
    assert restart[0]["ansible.builtin.include_tasks"].endswith("tasks/defer.yml")
    assert restart[0]["when"] == "mc_restart_deferred | bool"
    assert restart[1]["when"] == "not (mc_restart_deferred | bool)"
    block = [t["name"] for t in restart[1]["block"]]
    assert (
        block.index("Apply config overlays while stopped")
        < block.index("Discard a restart that waited for the maintenance window (done now)")
        < block.index("Start the server")
    )


def test_defer_stages_overlays_marks_once_and_notifies_on_the_first_deferral() -> None:
    defer = tasks("defer.yml")
    stage = defer[0]["mc_config"]
    assert (
        stage["stage"] == "{{ mc_restart_pending_dir }}/files/{{ item.path }}"
        and defer[0]["become_user"] == "minecraft"
    )
    assert defer[1]["ansible.builtin.stat"]["path"] == "{{ mc_restart_pending_dir }}/pending.json"
    marker = defer[2]["ansible.builtin.copy"]
    assert marker["force"] is False and marker["owner"] == "minecraft"
    assert defer[4]["when"] == f"not {defer[1]['register']}.stat.exists" and defer[4]["changed_when"] is False
    assert "restart_window.py deferred" in defer[4]["ansible.builtin.command"]
    # the check compares against the staged copy, so a second deploy outside the window changes nothing
    check = tasks("overlays.yml")[0]
    assert check["check_mode"] is True and check["mc_config"]["stage"] == stage["stage"]
    assert "stage" not in tasks("overlays.yml")[1]["mc_config"]


def test_a_pending_restart_happens_at_the_next_deploy_inside_the_window() -> None:
    main = tasks("main.yml")
    names = [t["name"] for t in main]
    do = main[names.index("Do the restart that waited for the maintenance window now")]
    assert (
        do["notify"] == "restart minecraft" and "not (mc_restart_defer | default(false) | bool)" in do["when"]
    )
    ask = main[
        names.index("May the waiting restart happen now (nobody online, or the window's last minutes)?")
    ]
    assert "restart_window.py decide" in ask["ansible.builtin.command"]
    assert (
        "{{ mc_maintenance_force_minutes }}" in ask["ansible.builtin.command"] and ask["check_mode"] is False
    )
    assert "not (mc_force_restart | default(false) | bool)" in ask["when"]  # forced: no question
    assert (
        "mc_pending_decision.stdout is not defined or mc_pending_decision.stdout.startswith('restart')"
        in do["when"]
    )
    assert names.index(ask["name"]) < names.index(do["name"])
    assert names.index(do["name"]) < names.index(
        "Restart now if anything changed (so world border etc. apply to the new config)"
    )
    discard = main[
        names.index(
            "Discard a restart that waited for the maintenance window (the server was stopped anyway)"
        )
    ]
    assert names.index("Apply config overlays while stopped") < names.index(discard["name"])


def test_units_apply_at_every_start_and_the_timer_fires_at_the_window_start() -> None:
    from test_hardening import render

    unit = render(REPO / "ansible/roles/minecraft/templates/minecraft.service.j2")
    assert "ExecStartPre=-/opt/minecraft/bin/restart_window.py apply" in unit.splitlines()
    assert "ReadWritePaths=-/var/lib/minecraft/restart-pending" in unit.splitlines()
    timer = render(REPO / "ansible/roles/minecraft/templates/mc-restart-window.timer.j2").splitlines()
    assert "OnCalendar=*-*-* 04:00:00 Europe/Dublin" in timer and "OnCalendar=*-*-* *:00/15:00" in timer
    assert (
        "OnCalendar=*-*-* 06:30:00 Europe/Dublin" in timer
    )  # the force period's start (30 min before 07:00)
    short = render(REPO / "ansible/roles/minecraft/templates/mc-restart-window.timer.j2",
                   mc_maintenance_window="23:50-00:20", mc_maintenance_force_minutes=20).splitlines()  # fmt: skip
    assert (
        "OnCalendar=*-*-* 23:50:00 Europe/Dublin" in short
        and "OnCalendar=*-*-* 00:00:00 Europe/Dublin" in short
    )
    never = render(
        REPO / "ansible/roles/minecraft/templates/mc-restart-window.timer.j2", mc_maintenance_force_minutes=0
    )
    assert sum(line.startswith("OnCalendar=") for line in never.splitlines()) == 2
    svc = render(REPO / "ansible/roles/minecraft/templates/mc-restart-window.service.j2").splitlines()
    assert "ConditionPathExists=/var/lib/minecraft/restart-pending/pending.json" in svc
    assert (
        'Environment="RESTART_WINDOW=04:00-07:00"' in svc and 'Environment="RESTART_TZ=Europe/Dublin"' in svc
    )
    # polkit: restarting minecraft.service is already allowed (self-heal); nothing new
    rules = (REPO / "ansible/roles/base/templates/50-mc-ops.rules.j2").read_text()
    assert '"minecraft.service": ["restart", "kill", "start", "stop"]' in rules
    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    assert gv["mc_restart_pending_dir"] == "{{ mc_state_dir }}/restart-pending"
    assert str(rw.PENDING_DIR) == "/var/lib/minecraft/restart-pending"


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
def test_timer_calendar_is_valid() -> None:
    proc = subprocess.run(["systemd-analyze", "calendar", "*-*-* 04:00:00 Europe/Dublin"],
                          capture_output=True, text=True, check=False)  # fmt: skip
    assert proc.returncode == 0, proc.stderr


def test_workflow_force_restart_is_optional_and_reaches_only_the_first_run() -> None:
    """The reusable workflow stays backwards compatible (an optional boolean input, default false); a config repo's
    manual run can set it through its own dispatch input (template); only the first site.yml run gets it."""
    wf = yaml.safe_load((REPO / ".github/workflows/server.yml").read_text())
    on = wf[True] if True in wf else wf["on"]
    inp = on["workflow_call"]["inputs"]["force_restart"]
    assert inp["type"] == "boolean" and inp["default"] is False and "required" not in inp
    steps = wf["jobs"]["deploy"]["steps"]
    first = next(s for s in steps if s.get("name") == "ansible-playbook site.yml")
    assert "inputs.force_restart" in first["env"]["MC_FORCE_RESTART"]
    assert "github.event.inputs.force_restart == 'true'" in first["env"]["MC_FORCE_RESTART"]
    second = next(s for s in steps if s.get("name") == "Second run must change nothing (manual deploys)")
    assert "MC_FORCE_RESTART" not in second["env"]
    tpl = yaml.safe_load((REPO / "template/.github/workflows/server.yml").read_text())
    dispatch = (tpl[True] if True in tpl else tpl["on"])["workflow_dispatch"]["inputs"]["force_restart"]
    assert dispatch["type"] == "boolean" and dispatch["default"] is False
