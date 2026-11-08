import json
import re
from dataclasses import asdict
from datetime import date
from pathlib import Path

import health
import pytest
from health import GcStats, HostStats, State

CFG = {"mspt_alert": 45, "mspt_checks": 3, "heap_pct": 85, "heap_checks": 2, "gc_pause_ms": 100}

MSPT_OUT = (
    "§6Server tick times §e(§7avg§e/§7min§e/§7max§e)§6 from last 5s§e,§6 10s§e,§6 1m§e:\n"
    "§6◴ §a12.1§7/§a5.0§7/§a30.2§e, §a11.0§7/§a4.0§7/§a31.0§e, §a10.5§7/§a3.9§7/§a40.0"
)
TPS_OUT = "§6TPS from last 5s, 1m, 5m, 15m: §a*20.0§6, §a19.8§6, §a19.9§6, §a20.0"
GC = [
    "[2026-10-30T10:00:00.000+0000][100.000s][info][gc] GC(1) Pause Young (Normal) (G1 Evacuation Pause) 3000M->1000M(6656M) 12.345ms\n",
    "[2026-10-30T10:00:05.000+0000][105.000s][info][gc] GC(2) Pause Young (Mixed) (G1 Evacuation Pause) 3200M->900M(6656M) 150.000ms\n",
    "[2026-10-30T10:00:06.000+0000][106.000s][info][gc] GC(3) Pause Remark 1500M->1400M(6656M) 3.000ms\n",
    "[2026-10-30T10:00:07.000+0000][107.000s][info][gc] GC(3) Concurrent Mark Cycle 50.000ms\n",
]


def test_parse_mspt_uses_1m_avg() -> None:
    assert health.parse_mspt(MSPT_OUT) == 10.5


def test_parse_tps_uses_1m() -> None:
    assert health.parse_tps(TPS_OUT) == 19.8


def test_parse_tps_paper_format() -> None:
    assert health.parse_tps("§6TPS from last 1m, 5m, 15m: §a19.7§6, §a19.9§6, §a*20.0") == 19.7


def test_parse_gc_since_and_heap() -> None:
    s = health.parse_gc(GC, since_uptime=101.0, pause_alert_ms=100)
    assert s.max_pause_ms == 150.0
    assert s.long_pauses == [150.0]
    assert s.last_uptime == 106.0
    assert round(s.heap_after_pct, 1) == round(100 * 900 / 6656, 1)


def test_parse_gc_detects_jvm_restart() -> None:
    s = health.parse_gc(GC, since_uptime=5000.0, pause_alert_ms=100)
    assert s.max_pause_ms == 150.0  # uptime reset -> everything is new


# P16: a real Generational ZGC log (Temurin 25.0.5+8, -Xlog:gc,gc+phases:...:time,uptime,level,tags as in
# mc_jvm_flags_zgc): a major collection, two minor ones, one interleaved old-generation pause, an allocation stall.
ZGC = (
    (Path(__file__).parent / "fixtures" / "gc" / "zgc-temurin-25.0.5.log")
    .read_text()
    .splitlines(keepends=True)
)


def test_parse_gc_zgc_pauses_stalls_and_heap() -> None:
    events = health.gc_events(ZGC)
    pauses = [e.pause_ms for e in events if e.pause_ms is not None]
    # 5 pauses of the major GC(1), 3 + 3 of the minor GC(183)/GC(184), 1 old-generation one of GC(178), 1 stall
    assert len(pauses) == 13
    assert sum("Pause " in ln for ln in ZGC) + sum("Allocation Stall" in ln for ln in ZGC) == 13
    s = health.parse_gc(ZGC, since_uptime=0.0, pause_alert_ms=0.05)
    # the stall (main thread: 0.073 ms) is a stall, not a pause; ZGC's own pauses are all below 0.05 ms
    assert s.stalls == [0.073] and s.long_pauses == [] and s.collector == "zgc"
    assert s.freeze_ms() == 0.073 and s.max_pause_ms < 0.05
    assert s.last_uptime == 1.142
    assert s.heap_after_pct == 12.0  # minimum after-collection occupancy (the % ZGC prints)
    # since: only what came after GC(1)
    later = health.parse_gc(ZGC, since_uptime=1.0, pause_alert_ms=100)
    assert later.freeze_ms() == 0.073 and later.long_pauses == []
    # no pause-free ZGC line ("Concurrent Mark 13.968ms") counts as a pause
    assert max(pauses) < 1.0


# A live box (Temurin 25, generational ZGC): concurrent cycle times (1.418 s, 0.400 s), never stop-the-world pauses.
ZGC_LIVE = [
    "[2026-11-02T18:10:01.100+0000][3600.100s][info][gc,phases] GC(24) O: Old Generation 662M(13%)->806M(16%) 1.009s\n",
    "[2026-11-02T18:10:01.500+0000][3600.500s][info][gc       ] GC(24) Major Collection (Proactive) 2832M(55%)->806M(16%) 1.418s\n",
    "[2026-11-02T18:12:00.100+0000][3719.100s][info][gc,phases] GC(25) y: Young Generation 4756M(93%)->678M(13%) 0.399s\n",
    "[2026-11-02T18:12:00.200+0000][3719.200s][info][gc       ] GC(25) Minor Collection (Allocation Rate) 4754M(93%)->678M(13%) 0.400s\n",
]


def test_zgc_concurrent_cycle_times_are_never_pauses() -> None:
    s = health.parse_gc(ZGC_LIVE, since_uptime=0.0, pause_alert_ms=50)
    assert (s.max_pause_ms, s.long_pauses, s.stalls, s.freeze_ms()) == (0.0, [], [], 0.0)
    assert s.collector == "zgc" and s.heap_after_pct == 13.0  # from the collections' ->NNNM(NN%)
    assert all(e.pause_ms is None for e in health.gc_events(ZGC_LIVE))
    _, _, msgs = health.evaluate(State(status="ok"), True, 20.0, 20.0, s, CFG, health.Ctx(now=NOW))
    assert msgs == []  # no "GC pause" alert from a 1.4 s concurrent cycle


def test_last_heap_g1_and_zgc() -> None:
    assert health.last_heap(GC) == (1400, 6656)  # G1: the capacity is on the line
    # ZGC prints M(%) only: 132 MB after the last collection, capacity estimated (160 MB heap)
    assert health.last_heap(ZGC) == (132, 160)
    assert health.last_heap([]) is None
    assert health.last_heap(["[0.1s][info][gc] Using The Z Garbage Collector\n"]) is None


def test_zgc_lines_dont_match_g1_and_back() -> None:
    assert not any(health.GC_LINE.search(ln) for ln in ZGC)
    assert not any(health.ZGC_PAUSE.search(ln) or health.ZGC_CYCLE.search(ln) for ln in GC)


def test_down_alerts_once() -> None:
    st, hc, msgs = health.evaluate(State(status="ok"), False, None, None, GcStats(), CFG)
    assert (st.status, hc, len(msgs)) == ("down", "fail", 1)
    _, hc2, msgs2 = health.evaluate(st, False, None, None, GcStats(), CFG)
    assert (hc2, msgs2) == ("fail", [])


