#!/usr/bin/env python3
"""Test vectors for the player-experience telemetry's pure functions, from the Python implementation (the
reference for the Rust port of scripts/telemetry.py, v1.4.0).

Writes tests/telemetry_parity/vectors/*.json: inputs and Python's outputs for `clean_text`, `parse_hot`
/ `merge_hot`, `parse_snapshot`, `_fps`, `_median` / `_r`, `summarize_server` / `summarize_players`,
`lag_events`, `Sessions.feed`, `parse_show`, `consumed_cpu_ms`, `Overhead` (journal + samples), `read_stats`,
`segment_bytes` (the decompressed content), segment names and retention (`Segments.write` / `prune`),
`in_window` / `quiet`, `Pacer` and `Backfill.run_once` decision traces (with an injected clock: the back-off
ladder and the row give-up the black-box scenarios can't reach in real time), `verdicts_tsv`, `Messages`
(rate limits and messages.tsv: not reachable from the sidecar, whose player_message runs in the agent),
`load_identity` / `save_identity` and `Config.from_env`, with their Unicode and float edge cases.

    python scripts/telemetry_vectors.py           # rewrite the files
    python scripts/telemetry_vectors.py --check   # exit 1 if a file is stale (tests/telemetry_parity/
                                                  # test_telemetry_vectors.py runs the same comparison)

Files marked "unicode_sensitive" depend on the interpreter's Unicode tables (str.isspace / isprintable):
compared only on a Python with the Unicode version they were generated with. Floats the JSON can't hold are
{"float": "NaN" | "Infinity" | "-Infinity"} (parity_vectors.plain). Entries under "python_raises" are inputs
on which the Python reference raises (reported as bugs, not a contract to reproduce).
"""

from __future__ import annotations

import argparse
import contextlib
import gzip
import json
import os
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import parity_vectors  # noqa: E402
import telemetry as t  # noqa: E402

OUT = HERE.parent / "tests" / "telemetry_parity" / "vectors"
plain, outcome = parity_vectors.plain, parity_vectors.outcome
T0 = 1_768_471_200_000  # 2026-01-15T10:00:00Z (ms), as tests/telemetry_parity
UA = "aaaaaaaa-1111-4111-8111-111111111111"
UB = "bbbbbbbb-2222-4222-8222-222222222222"
UC = "cccccccc-3333-4333-8333-333333333333"


def vec(
    name: str,
    data: Any,  # noqa: ANN401
    *,
    unicode_sensitive: bool = False,
    doc: str = "",
) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {"generator": "scripts/telemetry_vectors.py", "doc": doc}
    if unicode_sensitive:
        meta["unicode_sensitive"] = True
        meta["unidata_version"] = unicodedata.unidata_version
    return name, {"meta": meta, "vectors": plain(data)}


def snap_json(seq: int, ts: int, pl: list[dict[str, Any]] | None = None, **srv: Any) -> dict[str, Any]:  # noqa: ANN401
    base: dict[str, Any] = {
        "tps": 20.0,
        "ticks": 200,
        "mean": 12.5,
        "p50": 11.0,
        "p95": 20.0,
        "p99": 30.0,
        "p999": 40.0,
        "max": 45.0,
        "spikes": [],
        "scanned": 10,
        "worlds": [
            {
                "n": "world",
                "chunks": 400,
                "entities": 300,
                "tiles": 50,
                "ticking": 40,
                "players": len(pl or []),
            }
        ],
        "hot": [],
    }
    base.update(srv)
    return {"v": 1, "seq": seq, "ts": ts, "win_ms": 10000, "srv": base, "pl": pl or []}


def pl(u: str, n: str, join: int, **kw: Any) -> dict[str, Any]:  # noqa: ANN401
    p: dict[str, Any] = {
        "u": u,
        "n": n,
        "ping": 40,
        "brand": "vanilla",
        "view": 10,
        "send": 10,
        "loc": "en_us",
        "w": "world",
        "cx": 0,
        "cz": 0,
        "ent": 10,
        "glide": False,
        "tips": True,
        "join": join,
    }
    p.update(kw)
    return p


def parsed(*snaps: dict[str, Any]) -> list[dict[str, Any]]:
    out = [t.parse_snapshot(json.dumps(s).encode()) for s in snaps]
    assert all(out)
    return out  # type: ignore[return-value]


# --- text ---------------------------------------------------------------------------------------------------

TEXTS = [
    "",
    "plain",
    "  lead and trail  ",
    "tab\there",
    "new\nline\r\n",
    "unit\x1fsep",
    "nul\x00byte",
    "bell\x07",
    "del\x7f",
    "c1\x85next",
    "nbsp\u00a0x",
    "line\u2028sep",
    "para\u2029sep",
    "zwsp\u200bx",
    "bom\ufeffx",
    "soft\u00adhyphen",
    "ideo\u3000space",
    "caf\u00e9",
    "\u2713 check",
    "emoji \U0001f600",
    "combining e\u0301",
    "rtl \u202eevil",
    "private \ue000",
    "unassigned \U000e0080",
    "a" * 250,
    "\u00e9" * 250,
    "  many    spaces\t\t and\n\nlines  ",
    "x\u2000y\u200az",
    42,
    None,
    3.5,
    ["list"],
    "fffe \ufffe",
]


