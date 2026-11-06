"""`mcctl metrics`: player-experience history from Cloudflare (design §8 P13), no SSH, no server round trip.

    mcctl metrics [-C CONFIG] [--days 7]
        summary: worst hours, verdict minutes, overhead, tick p99 before/after each deploy/restart
    mcctl metrics export [-C CONFIG] --since 2026-10-01 [--until ...]
                         [--table minute|hour|players|sessions|events]
        CSV on stdout (or -o FILE)

Reads the Worker's TELEMETRY D1 database (`<name>-telemetry`) through the Cloudflare REST API
(`POST /accounts/{account}/d1/database/{id}/query`, D1 Read) with the admin token from the config repo's .env
(CLOUDFLARE_API_TOKEN, never printed). Older than D1 keeps: the R2 archive
`telemetry/YYYY/MM/DD/*.ndjson.gz` in the logs bucket (DuckDB:
`read_ndjson('s3://<name>-mc-logs/telemetry/*/*/*/*.ndjson.gz')`). The data holds no IP addresses
(SECURITY.md "Player-experience telemetry").
"""

# ruff: noqa: S608 - every SQL text here is built from constants and int() values only (no user text)
from __future__ import annotations

import csv
import datetime
import io
import json
import re
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from mcctl import http, ui
from mcctl.providers.cloudflare import API, CloudflareError

TABLES = {
    "minute": "ts, players, tps, mean, p50, p95, p99, p999, max, spikes, chunks, entities, ping_p50, "
    "ping_max, poor, fair, self_p99_us, heap_pct, verdict",
    "hour": "ts, minutes, players_avg, players_max, tps_min, mean, p50, p99, p999, max, spikes, "
    "entities_max, ping_avg, poor_minutes, fair_minutes, self_p99_us",
    "players": "uuid, name, first_seen, last_seen, verdict, headline",
    "sessions": "uuid, name, join_ts, leave_ts",
    "events": "ts, kind, uuid, detail",
}
PAGE = 5000
# Every SQL text below is built from constants and int() values only (no user text): hence the S608 waivers.


@dataclass
class D1:
    token: str
    account: str
    database: str
    transport: http.Transport

    def query(self, sql: str) -> list[dict[str, Any]]:
        url = f"{API}/accounts/{self.account}/d1/database/{self.database}/query"
        resp = http.send_json(
            self.transport, "POST", url, {"sql": sql}, {"Authorization": f"Bearer {self.token}"}
        )
        body = resp.json() or {}
        if resp.status != 200 or not body.get("success"):
            errs = "; ".join(
                str(e.get("message", e)) for e in body.get("errors") or [] if isinstance(e, dict)
            )
            raise CloudflareError(f"D1 query failed (HTTP {resp.status}{': ' + errs if errs else ''})")
        result = body.get("result") or [{}]
        return list(result[0].get("results") or [])


def connect(token: str, zone: str, name: str, transport: http.Transport) -> D1:
    """The deployment's TELEMETRY database: the account from the zone, the database by name."""
    headers = {"Authorization": f"Bearer {token}"}
    resp = http.get(transport, f"{API}/zones?{urllib.parse.urlencode({'name': zone})}", headers)
    zones = (resp.json() or {}).get("result") if resp.status == 200 else None
    if not zones:
        raise CloudflareError(f"zone {zone} not found with this token (HTTP {resp.status})")
    account = zones[0]["account"]["id"]
    db_name = f"{name}-telemetry"
    resp = http.get(
        transport,
        f"{API}/accounts/{account}/d1/database?{urllib.parse.urlencode({'name': db_name})}",
        headers,
    )
    dbs = [d for d in ((resp.json() or {}).get("result") or []) if d.get("name") == db_name]
    if resp.status != 200 or not dbs:
        raise CloudflareError(
            f"D1 database {db_name} not found (needs experience.enabled, panel.mode cloudflare, apply)"
        )
    return D1(token, account, dbs[0]["uuid"], transport)


