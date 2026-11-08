"""P13 mc-agent telemetry (scripts/telemetry.py + mc_agent's player_message): snapshot hand-off, minute rows,
sessions, product overhead, segments (cap/rotation), the ack cursor, quiet-time backfill (outage -> backlog ->
quiet -> backfill without duplicates or gaps; a player joining stops it; a crash mid-upload resumes), the verdict
and message files for the plugin, and the agent-side message rate limits. No network: fake post/put."""

from __future__ import annotations

import gzip
import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import health
import mc_agent
import mc_rcon
import pytest
import telemetry
import yaml

ED = "3f2a0000-0000-4000-8000-000000000c41"
MIA = "4f2a0000-0000-4000-8000-000000001c41"
T0 = 1_793_000_000_000 // 60000 * 60000  # a minute boundary (ms)


def snap(seq: int, ts: int, players: list[dict[str, Any]] | None = None, **srv: Any) -> dict[str, Any]:
    base = {"tps": 20.0, "ticks": 200, "mean": 10.0, "p50": 9.0, "p95": 15.0, "p99": 22.0, "p999": 30.0, "max": 31.0,
            "self_p99_us": 5.0, "self_max_us": 30.0, "budget_skips": 0, "lost_windows": 0, "heap_used_mb": 900,
            "heap_max_mb": 4096, "spikes": [], "worlds": [{"n": "world", "chunks": 400, "entities": 300, "tiles": 20,
                                                           "ticking": 10, "players": len(players or [])}]}  # fmt: skip
    base |= srv
    return {"v": 1, "seq": seq, "ts": ts, "win_ms": 10000, "srv": base, "pl": players or []}


def pl(u: str = ED, n: str = "Em_K", **kw: Any) -> dict[str, Any]:
    return {"u": u, "n": n, "ping": 40, "brand": "vanilla", "view": 10, "send": 8, "loc": "en_gb", "w": "world",
            "cx": 3, "cz": -7, "ent": 20, "glide": False, "tips": True, "join": T0 - 600000} | kw  # fmt: skip


def write_snap(d: Path, s: dict[str, Any]) -> None:
    (d / f"snap-{s['seq']}.json").write_text(json.dumps(s))


# --- snapshots ------------------------------------------------------------------------------------------------


def test_parse_snapshot_keeps_known_fields_only_and_never_an_ip() -> None:
    s = snap(1, T0, [pl(ip="203.0.113.9", brand="evil\tbrand\n" + "x" * 99)], extra="no")
    got = telemetry.parse_snapshot(json.dumps(s).encode())
    assert got is not None
    assert "extra" not in got["srv"] and "ip" not in got["pl"][0]
    assert got["pl"][0]["brand"] == ("evil brand " + "x" * 99)[:32]
    assert telemetry.parse_snapshot(b"{nope") is None
    assert telemetry.parse_snapshot(json.dumps({"v": 2, "ts": 1}).encode()) is None
    bad = snap(1, T0, [{"u": "not-a-uuid", "n": "x"}, pl(n="bad name!")])
    assert telemetry.parse_snapshot(json.dumps(bad).encode())["pl"] == []


def test_non_finite_numbers_in_a_snapshot_never_stall_the_reader(tmp_path: Path) -> None:
    """v1.4 fix (Python and Rust, rust/smallworld-agent/src/telemetry/snapshot.rs `num`): NaN, Infinity or an int
    too big for a float (json.loads accepts all three) read as missing; `int()` of them used to raise, failing
    every round at that snapshot (the reader never moved past it)."""
    raw = (
        json.dumps(snap(1, T0, [pl()]))
        .replace('"ping": 40', '"ping": Infinity')
        .replace('"tps": 20.0', '"tps": NaN')
        .replace('"ticks": 200', '"ticks": ' + "9" * 400)
    )
    got = telemetry.parse_snapshot(raw.encode())
    assert got is not None
    assert got["pl"][0]["ping"] == 0 and got["srv"]["tps"] is None and got["srv"]["ticks"] is None
    assert telemetry._fps(float("inf")) is None and telemetry._num(10**400) is None
    (tmp_path / "snap-1.json").write_text(raw)
    write_snap(tmp_path, snap(2, T0 + 10000))
    assert [s["seq"] for s in telemetry.SnapshotReader(tmp_path).read()] == [1, 2]


def test_snapshot_reader_reads_new_files_once_and_follows_a_plugin_restart(tmp_path: Path) -> None:
    r = telemetry.SnapshotReader(tmp_path)
    for i in range(3):
        write_snap(tmp_path, snap(i, T0 + i * 10000))
    assert [s["seq"] for s in r.read()] == [0, 1, 2]
    assert r.read() == []
    write_snap(tmp_path, snap(3, T0 + 30000))
    assert [s["seq"] for s in r.read()] == [3]
    for p in tmp_path.iterdir():
        p.unlink()
    write_snap(tmp_path, snap(0, T0 + 60000))  # the plugin restarted: seq starts at 0 again, newer ts
    assert [s["ts"] for s in r.read()] == [T0 + 60000]
    (tmp_path / "snap-1.json").write_text("{half")  # a torn file can't happen (atomic rename), but is skipped
    assert r.read() == []


def plant(path: Path, kind: str, target: Path | None = None) -> None:
    """What a compromised JVM could leave in a directory it writes: a FIFO (blocks a plain open() forever) or
    a symlink to a file it can't read itself."""
    if kind == "fifo":
        os.mkfifo(path)
    else:
        path.symlink_to(target or "/etc/hostname")


