"""Differential test of the console allowlist: the Rust agent's `check_console`/`normalise_console` against
mc_agent.py's on generated input (Hypothesis).

`smallworld-agent selftest-console` is a hidden, test-only subcommand (rust/smallworld-agent/PORTING.md
"Divergences"): it reads a JSON array of inputs on stdin and prints `{"normalise": outcome, "check": outcome}`
for each, the shape of tests/agent_parity/vectors/console.json. It runs the two pure functions and nothing else
(no RCON, no file, no audit). The binary is rust/dist's (or SMALLWORLD_AGENT_BIN, e.g. a `cargo build`); a
binary built before the subcommand existed is skipped.

Characters are drawn from what this interpreter's Unicode tables assign (category != Cn, no surrogates):
parity is defined over assigned code points (PORTING.md "Unicode"); the Rust crates' tables can be newer.
"""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import mc_agent
import pytest
from conftest import REPO

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

TARGETS = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}


def _binary() -> Path | None:
    if os.environ.get("SMALLWORLD_AGENT_BIN"):
        return Path(os.environ["SMALLWORLD_AGENT_BIN"])
    if sys.platform != "linux" or platform.machine() not in TARGETS:
        return None
    return REPO / "rust" / "dist" / f"smallworld-agent-{TARGETS[platform.machine()]}"


def run_rust(binary: Path, inputs: list[Any]) -> list[dict[str, Any]] | None:
    """The Rust outcomes, or None if this binary has no selftest-console (built before it existed)."""
    env = {"PATH": "/nonexistent"}  # nothing to find: the subcommand runs no program
    r = subprocess.run([str(binary), "selftest-console"], input=json.dumps(inputs), capture_output=True,
                       text=True, timeout=60, env=env, check=False)  # fmt: skip
    if r.returncode != 0 or not r.stdout.startswith("["):
        return None
    return json.loads(r.stdout)


def outcome(fn: Any) -> dict[str, Any]:
    try:
        return {"ok": fn()}
    except mc_agent.Rejected as e:
        return {"error": str(e), "type": "Rejected"}


def python(item: Any) -> dict[str, Any]:
    return {
        "normalise": outcome(lambda: mc_agent.normalise_console(item)),
        "check": outcome(lambda: mc_agent.check_console(item)),
    }


BINARY = _binary()
pytestmark = pytest.mark.skipif(
    BINARY is None or not BINARY.is_file(), reason="no smallworld-agent binary (or set SMALLWORLD_AGENT_BIN)"
)


@pytest.fixture(scope="module")
def binary() -> Path:
    assert BINARY is not None
    if run_rust(BINARY, []) is None:
        pytest.skip(f"{BINARY} has no selftest-console (rebuild it: python scripts/build_rust.py build)")
    return BINARY


ASSIGNED = st.characters(exclude_categories=("Cs", "Cn"))
# The allowlist's alphabet: heads, digits of several scripts, fullwidth forms, selectors, separators, the
# whitespace/control edge cases (U+001C..U+001F, U+0085, U+2028), combining marks and NFKC expanders.
EDGES = st.sampled_from(
    list(" /:@[].-_0123456789abcdefgABCDEFGH\t\n\x1c\x1d\x1e\x1f\x85\u00a0\u2028\u3000\u200b\u0301\ufdfa")
    + list("\u0660\u0663\u06f5\u0967\u0e53\uff10\uff13\uff21\uff41\uff54\U0001d7ce\u00b2\u2460\u2163")
    + ["\u0391", "\u03a3", "\u03c3", "\u0130", "\u212a", "\ufb00"]
)
HEADS = st.sampled_from(sorted({*mc_agent.CONSOLE_ALLOWLIST, *mc_agent.REFUSED_HEADS, "minecraft:op", "x:y"}))
WORDS = st.one_of(
    HEADS,
    st.text(st.one_of(EDGES, ASSIGNED), max_size=12),
    st.sampled_from(["@a", "@e[type=x]", "@p", "@s", "203.0.113.7", "2001:db8::1", "100", "3d", "uuids"]),
)
LINES = st.one_of(
    st.lists(WORDS, min_size=0, max_size=6).map(" ".join),
    st.tuples(HEADS, st.lists(WORDS, max_size=4)).map(lambda t: " ".join([t[0], *t[1]])),
    st.text(st.one_of(EDGES, ASSIGNED), max_size=300),
)


def compare(binary: Path, lines: list[str]) -> None:
    got = run_rust(binary, lines)
    assert got is not None
    for line, rust in zip(lines, got, strict=True):
        assert rust == python(line), f"{line!r}: rust {rust} != python {python(line)}"


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(st.lists(LINES, min_size=1, max_size=50))
def test_console_allowlist_matches_python(binary: Path, lines: list[str]) -> None:
    compare(binary, lines)


def test_console_corpus_and_odd_values(binary: Path) -> None:
    import parity_vectors

    lines: list[Any] = [*parity_vectors.CONSOLE_CORPUS, *parity_vectors.CONSOLE_EXTRA, None, 5, ["list"], {}]
    got = run_rust(binary, lines)
    assert got is not None
    for item, rust in zip(lines, got, strict=True):
        assert rust == python(item), f"{item!r}"
