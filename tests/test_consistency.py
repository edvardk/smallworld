"""Cross-phase consistency: pins and names that must agree across Terraform, Ansible, scripts and CI."""

import ast
import re
import sys

import pytest
import yaml
from conftest import REPO

GROUP_VARS = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
SERVER_LOCK = yaml.safe_load((REPO / "template/server.lock.yml").read_text())
PLUGINS_LOCK = yaml.safe_load((REPO / "template/plugins.lock.yml").read_text())
REUSABLE = REPO / ".github/workflows/server.yml"
CALLER = REPO / "template/.github/workflows/server.yml"
# Actions secrets the workflows use that aren't MC_SECRET_* (Tailscale federation, product checkout).
WORKFLOW_ONLY_SECRETS = {
    "TS_OAUTH_CLIENT_ID",
    "TS_AUDIENCE",
    "PRODUCT_READ_TOKEN",
    "UPDATER_APP_ID",
    "UPDATER_APP_KEY",
    "ARTIFACT_PUBLISH_TOKEN",  # v1.1: the deploy job's mirror uploads (never Ansible's)
}


def _wf(path) -> dict:
    return yaml.safe_load(path.read_text())


SERVER_SCRIPTS = [
    "mc_panel.py",
    "telemetry.py",
    "mc_rcon.py",
    "notify.py",
    "alerts.py",
    "backup.py",
    "restore.py",
    "health.py",
    "admin.py",
    "mc_agent.py",
    "reboot_gate.py",
    "restic_rekey.py",
    "worlds.py",  # v1.2 world library
    "world_info.py",
    "backup_check.py",
]


def test_temurin_matches_java_major() -> None:
    assert GROUP_VARS["temurin_version"].split(".")[0] == str(SERVER_LOCK["java_major"])


def test_restic_pin_matches_backup_check() -> None:
    env = _wf(REUSABLE)["jobs"]["backup-check"]["env"]
    assert env["RESTIC_VERSION"] == GROUP_VARS["restic_version"]
    assert env["RESTIC_SHA256"] == GROUP_VARS["restic_sha256"]["amd64"]


def test_tailscale_pin_matches_deploy() -> None:
    steps = _wf(REUSABLE)["jobs"]["deploy"]["steps"]
    ts = next(s for s in steps if "tailscale/github-action" in s.get("uses", ""))
    assert ts["with"]["version"] == GROUP_VARS["tailscale_version"]
    # The tailnet name comes from server.yml (host.tailscale_name), not a hard-coded host.
    assert ts["with"]["ping"] == "${{ needs.validate.outputs.tailscale_name }}"


def test_tailscale_server_tag_matches_terraform() -> None:
    """The tailscale role checks the node carries the tag the bootstrap key gives it."""
    tf = (REPO / "terraform/tailscale.tf").read_text()
    key = tf[tf.index('resource "tailscale_tailnet_key" "server"') :].split("\n}", 1)[0]
    assert f'tags                = ["{GROUP_VARS["mc_tailscale_server_tag"]}"]' in key
    from mcctl import bootstrap

    assert bootstrap.SERVER_TAG == GROUP_VARS["mc_tailscale_server_tag"]


def test_lock_entries_are_pinned() -> None:
    entries = [SERVER_LOCK["server"], *PLUGINS_LOCK["plugins"]]
    for e in entries:
        assert re.fullmatch(r"[0-9a-f]{64}", e["sha256"]), e
        assert e["url"].startswith("https://"), e


def test_server_scripts_are_stdlib_only() -> None:
    allowed = set(sys.stdlib_module_names) | {s.removesuffix(".py") for s in SERVER_SCRIPTS}
    for name in SERVER_SCRIPTS:
        tree = ast.parse((REPO / "scripts" / name).read_text())
        for node in ast.walk(tree):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                mods = [node.module.split(".")[0]]
            assert set(mods) <= allowed, f"{name} imports {mods}"


def test_base_role_installs_all_server_scripts() -> None:
    tasks = (REPO / "ansible/roles/base/tasks/main.yml").read_text()
    m = re.search(r"loop: \[(.*?)\]", tasks.split("Install server-side scripts")[1], re.S)
    assert m and sorted(x.strip() for x in m.group(1).split(",")) == sorted(SERVER_SCRIPTS)