def within(seconds: float, fn: Any) -> Any:
    """Runs fn in a thread; fails if it hasn't returned in time (a blocked open())."""
    out: list[Any] = []

    def run() -> None:
        try:
            out.append(fn())
        except BaseException as e:  # re-raised in the caller
            out.append(e)

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(seconds)
    assert not th.is_alive(), "blocked"
    if isinstance(out[0], BaseException):
        raise out[0]
    return out[0]


@pytest.mark.parametrize("kind", ["fifo", "symlink"])
def test_untrusted_files_never_block_and_never_follow_a_link(tmp_path: Path, kind: str) -> None:
    """P13 review L4: every file the agent reads from a minecraft-writable directory (snapshots, stats, the GC
    log, the server's JSON lists, server.properties) is opened O_NONBLOCK|O_NOFOLLOW and must be regular."""
    secret = tmp_path / "secret.json"
    secret.write_text('{"stats": {"minecraft:custom": {"minecraft:deaths": 7}}}')
    snaps = tmp_path / "snaps"
    snaps.mkdir()
    write_snap(snaps, snap(0, T0))
    plant(snaps / "snap-1.json", kind, secret)
    reader = telemetry.SnapshotReader(snaps)
    assert [s["seq"] for s in within(5, reader.read)] == [0]  # the planted file is skipped, the real one read
    plant(tmp_path / "stats.json", kind, secret)
    assert within(5, lambda: telemetry.read_stats(tmp_path / "stats.json")) is None
    logs = tmp_path / "server" / "logs"
    logs.mkdir(parents=True)
    plant(logs / "gc.log", kind, secret)
    assert within(5, lambda: health.read_gc_log(tmp_path / "server")) == []
    plant(tmp_path / "server" / "whitelist.json", kind, tmp_path / "list.json")
    (tmp_path / "list.json").write_text('[{"name": "x", "uuid": "y"}]')
    assert within(5, lambda: mc_agent.read_json_list(tmp_path / "server" / "whitelist.json")) == []
    plant(tmp_path / "server" / "server.properties", kind)
    with pytest.raises(OSError):
        within(5, lambda: mc_rcon.read_properties(tmp_path / "server" / "server.properties"))


def test_read_untrusted_caps_the_size_and_reads_a_log_tail(tmp_path: Path) -> None:
    f = tmp_path / "gc.log"
    f.write_bytes(b"a" * 100 + b"\nlast line\n")
    assert mc_rcon.read_untrusted(f, 10) == b"a" * 10
    assert mc_rcon.read_untrusted(f, 11, tail=True) == b"\nlast line\n"
    assert mc_rcon.read_untrusted(f) == f.read_bytes()


def test_a_player_message_never_waits_for_the_telemetry_threads_http(tmp_path: Path) -> None:
    """P13 review L4: the telemetry thread may sit in a 20 s HTTP call (live row); a signed player_message
    (handled inline in the WebSocket loop) must not wait for it."""
    w, clock = FakeWorker(), [T0 / 1000]
    t = make(tmp_path, w, clock)
    run_minutes(t, clock, T0, 1, [pl()])
    entered, release = threading.Event(), threading.Event()

    def slow_post(path: str, body: Any) -> dict[str, Any]:
        entered.set()
        release.wait(10)
        return {"ok": True}

    t.post = slow_post  # type: ignore[method-assign]
    write_snap(t.cfg.snap_dir, snap(t.reader.last_seq + 1, T0 + 60000, [pl()]))  # closes the minute: ships it
    shipping = threading.Thread(target=t.run, daemon=True)  # the agent's real telemetry loop
    shipping.start()
    assert entered.wait(5)
    clock[0] += 120
    ex = mc_agent.Executor(lambda: {}, telemetry_=t)
    msg = {"to": ED, "kind": "lag", "text": "hi", "id": "lag-0123456789"}
    try:
        out = within(2, lambda: ex.run({"action": "player_message", "args": msg}))
    finally:
        t.stop.set()
        release.set()
        shipping.join(10)
    assert out == "queued for Em_K"


# --- minute rows -------------------------------------------------------------------------------------------------


def test_summaries_are_conservative_for_tail_percentiles() -> None:
    snaps = [telemetry.parse_snapshot(json.dumps(s).encode()) for s in [
        snap(0, T0, [pl(ping=40)], p99=20.0, max=25.0, mean=10.0),
        snap(1, T0 + 10000, [pl(ping=80, ent=500)], p99=70.0, max=140.0, mean=12.0, spikes=[[T0 + 12000, 140.0]]),
        snap(2, T0 + 20000, [pl(ping=60)], p99=22.0, max=30.0, mean=11.0, tps=19.4),
    ]]  # fmt: skip
    srv = telemetry.summarize_server(snaps)  # type: ignore[arg-type]
    assert srv["p99"] == 70.0 and srv["max"] == 140.0 and srv["tps"] == 19.4
    assert srv["mean"] == 11.0 and srv["ticks"] == 600 and srv["spikes"] == 1 and srv["samples"] == 3
    p = telemetry.summarize_players(snaps)[0]  # type: ignore[arg-type]
    assert (p["ping"], p["ping_max"], p["pings"], p["ent"]) == (60, 80, [40, 80, 60], 500)
    lag = telemetry.lag_events(snaps, ["backup"])  # type: ignore[arg-type]
    assert lag[0]["ms"] == 140.0 and lag[0]["ctx"]["running"] == ["backup"]
    assert lag[0]["ctx"]["near"][0]["n"] == "Em_K" and lag[0]["ctx"]["near"][0]["ent"] == 500


