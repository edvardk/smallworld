"""The product CI's minute budget (docs/ci.md): change detection, triggers, required checks that always report."""

import re
import subprocess

import ci_changes
import pytest
import yaml
from conftest import REPO

from mcctl import productsetup

WORKFLOWS = REPO / ".github/workflows"


def load(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text())
    data["on"] = data.pop(True)  # YAML 1.1 reads the `on` key as True
    return data


@pytest.mark.parametrize(
    ("files", "want"),
    [
        (["docs/ci.md", "README.md", "docs/images/setup-mock.svg"], set()),
        (["CHANGELOG.md", "LICENSE"], set()),
        (["docs/images/brand-server-icon.png"], {"code", "plugin"}),
        (["template/README.md"], {"code", "e2e"}),
        (["tests/test_docs.py"], {"code"}),
        (["ansible/roles/agent/tasks/main.yml"], {"code", "e2e"}),
        (["ansible/roles/minecraft/tasks/main.yml"], {"code", "e2e", "e2e_full"}),
        (["ansible/group_vars/all/main.yml"], {"code", "e2e", "plugin"}),
        (["plugin/mc-brand/src/main/java/X.java"], {"code", "e2e", "e2e_full", "plugin"}),
        (["plugin/mc-telemetry/build.yml"], {"code", "e2e", "e2e_full", "plugin", "seeds"}),
        (["client/mod/src/main/java/X.java"], {"code", "plugin"}),
        (["watchdog/worker.js"], {"code", "e2e", "e2e_full"}),
        (["terraform/github.tf"], {"code"}),
        ([".github/workflows/ci.yml"], {"code", "workflows"}),
        ([".github/workflows/e2e-mock.yml"], {"code", "workflows", "e2e"}),
        (["examples/config-repo/.github/workflows/server.yml"], {"code", "workflows"}),
        (["template/server.lock.yml"], {"code", "plugin", "e2e", "e2e_full", "seeds"}),
        (["worlds/seeds.yml"], {"code", "seeds"}),
        (["watchdog/panel/seeds.json"], {"code", "e2e", "e2e_full", "seeds"}),
        (["scripts/boot_test.py"], {"code", "e2e", "seeds"}),
        ([], set()),
    ],
)
def test_groups(files: list[str], want: set[str]) -> None:
    got = ci_changes.groups(files)
    assert set(got) == set(ci_changes.GROUPS)
    assert {k for k, v in got.items() if v} == want


def test_unknown_changes_run_everything() -> None:
    assert all(ci_changes.groups(None).values())

    def boom(argv: list[str]) -> str:
        raise subprocess.CalledProcessError(128, ["git", *argv])

    assert ci_changes.changed_files("pull_request", "", git=boom) is None
    assert ci_changes.changed_files("push", "abc", git=boom) is None
    # a new branch (before = zeros), a schedule, a dispatch, release.yml's tag push: everything
    assert ci_changes.changed_files("push", "0" * 40, git=lambda a: "x\n") is None
    for event in ("schedule", "workflow_dispatch", ""):
        assert ci_changes.changed_files(event, "abc", git=lambda a: "x\n") is None


def test_pull_request_diffs_the_merge_commit_against_its_base() -> None:
    calls: list[list[str]] = []

    def git(argv: list[str]) -> str:
        calls.append(argv)
        return "docs/a.md\nmcctl/cli.py\n"

    assert ci_changes.changed_files("pull_request", "", git=git) == ["docs/a.md", "mcctl/cli.py"]
    assert calls == [["diff", "--name-only", "HEAD^1", "HEAD"]]
    calls.clear()
    assert ci_changes.changed_files("push", "abc123", git=git) == ["docs/a.md", "mcctl/cli.py"]
    assert calls[-1] == ["diff", "--name-only", "abc123", "HEAD"]


def test_every_product_job_has_a_timeout_and_runs_are_cancelled_when_obsolete() -> None:
    for path in sorted(WORKFLOWS.glob("*.yml")):
        wf = load(path.name)
        for name, job in wf["jobs"].items():
            if "uses" in job:
                continue  # a reusable-workflow call: its own jobs carry the timeouts
            assert job.get("timeout-minutes"), f"{path.name}: {name}"
    for name in ("ci.yml", "e2e-mock.yml"):
        conc = load(name)["concurrency"]
        assert conc["group"] == "${{ github.workflow }}-${{ github.ref }}"
        assert "pull_request" in conc["cancel-in-progress"]