def test_mspt_needs_consecutive_checks() -> None:
    st = State(status="ok")
    for _ in range(2):
        st, hc, _ = health.evaluate(st, True, 50.0, 18.0, GcStats(), CFG)
        assert hc == ""
    st, hc, msgs = health.evaluate(st, True, 50.0, 18.0, GcStats(), CFG)
    assert (st.status, hc) == ("degraded", "fail") and "The server is lagging" in msgs[0]
    assert "50.0 ms per tick" in msgs[0]
    st, hc, msgs = health.evaluate(st, True, 20.0, 20.0, GcStats(), CFG)
    assert (st.status, hc) == ("ok", "") and msgs[0].startswith(
        "\u2705 **The server runs at full speed again**"
    )


def test_heap_alert_once() -> None:
    st = State(status="ok")
    gc = GcStats(heap_after_pct=90.0)
    st, _, m1 = health.evaluate(st, True, 20.0, 20.0, gc, CFG)
    st, _, m2 = health.evaluate(st, True, 20.0, 20.0, gc, CFG)
    st, _, m3 = health.evaluate(st, True, 20.0, 20.0, gc, CFG)
    assert not m1 and any("heap" in m for m in m2) and not m3


HOST_CFG = {"disk_pct": 85, "steal_pct": 10, "steal_checks": 3}
DAY = date(2026, 10, 31)
PROC_STAT = "cpu  1000 10 500 8000 50 0 20 100 0 0\ncpu0 500 5 250 4000 25 0 10 50 0 0\nintr 12345\n"


def cpu_after(prev: list[int], busy: int, steal: int) -> list[int]:
    cur = list(prev)
    cur[0] += busy
    cur[7] += steal
    return cur


def test_parse_proc_stat_uses_aggregate_line() -> None:
    assert health.parse_proc_stat(PROC_STAT) == [1000, 10, 500, 8000, 50, 0, 20, 100, 0, 0]


def test_steal_pct_from_deltas() -> None:
    prev = health.parse_proc_stat(PROC_STAT)
    assert health.steal_pct(prev, cpu_after(prev, 80, 20)) == 20.0
    assert health.steal_pct([], prev) is None  # first run
    assert health.steal_pct(prev, [0] * 10) is None  # counters reset (reboot)


def test_steal_alerts_after_sustained_checks_once() -> None:
    st = State(cpu_prev=health.parse_proc_stat(PROC_STAT))
    msgs: list[str] = []
    for _ in range(3):
        st, steal, m = health.evaluate_host(st, HostStats(cpu=cpu_after(st.cpu_prev, 80, 20)), HOST_CFG, DAY)
        msgs += m
    assert steal == 20.0 and len(msgs) == 1 and health.RESIZE_HINT in msgs[0]
    st, _, m = health.evaluate_host(st, HostStats(cpu=cpu_after(st.cpu_prev, 80, 20)), HOST_CFG, DAY)
    assert m == []  # no repeat while it stays high
    st, _, _ = health.evaluate_host(st, HostStats(cpu=cpu_after(st.cpu_prev, 99, 1)), HOST_CFG, DAY)
    assert (st.steal_high, st.steal_alerted) == (0, False)


def test_steal_needs_consecutive_checks() -> None:
    st = State(cpu_prev=health.parse_proc_stat(PROC_STAT))
    for busy, steal in [(80, 20), (80, 20), (99, 1), (80, 20), (80, 20)]:
        st, _, m = health.evaluate_host(st, HostStats(cpu=cpu_after(st.cpu_prev, busy, steal)), HOST_CFG, DAY)
        assert m == []


def test_disk_alert_once_per_crossing() -> None:
    st, _, m1 = health.evaluate_host(State(), HostStats(disk_pct=90.0), HOST_CFG, DAY)
    st, _, m2 = health.evaluate_host(st, HostStats(disk_pct=91.0), HOST_CFG, DAY)
    st, _, m3 = health.evaluate_host(st, HostStats(disk_pct=50.0), HOST_CFG, DAY)
    st, _, m4 = health.evaluate_host(st, HostStats(disk_pct=85.0), HOST_CFG, DAY)
    assert len(m1) == 1 and "disk is filling up" in m1[0] and "90% used" in m1[0]
    assert m2 == []
    assert len(m3) == 1 and "Disk space is fine again" in m3[0]  # one recovered message
    assert len(m4) == 1


def test_pending_reboots_are_left_to_the_reboot_gate() -> None:
    """P11: mc-reboot-gate announces pending reboots (and warns after 3 days); health.py doesn't alert twice."""
    st, _, m1 = health.evaluate_host(State(), HostStats(reboot_required=True), HOST_CFG, DAY)
    st, _, m2 = health.evaluate_host(st, HostStats(reboot_required=True), HOST_CFG, date(2026, 11, 1))
    assert m1 == [] and m2 == []


def test_disk_used_pct(tmp_path) -> None:
    pct = health.disk_used_pct(tmp_path)
    assert pct is not None and 0 <= pct <= 100
    assert health.disk_used_pct(tmp_path / "missing") is None


def test_old_state_file_still_loads(tmp_path) -> None:
    p = tmp_path / "state.json"
    p.write_text(
        '{"status": "ok", "mspt_high": 1, "heap_high": 0, "heap_alerted": false, "last_gc_uptime": 3.0}'
    )
    st = health.load_state(p)
    assert st.mspt_high == 1 and st.cpu_prev == []


def test_resize_hint_comes_from_env(monkeypatch) -> None:
    # The deploy writes server.yml host.resize_hint into health.env as RESIZE_HINT.
    monkeypatch.setenv("RESIZE_HINT", "netcup RS (dedicated cores) or larger server")
    st = State(status="ok")
    for _ in range(3):
        st, _, msgs = health.evaluate(st, True, 50.0, 18.0, GcStats(), CFG)
    assert "netcup RS (dedicated cores)" in msgs[0]
    monkeypatch.delenv("RESIZE_HINT")
    assert health.resize_hint() == health.RESIZE_HINT


# --- self-heal (design §6) ---------------------------------------------------------------------------

from health import ServiceState, apply_heal, decide_heal  # noqa: E402

NOW = 1_800_000_000.0
DOWN = ServiceState(active_state="failed")
HUNG = ServiceState(active_state="active", active_for_s=3600)


def test_down_server_is_restarted_quietly() -> None:
    d = decide_heal(State(status="ok"), False, DOWN, NOW)
    assert (d.action, d.quiet, d.alerts) == ("restart", True, [])
    assert d.state.heals == [NOW] and d.state.heal_pending


