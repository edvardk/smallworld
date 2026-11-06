"""The watchdog Worker's freshness (design §4 `doctor`, init step 12; Worker: watchdog/worker.js).

`GET https://hb.<hostname>.<zone>/status` (no credentials) -> `{ok, last_run, last_run_age_s, maintenance,
client, checks: {health|backup: {state, ...}}, probes: {v4|v6: {ok, error, ...}}}`. Freshness only: the Worker
never puts addresses or messages there (v1.2 `client`: a boolean, whether the client page's bindings exist).
"""

from __future__ import annotations

from typing import Any, Literal

from mcctl import http

MAX_RUN_AGE_S = 15 * 60  # the cron runs every 5 min (design §6 expiry-check threshold)


def status(url: str, transport: http.Transport) -> dict[str, Any] | None:
    resp = http.get(transport, f"{url.rstrip('/')}/status")
    data = resp.json()
    return data if resp.status == 200 and isinstance(data, dict) else None


def assess(data: dict[str, Any] | None) -> tuple[Literal["ok", "warn", "fail"], str]:
    """(level, one-line summary) of a /status document."""
    if data is None:
        return "fail", "/status didn't answer with JSON (is the Worker deployed? `mcctl apply`)"
    age = data.get("last_run_age_s")
    if not isinstance(age, int | float):
        return "fail", "the cron trigger has never run"
    if age > MAX_RUN_AGE_S:
        limit = MAX_RUN_AGE_S // 60
        return "fail", f"last cron run {int(age) // 60} min ago (> {limit}): the cron isn't running"
    problems = []
    for name, c in sorted((data.get("checks") or {}).items()):
        if isinstance(c, dict) and c.get("state") in ("late", "failing"):
            problems.append(f"{name} {c['state']}")
    for family, p in sorted((data.get("probes") or {}).items()):
        if isinstance(p, dict) and p.get("ok") is False:
            problems.append(f"{family} probe failing ({p.get('error') or 'no reply'})")
    maint = data.get("maintenance")
    note = f"; maintenance until {maint.get('until')}" if isinstance(maint, dict) else ""
    if problems:
        return "warn", f"cron ran {int(age)} s ago; " + ", ".join(problems) + note
    return "ok", f"cron ran {int(age)} s ago; checks and probes fine" + note


def assess_client(data: dict[str, Any]) -> tuple[Literal["ok", "warn", "fail"], str]:
    """(level, one-line summary) of /status's `client` flag (v1.2), for a deployment with client.enabled: the
    Worker's CLIENT and CLIENT_KEY bindings come only with `mcctl apply` (terraform/client.tf), a deploy alone
    leaves /modpack answering "the client page isn't set up on this server yet"."""
    flag = data.get("client")
    if flag is True:
        return "ok", "the Worker has the client set up"
    if flag is False:
        return "fail", "client.enabled, but the Worker has no client set up: /modpack can't hand out codes"
    return "warn", "the Worker predates this check (v1.2), so it can't say whether the client is set up"
