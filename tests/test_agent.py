"""P8 mc-agent (scripts/mc_agent.py): signature, replay, console allowlist, buffer, log shipping, the WebSocket
client, the executor and the Free-plan budget at the defaults. No network: a local HTTP server and socket pairs."""

from __future__ import annotations

import base64
import contextlib
import dataclasses
import gzip
import hashlib
import http.server
import json
import re
import shutil
import socket
import struct
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import mc_agent
import mc_panel
import notify
import pytest

KEY = b"k" * 64
TOKEN = "t" * 48
NOW = 1_800_000_000.0


def signed(action: str = "status", args: dict | None = None, *, iat: float = NOW, nonce: str | None = None,
           key: bytes = KEY, **extra: Any) -> dict[str, str]:  # fmt: skip
    cmd = {"id": "c1", "action": action, "args": args or {}, "login": "admin@x", "iat": int(iat * 1000)}
    cmd["nonce"] = nonce or hashlib.sha256(f"{action}{args}{iat}{extra}".encode()).hexdigest()[:24]
    cmd |= extra
    payload = json.dumps(cmd)
    return {"payload": payload, "sig": mc_agent.sign(key, payload)}


@pytest.fixture
def settings(tmp_path: Path) -> mc_agent.Settings:
    return mc_agent.Settings("https://hb.example.invalid", TOKEN, KEY, state_dir=tmp_path)


# --- commands: signature, age, replay -----------------------------------------------------------------------


def test_a_valid_command_passes_once(tmp_path: Path) -> None:
    nonces = mc_agent.Nonces(tmp_path / "nonces.json")
    msg = signed()
    assert mc_agent.verify(msg, KEY, nonces, NOW)["action"] == "status"
    with pytest.raises(mc_agent.Rejected, match="replayed"):
        mc_agent.verify(msg, KEY, nonces, NOW + 1)
    # a restart doesn't reopen the window: the nonces are persisted
    with pytest.raises(mc_agent.Rejected, match="replayed"):
        mc_agent.verify(msg, KEY, mc_agent.Nonces(tmp_path / "nonces.json"), NOW + 2)


@pytest.mark.parametrize(
    ("msg", "why"),
    [
        (signed(key=b"x" * 64), "bad signature"),
        ({**signed(), "sig": "00" * 32}, "bad signature"),
        ({"payload": signed()["payload"].replace("status", "stop"), "sig": signed()["sig"]}, "bad signature"),
        (signed(iat=NOW - 61), "too old"),
        (signed(iat=NOW + 61), "from the future"),
        (signed(nonce="short"), "bad id or nonce"),
        ({"payload": 1, "sig": "x"}, "malformed"),
        ("nope", "not a command"),
    ],
)
def test_bad_commands_are_rejected(msg: object, why: str, tmp_path: Path) -> None:
    with pytest.raises(mc_agent.Rejected, match=why):
        mc_agent.verify(msg, KEY, mc_agent.Nonces(None), NOW)


def test_missing_fields_are_rejected() -> None:
    payload = json.dumps({"id": "c1", "action": "status", "iat": int(NOW * 1000), "nonce": "n" * 16})
    with pytest.raises(mc_agent.Rejected, match="login"):
        mc_agent.verify(
            {"payload": payload, "sig": mc_agent.sign(KEY, payload)}, KEY, mc_agent.Nonces(None), NOW
        )


def test_nonces_expire() -> None:
    n = mc_agent.Nonces(None)
    assert n.use("a" * 16, NOW) and not n.use("a" * 16, NOW + 10)
    assert n.use("a" * 16, NOW + mc_agent.NONCE_TTL_S + 1)


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"n": "x"}', ""])
def test_a_corrupt_nonce_store_fails_closed(content: str, tmp_path: Path, monkeypatch) -> None:
    audits: list[str] = []
    monkeypatch.setattr(mc_agent, "audit", lambda msg, **f: audits.append(f["result"]))
    path = tmp_path / "nonces.json"
    path.write_text(content)
    nonces = mc_agent.Nonces(path, now=NOW)
    assert audits == ["fail-closed"]
    # every command is refused for the whole replay window (any of them could be a replay)...
    for t in (NOW, NOW + mc_agent.NONCE_TTL_S - 1):
        with pytest.raises(mc_agent.Rejected, match="unreadable"):
            mc_agent.verify(signed(iat=t, nonce=f"n{int(t)}" * 4), KEY, nonces, t)
    # ...then the store starts afresh (and is written again)
    later = NOW + mc_agent.NONCE_TTL_S + 1
    assert mc_agent.verify(signed(iat=later), KEY, nonces, later)["action"] == "status"
    assert json.loads(path.read_text())


def test_an_unwritable_nonce_store_refuses_the_command(tmp_path: Path) -> None:
    nonces = mc_agent.Nonces(tmp_path / "missing-dir" / "nonces.json")  # missing file: a fresh store
    with pytest.raises(mc_agent.Rejected, match="can't be written"):
        nonces.use("b" * 16, NOW)


# --- console allowlist (P8 review: the denylist was bypassable) --------------------------------------------

# Every bypass of the P8 denylist found in review, plus variants: chains, namespaces, case, Unicode look-alikes.
BYPASSES = [
    "execute as run run op Steve",
    "return run op Steve",
    "execute run return run op Steve",
    "execute if entity run run minecraft:op Steve",
    "execute as @a run stop",
    "execute run execute run minecraft:op Alex",
    "execute as @p at @s run deop me",
    "EXECUTE run op Steve",
    "/execute run say hi",
    "function minecraft:give_op",
    "return 1",
    "minecraft:execute run op Steve",
    "minecraft:op Steve",
    "bukkit:op Steve",
    "paper:reload",
    "spigot:restart",
    "purpur:stop",
    "Minecraft:Stop",
    "\uff4f\uff50 Steve",  # full-width "op": NFKC turns it into op
    "\uff45\uff58\uff45\uff43\uff55\uff54\uff45 run op Steve",  # full-width "execute"
    "//op Steve",
]
REFUSED = [
    "stop", "/stop", "STOP", "minecraft:stop", "restart", "reload", "rl", "reload confirm", "op Steve",
    "deop Steve", "save-off", "whitelist off", "paper reload", "spigot reload", "purpur reload",
    "bukkit:reload", "give Steve diamond 64", "gamemode creative Steve", "tp Steve 0 0 0", "kill @e",
    "transfer example.org", "datapack enable x", "gamerule keepInventory true", "worldborder set 10",
    "spark profiler start", "spark health --upload", "chunky trim", "kick @a", "ban @a griefing",
    "pardon @a", "time set day extra", "weather clear forever", "stopwatch", "scoreboard players list",
]  # fmt: skip


@pytest.mark.parametrize("line", BYPASSES + REFUSED)
def test_console_refuses_everything_off_the_allowlist(line: str) -> None:
    with pytest.raises(mc_agent.Rejected, match="not allowed"):
        mc_agent.check_console(line)


@pytest.mark.parametrize("line", ["whitelist add Steve", "/WHITELIST remove Alex", "whitelist   ADD x"])
def test_whitelist_changes_are_redirected(line: str) -> None:
    with pytest.raises(mc_agent.Rejected, match="whitelist flow"):
        mc_agent.check_console(line)


@pytest.mark.parametrize(
    "line",
    ["", "   ", "/", "say hi\nstop", "say hi\rstop", "say a\x00b", "say \u2028op", "say a\tb",
     "say \u200bhidden", "say \u202eevil", "x" * 257, None, 42],
)  # fmt: skip
def test_console_shape(line: object) -> None:
    with pytest.raises(mc_agent.Rejected):
        mc_agent.check_console(line)


@pytest.mark.parametrize(
    ("line", "sent"),
    [
        ("list", "list"), ("/say hello World", "say hello World"), ("tps", "tps"), ("MSPT", "mspt"),
        ("whitelist list", "whitelist list"), ("whitelist on", "whitelist on"), ("time set day", "time set day"),
        ("time query daytime", "time query daytime"), ("weather rain 600", "weather rain 600"),
        ("kick Steve please rejoin", "kick Steve please rejoin"), ("ban griefer_99 Stole stuff", "ban griefer_99 Stole stuff"),
        ("ban-ip 203.0.113.5 spam", "ban-ip 203.0.113.5 spam"), ("pardon-ip 2001:db8::1", "pardon-ip 2001:db8::1"),
        ("pardon Steve", "pardon Steve"), ("banlist ips", "banlist ips"), ("save-all flush", "save-all flush"),
        ("gamerule keepInventory", "gamerule keepInventory"), ("worldborder get", "worldborder get"),
        ("spark health --memory", "spark health --memory"), ("chunky progress", "chunky progress"),
        ("seed", "seed"), ("difficulty", "difficulty"), ("msg @a hi there", "msg @a hi there"),
        ("say  spaced   out ", "say spaced out"), ("\uff53\uff41\uff59 hi", "say hi"),  # full-width "say"
        ("say caf\u00e9 \u00a0ok", "say café ok"),
    ],
)  # fmt: skip
def test_console_allows_the_allowlist_and_sends_what_it_checked(line: str, sent: str) -> None:
    assert mc_agent.check_console(line) == sent