def test_quiet_heal_suppresses_down_and_recovery_messages() -> None:
    prev = State(status="ok")
    st, hb, msgs = health.evaluate(prev, False, None, None, GcStats(), CFG)
    assert len(msgs) == 1 and msgs[0].startswith("\U0001f534 **The Minecraft server is down**")
    d = decide_heal(st, False, DOWN, NOW)
    st, msgs, hb = apply_heal(prev, d, msgs, hb, rcon_ok=False)
    assert (msgs, hb) == ([], "")  # nobody alerted, the heartbeat stays green
    # Next check: back up. The heal worked, so no "recovered" message either.
    st2, hb2, msgs2 = health.evaluate(st, True, 20.0, 20.0, GcStats(), CFG)
    d2 = decide_heal(st2, True, ServiceState(), NOW + 300)
    st2, msgs2, hb2 = apply_heal(st, d2, msgs2, hb2, rcon_ok=True)
    assert (msgs2, hb2) == ([], "") and not st2.heal_pending and st2.heals == [NOW]


def test_failed_heal_alerts_then_recovery_is_announced() -> None:
    st = decide_heal(State(status="down"), False, DOWN, NOW).state
    d = decide_heal(st, False, DOWN, NOW + 300)  # still down after the restart
    assert len(d.alerts) == 1 and "down and a restart didn't help" in d.alerts[0]
    assert d.action == "restart" and not d.quiet  # tries once more, but says so
    st, msgs, hb = apply_heal(st, d, [], "fail", rcon_ok=False)
    assert hb == "fail" and msgs == d.alerts and st.down_alerted
    st3, hb3, msgs3 = health.evaluate(st, True, 20.0, 20.0, GcStats(), CFG)
    st3, msgs3, hb3 = apply_heal(st, decide_heal(st3, True, ServiceState(), NOW + 600), msgs3, hb3, True)
    assert any("The Minecraft server is back" in m for m in msgs3) and not st3.down_alerted


def test_more_than_two_heals_a_day_alert_and_the_fourth_gives_up() -> None:
    st = State(status="ok")
    alerts = []
    for i in range(3):
        d = decide_heal(st, False, DOWN, NOW + i * 3600)
        assert d.action == "restart"
        alerts.append(d.alerts)
        # it came back up in between
        st = decide_heal(d.state, True, ServiceState(), NOW + i * 3600 + 300).state
    assert alerts[:2] == [[], []] and "3 restarts in 24 h" in alerts[2][0]
    d = decide_heal(st, False, DOWN, NOW + 4 * 3600)
    assert d.action == "none" and "gave up after 3 restarts" in d.alerts[0]
    d2 = decide_heal(d.state, False, DOWN, NOW + 5 * 3600)
    assert d2.action == "none" and d2.alerts == [] and not d2.quiet  # keeps failing, no repeat message
    # A day after the first heal the budget frees up again.
    assert decide_heal(d2.state, False, DOWN, NOW + health.DAY_S + 1).action == "restart"


def test_rcon_dead_with_process_up_dumps_threads_after_two_checks() -> None:
    d = decide_heal(State(status="ok"), False, HUNG, NOW)
    assert (d.action, d.quiet, d.state.rcon_dead) == ("none", True, 1)
    d = decide_heal(d.state, False, HUNG, NOW + 300)
    assert d.action == "dump-restart" and "RCON dead for 2 checks" in d.reason and d.state.rcon_dead == 0


def test_starting_server_is_left_alone() -> None:
    starting = ServiceState(active_state="active", active_for_s=120)
    st = State()
    for i in range(5):
        d = decide_heal(st, False, starting, NOW + i * 300)
        assert (d.action, d.quiet, d.reason) == ("none", True, "starting")
        st = d.state


@pytest.mark.parametrize(
    ("svc", "reason"),
    [
        (ServiceState(active_state="inactive", admin_stopped=True), "admin-stopped"),
        (ServiceState(active_state="inactive", maintenance="deploy"), "deploy"),
        (ServiceState(active_state="activating"), "unit activating"),
        (ServiceState(active_state="deactivating"), "unit deactivating"),
    ],
)
def test_no_heal_when_stopped_on_purpose_or_in_transition(svc, reason) -> None:
    d = decide_heal(State(status="down", heal_pending=True), False, svc, NOW)
    assert (d.action, d.quiet, d.reason, d.alerts) == ("none", True, reason, [])


def test_service_state_parses_systemctl(monkeypatch, tmp_path) -> None:
    import subprocess

    out = "ActiveState=active\nActiveEnterTimestamp=@1799999000\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, out, ""))
    monkeypatch.setattr(health, "ADMIN_STOPPED", tmp_path / "admin-stopped")
    monkeypatch.setattr(health, "maintenance_active", lambda now: None)
    svc = health.service_state(NOW)
    assert (svc.active_state, svc.active_for_s, svc.admin_stopped, svc.maintenance) == (
        "active",
        1000,
        False,
        None,
    )


def test_heal_restarts_through_systemctl_and_dumps_threads(monkeypatch, capsys) -> None:
    import subprocess

    calls: list[list[str]] = []

    def run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(health.os, "kill", lambda pid, sig: pytest.fail("the JVM is another user's process"))
    monkeypatch.setattr(health.time, "sleep", lambda s: None)
    assert health.heal("dump-restart", "RCON dead")
    assert calls == [
        ["systemctl", "--no-ask-password", "kill", "--signal=SIGQUIT", "minecraft.service"],
        ["systemctl", "--no-ask-password", "restart", "--no-block", "minecraft.service"],
    ]
    assert "AUDIT self-heal action=dump-restart reason='RCON dead'" in capsys.readouterr().out


def test_startup_grace_comes_from_env(monkeypatch) -> None:
    svc = health.ServiceState(active_state="active", active_for_s=120)
    state = health.State(status="down", rcon_dead=1)
    assert health.decide_heal(state, False, svc, 1e6).reason == "starting"
    monkeypatch.setenv("HEAL_STARTUP_GRACE_S", "60")
    d = health.decide_heal(state, False, svc, 1e6, health.startup_grace_s())
    assert d.action == "dump-restart"


# --- P6 review: liveness is "RCON answers", never "MSPT parsed" ---------------------------------------


def test_probe_rcon_reads_mspt_and_tps(fake_server) -> None:
    replies = {"mspt": MSPT_OUT, "tps": TPS_OUT}
    fake_server(lambda cmd: replies[cmd])
    probe = health.probe_rcon()
    assert (probe.alive, probe.mspt, probe.tps, probe.warnings) == (True, 10.5, 19.8, [])


def test_unparseable_replies_are_a_warning_not_a_dead_server(fake_server) -> None:
    fake_server(lambda cmd: "Unknown command. Type /help")
    probe = health.probe_rcon()
    assert probe.alive is True and probe.mspt is None and probe.tps is None
    assert len(probe.warnings) == 2
    st, hb, msgs = health.evaluate(State(status="ok"), probe.alive, None, None, GcStats(), CFG)
    assert (st.status, hb, msgs) == ("ok", "", [])
    d = decide_heal(st, probe.alive, ServiceState(), NOW)
    assert d.action == "none" and not d.alerts


def test_refused_rcon_password_is_alive(fake_server, tmp_path) -> None:
    srv = fake_server(lambda cmd: "")
    props = tmp_path / "server.properties"
    props.write_text(props.read_text().replace("rcon.password=pw", "rcon.password=stale"))
    probe = health.probe_rcon()
    assert probe.alive is True and "refused the password" in probe.warnings[0]
    assert srv.commands == []