def text_vectors() -> dict[str, Any]:
    not_printable = [cp for cp in range(0x110000) if not chr(cp).isprintable() and not chr(cp).isspace()]
    return {
        "clean_text": [
            {"in": x, "limit": lim, "out": t.clean_text(x, lim)} for x in TEXTS for lim in (200, 8)
        ],
        "isspace_ranges": parity_vectors.ranges([cp for cp in range(0x110000) if chr(cp).isspace()]),
        "not_printable_ranges": parity_vectors.ranges(not_printable),
        "join": [
            {"in": x, "out": t._join(x)}
            for x in (
                ["a", "", "b\tc"],
                ["  ", "x"],
                [str(i) for i in range(12)],
                "not a list",
                None,
                [1, 2.5, None],
            )
        ],
    }


# --- snapshots ----------------------------------------------------------------------------------------------


def snapshot_vectors() -> dict[str, Any]:
    good = snap_json(
        7,
        T0 + 5000,
        [pl(UA, "Alice", T0 - 1000, fps=120)],
        spikes=[[T0 + 1000, 55.5]],
        hot=[{"w": "world", "cx": 1, "cz": 2, "ent": 30, "tiles": 4, "vil": 1, "items": 2}],
    )
    cases: list[tuple[str, Any]] = [
        ("good", good),
        ("not json", "{oops"),
        ("not an object", [1, 2]),
        ("v 2", good | {"v": 2}),
        ("v true (bool == 1)", good | {"v": True}),
        ("v 1.0", good | {"v": 1.0}),
        ("ts missing", {k: v for k, v in good.items() if k != "ts"}),
        ("ts float", good | {"ts": 1.5}),
        ("ts true (bool is an int)", good | {"ts": True}),
        ("ts negative", good | {"ts": -5}),
        ("ts huge", good | {"ts": 10**20}),
        ("srv not a dict", good | {"srv": [1]}),
        ("pl not a list", good | {"pl": {"u": UA}}),
        (
            "numbers as strings / bools",
            good
            | {
                "srv": {
                    "tps": "20",
                    "ticks": True,
                    "mean": None,
                    "p50": 1e308,
                    "p99": -0.0,
                    "heap_used_mb": 1e400,
                }
            },
        ),
        ("seq/win floats", good | {"seq": 3.9, "win_ms": "x"}),
        (
            "spikes",
            good
            | {
                "srv": {
                    "spikes": [[1, 2], [True, 3], [1, False], [1.5, 2], [1], "x", [2, "3"], [3, 4.25]]
                    + [[i, i] for i in range(40)]
                }
            },
        ),
        (
            "worlds",
            good
            | {
                "srv": {
                    "worlds": [
                        {
                            "n": "w\u00e9\tx",
                            "chunks": 1.9,
                            "entities": "5",
                            "tiles": None,
                            "ticking": True,
                            "players": -1,
                        },
                        {"n": 5},
                        "x",
                    ]
                    + [{"n": f"w{i}"} for i in range(20)]
                }
            },
        ),
        (
            "players",
            good
            | {
                "pl": [
                    pl(
                        UA,
                        "Alice",
                        5,
                        ping="40",
                        fps=-1,
                        glide="yes",
                        tips=0,
                        ent=1.9,
                        cx=-2.5,
                        brand="Lunar\tClient\x00",
                        loc="x" * 40,
                        w="w\u2028",
                    ),
                    pl(UB, "Bob", 6, fps=10001.7, tips=False, glide=True),
                    pl(UB.upper(), "Upper", 1),
                    pl(UC, "Bad Name", 1),
                    pl(UC, "x" * 17, 1),
                    pl(UC, "", 1),
                    pl(UC + " ", "Trail", 1),
                    pl(UC, "Caf\u00e9", 1),
                    "not a dict",
                    {"u": UC, "n": "Min"},
                ]
                + [pl(UC, f"P{i}", 1) for i in range(600)]
            },
        ),
        ("unicode digits in names", good | {"pl": [pl(UC, "\u0661\u0662", 1)]}),
    ]
    snapshots = []
    for label, raw in cases:
        data = raw.encode() if isinstance(raw, str) else json.dumps(raw).encode()
        got = outcome(lambda d=data: t.parse_snapshot(d))
        if label == "players":  # 610 entries: the file holds the first 10 and says how the rest are made
            got["ok"]["pl"] = got["ok"]["pl"][:10]
            snapshots.append(
                {
                    "label": label,
                    "json": json.dumps(dict(raw, pl=raw["pl"][:10])),
                    "generated": "pl += [pl(UC, f'P{i}', 1) for i in range(600)]; out: first 10 players "
                    "(the parse keeps the first 500 entries, then drops the invalid ones)",
                    "out": got,
                }
            )
            continue
        snapshots.append({"label": label, "json": data.decode(), "out": got})
    raises = []
    for label, text in (
        ("NaN ping", '{"v":1,"ts":5,"pl":[{"u":"' + UA + '","n":"A","ping":NaN}]}'),
        ("Infinity scanned", '{"v":1,"ts":5,"srv":{"scanned":Infinity}}'),
        ("1e400 world chunks", '{"v":1,"ts":5,"srv":{"worlds":[{"n":"w","chunks":1e400}]}}'),
        ("Infinity hot ent", '{"v":1,"ts":5,"srv":{"hot":[{"w":"w","cx":1,"cz":1,"ent":Infinity}]}}'),
    ):
        raises.append(
            {"label": label, "json": text, "out": outcome(lambda x=text: t.parse_snapshot(x.encode()))}
        )
    hot_cases = [
        [{"w": "world", "cx": 1, "cz": 2, "ent": 5, "tiles": 1, "vil": 0, "items": 3}],
        [
            {"w": "w", "cx": True, "cz": False, "ent": -3, "tiles": 2.9, "vil": "1"},
            {"w": 3, "cx": 1, "cz": 1},
            {"w": "x", "cx": 1.0, "cz": 1},
            "x",
            None,
        ],
        [{"w": "w\u0007orld" + "x" * 80, "cx": i, "cz": -i, "ent": i} for i in range(14)],
        "not a list",
    ]
    many = [
        {
            "srv": {
                "hot": t.parse_hot(
                    [
                        {
                            "w": "w",
                            "cx": i % 7,
                            "cz": 0,
                            "ent": (i * 37) % 11,
                            "tiles": (i * 13) % 5,
                            "vil": i % 3,
                            "items": i,
                        }
                        for i in range(k, k + 8)
                    ]
                )
            }
        }
        for k in (0, 5, 9)
    ]
    tiles_only = [
        {
            "srv": {
                "hot": t.parse_hot(
                    [{"w": "n", "cx": i, "cz": 0, "ent": 0, "tiles": 10 - i} for i in range(10)]
                    + [{"w": "o", "cx": 0, "cz": 0, "ent": 9, "tiles": 0}]
                )
            }
        }
    ]
    zero = [{"srv": {"hot": t.parse_hot([{"w": "z", "cx": i, "cz": 0} for i in range(4)])}}]
    return {
        "parse_snapshot": snapshots,
        "python_raises": raises,
        "fps": [
            {"in": plain(v), "out": t._fps(v)}
            for v in (None, -1, -0.5, 0, 59.9, 144, 10000, 10001, 1e9, "60", True, float("-inf"))
        ],
        "parse_hot": [{"in": c, "out": t.parse_hot(c)} for c in hot_cases],
        "merge_hot": [
            {"in": [s["srv"]["hot"] for s in case], "out": t.merge_hot(case)}  # type: ignore[arg-type]
            for case in (many, tiles_only, zero, [])
        ],
        "constants": {
            "MAX_SNAPSHOT_BYTES": t.MAX_SNAPSHOT_BYTES,
            "SNAP_NAME": t.SNAP_NAME.pattern,
            "UUID": t.UUID.pattern,
            "NAME": t.NAME.pattern,
            "MAX_HOT": t.MAX_HOT,
            "TEXT_MAX": t.TEXT_MAX,
        },
        "snap_names": [
            {"name": n, "match": bool(t.SNAP_NAME.match(n))}
            for n in (
                "snap-0.json",
                "snap-123456789012.json",
                "snap-1234567890123.json",
                "snap--1.json",
                "snap-1.json.tmp",
                "Snap-1.json",
                "snap-\u0661.json",
                "snap-1.json\n",
                ".snap.tmp",
                "snap-01.json",
            )
        ],
    }


