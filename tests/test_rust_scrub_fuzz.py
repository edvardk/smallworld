"""Differential fuzzing of the Rust agent's secret scrubber and journal parser against the Python ones (PORTING.md
"ship"). Hypothesis generates random and adversarial log text (secret names, separators, quotes, URL userinfo,
key shapes, Python-only whitespace and case folds) and journal lines; `smallworld-agent selftest-scrub` (a
hidden, test-only subcommand: a pure function of its stdin, it reads no environment, settings or files) must
return exactly what `mc_agent.Scrubber(known)(text)` and `mc_agent.journal_entry(raw)` return.

The binary: SMALLWORLD_AGENT_BIN (e.g. `cargo build`'s target/debug/smallworld-agent), else rust/dist's for this
machine; skipped when it lacks the subcommand. More examples: SCRUB_FUZZ_EXAMPLES=5000; fresh ones each run:
SCRUB_FUZZ_RANDOM=1 (by default the examples are derandomized, the same every run)."""

from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

hypothesis = pytest.importorskip("hypothesis")
from hypothesis import HealthCheck, given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "scripts"))
import mc_agent  # noqa: E402

TARGETS = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}
EXAMPLES = int(os.environ.get("SCRUB_FUZZ_EXAMPLES", "300"))
DERANDOMIZE = "SCRUB_FUZZ_RANDOM" not in os.environ  # set it for fresh examples each run (nightly, by hand)


def _binary() -> Path | None:
    if os.environ.get("SMALLWORLD_AGENT_BIN"):
        path = Path(os.environ["SMALLWORLD_AGENT_BIN"])
    elif sys.platform == "linux" and platform.machine() in TARGETS:
        path = REPO / "rust" / "dist" / f"smallworld-agent-{TARGETS[platform.machine()]}"
    else:
        return None
    try:
        r = subprocess.run([str(path), "selftest-scrub"], input=b"{}", capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return path if r.returncode == 0 and r.stdout.startswith(b"{") else None


BINARY = _binary()
pytestmark = pytest.mark.skipif(
    BINARY is None,
    reason="no smallworld-agent with selftest-scrub (set SMALLWORLD_AGENT_BIN to a cargo build)",
)


def rust(request: dict[str, Any]) -> dict[str, Any]:
    assert BINARY is not None
    r = subprocess.run(
        [str(BINARY), "selftest-scrub"], input=json.dumps(request).encode(), capture_output=True, timeout=60
    )
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    out: dict[str, Any] = json.loads(r.stdout)
    return out


# Pieces of the patterns and of what defeats them: names, separators, quotes, URL parts, key prefixes, Python's
# extra whitespace (U+001C..U+001F) and case folds (U+0130, U+0131, U+017F, U+212A), other Unicode.
FRAGMENTS = [
    "bearer", "Bearer", "BEARER", "basic", "Basic", "ba\u017fic", "BAS\u0130C", "\u0131", "\u212a",
    "password", "passwd", "pass", "pwd", "PWD", "token", "Token", "secret", "api_key", "api-key", "apikey",
    "API", "key", "KEY", "_", "-", ".", "x", "my", "RESTIC", "monkey", "s3cr3t", "=", ":", " = ", ": ",
    '"', "'", ",", ";", "&", "}", "]", "{", "[", " ", "  ", "\t", "\n", "\x0b", "\x1c", "\x1f", "\x85",
    "\xa0", "\u2028", "\u3000", "://", "https://", "ftp://", "u+v.w-x://", "@", "/", "user", "pw",
    "discord.com/api/webhooks/", "discordapp.com/api/webhooks/", "x.discord.com/api/webhooks/", "AKIA", "ASIA",
    "0123456789ABCDEF", "Z", "9", "ghp_", "gho_", "github_pat_", "aaaaaaaaaaaaaaaaaaaaaaaa", "tskey-", "auth-k1",
    "\u00e9", "\u00df", "\U0001f600", "\u0663", "+/=", "dXNlcjpwYXNz",
]  # fmt: skip
SAFE_TEXT = st.text(st.characters(exclude_categories=["Cs"]), max_size=12)
PIECE = st.one_of(st.sampled_from(FRAGMENTS), SAFE_TEXT)
TEXT = st.lists(PIECE, max_size=24).map("".join)
# Known values of distinct lengths: Python orders equal-length values by set iteration (hash seed), Rust sorts.
KNOWN = st.lists(st.one_of(TEXT, st.text(st.characters(exclude_categories=["Cs"]), min_size=8, max_size=20)),
                 max_size=4, unique_by=len)  # fmt: skip


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    derandomize=DERANDOMIZE,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.data_too_large],
)
@given(known=KNOWN, texts=st.lists(TEXT, min_size=1, max_size=20))
def test_the_scrubber_matches_python(known: list[str], texts: list[str]) -> None:
    scrub = mc_agent.Scrubber(known)
    got = rust({"known": known, "texts": texts})["texts"]
    assert got == [scrub(t) for t in texts]