def test_nothing_listening_is_dead_and_missing_settings_are_unknown(fake_server, tmp_path) -> None:
    import socket

    fake_server(lambda cmd: "")
    with socket.socket() as free:  # a port nothing listens on
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    props = tmp_path / "server.properties"
    props.write_text(re.sub(r"rcon\.port=\d+", f"rcon.port={port}", props.read_text()))
    assert health.probe_rcon(timeout=2).alive is False
    (tmp_path / "server.properties").unlink()
    probe = health.probe_rcon()
    assert probe.alive is None and "settings unreadable" in probe.warnings[0]


def test_unknown_liveness_or_unit_state_never_restarts() -> None:
    d = decide_heal(State(status="ok"), None, DOWN, NOW)
    assert (d.action, d.quiet) == ("none", True)
    st, hb, msgs = health.evaluate(State(status="ok"), None, None, None, GcStats(), CFG)
    assert (st.status, hb, msgs) == ("ok", "", [])
    for i in range(5):  # systemctl unreadable: quiet, no restart, no matter how long
        d = decide_heal(d.state, False, ServiceState(active_state="unknown"), NOW + i * 300)
        assert (d.action, d.quiet, d.reason) == ("none", True, "unit unknown")


def test_alive_after_down_recovers_even_without_mspt() -> None:
    st, _, msgs = health.evaluate(State(status="down", down_alerted=True), True, None, None, GcStats(), CFG)
    assert st.status == "ok" and len(msgs) == 1 and "The Minecraft server is back" in msgs[0]
    assert "MSPT n/a" in msgs[0]


def test_restore_in_progress_is_left_alone() -> None:
    d = decide_heal(
        State(status="down"), False, ServiceState(active_state="inactive", restore_running=True), NOW
    )
    assert (d.action, d.quiet, d.reason) == ("none", True, "restore running")


def test_crash_loop_is_reported_not_hidden_as_starting() -> None:
    looping = [
        ServiceState(active_state="activating", sub_state="auto-restart", n_restarts=n) for n in (3, 9, 15)
    ]
    d = decide_heal(State(status="down"), False, looping[0], NOW)  # first sight: baseline only
    assert (d.action, d.quiet) == ("none", True)
    d = decide_heal(d.state, False, looping[1], NOW + 300)
    assert d.action == "none" and not d.quiet and "keeps crashing" in d.alerts[0] and "6 times" in d.alerts[0]
    d = decide_heal(d.state, False, looping[2], NOW + 600)
    assert d.alerts == [] and not d.quiet  # alerted once; the heartbeat keeps failing
    _st, _msgs, hb = apply_heal(State(status="down"), d, [], "fail", False)
    assert hb == "fail"
    assert decide_heal(d.state, True, ServiceState(), NOW + 900).state.crash_loop_alerted is False


def test_start_limit_hit_is_a_crash_loop() -> None:
    svc = ServiceState(active_state="failed", result="start-limit-hit", n_restarts=5)
    d = decide_heal(State(status="down"), False, svc, NOW)
    assert d.action == "none" and "start limit" in d.alerts[0]


def test_service_state_reads_restarts_and_markers(monkeypatch, tmp_path) -> None:
    import subprocess

    out = "ActiveState=activating\nSubState=auto-restart\nResult=exit-code\nNRestarts=7\nActiveEnterTimestamp=\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, out, ""))
    (tmp_path / "admin-stopped").write_text("{}")
    monkeypatch.setattr(health, "ADMIN_STOPPED", tmp_path / "admin-stopped")
    monkeypatch.setattr(health, "maintenance_active", lambda now: None)
    monkeypatch.setattr(health, "restore_running", lambda: True)
    svc = health.service_state(NOW)
    assert (svc.sub_state, svc.result, svc.n_restarts) == ("auto-restart", "exit-code", 7)
    assert svc.admin_stopped and svc.restore_running and svc.active_for_s is None


def test_service_state_counts_a_fenced_box_as_admin_stopped(monkeypatch, tmp_path) -> None:
    """P12 review: self-heal never starts a box `mcctl migrate` fenced."""
    import subprocess

    import notify

    out = "ActiveState=inactive\nSubState=dead\nResult=success\nNRestarts=0\nActiveEnterTimestamp=\n"
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 0, out, ""))
    monkeypatch.setattr(health, "ADMIN_STOPPED", tmp_path / "admin-stopped")
    monkeypatch.setattr(notify, "FENCED_FILE", tmp_path / "fenced")
    monkeypatch.setattr(health, "maintenance_active", lambda now: None)
    monkeypatch.setattr(health, "restore_running", lambda: False)
    assert not health.service_state(NOW).admin_stopped
    (tmp_path / "fenced").write_text("{}")
    svc = health.service_state(NOW)
    assert svc.admin_stopped
    assert decide_heal(State(status="down"), False, svc, NOW).action == "none"


# --- P6 review: the watchdog's own freshness -----------------------------------------------------------


def test_stale_watchdog_cron_alerts_once_then_recovers() -> None:
    st, msgs = health.evaluate_watchdog(State(), True, {"last_run_age_s": 300})
    assert msgs == []
    st, msgs = health.evaluate_watchdog(st, True, {"last_run_age_s": 1800})
    assert len(msgs) == 1 and "has stopped checking" in msgs[0] and "30 min ago" in msgs[0]
    st, msgs = health.evaluate_watchdog(st, True, {"last_run_age_s": 2100})
    assert msgs == []
    st, msgs = health.evaluate_watchdog(st, True, {"last_run_age_s": 60})
    assert len(msgs) == 1 and "The outside watchdog is checking again" in msgs[0]
    assert health.evaluate_watchdog(State(), True, {"last_run_age_s": None})[1] == []  # never ran yet
    assert health.evaluate_watchdog(State(), False, None)[1] == []  # watchdog not configured


def test_unreachable_watchdog_alerts_after_three_checks() -> None:
    st = State()
    out = []
    for _ in range(4):
        st, msgs = health.evaluate_watchdog(st, True, None)
        out += msgs
    assert len(out) == 1 and "can't be reached from the server" in out[0]
    st, msgs = health.evaluate_watchdog(st, True, {})
    assert len(msgs) == 1 and "reachable again" in msgs[0]


