"""The telemetry parity scenarios. Each takes a fresh TelScenario, writes the plugin's snapshots, drives the sidecar
and records notes; the harness adds every Worker request, the files the runs changed, the fake systemd calls,
the output and the exit codes. Keep them deterministic: write the next input only after the event it depends on
(a request, a read loop: `s.next_poll()`), never on a sleep alone."""

from __future__ import annotations

import gzip
import json
import os
import time
from collections.abc import Callable
from typing import Any

from tel_harness import (
    IDENTITY,
    ONLINE,
    SEGMENTS,
    SRC,
    UUIDS,
    TelScenario,
    minute,
    ok_reply,
    player,
    snap,
)

SCENARIOS: dict[str, Callable[[TelScenario], None]] = {}


def scenario(fn: Callable[[TelScenario], None]) -> Callable[[TelScenario], None]:
    SCENARIOS[fn.__name__] = fn
    return fn


def first_seq(rec: dict[str, Any]) -> int:
    rows = (rec["body"] or {}).get("rows") or [] if isinstance(rec["body"], dict) else []
    return int(rows[0]["seq"]) if rows else -1


def is_live(rec: dict[str, Any]) -> bool:
    return isinstance(rec["body"], dict) and rec["body"].get("live") is True


def stats_file(s: TelScenario, name: str, play_ticks: int, deaths: int = 0) -> None:
    s.box.write(f"/opt/minecraft/server/world/stats/{UUIDS[name]}.json",
                {"stats": {"minecraft:custom": {"minecraft:play_time": play_ticks, "minecraft:deaths": deaths,
                                                "minecraft:walk_one_cm": 12345, "minecraft:jump": 7,
                                                "minecraft:aviate_one_cm": "x"}}, "DataVersion": 4671})  # fmt: skip


VERDICTS = {
    "server": {
        "verdict": "fine-and-more",  # cut to 8 characters
        "headline": "Server\tsmooth\nall minute",
        "reasons": ["tick time fine", "", "a\x1fb", "  spaced   out  ", "r5", "r6", "r7", "r8", "r9 (dropped)"],
        "tips": ["caf\u00e9 \u2713 \u00fcn\u00efc\u00f8d\u00e9", "line\u2028sep\u0007bell", 42],
    },
    "players": {
        UUIDS["Alice"]: {"verdict": "laggy", "headline": "Your ping is high (" + "x" * 250 + ")",
                         "reasons": ["ping 180 ms"], "tips": "not a list"},
        UUIDS["Bob"]: "not a dict",
        "not-a-uuid": {"verdict": "ok"},
        UUIDS["Carol"].upper(): {"verdict": "ok"},
    },
}  # fmt: skip


# --- 1: a normal minute -------------------------------------------------------------------------------------


@scenario
def minute_row(s: TelScenario) -> None:
    """Snapshots -> one minute row -> POST /ingest/experience -> verdicts.tsv; the next minute closes when the
    plugin goes quiet (75 s after its start), a reply that isn't an object leaves verdicts.tsv as it was."""
    s.identity(SRC, 40)
    stats_file(s, "Alice", 72000, 1)
    hot = [{"w": "world", "cx": 3, "cz": -2, "ent": 140, "tiles": 5, "vil": 12, "items": 40},
           {"w": "world_nether", "cx": 0, "cz": 0, "ent": 2, "tiles": 90, "vil": 0, "items": 0},
           {"w": "world", "cx": 9, "cz": 9, "ent": 0, "tiles": 0, "vil": 0, "items": 0}]  # fmt: skip
    worlds = [{"n": "world", "chunks": 420, "entities": 310, "tiles": 55, "ticking": 41, "players": 2},
              {"n": "world_nether", "chunks": 80, "entities": 20, "tiles": 90, "ticking": 30, "players": 0}]  # fmt: skip
    alice = player("Alice", minute(0, -30), ping=35, fps=118, brand="fabric", cx=3, cz=-2, ent=140)
    bob = player("Bob", minute(0, 2), ping=80, w="world", ent=4)
    s.write_snap(
        snap(1, minute(0, 5), [alice], p50=11.0, p99=31.5, mean=12.0, ticks=200, hot=hot, worlds=worlds)
    )
    s.write_snap(snap(2, minute(0, 15), [alice | {"ping": 45, "fps": 61}, bob], p50=2.675, p99=48.25,
                      mean=14.0, ticks=198, hot=hot[1:], worlds=worlds, heap_used_mb=2100.5))  # fmt: skip
    s.write_snap(snap(3, minute(0, 25), [alice | {"ping": 40, "fps": None}, bob | {"ping": 90, "glide": True}],
                      p50=12.5, tps=19.87, mean=None, ticks=199, worlds=worlds, budget_skips=2,
                      lost_windows=1, self_max_us=151.25))  # fmt: skip
    s.write_snap(snap(4, minute(1, 5), [bob]))

    def reply(rec: dict[str, Any], n: int) -> tuple[int, Any]:
        return 200, (ok_reply(rec, VERDICTS) if n == 0 else ["not", "an", "object"])

    s.experience = reply
    s.begin()
    s.start()
    s.wait_requests(1)
    tsv = s.box.host("/run/mc-verdicts/verdicts.tsv")
    s.wait(tsv.exists, 10, "verdicts.tsv")
    s.wait_requests(2)
    s.next_poll()
    s.stop()


