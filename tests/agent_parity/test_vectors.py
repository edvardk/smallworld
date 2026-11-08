"""The parity test vectors (tests/agent_parity/vectors/*.json) are what this checkout's Python agent computes:
`python scripts/parity_vectors.py` regenerates them after a change to the agent (docs/ci.md)."""

from __future__ import annotations

import json
import sys
import unicodedata

import pytest

pytestmark = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux only (the box's scripts)")


def test_vectors_are_current() -> None:
    import parity_vectors

    files = parity_vectors.build()
    have = {p.stem for p in parity_vectors.OUT.glob("*.json")}
    assert have == set(files), f"stale or missing vector files: {sorted(have ^ set(files))}"
    stale = []
    for name, data in files.items():
        current = (parity_vectors.OUT / f"{name}.json").read_text()
        if current == parity_vectors.render(data):
            continue
        old = json.loads(current)["meta"]
        if (
            data["meta"].get("unicode_sensitive")
            and old.get("unidata_version") != unicodedata.unidata_version
        ):
            continue  # generated on another Unicode version (the interpreter's tables): not comparable here
        stale.append(name)
    assert not stale, f"vector files out of date: {stale}: run python scripts/parity_vectors.py"


def _strings(v: object) -> list[str]:
    if isinstance(v, str):
        return [v]
    if isinstance(v, dict):
        return [s for k, x in v.items() for s in (*_strings(k), *_strings(x))]
    if isinstance(v, list):
        return [s for x in v for s in _strings(x)]
    return []


def test_vectors_are_plain_json() -> None:
    """What a Rust test reads with serde_json: ASCII text, no NaN/Infinity, no lone surrogates."""
    import parity_vectors

    for path in sorted(parity_vectors.OUT.glob("*.json")):
        text = path.read_text()
        assert text.isascii(), path.name
        data = json.loads(
            text, parse_constant=lambda c, name=path.name: pytest.fail(f"{name}: {c} isn't JSON")
        )
        lone = [s for s in _strings(data) if any(0xD800 <= ord(ch) <= 0xDFFF for ch in s)]
        assert not lone, f"{path.name}: lone surrogates in {lone[:3]}"