def test_console_length_is_checked_after_normalisation() -> None:
    line = "say " + "\ufdfa" * 20  # 24 characters typed, 364 after NFKC
    assert len(line) <= mc_agent.CONSOLE_MAX
    with pytest.raises(mc_agent.Rejected, match="after normalisation"):
        mc_agent.check_console(line)


@pytest.mark.parametrize("line", ["say @e", "say hi @a", "me waves at @p", "tell Steve look @e[type=cow]",
                                  "msg @a @r", "say @s", "w Steve @n"])  # fmt: skip
def test_selectors_inside_messages_are_refused(line: str) -> None:
    with pytest.raises(mc_agent.Rejected, match="selectors"):
        mc_agent.check_console(line)


def test_tell_target_selector_and_at_signs_in_text_are_fine() -> None:
    assert mc_agent.check_console("tell @a server restarts at 18:00") == "tell @a server restarts at 18:00"
    assert mc_agent.check_console("say mail me @ home or @everyone") == "say mail me @ home or @everyone"


def test_every_allowlisted_head_is_plain_ascii_and_never_a_chain() -> None:
    for head in mc_agent.CONSOLE_ALLOWLIST:
        assert re.fullmatch(r"[a-z][a-z-]*", head), head
    assert not set(mc_agent.CONSOLE_ALLOWLIST) & set(mc_agent.REFUSED_HEADS)
    assert {"execute", "return", "function", "op", "deop", "stop", "reload"} <= set(mc_agent.REFUSED_HEADS)


# --- executor + audit -----------------------------------------------------------------------------------------


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp_path / "admin-stopped")
    monkeypatch.setattr(notify, "MAINTENANCE_FILE", tmp_path / "maintenance.json")
    monkeypatch.delenv("WATCHDOG_URL", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(mc_agent, "systemctl", lambda *a: calls.append(list(a)))
    monkeypatch.setattr(mc_agent, "audit", lambda msg, **f: calls.append(["AUDIT", msg, f["result"]]))
    return calls


def test_executor_allowlist(box: list[list[str]], settings: mc_agent.Settings, tmp_path: Path) -> None:
    ex = mc_agent.Executor(lambda: {"tps": 20.0})
    run = lambda msg: mc_agent.handle_command(msg, settings, mc_agent.Nonces(None), ex)  # noqa: E731
    now = time.time()
    assert run(signed("status", iat=now))["output"] == '{"tps":20.0}'
    r = run(signed("stop", {"reason": "maintenance"}, iat=now))
    assert r["ok"] and json.loads((tmp_path / "admin-stopped").read_text())["reason"] == "maintenance"
    r = run(signed("restart", iat=now))
    assert not r["ok"] and "admin-stopped" in r["error"]
    assert run(signed("start", iat=now))["ok"] and not (tmp_path / "admin-stopped").exists()
    assert run(signed("restart", iat=now))["ok"]
    assert run(signed("backup", iat=now))["ok"]
    r = run(signed("shell", {"cmd": "id"}, iat=now))
    assert not r["ok"] and r["error"] == "unknown action: shell"
    r = run(signed("console", {"command": "stop"}, iat=now))
    assert not r["ok"] and "not allowed" in r["error"]
    actions = [c for c in box if c[0] != "AUDIT"]
    assert actions == [
        ["stop", "minecraft.service"],
        ["start", "minecraft.service"],
        ["restart", "minecraft.service"],
        ["start", "--no-block", "mc-backup.service"],
    ]
    audits = [c for c in box if c[0] == "AUDIT"]
    assert len(audits) == 8  # every command, refused or not, reaches the local journal
    assert "login=admin@x" in audits[0][1]


def test_console_runs_over_rcon(box, settings, fake_server) -> None:
    fake_server(lambda cmd: f"ran {cmd}")
    ex = mc_agent.Executor(dict)
    r = mc_agent.handle_command(signed("console", {"command": "/list"}, iat=time.time()), settings,
                                mc_agent.Nonces(None), ex)  # fmt: skip
    assert r == {"id": "c1", "ok": True, "output": "ran list", "finished_at": r["finished_at"]}


def test_audit_uses_the_journal_protocol(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "journal.sock"
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(str(path))
    monkeypatch.setattr(mc_agent, "JOURNAL_SOCKET", str(path))
    mc_agent.audit(
        "agent action=console ok=True", action="console", args='{"command":"a\\nb"}', result="x\ny"
    )
    data = srv.recv(65536)
    srv.close()
    assert b"SYSLOG_IDENTIFIER=mc-agent\n" in data and b"MC_AUDIT=1\n" in data
    assert b"MC_RESULT\n" + struct.pack("<Q", 3) + b"x\ny\n" in data  # binary-safe multi-line field


# --- metrics buffer ----------------------------------------------------------------------------------------


def test_spool_buffers_until_delivered_oldest_first(tmp_path: Path) -> None:
    spool = mc_agent.Spool(tmp_path / "spool.jsonl")
    for i in range(3):
        spool.add({"ts": int(NOW * 1000) + i}, now=NOW)

    def down(batch: list) -> None:
        raise mc_agent.IngestError("HTTP 503")

    with pytest.raises(mc_agent.IngestError):
        spool.flush(down, now=NOW)
    assert len(spool) == 3
    got: list[list[int]] = []
    assert spool.flush(lambda b: got.append([s["ts"] - int(NOW * 1000) for s in b]), now=NOW) == 3
    assert got == [[0, 1, 2]] and len(spool) == 0


def test_spool_keeps_24_hours_and_batches(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mc_agent, "METRICS_BATCH", 2)
    spool = mc_agent.Spool(tmp_path / "spool.jsonl")
    spool.add({"ts": int((NOW - 90000) * 1000)}, now=NOW - 90000)
    for i in range(5):
        spool.add({"ts": int(NOW * 1000) + i}, now=NOW)
    sizes: list[int] = []

    def flaky(batch: list) -> None:
        if len(sizes) == 2:
            raise mc_agent.IngestError("down")
        sizes.append(len(batch))

    with pytest.raises(mc_agent.IngestError):
        spool.flush(flaky, now=NOW)
    assert sizes == [2, 2] and len(spool) == 1  # the day-old sample was dropped; 4 went out


def test_push_metrics_never_raises(settings: mc_agent.Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    def down(*a: object) -> None:
        raise mc_agent.IngestError("POST /ingest/metrics: URLError")

    monkeypatch.setattr(mc_agent, "post_json", down)
    spool = mc_agent.Spool(settings.state_dir / "spool.jsonl")
    t0 = int(time.time() * 1000)
    assert mc_agent.push_metrics(settings, spool, {"ts": t0}) == 0
    sent: list = []
    monkeypatch.setattr(mc_agent, "post_json", lambda s, path, body: sent.append((path, body)))
    assert mc_agent.push_metrics(settings, spool, {"ts": t0 + 1}) == 2
    assert sent == [("/ingest/metrics", {"agent": "1", "samples": [{"ts": t0}, {"ts": t0 + 1}]})]


def test_collect_reads_rcon_gc_and_host(fake_server, tmp_path: Path, monkeypatch) -> None:
    replies = {
        "mspt": "§6Server tick times §e(§7avg§e/§7min§e/§7max§e)§6 from last 5s§7,§6 10s§7,§6 1m§e:\n"
        "§a5.1§7/§a1.0§7/§a9.9§e, §a5.2§7/§a1.0§7/§a9.9§e, §a5.3§7/§a1.0§7/§a9.9",
        "tps": "§6TPS from last 1m, 5m, 15m: §a20.0, §a19.9, §a19.8",
        "list": "There are 2 of a max of 20 players online: a, b",
    }
    fake_server(lambda cmd: replies[cmd])
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs/gc.log").write_text(
        "[2026][12.0s][info][gc] GC(1) Pause Young (Normal) (G1 Evacuation Pause) 3000M->1200M(6656M) 12.3ms\n"
    )
    monkeypatch.setattr(mc_agent, "unit_memory_mb", lambda unit="": 4000)
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp_path / "admin-stopped")
    counters = mc_agent.Counters()
    first = mc_agent.collect(counters, now=NOW, server_dir=tmp_path)
    assert first["up"] is True and first["tps"] == 20.0 and first["mspt"] == 5.3
    assert (first["players"], first["max_players"]) == (2, 20)
    assert (first["heap_used_mb"], first["heap_max_mb"], first["gc_max_pause_ms"]) == (1200, 6656, 12.3)
    assert first["jvm_mem_mb"] == 4000 and first["admin_stopped"] is False
    assert first["net_rx_bps"] is None  # no previous sample yet
    second = mc_agent.collect(counters, now=NOW + 60, server_dir=tmp_path)
    assert second["gc_max_pause_ms"] == 0  # that pause was counted already
    assert second["net_rx_bps"] is not None and second["ts"] == int((NOW + 60) * 1000)


def test_collect_without_a_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mc_agent, "unit_memory_mb", lambda unit="": None)
    sample = mc_agent.collect(mc_agent.Counters(), now=NOW, server_dir=tmp_path)
    assert sample["up"] is None and sample["tps"] is None and sample["heap_used_mb"] is None


