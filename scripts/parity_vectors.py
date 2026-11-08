#!/usr/bin/env python3
"""Test vectors for mc-agent's pure functions, from the Python implementation (the reference).

Writes tests/agent_parity/vectors/*.json: inputs and the Python agent's outputs for the functions a second
implementation (the Rust port, v1.4.0) must reproduce exactly: the console allowlist, `_one_line`, the log
scrubber, the command envelope check (`verify`), players.yml, `merge_sync`, `token_shape`/TOKEN_RE and the
token expiry parse, the rejected-logins feed, the alert texts, the metrics and journal parsers, the world
library helpers, the WebSocket framing, `json.dumps` byte-exactness, `round()`, the Unicode properties the
agent relies on (`isspace`, `\\d`, categories, NFKC, code-point lengths) and the agent's constants.

    python scripts/parity_vectors.py           # rewrite the files
    python scripts/parity_vectors.py --check   # exit 1 if a file is stale (tests/agent_parity/test_vectors.py
                                               # runs the same comparison)

Files marked "unicode_sensitive" depend on the interpreter's Unicode tables (unicodedata.unidata_version):
the test compares them only on a Python with the version they were generated with.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Callable
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import alerts  # noqa: E402
import health  # noqa: E402
import mc_agent  # noqa: E402
import mc_panel  # noqa: E402
import worlds  # noqa: E402

OUT = HERE.parent / "tests" / "agent_parity" / "vectors"
KEY = b"CommandKeyParity-0123456789abcdefABCDEF"
NOW = 1_794_100_000.0  # a fixed "now" (s) for verify

# The console corpus (shared with tests/agent_parity/scenarios.py, which sends it to the live agent).
CONSOLE_CORPUS = [
    "list",
    "/list uuids",
    "LIST",
    "  list  ",
    "list extra",
    "say Hello World",
    "say hi @e",
    "say hi @a[distance=..5]",
    "me waves",
    "tell Bob hi there",
    "tell @a hi",
    "msg Bob look @p",
    "w Bob x",
    "tell @e hi",
    "kick Bob being rude",
    "kick @a",
    "ban Griefer2 x",
    "ban-ip 203.0.113.7 spam",
    "ban-ip 2001:db8::1",
    "pardon Bob",
    "pardon-ip 203.0.113.7",
    "pardon-ip \u0662\u0660\u0663.0.113.7",
    "time add 100",
    "time add \u0663",
    "time set \u0661\u0660\u0660",
    "time add \u0967",
    "weather clear \u06f5",
    "time add \uff13",
    "time set day",
    "time query daytime",
    "weather rain 600",
    "weather rain 600x",
    "\uff54\uff49\uff4d\uff45\u3000\uff51\uff55\uff45\uff52\uff59\u3000\uff44\uff41\uff59",
    "op Bob",
    "deop Bob",
    "stop",
    "restart",
    "execute run op Bob",
    "minecraft:op Bob",
    "bukkit:reload",
    "reload",
    "rl",
    "save-off",
    "function x:y",
    "return 1",
    "whitelist add Bob",
    "WHITELIST Remove Bob",
    "whitelist list",
    "whitelist off",
    "say a\x1cb",
    "say a\x1fb",
    "say a\u2028b",
    "say a\u00a0b",
    "say a\u200bb",
    "say a\tb",
    "say a\u3000b",
    "say \U0001d407\U0001d422",
    "say " + "\ufdfa" * 15,
    "say " + "x" * 252,
    "say " + "x" * 253,
    "",
    "   ",
    "/",
    "gamerule keepInventory",
    "gamerule keepInventory true",
    "difficulty hard",
    "difficulty",
    "spark health --memory",
    "spark profiler",
    "chunky progress",
    "chunky start",
    "version Paper",
    "help me",
    "seed",
    "tps",
    "mspt",
    "worldborder get",
    "worldborder set 10",
    "plugins extra",
    "save-all flush",
    "banlist ips",
    "ban Bo",
    "kick Bob \u00e9t\u00e9",
    "say \ud7ff",
    "say \ue000",
    "time add 1\u0301",
]
# More allowlist edges for the vectors only (no RCON needed): every head and alias, digits of every script,
# selectors, IPs, durations, the 256-character limits before and after NFKC.
CONSOLE_EXTRA = [
    *(f"{h}" for h in sorted(mc_agent.CONSOLE_ALLOWLIST)),
    *(f"{h} x" for h in sorted(mc_agent.CONSOLE_ALLOWLIST)),
    *(f"{h}" for h in sorted(mc_agent.REFUSED_HEADS)),
    "time add 1d",
    "time add 12s",
    "time add 1t",
    "time add 1234567890",
    "time add 123456789",
    "time set midnight",
    "weather thunder 5d",
    "weather snow",
    "difficulty PEACEFUL",
    "version a.b-c_d",
    "version a/b",
    "help x" * 6,
    "ban-ip 1.2.3",
    "ban-ip ::",
    "ban-ip fe80::1%eth0",
    "pardon-ip 192.0.2.4 extra",
    "pardon-ip Bob",
    "ban-ip \uff11\uff19\uff12.\uff10.\uff12.\uff14",
    "pardon-ip \u0661.\u0662.\u0663.\u0664",
    "time add \u0e53",
    "time add \u1813",
    "time add \U0001d7d1",
    "weather rain \u00b2",
    "time add \u2463",
    "say \u00b2",
    "tell Bob @a",
    "tell Bob hi @a[x]",
    "tell Bob @s",
    "me @n",
    "say @x",
    "say @",
    "say a@e",
    "msg @r @p",
    "kick B\u00f6b",
    "kick Bob\u200d",
    "say \u0085x",
    "say x\u000b",
    "say x\u000c",
    "say \u180e",
    "say \u2060",
    "say \u00ad",
    "say \ufeff",
    "say \U000e0001",
    "say \U0010fffd",
    "say \u0378",
    "say \u1680",
    "say \u202f",
    "say \u205f",
    "\u3000list",
    "list\u3000",
    "\uff0flist",
    "LIST UUIDS",
    "/" * 3 + "list",
    "/ list",
    "say \u2126",
    "say \ufb01",
    "say \u2460",
    "say \u00bd",
    "say \u1e9b\u0323",
    "gamerule a:b.c_d",
    "gamerule " + "a" * 65,
    "spark health --memory --network",
    "spark health --cpu",
    "spark gc",
    "chunky selection",
    "say " + "\ufdfa" * 13 + "x" * 9,
    "say " + "\ufdfa" * 14,
    "x" * 257,
    "say " + "\u00e9" * 252,
    "say " + "e\u0301" * 126,
    "say " + "e\u0301" * 127,
]

ONE_LINE = [
    None,
    "",
    "plain",
    "  padded  ",
    "a\nb",
    "a\r\nb",
    "a\tb",
    "a\x0bb",
    "a\x0cb",
    "a\x1cb",
    "a\x1db",
    "a\x1eb",
    "a\x1fb",
    "a\x85b",
    "a\xa0b",
    "a\u1680b",
    "a\u2000b",
    "a\u2028b",
    "a\u2029b",
    "a\u202fb",
    "a\u205fb",
    "a\u3000b",
    "a\x00b",
    "a\x07b",
    "a\x7fb",
    "a\u200bb",
    "a\u200db",
    "a\ufeffb",
    "a\ud7ffb",
    "a\ue000b",
    "a\U000f0000b",
    "a\u0378b",
    "\ufdfa",
    "\uff21\uff22",
    "\u2126",
    "e\u0301",
    "\u1e9b\u0323",
    "x" * 150,
    "\U0001f600" * 120,
    "\u00e9" * 120,
    "multiple   spaces\n\n\nand lines",
    42,
    True,
    ["list"],
]

SCRUB_KNOWN = [
    "AgentTokenParity-0123456789abcdefABCDEF",
    "CommandKeyParity-0123456789abcdefABCDEF",
    "rconPassParity42",
    "short",
    "https://127.0.0.1:1/api/webhooks/123/parity-webhook-secret",
    "rconPass",
]
SCRUB_INPUTS = [
    "nothing secret here",
    "agent token AgentTokenParity-0123456789abcdefABCDEF in a line",
    "rcon.password=rconPassParity42",
    "rconPassParity42rconPass",
    "short is short",
    "Authorization: Bearer abc.def-ghi",
    "BEARER token",
    "xbearer abc",
    "_bearer abc",
    "auth basic dXNlcjpwYXNz",
    "basic short",
    "Basic abc",
    "RESTIC_PASSWORD=hunter2hunter2",
    "HEARTBEAT_TOKEN='quoted value' ok",
    'json {"api_key": "s3cr3t-value", "x_secret":"v2"} end',
    "my.db.pass: plain",
    "PASS=x",
    "passwd = 'a b'",
    "pwd:\tv",
    "api-key=1",
    "apikey:2",
    "API_KEY=3",
    "aws_access_key_id=AKIA",
    "foo_key=bar",
    "key=bar",
    "monkey=bar",
    "x_key= spaced",
    "x_key=",
    "token_bucket=5 tokenizer=no",
    "tokens=5",
    "secret=a&b=c",
    "secret=a}b",
    "secret=a]b",
    "secret=a;b",
    "secret=a,b",
    "AKIA0189ABCD0189EFGH",
    "xAKIA0189ABCD0189EFGH",
    "AKIA0189ABCD0189EFGHQ",
    "ASIA0189QRST0189UVWX",
    "gh ghp_" + "a" * 36,
    "ghs_" + "b" * 19,
    "ghs_" + "b" * 20,
    "github_pat_" + "c" * 30,
    "tskey-auth-kAbC123-xyz",
    "tskey-",
    "hook https://discord.com/api/webhooks/1/abc x",
    "https://ptb.discordapp.com/api/webhooks/9/z",
    "HTTPS://DISCORD.COM/API/WEBHOOKS/1/A",
    "url https://user:pw@example.com/x",
    "ftp://u:p@h/",
    "redis://:pw@h",
    "x+y://a:b@c",
    "1ab://a:b@c",
    "mailto:a@b",
    "user:pw@host",
    "\u00e9t\u00e9 Bearer \u00e9\u00e9\u00e9 ok",
    "Bearer\u00a0xyz",
    "Bearer\u3000xyz",
    "token\u2028= a",
    "TOKEN=\u00e9\u00e9",
    "\u0130_token=x",
    "\u212a_KEY=x",
    "a" * 50 + "_password=" + "b" * 50,
    "multi\nline token=a\nsecret=b",
    "known twice AgentTokenParity-0123456789abcdefABCDEF AgentTokenParity-0123456789abcdefABCDEF",
]


def vec(
    name: str,
    data: Any,  # noqa: ANN401
    *,
    unicode_sensitive: bool = False,
    doc: str = "",
) -> tuple[str, dict[str, Any]]:
    meta: dict[str, Any] = {"generator": "scripts/parity_vectors.py", "doc": doc}
    if unicode_sensitive:
        meta["unicode_sensitive"] = True
        meta["unidata_version"] = unicodedata.unidata_version
    return name, {"meta": meta, "vectors": data}


def plain(v: Any) -> Any:  # noqa: ANN401
    """NaN and the infinities (which Python's json accepts) as {"float": "NaN"|"Infinity"|"-Infinity"}."""
    if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
        return {"float": "NaN" if v != v else ("Infinity" if v > 0 else "-Infinity")}
    if isinstance(v, dict):
        return {k: plain(x) for k, x in v.items()}
    if isinstance(v, list | tuple):
        return [plain(x) for x in v]
    return v


def outcome(fn: Callable[[], Any]) -> dict[str, Any]:
    """{"ok": result} or {"error": message, "type": the Python exception (for reading, not for parity)}."""
    try:
        return {"ok": plain(fn())}
    except Exception as e:
        return {"error": str(e), "type": type(e).__name__}


def ranges(cps: list[int]) -> list[list[int]]:
    out: list[list[int]] = []
    for cp in cps:
        if out and out[-1][1] == cp - 1:
            out[-1][1] = cp
        else:
            out.append([cp, cp])
    return out


# --- the vectors ------------------------------------------------------------------------------------------


def console_vectors() -> list[dict[str, Any]]:
    out = []
    for line in [*CONSOLE_CORPUS, *CONSOLE_EXTRA]:
        out.append(
            {
                "input": line,
                "normalise": outcome(lambda line=line: mc_agent.normalise_console(line)),
                "check": outcome(lambda line=line: mc_agent.check_console(line)),
            }
        )
    for bad in (None, 5, ["list"]):
        out.append({"input": bad, "check": outcome(lambda bad=bad: mc_agent.check_console(bad))})
    return out


def verify_vectors() -> list[dict[str, Any]]:
    def sign(payload: str) -> str:
        return mc_agent.sign(KEY, payload)

    def cmd(**f: Any) -> str:  # noqa: ANN401
        base = {
            "id": "c1",
            "action": "status",
            "args": {},
            "login": "a@b",
            "iat": int(NOW * 1000),
            "nonce": "nonce-0001",
        }
        base.update(f)
        return json.dumps({k: v for k, v in base.items() if v is not ...}, separators=(",", ":"))

    cases: list[tuple[str, list[Any]]] = [
        ("ok", [{"payload": cmd(), "sig": sign(cmd())}]),
        ("uppercase_sig", [{"payload": cmd(), "sig": sign(cmd()).upper()}]),
        ("replay", [{"payload": cmd(), "sig": sign(cmd())}, {"payload": cmd(), "sig": sign(cmd())}]),
        ("bad_sig", [{"payload": cmd(), "sig": "0" * 64}]),
        ("short_sig", [{"payload": cmd(), "sig": "abc"}]),
        ("not_dict", ["text"]),
        ("list", [[1]]),
        ("no_sig", [{"payload": cmd()}]),
        ("sig_int", [{"payload": cmd(), "sig": 5}]),
        ("payload_dict", [{"payload": {"a": 1}, "sig": "x"}]),
        ("too_long", [{"payload": cmd(args={"p": "x" * 8200}), "sig": sign(cmd(args={"p": "x" * 8200}))}]),
        (
            "max_len",
            [{"payload": (p := cmd(args={"p": "x" * (8192 - len(cmd(args={"p": ""})))})), "sig": sign(p)}],
        ),
        ("not_json", [{"payload": "nope", "sig": sign("nope")}]),
        (
            "nan",
            [
                {
                    "payload": (
                        p := '{"id":"c","action":"s","args":{},"login":"l","iat":NaN,"nonce":"nonce-0001"}'
                    ),
                    "sig": sign(p),
                }
            ],
        ),
        (
            "iat_infinity",
            [
                {
                    "payload": (
                        p := '{"id":"c","action":"s","args":{},"login":"l","iat":-Infinity,'
                        '"nonce":"nonce-0001"}'
                    ),
                    "sig": sign(p),
                }
            ],
        ),
        ("array", [{"payload": "[]", "sig": sign("[]")}]),
        ("missing_id", [{"payload": (p := cmd(id=...)), "sig": sign(p)}]),
        ("id_int", [{"payload": (p := cmd(id=5)), "sig": sign(p)}]),
        ("args_list", [{"payload": (p := cmd(args=[])), "sig": sign(p)}]),
        ("args_null", [{"payload": (p := cmd(args=None)), "sig": sign(p)}]),
        ("iat_str", [{"payload": (p := cmd(iat="1")), "sig": sign(p)}]),
        ("iat_bool", [{"payload": (p := cmd(iat=True)), "sig": sign(p)}]),
        ("iat_float", [{"payload": (p := cmd(iat=NOW * 1000 + 0.5)), "sig": sign(p)}]),
        ("iat_edge_old", [{"payload": (p := cmd(iat=int((NOW - 60) * 1000))), "sig": sign(p)}]),
        ("iat_too_old", [{"payload": (p := cmd(iat=int((NOW - 60) * 1000) - 1)), "sig": sign(p)}]),
        ("iat_future", [{"payload": (p := cmd(iat=int((NOW + 61) * 1000))), "sig": sign(p)}]),
        ("nonce_short", [{"payload": (p := cmd(nonce="1234567")), "sig": sign(p)}]),
        ("nonce_64", [{"payload": (p := cmd(nonce="n" * 64)), "sig": sign(p)}]),
        ("nonce_65", [{"payload": (p := cmd(nonce="n" * 65)), "sig": sign(p)}]),
        ("nonce_unicode_digit", [{"payload": (p := cmd(nonce="nonce-\u0661\u0662\u0663")), "sig": sign(p)}]),
        ("nonce_trailing_newline", [{"payload": (p := cmd(nonce="nonce-0001\n")), "sig": sign(p)}]),
        ("id_65", [{"payload": (p := cmd(id="i" * 65)), "sig": sign(p)}]),
        ("id_trailing_newline", [{"payload": (p := cmd(id="c1\n")), "sig": sign(p)}]),
        ("unknown_fields_kept", [{"payload": (p := cmd(extra=[1, 2])), "sig": sign(p)}]),
        (
            "duplicate_key_last_wins",
            [
                {
                    "payload": (
                        p := '{"id":"a","id":"b","action":"status","args":{},'
                        f'"login":"l","iat":{int(NOW * 1000)},"nonce":"nonce-0001"}}'
                    ),
                    "sig": sign(p),
                }
            ],
        ),
        ("unicode_escape_payload", [{"payload": (p := cmd(login="caf\\u00e9")), "sig": sign(p)}]),
        ("raw_unicode_payload", [{"payload": (p := cmd(login="caf\u00e9")), "sig": sign(p)}]),
    ]
    out = []
    for label, messages in cases:
        nonces = mc_agent.Nonces(None)
        results = [outcome(lambda m=m, n=nonces: mc_agent.verify(m, KEY, n, NOW)) for m in messages]
        out.append({"case": label, "key": KEY.decode(), "now": NOW, "messages": messages, "results": results})
    return out


def scrubber_vectors() -> dict[str, Any]:
    scrub = mc_agent.Scrubber(SCRUB_KNOWN)
    return {
        "known": SCRUB_KNOWN,
        "known_used": sorted(scrub.known, key=lambda k: (-len(k), k)),
        "patterns": [p.pattern for p in mc_agent.SECRET_PATTERNS],
        "cases": [{"input": t, "output": scrub(t)} for t in SCRUB_INPUTS],
    }


def players_vectors() -> dict[str, Any]:
    u = [
        "11111111-1111-4111-8111-111111111111",
        "22222222-2222-4222-8222-222222222222",
        "33333333-3333-4333-8333-333333333333",
    ]
    texts = [
        f'whitelist:\n  - {{name: "Bob", uuid: "{u[1]}"}}\n  - {{name: "alice", uuid: "{u[0]}"}}\nops:\n'
        f'  - {{name: "alice", uuid: "{u[0]}", level: 4}}\n',
        "whitelist: []\nops: []\n",
        "# comment\n\nwhitelist: []\n",
        f'whitelist:\n  - {{name: "B\\"o", uuid: "{u[1]}"}}\n',
        f'whitelist:\n  - {{uuid: "{u[1]}", name: "x"}}\n',
        f'whitelist:\n  - {{name: "x", uuid: "{u[1]}", level: 4}}\n',
        "whitelist:\n  - {name: x, uuid: y}\n",
        '  - {name: "x", uuid: "y"}\n',
        "players: []\n",
        f'ops:\n  - {{name: "a", uuid: "{u[0]}", level: -1}}\n',
        f'ops:\n  - {{name: "a",uuid: "{u[0]}",level: 4}}   \n',
        f'whitelist:\n  - {{name: "\\u00e9", uuid: "{u[0]}"}}\n',
        "whitelist: [] # trailing\n",
        "whitelist:\r\n  - {}\r\n",
    ]
    parsed = [{"input": t, "parse": outcome(lambda t=t: mc_panel.parse_players(t))} for t in texts]
    players = {
        "whitelist": [
            {"name": "Bob", "uuid": u[1]},
            {"name": "alice", "uuid": u[0]},
            {"name": "\u00e9mile", "uuid": u[2]},
        ],
        "ops": [{"name": "alice", "uuid": u[0], "level": 4}],
    }
    dumps = [
        {"input": p, "dump": mc_panel.dump_players(p)} for p in (players, {"whitelist": [], "ops": []}, {})
    ]
    entries = [
        {"op": "add", "name": "Carol", "uuid": u[2]},
        {"op": "remove", "name": "alice", "uuid": u[0]},
        {"op": "remove", "name": "Bob", "uuid": u[1]},
        {"op": "add", "name": "Bob2", "uuid": u[1]},
        {"op": "add", "name": "Carol", "uuid": u[2]},
    ]
    applied = [
        {
            "players": players,
            "entries": entries[:k],
            "result": list(mc_panel.apply_entries(players, entries[:k])),
        }
        for k in range(len(entries) + 1)
    ]
    return {"parse": parsed, "dump": dumps, "apply_entries": applied}


def merge_sync_vectors() -> list[dict[str, Any]]:
    def e(uuid: str, ts: float, pr: Any = None, op: str = "add") -> dict[str, Any]:  # noqa: ANN401
        return {"uuid": uuid, "name": uuid[:4], "op": op, "ts": ts, "login": "l", "pr": pr}

    a, b, c = (
        "aaaaaaaa-0000-4000-8000-000000000000",
        "bbbbbbbb-0000-4000-8000-000000000000",
        "cccccccc-0000-4000-8000-000000000000",
    )
    base = {"pr": None, "error": "", "changed": 1.0, "expired": [], "world": None}
    w1 = {
        "active": "a",
        "library": [],
        "what": "x",
        "ts": 1,
        "login": "l",
        "pr": None,
        "open_pr": None,
        "error": "",
    }
    w2 = dict(w1, active="b", ts=2)
    cases = [
        (
            "nothing_meanwhile",
            dict(base, entries=[e(a, 1)]),
            dict(base, entries=[e(a, 1)]),
            dict(base, entries=[e(a, 1, 7)], pr={"number": 7}),
        ),
        (
            "recorded_again",
            dict(base, entries=[e(a, 2)]),
            dict(base, entries=[e(a, 1)]),
            dict(base, entries=[e(a, 1, 7)], pr={"number": 7}),
        ),
        (
            "new_entry",
            dict(base, entries=[e(a, 1), e(b, 3)]),
            dict(base, entries=[e(a, 1)]),
            dict(base, entries=[], error="merged"),
        ),
        (
            "dropped_by_round",
            dict(base, entries=[e(a, 1), e(c, 1)]),
            dict(base, entries=[e(a, 1), e(c, 1)]),
            dict(base, entries=[e(c, 1)], expired=[{"name": "aaaa", "op": "add", "ts": 9}]),
        ),
        (
            "world_same",
            dict(base, entries=[], world=w1),
            dict(base, entries=[], world=w1),
            dict(base, entries=[], world=dict(w1, pr=3, open_pr={"number": 3})),
        ),
        (
            "world_newer",
            dict(base, entries=[], world=w2),
            dict(base, entries=[], world=w1),
            dict(base, entries=[], world=dict(w1, pr=3, open_pr={"number": 3})),
        ),
        (
            "world_newer_round_dropped",
            dict(base, entries=[], world=w2),
            dict(base, entries=[], world=w1),
            dict(base, entries=[], world=None),
        ),
        (
            "world_removed_meanwhile",
            dict(base, entries=[], world=None),
            dict(base, entries=[], world=w1),
            dict(base, entries=[], world=dict(w1, pr=3)),
        ),
    ]
    return [
        {
            "case": n,
            "current": cur,
            "snap": snap,
            "done": done,
            "result": mc_panel.merge_sync(cur, snap, done),
        }
        for n, cur, snap, done in cases
    ]


def token_vectors() -> dict[str, Any]:
    tokens = [
        "ghs_" + "a" * 36,
        "ghs_short",
        "a" * 20,
        "a" * 19,
        "x" * 4096,
        "x" * 4097,
        "ghs_abc.def-ghi_" + "j" * 10,
        "ghs_" + "\u00e9" * 20,
        "ghs_" + "a" * 20 + "\n",
        "ghs_" + "\uff41" * 20,
        "ghs " + "a" * 20,
        "",
    ]
    shapes = [
        ({"token": "ghs_" + "t!" * 12 + "\u00e9"}, "ghs_" + "t!" * 12 + "\u00e9"),
        ({"token": "ghs_short"}, "ghs_short"),
        ({"error": "GitHub App: down", "x": 1}, ""),
        ({"error": "e" * 200}, ""),
        ({}, ""),
        ({1: "a", "b": 2}, ""),
        (["token"], ""),
        ("text", ""),
        (None, ""),
        ({"token": "a b\tc\nd" + "z" * 20}, "a b\tc\nd" + "z" * 20),
        ({"token": "".join(chr(c) for c in range(0x21, 0x7F))}, "".join(chr(c) for c in range(0x21, 0x7F))),
    ]
    expiry = [
        "2026-11-08T12:00:00Z",
        "2026-11-08T12:00:00.123Z",
        "2026-11-08T12:00:00+02:00",
        "2026-11-08 12:00:00",
        "2026-11-08T12:00",
        "garbage",
        "",
        "2026-02-30T00:00:00Z",
        "2026-11-08T24:00:00Z",
        "1970-01-01T00:00:00Z",
        "2026-11-08T12:00:60Z",
        "\uff12\uff10\uff12\uff16-11-08T12:00:00Z",
    ]

    def parse_expiry(text: str) -> float | str:
        cache = mc_panel.TokenCache(
            lambda: {"token": "ghs_" + "a" * 36, "expires_at": text}, clock=lambda: 1000.0
        )
        cache()
        return cache.expires if cache.expires != 1000.0 + 1800 else "fallback: now + 1800"

    return {
        "token_re": mc_panel.TOKEN_RE.pattern,
        "fullmatch": [{"token": t, "match": bool(mc_panel.TOKEN_RE.fullmatch(t))} for t in tokens],
        "token_shape": [{"data": d, "token": t, "shape": mc_panel.token_shape(d, t)} for d, t in shapes],
        "expires_at": [{"expires_at": t, "expires": parse_expiry(t)} for t in expiry],
        "reuse_margin_s": 300,
    }


def rejected_vectors() -> dict[str, Any]:
    addrs = [
        "/203.0.113.7:51234",
        "203.0.113.7",
        "/[2001:db8:1:2::5]:4444",
        "2001:db8::1",
        "[::1]",
        "/fe80::1%eth0",
        "local",
        "",
        "/192.0.2.4:5:6",
        "/256.1.1.1:1",
        "/::ffff:192.0.2.4",
        "/\u066192.0.2.4:5",
    ]
    feed_lines = [
        (1000, "[23:58:01 INFO]: UUID of player Mallory is 66666666-6666-4666-8666-666666666666"),
        (
            2000,
            "[23:58:01 INFO]: Disconnecting Mallory (/203.0.113.7:51234): "
            "You are not whitelisted on this server!",
        ),
        (
            3000,
            "[23:58:02 INFO]: Mallory (/203.0.113.7:51234) lost connection: "
            "You are not whitelisted on this server!",
        ),
        (
            13000,
            "[23:58:12 INFO]: Mallory (/203.0.113.7:51234) lost connection: "
            "You are not white-listed on this server!",
        ),
        (14000, "[23:58:03 INFO]: <Eve> [23:58:03 INFO]: Disconnecting Bob (/192.0.2.4:5): not whitelisted"),
        (
            15000,
            "\x1b[0m[23:58:04 INFO]: Disconnecting Trudy (/[2001:db8::5]:4444): "
            "You are not white-listed!\r\n",
        ),
        (16000, "[23:58:05 WARN]: Disconnecting Zed (/192.0.2.4:5): not whitelisted"),
        (17000, "[23:58:06 INFO]: Disconnecting Zed (/192.0.2.4:5): NOT   WHITELISTED"),
        (18000, "[23:58:07 INFO]: Disconnecting Zed2 (/192.0.2.4:5): You are banned"),
        (19000, "[23:58:08 INFO]: Disconnecting ThisNameIsWayTooLong (/192.0.2.4:5): not whitelisted"),
        (20000, "[\u0662\u0663:58:09 INFO]: Disconnecting Arab (/192.0.2.4:5): not whitelisted"),
    ]
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        rl = mc_panel.RejectedLogins(Path(tmp) / "rejected.json")
        changed = rl.feed(feed_lines)
        entries = rl.entries()
    return {
        "mask_address": [{"input": a, "output": mc_panel.mask_address(a)} for a in addrs],
        "feed": {"lines": feed_lines, "changed": changed, "entries": entries},
    }


def texts_vectors() -> dict[str, Any]:
    return {
        "say_text": [
            {"label": lab, "left": n, "text": mc_panel.say_text(lab, n)}
            for lab in ("Restart", "Server stopping")
            for n in (*mc_panel.ANNOUNCE_AT, 600, 0)
        ],
        "announce_components": [
            json.dumps(["", {"text": "[Server] ", "color": "gold"}, {"text": t}])
            for t in ("Restart in 10 seconds. Find a safe spot!", "Restart cancelled.", "caf\u00e9")
        ],
        "cli_confirm": [
            {"action": a, "args": args, "phrase": mc_agent.cli_confirm(a, args)}
            for a, args in (
                ("world_create", {"name": "n"}),
                ("world_switch", {"name": "n"}),
                ("world_delete", {"name": "n"}),
                ("plugin", {"name": "p", "enabled": True}),
                ("plugin", {"name": "p", "enabled": 1}),
                ("plugin", {"name": "p"}),
                ("restart", {}),
                ("moderate", {"verb": "kick"}),
                ("world_create", {}),
            )
        ],
        "alerts": {
            "selftest_alert": mc_panel.selftest_alert("token: no GitHub token from the Worker: HTTP 502", 2),
            "selftest_back_alert": mc_panel.selftest_back_alert(3),
            "expired_alert": mc_panel.expired_alert("add Dave, remove Bob"),
            "rejected_alert": [
                mc_panel.rejected_alert(4, r, f)
                for r, f in ((["remove Bob"], []), ([], ["add Eve"]), (["remove A"], ["add B"]))
            ],
            "world_rejected_alert": mc_panel.world_rejected_alert(2, "main"),
            "render_ip_scrub": alerts.render(
                alerts.Alert("info", "from 203.0.113.7 and 2001:db8::1", details="192.0.2.4")
            ),
            "render_long": alerts.render(alerts.Alert("action", "h" * 1950)),
        },
    }


def parser_vectors() -> dict[str, Any]:
    tps = [
        "\u00a76TPS from last 1m, 5m, 15m: \u00a7a19.98, \u00a7a20.0, \u00a7a*20.0",
        "TPS from last 5s, 1m, 5m, 15m: 20.0, 19.5, 18.0, 17.0",
        "TPS: 20",
        "nonsense",
        "TPS from last 1m: 20.0, 19",
    ]
    lists = [
        "There are 2 of a max of 20 players online: Bob, Carol",
        "There are 0 of a max of 20 players online: ",
        "There are 1 of a max 10 players online: \u00a7eBob\u00a7r",
        "There are 3/20 players: a, b, c-d, " + "x" * 17,
        "no colon",
    ]
    net = (
        "Inter-|   Receive\n face |bytes\n    lo: 100 0 0 0 0 0 0 0 100 0\n"
        "  eth0: 1000 1 0 0 0 0 0 0 2000 2\n wg0: 5 0 0 0 0 0 0 0 7 0\n bad: 1 2\n"
    )
    mem = "MemTotal:       16000000 kB\nMemFree: 1 kB\nMemAvailable:    4000000 kB\n"
    stat = "cpu  10 0 10 80 0 0 0 0 0 0\ncpu0 5 0 5 40 0 0 0 0\ncpu1 5 0 5 40 0 0 0 0\nintr 1\n"
    gc = [
        "[2026-11-08T10:00:10.000+0000][130.4s][info][gc] GC(12) Pause Young (Normal) (G1 Evacuation Pause) "
        "3000M->1200M(6656M) 12.3ms\n",
        "[12.5s][info][gc,phases] GC(7) y: Pause Mark Start 0.013ms\n",
        "[12.6s][info][gc       ] Allocation Stall (Server thread) 3.120ms\n",
        "[12.7s][info][gc       ] GC(7) Minor Collection (Allocation Rate) 416M(81%)->362M(71%) 0.006s\n",
    ]
    journal = [
        {
            "__REALTIME_TIMESTAMP": "1794009600123456",
            "_SYSTEMD_UNIT": "minecraft.service",
            "PRIORITY": "4",
            "MESSAGE": "hi",
        },
        {
            "__REALTIME_TIMESTAMP": "1794009600123456",
            "SYSLOG_IDENTIFIER": "mc-agent",
            "MESSAGE": [104, 255],
            "MC_AUDIT": "1",
        },
        {"__REALTIME_TIMESTAMP": "x", "MESSAGE": "bad ts"},
        {"__REALTIME_TIMESTAMP": 1794009600123456, "MESSAGE": 5, "PRIORITY": "\u0663"},
        {
            "__REALTIME_TIMESTAMP": "1794009600123456",
            "SYSLOG_IDENTIFIER": "kernel",
            "MESSAGE": "mc-setfull: IN=eth0",
        },
        {
            "__REALTIME_TIMESTAMP": "1794009600123456",
            "_SYSTEMD_UNIT": "mc-world@x.service",
            "MESSAGE": "m" * 9000,
        },
        {"__REALTIME_TIMESTAMP": "1794009600123456", "_SYSTEMD_UNIT": "sshd.service", "MESSAGE": [1000]},
        {"__REALTIME_TIMESTAMP": "1794009600123456", "_SYSTEMD_UNIT": "Minecraft.service", "UNIT": "u" * 120},
    ]
    entries = [mc_agent.journal_entry(j) for j in journal]
    rows_state = mc_agent.LogState(None, "20261107", 2)
    kept = [e for e in entries if e]
    clients = [
        "mc-client-request 1\n00000000-0000-4000-8000-000000000000\tPlayer0\t" + "0" * 32 + "\t1\n",
        "mc-client-request 1\n00000000-0000-4000-8000-000000000000\tPlayer0\t" + "0" * 32 + "\n",
        "mc-client-request 2\n00000000-0000-4000-8000-000000000000\tP\t" + "0" * 32 + "\t1\n",
        "mc-client-request 1\n00000000-0000-4000-8000-00000000000A\tP\t" + "0" * 32 + "\t1\n",
        "mc-client-request 1\n00000000-0000-4000-8000-000000000000\tP\u00e9\t" + "0" * 32 + "\t1\n",
        "mc-client-request 1\r\n00000000-0000-4000-8000-000000000000\tP\t" + "0" * 32 + "\t1",
        "mc-client-request 1\n00000000-0000-4000-8000-000000000000\tP\t" + "0" * 32 + "\t1\nextra\n",
    ]
    return {
        "parse_tps_all": [{"input": t, "output": mc_agent.parse_tps_all(t)} for t in tps],
        "parse_tps": [{"input": t, **outcome(lambda t=t: health.parse_tps(t))} for t in tps],
        "parse_mspt": [
            {"input": t, **outcome(lambda t=t: health.parse_mspt(t))}
            for t in (
                "\u00a76\u25f4 \u00a7a12.1\u00a77/\u00a7a9.8\u00a77/\u00a7a20.3\u00a77, "
                "\u00a7a12.4/9.1/25.0, \u00a7a11.9/8.0/31.2",
                "none",
            )
        ],
        "player_names": [{"input": t, "output": mc_agent.player_names(t)} for t in lists],
        "players_regex": [
            {"input": t, "match": list(m.groups()) if (m := mc_agent.PLAYERS.search(t)) else None}
            for t in lists
        ],
        "parse_net_dev": {"input": net, "output": list(mc_agent.parse_net_dev(net))},
        "parse_meminfo": {
            "input": mem,
            "pct": mc_agent.parse_meminfo(mem),
            "mb": list(mc_agent.parse_meminfo_mb(mem) or []),
        },
        "parse_cores": {
            "input": stat,
            "output": mc_agent.parse_cores(stat),
            "proc_stat": health.parse_proc_stat(stat),
        },
        "cpu_busy_pct": [
            {"prev": p, "cur": c, "pct": mc_agent.cpu_busy_pct(p, c)}
            for p, c in (
                ([10, 0, 10, 80, 0, 0, 0, 0], [20, 0, 20, 160, 0, 0, 0, 0]),
                ([0] * 8, [0] * 8),
                ([5] * 8, [4] * 8),
                ([1] * 7, [2] * 8),
                ([0] * 8, [1, 1, 1, 1, 1, 1, 1, 2]),
            )
        ],
        "gc": {
            "lines": gc,
            "last_heap": list(health.last_heap(gc) or []),
            "max_pause_ms": round(max([(g := health.parse_gc(gc, 0.0, 100.0)).max_pause_ms, *g.stalls]), 1),
        },
        "journal_entry": [{"input": j, "output": e} for j, e in zip(journal, entries, strict=True)],
        "for_d1": [{"entry": e, "d1": mc_agent.for_d1(e), "archive": mc_agent.for_archive(e)} for e in kept],
        "select_rows": {
            "state_before": {"day": "20261107", "rows": 2},
            "cap": 3,
            "entries": kept,
            "rows": mc_agent.select_rows(kept, rows_state, 3),
            "state_after": {"day": rows_state.day, "rows": rows_state.rows},
        },
        "journal_argv": [mc_agent.journal_argv(None), mc_agent.journal_argv("s=1;i=2")],
        "archive_batch_id": [
            {"cursor": c, "batch": hashlib.sha256(c.encode()).hexdigest()[:32]}
            for c in ("", "s=parity;i=05014")
        ],
        "parse_client_request": [{"input": t, "output": mc_agent.parse_client_request(t)} for t in clients],
        "utc_day": [{"ms": ms, "day": mc_agent.utc_day(ms)} for ms in (0, 1794009599999, 1794009600000)],
    }


def worlds_vectors() -> dict[str, Any]:
    entries: list[Any] = [
        {"name": "w"},
        {
            "name": "new1",
            "seed": "-123",
            "type": "amplified",
            "preset": "survival_smp",
            "border_radius": {"minecraft:overworld": 3000},
            "pregen": True,
        },
        {"name": "Bad"},
        {"name": "a" * 33},
        {"name": "-x"},
        {"name": "w", "seed": "1" * 20},
        {"name": "w", "seed": 5},
        {"name": "w", "type": "nope"},
        {"name": "w", "preset": "nope"},
        {"name": "w", "border_radius": {"x": 1}},
        {"name": "w", "border_radius": {"minecraft:the_end": True}},
        {"name": "w", "border_radius": {"minecraft:the_end": 29999985}},
        {"name": "w", "pregen": "yes"},
        "string",
        {"name": "w\u0661"},
    ]
    text = (
        '# c\nname: x\nworld:\n  # kept\n  active: "main"\n  library:\n    - {name: "main"}\n'
        "  border_radius: 5\nafter: 1\n"
    )
    lib = [
        worlds.check_entry({"name": "zeta", "border_radius": {"minecraft:overworld": 1}, "pregen": False}),
        worlds.check_entry({"name": "alpha", "seed": "7", "type": "flat_void", "preset": "survival_smp"}),
    ]
    sections = [
        text,
        "name: x\n\n\n",
        "world: {active: x}\n",
        "world:\n    active: x\n",
        "world:\n  active:\n    - x\n",
        "",
    ]
    return {
        "world_name_re": worlds.WORLD_NAME.pattern,
        "check_entry": [{"input": e, **outcome(lambda e=e: worlds.check_entry(e))} for e in entries],
        "version_check": [
            {"target": t, "current": c, "why": worlds.version_check(t, c)}
            for t, c in ((1, 2), (2, 1), (None, 1), (1, None), (5, 5))
        ],
        "library_line": [worlds.library_line(e) for e in lib],
        "set_world_section": [
            {
                "text": t,
                "active": "alpha",
                "library": lib,
                **outcome(lambda t=t: worlds.set_world_section(t, "alpha", lib)),
            }
            for t in sections
        ]
        + [
            {
                "text": text,
                "active": "x",
                "library": [],
                **outcome(lambda: worlds.set_world_section(text, "x", [])),
            }
        ],
        "tag_value": [
            {"tags": t, "prefix": p, "value": worlds.tag_value(t, p)}
            for t, p in ((["a", "world:x", "world:y"], "world:"), (["dv:1"], "world:"))
        ],
    }


def websocket_vectors() -> dict[str, Any]:
    mask = bytes([0x37, 0xFA, 0x21, 0x3D])
    payloads = [b"", b"Hello", b"x" * 125, b"x" * 126, b"x" * 65535, b"x" * 65536, "caf\u00e9".encode()]
    return {
        "guid": mc_agent.WS_GUID,
        "accept": [
            {"nonce_b64": k, "accept": mc_agent.ws_accept(k)}
            for k in ("dGhlIHNhbXBsZSBub25jZQ==", "AAAAAAAAAAAAAAAAAAAAAA==")
        ],
        "encode_frame": [
            {
                "opcode": op,
                "payload_len": len(p),
                "payload_sha256": hashlib.sha256(p).hexdigest(),
                "mask": mask.hex(),
                "frame_head": (f := mc_agent.encode_frame(op, p, mask))[:14].hex(),
                "frame_sha256": hashlib.sha256(f).hexdigest(),
                "frame_len": len(f),
            }
            for op in (0x1, 0x9)
            for p in payloads
            if op == 0x1 or len(p) < 126
        ],
        "limits": {
            "max_message": mc_agent.WS_MAX_MESSAGE,
            "recv_timeout_s": mc_agent.WS_RECV_TIMEOUT_S,
            "ping_s": mc_agent.WS_PING_S,
            "dead_s": mc_agent.WS_DEAD_S,
        },
    }


def json_vectors() -> list[dict[str, Any]]:
    values: list[Any] = [
        {"b": 1, "a": [1, 2], "c": {"z": None, "y": True}},
        "caf\u00e9",
        "\u2603",
        "\U0001f600",
        "\x00\x1f\x7f",
        "\u2028\u2029",
        "a/b",
        'q"\\',
        "\ufeff",
        1.0,
        0.1,
        1e16,
        1e-7,
        123456789012345678901234567890,
        -0.0,
        2.5,
        [],
        {},
        {"": ""},
        {"\u00e9": 1, "e": 2, "E": 3, "\U0001f600": 4},
        float("1e308"),
        3.0e-5,
        100.0,
        1e22,
    ]
    out = []
    for v in values:
        out.append(
            {
                "value": v,
                "sort_keys": json.dumps(v, sort_keys=True),
                "default": json.dumps(v),
                "compact": json.dumps(v, separators=(",", ":")),
                "pending_style": json.dumps(v, sort_keys=True, indent=1),
            }
        )
    return out


def round_vectors() -> list[dict[str, Any]]:
    xs = [
        0.125,
        0.135,
        2.5,
        3.5,
        -2.5,
        2.675,
        12.345,
        1.005,
        0.0,
        -0.05,
        99.995,
        1e-9,
        12.3,
        19.98,
        1234.5678,
    ]
    return [{"x": x, "n": n, "round": round(x, n)} for x in xs for n in (0, 1, 2)] + [
        {"x": x, "n": None, "round": round(x)} for x in (0.5, 1.5, 2.5, -0.5, 1.4999999)
    ]


def unicode_vectors() -> dict[str, Any]:
    space = [cp for cp in range(0x110000) if chr(cp).isspace()]
    re_space = [cp for cp in range(0x110000) if re.fullmatch(r"\s", chr(cp))]
    digits = [cp for cp in range(0x110000) if re.fullmatch(r"\d", chr(cp))]
    cats = [
        0x00,
        0x09,
        0x1C,
        0x7F,
        0x85,
        0xA0,
        0xAD,
        0x378,
        0x1680,
        0x180E,
        0x200B,
        0x200D,
        0x2028,
        0x2029,
        0x2060,
        0x3000,
        0xD7FF,
        0xE000,
        0xFEFF,
        0xFDFA,
        0xFFFD,
        0x1D407,
        0xE0001,
        0xF0000,
        0x10FFFD,
        0x0663,
        0x0967,
    ]
    nfkc = [
        "\uff21",
        "\u3000",
        "\u00a0",
        "\ufdfa",
        "\u2126",
        "\ufb01",
        "\u2460",
        "\u00bd",
        "\u1e9b\u0323",
        "e\u0301",
        "\U0001d407",
        "\u0663",
        "\uff13",
        "\u00b2",
        "\u2163",
        "\u33a1",
    ]
    lengths = ["abc", "\u00e9", "e\u0301", "\U0001f600", "\ufdfa", "\u0663"]
    return {
        "isspace": ranges(space),
        "re_unicode_s": ranges(re_space),
        "isspace_but_not_rust_whitespace_note": (
            "Python: U+001C..U+001F are whitespace (str.isspace); Rust's char::is_whitespace is not"
        ),
        "re_unicode_d": {"count": len(digits), "ranges": ranges(digits)},
        "re_ascii_d": "0123456789",
        "category": [{"cp": cp, "category": unicodedata.category(chr(cp))} for cp in cats],
        "bad_categories": sorted(mc_agent.BAD_CATEGORIES),
        "nfkc": [
            {
                "input": s,
                "nfkc": unicodedata.normalize("NFKC", s),
                "len": len(s),
                "nfkc_len": len(unicodedata.normalize("NFKC", s)),
            }
            for s in nfkc
        ],
        "code_point_lengths": [
            {"input": s, "len": len(s), "utf8_len": len(s.encode()), "slice_2": s[:2]} for s in lengths
        ],
        "lower": [
            {"input": s, "lower": s.lower()}
            for s in ("LIST", "\u0130", "\u212a", "\u00c9", "\uff2c\uff29\uff33\uff34")
        ],
    }


def constants_vectors() -> dict[str, Any]:
    a = mc_agent
    return {
        "ACTIONS": list(a.ACTIONS),
        "CLI_ACTIONS": list(a.CLI_ACTIONS),
        "CLI_LISTS": list(a.CLI_LISTS),
        "CLI_LOGIN": a.CLI_LOGIN.pattern,
        "CLI_USAGE": a.CLI_USAGE,
        "COUNTDOWNS": list(mc_panel.COUNTDOWNS),
        "ANNOUNCE_AT": list(mc_panel.ANNOUNCE_AT),
        "MAX_AGE_S": a.MAX_AGE_S,
        "NONCE_TTL_S": a.NONCE_TTL_S,
        "CONSOLE_MAX": a.CONSOLE_MAX,
        "TAIL_MAX_S": a.TAIL_MAX_S,
        "TAIL_BATCH": a.TAIL_BATCH,
        "INVENTORY_S": a.INVENTORY_S,
        "INVENTORY_MIN_GAP_S": a.INVENTORY_MIN_GAP_S,
        "INVENTORY_MAX": a.INVENTORY_MAX,
        "SPOOL_MAX_AGE_S": a.SPOOL_MAX_AGE_S,
        "METRICS_BATCH": a.METRICS_BATCH,
        "LOG_ROW_MAX_CHARS": a.LOG_ROW_MAX_CHARS,
        "LOG_BATCH_MAX_ENTRIES": a.LOG_BATCH_MAX_ENTRIES,
        "LOG_BATCH_MAX_BYTES": a.LOG_BATCH_MAX_BYTES,
        "LOG_ENTRY_MAX_CHARS": a.LOG_ENTRY_MAX_CHARS,
        "LOG_CATCHUP_BATCHES": a.LOG_CATCHUP_BATCHES,
        "JOURNAL_FIELDS": a.JOURNAL_FIELDS,
        "MAX_CORES": a.MAX_CORES,
        "SELFTEST_S": a.SELFTEST_S,
        "SELFTEST_FIRST_S": a.SELFTEST_FIRST_S,
        "SELFTEST_MIN_GAP_S": a.SELFTEST_MIN_GAP_S,
        "SELFTEST_STEPS": list(mc_panel.SELFTEST_STEPS),
        "SELFTEST_NEEDS": mc_panel.SELFTEST_NEEDS,
        "SELFTEST_ALERT_AFTER": mc_panel.SELFTEST_ALERT_AFTER,
        "MOJANG_LOOKUPS": list(a.MOJANG_LOOKUPS),
        "LOOKUP_TIMEOUT_S": a.LOOKUP_TIMEOUT_S,
        "LOOKUP_TTL_S": a.LOOKUP_TTL_S,
        "LOOKUP_PER_MINUTE": a.LOOKUP_PER_MINUTE,
        "RESTORE_TIMEOUT_S": a.RESTORE_TIMEOUT_S,
        "RCON_TIMEOUT_S": a.RCON_TIMEOUT_S,
        "RCON_WAIT_S": mc_panel.RCON_WAIT_S,
        "PENDING_TTL_S": mc_panel.PENDING_TTL_S,
        "BATCH_S": mc_panel.BATCH_S,
        "PR_CHECK_S": mc_panel.PR_CHECK_S,
        "REJECTED_KEEP": mc_panel.REJECTED_KEEP,
        "CLIENT_POLL_S": a.CLIENT_POLL_S,
        "CLIENT_KEEP_S": a.CLIENT_KEEP_S,
        "CLIENT_MAX_REQUESTS": a.CLIENT_MAX_REQUESTS,
        "CLIENT_PER_HOUR": a.CLIENT_PER_HOUR,
        "CLIENT_REASONS": {str(k): v for k, v in a.CLIENT_REASONS.items()},
        "CLIENT_FIXES": {str(k): v for k, v in a.CLIENT_FIXES.items()},
        "REFUSED_HEADS": a.REFUSED_HEADS,
        "FREE_TEXT": sorted(a.FREE_TEXT),
        "MESSAGE_FROM": a.MESSAGE_FROM,
        "CONSOLE_ALLOWLIST": {k: v.pattern for k, v in a.CONSOLE_ALLOWLIST.items()},
        "SELECTOR_WORD": a.SELECTOR_WORD.pattern,
        "NAME": a.NAME,
        "IP": a.IP,
        "DURATION": a.DURATION,
        "D1_UNITS": a.D1_UNITS.pattern,
        "D1_IDENTIFIERS": a.D1_IDENTIFIERS.pattern,
        "FIREWALL_PREFIXES": list(a.FIREWALL_PREFIXES),
        "SECRET_ENV": list(a.SECRET_ENV),
        "SECRET_SUFFIXES": list(a.SECRET_SUFFIXES),
        "USER_AGENT": a.USER_AGENT,
        "VERSION": a.VERSION,
        "PANEL_REQUESTS": a.PANEL_REQUESTS,
        "PANEL_ROWS": a.PANEL_ROWS,
        "EXPERIENCE_REQUESTS": a.EXPERIENCE_REQUESTS,
        "EXPERIENCE_ROWS": a.EXPERIENCE_ROWS,
        "CLIENT_REQUESTS": a.CLIENT_REQUESTS,
        "CLIENT_ROWS": a.CLIENT_ROWS,
        "daily_budget": [
            {"args": kw, "budget": a.daily_budget(**kw)}
            for kw in (
                {},
                {"interval_s": 30, "poll_s": 5},
                {"experience": False, "client": True},
                {"interval_s": 300, "max_rows": 0, "log_batch_s": 10, "poll_s": 60},
            )
        ],
        "settings_clamps": [
            {
                "env": env,
                **outcome(
                    lambda env=env: {
                        k: (str(v) if isinstance(v, Path | bytes) else v)
                        for k, v in vars(a.Settings.from_env(env)).items()
                        if k not in ("token", "hmac_key")
                    }
                ),
            }
            for env in (
                {
                    "INGEST_URL": "https://hb.example.test/",
                    "AGENT_TOKEN": "t" * 32,
                    "COMMAND_HMAC_KEY": "k" * 32,
                },
                {
                    "INGEST_URL": "http://127.0.0.1:8787",
                    "AGENT_TOKEN": "t" * 32,
                    "COMMAND_HMAC_KEY": "k" * 32,
                    "METRICS_INTERVAL_S": "5",
                    "POLL_S": "1",
                    "LOG_BATCH_S": "1",
                    "LOG_MAX_ROWS_PER_DAY": "-5",
                    "PANEL_SYNC_S": "0",
                    "STATE_DIRECTORY": "/var/lib/mc-agent:/var/lib/mc-telemetry",
                    "EXPERIENCE": "1",
                    "CLIENT": "true",
                },
                {
                    "INGEST_URL": "http://[::1]:1",
                    "AGENT_TOKEN": "t" * 32,
                    "COMMAND_HMAC_KEY": "k" * 32,
                    "METRICS_INTERVAL_S": "9999",
                },
                {"INGEST_URL": "http://localhost", "AGENT_TOKEN": "t" * 32, "COMMAND_HMAC_KEY": "k" * 32},
                {"INGEST_URL": "http://10.0.0.1", "AGENT_TOKEN": "t" * 32, "COMMAND_HMAC_KEY": "k" * 32},
                {"INGEST_URL": "https://", "AGENT_TOKEN": "t" * 32, "COMMAND_HMAC_KEY": "k" * 32},
                {"INGEST_URL": "https://x", "AGENT_TOKEN": "t" * 31, "COMMAND_HMAC_KEY": "k" * 32},
                {
                    "INGEST_URL": "https://x",
                    "AGENT_TOKEN": "t" * 32,
                    "COMMAND_HMAC_KEY": "k" * 32,
                    "POLL_S": " 7 ",
                },
                {
                    "INGEST_URL": "https://x",
                    "AGENT_TOKEN": "t" * 32,
                    "COMMAND_HMAC_KEY": "k" * 32,
                    "POLL_S": "\u0667",
                },
            )
        ],
    }


def one_line_vectors() -> list[dict[str, Any]]:
    out = []
    for t in ONE_LINE:
        for limit in (100, 200, 256, 5):
            if limit != 100 and not isinstance(t, str):
                continue
            out.append({"input": t, "limit": limit, "output": mc_agent._one_line(t, limit)})
    return out


def build() -> dict[str, dict[str, Any]]:
    files = [
        vec(
            "console",
            console_vectors(),
            unicode_sensitive=True,
            doc="check_console/normalise_console: the line RCON gets, or the refusal message",
        ),
        vec(
            "one_line",
            one_line_vectors(),
            unicode_sensitive=True,
            doc="_one_line(text, limit): NFKC, every isspace() char -> ' ' (U+001C-U+001F too), bad "
            "categories dropped, collapsed, first `limit` code points",
        ),
        vec(
            "scrubber",
            scrubber_vectors(),
            unicode_sensitive=True,
            doc="Scrubber(known)(text): known values (>= 8 chars, longest first) then SECRET_PATTERNS "
            "in order",
        ),
        vec(
            "verify",
            verify_vectors(),
            doc="verify(message, key, Nonces(None), now): the command or the refusal",
        ),
        vec("players", players_vectors(), doc="mc_panel.parse_players / dump_players / apply_entries"),
        vec("merge_sync", merge_sync_vectors(), doc="mc_panel.merge_sync(current, snap, done)"),
        vec(
            "tokens",
            token_vectors(),
            doc="TOKEN_RE, token_shape, TokenCache's expires_at parse (UTC, first 19 chars)",
        ),
        vec("rejected", rejected_vectors(), doc="mask_address and RejectedLogins.feed"),
        vec(
            "texts",
            texts_vectors(),
            doc="say_text, the announce components, cli_confirm and the Discord alert texts (byte-exact)",
        ),
        vec("parsers", parser_vectors(), doc="the metrics, journal and client-request parsers"),
        vec(
            "worlds",
            worlds_vectors(),
            doc="worlds.check_entry / version_check / library_line / set_world_section",
        ),
        vec("websocket", websocket_vectors(), doc="ws_accept and encode_frame with a fixed mask"),
        vec(
            "json",
            json_vectors(),
            doc="json.dumps byte-exactness: MC_ARGS (sort_keys), the cli lists, pending.json (indent=1)",
        ),
        vec("round", round_vectors(), doc="Python round(x, n) (the metrics fields)"),
        vec(
            "unicode",
            unicode_vectors(),
            unicode_sensitive=True,
            doc="str.isspace, re's Unicode \\s and \\d versus [0-9] (re.ASCII), categories, NFKC, "
            "code-point lengths",
        ),
        vec("constants", constants_vectors(), doc="the agent's constants, budget model and Settings clamps"),
    ]
    return dict(files)


def render(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=1, ensure_ascii=True) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if a vector file is stale")
    args = ap.parse_args(argv)
    stale = []
    files = build()
    for name, data in files.items():
        path = OUT / f"{name}.json"
        text = render(data)
        current = path.read_text() if path.exists() else None
        if current == text:
            continue
        if args.check:
            with contextlib.suppress(ValueError):
                old = json.loads(current or "{}")
                if (
                    data["meta"].get("unicode_sensitive")
                    and old.get("meta", {}).get("unidata_version") != unicodedata.unidata_version
                ):
                    print(
                        f"{name}.json: generated with Unicode {old['meta'].get('unidata_version')}, "
                        "this Python "
                        f"has {unicodedata.unidata_version}: not compared"
                    )
                    continue
            stale.append(name)
            continue
        OUT.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        print(f"wrote {path.relative_to(HERE.parent)}")
    extra = sorted(p.stem for p in OUT.glob("*.json") if p.stem not in files) if OUT.exists() else []
    if args.check and (stale or extra):
        print(
            f"stale vector files: {', '.join(stale + extra)}: run python scripts/parity_vectors.py",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