# Actions secrets of the product repository itself (set by its maintainer, docs/publishing.md), not of a config
# repo: only the product's own CI uses them, never the reusable workflow.
PRODUCT_REPO_SECRETS = {"PUBLISH_DENYLIST": {"ci.yml", "release.yml"}}


def test_actions_secrets_used_by_workflows_exist_in_terraform() -> None:
    tf = (REPO / "terraform/github.tf").read_text()
    for wf in [*(REPO / ".github/workflows").glob("*.yml"), CALLER]:
        for secret in re.findall(r"secrets\.([A-Z0-9_]+)", wf.read_text()):
            if secret in PRODUCT_REPO_SECRETS:
                assert wf.parent == REPO / ".github/workflows" and wf.name in PRODUCT_REPO_SECRETS[secret], wf
                continue
            assert re.search(rf"\b{secret}\s*=", tf), (
                f"{wf.name} uses secrets.{secret}, not created by Terraform"
            )


def _ansible_playbook_steps(wf: dict) -> list[dict]:
    return [
        step
        for job in wf["jobs"].values()
        for step in job.get("steps", [])
        if "ansible-playbook" in step.get("run", "")
    ]


def test_secret_names_agree_everywhere() -> None:
    # One table (mcctl/secrets.py) -> Terraform outputs, Actions secrets, CI env, Ansible variables.
    from mcctl.secrets import SECRETS

    outputs = set(re.findall(r'output "([a-z0-9_]+)"', (REPO / "terraform/outputs.tf").read_text()))
    assert {s.output for s in SECRETS} <= outputs

    github_tf = (REPO / "terraform/github.tf").read_text()
    for s in SECRETS:
        assert re.search(rf"^\s+{s.name}\s+=", github_tf, re.M), f"{s.name} isn't an Actions secret"

    secrets_yml = yaml.safe_load((REPO / "ansible/group_vars/all/secrets.yml").read_text())
    assert secrets_yml == {
        s.ansible_var: f"{{{{ lookup('ansible.builtin.env', '{s.env}') }}}}" for s in SECRETS
    }

    used = set()
    for p in [
        *(REPO / "ansible").rglob("*.yml"),
        *(REPO / "ansible").rglob("*.j2"),
        *(REPO / "server-config").rglob("*.j2"),
    ]:
        used |= set(re.findall(r"\bmc_secret_[a-z0-9_]+", p.read_text()))
    assert used == {s.ansible_var for s in SECRETS}, used ^ {s.ansible_var for s in SECRETS}

    wf = _wf(REUSABLE)
    expected_env = {s.env: f"${{{{ secrets.{s.name} }}}}" for s in SECRETS}
    steps = _ansible_playbook_steps(wf)
    assert len(steps) == 2
    for step in steps:
        assert {k: v for k, v in step["env"].items() if k.startswith("MC_SECRET_")} == expected_env, step[
            "name"
        ]
    for job in wf["jobs"].values():  # step-scoped only: other steps (third-party actions) never see them
        assert not any(k.startswith("MC_SECRET_") for k in job.get("env", {}))

    # The config repo passes every secret explicitly (not `secrets: inherit`), the reusable workflow declares
    # exactly those, and Terraform creates each one.
    names = {s.name for s in SECRETS} | WORKFLOW_ONLY_SECRETS
    caller = _wf(CALLER)["jobs"]["server"]
    assert caller["secrets"] == {n: f"${{{{ secrets.{n} }}}}" for n in names}
    assert set(_wf(REUSABLE)[True]["workflow_call"]["secrets"]) == names  # YAML 1.1 reads `on` as True
    for n in names:
        assert re.search(rf"^\s+{n}\s+=", github_tf, re.M), f"{n} isn't an Actions secret"

    site = yaml.safe_load((REPO / "ansible/site.yml").read_text())[0]
    check = next(t for t in site["pre_tasks"] if t["name"] == "Check secrets are present")
    assert check["ansible.builtin.assert"]["that"] == [
        f"{s.ansible_var} | length > 0" for s in SECRETS if s.required
    ]