# --- 2: sessions --------------------------------------------------------------------------------------------


@scenario
def sessions(s: TelScenario) -> None:
    """Join and leave events across minutes: a join before the first snapshot, a join without a join time, a
    leave, a gap (the server was away > 60 s: who is gone left when last seen), a reconnect with a new join time
    (the old session closes at its last-seen time) and the vanilla stats read on a leave."""
    s.identity(SRC, 0)
    stats_file(s, "Alice", 1000)
    stats_file(s, "Bob", 2000, 3)
    alice = player("Alice", minute(0, -30))
    bob = player("Bob", minute(0, 8))
    carol = player("Carol", 0)  # no join time from the plugin: the snapshot's time stands in
    s.write_snap(snap(1, minute(0, 0), [alice]))
    s.write_snap(snap(2, minute(0, 10), [alice, bob]))
    s.write_snap(snap(3, minute(1, 0), [bob]))
    s.write_snap(snap(4, minute(1, 10), [bob, carol]))
    s.write_snap(snap(5, minute(3, 20), [bob | {"join": minute(3, 10)}]))  # after a gap; Bob reconnected
    s.write_snap(snap(6, minute(3, 30), []))
    s.begin()
    s.start()
    s.wait_requests(3)
    s.next_poll()
    s.stop()


# --- 3: lag events ------------------------------------------------------------------------------------------


@scenario
def lag(s: TelScenario) -> None:
    """Spikes with their context: at most 32 per snapshot, the 20 worst per minute (ties keep their order),
    sorted by time; the three busiest worlds, the three players near the most entities, our running units."""
    s.identity(SRC, 7)
    s.systemd["is_active"] = {"mc-backup.service": "active", "mc-prune.service": "activating",
                              "mc-restore-apply.service": "inactive"}  # fmt: skip
    s.save_systemd()
    worlds = [{"n": f"w{i}", "chunks": 100 * i, "entities": [50, 400, 10, 400, 300][i], "tiles": i,
               "ticking": i, "players": 1} for i in range(5)]  # fmt: skip
    names = ["Alice", "Bob", "Carol", "Dave", "Erin"]
    pl = [player(n, minute(0, -100), ent=[30, 200, 200, 5, 90][i], w=f"w{i}", cx=i, cz=-i, glide=i == 2)
          for i, n in enumerate(names)]  # fmt: skip
    spikes1 = [[minute(0, 1) + i * 37, 50.0 + (i % 7) * 10.25] for i in range(40)]  # 40: only 32 are read
    spikes1 += [["x", 99.0], [1, 2, 3], [minute(0, 2), "slow"]]
    spikes2 = [[minute(0, 12) + i, ms] for i, ms in enumerate([52.25, 52.35, 112.05, 50.0, 0.05, 1e3, 52.25])]
    s.write_snap(snap(1, minute(0, 5), pl, spikes=spikes1, worlds=worlds, max=120.0))
    s.write_snap(snap(2, minute(0, 15), pl[:2], spikes=spikes2, worlds=worlds[:2], max=1000.0))
    s.write_snap(snap(3, minute(0, 25), pl[:1]))
    s.write_snap(snap(4, minute(1, 5), [], spikes=[[minute(1, 6), 75.55]]))
    s.begin()
    s.start()
    s.wait_requests(2)
    s.next_poll()
    s.stop()


# --- 7: malformed, oversized and out-of-order snapshots ----------------------------------------------------