def test_sessions_from_consecutive_snapshots() -> None:
    s = telemetry.Sessions()
    a = snap(0, T0, [pl()])
    b = snap(1, T0 + 10000, [pl(), pl(MIA, "MiaBuilds", join=T0 + 5000)])
    c = snap(2, T0 + 20000, [pl(MIA, "MiaBuilds", join=T0 + 5000)])
    ev = s.feed([a, b, c])
    assert [(e["k"], e["n"]) for e in ev] == [("join", "Em_K"), ("join", "MiaBuilds"), ("leave", "Em_K")]
    assert ev[0]["join"] == T0 - 600000  # the plugin's join time: the Worker's session key
    assert ev[2]["join"] == T0 - 600000 and ev[2]["t"] == T0 + 20000


def test_sessions_never_span_a_server_down_period() -> None:
    """P13 review L6: the server stops with the admin online and comes back two hours later. His session ends when he
    was last seen, not when the server is back; reconnecting after a quick restart (a new join time, no gap)
    closes the old session and opens a new one."""
    s = telemetry.Sessions()
    ev = s.feed([snap(0, T0, [pl()]), snap(1, T0 + 10000, [pl()])])
    assert [e["k"] for e in ev] == ["join"]
    back = T0 + 2 * 3600000
    ev = s.feed([snap(0, back, [])])  # the plugin restarted: nobody online yet
    assert [(e["k"], e["t"]) for e in ev] == [("leave", T0 + 10000)]
    ev = s.feed([snap(1, back + 10000, [pl(join=back + 5000)])])
    assert [(e["k"], e["join"]) for e in ev] == [("join", back + 5000)]
    # a quick restart: still online by UUID, but a new join time -> old session closed when last seen
    ev = s.feed([snap(2, back + 20000, [pl(join=back + 15000)])])
    assert [(e["k"], e["join"], e["t"]) for e in ev] == [
        ("leave", back + 5000, back + 10000),
        ("join", back + 15000, back + 15000),
    ]
    # without a gap a leave is still the first snapshot without the player (as before)
    ev = s.feed([snap(3, back + 30000, [])])
    assert [(e["k"], e["t"]) for e in ev] == [("leave", back + 30000)]


# --- overhead -------------------------------------------------------------------------------------------------


def test_overhead_from_cgroup_counters_and_systemd_consumed_lines() -> None:
    show = ("Id=minecraft.service\nCPUUsageNSec=1000000000\nMemoryCurrent=6000000000\nMemoryPeak=6100000000\n"
            "IOReadBytes=1048576\nIOWriteBytes=0\n\nId=mc-agent.service\nCPUUsageNSec=50000000\nMemoryCurrent=25165824\n"
            "MemoryPeak=[not set]\nIOReadBytes=0\nIOWriteBytes=2048\n")  # fmt: skip
    o = telemetry.Overhead()
    first = telemetry.parse_show(show)
    assert first["mc-agent.service"]["MemoryPeak"] is None
    o.sample(first, 1000.0)
    later = telemetry.parse_show(show.replace("CPUUsageNSec=1000000000", "CPUUsageNSec=31000000000")
                                 .replace("CPUUsageNSec=50000000", "CPUUsageNSec=110000000"))  # fmt: skip
    o.observe_journal([
        {"ident": "systemd", "sd_unit": "mc-backup.service", "msg": "mc-backup.service: Consumed 1min 2.500s CPU time, 88.4M memory peak."},
        {"ident": "systemd", "sd_unit": "mc-health.service", "msg": "mc-health.service: Consumed 312ms CPU time."},
        {"ident": "sshd", "sd_unit": "mc-health.service", "msg": "Consumed 9s CPU time"},
    ])  # fmt: skip
    out = o.sample(later, 1060.0)
    assert out["minecraft.service"]["cpu_pct"] == 50.0  # 30 s of CPU in 60 s
    assert out["mc-agent.service"]["cpu_pct"] == 0.1 and out["mc-agent.service"]["mem_mb"] == 24.0
    assert out["mc-backup.service"] == {"cpu_ms": 62500.0, "runs": 1, "mem_peak_mb": 88.4}
    assert out["mc-health.service"]["cpu_ms"] == 312.0
    assert "mc-backup.service" not in o.sample(later, 1120.0)  # counted once
    assert telemetry.consumed_cpu_ms("nothing") is None


def test_stats_reader_reads_vanilla_stats_when_they_change(tmp_path: Path) -> None:
    f = tmp_path / f"{ED}.json"
    f.write_text(json.dumps({"stats": {"minecraft:custom": {"minecraft:play_time": 72000, "minecraft:deaths": 3,
                                                            "minecraft:walk_one_cm": 123456}}}))  # fmt: skip
    r = telemetry.StatsReader(tmp_path)
    got = r.changed([ED, MIA, "../etc"])
    assert got[ED]["play_ticks"] == 72000 and got[ED]["deaths"] == 3 and got[ED]["walk_cm"] == 123456
    assert r.changed([ED]) == {}


# --- segments, ack cursor, backfill --------------------------------------------------------------------------


def rows(n: int, start: int = 0, src: str = "abcd0123") -> list[dict[str, Any]]:
    return [{"v": 1, "id": f"{src}:{i}", "src": src, "seq": i, "ts": T0 + i * 60000, "srv": {}, "pl": []}
            for i in range(start, start + n)]  # fmt: skip


def test_segment_written_in_one_go_and_the_ack_cursor(tmp_path: Path) -> None:
    seg = telemetry.Segments(tmp_path)
    rs = rows(5)
    name = seg.write(rs, {rs[0]["id"], rs[1]["id"]}, "abcd0123")
    assert name and telemetry.SEGMENT_NAME.match(name)
    assert telemetry.read_segment(tmp_path / name) == rs
    st = seg.load()[name]
    assert st == {"todo": [2, 3, 4], "r2": False, "rows": 5}
    seg.mark(name, rows=[2, 3])
    seg.mark(name, r2=True)
    assert seg.load()[name]["todo"] == [4]
    assert seg.backlog()["rows"] == 1 and seg.backlog()["segments"] == 1
    seg.mark(name, rows=[4])
    assert seg.pending() == []
    assert not list(tmp_path.glob(".*.tmp"))


