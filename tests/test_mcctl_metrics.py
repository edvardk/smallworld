"""P13 `mcctl metrics`: summary and CSV export from the Worker's TELEMETRY D1 database over the Cloudflare REST
API. Recorded zone/database lookups (fixtures/http/cloudflare_d1_telemetry.json); the query endpoint is answered
from a real SQLite database with the Worker's schema, so the SQL mcctl sends must be valid SQLite (= D1)."""

# ruff: noqa: S608 - test SQL built from constants
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import time
from pathlib import Path

import pytest
from conftest import Replay, example_server, make_config_repo

from mcctl import cli, http, metrics
from mcctl.providers.cloudflare import CloudflareError

QUERY = "https://api.cloudflare.com/client/v4/accounts/f037e56e89293a057740de681ac9abbe/d1/database/7a9e8d1c-0000-4000-8000-00000000d1d1/query"
NOW = 1_793_500_000_000 // 3600000 * 3600000
ED = "3f2a0000-0000-4000-8000-000000000c41"


def seeded() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(
        "CREATE TABLE exp_1h (ts INTEGER PRIMARY KEY, minutes INTEGER, players_avg REAL, players_max INTEGER, tps_min REAL, mean REAL, p50 REAL, p99 REAL, p999 REAL, max REAL, spikes INTEGER, entities_max INTEGER, ping_avg REAL, poor_minutes INTEGER, fair_minutes INTEGER, self_p99_us REAL, ovh TEXT);"
        "CREATE TABLE exp_events (ts INTEGER NOT NULL, kind TEXT NOT NULL, uuid TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL, PRIMARY KEY (ts, kind, uuid)) WITHOUT ROWID;"
        "CREATE TABLE exp_players (uuid TEXT PRIMARY KEY, name TEXT NOT NULL, first_seen INTEGER, last_seen INTEGER, stats TEXT, verdict TEXT, headline TEXT, reasons TEXT, since INTEGER, last_msg INTEGER, last_poor TEXT, brand TEXT) WITHOUT ROWID;"
        "CREATE TABLE exp_sessions (uuid TEXT NOT NULL, join_ts INTEGER NOT NULL, leave_ts INTEGER, name TEXT, worst TEXT, PRIMARY KEY (uuid, join_ts)) WITHOUT ROWID;"
    )  # fmt: skip
    deploy = NOW - 30 * 3600000
    for i in range(72):
        ts = NOW - (72 - i) * 3600000
        p99 = 60.0 if ts < deploy else 25.0
        ovh = json.dumps({"minecraft.service": {"cpu_pct": 55.0}, "mc-agent.service": {"cpu_pct": 0.3}})
        db.execute(
            "INSERT INTO exp_1h VALUES (?, 60, 2.5, 4, 19.8, 12, 10, ?, ?, ?, 1, 900, 45, ?, 3, 8.5, ?)",
            (ts, p99 + (i == 10) * 200, p99 + 20, p99 + 40, 5 if ts < deploy else 0, ovh),
        )
    db.execute(
        "INSERT INTO exp_events VALUES (?, 'note', 'maintenance', ?)",
        (deploy, json.dumps({"text": "deploy (cfg@abc)"})),
    )
    db.execute(
        "INSERT INTO exp_players VALUES (?, 'Em_K', ?, ?, NULL, 'good', 'Smooth', '[]', NULL, NULL, NULL, 'vanilla')",
        (ED, NOW - 86400000, NOW),
    )
    db.execute("INSERT INTO exp_sessions VALUES (?, ?, ?, 'Em_K', NULL)", (ED, NOW - 7200000, NOW - 3600000))
    for month, base in (("202610", NOW - 40 * 86400000), ("202611", NOW - 3600000)):
        db.execute(f"CREATE TABLE exp_1m_{month} (ts INTEGER PRIMARY KEY, players INTEGER, tps REAL, mean REAL, p50 REAL, p95 REAL, p99 REAL, p999 REAL, max REAL, spikes INTEGER, chunks INTEGER, entities INTEGER, ping_p50 REAL, ping_max REAL, poor INTEGER, fair INTEGER, self_p99_us REAL, heap_pct REAL, verdict TEXT, ovh TEXT, backlog TEXT)")  # fmt: skip
        for i in range(30):
            db.execute(
                f"INSERT INTO exp_1m_{month} (ts, players, p99, verdict) VALUES (?, 2, 20, 'good')",
                (base // 60000 * 60000 + i * 60000,),
            )
    return db


class D1Fake:
    """Replay for the lookups + the query endpoint answered by SQLite (and a record of the SQL)."""

    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db
        self.replay = Replay("cloudflare_d1_telemetry.json")
        self.sql: list[str] = []
        self.requests: list[http.Request] = []

    def __call__(self, req: http.Request) -> http.Response:
        self.requests.append(req)
        if req.method == "POST" and req.url == QUERY:
            sql = json.loads(req.body or b"{}")["sql"]
            self.sql.append(sql)
            if not sql.lstrip().upper().startswith("SELECT"):
                raise AssertionError(f"mcctl metrics must only read: {sql}")
            results = [dict(r) for r in self.db.execute(sql).fetchall()]
            body = {
                "success": True,
                "errors": [],
                "messages": [],
                "result": [{"results": results, "success": True, "meta": {}}],
            }
            return http.Response(200, {"content-type": "application/json"}, json.dumps(body).encode())
        return self.replay(req)


@pytest.fixture
def fake() -> D1Fake:
    return D1Fake(seeded())


def test_summary_worst_hours_coverage_overhead_and_before_after(fake: D1Fake) -> None:
    db = metrics.connect("tok", "example.org", "lh", fake)
    text = metrics.summary(db, NOW, days=3)
    assert "4320 minutes recorded (100% coverage)" in text
    assert "minutes with a poor experience: 210" in text
    first = text.split("worst hours")[1].splitlines()[1]
    assert first.strip().startswith(metrics.iso(NOW - 62 * 3600000)[:13]) and "260 /" in first
    assert "mc-agent 0.30%" in text and "minecraft 55.00%" in text
    assert "maintenance (deploy (cfg@abc)): 60 -> 25 ms" in text
    # the token only ever goes to Cloudflare's API, in the Authorization header
    assert all(
        r.url.startswith("https://api.cloudflare.com/") and r.headers["Authorization"] == "Bearer tok"
        for r in fake.requests
    )


def test_export_minute_rows_span_month_tables_and_page(fake: D1Fake, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(metrics, "PAGE", 7)
    db = metrics.connect("tok", "example.org", "lh", fake)
    out = metrics.export_csv(db, "minute", NOW - 60 * 86400000, NOW + 1)
    rows = list(csv.DictReader(io.StringIO(out)))
    assert len(rows) == 60
    assert rows[0]["ts_utc"].endswith("Z") and int(rows[0]["ts"]) < int(rows[-1]["ts"])
    assert any("exp_1m_202610" in s for s in fake.sql) and any("exp_1m_202611" in s for s in fake.sql)
    only_nov = list(csv.DictReader(io.StringIO(metrics.export_csv(db, "minute", NOW - 2 * 3600000, NOW + 1))))
    assert len(only_nov) == 30
    for table in ("hour", "players", "sessions", "events"):
        text = metrics.export_csv(db, table, NOW - 30 * 86400000, NOW + 1)
        assert len(text.splitlines()) > 1, table
    assert "203.0.113" not in out  # no IPs (there are none to export)


@pytest.mark.posix_only  # a 0600 .env
def test_cli_metrics_export_and_errors(
    tmp_path: Path, fake: D1Fake, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(time, "time", lambda: NOW / 1000)  # the fake's data is around NOW, not today
    cfg = make_config_repo(
        tmp_path / "cfg", example_server() | {"name": "lh", "dns": {"zone": "example.org", "hostname": "mc"}}
    )
    (cfg / ".env").write_text("CLOUDFLARE_API_TOKEN=tok\n")
    (cfg / ".env").chmod(0o600)
    ns = argparse.Namespace(config_dir=cfg, i_know=False, action="export", since="2026-01-01", until="", table="hour",
                            output=str(tmp_path / "h.csv"), days=7, format="csv")  # fmt: skip
    assert cli.cmd_metrics(ns, transport=fake) == 0
    assert (tmp_path / "h.csv").read_text().startswith("ts,minutes,players_avg")
    assert "tok" not in capsys.readouterr().err
    ns.since = ""
    assert cli.cmd_metrics(ns, transport=fake) == 1
    assert "needs --since" in capsys.readouterr().err
    ns.action, ns.since = "summary", "x"
    assert cli.cmd_metrics(ns, transport=fake) == 0
    assert "worst hours" in capsys.readouterr().out


def test_missing_database_and_query_errors() -> None:
    def no_db(req: http.Request) -> http.Response:
        if "/d1/database?" in req.url:
            return http.Response(200, {}, json.dumps({"success": True, "result": []}).encode())
        return Replay("cloudflare_d1_telemetry.json")(req)

    with pytest.raises(CloudflareError, match="lh-telemetry not found"):
        metrics.connect("tok", "example.org", "lh", no_db)

    def failing(req: http.Request) -> http.Response:
        return http.Response(
            400, {}, json.dumps({"success": False, "errors": [{"message": "no such table"}]}).encode()
        )

    with pytest.raises(CloudflareError, match="no such table"):
        metrics.D1("t", "a", "d", failing).query("SELECT 1")
    with pytest.raises(ValueError, match="not a date"):
        metrics.parse_day("yesterday")
