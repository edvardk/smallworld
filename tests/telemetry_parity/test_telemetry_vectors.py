"""The telemetry test vectors (tests/telemetry_parity/vectors/*.json) are what this checkout's scripts/telemetry.py
computes: `python scripts/telemetry_vectors.py` regenerates them after a change to it (docs/ci.md "The telemetry
parity harness")."""

from __future__ import annotations

import json
import sys

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only (the box's scripts)")


def test_telemetry_vectors_are_current() -> None:
    import telemetry_vectors

    files = telemetry_vectors.build()
    assert not telemetry_vectors.stale(files), "run python scripts/telemetry_vectors.py"


def _strings(v: object) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for k, x in v.items() for s in (*_strings(k), *_strings(x))]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def test_telemetry_vectors_are_plain_json() -> None:
    """What a Rust test reads with serde_json: ASCII text, no NaN/Infinity, no lone surrogates."""
    import telemetry_vectors

    for path in sorted(telemetry_vectors.OUT.glob("*.json")):
        text = path.read_text()
        assert text.isascii(), path.name
        data = json.loads(text, parse_constant=lambda c, n=path.name: pytest.fail(f"{n}: {c} isn't JSON"))
        lone = [s for s in _strings(data) if any(0xD800 <= ord(ch) <= 0xDFFF for ch in s)]
        assert not lone, f"{path.name}: lone surrogates in {lone[:3]}"