def test_segments_rotate_by_age_and_disk_cap(tmp_path: Path) -> None:
    now = [time.time()]
    seg = telemetry.Segments(tmp_path, days=30, cap_mb=1, clock=lambda: now[0])
    names = [seg.write(rows(1, i), set(), "abcd0123") for i in range(3)]
    old = tmp_path / names[0]  # type: ignore[operator]
    os.utime(old, (now[0] - 31 * 86400, now[0] - 31 * 86400))
    assert seg.prune() == [names[0]]
    assert names[0] not in seg.load()
    big = os.urandom(600_000)  # incompressible: two of these exceed the 1 MB cap
    for i in (10, 11):
        name = seg.write(
            [{"id": f"abcd0123:{i}", "seq": i, "ts": T0 + i * 60000, "blob": big.hex()}], set(), "abcd0123"
        )
        assert name
    left = sorted(p.name for p in tmp_path.glob("*.ndjson.gz"))
    assert names[1] not in left and names[2] not in left  # oldest dropped first
    assert sum(p.stat().st_size for p in tmp_path.glob("*.ndjson.gz")) <= 1 << 20 or len(left) == 1


class FakeWorker:
    """Records what reaches it; can be 'down'. Upserts by id like the Worker (so duplicates are visible)."""

    def __init__(self) -> None:
        self.down = False
        self.rows: dict[str, int] = {}
        self.segments: list[str] = []
        self.requests = 0
        self.fail_after: int | None = None

    def post(self, path: str, body: Any) -> dict[str, Any]:
        self.requests += 1
        if self.down or (self.fail_after is not None and self.requests > self.fail_after):
            raise mc_agent.IngestError("down")
        for r in body["rows"]:
            self.rows[r["id"]] = self.rows.get(r["id"], 0) + 1
        return {"ok": True, "verdicts": {"server": {"verdict": "good", "headline": "Running smoothly", "reasons": [], "tips": []},
                                         "players": {ED: {"verdict": "fair", "headline": "Your connection", "reasons": ["Ping is a bit high (180 ms)"], "tips": ["Wired\thelps"]}}}}  # fmt: skip

    def put(self, name: str, data: bytes) -> None:
        if self.down:
            raise mc_agent.IngestError("down")
        assert data[:2] == b"\x1f\x8b"
        self.segments.append(name)


def make(tmp_path: Path, w: FakeWorker, clock: list[float]) -> telemetry.Telemetry:
    for d in ("snap", "verd", "seg", "state"):
        (tmp_path / d).mkdir(exist_ok=True)
    cfg = telemetry.Config(snap_dir=tmp_path / "snap", verdict_dir=tmp_path / "verd", segment_dir=tmp_path / "seg",
                           backfill_pause_s=0)  # fmt: skip
    t = telemetry.Telemetry(
        cfg, tmp_path / "state", post=w.post, put=w.put, clock=lambda: clock[0], stats=lambda: {}
    )
    t.stop.wait = lambda s: False  # type: ignore[method-assign]
    return t


def run_minutes(
    t: telemetry.Telemetry, clock: list[float], start: int, minutes: int, players: list[dict[str, Any]]
) -> None:
    """Six snapshots per minute, read as the agent would (clock in seconds)."""
    for m in range(minutes):
        for k in range(6):
            ts = start + m * 60000 + k * 10000
            write_snap(t.cfg.snap_dir, snap(t.reader.last_seq + 1, ts, players))
            clock[0] = ts / 1000 + 1
            t.poll()


def test_live_rows_verdicts_file_and_segments_when_the_worker_answers(tmp_path: Path) -> None:
    w, clock = FakeWorker(), [T0 / 1000]
    t = make(tmp_path, w, clock)
    run_minutes(t, clock, T0, 6, [pl()])
    assert len(w.rows) == 5 and all(v == 1 for v in w.rows.values())  # minute 6 is still open
    verdicts = (tmp_path / "verd" / "verdicts.tsv").read_text().splitlines()
    assert verdicts[0].startswith("mc-verdicts 1 ")
    assert verdicts[1] == "S\tgood\tRunning smoothly\t\t"
    assert verdicts[2] == f"P\t{ED}\tfair\tYour connection\tPing is a bit high (180 ms)\tWired helps"
    assert oct((tmp_path / "verd" / "verdicts.tsv").stat().st_mode & 0o777) == "0o640"
    assert len(w.segments) == 1 and t.segments.pending() == []
    first = json.loads(gzip.decompress((tmp_path / "seg" / w.segments[0]).read_bytes()).splitlines()[0])
    assert first["ts"] == T0 and len(first["raw"]) == 6 and first["ev"][0]["k"] == "join"
    assert first["id"].endswith(":0")


def test_outage_then_quiet_backfill_no_duplicates_no_gaps(tmp_path: Path) -> None:
    """Worker down for an hour -> rows pile up in segments -> it comes back while players are online (no
    backfill) -> they leave (quiet) -> backfill delivers every missing row exactly once, oldest first."""
    w, clock = FakeWorker(), [T0 / 1000]
    t = make(tmp_path, w, clock)
    run_minutes(t, clock, T0, 2, [pl()])
    w.down = True
    run_minutes(t, clock, T0 + 2 * 60000, 60, [pl()])
    assert t.segments.backlog()["rows"] >= 55
    w.down = False
    run_minutes(t, clock, T0 + 62 * 60000, 2, [pl()])  # live again; a player is online: no backfill
    assert t.ok and t.segments.pending() and not t.may_backfill()
    run_minutes(t, clock, T0 + 64 * 60000, 7, [])  # everyone left: quiet time
    assert t.may_backfill()
    while t.segments.pending():
        t.backfill(max_requests=3)
    t.flush_segment()
    while t.segments.pending():
        t.backfill(max_requests=3)
    seqs = sorted(int(i.split(":")[1]) for i in w.rows)
    assert seqs == list(range(seqs[-1] + 1))  # no gaps
    assert all(v == 1 for v in w.rows.values())  # no duplicates
    assert t.segments.backlog() == t.segments.backlog() | {"segments": 0, "rows": 0}