@scenario
def bad_snapshots(s: TelScenario) -> None:
    """What the plugin (a less privileged process) leaves in /run/mc-telemetry is never trusted: bad names,
    bad JSON, a wrong version, more than MAX_SNAPSHOT_BYTES (only the first MiB is read: a valid document padded
    with whitespace still parses, a cut one doesn't), a timestamp older than the last read loop's, a symlink, a FIFO, a directory, junk
    fields; then the plugin restarts (its sequence starts again at 0) and is followed."""
    s.identity(SRC, 0)
    d = s.box.host("/run/mc-telemetry")
    s.write_snap(snap(1, minute(0, 0), [player("Alice", minute(0, -5))]))
    s.write_snap(b"{not json", seq=2)
    s.write_snap(json.dumps(snap(3, minute(0, 2)) | {"v": 2}).encode(), seq=3)
    big = json.dumps(snap(4, minute(0, 3), [player("Dave", 1)] * 400) | {"pad": "x" * (1 << 20)}).encode()
    s.write_snap(big, seq=4)
    s.write_snap(json.dumps(snap(5, minute(0, 20), [player("Bob", minute(0, 18))])).encode() + b" " * (1 << 20),
                 seq=5)  # fmt: skip
    os.symlink("snap-1.json", d / "snap-7.json")
    os.mkfifo(d / "snap-8.json", 0o640)
    (d / "snap-9.json").mkdir()
    for name in (
        "snap-abc.json",
        "snap-1234567890123.json",
        "snap-11.json.tmp",
        "snap--1.json",
        "Snap-12.json",
    ):
        s.write_snap(snap(99, minute(0, 50), [player("Erin", 1)]), name=name)
    junk = snap(10, minute(0, 40), [
        player("Alice", minute(0, -5), ping="40", fps=-1, brand="Lunar\tClient\n\u0000x", loc="en_us" * 9,
               w="wor\u2028ld", glide="yes", tips=0, ent=12.9, cx=1.5),
        player("Bob", minute(0, 18), fps=99999, tips=False),
        player("Bad Name", 1), player("x" * 17, 1), player("Carol", 1, u=UUIDS["Carol"].upper()),
        "not a dict",
    ], tps="20", p50=True, p99=None, heap_used_mb=1e6, ticks=200.7,
        worlds=[{"n": "world", "chunks": "9", "entities": 5.9, "tiles": None, "ticking": 1, "players": 2},
                {"n": 5}, "x", {"n": "n\u00e9ther\u0007", "chunks": 1}],
        hot=[{"w": "world", "cx": 1, "cz": 2, "ent": -5, "tiles": "3", "vil": 2.7, "items": None},
             {"w": "world", "cx": "1", "cz": 2}, {"cx": 1, "cz": 1}, "x"],
        spikes=[[minute(0, 41), 60], [minute(0, 42), True], [minute(0, 43)], [minute(0, 44), 70.5]],
        extra_field={"ignored": True})  # fmt: skip
    s.write_snap(junk)
    s.begin()
    s.start()
    s.wait_requests(1)  # minute 0 closed in the first read loop (the plugin is quiet: it's 75 s old)
    s.write_snap(snap(11, minute(0, 30), [player("Carol", 1)]))  # older than the last one read: ignored
    s.write_snap(snap(12, minute(1, 0), [player("Bob", minute(0, 18))]))
    s.wait_requests(2)
    s.clear_snaps()  # the plugin restarted (Writer.clean) and starts again at 0
    s.write_snap(snap(0, minute(2, 5), [player("Bob", minute(2, 0))]))
    s.write_snap(snap(1, minute(2, 15), [player("Bob", minute(2, 0))]))
    s.wait_requests(3)
    s.next_poll()
    s.stop()


# --- 8: identity --------------------------------------------------------------------------------------------


@scenario
def identity(s: TelScenario) -> None:
    """telemetry-id.json: a fresh installation id, the sequence continuing across a restart (the snapshots
    still in /run/mc-telemetry are read again, under new ids: the Worker upserts on the minute), and a corrupt
    file replaced by a new id starting at 0."""
    s.write_snap(snap(1, minute(0, 0), [player("Alice", minute(0, 0))]))
    s.write_snap(snap(2, minute(1, 0), [player("Alice", minute(0, 0))]))
    s.begin()
    for label in ("first", "restart"):
        before = len(s.requests())
        s.start()
        s.wait_requests(before + 2)
        s.next_poll()
        s.stop(label)
        s.note(
            f"{label}_identity",
            {"json": s.box.json(IDENTITY), "mode": oct(s.box.host(IDENTITY).stat().st_mode & 0o777)},
        )
    s.box.write(IDENTITY, json.dumps({"src": "NOT-HEX!", "seq": 9}), 0o600)
    s.clear_snaps()
    s.write_snap(snap(1, minute(5, 0), []))
    before = len(s.requests())
    s.start()
    s.wait_requests(before + 1)
    s.next_poll()
    s.stop("corrupt")