def test_no_vault_left() -> None:
    # P2 removed the Ansible vault; Terraform state is the only secret store. The design doc describes
    # the removal, and this test has to name the word.
    needle = "vault"
    exempt = {REPO / "docs/design/product-v1.md", REPO / "tests/test_consistency.py"}
    skip_dirs = {".git", ".ruff_cache", "__pycache__", ".terraform", ".venv", ".pytest_cache", ".mcctl"}
    skip_dirs |= {"plans"}  # working notes on release branches, never on main (the public squash drops them)
    skip_dirs |= {".claude"}  # agent worktrees: other checkouts of this repo
    hits = []
    for p in REPO.rglob("*"):
        if p.is_dir() or p in exempt or skip_dirs & set(p.relative_to(REPO).parts):
            continue
        try:
            text = p.read_text()
        except UnicodeDecodeError:
            continue
        if needle in text.lower() or needle in p.name.lower():
            hits.append(str(p.relative_to(REPO)))
    assert hits == []


BLOCK_KEYS = ("block", "rescue", "always")
PLAYBOOKS = [REPO / "ansible/site.yml", REPO / "ansible/bootstrap.yml"]


def _walk_tasks(tasks: list | None, inherited_no_log: bool = False):
    """Yield (task, effective no_log) for every leaf task, descending into block/rescue/always."""
    for task in tasks or []:
        no_log = task.get("no_log", inherited_no_log) is True
        if any(k in task for k in BLOCK_KEYS):
            for k in BLOCK_KEYS:
                yield from _walk_tasks(task.get(k), no_log)
        else:
            yield task, no_log


def _secret_aliases() -> set[str]:
    """mc_secret_* plus every variable whose value refers to one, transitively (role defaults/vars,
    group_vars, play/task/block vars and set_fact), so `x: "{{ mc_secret_y }}"` can't dodge the check."""
    defs: dict[str, str] = {}

    def collect(mapping: object) -> None:
        if isinstance(mapping, dict):
            for k, v in mapping.items():
                defs[str(k)] = defs.get(str(k), "") + yaml.safe_dump(v)

    for p in [
        *(REPO / "ansible/roles").glob("*/defaults/*.yml"),
        *(REPO / "ansible/roles").glob("*/vars/*.yml"),
    ]:
        collect(yaml.safe_load(p.read_text()))
    for p in (REPO / "ansible/group_vars").rglob("*.yml"):
        collect(yaml.safe_load(p.read_text()))

    def collect_tasks(tasks: list | None) -> None:
        for task in tasks or []:
            if not isinstance(task, dict):
                continue
            collect(task.get("vars"))
            collect(task.get("ansible.builtin.set_fact") or task.get("set_fact"))
            for k in BLOCK_KEYS:
                collect_tasks(task.get(k))

    for path in _task_files():
        data = yaml.safe_load(path.read_text()) or []
        for item in data:
            if isinstance(item, dict) and "hosts" in item:  # a play
                collect(item.get("vars"))
                for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                    collect_tasks(item.get(section))
        collect_tasks([t for t in data if isinstance(t, dict) and "hosts" not in t])

    names = {n for n in defs if n.startswith("mc_secret_")}
    while True:
        more = {
            n
            for n, text in defs.items()
            if n not in names and any(re.search(rf"\b{a}\b", text) for a in names)
        }
        if not more:
            return names
        names |= more


def _task_files() -> list:
    roles = REPO / "ansible/roles"
    return sorted([*roles.rglob("tasks/*.yml"), *roles.rglob("handlers/*.yml"), *PLAYBOOKS])


def _leaf_tasks(path) -> list:
    data = yaml.safe_load(path.read_text()) or []
    if path in PLAYBOOKS:  # playbook: walk each play's task sections
        out = []
        for play in data:
            for section in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                out += list(_walk_tasks(play.get(section), play.get("no_log") is True))
        return out
    return list(_walk_tasks(data))


