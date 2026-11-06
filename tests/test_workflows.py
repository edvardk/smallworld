"""Static checks of the GitHub workflows (actionlint runs in CI; these hold the cross-file contracts)."""

import re
import subprocess
import sys

import pytest
import yaml
from conftest import EXAMPLE, REPO, TEST_SHA, make_config_repo

from mcctl import cli, configrepo

WORKFLOWS = sorted((REPO / ".github/workflows").glob("*.yml"))
REUSABLE = REPO / ".github/workflows/server.yml"
CALLER = REPO / "template/.github/workflows/server.yml"


def load(path) -> dict:
    data = yaml.safe_load(path.read_text())
    data["on"] = data.pop(True)  # YAML 1.1 reads the `on` key as True
    return data


def all_uses(path) -> list[str]:
    return re.findall(r"^\s*(?:-\s+)?uses:\s*(\S+)", path.read_text(), re.M)


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_actions_pinned_by_sha(path) -> None:
    for uses in all_uses(path):
        if uses.startswith("./"):
            continue
        assert re.fullmatch(r"[\w.-]+/[\w./-]+@[0-9a-f]{40}", uses), f"{path.name}: {uses}"


def test_reusable_workflow_contracts() -> None:
    wf = load(REUSABLE)
    text = REUSABLE.read_text()
    call = wf["on"]["workflow_call"]
    declared = set(call["secrets"])
    assert set(re.findall(r"secrets\.([A-Z0-9_]+)", text)) == declared
    assert all(v == {"required": False} for v in call["secrets"].values())
    assert set(re.findall(r"(?<!event\.)inputs\.([\w-]+)", text)) == set(call["inputs"])
    validate = wf["jobs"]["validate"]
    outputs = set(validate["outputs"])
    used = set(re.findall(r"needs\.validate\.outputs\.(\w+)", text))
    assert used <= outputs, used - outputs
    for name, job in wf["jobs"].items():
        assert job.get("timeout-minutes"), name
        if name != "validate":
            needs = [job["needs"]] if isinstance(job["needs"], str) else job["needs"]
            assert needs[0] == "validate" and set(needs) <= set(wf["jobs"]), name
        job_text = yaml.safe_dump(job)
        ids = {s.get("id") for s in job.get("steps", [])}
        for ref in re.findall(r"steps\.([\w-]+)\.outputs", job_text):
            assert ref in ids, f"{name}: steps.{ref} doesn't exist"


def test_gate_is_the_required_check_the_ruleset_names() -> None:
    """P10 review: `server / gate` (and `server / validate`) are the checks terraform/github.tf requires on main;
    the gate never gets skipped on a config repo's pull request (a skipped required check counts as passed)."""
    wf = load(REUSABLE)
    gate = wf["jobs"]["gate"]
    assert gate["name"] == "gate" and "name" not in wf["jobs"]["validate"]
    assert gate["if"] == "always() && github.event_name == 'pull_request' && inputs.config-path == '.'"
    assert gate["permissions"] == {"contents": "read"}
    first = gate["steps"][0]
    assert first["if"] == "needs.validate.result != 'success'" and "exit 1" in first["run"]
    assert any("mcctl pr-gate" in st.get("run", "") for st in gate["steps"])
    assert "needs.gate.result == 'success'" in wf["jobs"]["panel-merge"]["if"]
    assert "gate" in wf["jobs"]["panel-merge"]["needs"]
    tf = (REPO / "terraform/github.tf").read_text()
    caller_job = next(iter(load(CALLER)["jobs"]))
    for job in ("validate", "gate"):
        assert f'context        = "{caller_job} / {job}"' in tf, job
    assert tf.count("integration_id = local.github_actions_app_id") == 2


def test_ci_outputs_cover_reusable_workflow(tmp_path) -> None:
    out = tmp_path / "out"
    assert cli.main(["validate", "-C", str(EXAMPLE), "--github-output", str(out)]) == 0
    written = {line.split("=", 1)[0] for line in out.read_text().splitlines()}
    validate = load(REUSABLE)["jobs"]["validate"]
    needed = {
        re.search(r"steps\.config\.outputs\.(\w+)", v).group(1)
        for v in validate["outputs"].values()
        if "steps.config." in v
    }
    needed |= set(re.findall(r"steps\.config\.outputs\.(\w+)", REUSABLE.read_text()))
    assert needed <= written, needed - written