# --- configuration errors -----------------------------------------------------------------------------------


@scenario
def startup_errors(s: TelScenario) -> None:
    """The sidecar refuses to start without its settings (exit 2, one line on stderr, nothing written)."""
    s.begin()
    s.run_to_exit("experience_off", EXPERIENCE="0")
    s.run_to_exit("short_token", AGENT_TOKEN="short")
    s.run_to_exit("remote_http", INGEST_URL="http://192.0.2.10:8080")
    s.run_to_exit("bad_number", SEGMENT_S="soon")


# --- 4: product overhead ------------------------------------------------------------------------------------


def journal_entry(i: int, unit: str, msg: str, ident: str = "systemd") -> str:
    return json.dumps({"__CURSOR": f"s=telparity;i={i:05d}", "__REALTIME_TIMESTAMP": str(1768471200000000 + i),
                       "SYSLOG_IDENTIFIER": ident, "PRIORITY": "6", "UNIT": unit, "_SYSTEMD_UNIT": "init.scope",
                       "MESSAGE": msg})  # fmt: skip


@scenario
def overhead(s: TelScenario) -> None:
    """The overhead card: long-running units from `systemctl show` (memory, peak, CPU and IO deltas between two
    samples; UINT64_MAX and "[not set]" as systemd prints them), oneshot units from systemd's "Consumed" lines
    read by the sidecar's own journal loop (LOG_BATCH_S, cursor in journal.json), summed per unit (an instance
    counts for its template) and reset after each row."""
    s.identity(SRC, 3)
    s.systemd["show"]["mc-telemetry.service"] |= {
        "MemoryPeak": "18446744073709551615",
        "IOReadBytes": "[not set]",
    }
    lines = [
        journal_entry(1, "mc-backup.service", "mc-backup.service: Consumed 1min 2.500s CPU time, 812.3M memory peak."),
        journal_entry(2, "mc-health@daily.service", "mc-health@daily.service: Consumed 350ms CPU time."),
        journal_entry(3, "mc-backup.service", "mc-backup.service: Consumed 2.1s CPU time, 1.5G memory peak, 0B swap."),
        journal_entry(4, "minecraft.service", "minecraft.service: Consumed 3h 2min CPU time."),
        journal_entry(5, "mc-prune.service", "mc-prune.service: Consumed 900ms CPU time, 512K memory peak.",
                      ident="mc-agent"),
        journal_entry(6, "mc-evil.service; rm", "mc-evil.service: Consumed 1s CPU time."),
        journal_entry(7, "mc-prune.service", "mc-prune.service: Deactivated successfully."),
        journal_entry(8, "mc-prune.service", "mc-prune.service: Consumed 12.75ms CPU time, 2.5M memory peak."),
    ]  # fmt: skip
    fixture = s.tmp / "journal.jsonl"
    fixture.write_text("\n".join(lines) + "\n")
    s.systemd["journal"] = str(fixture)
    s.save_systemd()
    s.write_snap(snap(1, minute(0, 5)))
    s.write_snap(snap(2, minute(1, 5)))
    s.begin()
    s.start(LOG_BATCH_S="10")
    s.wait_requests(1)
    s.systemd["show"]["minecraft.service"] |= {"CPUUsageNSec": "8000000000", "MemoryCurrent": str(3 << 30 | 1 << 20),
                                               "IOReadBytes": "3145728", "IOWriteBytes": "2097152"}  # fmt: skip
    s.systemd["show"]["mc-agent.service"] |= {
        "CPUUsageNSec": "100000000",
        "IOWriteBytes": "9216",
    }  # counter reset
    s.save_systemd()
    s.wait_requests(2)
    s.wait(s.box.host("/var/lib/mc-telemetry/journal.json").exists, 20, "the first overhead journal round")
    s.write_snap(snap(3, minute(2, 5)))
    s.wait_requests(3)
    s.wait(lambda: sum(c["tool"] == "journalctl" for c in s.calls()) >= 2, 20, "the second journal round")
    s.stop()