def test_tasks_writing_secrets_use_no_log() -> None:
    # A task that renders a secret value (an mc_secret_* variable or an alias of one, inline or through a
    # template) must not log it: in roles, handlers and playbooks, inside block/rescue/always too. mc_config
    # is the exception: its `settings` argument is no_log in the module spec and its diff lists key names
    # only. Quiet asserts only test the values (`| length > 0`) and print fail_msg, never the value.
    aliases = _secret_aliases()
    assert {"mc_secret_rcon_password", "mc_secret_discord_webhook_url"} <= aliases
    pattern = re.compile(r"\b(" + "|".join(sorted(aliases)) + r")\b")
    secret_templates = {
        p.name for p in (REPO / "ansible/roles").rglob("templates/*.j2") if pattern.search(p.read_text())
    }
    assert secret_templates == {"restic.env.j2", "health.env.j2", "agent.env.j2"}
    checked = 0
    for path in _task_files():
        for task, no_log in _leaf_tasks(path):
            text = yaml.safe_dump(task)
            src = str((task.get("ansible.builtin.template") or task.get("template") or {}).get("src", ""))
            if not (pattern.search(text) or src.rsplit("/", 1)[-1] in secret_templates):
                continue
            if "mc_config" in task:
                continue
            assert_task = task.get("ansible.builtin.assert")
            if assert_task and assert_task.get("quiet") is True:
                assert all(re.fullmatch(r"\w+ \| length > 0", c) for c in assert_task["that"]), task["name"]
                continue
            checked += 1
            assert no_log, f"{path.relative_to(REPO)}: {task.get('name')}"
    # restic.env, health.env, notify.env, agent.env; v1.1: the mirror downloads (server jar, plugins, restic)
    assert checked == 7
    assert (
        '"settings": {"type": "dict", "required": True, "no_log": True}'
        in (REPO / "ansible/library/mc_config.py").read_text()
    )


def test_no_log_walker_finds_nested_and_aliased_secrets(tmp_path) -> None:
    # Self-test of the walker: a secret written inside block/rescue/always is found, and block-level
    # no_log covers its children.
    tasks = [
        {
            "name": "outer",
            "block": [{"name": "in block", "copy": {"content": "{{ x }}"}}],
            "rescue": [{"name": "in rescue", "copy": {"content": "{{ y }}"}}],
            "always": [{"name": "inner", "no_log": True, "block": [{"name": "deep", "copy": {}}]}],
        },
    ]
    flat = {t["name"]: nl for t, nl in _walk_tasks(tasks)}
    assert flat == {"in block": False, "in rescue": False, "deep": True}
    assert {
        t["name"]: nl for t, nl in _walk_tasks([{"name": "b", "no_log": True, "block": tasks}])
    } == dict.fromkeys(flat, True)


def test_no_home_hosting_leftovers() -> None:
    # The DDNS/home profile was removed: nothing may reference it again.
    paths = [
        *REPO.glob("terraform/*.tf"),
        *(REPO / "ansible").rglob("*.yml"),
        *(REPO / "scripts").glob("*.py"),
    ]
    for p in paths:
        assert "ddns" not in p.read_text().lower(), p


def test_ci_uses_federated_identity_not_oauth_secret() -> None:
    job = _wf(REUSABLE)["jobs"]["deploy"]
    assert job["permissions"]["id-token"] == "write"
    ts = next(s for s in job["steps"] if "tailscale/github-action" in s.get("uses", ""))
    assert set(ts["with"]) >= {"oauth-client-id", "audience", "tags"}
    assert "oauth-secret" not in ts["with"]
    tf = (REPO / "terraform/tailscale.tf").read_text()
    assert 'resource "tailscale_federated_identity" "ci"' in tf
    assert "tailscale_oauth_client" not in tf
    # Only the config repo's main branch, in the `sub` format GitHub issues for it (immutable ids or names
    # only, server.yml github.oidc_subject); never a wildcard in the owner/repo part (tests/test_ci_oidc.py
    # evaluates both formats against mcctl's builder).
    assert re.search(r"\n  subject\s*=\s*local\.ci_oidc_subject\n", tf)
    assert 'ci_oidc_subject = "repo:${local.ci_oidc_repo}:${local.ci_oidc_context}"' in tf
    # security review M2: in environment `deploy` (main only) the sub names the environment, not the ref
    assert 'ci_oidc_context = var.github_environments ? "environment:deploy" : "ref:refs/heads/main"' in tf
    block = tf[tf.index("  ci_oidc_repo = (") : tf.index("  ci_oidc_subject =")]
    assert "*" not in block, "federated identity must only trust the config repo's main branch"
    # ... running the product's reusable workflow (OIDC claim of the called workflow).
    assert 'job_workflow_ref = "${var.product_repository}/.github/workflows/server.yml@*"' in tf, (
        "federated identity must require the product's reusable workflow"
    )