def test_a_player_joining_stops_the_backfill_and_a_crash_resumes(tmp_path: Path) -> None:
    w, clock = FakeWorker(), [T0 / 1000]
    t = make(tmp_path, w, clock)
    w.down = True
    run_minutes(t, clock, T0, 20, [])
    w.down = False
    run_minutes(t, clock, T0 + 20 * 60000, 1, [])
    before = t.segments.backlog()["rows"]
    assert before > 10
    # a player joins between two batches: the next check refuses
    calls = {"n": 0}

    def may_run() -> bool:
        calls["n"] += 1
        return calls["n"] <= 1

    bf = telemetry.Backfill(
        t.segments, lambda rs: w.post("", {"rows": rs}), w.put, may_run, pause_s=0, sleep=lambda s: None
    )
    done = bf.run_once()
    assert done["stopped"] == 1 and done["requests"] == 1
    mid = t.segments.backlog()["rows"]
    assert mid < before
    # the next round fails mid-way (the Worker goes away): progress so far is kept, nothing is lost, the backfill
    # backs off instead of retrying the same batch at once
    w.fail_after = w.requests + 1
    done = telemetry.Backfill(t.segments, lambda rs: w.post("", {"rows": rs}), w.put, lambda: True, pause_s=0,
                              sleep=lambda s: None).run_once()  # fmt: skip
    assert done["failed"] == 1 and done["stopped"] == 1 and 1 <= done["rows"] <= telemetry.BACKFILL_BATCH
    after_crash = telemetry.Segments(tmp_path / "seg").backlog()[
        "rows"
    ]  # a fresh process reads the state file
    assert 0 < after_crash < mid
    w.fail_after = None
    t2 = telemetry.Segments(tmp_path / "seg")
    telemetry.Backfill(
        t2, lambda rs: w.post("", {"rows": rs}), w.put, lambda: True, pause_s=0, sleep=lambda s: None
    ).run_once()
    assert t2.backlog()["rows"] == 0
    assert all(v == 1 for v in w.rows.values())


class FlakyWorker(FakeWorker):
    """Answers 503 (the Worker's CPU limit) to any request with more than `max_rows` rows, and always for the
    ids in `poison`."""

    def __init__(self, max_rows: int, poison: frozenset[str] = frozenset()) -> None:
        super().__init__()
        self.max_rows, self.poison, self.sizes = max_rows, poison, []

    def post(self, path: str, body: Any) -> dict[str, Any]:
        self.sizes.append(len(body["rows"]))
        if len(body["rows"]) > self.max_rows or any(r["id"] in self.poison for r in body["rows"]):
            self.requests += 1
            raise mc_agent.IngestError("POST /ingest/experience: HTTP 503")
        return super().post(path, body)


def backlog_of(tmp_path: Path, n: int) -> telemetry.Segments:
    seg = telemetry.Segments(tmp_path)
    seg.write(rows(n), set(), "abcd0123")
    return seg


def test_backfill_batches_are_small_and_halve_on_a_server_error(tmp_path: Path) -> None:
    """P13 review M2: a few rows per request; a 5xx halves the batch (down to 1) instead of repeating it."""
    assert telemetry.BACKFILL_BATCH <= 3
    seg, w = backlog_of(tmp_path, 9), FlakyWorker(max_rows=1)
    now = [1000.0]
    pacer = telemetry.Pacer(clock=lambda: now[0])
    bf = telemetry.Backfill(seg, lambda rs: w.post("", {"rows": rs}), w.put, lambda: True, pause_s=0,
                            sleep=lambda s: None, pacer=pacer)  # fmt: skip
    done = bf.run_once()
    assert w.sizes == [3, 1, 1, 1, 1, 1, 1, 1, 1, 1]  # 3 fails -> 1 at a time (no retry of the same batch)
    assert done == done | {"stopped": 0, "failed": 1, "rows": 9} and seg.backlog()["rows"] == 0
    assert all(v == 1 for v in w.rows.values())
    for _ in range(
        telemetry.BACKFILL_GROW_AFTER
    ):  # a long healthy run grows the batch again, one row at a time
        pacer.succeeded()
    assert pacer.size == 2


def test_backfill_backs_off_and_gives_up_only_a_poison_row(tmp_path: Path) -> None:
    seg = backlog_of(tmp_path, 4)
    w = FlakyWorker(max_rows=3, poison=frozenset({"abcd0123:1"}))
    now = [1000.0]
    logs: list[str] = []
    pacer = telemetry.Pacer(clock=lambda: now[0])
    bf = telemetry.Backfill(seg, lambda rs: w.post("", {"rows": rs}), w.put, lambda: True, pause_s=0,
                            sleep=lambda s: None, pacer=pacer, log=logs.append)  # fmt: skip
    waits = []
    for _ in range(20):
        done = bf.run_once()
        if not seg.pending():
            break
        assert done["stopped"] == 1 and not pacer.ready()  # backing off, not hammering the Worker
        waits.append(pacer.retry_at - now[0])
        now[0] = pacer.retry_at
    assert waits[:3] == [60.0, 120.0, 240.0]  # doubles
    assert sorted(w.rows) == ["abcd0123:0", "abcd0123:2", "abcd0123:3"]  # every other row delivered once
    assert len([x for x in logs if "gave up row abcd0123:1" in x]) == 1
    # a network failure (not 5xx) backs off without shrinking the batch
    p2 = telemetry.Pacer(clock=lambda: now[0])
    assert p2.failed(mc_agent.IngestError("POST /ingest/experience: URLError"), 3) is False
    assert p2.size == telemetry.BACKFILL_BATCH and not p2.ready()