def test_main_reports_admin_stopped_and_alerts_a_stale_cron(monkeypatch, tmp_path) -> None:
    hb: list[tuple] = []
    posted: list[str] = []
    healed: list[str] = []
    monkeypatch.setenv("STATE_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("WATCHDOG_URL", "https://hb.mc.tests.mcctl.internal")
    monkeypatch.setenv("HEARTBEAT_TOKEN", "t")
    monkeypatch.setattr(health, "probe_rcon", lambda: health.RconProbe(False))
    monkeypatch.setattr(
        health, "service_state", lambda now: ServiceState(active_state="inactive", admin_stopped=True)
    )
    monkeypatch.setattr(health, "read_gc_log", lambda d: [])
    monkeypatch.setattr(health, "read_host_stats", lambda d: health.HostStats())
    monkeypatch.setattr(health, "heal", lambda a, r: healed.append(a) or True)

    def reply(check, status, message, admin_stopped):
        hb.append((check, status, admin_stopped))
        return {"ok": True, "last_run_age_s": 3600}

    monkeypatch.setattr(health, "heartbeat_reply", reply)
    monkeypatch.setattr(health, "discord", posted.append)
    assert health.main() == 0
    assert hb == [("health", "", True)]
    assert healed == []
    assert len(posted) == 1 and "has stopped checking" in posted[0] and "60 min ago" in posted[0]


# --- Chunky pre-generation (one world at a time, only with nobody online) ------------------------------------


class FakeChunky:
    """The replies of Chunky 1.5 (lang/en.json) and `list`, for health.pregen_step."""

    def __init__(self) -> None:
        self.players = 0
        self.running: str | None = None
        self.saved: str | None = None  # a paused (or restart-interrupted) task
        self.selected = "world"
        self.log: list[str] = []

    def __call__(self, cmd: str) -> str:
        self.log.append(cmd)
        if cmd == "list":
            return f"There are {self.players} of a max of 20 players online:"
        if cmd == "chunky progress":
            return (
                f"[Chunky] Task running for {self.running}. Processed: 10 chunks (1.00%)"
                if self.running
                else ("[Chunky] No tasks running.")
            )
        if cmd.startswith("chunky world "):
            self.selected = cmd.split()[-1]
            return f"[Chunky] World changed to {self.selected}."
        if cmd == "chunky worldborder":
            return f"[Chunky] Selection loaded from world border for {self.selected}."
        if cmd == "chunky start":
            self.running = self.selected
            return f"[Chunky] Task started in {self.selected} for the square region centered at 0, 0 with radius 5000."
        if cmd == "chunky pause":
            self.saved, self.running = self.running, None
            return f"[Chunky] Task paused for {self.saved}."
        if cmd == "chunky continue":
            if self.saved:
                self.running, self.saved = self.saved, None
                return f"[Chunky] Task continuing for {self.running}."
            return "[Chunky] No tasks to continue."
        raise AssertionError(cmd)


def test_pregen_runs_each_world_in_turn_only_when_empty() -> None:
    worlds = ["world", "world_nether", "world_the_end"]
    c, st = FakeChunky(), State()
    st, what = health.pregen_step(st, worlds, c)
    assert what == "started (world)" and c.running == "world" and "chunky worldborder" in c.log
    st, what = health.pregen_step(st, worlds, c)
    assert what == "running (world)"
    c.players = 2  # someone joins: pause
    st, what = health.pregen_step(st, worlds, c)
    assert what.startswith("paused") and c.running is None and st.pregen_paused
    st, what = health.pregen_step(st, worlds, c)
    assert what == "waiting for an empty server" and c.running is None
    c.players = 0  # empty again: continue the same world, not the next
    st, what = health.pregen_step(st, worlds, c)
    assert what == "continued (world)" and c.running == "world" and not st.pregen_paused
    c.running = None  # Chunky finished the overworld
    st, what = health.pregen_step(st, worlds, c)
    assert what == "started (world_nether)" and st.pregen_done == ["world"]
    c.running = None  # restart: Chunky resumes by itself (continue-on-restart); a saved task continues
    c.saved = "world_nether"
    st, what = health.pregen_step(st, worlds, c)
    assert what == "continued (world_nether)"
    c.running = None
    st, what = health.pregen_step(st, worlds, c)
    assert what == "started (world_the_end)"
    c.running = None
    st, what = health.pregen_step(st, worlds, c)
    assert what == "done" and st.pregen_done == worlds and st.pregen_current is None
    n = len(c.log)
    assert health.pregen_step(st, worlds, c)[1] == "done" and len(c.log) == n  # nothing sent any more
    assert State(**json.loads(json.dumps(asdict(st)))) == st  # survives the state file


def test_pregen_never_generates_when_the_player_count_is_unknown() -> None:
    c = FakeChunky()
    c.players = 0
    unknown = lambda cmd: "garbage" if cmd == "list" else c(cmd)  # noqa: E731
    _, what = health.pregen_step(State(), ["world"], unknown)
    assert what == "waiting for an empty server" and "chunky start" not in c.log
    assert health.pregen_step(State(), [], c) == (State(), "")


def test_pregen_worlds_env(monkeypatch) -> None:
    monkeypatch.setenv("PREGEN_WORLDS", "world,world_the_end,bad;rm,")
    assert health.pregen_worlds() == ["world", "world_the_end"]
    monkeypatch.setenv("PREGEN_WORLDS", "")
    assert health.pregen_worlds() == []


# --- v1.4: progress, the done notice, the backup hold, frontier expansion -------------------------------------

# `chunky progress` with two running tasks, as Chunky 1.5.3 sends it over RCON (prefix, one message per task,
# GenerationTask$Progress.sendUpdate's task_update; the percentage is String.format("%.2f") in the JVM's locale).
PROGRESS_TWO = (
    "§b[Chunky]§r Task running for world. Processed: 123456 chunks (63.27%), ETA: 1:02:03, Rate: 85.2 cps, "
    "Current: 12, -40\n"
    "§b[Chunky]§r Task running for world_the_end. Processed: 99 chunks (0,50%), ETA: 0:10:00, Rate: 10.0 cps, "
    "Current: 1, 2"
)


def test_parse_chunky_progress_reads_every_running_task() -> None:
    assert health.parse_chunky_progress(PROGRESS_TWO) == {
        "world": (63.27, 123456), "world_the_end": (0.5, 99)
    }  # fmt: skip
    assert health.parse_chunky_progress("[Chunky] No tasks running.") == {}
    # the task_done line (a task finishing as it is asked) is not a running task
    done = "[Chunky] Task finished for world. Processed: 10 chunks (100.00%), Total time: 0:01:02"
    assert health.parse_chunky_progress(done) == {}


def test_pregen_records_progress_and_summarises_it() -> None:
    worlds = ["world", "world_nether", "world_the_end"]
    c, st = FakeChunky(), State()
    st, _ = health.pregen_step(st, worlds, c)  # started (world)
    st, what = health.pregen_step(st, worlds, c)
    assert what == "running (world)" and st.pregen_progress == {"world": {"pct": 1.0, "chunks": 10}}
    assert health.pregen_summary(st, worlds, None) == "world 1% · world_nether queued · world_the_end queued"
    c.players = 1
    st, _ = health.pregen_step(st, worlds, c)
    assert health.pregen_summary(st, worlds, None).startswith("world 1% (paused) · ")
    assert health.pregen_summary(st, worlds, None, "backup").endswith("(held: backup)")
    c.players, c.running = 0, None
    c.saved = None  # finished while paused
    st, _ = health.pregen_step(st, worlds, c)
    assert st.pregen_done == ["world"] and st.pregen_progress["world"]["pct"] == 100.0
    want = "world done · world_nether started · world_the_end queued"  # no percentage seen yet
    assert health.pregen_summary(st, worlds, None) == want
    st.pregen_done = list(worlds)
    assert health.pregen_summary(st, worlds, None) == "done"
    assert health.pregen_summary(State(), [], None) == "off"


def test_pregen_paused_flag_clears_when_chunky_resumed_by_itself() -> None:
    """A restart (continue-on-restart) resumes a task paused for players: the state follows what runs."""
    c = FakeChunky()
    st = State(pregen_current="world", pregen_paused=True)
    c.running = "world"
    st, what = health.pregen_step(st, ["world"], c)
    assert what == "running (world)" and not st.pregen_paused and health.pregen_task(st) == "world"


def test_pregen_done_notice_is_sent_once() -> None:
    worlds = ["world", "world_nether"]
    before = State(pregen_done=["world"], pregen_current="world_nether",
                   pregen_progress={"world": {"pct": 100.0, "chunks": 4000}})  # fmt: skip
    after = State(**asdict(before))
    after.pregen_done, after.pregen_current = ["world", "world_nether"], None
    note = health.pregen_notice(before, after, worlds, frontier=True)
    assert note and note.startswith("\u2139\ufe0f **Chunk pre-generation is finished**")
    assert "chunks processed at the last look: world 4000" in note and "frontier expansion" in note
    assert health.pregen_notice(after, after, worlds, frontier=True) is None  # already done: never again
    assert health.pregen_notice(before, before, worlds, frontier=False) is None
    assert health.pregen_notice(State(), State(), [], frontier=True) is None  # nothing bordered: no note


def test_run_pregen_sends_the_notice_once_and_honours_the_backup_hold(monkeypatch) -> None:
    monkeypatch.setenv("PREGEN_WORLDS", "world")
    monkeypatch.delenv("PREGEN_FRONTIER", raising=False)
    monkeypatch.setattr(health, "admin_stopped", lambda path=None: False)
    monkeypatch.setattr(health, "maintenance_active", lambda now=None: None)
    monkeypatch.setattr(health, "active_world_id", lambda: None)
    posted: list[str] = []
    monkeypatch.setattr(health, "discord", posted.append)
    c = FakeChunky()

    class Conn:
        def __enter__(self):
            return type("R", (), {"command": staticmethod(c)})()

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(health.mc_rcon, "connect", lambda *a, **k: Conn())
    held: list[str | None] = ["backup"]
    monkeypatch.setattr(health, "pregen_held", lambda now=None: held[0])
    st = State(pregen_current="world")
    assert health.run_pregen(st, True) is st and c.log == []  # held: Chunky isn't even asked
    held[0] = None
    st = health.run_pregen(st, True)  # nothing to continue: world done -> the note
    assert st.pregen_done == ["world"] and len(posted) == 1 and "pre-generation is finished" in posted[0]
    st = health.run_pregen(st, True)
    assert len(posted) == 1  # once


class FrontierChunky(FakeChunky):
    """FakeChunky plus `chunky start W square X Z R` and `chunky confirm` (format_start_confirm)."""

    def __init__(self) -> None:
        super().__init__()
        self.stale: str | None = None  # a saved, unfinished task: `chunky start` asks for a confirmation
        self.starts: list[tuple[str, int]] = []
        self.pending: tuple[str, int] | None = None

    def __call__(self, cmd: str) -> str:
        parts = cmd.split()
        if parts[:2] == ["chunky", "start"] and len(parts) == 7:
            world, shape, x, z, radius = parts[2:]
            assert (shape, x, z) == ("square", "0", "0")
            self.log.append(cmd)
            if self.stale == world:
                self.pending = (world, int(radius))
                return ("[Chunky] A task was already started for this world. To continue running it, type "
                        "'/chunky continue'. To start a new task, type '/chunky confirm'.")  # fmt: skip
            return self._start(world, int(radius))
        if cmd == "chunky cancel":  # format_cancel_confirm: always asks first
            self.log.append(cmd)
            self.pending = ("cancel", 0)
            return ("[Chunky] Cancelled tasks cannot be continued. If you are sure you want to cancel, type "
                    "'/chunky confirm'.")  # fmt: skip
        if cmd == "chunky confirm":
            self.log.append(cmd)
            assert self.pending
            pending, self.pending = self.pending, None
            if pending[0] == "cancel":
                self.running = self.saved = self.stale = None
                return "[Chunky] Cancelling all tasks."
            self.stale = None
            return self._start(*pending)
        return super().__call__(cmd)

    def _start(self, world: str, radius: int) -> str:
        self.running = world
        self.starts.append((world, radius))
        return (
            f"[Chunky] Task started in {world} for the square region centered at 0, 0 with radius {radius}."
        )


END = health.FrontierDim("world_the_end", "the_end", None)
OVER = health.FrontierDim("world", "overworld", 5000)


def frontier(reach: dict[str, int | None], **kw) -> health.Frontier:
    return health.Frontier([OVER, END], explored=lambda d: reach.get(d.world), **kw)


def test_frontier_plan_step_threshold_and_caps() -> None:
    worlds = ["world"]
    done = State(pregen_done=["world"])
    # the end has no border: farthest chunk 1000 + margin 1024 -> 2032 (whole chunks), from nothing
    assert health.frontier_plan(done, worlds, frontier({"world_the_end": 1000})) == (END, 2032)
    # less than a step beyond what was generated: nothing (no new task every 5 minutes)
    st = State(pregen_done=["world"], pregen_frontier={"world_the_end": 2032})
    assert health.frontier_plan(st, worlds, frontier({"world_the_end": 1200})) is None
    assert health.frontier_plan(st, worlds, frontier({"world_the_end": 1264})) == (END, 2288)  # +256
    # max_radius caps an unbordered dimension (and a capped target is worth finishing, step or not)
    st = State(pregen_done=["world"], pregen_frontier={"world_the_end": 9900})
    assert health.frontier_plan(st, worlds, frontier({"world_the_end": 9500})) == (END, 10000)
    st.pregen_frontier["world_the_end"] = 10000
    assert health.frontier_plan(st, worlds, frontier({"world_the_end": 20000})) is None
    assert health.frontier_plan(done, worlds, frontier({"world_the_end": 9000}, max_radius=4000)) == (
        END,
        4000,
    )
    # a bordered dimension whose pass is done is generated up to its border: never past it
    assert health.frontier_plan(done, worlds, frontier({"world": 4990})) is None
    # ... and waits for its bordered pass
    assert health.frontier_plan(State(), worlds, frontier({"world": 100})) is None
    # a border enlarged after the pass: the new ring is grown around players, capped at the new border
    big = health.Frontier([health.FrontierDim("world", "overworld", 8000)], explored=lambda d: 5000)
    st = State(pregen_done=["world"], pregen_frontier={"world": 5000})
    assert health.frontier_plan(st, worlds, big) == (big.dims[0], 6032)
    # unknown reach (no region folder): skipped; the biggest gap wins
    assert health.frontier_plan(done, worlds, frontier({})) is None
    two = health.Frontier([health.FrontierDim("world_nether", "the_nether", None), END],
                          explored=lambda d: {"world_nether": 100, "world_the_end": 3000}[d.world])  # fmt: skip
    assert health.frontier_plan(State(), [], two) == (END, 4032)


def test_frontier_grows_only_while_idle_after_the_bordered_pass() -> None:
    worlds = ["world"]
    reach = {"world_the_end": 1000}
    f = frontier(reach)
    c, st = FrontierChunky(), State()
    st, what = health.pregen_step(st, worlds, c, frontier=f)
    assert what == "started (world)" and c.starts == []  # the bordered pass first
    c.running = None
    st, what = health.pregen_step(st, worlds, c, frontier=f)  # world done -> the end's frontier
    assert what == "frontier started (world_the_end to radius 2032)" and c.starts == [("world_the_end", 2032)]
    assert st.pregen_done == ["world"] and st.pregen_frontier_current == "world_the_end"
    st, what = health.pregen_step(st, worlds, c, frontier=f)
    assert what == "running (frontier world_the_end)" and st.pregen_frontier_pct == 1.0
    assert health.pregen_summary(st, worlds, f) == "done · frontier world_the_end to 2032 1%"
    c.players = 1  # a player joins: paused, and nothing new starts while anyone is online
    st, what = health.pregen_step(st, worlds, c, frontier=f)
    assert what == "paused (frontier world_the_end: players online)" and c.running is None
    c.saved = None  # (the ring finished as it was paused)
    reach["world_the_end"] = 5000
    st, what = health.pregen_step(st, worlds, c, frontier=f)
    assert what == "waiting for an empty server" and len(c.starts) == 1
    c.players = 0
    st, what = health.pregen_step(st, worlds, c, frontier=f)  # ring done, players went further: the next
    assert (
        st.pregen_frontier == {"world_the_end": 2032}
        and what == "frontier started (world_the_end to radius 6032)"
    )
    c.running = None
    reach["world_the_end"] = 5100  # less than a step: rest
    st, what = health.pregen_step(st, worlds, c, frontier=f)
    assert what == "done (frontier: nothing to grow)" and st.pregen_frontier == {"world_the_end": 6032}
    assert health.pregen_summary(st, worlds, f) == "done · frontier world_the_end 6032"
    # a full disk holds new rings
    reach["world_the_end"] = 9000
    held = frontier(reach, allowed=False)
    st, what = health.pregen_step(st, worlds, c, frontier=held)
    assert "frontier held" in what and len(c.starts) == 3 - 1


def test_frontier_replaces_a_stale_saved_task_with_confirm() -> None:
    c, st = FrontierChunky(), State()
    c.stale = "world_the_end"
    st, what = health.pregen_step(st, [], c, frontier=frontier({"world_the_end": 0}))
    assert what == "frontier started (world_the_end to radius 1024)" and c.log[-1] == "chunky confirm"


def test_world_switch_resets_the_frontier() -> None:
    c = FrontierChunky()
    st = State(pregen_for="a@1", pregen_done=["world"], pregen_frontier={"world_the_end": 4000},
               pregen_frontier_current="world_the_end", pregen_progress={"world": {"pct": 100.0}})  # fmt: skip
    st, _ = health.pregen_step(st, ["world"], c, world_id="b@2", frontier=frontier({}))
    assert c.log[:2] == ["chunky cancel", "chunky confirm"]  # Chunky asks before it cancels anything
    assert st.pregen_frontier == {} and st.pregen_progress == {}
    assert st.pregen_current == "world" and st.pregen_frontier_current is None


def _region(path: Path, chunks: list[tuple[int, int]]) -> None:
    header = bytearray(health.REGION_HEADER)
    for lx, lz in chunks:
        i = 4 * (lx + 32 * lz)
        header[i : i + 4] = b"\x00\x00\x02\x01"
    path.write_bytes(bytes(header) + b"\x00" * 4096)


def test_explored_radius_from_region_names_and_headers(tmp_path) -> None:
    d = tmp_path / "region"
    d.mkdir()
    _region(d / "r.0.0.mca", [(0, 0), (5, 3)])  # chunk 5,3: edge at 96 blocks
    _region(d / "r.-2.0.mca", [(31, 0), (31, 2)])  # chunk -33,0: edge at 528 blocks
    _region(d / "r.3.0.mca", [])  # the outermost file has no chunk in it (e.g. emptied): ignored
    (d / "r.9.9.mca.tmp").write_bytes(b"x")  # not a region file
    reads: list[str] = []

    def read(p: Path) -> bytes:
        reads.append(p.name)
        return health.read_region_header(p)

    assert health.explored_radius(d, read) == 528
    # outermost first, and r.0.0 (no chunk of it can reach past 512 blocks) is never opened
    assert reads == ["r.3.0.mca", "r.-2.0.mca"]
    assert health.explored_radius(tmp_path / "missing") is None
    assert health.explored_radius(None) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert health.explored_radius(empty) == 0
    assert (
        health.chunk_reach(0, 0) == 16
        and health.chunk_reach(-1, 0) == 16
        and health.chunk_reach(-30, 7) == 480
    )


def test_frontier_env_and_region_dirs(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv(
        "PREGEN_FRONTIER", "world:overworld:5000,world_the_end:the_end:,bad:x:1,w;rm:overworld:"
    )
    monkeypatch.setenv("PREGEN_MARGIN", "512")
    monkeypatch.setenv("PREGEN_MAX_RADIUS", "3000")
    f = health.frontier_env(server_dir=tmp_path)
    assert f is not None and f.dims == [OVER, END] and (f.margin, f.max_radius) == (512, 3000)
    (tmp_path / "world_the_end" / "DIM1" / "region").mkdir(parents=True)
    _region(tmp_path / "world_the_end" / "DIM1" / "region" / "r.1.0.mca", [(0, 0)])
    assert f.explored(END) == 32 * 16 + 16 and f.explored(OVER) is None
    nested = tmp_path / "world" / "dimensions" / "minecraft" / "overworld" / "region"
    nested.mkdir(parents=True)
    assert health.region_dir(tmp_path, OVER) == nested
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (tmp_path / "world_nether").mkdir()
    (tmp_path / "world_nether" / "DIM-1").symlink_to(elsewhere, target_is_directory=True)
    (elsewhere / "region").mkdir()
    nether = health.FrontierDim("world_nether", "the_nether")
    assert health.region_dir(tmp_path, nether) is None  # a planted symlink: never listed
    monkeypatch.setenv("PREGEN_FRONTIER", "")
    assert health.frontier_env() is None


def test_main_writes_the_status_line(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("PREGEN_WORLDS", "world,world_nether")
    monkeypatch.delenv("PREGEN_FRONTIER", raising=False)
    monkeypatch.setattr(health, "pregen_held", lambda now=None: None)
    st = State(pregen_done=["world"], pregen_current="world_nether",
               pregen_progress={"world_nether": {"pct": 42.4, "chunks": 1}})  # fmt: skip
    health.write_pregen_status(tmp_path / "pregen.json", st, 5.0)
    assert json.loads((tmp_path / "pregen.json").read_text()) == {
        "line": "world done · world_nether 42%",
        "at": 5,
    }


# --- alerts: one template, one message per episode, reminders, recovery (fake clock) ---------------------------

G1_PAUSE = "[2026-11-02T18:00:00.000+0000][{u}s][info][gc] GC(9) Pause Young (Normal) (G1 Evacuation Pause) 3000M->1200M(6656M) {ms}ms\n"


def g1(uptime: float, ms: float) -> list[str]:
    return [G1_PAUSE.format(u=uptime, ms=ms)]


def test_gc_alerts_once_then_reminds_at_most_every_repeat_hours_then_recovers() -> None:
    cfg = CFG | {"gc_pause_ms": 50}
    st, sent, t = State(status="ok"), [], NOW
    ctx = lambda now: health.Ctx(now=now, players=0, pregen="world", repeat_s=6 * 3600)  # noqa: E731
    for i in range(80):  # a pause > 50 ms every 5 minutes for 6 h 40 min
        t = NOW + i * 300
        gc = health.parse_gc(g1(100.0 + i, 120.0 + (60 if i > 40 else 0)), st.last_gc_uptime, 50)
        st, _, msgs = health.evaluate(st, True, 20.0, 20.0, gc, cfg, ctx(t))
        sent += [(i, m) for m in msgs]
    assert [i for i, _ in sent] == [0, 72]  # the first, then one reminder 6 h later
    first, remind = sent[0][1], sent[1][1]
    assert first.startswith("\u26a0\ufe0f **Short freezes: Java memory management paused the server**")
    assert "Nobody is online right now." in first and "pre-generation is running (world)" in first
    assert (
        "Usually nothing: it clears when chunk pre-generation finishes" in first
        and "`mcctl config set jvm.gc zgc`" in first
    )
    assert "(~2 ticks)" in first
    assert "72 more occurrence(s)" in remind and "worse than last time" in remind
    # quiet for 30 min -> one recovered message with how long it lasted
    gc0 = GcStats(last_uptime=st.last_gc_uptime, collector="g1")
    st, _, m = health.evaluate(st, True, 20.0, 20.0, gc0, cfg, ctx(t + 600))
    assert m == []
    st, _, m = health.evaluate(st, True, 20.0, 20.0, gc0, cfg, ctx(t + 1800))
    assert len(m) == 1 and m[0].startswith("\u2705 **The short freezes") and "they went on for 7 h" in m[0]
    assert st.alerts == {}


def test_zgc_alerts_on_allocation_stalls_with_heap_advice() -> None:
    stall = [
        "[2026-11-02T18:00:00.000+0000][200.0s][info][gc       ] Allocation Stall (Server thread) 70.5ms\n"
    ]
    gc = health.parse_gc(stall, 0.0, 50)
    _st, _, msgs = health.evaluate(
        State(status="ok"), True, 20.0, 20.0, gc, CFG | {"gc_pause_ms": 50}, health.Ctx(now=NOW, players=3)
    )
    assert len(msgs) == 1 and "allocation stall" in msgs[0] and "jvm.heap_mb" in msgs[0]
    assert "3 player(s) online right now." in msgs[0] and "collector zgc" in msgs[0]


def test_gate_state_survives_a_restart(tmp_path) -> None:
    # the gate's state is part of state.json: a restarted health.py doesn't alert the same episode again
    gc = health.parse_gc(g1(100.0, 150.0), 0.0, 50)
    st, _, m1 = health.evaluate(
        State(status="ok"), True, 20.0, 20.0, gc, CFG | {"gc_pause_ms": 50}, health.Ctx(now=NOW)
    )
    p = tmp_path / "state.json"
    p.write_text(json.dumps(asdict(st)))
    gc2 = health.parse_gc(g1(101.0, 150.0), st.last_gc_uptime, 50)
    _, _, m2 = health.evaluate(
        health.load_state(p), True, 20.0, 20.0, gc2, CFG | {"gc_pause_ms": 50}, health.Ctx(now=NOW + 300)
    )
    assert len(m1) == 1 and m2 == []


def test_alert_template_limits_and_scrubbing() -> None:
    import alerts

    text = alerts.render(
        alerts.Alert("action", "x" * 50, impact="203.0.113.9 and 3ffe:ffff:1:2::1 " + "y" * 3000)
    )
    assert len(text) <= alerts.MAX_CHARS and "203.0.113.9" not in text and "3ffe:ffff" not in text
    assert alerts.render(alerts.Alert("info", "Hi")).endswith(
        "**What to do:** No action needed - informational."
    )


def test_backup_hold_stops_frontier_expansion_too(monkeypatch, tmp_path) -> None:
    """While backup.py holds pre-generation nothing starts, the frontier included; afterwards it grows."""
    import notify

    monkeypatch.setenv("PREGEN_WORLDS", "")
    monkeypatch.setenv("PREGEN_FRONTIER", "world_the_end:the_end:")
    monkeypatch.setattr(health.mc_rcon, "SERVER_DIR", tmp_path)
    (tmp_path / "world_the_end" / "DIM1" / "region").mkdir(parents=True)
    _region(tmp_path / "world_the_end" / "DIM1" / "region" / "r.0.0.mca", [(3, 0)])
    hold = tmp_path / "pregen-hold.json"
    monkeypatch.setattr(notify, "PREGEN_HOLD_FILE", hold)
    monkeypatch.setattr(health, "admin_stopped", lambda path=None: False)
    monkeypatch.setattr(health, "maintenance_active", lambda now=None: None)
    monkeypatch.setattr(health, "active_world_id", lambda: None)
    c = FrontierChunky()

    class Conn:
        def __enter__(self):
            return type("R", (), {"command": staticmethod(c)})()

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(health.mc_rcon, "connect", lambda *a, **k: Conn())
    notify.pregen_hold("backup", path=hold)
    st = health.run_pregen(State(), True)
    assert c.log == [] and st == State()
    notify.pregen_release(hold)
    st = health.run_pregen(st, True)
    assert c.starts == [("world_the_end", 64 + 1024)] and st.pregen_frontier_current == "world_the_end"
    assert health.run_pregen(st, True, disk_ok=False).pregen_frontier_current == "world_the_end"  # running


def test_turning_pregen_off_cancels_the_task_it_started(monkeypatch) -> None:
    monkeypatch.setenv("PREGEN_WORLDS", "")
    monkeypatch.setenv("PREGEN_FRONTIER", "")
    monkeypatch.setattr(health, "admin_stopped", lambda path=None: False)
    c = FrontierChunky()
    c.running = "world"

    class Conn:
        def __enter__(self):
            return type("R", (), {"command": staticmethod(c)})()

        def __exit__(self, *a):
            return None

    monkeypatch.setattr(health.mc_rcon, "connect", lambda *a, **k: Conn())
    st = health.run_pregen(State(pregen_current="world", pregen_done=["x"]), True)
    assert c.log == ["chunky cancel", "chunky confirm"] and c.running is None
    assert st.pregen_current is None and st.pregen_done == ["x"]
    c.log.clear()
    assert health.run_pregen(st, True) is st and c.log == []  # nothing of ours left: nothing sent