# --- minute rows --------------------------------------------------------------------------------------------


def minute_vectors() -> dict[str, Any]:
    medians = [
        [],
        [1.0],
        [3.0, 1.0],
        [1.0, 2.0, 4.0],
        [2.675, 2.675],
        [0.1, 0.2],
        [1e308, 1e308],
        [5, 1, 3, 2],
    ]
    rs = [
        0.125,
        0.135,
        2.675,
        1.005,
        12.345,
        -0.05,
        99.995,
        1e-9,
        19.98,
        80.25,
        151.25,
        52.25,
        52.35,
        112.05,
        0.05,
        1234.5678,
        3.0000000000000004,
    ]
    a, b = (
        pl(UA, "Alice", T0 - 30000, ping=35, fps=118, ent=140, cx=3),
        pl(UB, "Bob", T0 + 2000, ping=80, ent=4),
    )
    srv_cases = [
        parsed(
            snap_json(1, T0 + 5000, [a], p50=11.0, p99=31.5, mean=12.0, ticks=200, heap_used_mb=2000),
            snap_json(
                2,
                T0 + 15000,
                [a | {"ping": 45, "fps": 61}, b],
                p50=2.675,
                p99=48.25,
                mean=14.0,
                ticks=198,
                self_p99_us=80.25,
                budget_skips=2,
            ),
            snap_json(
                3,
                T0 + 25000,
                [a | {"fps": None}, b | {"glide": True}],
                p50=12.5,
                tps=19.87,
                mean=None,
                ticks=199,
                lost_windows=1,
                self_max_us=151.25,
            ),
        ),
        parsed(
            snap_json(
                1,
                T0,
                [],
                tps=None,
                ticks=None,
                mean=None,
                p50=None,
                p95=None,
                p99=None,
                p999=None,
                max=None,
                worlds=[],
            )
        ),
        parsed(snap_json(1, T0, [], ticks=0, mean=50.0), snap_json(2, T0 + 10000, [], ticks=3, mean=0.1)),
        [],
    ]
    names = ["Alice", "Bob", "Carol", "Dave", "Erin"]
    spk = parsed(
        snap_json(
            1,
            T0 + 5000,
            [
                pl(
                    f"{i:08x}-1111-4111-8111-111111111111",
                    n,
                    1,
                    ent=[30, 200, 200, 5, 90][i],
                    glide=i == 2,
                    w=f"w{i}",
                )
                for i, n in enumerate(names)
            ],
            spikes=[[T0 + i * 37, 50.0 + (i % 7) * 10.25] for i in range(40)],
            worlds=[{"n": f"w{i}", "chunks": i, "entities": [50, 400, 10, 400, 300][i]} for i in range(5)],
        ),
        snap_json(
            2,
            T0 + 15000,
            [],
            spikes=[
                [T0 + 12000 + i, ms] for i, ms in enumerate([52.25, 52.35, 112.05, 50.0, 0.05, 1e3, 52.25])
            ],
        ),
        snap_json(3, T0 + 25000, []),
    )
    sess_feed = [
        [parsed(snap_json(1, T0, [pl(UA, "Alice", T0 - 30000)]))[0]],
        parsed(
            snap_json(2, T0 + 10000, [pl(UA, "Alice", T0 - 30000), pl(UB, "Bob", T0 + 8000)]),
            snap_json(3, T0 + 60000, [pl(UB, "Bob", T0 + 8000)]),
        ),
        parsed(snap_json(4, T0 + 70000, [pl(UB, "Bob", T0 + 8000), pl(UC, "Carol", 0)])),
        parsed(snap_json(5, T0 + 200000, [pl(UB, "Bob", T0 + 190000)])),  # a gap; Bob reconnected
        parsed(snap_json(6, T0 + 210000, [pl(UB, "Bob", T0 + 190000)]), snap_json(7, T0 + 220000, [])),
        parsed(snap_json(8, T0 + 215000, [pl(UA, "Alice", 0)])),  # out of order (older than the last)
    ]
    sessions = t.Sessions()
    steps = []
    for feed in sess_feed:
        steps.append(
            {
                "feed": [{"ts": s["ts"], "pl": [[p["u"], p["n"], p["join"]] for p in s["pl"]]} for s in feed],
                "events": sessions.feed(feed),
                "names": sessions.names(),
            }
        )
    return {
        "median": [{"in": m, "out": t._median(m)} for m in medians],
        "r": [{"x": x, "nd": nd, "out": t._r(x, nd)} for x in rs for nd in (1, 2)]
        + [{"x": None, "nd": 2, "out": None}],
        "summarize_server": [
            {"snapshots": [s["srv"] for s in case], "out": t.summarize_server(case)} for case in srv_cases
        ],
        "summarize_players": [
            {"snapshots": [s["pl"] for s in case], "out": t.summarize_players(case)}
            for case in [
                srv_cases[0],
                parsed(
                    snap_json(1, T0, [pl(UB, "bob", 1), pl(UA, "Alice", 1), pl(UC, "_c", 1, ping=3)]),
                    snap_json(2, T0, [pl(UA, "Alice", 1, ping=4)]),
                ),
            ]
        ],
        "lag_events": [
            {
                "snapshots": [{"srv": s["srv"], "pl": s["pl"]} for s in spk],
                "running": ["backup"],
                "out": t.lag_events(spk, ["backup"]),
            }
        ],
        "sessions": {"SESSION_GAP_MS": t.SESSION_GAP_MS, "steps": steps},
    }