def parse_day(text: str) -> int:
    """YYYY-MM-DD (UTC) or an ISO timestamp -> epoch ms."""
    try:
        d = datetime.datetime.fromisoformat(text)
    except ValueError:
        raise ValueError(f"not a date: {text!r} (YYYY-MM-DD)") from None
    if d.tzinfo is None:
        d = d.replace(tzinfo=datetime.UTC)
    return int(d.timestamp() * 1000)


def iso(ms: object) -> str:
    if not isinstance(ms, (int, float)):
        return ""
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def minute_tables(db: D1, since: int, until: int) -> list[str]:
    names = [
        r["name"]
        for r in db.query("SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'exp_1m_%'")
    ]
    lo = iso(since)[:7].replace("-", "")
    hi = iso(until)[:7].replace("-", "")
    return sorted(n for n in names if re.fullmatch(r"exp_1m_\d{6}", n) and lo <= n[7:] <= hi)


def rows(db: D1, table: str, since: int, until: int) -> Iterator[dict[str, Any]]:
    """Every row of one export table in [since, until), paged by time (keyset: no OFFSET scans)."""
    cols = TABLES[table]
    s, u = int(since), int(until)
    if table in ("minute", "hour", "events"):
        sources = (
            minute_tables(db, s, u) if table == "minute" else ["exp_1h" if table == "hour" else "exp_events"]
        )
        for src in sources:
            cursor = s - 1
            while True:
                page = db.query(
                    f"SELECT {cols} FROM {src} WHERE ts > {cursor} AND ts < {u} ORDER BY ts LIMIT {PAGE}"
                )
                yield from page
                if len(page) < PAGE:
                    break
                cursor = int(page[-1]["ts"])
    elif table == "sessions":
        yield from db.query(
            f"SELECT {cols} FROM exp_sessions WHERE join_ts >= {s} AND join_ts < {u} ORDER BY join_ts"
        )
    else:
        yield from db.query(f"SELECT {cols} FROM exp_players ORDER BY name")


def export_csv(db: D1, table: str, since: int, until: int) -> str:
    out = io.StringIO()
    cols = [c.strip() for c in TABLES[table].split(",")]
    time_cols = {"ts", "first_seen", "last_seen", "join_ts", "leave_ts"}
    w = csv.writer(out, lineterminator="\n")
    w.writerow([*cols, *(f"{c}_utc" for c in cols if c in time_cols)])
    for r in rows(db, table, since, until):
        w.writerow([*(r.get(c) for c in cols), *(iso(r.get(c)) for c in cols if c in time_cols)])
    return out.getvalue()


@dataclass
class Summary:
    """`mcctl metrics` summary data: rendered as text (summary) or tables (show_summary)."""

    days: int
    facts: list[str] = field(default_factory=list)
    worst: list[list[str]] = field(default_factory=list)  # hour, p99, p99.9, max, players
    self_cost: str | None = None
    cpu: str | None = None
    events: list[list[str]] = field(default_factory=list)  # when, kind, text, before, after

    @property
    def title(self) -> str:
        return f"Player experience, last {self.days} days (hourly rollups from D1)"