# --- 5: segments --------------------------------------------------------------------------------------------


@scenario
def segments(s: TelScenario) -> None:
    """Every SEGMENT_S the buffered rows become one gzip NDJSON segment (todo = the rows the Worker didn't take
    live), PUT /ingest/segment, then the backlog row backfilled; retention: older than SEGMENT_DAYS, then the
    oldest over SEGMENT_CAP_MB, dropped; the state of a segment deleted by hand is forgotten (after the backfill
    gave up on reading it)."""
    s.identity(SRC, 10)
    seg = s.box.host(SEGMENTS)
    seg.mkdir(mode=0o700)
    old = "20251101T0000-0badc0de-1-2.ndjson.gz"
    rows = [
        {"v": 1, "id": f"{SRC}:{i}", "src": SRC, "seq": i, "ts": 1761955200000 + i * 60000} for i in (1, 2)
    ]
    (seg / old).write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode(), mtime=0))
    forty_days = time.time() - 40 * 86400
    os.utime(seg / old, (forty_days, forty_days))
    for name, mib in (("20251201T0000-0badc0de-3-3", 4), ("20251202T0000-0badc0de-4-4", 4),
                      ("20251203T0000-0badc0de-5-5", 3)):  # fmt: skip
        with open(seg / f"{name}.ndjson.gz", "wb") as f:
            f.truncate(mib << 20)  # sparse: what counts is st_size
    (seg / "notes.ndjson.gz").write_bytes(b"not a segment name")
    state = {old: {"todo": [], "r2": True, "rows": 2},
             "20251204T0000-0badc0de-6-6.ndjson.gz": {"todo": [0], "r2": False, "rows": 1},
             "bogus-name.ndjson.gz": {"todo": [0], "r2": False}}  # fmt: skip
    (seg / "state.json").write_text(json.dumps(state))
    for p in seg.iterdir():
        p.chmod(0o600)
    s.write_snap(snap(1, minute(0, 5), [player("Alice", minute(0, 0))]))
    s.write_snap(snap(2, minute(1, 5), [player("Alice", minute(0, 0))]))
    s.write_snap(snap(3, minute(2, 5), []))

    def reply(rec: dict[str, Any], n: int) -> tuple[int, Any]:
        if is_live(rec) and first_seq(rec) == 11:
            return 429, {"error": "slow down"}
        return 200, ok_reply(rec)

    s.experience = reply
    s.begin()
    s.start(SEGMENT_S="30", SEGMENT_CAP_MB="10", SEGMENT_DAYS="30")
    s.wait_requests(3, live=True)
    s.wait_requests(1, route="PUT /ingest/segment", timeout=45)
    s.wait_requests(1, live=False)
    s.next_poll()
    s.stop()


# --- 6: backfill --------------------------------------------------------------------------------------------


@scenario
def backfill_quiet(s: TelScenario) -> None:
    """Rows the Worker refused live (5xx) wait in the segment and are backfilled, oldest first, BACKFILL_BATCH
    rows a request and at most 4 requests a round, only in quiet time: a player online stops it (the next round
    doesn't run), nobody online resumes it."""
    s.identity(SRC, 0)
    for i in range(15):
        s.write_snap(snap(i + 1, minute(i, 5)))

    def reply(rec: dict[str, Any], n: int) -> tuple[int, Any]:
        if is_live(rec) and first_seq(rec) < 13:
            return 503, {"error": "D1 unavailable"}
        return 200, ok_reply(rec)

    s.experience = reply
    s.begin()
    s.start(SEGMENT_S="30")
    s.wait_requests(15, live=True)
    s.wait_requests(4, live=False, timeout=45)
    bob = player("Bob", minute(15, 0))
    s.write_snap(snap(16, minute(15, 5), [bob]))
    s.wait(lambda: UUIDS["Bob"] in (s.box.read(ONLINE) or ""), 20, "Bob online")
    time.sleep(1)  # the round after that read loop (it would run at once) must not start
    s.note("backfill_while_bob_online", len(s.requests(live=False)))
    s.write_snap(snap(17, minute(16, 5)))
    s.wait_requests(5, live=False)
    s.next_poll()
    s.stop()