# --- overhead -----------------------------------------------------------------------------------------------

SHOW_TEXTS = [
    "Id=minecraft.service\nCPUUsageNSec=5000000000\nMemoryCurrent=3221225472\nMemoryPeak=[not set]\n"
    "IOReadBytes=18446744073709551615\nIOWriteBytes=0\n\nId=mc-agent.service\nCPUUsageNSec=\nMemoryCurrent=42\n",
    "Id=a.service\nX=1=2\nY=-5\nZ= 7\nW=\u0661\u0662\nV=99999999999999999999999\n\n\n\n"
    "Id=\nCPUUsageNSec=1\n\n"
    "no equals line\nId=b.service",
    "",
    "\n\n",
]
CONSUMED = [
    "mc-backup.service: Consumed 1min 2.500s CPU time, 812.3M memory peak.",
    "mc-backup.service: Consumed 2.1s CPU time, 1.5G memory peak, 0B memory swap peak.",
    "mc-health@daily.service: Consumed 350ms CPU time.",
    "x: Consumed 12.75ms CPU time, 512K memory peak.",
    "x: Consumed 3h 2min CPU time.",
    "x: Consumed 59min 59.999s CPU time over 1h 2min elapsed time, 2G memory peak",
    "x: Consumed 1.5us CPU time.",
    "Deactivated successfully.",
    "x: Consumed 7s CPU time, 1.2T memory peak.",
]


def overhead_vectors() -> dict[str, Any]:
    o = t.Overhead()
    journal = [
        {"ident": "systemd", "sd_unit": "mc-backup.service", "msg": CONSUMED[0]},
        {"ident": "systemd", "sd_unit": "mc-health@daily.service", "msg": CONSUMED[2]},
        {"ident": "systemd", "sd_unit": "mc-backup.service", "msg": CONSUMED[1]},
        {"ident": "systemd", "sd_unit": "minecraft.service", "msg": CONSUMED[4]},
        {"ident": "mc-agent", "sd_unit": "mc-prune.service", "msg": CONSUMED[3]},
        {"ident": "systemd", "sd_unit": "mc-evil.service; rm", "msg": CONSUMED[3]},
        {"ident": "systemd", "sd_unit": "mc-prune.service", "msg": CONSUMED[7]},
        {"ident": "systemd", "sd_unit": "MC-UPPER.service", "msg": CONSUMED[3]},
        {"ident": "systemd", "sd_unit": None, "msg": CONSUMED[3]},
    ]
    stats = [
        (
            {
                "minecraft.service": {
                    "CPUUsageNSec": 5_000_000_000,
                    "MemoryCurrent": 3 << 30,
                    "MemoryPeak": None,
                    "IOReadBytes": 1 << 20,
                    "IOWriteBytes": 0,
                }
            },
            1000.0,
        ),
        (
            {
                "minecraft.service": {
                    "CPUUsageNSec": 8_000_000_000,
                    "MemoryCurrent": (3 << 30) + 123456,
                    "MemoryPeak": 18446744073709551615,
                    "IOReadBytes": 3 << 20,
                    "IOWriteBytes": 1000,
                },
                "mc-agent.service": {"CPUUsageNSec": 1},
            },
            1060.0,
        ),
        (
            {
                "minecraft.service": {
                    "CPUUsageNSec": 7_000_000_000,
                    "MemoryCurrent": None,
                    "IOReadBytes": 1,
                    "IOWriteBytes": 1500,
                }
            },
            1060.0,
        ),
        ({"minecraft.service": {"CPUUsageNSec": 7_000_000_333, "IOReadBytes": 2}}, 1090.5),
    ]
    samples = []
    o.observe_journal(journal)
    for k, (st, now) in enumerate(stats):
        if k == 2:
            o.observe_journal(journal[:1])
        samples.append(
            {
                "stats": st,
                "now": now,
                "journal_before": journal if k == 0 else journal[:1] if k == 2 else [],
                "out": o.sample(st, now),
            }
        )
    return {
        "parse_show": [{"in": x, "out": outcome(lambda x=x: t.parse_show(x))} for x in SHOW_TEXTS],
        "consumed_cpu_ms": [{"in": m, "out": plain(t.consumed_cpu_ms(m))} for m in CONSUMED],
        "overhead": {"ONESHOT_UNITS": t.ONESHOT_UNITS.pattern, "steps": samples},
        "unit_stats_argv": [
            "systemctl",
            "show",
            "-p",
            "Id",
            "-p",
            "CPUUsageNSec",
            "-p",
            "MemoryCurrent",
            "-p",
            "MemoryPeak",
            "-p",
            "IOReadBytes",
            "-p",
            "IOWriteBytes",
        ],
    }


