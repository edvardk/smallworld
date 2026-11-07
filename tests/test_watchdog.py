"""P6 watchdog: the Worker's own tests (node --test) and the contracts between the Worker, Terraform, the
server-side scripts and Ansible."""

import json
import re
import shutil
import subprocess

import health
import notify
import publish_fresh
import pytest
import yaml
from conftest import REPO

from mcctl import render, secrets

WORKER = REPO / "watchdog" / "worker.js"
TF = (REPO / "terraform" / "watchdog.tf").read_text()
JS = WORKER.read_text()
NODE_TEST = [
    "node",
    "--import",
    "./watchdog/test/register.mjs",
    "--test",
    "watchdog/test/worker.test.mjs",
    "watchdog/test/monitoring.test.mjs",
    "watchdog/test/panel.test.mjs",
    "watchdog/test/actions.test.mjs",
    "watchdog/test/experience.test.mjs",
    "watchdog/test/discord.test.mjs",
    "watchdog/test/client.test.mjs",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node isn't installed (CI runs it in the lint job)")
def test_worker_unit_tests_pass() -> None:
    proc = subprocess.run(NODE_TEST, cwd=REPO, capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stdout[-3000:] + proc.stderr[-3000:]
    assert re.search(r"^# fail 0$", proc.stdout, re.M)


def test_ci_runs_the_worker_tests() -> None:
    ci = (REPO / ".github/workflows/ci.yml").read_text()
    assert " ".join(NODE_TEST) in " ".join(ci.split())


def test_worker_has_no_dependencies_and_no_build_step() -> None:
    imports = re.findall(r'^import .* from "([^"]+)";', JS, re.M)
    assert imports == ["cloudflare:sockets"]
    assert "require(" not in JS
    assert not (REPO / "watchdog" / "package.json").exists()


def test_terraform_uploads_the_worker_as_an_es_module() -> None:
    assert 'main_module        = "worker.js"' in TF
    assert 'content_file       = "${path.module}/../watchdog/worker.js"' in TF
    assert 'content_sha256     = filesha256("${path.module}/../watchdog/worker.js")' in TF
    # bindings the Worker reads
    for name in ("DB", "HEARTBEAT_TOKEN", "TARGET", "DISCORD_WEBHOOK"):
        assert f'name = "{name}"' in TF
        assert f"env.{name}" in JS
    assert 'cron = "*/5 * * * *"' in TF
    assert "enabled          = false" in TF  # workers.dev off


def test_checks_agree_between_worker_terraform_and_notify() -> None:
    js_checks = re.search(r'const CHECKS = \["(\w+)", "(\w+)"\];', JS).groups()
    assert tuple(js_checks) == notify.CHECKS
    for check in notify.CHECKS:
        assert re.search(rf"^\s+{check}\s+= \{{\s*period_minutes\s+= ", TF, re.M)
    assert f"const MAX_MAINTENANCE_MINUTES = {notify.MAX_MAINTENANCE_MINUTES};" in JS
    assert re.search(r"/\^\\/hb\\/\(health\|backup\)\(\?:\\/\(start\|fail\)\)\?\$/", JS)
    assert tuple(s for s in notify.STATUSES if s) == ("start", "fail")


def test_watchdog_hostname_agrees_with_render() -> None:
    assert f'watchdog_hostname = "{render.WATCHDOG_LABEL}.${{local.fqdn}}"' in TF


def test_ipv6_literals_are_bracketed() -> None:
    assert '"[${var.server_ipv6}]:25565"' in TF
    assert '"${var.server_ipv4}:25565"' in TF


def test_probe_by_name_is_the_default_with_single_family_dns_only_records() -> None:
    """First real setup: connect() to the IPv4 literal failed ("proxy request failed, cannot connect to the
    specified address"); the watchdog probes v4.<fqdn> (A only) and v6.<fqdn> (AAAA only) by default."""
    variables = (REPO / "terraform" / "variables.tf").read_text()
    block = variables.split('variable "watchdog_probe_by_name"', 1)[1].split("\n}\n", 1)[0]
    assert re.search(r"^\s*default\s*=\s*true$", block, re.M), block
    assert '"v4.${local.fqdn}:25565"' in TF and '"v6.${local.fqdn}:25565"' in TF
    for name, rtype, var in (("probe_v4", "A", "server_ipv4"), ("probe_v6", "AAAA", "server_ipv6")):
        res = TF.split(f'resource "cloudflare_dns_record" "{name}"', 1)[1].split("\n}\n", 1)[0]
        assert f'type    = "{rtype}"' in res and f"content = var.{var}" in res and "proxied = false" in res


def test_probe_rate_stays_under_the_firewall_limit() -> None:
    """Cloudflare's egress address varies, but even one source doing every attempt of a run stays far below the
    per-source new-connection limit (rate + burst) and the concurrency cap."""
    fw = yaml.safe_load((REPO / "ansible/roles/firewall/defaults/main.yml").read_text())
    m = re.search(r"probe = \{ attempts = (\d+), retry_seconds = (\d+), timeout_seconds = (\d+)", TF)
    assert m, "watchdog_config.probe not found"
    attempts, retry_s, timeout_s = map(int, m.groups())
    per_run = attempts * 2  # both families may share one egress address
    assert per_run <= fw["firewall_mc_new_burst"] // 2 and per_run <= fw["firewall_mc_new_per_minute"] // 2
    assert 2 <= fw["firewall_mc_max_concurrent"]  # at most one open probe per family at a time
    assert attempts * (retry_s + timeout_s) < 5 * 60  # one run finishes before the next cron


def test_heartbeat_token_is_a_required_deploy_secret() -> None:
    assert secrets.Secret("HEARTBEAT_TOKEN", required=True) in secrets.SECRETS
    assert not any("HEALTHCHECKS" in s.name for s in secrets.SECRETS)
    outputs = (REPO / "terraform/outputs.tf").read_text()
    assert 'output "heartbeat_token"' in outputs and "random_password.heartbeat.result" in outputs


def test_no_healthchecks_left() -> None:
    hits = []
    history = publish_fresh.parse_excludes((REPO / publish_fresh.EXCLUDE_FILE).read_text())  # never published
    for path in REPO.rglob("*"):
        rel = path.relative_to(REPO).as_posix()
        if (
            not path.is_file()
            or rel.startswith(
                (
                    ".git/",
                    ".claude/",
                    ".venv/",
                    "terraform/.terraform",
                    ".pytest_cache/",
                    ".ruff_cache/",
                )
            )
            or "__pycache__" in rel
            or rel in ("CHANGELOG.md", "tests/test_watchdog.py")
            or publish_fresh.is_excluded(rel, history)
        ):
            continue
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        if re.search(r"healthchecks|HC_PING|HC_BACKUP", text, re.I):
            hits.append(rel)
    assert hits == []


def test_markers_agree_between_scripts_and_ansible() -> None:
    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    assert str(notify.MAINTENANCE_FILE.parent) == gv["mc_state_dir"]
    assert str(health.ADMIN_STOPPED.parent) == gv["mc_state_dir"]
    rule = (REPO / "ansible/roles/base/templates/50-mc-ops.rules.j2").read_text()
    assert f'"{health.UNIT}": ["restart", "kill"' in rule
    assert 'subject.user != "{{ mc_ops_user }}"' in rule and gv["mc_ops_user"] == "mcops"
    unit = (REPO / "ansible/roles/monitoring/templates/mc-health.service.j2").read_text()
    assert "User={{ mc_ops_user }}" in unit


def test_deploy_restart_opens_and_closes_a_maintenance_window() -> None:
    # v1.3: the restart itself is the "Restart now" block (the other branch defers it to maintenance.window)
    tasks = next(
        t["block"]
        for t in yaml.safe_load((REPO / "ansible/roles/minecraft/tasks/restart.yml").read_text())
        if t.get("name") == "Restart now"
    )
    cmds = [t.get("ansible.builtin.command", "") for t in tasks]
    opens = [
        i for i, c in enumerate(cmds) if "notify.py maintenance {{ mc_deploy_maintenance_minutes }}" in c
    ]
    closes = [i for i, c in enumerate(cmds) if "notify.py maintenance 0" in c]
    stop = next(
        i
        for i, t in enumerate(tasks)
        if t.get("ansible.builtin.systemd_service", {}).get("state") == "stopped"
    )
    assert opens and closes and opens[0] < stop < closes[0] == len(tasks) - 1
    assert all("mc-run -{{ mc_etc_dir }}/notify.env" in cmds[i] for i in opens + closes)


# --- Durable Object migration tag (10079: "Actor migration tag precondition failed") ---------------------------

_MIGRATION_LOCALS = re.search(r'\nlocals \{\n  watchdog_do_tag = "v1"\n.*?\n\}\n', TF, re.S)


def test_worker_sends_its_durable_object_migration_only_while_the_tag_is_missing() -> None:
    """Provider 5.28.1 uploads `migrations` as configured on every create and update, and the API refuses a
    migration whose old_tag isn't the live tag. A fixed { new_tag = "v1" } therefore broke every upload after
    the first; the configured value has to follow the live tag."""
    assert _MIGRATION_LOCALS, "watchdog.tf: the watchdog_do_tag locals block moved"
    assert "  migrations = local.watchdog_migrations\n" in TF
    assert re.search(
        r'data "cloudflare_workers_scripts" "account" \{\n  count      = local.cloudflare_mode', TF
    )
    # ignore_changes would keep a planned value the provider doesn't return ("inconsistent result after apply")
    worker = re.search(r'\nresource "cloudflare_workers_script" "watchdog" \{\n.*?\n\}\n', TF, re.S)
    assert worker and "ignore_changes =" not in worker.group(0)
    assert (
        'contains(["", local.watchdog_do_tag], local.watchdog_live_do_tag)' in TF
    )  # unknown tags fail the plan


def _eval_migrations(
    tmp_path, scripts: list[dict[str, str | None]] | None, cloudflare_mode: bool = True
) -> object:
    """Evaluate watchdog.tf's migration locals verbatim with `terraform console` (no providers, no network)."""
    assert _MIGRATION_LOCALS
    block = _MIGRATION_LOCALS.group(0).replace(
        "data.cloudflare_workers_scripts.account[0].result", "var.scripts"
    )
    (tmp_path / "main.tf").write_text(
        'variable "scripts" {\n  type = list(object({ id = string, migration_tag = string }))\n}\n'
        f"locals {{\n  cloudflare_mode = {str(cloudflare_mode).lower()}\n"
        '  watchdog_name = "lh-watchdog"\n}\n' + block
    )
    (tmp_path / "t.auto.tfvars.json").write_text(json.dumps({"scripts": scripts or []}))
    proc = subprocess.run(
        ["terraform", "console"],
        cwd=tmp_path,
        input="jsonencode(local.watchdog_migrations)\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(json.loads(proc.stdout.strip()))


@pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform isn't installed (CI's terraform job)"
)
def test_migration_locals_follow_the_live_tag(tmp_path) -> None:
    fresh = {"new_tag": "v1", "new_sqlite_classes": ["Control"]}
    assert _eval_migrations(tmp_path, []) == fresh  # no Worker yet
    assert _eval_migrations(tmp_path, [{"id": "other", "migration_tag": "v1"}]) == fresh
    assert _eval_migrations(tmp_path, [{"id": "lh-watchdog", "migration_tag": None}]) == fresh
    assert _eval_migrations(tmp_path, [{"id": "lh-watchdog", "migration_tag": ""}]) == fresh
    assert (
        _eval_migrations(tmp_path, [{"id": "lh-watchdog", "migration_tag": "v1"}]) is None
    )  # live: never re-sent
    assert _eval_migrations(tmp_path, [], cloudflare_mode=False) is None
