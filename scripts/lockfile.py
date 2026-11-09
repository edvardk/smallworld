"""Shared helpers for the lock files (server.lock.yml, plugins.lock.yml). Controller-side only."""

from __future__ import annotations

import email.utils
import hashlib
import http.client
import json
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# The product's reference locks: what new config repos start with (template/), and what the product's
# config keys (server-config/keys/) and tests are checked against. A config repo has its own copies.
LOCK_DIR = REPO_ROOT / "template"
SERVER_LOCK = LOCK_DIR / "server.lock.yml"
PLUGINS_LOCK = LOCK_DIR / "plugins.lock.yml"
USER_AGENT = "edvardk/smallworld (lock updater)"


# v1.5.1: GitHub's release downloads answered HTTP 504 during the v1.5.0 release (the pinned JDK, gitleaks),
# so a transient failure is retried: 5xx, 429, a URLError (DNS, refused, reset), a timeout or a cut-off body,
# ATTEMPTS times with exponential backoff (2, 4, 8, 16 s) or the server's Retry-After (capped). A 404 or any
# other 4xx fails at once. Callers still check the hashes (download_verified).
ATTEMPTS = 5
BACKOFF = 2.0  # seconds before the 2nd attempt, doubled after each failure
MAX_RETRY_AFTER = 120.0
Opener = Callable[..., Any]  # urllib.request.urlopen's shape: (request, timeout=...) -> a context manager


def _retry_after(err: urllib.error.HTTPError) -> float | None:
    """Retry-After in seconds (delta-seconds or an HTTP-date), None if absent or unreadable."""
    value = (err.headers.get("Retry-After") if err.headers else None) or ""
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())


_NETWORK_ERRORS = (urllib.error.URLError, TimeoutError, ConnectionError, http.client.IncompleteRead)


def _transient(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    return isinstance(exc, _NETWORK_ERRORS)


def http_get(
    url: str,
    timeout: float = 60,
    *,
    opener: Opener | None = None,
    sleep: Callable[[float], None] | None = None,
    attempts: int = ATTEMPTS,
) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - https enforced above
    open_url = opener or urllib.request.urlopen
    pause = sleep or time.sleep
    delay = BACKOFF
    for attempt in range(1, attempts + 1):
        try:
            with open_url(req, timeout=timeout) as resp:
                return resp.read()
        except _NETWORK_ERRORS as exc:
            if attempt == attempts or not _transient(exc):
                raise
            wait = delay
            if isinstance(exc, urllib.error.HTTPError) and (after := _retry_after(exc)) is not None:
                wait = min(after, MAX_RETRY_AFTER)
            reason = f"HTTP {exc.code}" if isinstance(exc, urllib.error.HTTPError) else type(exc).__name__
            print(f"{url}: {reason}, retry {attempt}/{attempts - 1} in {wait:g}s", file=sys.stderr)
            pause(wait)
            delay *= 2
    raise AssertionError("unreachable")  # pragma: no cover


def get_json(url: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads(http_get(url))


def digest(data: bytes, algo: str) -> str:
    return hashlib.new(algo, data).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def dump_yaml(path: Path, data: dict[str, Any], header: str) -> None:
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    path.write_text(f"{header.rstrip()}\n{body}")


def download_verified(url: str, expected: dict[str, str]) -> bytes:
    """Download and check every given {algo: hexdigest}. Raises on any mismatch."""
    data = http_get(url, timeout=300)
    for algo, want in expected.items():
        got = digest(data, algo)
        if got != want:
            raise ValueError(f"{algo} mismatch for {url}: expected {want}, got {got}")
    return data