# --- segments -----------------------------------------------------------------------------------------------


def segment_vectors() -> dict[str, Any]:
    rows_cases = [
        [
            {
                "id": "0badc0de:1",
                "ts": T0,
                "seq": 1,
                "f": 0.1,
                "s": "caf\u00e9 \u2713",
                "n": None,
                "b": True,
                "e": {},
                "l": [1, 2.5, -0.0, 1e16, 1e-7, 123456789012345678],
            }
        ],
        [{"id": f"0badc0de:{i}", "ts": T0 + i * 60000, "seq": i} for i in range(3)],
        [],
    ]
    content = [{"rows": r, "ndjson": gzip.decompress(t.segment_bytes(r)).decode()} for r in rows_cases]
    names, prunes = [], []
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        for rows, acked, src in (
            (
                [{"id": "abcd0123:5", "ts": T0 + 59999, "seq": 5}, {"id": "abcd0123:6", "ts": T0, "seq": 6}],
                {"abcd0123:6"},
                "abcd0123",
            ),
            ([{"id": "x:0", "ts": 0, "seq": 0}], set(), "00000000"),
            ([{"id": "x:9", "ts": 253402300799000, "seq": 9}], {"x:9"}, "ffffffff"),
        ):
            seg = t.Segments(d / f"n{len(names)}", clock=lambda: 2e9)
            name = seg.write(rows, acked, src)
            names.append(
                {"rows": rows, "acked": sorted(acked), "src": src, "name": name, "state": seg.load()}
            )
        now = 1_800_000_000.0
        for case in (
            {
                "days": 30,
                "cap_mb": 10,
                "files": [
                    ["20251101T0000-0badc0de-1-2", 1000, 40],
                    ["20251201T0000-0badc0de-3-3", 4 << 20, 1],
                    ["20251202T0000-0badc0de-4-4", 4 << 20, 1],
                    ["20251203T0000-0badc0de-5-5", 3 << 20, 0],
                ],
            },
            {
                "days": 1,
                "cap_mb": 200,
                "files": [
                    ["20251101T0000-0badc0de-1-2", 10, 2],
                    ["20251102T0000-0badc0de-1-2", 10, 0.5],
                    ["20251103T0000-0badc0de-1-2", 10, 3],
                ],
            },
            {"days": 30, "cap_mb": 10, "files": [["20251101T0000-0badc0de-1-2", 10 << 20, 0]]},
            {"days": 30, "cap_mb": 10, "files": [["20251101T0000-0badc0de-1-2", (10 << 20) + 1, 0]]},
        ):
            sd = d / f"p{len(prunes)}"
            sd.mkdir()
            state = {}
            for name, size, age_days in case["files"]:  # type: ignore[attr-defined]
                p = sd / f"{name}.ndjson.gz"
                with open(p, "wb") as f:
                    f.truncate(size)
                os.utime(p, (now - age_days * 86400, now - age_days * 86400))
                state[p.name] = {"todo": [], "r2": True}
            state["20250101T0000-0badc0de-9-9.ndjson.gz"] = {"todo": [0], "r2": False}  # deleted by hand
            seg = t.Segments(sd, days=case["days"], cap_mb=case["cap_mb"], clock=lambda: now)  # type: ignore[arg-type]
            seg.save(state)
            dropped = seg.prune()
            prunes.append(
                case
                | {
                    "now": now,
                    "hand_deleted": "20250101T0000-0badc0de-9-9.ndjson.gz",
                    "dropped": dropped,
                    "state_after": seg.load(),
                }
            )
        stats_cases = []
        for raw in (
            {
                "stats": {
                    "minecraft:custom": {
                        "minecraft:play_time": 72000,
                        "minecraft:deaths": 2,
                        "minecraft:walk_one_cm": 5.5,
                        "minecraft:jump": True,
                    }
                }
            },
            {"stats": {"minecraft:custom": []}},
            {"stats": None},
            [1],
            "not json",
        ):
            p = d / "stats.json"
            p.write_text(raw if isinstance(raw, str) else json.dumps(raw))
            stats_cases.append({"file": p.read_text(), "out": outcome(lambda p=p: t.read_stats(p))})
    return {
        "segment_bytes": content,
        "segment_name": t.SEGMENT_NAME.pattern,
        "write": names,
        "prune": prunes,
        "read_stats": {"STAT_KEYS": t.STAT_KEYS, "cases": stats_cases},
    }