def test_the_scrubber_corpus_matches_python() -> None:
    import parity_vectors

    known = parity_vectors.SCRUB_KNOWN
    texts = [t for t in parity_vectors.SCRUB_INPUTS if isinstance(t, str)]
    scrub = mc_agent.Scrubber(known)
    assert rust({"known": known, "texts": texts})["texts"] == [scrub(t) for t in texts]


def python_entry(raw: dict[str, Any]) -> Any:
    try:
        e = mc_agent.journal_entry(raw)
    except ValueError as exc:
        return {"error": f"{type(exc).__name__}: {exc}"}
    return json.loads(json.dumps(e))


# Journal values: what journalctl emits (strings, byte arrays) and what a hostile sender could put in a field.
# Integers stay within 64 bits and digit strings short (PORTING.md "Divergences": bigger ones differ).
DIGITS = st.text(st.sampled_from("0123456789\u0663\u00b2\u2460_ +-"), max_size=18)
VALUE = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-(2**62), 2**62),
    # Short floats only: serde_json (without its float_roundtrip feature) may read a float of 16-17 significant
    # digits one ulp off, which str() then shows (PORTING.md "Divergences").
    st.integers(-(2**30), 2**30).map(lambda n: n / 4),
    DIGITS,
    TEXT,
    st.lists(st.one_of(st.integers(-2, 300), st.booleans()), max_size=10),
    # str(list) escapes what isn't printable; code points one side's Unicode table doesn't assign are excluded
    # (PORTING.md "Divergences": parity over code points assigned in both).
    st.lists(st.text(st.characters(exclude_categories=["Cs", "Cn"]), max_size=12), max_size=3),
)
FIELDS = [
    "__REALTIME_TIMESTAMP",
    "_SYSTEMD_UNIT",
    "SYSLOG_IDENTIFIER",
    "PRIORITY",
    "MESSAGE",
    "MC_AUDIT",
    "UNIT",
]
RAW = st.dictionaries(st.sampled_from(FIELDS), VALUE, max_size=7)


@settings(
    max_examples=EXAMPLES,
    deadline=None,
    derandomize=DERANDOMIZE,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(raws=st.lists(RAW, min_size=1, max_size=30))
def test_journal_entry_matches_python(raws: list[dict[str, Any]]) -> None:
    got = rust({"journal": raws})["journal"]
    assert got == [python_entry(r) for r in raws]


def test_the_subcommand_only_echoes_scrubbed_input() -> None:
    env = {"PATH": os.environ.get("PATH", ""), "AGENT_TOKEN": "AgentTokenFuzz-0123456789abcdefABCDEF"}
    assert BINARY is not None
    r = subprocess.run(
        [str(BINARY), "selftest-scrub"],
        input=json.dumps({"texts": ["AgentTokenFuzz-0123456789abcdefABCDEF"]}).encode(),
        capture_output=True,
        timeout=10,
        env=env,
    )
    # The environment's secrets aren't known values here: the output is only a function of stdin.
    assert json.loads(r.stdout) == {"texts": ["AgentTokenFuzz-0123456789abcdefABCDEF"], "journal": []}
    bad = subprocess.run([str(BINARY), "selftest-scrub"], input=b"[]", capture_output=True, timeout=10)
    assert bad.returncode == 2 and bad.stdout == b""