def summarize(db: D1, now_ms: int, days: int = 7) -> Summary:
    since = now_ms - days * 86400000
    hours = db.query(
        "SELECT ts, minutes, players_avg, players_max, tps_min, p99, p999, max, spikes, poor_minutes, "
        f"fair_minutes, self_p99_us, ovh FROM exp_1h WHERE ts >= {int(since)} ORDER BY ts"
    )
    s = Summary(days)
    if not hours:
        return s
    minutes = sum(h["minutes"] or 0 for h in hours)
    played = [h for h in hours if (h["players_avg"] or 0) > 0]
    s.facts.append(
        f"{minutes} minutes recorded ({100 * minutes / (days * 1440):.0f}% coverage), players online in "
        f"{len(played)} hours, peak {max((h['players_max'] or 0) for h in hours)}"
    )
    s.facts.append(
        f"minutes with a poor experience: {sum(h['poor_minutes'] or 0 for h in hours)}, fair: "
        f"{sum(h['fair_minutes'] or 0 for h in hours)}; lag spikes: {sum(h['spikes'] or 0 for h in hours)}"
    )
    worst = sorted((h for h in hours if h["p99"] is not None), key=lambda h: h["p99"], reverse=True)[:5]
    s.worst = [
        [
            f"{iso(h['ts'])[:13]}:00Z",
            f"{h['p99']:.0f}",
            f"{h['p999'] or 0:.0f}",
            f"{h['max'] or 0:.0f}",
            f"{h['players_avg'] or 0:.1f}",
        ]
        for h in worst
    ]
    selfs = [h["self_p99_us"] for h in hours if h["self_p99_us"] is not None]
    if selfs:
        s.self_cost = f"p99 per tick up to {max(selfs):.1f} us"
    ovh: dict[str, list[float]] = {}
    for h in hours:
        for unit, o in (json.loads(h["ovh"]) if h.get("ovh") else {}).items():
            if isinstance(o, dict) and isinstance(o.get("cpu_pct"), (int, float)):
                ovh.setdefault(unit, []).append(o["cpu_pct"])
    if ovh:
        s.cpu = ", ".join(
            f"{u.removesuffix('.service')} {sum(v) / len(v):.2f}%" for u, v in sorted(ovh.items())
        )
    notes = db.query(
        "SELECT ts, uuid AS kind, detail FROM exp_events WHERE kind = 'note' "
        f"AND ts >= {int(since)} ORDER BY ts"
    )
    for n in notes[-10:]:
        before = [h["p99"] for h in hours if n["ts"] - 86400000 <= h["ts"] < n["ts"] and h["p99"] is not None]
        after = [h["p99"] for h in hours if n["ts"] < h["ts"] <= n["ts"] + 86400000 and h["p99"] is not None]
        text = (json.loads(n["detail"]) if n.get("detail") else {}).get("text", "")
        b = f"{sum(before) / len(before):.0f}" if before else "-"
        a = f"{sum(after) / len(after):.0f}" if after else "-"
        s.events.append([f"{iso(n['ts'])[:16]}Z", str(n["kind"]), str(text), b, a])
    return s


def summary(db: D1, now_ms: int, days: int = 7) -> str:
    return format_summary(summarize(db, now_ms, days))


def format_summary(s: Summary) -> str:
    lines = [s.title]
    if not s.facts:
        return "\n".join([*lines, "  no data yet (the Worker rolls up each complete hour)"])
    lines += [f"  {f}" for f in s.facts]
    lines.append("  worst hours (tick p99 / p99.9 / max, players):")
    lines += [f"    {w[0]}  {w[1]} / {w[2]} / {w[3]} ms, {w[4]} players" for w in s.worst]
    if s.self_cost:
        lines.append(f"  mc-telemetry main-thread cost: {s.self_cost}")
    if s.cpu:
        lines.append(f"  CPU by unit (average): {s.cpu}")
    if s.events:
        lines.append("  tick p99 24 h before -> after each event:")
        lines += [f"    {e[0]} {e[1]} ({e[2]}): {e[3]} -> {e[4]} ms" for e in s.events]
    return "\n".join(lines)


def show_summary(s: Summary, out: ui.Out) -> None:
    """The summary: plain, format_summary's text; rich, facts and tables."""
    if not out.rich:
        out.say(format_summary(s))
        return
    out.section(s.title)
    if not s.facts:
        out.note("no data yet (the Worker rolls up each complete hour)")
        return
    for f in s.facts:
        out.note(f)
    out.table(
        ["Hour", "tick p99 ms", "p99.9 ms", "max ms", "players"],
        s.worst,
        title="Worst hours",
        styles=("bold",),
        indent=2,
    )
    rows = []
    if s.self_cost:
        rows.append(("mc-telemetry main-thread cost", s.self_cost))
    if s.cpu:
        rows.append(("CPU by unit (average)", s.cpu))
    if rows:
        out.kv(rows)
    if s.events:
        out.table(
            ["When", "Kind", "Event", "p99 before ms", "after ms"],
            s.events,
            title="Tick p99 24 h before -> after each event",
            indent=2,
        )