# --- quiet time, Pacer, Backfill ----------------------------------------------------------------------------


def backfill_vectors() -> dict[str, Any]:
    tm = time.struct_time
    windows = ["", "02:00-06:00", "22:00-02:00", "00:00-00:00", "06:00-06:01", "23:59-00:00"]
    times = [(0, 0), (1, 59), (2, 0), (5, 59), (6, 0), (21, 59), (22, 0), (23, 59)]
    in_window = [
        {"window": w, "hh": h, "mm": m, "out": t.in_window(w, tm((2026, 1, 15, h, m, 0, 3, 15, 0)))}
        for w in windows
        for h, m in times
    ]
    latest = [
        None,
        {"pl": [], "srv": {"p99": 10.0}},
        {"pl": [], "srv": {"p99": 50.0}},
        {"pl": [], "srv": {"p99": None}},
        {"pl": [{"u": UA}], "srv": {"p99": 1.0}},
        {"pl": [], "srv": {}},
    ]
    quiet = [
        {"latest": lt, "window": w, "hh": h, "out": t.quiet(lt, w, tm((2026, 1, 15, h, 0, 0, 3, 15, 0)))}
        for lt in latest
        for w in ("", "02:00-06:00")
        for h in (3, 12)
    ]

    clock = {"now": 1000.0}
    pacer = t.Pacer(clock=lambda: clock["now"])
    script: list[tuple[str, Any]] = (
        [
            ("failed", ("POST /ingest/experience: HTTP 503", 3)),
            ("failed", ("POST /ingest/experience: HTTP 503", 1)),
        ]
        + [("tick", 10.0), ("ready", None), ("tick", 50.0), ("ready", None)]
        + [("failed", ("POST /ingest/experience: HTTP 500", 1))] * 4
        + [
            ("failed", ("POST /ingest/experience: HTTP 429", 3)),
            ("failed", ("URLError", 2)),
            ("failed", ("HTTP 5000", 2)),
            ("failed", ("PUT /ingest/segment: HTTP 502", 0)),
        ]
        + [("failed", ("POST /ingest/experience: HTTP 503", 2))]
        + [("succeeded", None)] * 21
        + [("failed", ("HTTP 503", 1))] * 12
    )
    steps = []
    for op, arg in script:
        if op == "tick":
            clock["now"] += arg
            out: Any = None
        elif op == "ready":
            out = pacer.ready()
        elif op == "succeeded":
            out = pacer.succeeded()
        else:
            out = pacer.failed(Exception(arg[0]), arg[1])
        steps.append(
            {
                "op": op,
                "arg": list(arg) if isinstance(arg, tuple) else arg,
                "now": clock["now"],
                "out": out,
                "size": pacer.size,
                "failures": pacer.failures,
                "row_failures": pacer.row_failures,
                "successes": pacer.successes,
                "retry_at": pacer.retry_at,
            }
        )
    return {
        "in_window": in_window,
        "quiet": quiet,
        "constants": {
            "BACKFILL_BATCH": t.BACKFILL_BATCH,
            "BACKFILL_BACKOFF_S": t.BACKFILL_BACKOFF_S,
            "BACKFILL_BACKOFF_MAX_S": t.BACKFILL_BACKOFF_MAX_S,
            "BACKFILL_ROW_GIVE_UP": t.BACKFILL_ROW_GIVE_UP,
            "BACKFILL_GROW_AFTER": t.BACKFILL_GROW_AFTER,
            "SERVER_ERROR": t.SERVER_ERROR.pattern,
        },
        "pacer": {"start": 1000.0, "steps": steps},
        "run_once": backfill_runs(),
    }