def test_telemetry_skips_the_backfill_while_backing_off(tmp_path: Path) -> None:
    w, clock = FakeWorker(), [T0 / 1000]
    t = make(tmp_path, w, clock)
    w.down = True
    run_minutes(t, clock, T0, 7, [])
    w.down = False
    run_minutes(t, clock, T0 + 7 * 60000, 1, [])
    t.pacer.retry_at = clock[0] + 600
    calls = []
    t.backfill = lambda max_requests=4: calls.append(1) or {}  # type: ignore[method-assign]
    t.stop.wait = lambda s: t.stop.set() or True  # type: ignore[method-assign]
    t.run()
    assert calls == []


def test_quiet_time_rules() -> None:
    lt = time.struct_time((2026, 11, 1, 3, 30, 0, 6, 305, 0))
    busy = {"pl": [{"u": ED}], "srv": {"p99": 10}}
    empty_lagging = {"pl": [], "srv": {"p99": 80}}
    assert not telemetry.quiet(busy, "00:00-23:59", lt)  # never while anyone is online
    assert telemetry.quiet({"pl": [], "srv": {"p99": 10}}, "", lt)
    assert not telemetry.quiet(empty_lagging, "", lt)
    assert telemetry.quiet(empty_lagging, "03:00-05:00", lt)
    assert telemetry.in_window("23:00-02:00", time.struct_time((2026, 11, 1, 1, 0, 0, 6, 305, 0)))
    assert not telemetry.quiet(None, "", lt)


# --- messages (agent side) -------------------------------------------------------------------------------------


def test_message_rate_limits_and_the_file_the_plugin_reads(tmp_path: Path) -> None:
    m = telemetry.Messages(tmp_path, per_player_s=1800, broadcast_s=1800, per_hour=3)
    now = T0 / 1000
    online = {ED: pl(join=T0 - 600000), MIA: pl(MIA, "MiaBuilds", join=T0 - 10000, tips=True)}
    assert m.check(ED, "lag", now, online) is None
    m.add("lag-0123456789abcdef", ED, "lag", "That stutter\twas the server", "hover\nline", now)
    lines = (tmp_path / "messages.tsv").read_text().splitlines()
    assert lines[0] == "mc-messages 1"
    assert lines[1].split("\t") == [
        "M",
        "lag-0123456789abcdef",
        ED,
        str(int((now + 120) * 1000)),
        "That stutter was the server",
        "hover line",
    ]
    assert m.check(ED, "lag", now + 60, online) == "player got a message recently"
    assert m.check(MIA, "lag", now, online) == "player joined less than a minute ago"
    assert m.check(MIA, "join", now, online) is None  # the join summary is the exception
    assert m.check("5f2a0000-0000-4000-8000-000000000c41", "lag", now, online) == "player isn't online"
    assert m.check(ED, "lag", now + 1801, {ED: pl(tips=False)}) == "player turned tips off"
    m.add("lag-1", "*", "server", "x", "", now)
    assert m.check("*", "server", now + 60, online) == "a broadcast was sent recently"
    m.add("lag-2", MIA, "join", "y", "", now)
    assert m.check(ED, "lag", now + 1900, online) == "hourly message limit reached"
    # expired entries leave the file
    m.add("lag-3", ED, "lag", "z", "", now + 7200)
    assert "lag-0123456789abcdef" not in (tmp_path / "messages.tsv").read_text()


def test_player_message_action(tmp_path: Path) -> None:
    w, clock = FakeWorker(), [T0 / 1000]
    t = make(tmp_path, w, clock)
    ex = mc_agent.Executor(lambda: {}, telemetry_=t)
    with pytest.raises(mc_agent.Rejected, match="isn't online"):
        ex.run(
            {
                "action": "player_message",
                "args": {"to": ED, "kind": "lag", "text": "hi", "id": "lag-0123456789"},
            }
        )
    t.latest = telemetry.parse_snapshot(json.dumps(snap(0, T0, [pl()])).encode())
    out = ex.run(
        {
            "action": "player_message",
            "args": {
                "to": ED,
                "kind": "lag",
                "text": "That stutter was the server",
                "hover": "h",
                "id": "lag-0123456789",
            },
        }
    )
    assert out == "queued for Em_K"
    for args, why in [
        ({"to": "bob", "kind": "lag", "text": "x", "id": "lag-0123456789"}, "to must be"),
        ({"to": ED, "kind": "shout", "text": "x", "id": "lag-0123456789"}, "kind must be"),
        ({"to": ED, "kind": "lag", "text": "x", "id": "../x"}, "bad id"),
        ({"to": ED, "kind": "lag", "text": "  ", "id": "lag-0123456789"}, "empty text"),
        ({"to": ED, "kind": "lag", "text": "again", "id": "lag-0123456780"}, "recently"),
    ]:
        with pytest.raises(mc_agent.Rejected, match=why):
            ex.run({"action": "player_message", "args": args})
    with pytest.raises(mc_agent.Rejected, match="telemetry is off"):
        mc_agent.Executor(lambda: {}).run({"action": "player_message", "args": {}})