@scenario
def backfill_errors(s: TelScenario) -> None:
    """A failed segment PUT (429) leaves the backfill waiting for the next live success; a 5xx halves the batch
    and retries at once, a 5xx for a single row backs off (no request in the next read loops), the segment PUT
    waits for its rows."""
    s.identity(SRC, 0)
    for i in range(5):
        s.write_snap(snap(i + 1, minute(i, 5)))
    backfills = iter([(503, {"error": "exceeded CPU"}), (200, None), (502, {"error": "bad gateway"})])

    def reply(rec: dict[str, Any], n: int) -> tuple[int, Any]:
        if is_live(rec):
            return (503, {"error": "D1 unavailable"}) if first_seq(rec) < 3 else (200, ok_reply(rec))
        status, body = next(backfills, (200, None))
        return status, body if body is not None else ok_reply(rec)

    s.experience = reply
    s.segment = lambda rec, n: (429, {"error": "slow down"}) if n == 0 else (200, {"ok": True})
    s.begin()
    s.start(SEGMENT_S="30")
    s.wait_requests(5, live=True)
    s.wait_requests(1, route="PUT /ingest/segment", timeout=45)
    s.next_poll()
    s.note("backfill_after_failed_put", len(s.requests(live=False)))
    s.write_snap(snap(6, minute(5, 5)))
    s.write_snap(snap(7, minute(6, 5)))
    s.wait_requests(3, live=False)
    s.next_poll(2)
    s.stop()


# --- 9: online.json -----------------------------------------------------------------------------------------


@scenario
def online(s: TelScenario) -> None:
    """online.json for the Rust agent's player_message: {uuid: {join, n, tips, u}} by the latest snapshot,
    0640, rewritten after every read loop (a fresh file = a live sidecar), kept as it was when the plugin goes
    quiet."""
    s.identity(SRC, 0)
    alice = player("Alice", minute(0, -20), tips=False, ping=33, brand="fabric", w="world_nether", cx=5)
    bob = player("Bob", minute(0, 1))
    s.write_snap(snap(1, minute(0, 5), [alice, bob]))
    s.begin()
    s.start()

    def observe(label: str) -> None:
        st = s.box.host(ONLINE).stat()
        s.note(label, {"text": s.box.read(ONLINE), "mode": oct(st.st_mode & 0o7777)})

    observe("first")
    stamp = s.online_stamp()
    s.next_poll()
    s.note("rewritten_unchanged", s.online_stamp() != stamp)
    s.write_snap(snap(2, minute(0, 15), [bob]))
    s.next_poll()
    observe("alice_left")
    s.next_poll()
    observe("plugin_quiet")
    s.stop()


# --- 11: state round trip -----------------------------------------------------------------------------------


@scenario
def state_roundtrip(s: TelScenario) -> None:
    """One state through Python, the implementation under test and Python again: the identity's sequence, a
    segment Python wrote (its rows refused live) backfilled by the implementation (which leaves one row behind
    after a 5xx), then finished, and its PUT done, by Python."""
    from tel_harness import PYTHON_IMPL

    s.identity(SRC, 100)
    phase = {"name": "python_1", "backfill": 0}

    def reply(rec: dict[str, Any], n: int) -> tuple[int, Any]:
        if is_live(rec):
            return (503, {"error": "down"}) if phase["name"] == "python_1" else (200, ok_reply(rec))
        phase["backfill"] += 1
        if phase["name"] == "impl" and phase["backfill"] == 2:
            return 503, {"error": "exceeded CPU"}
        return 200, ok_reply(rec)

    s.experience = reply
    for i in range(4):
        s.write_snap(snap(i + 1, minute(i, 5)))
    s.begin()

    def state() -> Any:
        return {"identity": s.box.json(IDENTITY), "segments": s.box.json(f"{SEGMENTS}/state.json")}

    s.start(PYTHON_IMPL, SEGMENT_S="30")
    s.wait_requests(4, live=True)
    s.wait(s.box.host(f"{SEGMENTS}/state.json").exists, 45, "the first segment")
    s.stop("python_1")
    s.note("after_python_1", state())
    for label, impl, minutes in (("impl", s.impl, (10, 11)), ("python_2", PYTHON_IMPL, (20, 21))):
        phase["name"] = label
        s.clear_snaps()
        for k, m in enumerate(minutes):
            s.write_snap(snap(k, minute(m, 5)))
        live = len(s.requests(live=True))
        s.start(impl, SEGMENT_S="3600")
        s.wait_requests(live + 2, live=True)
        s.next_poll()
        s.stop(label)
        s.note(f"after_{label}", state())