def backfill_runs() -> list[dict[str, Any]]:
    """Backfill.run_once over segments written by Segments.write, with scripted replies (None = accepted, else
    the error text) and an injected clock that each sleep advances: the requests, the marks, the counters."""
    cases: list[dict[str, Any]] = [
        {
            "label": "all accepted, then the segment PUT",
            "segments": [[5, [1]], [2, []]],
            "r2": [False, True],
            "rows": [],
            "puts": [],
            "may_run": None,
            "max_requests": 1000,
        },
        {
            "label": "halve, then back off",
            "segments": [[4, []]],
            "r2": [True],
            "rows": ["HTTP 503", None, "HTTP 503"],
            "puts": [],
            "may_run": None,
            "max_requests": 1000,
        },
        {
            "label": "give up a row after BACKFILL_ROW_GIVE_UP 5xx (rounds after each back-off)",
            "segments": [[3, []]],
            "r2": [True],
            "rows": ["HTTP 503"] * 7,
            "puts": [],
            "may_run": None,
            "max_requests": 1000,
            "rounds": 6,
        },
        {
            "label": "a 4xx never gives up (backs off forever)",
            "segments": [[2, []]],
            "r2": [True],
            "rows": ["HTTP 400"] * 8,
            "puts": [],
            "may_run": None,
            "max_requests": 1000,
            "rounds": 7,
        },
        {
            "label": "may_run false after two requests (a player joined)",
            "segments": [[9, []]],
            "r2": [True],
            "rows": [],
            "puts": [],
            "may_run": [True, True, False],
            "max_requests": 1000,
        },
        {
            "label": "max_requests",
            "segments": [[20, []]],
            "r2": [False],
            "rows": [],
            "puts": [],
            "may_run": None,
            "max_requests": 4,
        },
        {
            "label": "segment PUT fails: backs off, rows stay done",
            "segments": [[1, []]],
            "r2": [False],
            "rows": [],
            "puts": ["HTTP 503"],
            "may_run": None,
            "max_requests": 1000,
        },
        {
            "label": "unreadable segment: given up",
            "segments": [[2, []]],
            "r2": [False],
            "corrupt": [0],
            "rows": [],
            "puts": [],
            "may_run": None,
            "max_requests": 1000,
        },
    ]
    out = []
    for case in cases:
        with tempfile.TemporaryDirectory() as tmp:
            clock: dict[str, float] = {"now": 5000.0}
            seg = t.Segments(Path(tmp), clock=lambda c=clock: c["now"])
            names = []
            for k, (n, acked_idx) in enumerate(case["segments"]):
                rows = [
                    {"id": f"0badc0de:{k * 100 + i}", "ts": T0 + (k * 100 + i) * 60000, "seq": k * 100 + i}
                    for i in range(n)
                ]
                name = seg.write(rows, {rows[i]["id"] for i in acked_idx}, "0badc0de")
                assert name
                seg.mark(name, r2=case["r2"][k])
                names.append(name)
            for k in case.get("corrupt", []):
                (Path(tmp) / names[k]).write_bytes(b"not gzip")
            requests: list[Any] = []
            replies_rows, replies_put = list(case["rows"]), list(case["puts"])
            may = list(case["may_run"]) if case["may_run"] is not None else None

            def send_rows(
                rows: list[dict[str, Any]], rq: list[Any] = requests, rr: list[Any] = replies_rows
            ) -> None:
                rq.append(
                    {
                        "rows": [r["id"] for r in rows],
                        "backfill": all(r.get("backfill") is True for r in rows),
                    }
                )
                err = rr.pop(0) if rr else None
                if err:
                    raise RuntimeError(err)

            def send_segment(
                name: str, data: bytes, rq: list[Any] = requests, rp: list[Any] = replies_put
            ) -> None:
                rq.append({"segment": name, "rows": len(t.read_segment(Path(tmp) / name))})
                err = rp.pop(0) if rp else None
                if err:
                    raise RuntimeError(err)

            def may_run(m: list[Any] | None = may) -> bool:
                return m.pop(0) if m else m is None

            logs: list[str] = []
            pacer = t.Pacer(clock=lambda c=clock: c["now"])
            rounds = []
            for _ in range(case.get("rounds", 1)):
                bf = t.Backfill(
                    seg,
                    send_rows,
                    send_segment,
                    may_run,
                    pause_s=2.0,
                    sleep=lambda x, c=clock: c.__setitem__("now", c["now"] + x),
                    pacer=pacer,
                    log=logs.append,
                )
                done = bf.run_once(case["max_requests"])
                rounds.append(
                    {"now": clock["now"], "done": done, "retry_at": pacer.retry_at, "size": pacer.size}
                )
                clock["now"] = max(clock["now"], pacer.retry_at)  # the next round once the back-off expired
            out.append(
                {k: v for k, v in case.items()}
                | {"names": names, "requests": requests, "rounds": rounds, "log": logs, "state": seg.load()}
            )
    return out


# --- verdicts and messages ----------------------------------------------------------------------------------


def verdicts_vectors() -> dict[str, Any]:
    cases = [
        {
            "server": {
                "verdict": "fine-and-more",
                "headline": "Server\tsmooth\nall",
                "reasons": ["a", "", "b\x1fc"] + [f"r{i}" for i in range(9)],
                "tips": ["caf\u00e9", 42, None],
            },
            "players": {
                UA: {"verdict": "laggy", "headline": "x" * 250, "reasons": "no", "tips": ["t"]},
                UB: "not a dict",
                "not-a-uuid": {"verdict": "ok"},
                UC.upper(): {"verdict": "ok"},
            },
        },
        {},
        {"server": "nope", "players": None},
        {"players": {UA: {}}},
    ]
    msgs: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory() as tmp:
        m = t.Messages(Path(tmp), per_player_s=1800, broadcast_s=600, join_grace_s=60, per_hour=3)
        online = {
            UA: {"tips": True, "join": 1_000_000},
            UB: {"tips": False, "join": 0},
            UC: {"join": 1_950_000},
        }
        for to, kind, now, text in (
            (UA, "tip", 1000.0, "hello\tthere"),
            (UA, "tip", 1500.0, "again"),
            (UB, "tip", 1500.0, "x"),
            (UC, "tip", 2000.0, "early"),
            (UC, "join", 2000.0, "welcome \u2028 line"),
            ("*", "broadcast", 2001.0, "all"),
            ("*", "broadcast", 2300.0, "all again"),
            (UA, "tip", 2900.0, "limit?"),
            ("nobody", "tip", 3000.0, "x"),
            (UA, "tip", 4700.0, "after the hour " + "y" * 300),
        ):
            why = m.check(to, kind, now, online)
            if why is None:
                m.add(f"m{len(msgs)}", to, kind, text, "hover\ttext", now)
            msgs.append(
                {
                    "to": to,
                    "kind": kind,
                    "now": now,
                    "text": text,
                    "refused": why,
                    "messages_tsv": (Path(tmp) / "messages.tsv").read_text() if why is None else None,
                }
            )
    return {
        "verdicts_tsv": [
            {"verdicts": v, "now_ms": 1768471200123, "out": t.verdicts_tsv(v, 1768471200123)} for v in cases
        ],
        "messages": {
            "limits": {
                "per_player_s": 1800,
                "broadcast_s": 600,
                "join_grace_s": 60,
                "per_hour": 3,
                "EXPIRE_S": t.Messages.EXPIRE_S,
            },
            "online": {
                UA: {"tips": True, "join": 1_000_000},
                UB: {"tips": False, "join": 0},
                UC: {"join": 1_950_000},
            },
            "steps": msgs,
        },
    }