def test_task_picker_knows_the_callers_schedules_and_choices() -> None:
    caller = load(CALLER)
    text = REUSABLE.read_text()
    for cron in caller["on"]["schedule"]:
        c = cron["cron"]
        assert f'"{c}")' in text or (c.startswith("17 4 * * ") and '"17 4 * * "[0-6])' in text), cron
    for task in caller["on"]["workflow_dispatch"]["inputs"]["task"]["options"]:
        assert task in text
        assert f"contains(fromJSON(needs.validate.outputs.tasks), '{task}')" in text, task


def test_caller_template() -> None:
    caller = load(CALLER)
    assert list(caller["jobs"]) == ["server"]
    job = caller["jobs"]["server"]
    assert job["uses"] == "__PRODUCT_REPOSITORY__/.github/workflows/server.yml@__PRODUCT_SHA__"
    assert "# __PRODUCT_VERSION__" in CALLER.read_text()
    # The ceiling for the reusable workflow's jobs: an OIDC token for the tailnet (deploy), and write access to
    # contents + pull requests for P10's panel-merge only (every job declares its own, smaller set).
    assert job["permissions"] == {"contents": "write", "pull-requests": "write", "id-token": "write"}
    assert caller["permissions"] == {}
    assert "inherit" not in yaml.safe_dump(job["secrets"])
    assert set(caller["on"]) == {"push", "pull_request", "workflow_dispatch", "schedule"}
    assert caller["on"]["push"] == {"branches": ["main"]}


def test_product_ci_validates_the_example_config_repo() -> None:
    ci = load(REPO / ".github/workflows/ci.yml")
    job = ci["jobs"]["config-repo"]
    assert job["uses"] == "./.github/workflows/server.yml"
    assert job["with"] == {"config-path": "examples/config-repo"}
    assert job["permissions"] == {"contents": "write", "pull-requests": "write", "id-token": "write"}
    assert "actionlint" in ci["jobs"]
    # The reusable workflow never deploys from the product's own CI.
    assert 'if [ "$CONFIG_PATH" != "." ]; then tasks=\'["validate"]\'; fi' in REUSABLE.read_text()
    release = load(REPO / ".github/workflows/release.yml")
    assert release["jobs"]["ci"]["permissions"] == {
        "contents": "write",
        "pull-requests": "write",
        "id-token": "write",
    }
    # P10: only the panel-merge job writes, and only for a panel/* pull request of a real config repo.
    reusable = load(REUSABLE)
    for name, job in reusable["jobs"].items():
        perms = job.get("permissions", {})
        writes = {k for k, v in perms.items() if v == "write"}
        assert writes <= ({"contents", "pull-requests"} if name == "panel-merge" else {"id-token"}), name
    merge = reusable["jobs"]["panel-merge"]
    assert (
        "startsWith(github.head_ref, 'panel/')" in merge["if"] and "inputs.config-path == '.'" in merge["if"]
    )
    assert "github.event.pull_request.head.repo.full_name == github.repository" in merge["if"]
    runs = "\n".join(step.get("run", "") for step in merge["steps"])
    assert "mcctl panel-pr-check" in runs and "--match-head-commit" in runs


def test_no_deploy_workflows_left_in_the_product() -> None:
    names = {p.name for p in WORKFLOWS}
    assert names == {
        "ci.yml",
        "e2e-mock.yml",
        "release.yml",
        "server.yml",
        "update-lock.yml",
        "dependabot-automerge.yml",
    }