def test_clean_text_never_breaks_the_line_format() -> None:
    assert telemetry.clean_text("a\tb\nc\x1fd\x00e") == "a b c de"
    tsv = telemetry.verdicts_tsv({"server": {"verdict": "poor\t", "headline": "x\ny", "reasons": ["r\x1f1", "r2"], "tips": "notalist"},
                                  "players": {"not-a-uuid": {"verdict": "good"}}}, 5)  # fmt: skip
    assert tsv.splitlines() == ["mc-verdicts 1 5", "S\tpoor\tx y\tr 1\x1fr2\t"]


def test_p13_is_wired_into_e2e_bench_and_ansible() -> None:
    repo = Path(__file__).parent.parent
    wf = (repo / ".github/workflows/e2e-mock.yml").read_text()
    assert "python tests/e2e/telemetry_check.py mc-e2e" in wf
    assert "python scripts/bench_telemetry.py --jdk" in wf
    env = (repo / "ansible/roles/agent/templates/agent.env.j2").read_text()
    for var in ("EXPERIENCE", "TELEMETRY_DIR", "VERDICT_DIR", "SEGMENT_DIR", "SEGMENT_DAYS", "SEGMENT_CAP_MB",
                "SEGMENT_S", "BACKFILL_WINDOW", "MESSAGE_PER_PLAYER_S", "MESSAGE_BROADCAST_S", "MESSAGE_PER_HOUR",
                "LEVEL_NAME"):  # fmt: skip
        assert f"\n{var}=" in env, var
    cfg = telemetry.Config.from_env({"SEGMENT_S": "60", "BACKFILL_WINDOW": "bad"}, Path("/srv"), "world")
    assert cfg.segment_s == 60 and cfg.backfill_window == "" and cfg.stats_dir == Path("/srv/world/stats")
    tasks = (repo / "ansible/roles/plugins/tasks/telemetry.yml").read_text()
    assert "d {{ mc_verdict_dir }} 2750 {{ mc_ops_user }} minecraft -" in tasks


def test_the_workers_cpu_test_uses_the_agents_backfill_batch() -> None:
    """P13 review M2: watchdog/test/experience.test.mjs measures the Worker's CPU for a request of exactly the
    agent's backfill batch."""
    js = (Path(__file__).parent.parent / "watchdog/test/experience.test.mjs").read_text()
    assert f"const AGENT_BACKFILL_BATCH = {telemetry.BACKFILL_BATCH};" in js


def test_busiest_chunks_parsed_and_merged_per_minute() -> None:
    """The plugin's chunk scan (srv.hot): sane types only; a minute keeps per chunk the highest counts of its
    snapshots, the entity-heaviest first, tile-heavy chunks (hoppers) kept too."""
    hot = [{"w": "world", "cx": 12, "cz": -5, "ent": 300, "tiles": 2, "vil": 0, "items": 40},
           {"w": "world", "cx": 1, "cz": 1, "ent": 5, "tiles": 400, "vil": 0, "items": 0},
           {"w": 7, "cx": 1, "cz": 1}, {"w": "world", "cx": "1", "cz": 1}]  # fmt: skip
    got = telemetry.parse_snapshot(json.dumps(snap(1, T0, hot=hot, scanned=812)).encode())
    assert got is not None and got["srv"]["scanned"] == 812
    assert [(c["cx"], c["ent"], c["tiles"]) for c in got["srv"]["hot"]] == [(12, 300, 2), (1, 5, 400)]
    later = dict(hot[0], ent=410, items=12)
    a = telemetry.parse_snapshot(json.dumps(snap(2, T0 + 10000, hot=[later])).encode())
    assert a is not None
    minute = telemetry.summarize_server([got, a])["hot"]
    assert minute[0] == {"w": "world", "cx": 12, "cz": -5, "ent": 410, "tiles": 2, "vil": 0, "items": 40}
    assert minute[1]["tiles"] == 400 and len(minute) == 2
    many = [
        {"w": "world", "cx": i, "cz": 0, "ent": i, "tiles": 0, "vil": 0, "items": 0} for i in range(1, 30)
    ]
    b = telemetry.parse_snapshot(json.dumps(snap(3, T0, hot=many)).encode())
    assert b is not None and len(telemetry.summarize_server([b])["hot"]) == telemetry.MAX_HOT


# --- v1.4: `mc_agent.py telemetry`, the telemetry alone (the parity harness's entry point) ---------------


def agent_settings(tmp_path: Path) -> mc_agent.Settings:
    return mc_agent.Settings(ingest_url="https://w.example", token="t" * 32, hmac_key=b"k" * 32,
                             state_dir=tmp_path / "state", experience=True)  # fmt: skip


def test_the_python_agent_runs_telemetry_in_process_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """python mode: the agent's own Telemetry is the plain class, measuring the same units as before (no
    mc-telemetry.service row on a box that has no sidecar), with the state dir it always had."""
    monkeypatch.setenv("TELEMETRY_DIR", str(tmp_path / "snap"))
    t = mc_agent.make_telemetry(agent_settings(tmp_path))
    assert type(t) is telemetry.Telemetry and t.stats is telemetry.unit_stats
    assert telemetry.OVERHEAD_UNITS == ("minecraft.service", "mc-agent.service")
    assert t.id_file == tmp_path / "state" / "telemetry-id.json"
    assert mc_agent.SIDECAR_UNITS == (*telemetry.OVERHEAD_UNITS, "mc-telemetry.service")