# --- identity and settings ----------------------------------------------------------------------------------


def identity_vectors() -> dict[str, Any]:
    loads = []
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "telemetry-id.json"
        for text in (
            '{"src": "0badc0de", "seq": 42}',
            '{"src": "0badc0de", "seq": "7"}',
            '{"src": "0badc0de", "seq": 7.9}',
            '{"src": "0badc0de", "seq": true}',
            '{"src": "0BADC0DE", "seq": 1}',
            '{"src": "0badc0d", "seq": 1}',
            '{"src": "0badc0de"}',
            '{"seq": 1}',
            "[]",
            "not json",
            "",
            '{"src": 12345678, "seq": 1}',
            '{"src": "0badc0de", "seq": -3}',
            '{"src": "0badc0de", "seq": " 5 "}',
            None,
        ):
            if text is None:
                p.unlink(missing_ok=True)
            else:
                p.write_text(text)
            src, seq = t.load_identity(p)
            fresh = not (text and src in text)
            loads.append(
                {
                    "file": text,
                    "out": {"new_random_src": True, "seq": seq} if fresh else {"src": src, "seq": seq},
                }
            )
        t.save_identity(p, "0badc0de", 43)
        saved = p.read_text()
    envs = [
        {},
        {
            "SEGMENT_DAYS": "0",
            "SEGMENT_CAP_MB": "5",
            "SEGMENT_S": "10",
            "BACKFILL_PAUSE_S": "-1",
            "BACKFILL_WINDOW": "2:00-6:00",
        },
        {
            "SEGMENT_DAYS": "7",
            "SEGMENT_CAP_MB": "50",
            "SEGMENT_S": "60",
            "BACKFILL_PAUSE_S": "0.5",
            "BACKFILL_WINDOW": "02:00-06:00",
            "TELEMETRY_DIR": "/x",
            "VERDICT_DIR": "",
            "SEGMENT_DIR": "/s",
        },
        {"BACKFILL_WINDOW": "25:00-26:99"},
        {"SEGMENT_S": "soon"},
        {"SEGMENT_DAYS": " 3 "},
        {"SEGMENT_S": "\u0663\u0660"},
    ]
    configs = []
    for env in envs:

        def cfg(env: dict[str, str] = env) -> dict[str, Any]:
            c = t.Config.from_env(env, Path("/opt/minecraft/server"), "world")
            return {k: str(v) if isinstance(v, Path) else v for k, v in vars(c).items()}

        configs.append({"env": env, "out": outcome(cfg)})
    return {
        "load_identity": loads,
        "save_identity": {"src": "0badc0de", "seq": 43, "file": saved},
        "config_from_env": configs,
    }


# --- the files ----------------------------------------------------------------------------------------------


def build() -> dict[str, dict[str, Any]]:
    files = [
        vec(
            "text",
            text_vectors(),
            unicode_sensitive=True,
            doc="clean_text (str.isspace -> space, not isprintable -> dropped, whitespace collapsed, cut), "
            "the "
            "Unicode tables it relies on, _join (U+001F-separated, 8 items)",
        ),
        vec(
            "snapshot",
            snapshot_vectors(),
            unicode_sensitive=True,
            doc="parse_snapshot, _fps, parse_hot, merge_hot, SNAP_NAME; "
            "python_raises: NaN/Infinity reach int()",
        ),
        vec(
            "minute",
            minute_vectors(),
            doc="_median, _r, summarize_server, summarize_players, lag_events, Sessions",
        ),
        vec(
            "overhead",
            overhead_vectors(),
            doc="parse_show, consumed_cpu_ms, Overhead.observe_journal + sample",
        ),
        vec(
            "segments",
            segment_vectors(),
            doc="segment_bytes (decompressed), Segments.write names and state, prune, read_stats",
        ),
        vec(
            "backfill",
            backfill_vectors(),
            doc="in_window, quiet, Pacer decisions, Backfill.run_once traces (injected clock)",
        ),
        vec(
            "verdicts",
            verdicts_vectors(),
            unicode_sensitive=True,
            doc="verdicts_tsv; Messages.check / add (messages.tsv) with their rate limits",
        ),
        vec("identity", identity_vectors(), doc="load_identity / save_identity, Config.from_env clamps"),
    ]
    return dict(files)


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=True) + "\n"


def stale(files: dict[str, dict[str, Any]]) -> list[str]:
    """Vector files that differ from `files` (skipping unicode_sensitive ones made with other Unicode
    tables)."""
    out = []
    for name, data in files.items():
        path = OUT / f"{name}.json"
        current = path.read_text() if path.exists() else None
        if current == render(data):
            continue
        with contextlib.suppress(ValueError):
            old = json.loads(current or "{}").get("meta", {})
            if (
                data["meta"].get("unicode_sensitive")
                and current
                and old.get("unidata_version") != unicodedata.unidata_version
            ):
                continue
        out.append(name)
    extra = sorted(p.stem for p in OUT.glob("*.json") if p.stem not in files) if OUT.exists() else []
    return out + extra


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if a vector file is stale")
    args = ap.parse_args(argv)
    files = build()
    if args.check:
        bad = stale(files)
        if bad:
            print(
                f"stale vector files: {', '.join(bad)}: run python scripts/telemetry_vectors.py",
                file=sys.stderr,
            )
            return 1
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        path = OUT / f"{name}.json"
        text = render(data)
        if not path.exists() or path.read_text() != text:
            path.write_text(text)
            print(f"wrote {path.relative_to(HERE.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