def test_keep_within_outlasts_r2_lock() -> None:
    # forget must never try to delete a snapshot file the R2 bucket lock still protects.
    sys.path.insert(0, str(REPO / "scripts"))
    import backup

    tf = (REPO / "terraform/variables.tf").read_text()
    m = re.search(r'variable "r2_lock_days" \{.*?default\s*=\s*(\d+)', tf, re.S)
    assert m
    lock = int(m.group(1))
    from mcctl import schema

    assert lock == schema.Backups().lock_days
    assert lock + 1 == backup.KEEP_WITHIN_DAYS
    assert f"{backup.KEEP_WITHIN_DAYS}d" in backup.RETENTION
    # The deploy hands the value to backup.py through restic.env.
    env = (REPO / "ansible/roles/backups/templates/restic.env.j2").read_text()
    assert "KEEP_WITHIN_DAYS={{ mc_backup_keep_within_days }}" in env


def test_backup_timers_and_resize_hint_use_server_yml_vars() -> None:
    templates = REPO / "ansible/roles"
    assert (
        "OnCalendar={{ mc_backup_schedule }}"
        in (templates / "backups/templates/mc-backup.timer.j2").read_text()
    )
    assert (
        "OnCalendar={{ mc_prune_schedule }}"
        in (templates / "backups/templates/mc-prune.timer.j2").read_text()
    )
    assert (
        "RESIZE_HINT={{ mc_resize_hint }}" in (templates / "monitoring/templates/health.env.j2").read_text()
    )


def test_backend_has_no_deployment_values() -> None:
    # P3: the bucket and key come from server.yml state.* (rendered state.tfbackend, -backend-config).
    backend = (REPO / "terraform/backend.tf").read_text()
    assert 'backend "s3"' in backend
    assert not re.search(r"^\s*(bucket|key)\s*=", backend, re.M)
    assert not (REPO / "terraform/terraform.tfvars").exists()
    assert not list((REPO / "terraform").glob("*.auto.tfvars*"))


def test_backups_exclude_restore_marker() -> None:
    sys.path.insert(0, str(REPO / "scripts"))
    import backup
    import restore

    assert restore.MARKER in backup.EXCLUDE_PATTERNS


def test_no_snapshot_fails_when_world_expected() -> None:
    from mcctl import schema

    assert schema.World().expected is False  # `mcctl init` flips it once the world is backed up
    tasks = yaml.safe_load((REPO / "ansible/roles/backups/tasks/main.yml").read_text())
    names = [t["name"] for t in tasks]
    restore_i = names.index("Restore latest snapshot if there is no world yet")
    guard = tasks[restore_i + 1]
    assert guard["ansible.builtin.assert"]["that"] == (
        "not (mc_world_expected | bool and 'NO_SNAPSHOT' in backups_restore.stdout_lines)"
    )
    # restore.py prints the token on a line of its own
    assert 'print("NO_SNAPSHOT")' in (REPO / "scripts/restore.py").read_text()


def test_seed_and_template_configs_match_reference_locks() -> None:
    # The full example and the template must agree with the product's reference locks and backup.py.
    sys.path.insert(0, str(REPO / "scripts"))
    import backup

    from mcctl import configrepo, schema

    for path in (REPO / "examples/full/server.yml", REPO / "template/server.yml"):
        cfg = schema.load_server(yaml.safe_load(path.read_text()))
        assert configrepo.check_locks(cfg, SERVER_LOCK, PLUGINS_LOCK) == [], path
        assert cfg.backups.lock_days + 1 == backup.KEEP_WITHIN_DAYS