def test_proc_parsers() -> None:
    dev = "Inter-|\n face |\n    lo: 100 1 0 0 0 0 0 0 100 1 0 0 0 0 0 0\n  eth0: 500 5 0 0 0 0 0 0 700 7 0 0 0 0 0 0\n"
    assert mc_agent.parse_net_dev(dev) == (500, 700)
    assert mc_agent.parse_meminfo("MemTotal: 8000 kB\nMemAvailable: 2000 kB\n") == 75.0
    assert mc_agent.cpu_busy_pct([0] * 8, [50, 0, 50, 800, 100, 0, 0, 0]) == 10.0


# --- logs ----------------------------------------------------------------------------------------------------


def jline(i: int, unit: str = "minecraft.service", **extra: str) -> dict[str, Any]:
    raw = {"__CURSOR": f"c{i}", "__REALTIME_TIMESTAMP": str(int((NOW + i) * 1e6)), "_SYSTEMD_UNIT": unit,
           "PRIORITY": "6", "MESSAGE": f"line {i}"}  # fmt: skip
    return mc_agent.journal_entry(raw | extra)  # type: ignore[return-value]


def test_journal_entry_and_d1_selection() -> None:
    e = mc_agent.journal_entry({"__REALTIME_TIMESTAMP": "1000000", "MESSAGE": [104, 105], "MC_AUDIT": "1",
                                "SYSLOG_IDENTIFIER": "mc-agent"})  # fmt: skip
    assert e == {
        "ts": 1000,
        "unit": "mc-agent",
        "ident": "mc-agent",
        "prio": 6,
        "msg": "hi",
        "audit": 1,
        "sd_unit": "",
    }
    assert mc_agent.for_d1(e)
    assert mc_agent.for_d1(jline(1, "mc-backup.service")) and mc_agent.for_d1(jline(1))
    assert not mc_agent.for_d1(jline(1, "cron.service")) and not mc_agent.for_d1(jline(1, "ssh.service"))
    assert mc_agent.journal_entry({"MESSAGE": "no timestamp"}) is None


def test_rows_are_capped_per_utc_day() -> None:
    st = mc_agent.LogState(day="", rows=0)
    entries = [jline(i) for i in range(5)] + [jline(9, "cron.service")]
    rows = mc_agent.select_rows(entries, st, cap=3)
    assert [r["msg"] for r in rows] == ["line 0", "line 1", "line 2"] and st.rows == 3
    tomorrow = jline(86400)
    assert len(mc_agent.select_rows([tomorrow], st, cap=3)) == 1 and st.rows == 1


def test_ship_logs_moves_the_cursor_only_after_delivery(settings, monkeypatch, tmp_path) -> None:
    path = tmp_path / "logs.json"
    entries = [jline(1), jline(2, "cron.service"), jline(3, "mc-health.service")]
    reads: list[str | None] = []

    def fake_read(cursor: str | None) -> tuple[list, str, bool]:
        reads.append(cursor)
        return entries, "c3", False

    sent: list[tuple[str, Any]] = []
    fail = {"on": True}

    def fake_request(s, method, path_, body=None, ctype="", timeout=0.0):
        if fail["on"]:
            raise mc_agent.IngestError("POST /ingest/archive: HTTP 503")
        sent.append((path_, gzip.decompress(body).decode().splitlines()))
        return {}

    monkeypatch.setattr(mc_agent, "request", fake_request)
    monkeypatch.setattr(mc_agent, "post_json", lambda s, p, body: sent.append((p, body)))
    with pytest.raises(mc_agent.IngestError):
        mc_agent.ship_logs(settings, path, fake_read, scrub=str)
    assert mc_agent.LogState.load(path).cursor is None  # nothing delivered: same entries next round
    fail["on"] = False
    assert mc_agent.ship_logs(settings, path, fake_read, scrub=str) == (2, False)
    assert reads == [None, None]
    (archive, lines), (logs, body) = sent
    # P8 review: only the product's units leave the box (cron.service stays local)
    assert archive.startswith("/ingest/archive?batch=") and len(lines) == 2
    assert logs == "/ingest/logs" and [r["unit"] for r in body["rows"]] == [
        "minecraft.service",
        "mc-health.service",
    ]
    st = mc_agent.LogState.load(path)
    assert st.cursor == "c3" and st.rows == 2


def test_archive_keeps_only_the_product_units_and_firewall_drops() -> None:
    keep = [jline(1), jline(2, "mc-backup.service"), jline(3, "mc-agent.service"),
            jline(4, "", SYSLOG_IDENTIFIER="mc-health"),
            jline(5, "", SYSLOG_IDENTIFIER="kernel", MESSAGE="mc-ratelimit: IN=eth0 SRC=203.0.113.9")]  # fmt: skip
    drop = [jline(6, "ssh.service"), jline(7, "", SYSLOG_IDENTIFIER="sudo"),
            jline(8, "", SYSLOG_IDENTIFIER="ansible-ansible.legacy.command"),
            jline(9, "", SYSLOG_IDENTIFIER="kernel", MESSAGE="usb 1-1: new device"), jline(10, "cron.service")]  # fmt: skip
    assert all(mc_agent.for_archive(e) for e in keep)
    assert not any(mc_agent.for_archive(e) for e in drop)