def test_required_checks_always_report() -> None:
    """A required check whose workflow never starts stays pending forever: the workflows carrying required checks
    have no `paths` filter on pull_request; a job that may be skipped is skipped by its `if:` (reported as success),
    and an unknown change set (failed `changes` job, empty outputs) runs it."""
    jobs = {}
    for name in ("ci.yml", "e2e-mock.yml"):
        wf = load(name)
        pr = wf["on"]["pull_request"] or {}
        assert "paths" not in pr and "paths-ignore" not in pr, name
        jobs |= wf["jobs"]
    for check in productsetup.REQUIRED_CHECKS:
        job = jobs[check.split(" / ")[0]]
        cond = job.get("if", "")
        assert "github.event_name" not in cond, check  # never excluded by the event
        if cond:
            assert cond.startswith("${{ !cancelled()"), check
            assert re.fullmatch(
                r"\$\{\{ !cancelled\(\)( && needs\.changes\.outputs\.\w+ != 'false')? \}\}", cond
            )
    # e2e is an aggregator of the shards: it must wait for all of them and run whenever they may have
    e2e = load("e2e-mock.yml")["jobs"]
    assert "e2e-shard" in e2e["e2e"]["needs"] and e2e["e2e-shard"]["if"] == e2e["e2e"]["if"]
    ci = load("ci.yml")["jobs"]
    assert "if" not in ci["gitleaks"] and "if" not in ci["config-repo"] and "if" not in ci["publication"]
    lint = "\n".join(str(s.get("if", "")) for s in ci["lint"]["steps"])
    assert "env.CODE" in lint and ci["lint"]["env"]["CODE"] == "${{ needs.changes.outputs.code != 'false' }}"


def test_e2e_triggers_and_benches() -> None:
    wf = load("e2e-mock.yml")
    assert set(wf["on"]) == {"pull_request", "schedule", "workflow_dispatch"}  # not on every push
    assert wf["on"]["pull_request"] == {"branches": ["main"]}
    crons = [c["cron"] for c in wf["on"]["schedule"]]
    text = (WORKFLOWS / "e2e-mock.yml").read_text()
    assert crons[0].endswith(" * * *") and crons[1].endswith(" 1 * *")  # nightly e2e, monthly benches
    assert text.count(f"'{crons[1]}'") == 1 and text.count(f'"{crons[1]}"') == 2  # its window, its decision
    benches = [n for n in wf["jobs"] if n.startswith("bench")]
    assert set(benches) == {"bench", "bench-gc", "bench-brand", "bench-profile", "bench-view"}
    for name in benches:
        assert (
            wf["jobs"][name]["needs"] == "changes"
            and wf["jobs"][name]["if"] == "needs.changes.outputs.bench == 'true'"
        )
    decide = next(s for s in wf["jobs"]["changes"]["steps"] if s.get("id") == "decide")
    # every pull request runs e2e (its heavy half when it touches what that exercises) and never benches; the
    # nightly run is the whole e2e
    assert 'pull_request) e2e=true; full="$CHG_FULL" ;;' in decide["run"]
    assert "else e2e=true; full=true; fi ;;" in decide["run"]
    assert decide["run"].startswith("bench=false\n")
    # the self-hosted runner only for trusted code: never a fork's pull request (H1)
    trusted = (
        "(github.event_name != 'pull_request' || "
        "github.event.pull_request.head.repo.full_name == github.repository)"
    )
    shard = wf["jobs"]["e2e-shard"]
    assert shard["runs-on"] == f"${{{{ {trusted} && vars.E2E_RUNNER || 'ubuntu-24.04' }}}}"
    # parallel shards on GitHub's runners, one at a time on the self-hosted one (host-network ports clash)
    strategy = shard["strategy"]
    assert strategy["fail-fast"] is False
    assert strategy["max-parallel"] == f"${{{{ fromJSON({trusted} && vars.E2E_RUNNER && '1' || '4') }}}}"
    include = {m["shard"]: m for m in strategy["matrix"]["include"]}
    assert set(include) == {"core", "agent", "telemetry-brand", "migrate-p16"}
    assert {s for s, m in include.items() if m["full_only"]} == {"telemetry-brand", "migrate-p16"}
    assert all(m["timeout"] for m in include.values()) and shard["timeout-minutes"] == "${{ matrix.timeout }}"
    assert shard["if"] == "${{ !cancelled() && needs.changes.outputs.e2e != 'false' }}"
    # a heavy shard in an essential run skips everything (its boot too); the other shards always work
    assert shard["env"]["ACTIVE"] == (
        "${{ (!matrix.full_only || needs.changes.outputs.full != 'false') && 'true' || 'false' }}"
    )
    steps = shard["steps"]

    def where(fragment: str) -> list[str]:
        return [str(s.get("if")) for s in steps if fragment in s.get("name", s.get("uses", ""))]

    for part in ("checkout", "setup-python", "Start a systemd", "Install Ansible", "site.yml", "status ping"):
        assert where(part) == ["env.ACTIVE == 'true'"], part
    for part in ("init again", "stale product pin", "client on", "terraform validate", "Hardening", "Reboot",
                 "Deploy checks", "Firewall", "P12 dry runs"):  # fmt: skip
        assert where(part) == ["matrix.shard == 'core'"], part
    assert where("mc-agent checks") == ["matrix.shard == 'agent'"]
    # the heavy half is skipped only on an explicit `false`, in its own shards
    for part in ("telemetry", "Branding", "setup-node"):
        assert where(part) == ["matrix.shard == 'telemetry-brand' && env.FULL != 'false'"], part
    for part in ("Migrate", "P16"):  # P16 runs on the migrated box: same shard, after it
        assert where(part) == ["matrix.shard == 'migrate-p16' && env.FULL != 'false'"], part
    site = next(s for s in steps if s.get("name", "").startswith("site.yml"))
    assert site["env"]["RUNS"] == "${{ matrix.shard == 'core' && '1 2' || '1' }}"  # idempotency: core's
    assert any(s.get("if") == "failure()" and "journalctl" in s["run"] for s in steps)
    for s in steps:
        if "run" in s:  # a hung step fails on its own clock (an e2e hang was seen), not the job's
            assert s.get("timeout-minutes"), s["name"]
    # the required check: one job however the shards are cut, failing unless every shard passed
    agg = wf["jobs"]["e2e"]
    assert agg["needs"] == ["changes", "e2e-shard"] and agg["if"] == shard["if"]
    assert agg["runs-on"] == "ubuntu-24.04" and agg["timeout-minutes"] == 5
    (check,) = agg["steps"]
    assert check["env"]["RESULT"] == "${{ needs.e2e-shard.result }}"
    assert check["run"].rstrip().endswith('[ "$RESULT" = success ]')  # a skipped shard fails it too