def _pins(path) -> dict[str, str]:
    """name -> version of `name==version` lines (normalised names), following `-r` includes."""
    out: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("-r "):
            out |= _pins(path.parent / line[3:].strip())
        elif m := re.match(r"([A-Za-z0-9_.-]+)==([^\s;\\]+)", line):
            out[re.sub(r"[-_.]+", "-", m.group(1)).lower()] = m.group(2)
    return out


def test_python_requirements_are_resolved_and_hash_pinned() -> None:
    req = REPO / "requirements"
    sets = ("validate", "deploy", "dev")
    for name in sets:
        text = (req / f"{name}.txt").read_text()
        blocks = re.split(r"\n(?=[A-Za-z])", text.split("\n", 6)[-1])
        for block in blocks:
            if block.strip() and not block.startswith("#"):
                assert re.match(r"[a-z0-9-]+==\S+ \\", block), block[:80]
                assert "--hash=sha256:" in block, block[:80]
        compiled, direct = _pins(req / f"{name}.txt"), _pins(req / f"{name}.in")
        assert all(compiled[k] == v for k, v in direct.items()), name
    v, d, dev = (_pins(req / f"{n}.txt") for n in sets)
    # Supersets agree on versions, so a laptop, CI and the cached product venv run the same packages.
    assert v.items() <= d.items() <= dev.items()
    pyproject = (REPO / "pyproject.toml").read_text()
    deps = ", ".join(  # questionary: the home screen's menus (v1.3); tzdata: zoneinfo on Windows
        f'"{name}=={v[name.lower()]}"' for name in ("PyYAML", "questionary", "rich", "tzdata")
    )
    assert f"dependencies = [{deps}]" in pyproject


def test_terraform_cli_pin_matches_ci_and_versions_tf() -> None:
    from mcctl import tf

    ci = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    setup = next(s for s in ci["jobs"]["lint"]["steps"] if "setup-terraform" in s.get("uses", ""))
    assert setup["with"]["terraform_version"] == tf.TERRAFORM_VERSION
    assert f'required_version = "~> {tf.TERRAFORM_VERSION}"' in (REPO / "terraform/versions.tf").read_text()


def test_mcctl_reads_the_outputs_terraform_defines() -> None:
    from mcctl import bootstrap

    outputs = set(re.findall(r'^output "(\w+)"', (REPO / "terraform/outputs.tf").read_text(), re.M))
    assert bootstrap.KEY_OUTPUT in outputs
    resource, name = bootstrap.KEY_RESOURCE.split(".")
    assert f'resource "{resource}" "{name}"' in (REPO / "terraform/tailscale.tf").read_text()


def test_every_mcctl_package_is_installed() -> None:
    pyproject = (REPO / "pyproject.toml").read_text()
    for init in sorted((REPO / "mcctl").rglob("__init__.py")):
        pkg = ".".join(init.parent.relative_to(REPO).parts)
        assert f'"{pkg}"' in pyproject, f"{pkg} missing from [tool.setuptools] packages"


def test_python_floors_controller_312_server_311() -> None:
    """mcctl (the controller) needs 3.12 (pinned ansible-core); the on-server scripts stay 3.11, stdlib."""
    import mcctl

    pyproject = (REPO / "pyproject.toml").read_text()
    want = ".".join(map(str, mcctl.MIN_PYTHON))
    assert re.search(rf'^requires-python = ">={re.escape(want)}"', pyproject, re.M)
    assert mcctl.MIN_PYTHON == (3, 12)
    for name in SERVER_SCRIPTS:
        ast.parse((REPO / "scripts" / name).read_text(), feature_version=(3, 11))