def test_e2e_mock_workflow() -> None:
    """P5 acceptance: init --mock from the answers file, validate, terraform validate, site.yml in a systemd
    Debian 13 container (Tailscale/firewall/zram skipped), real server ping, second run changed=0."""
    path = REPO / ".github/workflows/e2e-mock.yml"
    wf, text = load(path), path.read_text()
    assert wf["permissions"] == {"contents": "read"} and "secrets." not in text and "id-token" not in text
    job = wf["jobs"]["e2e"]
    runs = "\n".join(step.get("run", "") for step in job["steps"])
    assert runs.count("--answers tests/e2e/answers.yml --non-interactive --mock") == 3  # P15: stale pin
    assert "move the product pin" in runs and "fetch-depth: 2" in text
    for fragment in (
        "mcctl validate",
        "mcctl render",
        "terraform -chdir=terraform validate",
        "--skip-tags tailscale,firewall",
        "-e @/src/product/tests/e2e/vars.yml",
        "for run in 1 2",
        "changed=0 +unreachable=0 +failed=0",
        "mcping.status",
        "docker exec -w /src/product/ansible",
    ):
        assert fragment in runs, fragment
    # every MC_SECRET_* site.yml reads is set (fakes) and passed by name only
    from mcctl.secrets import SECRETS

    for s in SECRETS:
        assert s.env in job["env"] and f"-e {s.env}" in runs
    containerfile = (REPO / "tests/e2e/Containerfile").read_text()
    assert re.search(r"^FROM debian:trixie@sha256:[0-9a-f]{64}$", containerfile, re.M)
    # Deleting the apt lists in a just-built image makes `cache_valid_time` skip the update: no packages.
    assert "rm -rf /var/lib/apt/lists" not in containerfile
    # The e2e-only switches are a vars file (so booleans stay booleans; `-e x=false` is a string), and no
    # production file references it.
    e2e_vars = yaml.safe_load((REPO / "tests/e2e/vars.yml").read_text())
    assert e2e_vars["base_zram_enabled"] is False and e2e_vars["mc_world_expected"] is False
    for prod in [*(REPO / "ansible").rglob("*.yml"), *(REPO / ".github/workflows").glob("*.yml")]:
        if prod.name != "e2e-mock.yml":
            assert "tests/e2e/vars.yml" not in prod.read_text(), prod
    # Secrets never go in an answers file (ui.secret reads them from the environment / --mock fakes).
    answers = yaml.safe_load((REPO / "tests/e2e/answers.yml").read_text())
    secret_keys = set(re.findall(r'secret\(\s*"(\w+)"', (REPO / "mcctl/init.py").read_text()))
    assert len(secret_keys) >= 7 and not secret_keys & set(answers)


def test_only_the_tailnet_job_gets_an_oidc_token() -> None:
    # A main-branch OIDC token matches tailscale_federated_identity.ci's trust, so only the job that joins the
    # tailnet may mint one; validate (which installs packages) must not.
    jobs = load(REUSABLE)["jobs"]
    with_token = {name for name, job in jobs.items() if job.get("permissions", {}).get("id-token") == "write"}
    assert with_token == {"deploy"}
    assert load(REUSABLE)["permissions"] == {}
    assert "ACTIONS_ID_TOKEN_REQUEST" not in REUSABLE.read_text()


def test_deploy_runs_only_from_main() -> None:
    wf = load(REUSABLE)
    assert "github.ref == 'refs/heads/main'" in wf["jobs"]["deploy"]["if"]
    picker = next(s for s in wf["jobs"]["validate"]["steps"] if s.get("id") == "task")["run"]
    assert '[ "$REF" != refs/heads/main ]' in picker


@pytest.mark.parametrize("path", [*WORKFLOWS, CALLER], ids=lambda p: p.name)
def test_pip_installs_are_hash_pinned(path) -> None:
    for line in re.findall(r"pip install[^\n]*", path.read_text()):
        assert "--require-hashes" in line and "--no-deps" in line and "-r " in line, f"{path.name}: {line}"
        req = re.search(r"-r (\S+)", line).group(1).removeprefix("product/")
        assert (REPO / req).is_file(), req


def _product_step() -> dict:
    steps = load(REUSABLE)["jobs"]["validate"]["steps"]
    return next(s for s in steps if s.get("id") == "product")


def _run_product_step(tmp_path, env: dict[str, str]) -> subprocess.CompletedProcess:
    step = _product_step()
    out = tmp_path / "out"
    out.write_text("")
    return subprocess.run(
        [sys.executable, "-c", step["run"]],
        cwd=tmp_path,
        env={"GITHUB_OUTPUT": str(out), **env},
        capture_output=True,
        text=True,
        check=False,
    )


def test_product_commit_from_the_callers_pin(tmp_path) -> None:
    make_config_repo(tmp_path / "config")
    env = {
        "CONFIG_PATH": ".",
        "CALLER_REPOSITORY": "alex/my-mc-config",
        "CALLER_WORKFLOW_REF": "alex/my-mc-config/.github/workflows/server.yml@refs/heads/main",
        "CALLER_WORKFLOW_SHA": "2" * 40,
    }
    proc = _run_product_step(tmp_path, env)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out").read_text() == f"repository={configrepo.PRODUCT_REPOSITORY}\nsha={TEST_SHA}\n"
    # Another workflow file in a config repo isn't the pinned caller.
    proc = _run_product_step(
        tmp_path, env | {"CALLER_WORKFLOW_REF": "alex/x/.github/workflows/other.yml@refs/heads/main"}
    )
    assert proc.returncode != 0
    # A tag/branch pin is refused.
    wf = tmp_path / "config" / configrepo.WORKFLOW
    wf.write_text(wf.read_text().replace(TEST_SHA, "main"))
    assert _run_product_step(tmp_path, env).returncode != 0