@pytest.mark.parametrize(
    ("text", "leak"),
    [
        (f"agent token {TOKEN} leaked", TOKEN),
        (f"key={KEY.decode()}", KEY.decode()),
        ("rcon pw s3cretRconPassw0rd in a stack trace", "s3cretRconPassw0rd"),
        ("auth with tskey-auth-kAbC123-XYZdef456", "tskey-auth-kAbC123-XYZdef456"),
        ("Authorization: Bearer abc.def.ghi", "abc.def.ghi"),
        ("url?password=hunter22&x=1", "hunter22"),
        ("TOKEN: 0123456789abcdef", "0123456789abcdef"),
        ("api_key=sk_live_123456", "sk_live_123456"),
        ("id AKIAIOSFODNN7EXAMPLE here", "AKIAIOSFODNN7EXAMPLE"),
        ("aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
        ("posting to https://discord.com/api/webhooks/123/abcDEF_ghi", "abcDEF_ghi"),
        ("https://canary.discordapp.com/api/webhooks/1/x-y", "webhooks/1/x-y"),
    ],
)
def test_scrubber_removes_known_values_and_secret_shapes(text: str, leak: str) -> None:
    scrub = mc_agent.Scrubber([TOKEN, KEY.decode(), "s3cretRconPassw0rd", "short"])
    out = scrub(text)
    assert leak not in out and "<redacted>" in out


# P9 review: the shapes the first scrubber missed. Values are built at run time (low-entropy, joined) so the
# source holds no secret-looking literal for gitleaks to flag.
_V = "Zq" + "9x" * 6  # a made-up value


@pytest.mark.parametrize(
    "text",
    [
        f"RESTIC_PASSWORD={_V}",
        f"HEARTBEAT_TOKEN={_V} next",
        f"R2_SECRET_ACCESS_KEY={_V}",
        f"export MY_API_SECRET={_V}",
        f"panel_github_token: {_V}",
        f'{{"password": "{_V} with spaces"}}',
        f"{{'agent_token': '{_V}'}}",
        f'env AGENT_TOKEN="{_V}"',
        f"Authorization: Basic {_V}==",
        f"fetch https://user:{_V}@example.invalid/repo.git",
        f"s3://key:{_V}@bucket",
        "token ghp_" + "a1" * 18,
        "github_pat_" + "B2" * 20,
    ],
)
def test_scrubber_catches_prefixed_names_quoted_values_basic_and_userinfo(text: str) -> None:
    out = mc_agent.Scrubber([])(text)
    assert _V not in out and "a1a1a1" not in out and "B2B2B2" not in out and "<redacted>" in out, out


def test_scrubber_knows_every_secret_the_agent_can_read(settings, monkeypatch) -> None:
    monkeypatch.setattr(mc_agent.mc_rcon, "read_properties", lambda path: {"rcon.password": "rcon" + _V})
    env = {"HEARTBEAT_TOKEN": "hb" + _V, "DISCORD_WEBHOOK_URL": "https://x.invalid/" + _V, "PANEL_SOME_TOKEN": "gh" + _V,
           "SOME_OTHER_SECRET": "o" + _V, "INGEST_URL": "https://hb.example.invalid", "METRICS_INTERVAL_S": "60"}  # fmt: skip
    known = mc_agent.known_secrets(settings, env)
    for value in (
        TOKEN,
        KEY.decode(),
        "hb" + _V,
        "https://x.invalid/" + _V,
        "gh" + _V,
        "o" + _V,
        "rcon" + _V,
    ):
        assert value in known
    assert "https://hb.example.invalid" not in known and "60" not in known
    scrub = mc_agent.Scrubber(known)
    assert scrub(f"leak gh{_V} and rcon{_V} plain") == "leak <redacted> and <redacted> plain"


def test_scrubber_leaves_ordinary_lines_alone() -> None:
    scrub = mc_agent.Scrubber([TOKEN])
    for line in ["Done (38.4s)! For help, type \"help\"", "Em_K joined the game", "Saved the game",
                 "snapshot 8c41d2e7 saved", "sha256 9be1c0ffee"]:  # fmt: skip
        assert scrub(line) == line
    # words that only look like secret names stay
    for line in ["bypass=on", "passed: 5 tests", "Unexpected token: }", "keyboard: ok", "monkey=1"]:
        assert scrub(line) == line, line


def test_ship_logs_scrubs_before_sending(settings, monkeypatch, tmp_path) -> None:
    sent: list[Any] = []
    monkeypatch.setattr(
        mc_agent, "request", lambda s, m, p, body=None, *a: sent.append(gzip.decompress(body))
    )
    monkeypatch.setattr(mc_agent, "post_json", lambda s, p, body: sent.append(json.dumps(body).encode()))
    monkeypatch.setattr(
        mc_agent.mc_rcon, "read_properties", lambda path: {"rcon.password": "rconPassw0rdXYZ"}
    )
    entries = [jline(1, MESSAGE=f"Bearer {TOKEN}"), jline(2, MESSAGE="rcon rconPassw0rdXYZ")]
    mc_agent.ship_logs(settings, tmp_path / "logs.json", lambda c: (entries, "c2", False))
    blob = b"".join(sent)
    assert TOKEN.encode() not in blob and b"rconPassw0rdXYZ" not in blob and blob.count(b"<redacted>") == 4


FAKE_JOURNALCTL = r"""
import json, sys
n = int(sys.argv[1])
start = int(sys.argv[2]) if len(sys.argv) > 2 else 0
pad = "x" * 400
for i in range(start, start + n):
    sys.stdout.write(json.dumps({"__CURSOR": f"c{i}", "__REALTIME_TIMESTAMP": str(1_800_000_000_000_000 + i),
                                 "_SYSTEMD_UNIT": "minecraft.service", "PRIORITY": "6",
                                 "MESSAGE": f"line {i} {pad}"}) + "\n")
"""


def test_read_journal_streams_a_large_backlog_within_a_memory_bound(tmp_path: Path) -> None:
    import sys
    import tracemalloc

    script = tmp_path / "journalctl.py"
    script.write_text(FAKE_JOURNALCTL)
    # 200000 entries (~90 MB of JSON) waiting; one batch reads 5000 and stops.
    tracemalloc.start()
    entries, cursor, more = mc_agent.read_journal("c0", argv=[sys.executable, str(script), "200000"])
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(entries) == mc_agent.LOG_BATCH_MAX_ENTRIES == 5000 and more
    assert cursor == "c4999" and entries[-1]["msg"].startswith("line 4999")
    assert peak < 16 << 20, f"peak {peak / 1e6:.1f} MB"
    # the next batch starts after that cursor; a short batch says nothing more is waiting
    entries, cursor, more = mc_agent.read_journal(cursor, argv=[sys.executable, str(script), "10", "5000"])
    assert cursor == "c5009" and len(entries) == 10 and not more
    # the byte cap cuts a batch early as well
    entries, _, more = mc_agent.read_journal(None, max_bytes=4000, argv=[sys.executable, str(script), "100"])
    assert more and len(entries) == 10


def test_read_journal_asks_only_for_the_fields_it_keeps() -> None:
    argv = mc_agent.journal_argv("c1")
    assert "--after-cursor=c1" in argv and "-n" not in argv  # -n would tail, skipping the middle of a backlog
    fields = next(a for a in argv if a.startswith("--output-fields="))
    assert set(fields.split("=", 1)[1].split(",")) == {"_SYSTEMD_UNIT", "SYSLOG_IDENTIFIER", "PRIORITY",
                                                       "MESSAGE", "MC_AUDIT", "UNIT"}  # fmt: skip
    assert "--since=-5min" in mc_agent.journal_argv(None)


def test_read_journal_reports_a_failing_journalctl(tmp_path: Path) -> None:
    import sys

    with pytest.raises(mc_agent.IngestError, match="exit 3"):
        mc_agent.read_journal(None, argv=[sys.executable, "-c", "raise SystemExit(3)"])
    with pytest.raises(mc_agent.IngestError, match="FileNotFoundError"):
        mc_agent.read_journal(None, argv=[str(tmp_path / "missing")])


def test_slicer_cedes_after_each_slice() -> None:
    now = [0.0]
    sleeps: list[float] = []
    s = mc_agent.Slicer(slice_s=0.02, pause_s=0.005, clock=lambda: now[0], sleep=sleeps.append)
    for _ in range(10):
        s()
        now[0] += 0.005
    assert sleeps == [0.005, 0.005] and s.pauses == 2  # at 0.02 and 0.045 (the clock restarts after a pause)


def test_ship_logs_and_read_journal_cede_per_entry(settings, monkeypatch, tmp_path) -> None:
    """P13 review M1: the CPU-bound log path checks in once per entry (parse and scrub)."""
    monkeypatch.setattr(mc_agent, "request", lambda *a, **k: {})
    monkeypatch.setattr(mc_agent, "post_json", lambda *a, **k: {})
    entries = [
        {"ts": 1, "unit": "mc-backup.service", "prio": 6, "audit": False, "msg": f"m{i}"} for i in range(7)
    ]
    ticks: list[int] = []
    mc_agent.ship_logs(settings, tmp_path / "l.json", lambda c: (entries, "c", False), scrub=str,
                       cede=lambda: ticks.append(1))  # fmt: skip
    assert len(ticks) == 7
    script = tmp_path / "j.py"
    script.write_text(
        "import json\nfor i in range(5): print(json.dumps({'__CURSOR': str(i), 'MESSAGE': 'x'}))\n"
    )
    ticks.clear()
    mc_agent.read_journal(None, argv=[sys.executable, str(script)], cede=lambda: ticks.append(1))
    assert len(ticks) == 5


def test_a_scrub_backlog_does_not_starve_the_metrics_thread(settings, monkeypatch, tmp_path) -> None:
    """P13 review M1: a long CPU-bound scrub (a journal backlog) shares the GIL with the metrics thread (the
    Worker's "host alive" signal). Without slicing, every GIL release of the light thread costs it a full
    switch interval (5 ms); with the Slicer it runs during the scrubber's pauses."""
    monkeypatch.setattr(mc_agent, "request", lambda *a, **k: {})
    monkeypatch.setattr(mc_agent, "post_json", lambda *a, **k: {})
    entries = [
        {"ts": 1, "unit": "mc-backup.service", "prio": 6, "audit": False, "msg": "m"} for _ in range(3000)
    ]

    def slow_scrub(text: str) -> str:
        end = time.perf_counter() + 0.001  # pure-Python CPU: holds the GIL
        while time.perf_counter() < end:
            pass
        return text

    scrubbing = threading.Thread(
        target=mc_agent.ship_logs,
        args=(settings, tmp_path / "l.json", lambda c: (entries, "c", False), slow_scrub),
    )
    scrubbing.start()
    time.sleep(0.05)
    started = time.monotonic()
    for _ in range(400):  # a metrics round: many short system calls, each releasing the GIL
        time.sleep(0)
    light = time.monotonic() - started
    scrubbing.join()
    assert light < 0.8, f"the light thread waited {light:.2f} s behind the scrubber"


def test_logs_loop_drains_a_backlog_batch_by_batch(settings, monkeypatch) -> None:
    calls: list[int] = []

    def fake_ship(s, path, observe=None, observe_all=None):
        calls.append(1)
        return 5000, len(calls) < 3

    monkeypatch.setattr(mc_agent, "ship_logs", fake_ship)
    agent = mc_agent.Agent(dataclasses.replace(settings, log_batch_s=0))
    waits = iter([False, True])
    monkeypatch.setattr(agent.stop, "wait", lambda t=None: next(waits))
    agent.logs_loop()
    assert len(calls) == 3  # three batches in one round, each moving the cursor


# --- HTTP to the Worker ------------------------------------------------------------------------------------


@pytest.fixture
def worker() -> Iterator[tuple[str, list[dict[str, Any]]]]:
    seen: list[dict[str, Any]] = []

    class H(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            pass

        def _reply(self, code: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:
            seen.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
            self._reply(200, {"commands": [signed("status", iat=time.time())]})

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append({"method": "POST", "path": self.path, "body": json.loads(body),
                         "auth": self.headers.get("Authorization")})  # fmt: skip
            self._reply(200 if self.headers.get("Authorization") == f"Bearer {TOKEN}" else 401, {"ok": True})

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()


def test_poll_fallback_answers_commands(worker, tmp_path: Path, monkeypatch) -> None:
    url, seen = worker
    monkeypatch.setattr(mc_agent, "audit", lambda *a, **k: None)
    agent = mc_agent.Agent(mc_agent.Settings(url, TOKEN, KEY, state_dir=tmp_path))
    agent.executor = mc_agent.Executor(lambda: {"ok": 1})
    assert agent.poll_once() == 1
    get, post = seen
    assert get["path"] == "/agent/commands" and get["auth"] == f"Bearer {TOKEN}"
    assert post["path"] == "/agent/results" and post["body"]["results"][0]["output"] == '{"ok":1}'


def test_http_errors_never_carry_the_token(tmp_path: Path) -> None:
    s = mc_agent.Settings("http://127.0.0.1:9", TOKEN, KEY, state_dir=tmp_path)
    with pytest.raises(mc_agent.IngestError) as e:
        mc_agent.post_json(s, "/ingest/metrics", {"samples": []})
    assert TOKEN not in str(e.value) and "127.0.0.1" not in str(e.value)


@pytest.mark.parametrize(
    ("url", "ok"),
    [("https://hb.mc.example.com", True), ("http://127.0.0.1:8787", True), ("http://[::1]:1", True),
     ("http://localhost:1", True), ("http://hb.mc.example.com", False), ("ftp://x", False), ("", False)],
)  # fmt: skip
def test_ingest_url_is_https_or_loopback(url: str, ok: bool) -> None:
    if ok:
        mc_agent.check_url(url)
    else:
        with pytest.raises(ValueError, match="https"):
            mc_agent.check_url(url)


def test_settings_require_secrets_and_clamp() -> None:
    env = {"INGEST_URL": "https://hb.x.y/", "AGENT_TOKEN": TOKEN, "COMMAND_HMAC_KEY": "k" * 64,
           "METRICS_INTERVAL_S": "5", "STATE_DIRECTORY": "/var/lib/mc-agent"}  # fmt: skip
    s = mc_agent.Settings.from_env(env)
    assert s.ingest_url == "https://hb.x.y" and s.metrics_interval_s == 30 and s.hmac_key == b"k" * 64
    with pytest.raises(ValueError, match="AGENT_TOKEN"):
        mc_agent.Settings.from_env(env | {"AGENT_TOKEN": ""})


# --- WebSocket client --------------------------------------------------------------------------------------


def server_frame(opcode: int, payload: bytes, fin: bool = True) -> bytes:
    head = bytes([(0x80 if fin else 0) | opcode])
    n = len(payload)
    head += bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n)
    return head + payload


def read_client_frame(conn: socket.socket) -> tuple[int, bytes]:
    b0, b1 = conn.recv(2)
    n = b1 & 0x7F
    if n == 126:
        (n,) = struct.unpack(">H", conn.recv(2))
    assert b1 & 0x80, "client frames must be masked"
    mask = conn.recv(4)
    data = b""
    while len(data) < n:
        data += conn.recv(n - len(data))
    return b0 & 0x0F, bytes(b ^ mask[i % 4] for i, b in enumerate(data))


def test_websocket_handshake_ping_fragments_and_close() -> None:
    lsock = socket.create_server(("127.0.0.1", 0))
    port = lsock.getsockname()[1]
    got: dict[str, Any] = {}

    def serve() -> None:
        conn, _ = lsock.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        lines = head.decode().split("\r\n")
        got["request"] = lines[0]
        hdr = {k.lower(): v.strip() for k, _, v in (ln.partition(":") for ln in lines[1:] if ln)}
        got["auth"] = hdr.get("authorization")
        accept = base64.b64encode(
            hashlib.sha1((hdr["sec-websocket-key"] + mc_agent.WS_GUID).encode()).digest()  # noqa: S324
        )
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + accept + b"\r\n\r\n")  # fmt: skip
        got["hello"] = read_client_frame(conn)
        conn.sendall(server_frame(mc_agent.OP_PING, b"p"))
        conn.sendall(server_frame(mc_agent.OP_TEXT, b'{"type":"com', fin=False))
        conn.sendall(server_frame(mc_agent.OP_CONT, b'mand"}' + b" " * 300))
        got["pong"] = read_client_frame(conn)
        got["reply"] = read_client_frame(conn)
        conn.sendall(server_frame(mc_agent.OP_CLOSE, struct.pack(">H", 1000)))
        got["close"] = read_client_frame(conn)
        conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    ws = mc_agent.WebSocket.connect(f"ws://127.0.0.1:{port}/agent/ws", {"Authorization": f"Bearer {TOKEN}"})
    ws.send_text("hello")
    msg = ws.recv()
    assert json.loads(msg or "") == {"type": "command"}
    ws.send_text("x" * 200)  # > 125 bytes: 16-bit length
    assert ws.recv() is None
    t.join(5)
    lsock.close()
    assert got["request"] == "GET /agent/ws HTTP/1.1" and got["auth"] == f"Bearer {TOKEN}"
    assert got["hello"] == (mc_agent.OP_TEXT, b"hello")
    assert got["pong"] == (mc_agent.OP_PONG, b"p")
    assert got["reply"] == (mc_agent.OP_TEXT, b"x" * 200)
    assert got["close"][0] == mc_agent.OP_CLOSE


def test_websocket_refuses_a_bad_accept() -> None:
    lsock = socket.create_server(("127.0.0.1", 0))

    def serve() -> None:
        conn, _ = lsock.accept()
        conn.recv(4096)
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: nope\r\n\r\n")
        conn.close()

    threading.Thread(target=serve, daemon=True).start()
    with pytest.raises(mc_agent.WebSocketError, match="Accept"):
        mc_agent.WebSocket.connect(f"ws://127.0.0.1:{lsock.getsockname()[1]}/", {})
    lsock.close()


def ws_pair(timeout: float = 0.2) -> tuple[mc_agent.WebSocket, socket.socket]:
    a, b = socket.socketpair()
    a.settimeout(timeout)
    return mc_agent.WebSocket(a), b


@pytest.mark.parametrize(
    ("raw", "why"),
    [
        (bytes([0x81, 0x80 | 2]) + b"\x01\x02\x03\x04" + b"hi", "masked frame"),
        (bytes([0x81 | 0x40, 2]) + b"hi", "reserved bits"),
        (bytes([0x89, 126, 0, 200]) + b"p" * 200, "control frame"),
        (bytes([0x09, 1]) + b"p", "control frame"),  # a fragmented ping
        (bytes([0x80, 1]) + b"x", "continuation without"),
        (bytes([0x01, 1]) + b"a" + bytes([0x81, 1]) + b"b", "inside a fragmented"),
        (bytes([0x83, 1]) + b"x", "unknown opcode"),
        (bytes([0x81, 127]) + struct.pack(">Q", 1 << 40), "too large"),
    ],
)
def test_websocket_rejects_protocol_violations(raw: bytes, why: str) -> None:
    ws, peer = ws_pair()
    peer.sendall(raw)
    with pytest.raises(mc_agent.WebSocketError, match=why):
        ws.recv()
    peer.close()


def test_websocket_timeout_mid_frame_keeps_the_framing() -> None:
    ws, peer = ws_pair()
    msg = json.dumps({"type": "command", "pad": "x" * 300}).encode()
    frame = server_frame(mc_agent.OP_TEXT, msg)
    peer.sendall(frame[:3])  # header only partly there
    with pytest.raises(TimeoutError):
        ws.recv()
    peer.sendall(frame[3:50])
    with pytest.raises(TimeoutError):
        ws.recv()
    peer.sendall(frame[50:] + server_frame(mc_agent.OP_TEXT, b"pong"))
    assert ws.recv() == msg.decode() and ws.recv() == "pong"
    # a fragmented message survives a timeout between its frames as well
    peer.sendall(server_frame(mc_agent.OP_TEXT, b"ab", fin=False))
    with pytest.raises(TimeoutError):
        ws.recv()
    peer.sendall(server_frame(mc_agent.OP_CONT, b"cd"))
    assert ws.recv() == "abcd"
    peer.close()


def handshake_server(behaviour: Any) -> tuple[int, socket.socket, dict[str, Any]]:
    lsock = socket.create_server(("127.0.0.1", 0))
    got: dict[str, Any] = {"frames": []}

    def serve() -> None:
        conn, _ = lsock.accept()
        head = b""
        while b"\r\n\r\n" not in head:
            head += conn.recv(4096)
        hdr = {
            k.lower(): v.strip()
            for k, _, v in (ln.partition(":") for ln in head.decode().split("\r\n")[1:] if ln)
        }
        accept = base64.b64encode(
            hashlib.sha1((hdr["sec-websocket-key"] + mc_agent.WS_GUID).encode()).digest()  # noqa: S324
        )
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: " + accept + b"\r\n\r\n")
        behaviour(conn, got)

    threading.Thread(target=serve, daemon=True).start()
    return lsock.getsockname()[1], lsock, got


def test_websocket_session_ends_when_the_path_goes_quiet(settings, monkeypatch) -> None:
    monkeypatch.setattr(mc_agent, "WS_RECV_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mc_agent, "WS_PING_S", 0.15)
    monkeypatch.setattr(mc_agent, "WS_DEAD_S", 0.5)

    def silent(conn: socket.socket, got: dict[str, Any]) -> None:
        conn.settimeout(2)
        with contextlib.suppress(OSError, ValueError):
            while True:
                got["frames"].append(read_client_frame(conn))

    port, lsock, got = handshake_server(silent)
    agent = mc_agent.Agent(dataclasses.replace(settings, ingest_url=f"http://127.0.0.1:{port}"))
    t0 = time.monotonic()
    with pytest.raises(mc_agent.WebSocketError, match="nothing received"):
        agent.ws_session()
    assert 0.4 < time.monotonic() - t0 < 3 and agent.ws_established and not agent.ws_connected
    time.sleep(0.1)
    ops = [(op, data) for op, data in got["frames"]]
    assert (mc_agent.OP_TEXT, b"ping") in ops and (mc_agent.OP_PING, b"") in ops
    lsock.close()


def test_websocket_session_stays_up_while_pongs_arrive(settings, monkeypatch) -> None:
    monkeypatch.setattr(mc_agent, "WS_RECV_TIMEOUT_S", 0.05)
    monkeypatch.setattr(mc_agent, "WS_PING_S", 0.1)
    monkeypatch.setattr(mc_agent, "WS_DEAD_S", 0.4)

    def answers(conn: socket.socket, got: dict[str, Any]) -> None:
        conn.settimeout(2)
        t_end = time.monotonic() + 1.2  # three dead-intervals long
        while time.monotonic() < t_end:
            op, data = read_client_frame(conn)
            if op == mc_agent.OP_TEXT and data == b"ping":
                got["frames"].append(data)
                conn.sendall(server_frame(mc_agent.OP_TEXT, b"pong"))
        conn.sendall(server_frame(mc_agent.OP_CLOSE, struct.pack(">H", 1000)))
        with contextlib.suppress(OSError):
            read_client_frame(conn)

    port, lsock, got = handshake_server(answers)
    agent = mc_agent.Agent(dataclasses.replace(settings, ingest_url=f"http://127.0.0.1:{port}"))
    agent.ws_session()  # returns normally on the server's close
    assert len(got["frames"]) >= 3
    lsock.close()


def test_frame_encoding_lengths_and_mask() -> None:
    frame = mc_agent.encode_frame(mc_agent.OP_TEXT, b"abc", mask=b"\x01\x02\x03\x04")
    assert frame == bytes([0x81, 0x83, 1, 2, 3, 4, ord("a") ^ 1, ord("b") ^ 2, ord("c") ^ 3])
    assert mc_agent.encode_frame(1, b"x" * 70000, mask=b"\0" * 4)[1] == 0x80 | 127


# --- budget (design §5a: validation rejects settings above ~half a daily cap) ----------------------------------


def test_budget_at_the_defaults_is_within_half_of_the_free_caps() -> None:
    from mcctl import schema

    agent = mc_agent.daily_budget()
    assert agent == {
        "requests": 1440 + 2 * 288 + 5760 + 144 + 400 + 1728,
        "d1_rows": 1440 + 30000 + 288 + 144 + 576 + 100 + 5480,
    }
    assert mc_agent.PANEL_REQUESTS == schema.PANEL_ACTIONS_PER_DAY
    assert mc_agent.PANEL_ROWS == schema.PANEL_ACTION_ROWS_PER_DAY
    assert mc_agent.EXPERIENCE_REQUESTS == schema.EXPERIENCE_REQUESTS_PER_DAY
    assert mc_agent.EXPERIENCE_ROWS == schema.EXPERIENCE_ROWS_PER_DAY
    cfg = schema.load_server(
        {"schema_version": 1, "name": "lh", "admin": {"name": "Sam", "tailscale_login": "sam@github",
         "email": "sam@x.internal"}, "dns": {"zone": "x.internal"}, "github": {"owner": "o", "config_repo": "r"}, "minecraft": {"version": "26.3", "accept_eula": True}}
    )  # fmt: skip
    est = schema.budget(cfg)
    requests, req_cap = est["Worker requests/day"]
    rows, row_cap = est["D1 rows written/day"]
    assert requests >= agent["requests"] and rows >= agent["d1_rows"]  # the validator counts the agent fully
    assert requests <= 0.5 * req_cap and rows <= 0.5 * row_cap
    # v1.1: with the client modpack on, the validator counts the agent's code requests too
    with_client = mc_agent.daily_budget(client=True)
    est_c = schema.budget(dataclasses.replace(cfg, client=dataclasses.replace(cfg.client, enabled=True)))
    assert est_c["Worker requests/day"][0] >= with_client["requests"]
    assert est_c["D1 rows written/day"][0] >= with_client["d1_rows"]
    assert (
        est_c["Worker requests/day"][0] <= 0.5 * req_cap and est_c["D1 rows written/day"][0] <= 0.5 * row_cap
    )
    # The spec's ceiling (50000 rows at a 30 s interval) still validates (<= 55 %).
    # P13: with experience telemetry on, that ceiling no longer fits (validation then rejects it: lower
    # max_rows_per_day or turn experience off); without it, it still does.
    fast = mc_agent.daily_budget(30, 50000, experience=False)
    assert mc_agent.daily_budget(30, 50000)["d1_rows"] > schema.BUDGET_FRACTION * row_cap
    assert fast["d1_rows"] + schema.AUDIT_WATCHDOG_ROWS_PER_DAY <= schema.BUDGET_FRACTION * row_cap


def test_signature_vector_shared_with_the_worker_tests() -> None:
    # watchdog/test/monitoring.test.mjs asserts the same digest for the Worker's hmacHex.
    want = "185e8cabe8ec874b9adbc8d200ab798033279c0ff095465c6cb31a0803ce3e2a"
    assert mc_agent.sign(b"k" * 64, '{"id":"x"}') == want
    assert want in (Path(__file__).parent.parent / "watchdog/test/monitoring.test.mjs").read_text()


def test_the_e2e_agent_check_splits_its_rounds_into_two_shards(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1.3: agent_check.py's rounds as two CI shards (--rounds a / b): together every round once, each with what
    its rounds need; without --rounds, every round in the full run's order."""
    import importlib

    repo = Path(__file__).parent.parent
    monkeypatch.syspath_prepend(str(repo / "tests/e2e"))
    monkeypatch.setenv("MC_SECRET_AGENT_TOKEN", "t")
    monkeypatch.setenv("MC_SECRET_COMMAND_HMAC_KEY", "k")
    check = importlib.import_module("agent_check")
    a, b = check.select_rounds("a"), check.select_rounds("b")
    assert sorted(a + b) == sorted(check.ROUNDS) and not set(a) & set(b)
    every = list(check.ROUNDS)
    assert check.select_rounds(None) == check.select_rounds("all") == check.select_rounds("a,b") == every
    for group in (a, b):  # a round's precondition from another round is in the same group, before it
        for r, needs in check.NEEDS.items():
            assert r not in group or group.index(needs) < group.index(r)
    assert check.select_rounds("whitelist") == ["stop-start", "whitelist"]
    with pytest.raises(SystemExit, match="unknown"):
        check.select_rounds("a,nope")
    # the one round of each group that drives the GitHub mock (the world round seeds players.yml itself)
    assert "whitelist" in a and "world" in b
    wf = (repo / ".github/workflows/e2e-mock.yml").read_text()
    assert "{shard: agent-a," in wf and "{shard: agent-b," in wf and "{shard: agent," not in wf


def test_the_e2e_cli_round_runs_what_mcctl_runs_over_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """v1.3: agent_check.py's cli round runs `mc_agent.py cli` with mcctl's very command line (mcctl/actions.py),
    as the login shape the agent accepts, and its made-up players are well-formed pending entries."""
    import importlib
    import shlex

    from mcctl import actions

    repo = Path(__file__).parent.parent
    monkeypatch.syspath_prepend(str(repo / "tests/e2e"))
    monkeypatch.setenv("MC_SECRET_AGENT_TOKEN", "t")
    monkeypatch.setenv("MC_SECRET_COMMAND_HMAC_KEY", "k")
    check = importlib.import_module("agent_check")
    args = {"verb": "kick", "target": "E2E_Nobody"}
    assert shlex.join(check.cli_argv("moderate", args)) == actions.agent_command(
        check.CLI_LOGIN, "moderate", args
    )
    assert shlex.join(check.cli_argv("worlds")) == actions.agent_command(check.CLI_LOGIN, "worlds")
    assert mc_agent.CLI_LOGIN.fullmatch(check.CLI_LOGIN) and "cli" in check.GROUPS["a"]
    uuid = f"{check.CLI_UUID_PREFIX}{3:04x}{19:08x}"
    assert mc_panel.UUID.fullmatch(uuid) and mc_panel.NAME.fullmatch("E2eCli3_19")
    assert (
        check.CLI_UUID_PREFIX in check.PENDING_WRITER and check.CLI_UUID_PREFIX[:9] in check.PENDING_CLEANUP
    )


@pytest.mark.skipif(not shutil.which("flock"), reason="needs util-linux flock")
def test_the_e2e_restore_lock_holder_releases_the_lock_when_done(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """v1.3: the restore-guard round's lock holder (flock + a sleeping script that inherits the locked fd) must
    release restore.lock as soon as its block ends; it used to outlive the kill and hold the lock until its
    `sleep 300` ended, so the round's next deploy waited ~285 s for a restore that wasn't running."""
    import importlib

    repo = Path(__file__).parent.parent
    monkeypatch.syspath_prepend(str(repo / "tests/e2e"))
    monkeypatch.setenv("MC_SECRET_AGENT_TOKEN", "t")
    monkeypatch.setenv("MC_SECRET_COMMAND_HMAC_KEY", "k")
    check = importlib.import_module("agent_check")
    lock, ready = tmp_path / "restore.lock", tmp_path / "ready"
    with check.holding_lock(str(lock), f"touch {ready} && sleep 60"):
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert check.lock_held(str(lock))
    started = time.monotonic()
    assert not check.lock_held(str(lock))
    assert time.monotonic() - started < 5


def test_agent_is_wired_into_ansible_and_e2e() -> None:
    repo = Path(__file__).parent.parent
    wf = (repo / ".github/workflows/e2e-mock.yml").read_text()
    assert "python3 /src/product/tests/e2e/agent_check.py" in wf
    unit = (repo / "ansible/roles/agent/templates/mc-agent.service.j2").read_text()
    assert "User={{ mc_ops_user }}" in unit and "MemoryMax=64M" in unit
    assert "SupplementaryGroups=minecraft systemd-journal" in unit
    rule = (repo / "ansible/roles/base/templates/50-mc-ops.rules.j2").read_text()
    assert '"minecraft.service": ["restart", "kill", "start", "stop"]' in rule
    assert '"mc-backup.service": ["start"]' in rule
    assert set(mc_agent.ACTIONS) == {"status", "console", "start", "stop", "restart", "backup", "tail",
                                     "cancel", "whitelist", "rejected_dismiss", "moderate", "plugin", "restore",
                                     "player_message", "world_create", "world_switch", "world_delete",
                                     "selftest", "lookup"}  # fmt: skip
    js = (repo / "watchdog/worker.js").read_text()
    block = js.split("const COMMAND_ACTIONS = [", 1)[1].split("];", 1)[0]
    assert set(re.findall(r'"([a-z_]+)"', block)) == set(mc_agent.ACTIONS)  # the Worker signs only these


# --- P9: richer samples, inventory, live console ---------------------------------------------------------------


def test_p9_sample_parsers() -> None:
    stat = "cpu  10 0 10 80 0 0 0 0\ncpu0 5 0 5 40 0 0 0 0\ncpu1 5 0 5 40 0 0 0 0\nintr 1\n"
    assert mc_agent.parse_cores(stat) == [[5, 0, 5, 40, 0, 0, 0, 0], [5, 0, 5, 40, 0, 0, 0, 0]]
    assert mc_agent.parse_meminfo_mb("MemTotal: 8192000 kB\nMemAvailable: 2048000 kB\n") == (6000, 8000)
    assert mc_agent.parse_tps_all("§6TPS from last 1m, 5m, 15m: §a20.0, §a19.9, §a*19.8") == {
        "1m": 20.0,
        "5m": 19.9,
        "15m": 19.8,
    }
    assert mc_agent.parse_tps_all("garbage") == {}
    assert mc_agent.player_names("There are 2 of a max of 20 players online: Em_K, MiaBuilds") == [
        "Em_K",
        "MiaBuilds",
    ]
    assert mc_agent.player_names("There are 0 of a max of 20 players online: ") == []
    assert mc_agent.player_names("§eThere are 1 of a max of 20 players online: §fbad name!, ok_1") == ["ok_1"]


def test_collect_reports_cores_memory_disk_uptime_and_names(fake_server, tmp_path: Path, monkeypatch) -> None:
    replies = {"mspt": "", "tps": "TPS from last 1m, 5m, 15m: 20.0, 19.5, 19.0",
               "list": "There are 1 of a max of 20 players online: Em_K"}  # fmt: skip
    fake_server(lambda cmd: replies[cmd])
    monkeypatch.setattr(mc_agent, "unit_memory_mb", lambda unit="": 4000)
    monkeypatch.setattr(mc_agent, "unit_uptime_s", lambda unit="": 3600)
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp_path / "admin-stopped")
    stats = iter(["cpu  0 0 0 100 0 0 0 0\ncpu0 0 0 0 50 0 0 0 0\ncpu1 0 0 0 50 0 0 0 0\n",
                  "cpu  50 0 0 150 0 0 0 0\ncpu0 50 0 0 50 0 0 0 0\ncpu1 0 0 0 100 0 0 0 0\n"])  # fmt: skip
    real_read = mc_agent.read
    monkeypatch.setattr(
        mc_agent, "read", lambda path: next(stats) if path == "/proc/stat" else real_read(path)
    )
    counters = mc_agent.Counters()
    first = mc_agent.collect(counters, now=NOW, server_dir=tmp_path)
    assert first["cpu_cores"] == 2 and first["cpu0_pct"] is None  # no previous sample yet
    assert (first["tps5"], first["tps15"], first["players_online"]) == (19.5, 19.0, ["Em_K"])
    assert first["uptime_s"] == 3600 and first["disk_total_gb"] > 0
    second = mc_agent.collect(counters, now=NOW + 60, server_dir=tmp_path)
    assert (second["cpu0_pct"], second["cpu1_pct"]) == (100.0, 0.0)


def test_inventory_reads_the_servers_own_files(tmp_path: Path) -> None:
    (tmp_path / "plugins/disabled").mkdir(parents=True)
    (tmp_path / "plugins/Chunky-1.4.40.jar").write_bytes(b"")
    (tmp_path / "plugins/disabled/Old-1.0.jar").write_bytes(b"")
    (tmp_path / "whitelist.json").write_text('[{"uuid": "u1", "name": "Em_K"}, "junk"]')
    (tmp_path / "ops.json").write_text(
        '[{"uuid": "u1", "name": "Em_K", "level": 4, "bypassesPlayerLimit": false}]'
    )
    (tmp_path / "banned-players.json").write_text('[{"name": "griefer", "reason": "x" , "created": "2026"}]')
    (tmp_path / "banned-ips.json").write_text("{not json")
    inv = mc_agent.collect_inventory(tmp_path)
    assert inv["whitelist"] == [{"name": "Em_K", "uuid": "u1"}]
    # v1.3 whitelist guard: players the last deploy kept although players.yml lacks them -> "not in Git"
    (tmp_path / "whitelist.json").write_text(
        '[{"uuid": "u1", "name": "Em_K"}, {"uuid": "U2", "name": "Dora"}]'
    )
    (tmp_path / mc_panel.PLAYERS_STATE_FILE).write_text('{"git": ["u1"], "kept": ["u2"]}')
    assert mc_agent.collect_inventory(tmp_path)["whitelist"] == [
        {"name": "Em_K", "uuid": "u1"},
        {"name": "Dora", "uuid": "U2", "unmanaged": True},
    ]
    for junk in ('{"kept": "u2"}', '{"kept": [{}, 3]}', "[1]", "{no"):
        (tmp_path / mc_panel.PLAYERS_STATE_FILE).write_text(junk)
        assert all("unmanaged" not in e for e in mc_agent.collect_inventory(tmp_path)["whitelist"])
    assert inv["ops"] == [{"name": "Em_K", "uuid": "u1", "level": 4}]
    assert inv["bans"] == [{"name": "griefer", "created": "2026", "reason": "x"}] and inv["ip_bans"] == []
    assert inv["plugins"] == [
        {"file": "Chunky-1.4.40.jar", "name": "Chunky-1.4.40", "enabled": True},
        {"file": "Old-1.0.jar", "name": "Old-1.0", "enabled": False},
    ]


def test_tail_needs_the_websocket_and_streams_scrubbed_lines(settings, tmp_path: Path, monkeypatch) -> None:
    import sys

    agent = mc_agent.Agent(settings)

    def run(args: dict[str, Any]) -> dict[str, Any]:
        msg = signed("tail", args, iat=time.time())
        return mc_agent.handle_command(msg, settings, mc_agent.Nonces(None), agent.executor)

    monkeypatch.setattr(mc_agent, "audit", lambda msg, **f: None)
    r = run({"seconds": 60})
    assert not r["ok"] and "WebSocket" in r["error"]
    assert not run({"seconds": "x"})["ok"]
    sent: list[str] = []

    class FakeWs:
        def send_text(self, text: str) -> None:
            sent.append(text)

    agent.ws = FakeWs()  # type: ignore[assignment]
    lines = [json.dumps({"__REALTIME_TIMESTAMP": str(int(NOW * 1e6) + i), "PRIORITY": "6",
                         "MESSAGE": f"[Server] line {i} {TOKEN if i == 1 else ''}"}) for i in range(3)]  # fmt: skip
    script = tmp_path / "jf.py"
    script.write_text(
        f"import sys, time\nsys.stdout.write({chr(10).join(lines)!r} + '\\n')\nsys.stdout.flush()\ntime.sleep(5)\n"
    )
    monkeypatch.setattr(mc_agent.ConsoleTail, "argv", lambda self: [sys.executable, str(script)])
    monkeypatch.setattr(mc_agent.mc_rcon, "read_properties", lambda path: {})
    r = run({"seconds": 500})
    assert r["ok"] and r["output"] == "live console for 180 s"  # clamped
    deadline = time.monotonic() + 5
    while not sent and time.monotonic() < deadline:
        time.sleep(0.05)
    agent.tail.until = 0  # the viewer went away
    agent.tail.thread.join(5)  # type: ignore[union-attr]
    msg = json.loads(sent[0])
    assert msg["type"] == "console" and [ln["msg"].split(" ")[2] for ln in msg["lines"]] == ["0", "1", "2"]
    assert TOKEN not in sent[0] and "<redacted>" in sent[0]


def test_executor_refuses_start_and_restart_on_a_fenced_box(
    box: list[list[str]], settings: mc_agent.Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P12 review: the panel's start/restart must not bring a box `mcctl migrate` fenced back to life."""
    monkeypatch.setattr(notify, "FENCED_FILE", tmp_path / "fenced")
    (tmp_path / "fenced").write_text("{}")
    ex = mc_agent.Executor(lambda: {})
    run = lambda msg: mc_agent.handle_command(msg, settings, mc_agent.Nonces(None), ex)  # noqa: E731
    now = time.time()
    r = run(signed("start", iat=now))
    assert not r["ok"] and "fenced" in r["error"]
    assert not run(signed("restart", iat=now))["ok"]
    assert [c for c in box if c[0] != "AUDIT"] == []


def test_state_directory_with_several_entries_uses_the_first() -> None:
    env = {"INGEST_URL": "https://hb.example.invalid", "AGENT_TOKEN": "t" * 48, "COMMAND_HMAC_KEY": "k" * 64,
           "STATE_DIRECTORY": "/var/lib/mc-agent:/var/lib/mc-telemetry"}  # fmt: skip
    assert mc_agent.Settings.from_env(env).state_dir == Path("/var/lib/mc-agent")


def test_rcon_stats_keeps_one_connection_and_survives_a_restart(fake_server, tmp_path: Path) -> None:
    # v1.0.1: every RCON connection logs "Thread RCON Client ... started/shutting down" on the server; the
    # agent polls every minute over one kept connection, and reconnects once the server restarted.
    replies = {"mspt": "", "tps": "TPS from last 1m, 5m, 15m: 20.0, 20.0, 20.0", "list": "There are 0 of a max of 20 players online:"}  # fmt: skip
    srv = fake_server(lambda cmd: replies[cmd])
    for _ in range(5):
        assert mc_agent.rcon_stats(tmp_path)["tps"] == 20.0
    assert srv.connections == 1 and srv.commands.count("list") == 5
    srv.drop_clients()
    assert mc_agent.rcon_stats(tmp_path)["up"] is True
    assert srv.connections == 2
    srv.stop()  # the server is gone
    assert mc_agent.rcon_stats(tmp_path)["up"] is False
    mc_agent.stats_rcon(tmp_path).close()


# --- v1.3: versions everywhere, the Panel -> Git self-test, the Mojang lookup from the box ------------------------


def test_read_product(tmp_path: Path) -> None:
    f = tmp_path / "product-version.json"
    assert mc_agent.read_product(f) is None  # deployed before v1.3
    f.write_text(json.dumps({"product": "smallworld", "version": "1.3.0", "commit": "a" * 40}))
    assert mc_agent.read_product(f) == {"version": "1.3.0", "commit": "a" * 40}
    f.write_text(json.dumps({"version": "1.3.0", "commit": "not a sha"}))
    assert mc_agent.read_product(f) == {"version": "1.3.0", "commit": ""}
    f.write_text(json.dumps({"version": "<script>"}))
    assert mc_agent.read_product(f) is None
    # = the base role's file (ansible mc_product_version_file)
    gv = (Path(__file__).parent.parent / "ansible/group_vars/all/main.yml").read_text()
    assert 'mc_product_version_file: "{{ mc_root }}/product-version.json"' in gv
    assert str(mc_agent.PRODUCT_VERSION_FILE) == "/opt/minecraft/product-version.json"


def test_inventory_reports_the_installed_and_the_running_version(tmp_path: Path, monkeypatch) -> None:
    f = tmp_path / "product-version.json"
    f.write_text(json.dumps({"version": "1.2.2", "commit": "b" * 40}))
    monkeypatch.setattr(mc_agent, "PRODUCT_VERSION_FILE", f)
    agent = mc_agent.Agent(mc_agent.Settings("https://hb.example.invalid", TOKEN, KEY, state_dir=tmp_path))
    assert agent.product_state() == {"version": "1.2.2", "commit": "b" * 40, "running": "1.2.2",
                                     "running_commit": "b" * 40}  # fmt: skip
    # a deploy installed new code but the process kept running (v1.2.3's incident): the inventory shows both
    f.write_text(json.dumps({"version": "1.3.0", "commit": "c" * 40}))
    state = agent.panel_state()
    assert state["product"]["version"] == "1.3.0" and state["product"]["running"] == "1.2.2"
    assert state["selftest"] is None  # not run yet


class FakeResp:
    def __init__(self, status: int, body: bytes) -> None:
        self.status, self.body = status, body

    def __enter__(self) -> FakeResp:
        return self

    def __exit__(self, *a: object) -> None:
        pass

    def read(self, n: int = -1) -> bytes:
        return self.body


def test_lookup_from_the_box_falls_back_and_caches(monkeypatch) -> None:
    import urllib.error

    asked: list[tuple[str, float, str]] = []
    answers: dict[str, Any] = {}

    def opener(req: Any, timeout: float) -> FakeResp:
        asked.append((req.full_url, timeout, req.get_header("User-agent")))
        a = answers[req.full_url.split("/")[2]]
        if isinstance(a, BaseException):
            raise a
        if a[0] >= 400:
            raise urllib.error.HTTPError(req.full_url, a[0], "x", {}, None)  # type: ignore[arg-type]
        return FakeResp(*a)

    now = [1000.0]
    look = mc_agent.Lookup(opener=opener, clock=lambda: now[0])
    profile = json.dumps({"id": "0d61aa00bb0011224455667788997f90", "name": "Felix_2011"}).encode()
    # api.mojang.com refuses (as it refuses Cloudflare's egress), the fallback answers
    answers.update({"api.mojang.com": (403, b""), "api.minecraftservices.com": (200, profile)})
    got = look("felix_2011")
    assert got == {"found": True, "name": "Felix_2011", "uuid": "0d61aa00-bb00-1122-4455-667788997f90",
                   "via": "api.minecraftservices.com"}  # fmt: skip
    assert [a[0] for a in asked] == [
        mc_agent.MOJANG_LOOKUPS[0] + "felix_2011",
        mc_agent.MOJANG_LOOKUPS[1] + "felix_2011",
    ]
    assert {a[1] for a in asked} == {mc_agent.LOOKUP_TIMEOUT_S} and asked[0][2] == mc_agent.USER_AGENT
    assert look("FELIX_2011")["cached"] is True and len(asked) == 2
    answers["api.mojang.com"] = (204, b"")
    assert look("NoSuchPlayer") == {"found": False, "via": "api.mojang.com"}
    # neither answers: unverified, not cached
    answers.update({"api.mojang.com": TimeoutError("timed out"), "api.minecraftservices.com": (503, b"")})
    out = look("brokenmojang")
    assert out == {
        "found": None,
        "unverified": True,
        "detail": "api.mojang.com: TimeoutError; api.minecraftservices.com: HTTP 503",
    }
    look("brokenmojang")
    assert sum("brokenmojang" in a[0] for a in asked) == 4
    with pytest.raises(mc_agent.Rejected, match="Java name"):
        look("../../etc")
    # the rate limit (cache hits don't count)
    answers.update({"api.mojang.com": (204, b"")})
    for i in range(mc_agent.LOOKUP_PER_MINUTE - 4):
        look(f"name_{i:03d}")
    with pytest.raises(mc_agent.Rejected, match="rate limit"):
        look("one_more")
    look("FELIX_2011")  # cached: fine
    now[0] += 61
    assert look("one_more")["found"] is False


def test_lookup_urls_are_https_or_loopback() -> None:
    assert mc_agent.lookup_urls({}) == mc_agent.MOJANG_LOOKUPS
    assert mc_agent.lookup_urls({"MOJANG_LOOKUP_URLS": "http://127.0.0.1:8788/a/, http://127.0.0.1:8788/b/"}) == (
        "http://127.0.0.1:8788/a/", "http://127.0.0.1:8788/b/")  # fmt: skip
    with pytest.raises(ValueError):
        mc_agent.lookup_urls({"MOJANG_LOOKUP_URLS": "http://mojang.example/"})


def test_selftest_and_lookup_actions(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(mc_agent, "audit", lambda *a, **k: None)
    sent: list[str] = []
    monkeypatch.setattr(notify, "discord", sent.append)
    agent = mc_agent.Agent(mc_agent.Settings("https://hb.example.invalid", TOKEN, KEY, state_dir=tmp_path))
    assert agent.github is None  # no CONFIG_REPO in the test environment
    ex = agent.executor
    out = ex.run({"action": "selftest", "args": {}, "login": "a@x", "id": "1"})
    assert out == "failed at configuration: no config repo for the panel (CONFIG_REPO) or no token source"
    assert agent.panel_state()["selftest"]["configured"] is False and not sent
    # with a config repo: each step through mc_panel.selftest; at most once a minute from the panel
    runs: list[str] = []

    def fake(fetch: Any, gh: Any, now: float) -> dict[str, Any]:
        runs.append("run")
        return {"ok": True, "ts": int(now * 1000), "configured": True, "error": "",
                "steps": [{"step": s, "ok": True, "detail": "ok"} for s in mc_panel.SELFTEST_STEPS]}  # fmt: skip

    monkeypatch.setattr(mc_panel, "selftest", fake)
    agent.github = object()  # type: ignore[assignment]
    (tmp_path / "selftest.json").unlink()
    assert ex.run({"action": "selftest", "args": {}, "login": "a@x", "id": "2"}) == (
        "passed: token, permissions, repository, main, pulls"
    )
    assert ex.run({"action": "selftest", "args": {}, "login": "a@x", "id": "3"}).startswith(
        "passed (an earlier run"
    )
    assert runs == ["run"]
    assert agent.run_selftest("daily")["ok"] and runs == ["run", "run"]  # the daily run isn't limited
    assert agent.panel_state()["selftest"]["trigger"] == "daily"
    # lookup: the panel's name check through the box
    agent.panel.lookup = lambda name: {"found": False, "via": "api.mojang.com", "asked": name}
    assert json.loads(ex.run({"action": "lookup", "args": {"name": "Steve"}, "login": "a@x", "id": "4"})) == {
        "found": False, "via": "api.mojang.com", "asked": "Steve"}  # fmt: skip