def test_gitleaks_pin_is_the_same_in_product_and_config_ci() -> None:
    """P7: gitleaks runs in the product's CI (history) and in every config repo's validate job (files)."""
    pins = []
    for wf in (REPO / ".github/workflows/ci.yml", REUSABLE):
        text = wf.read_text()
        version = re.findall(r"GITLEAKS_VERSION: (\d+\.\d+\.\d+)\b", text)
        sha = re.findall(r"GITLEAKS_SHA256: ([0-9a-f]{64})\b", text)
        assert len(version) == len(sha) == 1, wf.name
        assert "sha256sum -c -" in text and "--redact" in text, wf.name
        pins.append((version[0], sha[0]))
    assert pins[0] == pins[1]
    assert 'gitleaks" git --redact' in (REPO / ".github/workflows/ci.yml").read_text()
    reusable = REUSABLE.read_text()
    # P7 review: a config repo's whole history (fetch-depth 0), not just its files.
    assert '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --verbose "$GITHUB_WORKSPACE/config"' in reusable
    validate = _wf(REUSABLE)["jobs"]["validate"]["steps"][0]
    assert validate["with"]["path"] == "config" and validate["with"]["fetch-depth"] == 0


@pytest.mark.parametrize("plugin", ["mc-telemetry", "mc-brand"])
def test_p13_plugin_jar_pin_jdk_and_release_agree(plugin: str) -> None:
    """The committed in-house jars (P13 mc-telemetry, P17 mc-brand) are the pinned ones (group_vars, the name in
    build.yml `pin`), javac --release = java_major, and CI rebuilds them byte for byte (ci.yml job plugin:
    scripts/build_plugin.py check, every plugin in build_plugin.PLUGINS)."""
    import hashlib

    build = yaml.safe_load((REPO / f"plugin/{plugin}/build.yml").read_text())
    assert build["name"] == plugin and build["pin"] == f"{plugin.replace('-', '_')}_jar_sha256"
    jar = (REPO / build["jar"]).read_bytes()
    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    assert hashlib.sha256(jar).hexdigest() == gv[build["pin"]]
    assert plugin in (REPO / "scripts/build_plugin.py").read_text().split("PLUGINS = ", 1)[1].split("\n")[0]
    lock = yaml.safe_load((REPO / "template/server.lock.yml").read_text())
    assert int(build["release"]) == int(lock["java_major"]) == int(build["jdk"]["version"].split(".")[0])
    assert gv["temurin_version"].startswith(
        build["jdk"]["version"].split("+")[0]
    )  # same JDK update as the server
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    assert "python scripts/build_plugin.py check" in ci
    plugin_yml = (REPO / f"plugin/{plugin}/src/main/resources/plugin.yml").read_text()
    assert f"version: {build['version']}" in plugin_yml
    assert f"name: {plugin}" in plugin_yml


def _java_method(src: str, signature: str) -> str:
    start = src.index(signature)
    depth, i = 0, src.index("{", start)
    for j in range(i, len(src)):
        depth += {"{": 1, "}": -1}.get(src[j], 0)
        if depth == 0:
            return src[start : j + 1]
    raise AssertionError(signature)


def test_p13_plugin_collection_survives_a_throwing_getter() -> None:
    """P13 review L3 (no JVM test harness: the jar is javac-only, checked on the real Purpur in e2e): a getter
    that throws during a window's collection drops that window and returns its snapshot to the pool (two
    instances circulate: a leak would end telemetry), the slot index moves before the reads (no nulled slot read
    again), and errors are logged rate-limited, not once per tick."""
    src = (
        REPO / "plugin/mc-telemetry/src/main/java/io/github/edvardk/mctelemetry/TelemetryPlugin.java"
    ).read_text()
    step = _java_method(src, "private void step(long t0)")
    assert "catch (RuntimeException e)" in step and "fail(s, e);" in step
    world = step[step.index("worldBuf[worldIdx] = null;") :]
    assert world.index("worldIdx++;") < world.index("w.getName()")
    start = _java_method(src, "private void startWindow(long now)")
    assert "catch (RuntimeException e)" in start and "fail(s, e);" in start
    fail = _java_method(src, "private void fail(Snapshot s, RuntimeException e)")
    assert "abandon(s);" in fail and "current = null;" in fail and "ERROR_LOG_MS" in fail
    assert "writer.free.offer(s);" in _java_method(src, "private void abandon(Snapshot s)")