def test_product_commit_for_the_products_own_ci(tmp_path) -> None:
    proc = _run_product_step(
        tmp_path,
        {
            "CONFIG_PATH": "examples/config-repo",
            "CALLER_REPOSITORY": "edvardk/smallworld",
            "CALLER_WORKFLOW_REF": "edvardk/smallworld/.github/workflows/release.yml@refs/tags/v1.0.0",
            "CALLER_WORKFLOW_SHA": "3" * 40,
        },
    )
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / "out").read_text() == f"repository=edvardk/smallworld\nsha={'3' * 40}\n"


def test_deploys_only_what_changed() -> None:
    """docs/ci.md "Keeping a config repo's minutes low": validate restores the newest record of a successful
    deploy and decides (scripts/deploy_record.py); the deploy job records itself after the smoke test."""
    wf = load(REUSABLE)
    validate, deploy = wf["jobs"]["validate"], wf["jobs"]["deploy"]
    assert deploy["if"].rstrip().endswith("needs.validate.outputs.deploy_needed != 'false'")
    assert validate["outputs"]["deploy_needed"] == "${{ steps.last-deploy.outputs.deploy_needed }}"
    steps = validate["steps"]
    names = [s.get("id") or s.get("name") for s in steps]
    restore = next(s for s in steps if str(s.get("uses", "")).startswith("actions/cache/restore@"))
    decide = next(s for s in steps if s.get("id") == "last-deploy")
    assert names.index("task") < steps.index(restore) < steps.index(decide)
    for step in (restore, decide):
        assert step["if"] == "contains(steps.task.outputs.tasks, 'deploy') && inputs.config-path == '.'"
    assert restore["with"]["restore-keys"] == "mc-deployed-"
    assert not restore["with"]["key"].startswith("mc-deployed-$")  # the exact key never exists: newest prefix
    assert (
        "deploy_record.py decide" in decide["run"] and '--event "$EVENT" --before "$BEFORE"' in decide["run"]
    )
    assert decide["env"]["BEFORE"] == "${{ github.event.before }}"
    dsteps = deploy["steps"]
    record = next(i for i, s in enumerate(dsteps) if "deploy_record.py write" in s.get("run", ""))
    save = next(i for i, s in enumerate(dsteps) if str(s.get("uses", "")).startswith("actions/cache/save@"))
    smoke = next(i for i, s in enumerate(dsteps) if "smoke_test.py" in s.get("run", ""))
    assert smoke < record < save < len(dsteps) - 1 and "if" not in dsteps[record] and "if" not in dsteps[save]
    assert dsteps[save]["with"]["path"] == restore["with"]["path"]
    assert dsteps[save]["with"]["key"] == "mc-deployed-${{ github.run_id }}-${{ github.run_attempt }}"


def test_backup_check_runs_on_the_server_not_weekly_in_actions() -> None:
    text = REUSABLE.read_text()
    assert """"41 9 * * 1") tasks='["expiry-check"]' ;;""" in text
    assert "backup-check" in load(CALLER)["on"]["workflow_dispatch"]["inputs"]["task"]["options"]
    assert (REPO / "ansible/roles/backups/templates/mc-backup-check.timer.j2").is_file()


def test_caches_never_reach_a_job_that_runs_third_party_code() -> None:
    """pip's cache in every job that installs from a hashed requirements file; none in the jobs that boot a
    third-party jar (a cache they saved would be restored on main), and no unverified cache (galaxy) in deploy."""
    for name, job in load(REUSABLE)["jobs"].items():
        steps = job.get("steps", [])
        setup = [s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python@")]
        caches = [s for s in steps if str(s.get("uses", "")).startswith("actions/cache")]
        installs = [
            m for s in steps for m in re.findall(r"-r (product/requirements/\w+\.txt)", s.get("run", ""))
        ]
        if name in ("update-test", "boot-test"):
            assert all("cache" not in s.get("with", {}) for s in setup) and not caches, name
            continue
        for s in setup:
            assert s["with"]["cache"] == "pip" and s["with"]["cache-dependency-path"] in installs, name
        assert all(c["with"]["path"] == "${{ runner.temp }}/deploy-record" for c in caches), name
