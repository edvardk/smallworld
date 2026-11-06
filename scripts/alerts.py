#!/usr/bin/env python3
"""One template and one repeat policy for every Discord alert the box sends (health.py, the reboot gate, ...).

Stdlib only (server-side, Python >= 3.11). The Worker renders the same template in watchdog/worker.js
(`alertText`), so an alert reads the same wherever it comes from (docs/operations.md "Alerts"):

    <emoji> **<Headline: what's happening, in player terms>**
    **Impact:** who/what is affected right now
    **Likely cause:** concrete, from the data we have
    **What to do:** 1-3 concrete actions, or "No action needed - informational."
    **Details:** the raw numbers in one compact line

Repeat policy (`Gate`): per alert key, one message on entering the bad state, then at most one reminder per
`repeat_s` while it persists (with how many occurrences were folded into it and whether it got worse), and
one recovered message (with how long it was bad) when it clears. The gate's state is a plain dict the caller
persists (health.py: its state.json in StateDirectory, so it survives restarts).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

SEVERITY = {"info": "\u2139\ufe0f", "warning": "\u26a0\ufe0f", "action": "\U0001f534", "recovered": "\u2705"}
MAX_CHARS = 1900  # Discord's limit is 2000; notify/the Worker add a "[host] " prefix
NO_ACTION = "No action needed - informational."
WORSE = 1.25  # a reminder says "worse" when the value grew by this factor since the last message
IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
IPV6 = re.compile(r"\b(?:[0-9a-fA-F]{1,4}:){2,7}[0-9a-fA-F]{0,4}\b")


@dataclass(frozen=True)
class Alert:
    severity: str  # info | warning | action | recovered
    headline: str
    impact: str = ""
    cause: str = ""
    todo: str = NO_ACTION
    details: str = ""


def scrub(text: str) -> str:
    """No IP addresses in an alert (the box's own or a player's): Discord channels are not private."""
    return IPV6.sub("<address>", IPV4.sub("<address>", text))


def render(a: Alert) -> str:
    lines = [f"{SEVERITY[a.severity]} **{a.headline}**"]
    for label, value in (("Impact", a.impact), ("Likely cause", a.cause), ("What to do", a.todo)):
        if value:
            lines.append(f"**{label}:** {value}")
    if a.details:
        lines.append(f"**Details:** {a.details}")
    text = scrub("\n".join(lines))
    return text if len(text) <= MAX_CHARS else text[: MAX_CHARS - 1] + "…"


def duration(seconds: float) -> str:
    m = max(1, round(seconds / 60))
    if m < 90:
        return f"{m} min"
    h = m / 60
    return f"{h:.0f} h" if h < 48 else f"{h / 24:.0f} days"


@dataclass
class Verdict:
    kind: str  # first | remind | recovered
    suppressed: int = 0  # occurrences since the last message
    worse: bool = False
    bad_for_s: float = 0.0
    worst: float | None = None


@dataclass
class Gate:
    """The repeat policy over a persisted dict: key -> {since, last_sent, last_bad, worst, sent_worst, n}."""

    state: dict[str, dict[str, Any]] = field(default_factory=dict)
    repeat_s: float = 6 * 3600

    def check(
        self, key: str, bad: bool, now: float, value: float | None = None, clear_after_s: float = 0.0
    ) -> Verdict | None:
        a = self.state.get(key)
        if bad:
            if a is None:
                self.state[key] = {
                    "since": now,
                    "last_sent": now,
                    "last_bad": now,
                    "worst": value,
                    "sent_worst": value,
                    "n": 0,
                }
                return Verdict("first", worst=value)
            a["last_bad"] = now
            a["n"] = int(a.get("n", 0)) + 1
            if value is not None:
                a["worst"] = value if a.get("worst") is None else max(a["worst"], value)
            if now - float(a["last_sent"]) < self.repeat_s:
                return None
            sent = a.get("sent_worst")
            worse = value is not None and sent is not None and a["worst"] >= sent * WORSE
            v = Verdict(
                "remind", suppressed=a["n"], worse=worse, bad_for_s=now - a["since"], worst=a["worst"]
            )
            a.update(last_sent=now, sent_worst=a["worst"], n=0)
            return v
        if a is None or now - float(a["last_bad"]) < clear_after_s:
            return None
        del self.state[key]
        return Verdict("recovered", bad_for_s=float(a["last_bad"]) - float(a["since"]), worst=a.get("worst"))


def reminder_note(v: Verdict) -> str:
    """The line a reminder adds to its Details: how long, how often, worse or not."""
    if v.kind != "remind":
        return ""
    trend = "worse than last time" if v.worse else "not worse than last time"
    return (
        f"still happening after {duration(v.bad_for_s)}; {v.suppressed} more occurrence(s) since the last "
        f"message, {trend}"
    )
