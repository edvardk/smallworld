"""Black-box parity of mc-agent implementations (docs/ci.md "The agent parity harness").

`pytest tests/agent_parity` (= --impl=python) runs every scenario against this checkout's scripts/mc_agent.py
and compares its trace with tests/agent_parity/golden/<scenario>.json; `--impl=rust --agent-bin=PATH` runs the
same scenarios against a binary, which must produce the same traces. `--update-golden` (python only) rewrites
the golden files. Linux only: the agent runs in `unshare -rm` with fake systemctl/journalctl binaries.

The scenarios run in parallel threads (each has its own box, fakes and ports) when the session first needs
them, so the whole set costs about as long as the slowest one.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
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
    from fakes import make_cert
    from harness import GOLDEN, Scenario, differences, impl_for, namespaces_work, read_golden, write_golden
    from scenarios import SCENARIOS
else:  # collected on every OS, run on Linux only
    SCENARIOS = {}

NAMES = sorted(SCENARIOS)


def run_one(name: str, impl: Any, tmp: Path, cert: tuple[Path, Path]) -> Any:
    s = Scenario(name, impl, tmp / name, cert)
    try:
        SCENARIOS[name](s)
        return s.trace()
    except Exception as e:  # reported by the scenario's own test
        tree = subprocess.run(["ps", "-o", "pid,ppid,etimes,args", "--forest", "-u", str(os.getuid())],
                              capture_output=True, text=True, check=False).stdout  # fmt: skip
        tree = "\n".join(line for line in tree.splitlines() if str(s.tmp) in line or "mc_agent" in line)
        for p in s.procs:
            if p.alive():
                p.signal(signal.SIGABRT)  # faulthandler: every thread's stack on stderr
                with contextlib.suppress(Exception):
                    p.wait(10)
        keep = os.environ.get("AGENT_PARITY_KEEP")  # a directory for the full stderr of failed scenarios
        if keep:
            Path(keep).mkdir(parents=True, exist_ok=True)
            for k, p in enumerate(s.procs):
                (Path(keep) / f"{name}-{k}.txt").write_text("\n".join(p.stderr) + "\n")
        stderr = "\n".join(line for p in s.procs for line in [*p.stderr[:20], "...", *p.stderr[-60:]]
                           if not line.startswith(("Thread 0x", '  File "/usr')))  # fmt: skip
        jobs = [(j.get("id"), j.get("state"), j.get("step")) for r in s.worker.requests_to("POST /ingest/job")
                for j in [(r["body"] or {}).get("job") or {}]][-6:]  # fmt: skip
        results = [s.worker.results.get(rid) for _, rid in s.worker.result_order[-4:]]
        stderr += f"\nlast job reports: {jobs}\nlast results: {results}"
        stderr += "\nprocesses:\n" + tree
        seen = [r["route"] for r in s.worker.requests[-8:]]
        calls = [" ".join(c["argv"]) for c in s.calls()[-6:]]
        return RuntimeError(f"{type(e).__name__}: {e}\n{traceback.format_exc()}\nagent stderr:\n{stderr}\n"
                            f"last Worker requests: {seen}\nlast systemctl/journalctl calls: {calls}")  # fmt: skip
    finally:
        s.close()


@pytest.fixture(scope="session")
def parity_runs(request: pytest.FixtureRequest, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    config = request.config
    impl, why = impl_for(config)
    if impl is None:
        return {"skip": why}
    if config.getoption("--update-golden") and impl.name != "python":
        return {"fail": "--update-golden takes the golden files from --impl=python only"}
    tmp = tmp_path_factory.mktemp("agent-parity")
    why = namespaces_work(tmp)
    if why:
        if os.environ.get("AGENT_PARITY_REQUIRE") == "1":
            return {"fail": f"AGENT_PARITY_REQUIRE=1 but {why}"}
        return {"skip": why}
    wanted = [
        item.callspec.params["name"]
        for item in request.session.items
        if getattr(item, "originalname", "") == "test_scenario" and hasattr(item, "callspec")
    ]
    cert = make_cert(tmp)
    with ThreadPoolExecutor(max_workers=max(4, min(12, len(wanted) or 1))) as pool:
        futures = {name: pool.submit(run_one, name, impl, tmp, cert) for name in wanted}
        return {"traces": {name: f.result() for name, f in futures.items()}, "tmp": tmp}


@pytest.mark.parametrize("name", NAMES)
def test_scenario(name: str, parity_runs: dict[str, Any], request: pytest.FixtureRequest) -> None:
    if "skip" in parity_runs:
        pytest.skip(parity_runs["skip"])
    if "fail" in parity_runs:
        pytest.fail(parity_runs["fail"])
    got = parity_runs["traces"][name]
    if isinstance(got, Exception):
        pytest.fail(f"scenario {name} failed:\n{got}")
    if request.config.getoption("--update-golden"):
        write_golden(name, got)
        return
    want = read_golden(name)
    assert want is not None, f"no golden trace for {name}: run pytest tests/agent_parity --update-golden"
    if got != want:
        import json

        observed = parity_runs["tmp"] / f"{name}.observed.json"
        observed.write_text(json.dumps(got, indent=1, ensure_ascii=False) + "\n")
        diff = "\n".join(differences(want, got))
        pytest.fail(f"{name}: the trace differs from golden/{name}.json (observed: {observed})\n{diff}")


def test_every_golden_file_has_a_scenario() -> None:
    have = {p.stem for p in GOLDEN.glob("*.json")}
    assert have == set(NAMES), (
        f"golden files without a scenario: {sorted(have - set(NAMES))}, "
        f"scenarios without a golden file: {sorted(set(NAMES) - have)}"
    )
