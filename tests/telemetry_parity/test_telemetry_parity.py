"""Black-box parity of the player-experience telemetry (docs/ci.md "The telemetry parity harness").

`pytest tests/telemetry_parity` (= --impl=python) runs every scenario against this checkout's
`scripts/mc_agent.py telemetry` (the mc-telemetry.service sidecar) and compares its trace with
tests/telemetry_parity/golden/<scenario>.json; `--impl=rust --agent-bin=PATH` runs the same scenarios against
`PATH telemetry`, which must produce the same traces. `--update-golden` (python only) rewrites the golden files.
Linux only: the sidecar runs in `unshare -rm` (tests/agent_parity/nsinit.py) with fake systemctl/journalctl.
AGENT_PARITY_REQUIRE=1 (CI) turns "namespaces unavailable" from a skip into a failure, as for the agent harness.

The scenarios run in parallel threads (each has its own box, fake Worker and fake systemd), so the whole set costs
about as long as the slowest one (the segment and backfill scenarios: a 30 s SEGMENT_S plus a few read loops).
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import sys
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="Linux only (unshare, fake binaries)"
)

if sys.platform.startswith("linux"):
    sys.path[:0] = [
        str(Path(__file__).resolve().parent),
        str(Path(__file__).resolve().parent.parent / "agent_parity"),
    ]
    from harness import differences, impl_for, namespaces_work
    from tel_harness import GOLDEN, TelScenario, read_golden, write_golden
    from tel_scenarios import SCENARIOS
else:  # collected on every OS, run on Linux only
    SCENARIOS = {}

NAMES = sorted(SCENARIOS)


def run_one(name: str, impl: Any, tmp: Path) -> Any:
    s = TelScenario(name, impl, tmp / name)
    try:
        SCENARIOS[name](s)
        return s.trace()
    except Exception as e:  # reported by the scenario's own test
        for p in s.procs:
            if p.alive():
                p.signal(signal.SIGABRT)  # faulthandler: every thread's stack on stderr
                with contextlib.suppress(Exception):
                    p.wait(10)
        stderr = "\n".join(line for p in s.procs for line in [*p.stdout[-30:], *p.stderr[-60:]])
        seen = [(r["route"], r.get("status")) for r in s.worker.requests[-10:]]
        calls = [" ".join(c["argv"]) for c in s.calls()[-6:]]
        return RuntimeError(f"{type(e).__name__}: {e}\n{traceback.format_exc()}\nsidecar output:\n{stderr}\n"
                            f"last Worker requests: {seen}\nlast systemctl/journalctl calls: {calls}")  # fmt: skip
    finally:
        s.close()


@pytest.fixture(scope="session")
def telemetry_runs(
    request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory
) -> dict[str, Any]:
    config = request.config
    impl, why = impl_for(config)
    if impl is None:
        return {"skip": why}
    if config.getoption("--update-golden") and impl.name != "python":
        return {"fail": "--update-golden takes the golden files from --impl=python only"}
    tmp = tmp_path_factory.mktemp("telemetry-parity")
    why = namespaces_work(tmp)
    if why:
        if os.environ.get("AGENT_PARITY_REQUIRE") == "1":
            return {"fail": f"AGENT_PARITY_REQUIRE=1 but {why}"}
        return {"skip": why}
    wanted = [
        item.callspec.params["name"]
        for item in request.session.items
        if getattr(item, "originalname", "") == "test_telemetry_scenario" and hasattr(item, "callspec")
    ]
    with ThreadPoolExecutor(max_workers=max(1, len(wanted))) as pool:
        futures = {name: pool.submit(run_one, name, impl, tmp) for name in wanted}
        return {"traces": {name: f.result() for name, f in futures.items()}, "tmp": tmp}


@pytest.mark.parametrize("name", NAMES)
def test_telemetry_scenario(
    name: str, telemetry_runs: dict[str, Any], request: pytest.FixtureRequest
) -> None:
    if "skip" in telemetry_runs:
        pytest.skip(telemetry_runs["skip"])
    if "fail" in telemetry_runs:
        pytest.fail(telemetry_runs["fail"])
    got = telemetry_runs["traces"][name]
    if isinstance(got, Exception):
        pytest.fail(f"scenario {name} failed:\n{got}")
    if request.config.getoption("--update-golden"):
        write_golden(name, got)
        return
    want = read_golden(name)
    assert want is not None, f"no golden trace for {name}: run pytest tests/telemetry_parity --update-golden"
    if got != want:
        observed = telemetry_runs["tmp"] / f"{name}.observed.json"
        observed.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n")
        diff = "\n".join(differences(want, got))
        pytest.fail(f"{name}: the trace differs from golden/{name}.json (observed: {observed})\n{diff}")


def test_every_golden_file_has_a_scenario() -> None:
    have = {p.stem for p in GOLDEN.glob("*.json")} if sys.platform.startswith("linux") else set()
    assert have == set(NAMES), (
        f"golden files without a scenario: {sorted(have - set(NAMES))}, "
        f"scenarios without a golden file: {sorted(set(NAMES) - have)}"
    )