def test_the_sidecar_publishes_who_is_online_for_the_rust_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """online.json = {uuid: {u, n, tips, join}} (as rust telemetry/sidecar.rs writes it), rewritten after every
    poll, atomically and 0640, and narrowed: no position, world or client details leave the telemetry."""
    snaps = tmp_path / "snap"
    snaps.mkdir()
    monkeypatch.setenv("TELEMETRY_DIR", str(snaps))
    monkeypatch.setenv("VERDICT_DIR", str(tmp_path))
    monkeypatch.setenv("SEGMENT_DIR", str(tmp_path / "seg"))
    t = mc_agent.make_telemetry(agent_settings(tmp_path), cls=mc_agent.SidecarTelemetry)
    t.post = t.put = lambda *_: pytest.fail("no request in this test")  # type: ignore[method-assign]
    assert isinstance(t, mc_agent.SidecarTelemetry)
    t.online_file = tmp_path / "online.json"
    (tmp_path / "state").mkdir()
    now = (
        int(time.time() * 1000) // 60000 * 60000
    )  # both snapshots in this minute: no row closes, nothing is sent
    write_snap(snaps, snap(1, now, [pl(), pl(MIA, "Mia", tips=False)]))
    t.poll()
    data = json.loads(t.online_file.read_text())
    assert data == {ED: {"u": ED, "n": "Em_K", "tips": True, "join": T0 - 600000},
                    MIA: {"u": MIA, "n": "Mia", "tips": False, "join": T0 - 600000}}  # fmt: skip
    assert t.online_file.stat().st_mode & 0o777 == 0o640
    assert not list(tmp_path.glob(".online.json.tmp"))
    # nobody online: an empty object, still rewritten (a fresh mtime = a live sidecar)
    write_snap(snaps, snap(2, now + 10000, []))
    os.utime(t.online_file, (0, 0))
    t.poll()
    assert json.loads(t.online_file.read_text()) == {} and t.online_file.stat().st_mtime > 0
    # a failed poll still refreshes the file with the last known map
    t.reader.read = lambda: (_ for _ in ()).throw(OSError("boom"))  # type: ignore[method-assign]
    os.utime(t.online_file, (0, 0))
    with pytest.raises(OSError):
        t.poll()
    assert t.online_file.stat().st_mtime > 0


def test_the_sidecar_reads_systemds_resource_lines_itself(tmp_path: Path) -> None:
    """In-process, the agent's logs loop fed Overhead.observe_journal; the sidecar reads systemd's own lines with
    a cursor of its own, saved after each batch."""
    state = tmp_path / "journal.json"
    calls: list[tuple[str | None, list[str]]] = []
    seen: list[list[dict[str, Any]]] = []
    lines = [{"ts": 1, "unit": "init.scope", "ident": "systemd", "prio": 6, "audit": 0,
              "msg": "mc-backup.service: Consumed 2.500s CPU time, 40.0M memory peak.",
              "sd_unit": "mc-backup.service"}]  # fmt: skip

    def read(cursor: str | None, argv: list[str]) -> tuple[list[dict[str, Any]], str, bool]:
        calls.append((cursor, argv))
        return lines, "c9", False

    assert mc_agent.overhead_round(state, seen.append, read) is False
    assert (
        calls[0][0] is None
        and calls[0][1][-1] == "SYSLOG_IDENTIFIER=systemd"
        and "--since=-5min" in calls[0][1]
    )
    assert seen == [lines] and json.loads(state.read_text())["cursor"] == "c9"
    mc_agent.overhead_round(state, seen.append, read)
    assert calls[1][0] == "c9" and "--after-cursor=c9" in calls[1][1]
    o = telemetry.Overhead()
    o.observe_journal(lines)
    assert o.sample({}, 1.0)["mc-backup.service"] == {"cpu_ms": 2500.0, "runs": 1, "mem_peak_mb": 40.0}


def test_the_sidecar_refuses_without_experience(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("INGEST_URL", "https://w.example")
    monkeypatch.setenv("AGENT_TOKEN", "t" * 32)
    monkeypatch.setenv("COMMAND_HMAC_KEY", "k" * 32)
    monkeypatch.setenv("EXPERIENCE", "0")
    assert mc_agent.telemetry_main() == 2
    assert "EXPERIENCE is off" in capsys.readouterr().err
    monkeypatch.setenv("AGENT_TOKEN", "short")
    assert mc_agent.telemetry_main() == 2


def test_the_telemetry_subcommand_has_no_unit() -> None:
    """`mc_agent.py telemetry` dispatches to telemetry_main (the telemetry parity harness's entry point, with
    `smallworld-agent telemetry`), and no unit runs it: both agents run the telemetry in-process (v1.4)."""
    repo = Path(__file__).parent.parent
    src = (repo / "scripts/mc_agent.py").read_text()
    assert 'telemetry_main() if sys.argv[1:2] == ["telemetry"] else main()' in src
    units = [p for p in (repo / "ansible").rglob("*.service.j2") if "mc_agent.py telemetry" in p.read_text()]
    assert units == []
    main_rs = (repo / "rust/smallworld-agent/src/main.rs").read_text()
    assert 'Some("telemetry")' in main_rs and "telemetry::sidecar::main()" in main_rs


def test_ci_gates_every_telemetry_scenario_on_the_rust_agent() -> None:
    """v1.4: ci.yml's rust job runs every telemetry parity scenario against the committed release binary; a new
    scenario (a golden file) must be added there too."""
    repo = Path(__file__).parent.parent
    ci = yaml.safe_load((repo / ".github/workflows/ci.yml").read_text())
    step = next(
        s
        for s in ci["jobs"]["rust"]["steps"]
        if s.get("name") == "Telemetry parity scenarios against the Rust agent"
    )
    listed = step["env"]["RUST_TELEMETRY_SCENARIOS"].split()
    golden = sorted(p.stem for p in (repo / "tests/telemetry_parity/golden").glob("*.json"))
    assert sorted(listed) == golden and len(listed) == len(set(listed))