def test_update_lock_dispatches_the_full_e2e() -> None:
    text = (WORKFLOWS / "update-lock.yml").read_text()
    assert 'gh workflow run e2e-mock.yml --ref "$branch" -f scope=full' in text
    inputs = load("e2e-mock.yml")["on"]["workflow_dispatch"]["inputs"]
    assert inputs["scope"]["default"] == "full" and inputs["bench"]["default"] is False


def test_dependabot_batches_monthly() -> None:
    cfg = yaml.safe_load((REPO / ".github/dependabot.yml").read_text())
    for update in cfg["updates"]:
        assert update["schedule"]["interval"] == "monthly"
        (group,) = update["groups"].values()
        assert group["patterns"] == ["*"] and group["update-types"] == ["minor", "patch"]


def test_a_scheduled_run_looks_at_its_window() -> None:
    calls: list[list[str]] = []

    def git(argv: list[str]) -> str:
        calls.append(argv)
        return "\nmcctl/cli.py\n\ndocs/a.md\nmcctl/cli.py\n"

    assert ci_changes.changed_files("schedule", "", git=git, since_days=7) == ["docs/a.md", "mcctl/cli.py"]
    assert calls == [["log", "--since=7 days ago", "--name-only", "--format=", "HEAD"]]
    # an idle window: nothing heavy runs
    assert not any(
        ci_changes.groups(ci_changes.changed_files("schedule", "", git=lambda a: "", since_days=7)).values()
    )


def test_ci_runs_nightly_and_the_controller_matrix() -> None:
    """The public product repo is thorough (no minutes): ci.yml nightly with everything, the controller on Linux,
    macOS and Windows x every supported Python, on pull requests too."""
    ci = load("ci.yml")
    (cron,) = [c["cron"] for c in ci["on"]["schedule"]]
    assert cron.endswith(" * * *")
    step = next(s for s in ci["jobs"]["changes"]["steps"] if s.get("id") == "changes")
    assert "--since-days" not in step["run"]  # the nightly run: everything
    job = ci["jobs"]["controller"]
    assert job["if"] == "${{ !cancelled() && needs.changes.outputs.code != 'false' }}"
    matrix = job["strategy"]["matrix"]
    assert matrix["os"] == ["ubuntu-24.04", "macos-15", "windows-2025"]
    assert matrix["python"] == ["3.12", "3.13", "3.14"] and job["strategy"]["fail-fast"] is False
    from mcctl import MIN_PYTHON

    assert matrix["python"][0] == ".".join(map(str, MIN_PYTHON))
    runs = {s.get("name"): s for s in job["steps"]}
    assert runs["pytest (everything)"]["if"] == "runner.os == 'Linux'"
    other = runs["pytest (the controller's tests that make sense here)"]
    assert other["if"] == "runner.os != 'Linux'" and '-m "controller and not posix_only"' in other["run"]
    assert job["env"]["PYTHONUTF8"] == "1"


def test_the_cross_platform_subset(monkeypatch) -> None:
    """tests/conftest.py: the controller's files exist and get the marker; on Windows nothing else is collected
    (the box's scripts import fcntl) and posix_only tests are skipped."""
    import conftest

    for name in conftest.CONTROLLER_TESTS:
        assert (REPO / "tests" / name).is_file(), name
    test_file = REPO / "tests" / "test_admin.py"
    assert conftest.pytest_ignore_collect(test_file, None) is None
    monkeypatch.setattr(conftest.sys, "platform", "win32")
    assert conftest.pytest_ignore_collect(test_file, None) is True
    assert conftest.pytest_ignore_collect(REPO / "tests" / "test_gate.py", None) is False
    assert conftest.pytest_ignore_collect(REPO / "tests" / "conftest.py", None) is None
    pyproject = (REPO / "pyproject.toml").read_text()
    assert '"controller: ' in pyproject and '"posix_only: ' in pyproject
