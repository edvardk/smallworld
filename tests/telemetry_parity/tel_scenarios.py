"""The telemetry parity scenarios. Each takes a fresh TelScenario, writes the plugin's snapshots, drives the sidecar
and records notes; the harness adds every Worker request, the files the runs changed, the fake systemd calls,
the output and the exit codes. Keep them deterministic: write the next input only after the event it depends on
(a request, a read loop: `s.next_poll()`), never on a sleep alone."""

from __future__ import annotations

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
        "tips": ["café ✓ ünïcødé", "line\u2028sep\u0007bell", 42],
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
                {"n": 5}, "x", {"n": "néther\u0007", "chunks": 1}],
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


__all__ = ["ONLINE", "SEGMENTS", "first_seq", "is_live", "time"]
