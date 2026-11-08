"""P7 hardening (design §7): the static half of the acceptance. The e2e-mock run checks the same on a live
systemd (tests/e2e/hardening_check.py, tests/e2e/firewall_check.py)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

import jinja2
import pytest
import yaml
from conftest import REPO

GROUP_VARS = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
SANDBOX = GROUP_VARS["mc_systemd_sandbox"]
UNITS = {
    "mc-backup.service": (REPO / "ansible/roles/backups/templates/mc-backup.service.j2", 2.5),
    "mc-prune.service": (REPO / "ansible/roles/backups/templates/mc-prune.service.j2", 2.5),
    "mc-health.service": (REPO / "ansible/roles/monitoring/templates/mc-health.service.j2", 2.5),
    "mc-restore-apply.service": (REPO / "ansible/roles/backups/templates/mc-restore-apply.service.j2", 2.5),
    "mc-agent.service": (REPO / "ansible/roles/agent/templates/mc-agent.service.j2", 2.5),
    # P10: the panel's plugin switch (as minecraft) and restore (as mcops; a template, scored as one instance)
    "mc-plugins-apply.service": (REPO / "ansible/roles/plugins/templates/mc-plugins-apply.service.j2", 2.5),
    "mc-restore@abcdef12.service": (REPO / "ansible/roles/backups/templates/mc-restore@.service.j2", 2.5),
    # v1.2: the world library (as mcops, restic credentials) and its folder swap (as minecraft)
    "mc-world.service": (REPO / "ansible/roles/backups/templates/mc-world.service.j2", 2.5),
    "mc-world-apply.service": (REPO / "ansible/roles/backups/templates/mc-world-apply.service.j2", 2.5),
    # P11: the reboot gate (as mcops)
    "mc-reboot-gate.service": (REPO / "ansible/roles/autoreboot/templates/mc-reboot-gate.service.j2", 2.5),
    # v1.2: the weekly backup test (as mcops; was the config repo's Monday Actions job)
    "mc-backup-check.service": (REPO / "ansible/roles/backups/templates/mc-backup-check.service.j2", 2.5),
    # v1.3: a restart a deploy left for maintenance.window (as mcops; polkit: minecraft.service restart, as self-heal)
    "mc-restart-window.service": (
        REPO / "ansible/roles/minecraft/templates/mc-restart-window.service.j2",
        2.5,
    ),
    "minecraft.service": (REPO / "ansible/roles/minecraft/templates/minecraft.service.j2", 4.5),
}
HARDENING = yaml.safe_load((REPO / "ansible/roles/hardening/defaults/main.yml").read_text())
FIREWALL = yaml.safe_load((REPO / "ansible/roles/firewall/defaults/main.yml").read_text())
BACKUPS = yaml.safe_load((REPO / "ansible/roles/backups/defaults/main.yml").read_text())


def render(template: Path, **extra: object) -> str:
    env = jinja2.Environment(undefined=jinja2.StrictUndefined, keep_trailing_newline=True)  # noqa: S701 - not HTML
    variables = {
        **GROUP_VARS,
        "ansible_managed": "Managed by Ansible",
        "backups_cache_dir": "/var/cache/mc-restic",
        "mc_backup_schedule": "hourly",
        "mc_prune_schedule": "Sun 03:30",
        "server_lock": {"server": {"project": "purpur", "version": "26.3", "build": 1}},
        "mc_java_bin": "/usr/lib/jvm/temurin-25-jre-amd64/bin/java",
        "mc_heap_mb": 1024,
        "mc_jvm_flags": ["-XX:+UseG1GC"],
        **FIREWALL,
        "backups_inaccessible_paths": BACKUPS["backups_inaccessible_paths"],
        "backups_check_max_age_hours": BACKUPS["backups_check_max_age_hours"],
        "plugins_disabled_file": "/var/lib/minecraft/plugins-disabled.json",
        "mc_reboot_window": "04:30-06:00",
        "timezone": "Europe/Dublin",
        "mc_maintenance_window": "04:00-07:00",  # v1.3
        "mc_maintenance_timezone": "Europe/Dublin",
        "mc_maintenance_force_minutes": 30,
        "mc_helper_cpus": "",  # P13
        "mc_jvm_cpus": "",  # P16 (performance.cpu_affinity off)
        "mc_experience_enabled": True,
        "mc_client_enabled": False,  # v1.1
        "mc_panel_mode": "cloudflare",
        "mc_agent_implementation": "python",  # v1.4 (the rust unit: test_the_rust_agent_unit_*)
        "mc_watchdog_url": "https://hb.mc.example.com",
        **extra,
    }
    # Resolve the group_vars' own templating (mc_bin_dir: "{{ mc_root }}/bin", ...) once.
    for key, value in list(variables.items()):
        if isinstance(value, str) and "{{" in value:
            variables[key] = env.from_string(value).render(**variables)
    return env.from_string(template.read_text()).render(**variables)


@pytest.mark.parametrize("unit", sorted(UNITS))
def test_every_unit_gets_the_sandbox(unit: str) -> None:
    text = render(UNITS[unit][0])
    for directive in SANDBOX:
        assert directive in text.splitlines(), f"{unit}: {directive}"
    mdwe = "MemoryDenyWriteExecute=true" in text
    assert mdwe is (unit != "minecraft.service"), "MDWE everywhere except the JVM (its JIT)"
    # Privilege separation (P7 review): only the JVM and the credential-free apply step run as minecraft.
    user = (
        "minecraft"
        if unit
        in (
            "minecraft.service",
            "mc-restore-apply.service",
            "mc-plugins-apply.service",
            "mc-world-apply.service",
        )
        else "mcops"
    )
    assert f"User={user}" in text
    if user == "mcops":
        assert "SupplementaryGroups=minecraft" in text and "EnvironmentFile=" in text
    else:
        assert "EnvironmentFile=" not in text


FENCE_HONOURING = ("minecraft.service", "mc-backup.service", "mc-prune.service", "mc-agent.service",
                   "mc-health.service", "mc-reboot-gate.service", "mc-world.service",
                   "mc-backup-check.service", "mc-restart-window.service")  # fmt: skip


@pytest.mark.parametrize("unit", FENCE_HONOURING)
def test_units_never_start_on_a_fenced_box(unit: str) -> None:
    """P12 review (M1): boot, a timer, the reboot gate or a stray `systemctl start` can't run them while
    /var/lib/minecraft/fenced (mcctl migrate) exists."""
    text = render(UNITS[unit][0])
    unit_section = text.split("[Service]")[0]
    assert "ConditionPathExists=!/var/lib/minecraft/fenced" in unit_section.splitlines()


def test_deploy_checks_the_new_restic_password_before_writing_restic_env() -> None:
    """P12 review (H2): restic_rekey.py runs (password on stdin, no_log) before restic.env is rewritten, and a
    failure stops the deploy first; the Minecraft role backs the world up before a new version's jar."""
    tasks = yaml.safe_load((REPO / "ansible/roles/backups/tasks/main.yml").read_text())
    names = [t["name"] for t in tasks]
    rekey = next(t for t in tasks if "restic_rekey.py" in str(t.get("ansible.builtin.command", "")))
    assert rekey["no_log"] is True and "stdin" in rekey["ansible.builtin.command"]
    assert all("password" not in a.lower() for a in rekey["ansible.builtin.command"]["argv"])
    write = names.index("Write restic environment file")
    assert names.index(rekey["name"]) < names.index(
        "Stop unless the new restic password works (restic.env untouched)"
    )
    assert names.index("Stop unless the new restic password works (restic.env untouched)") < write
    mc = yaml.safe_load((REPO / "ansible/roles/minecraft/tasks/main.yml").read_text())
    mc_names = [t["name"] for t in mc]
    assert mc_names.index("Pre-upgrade backup before the new jar touches the world") < mc_names.index(
        "Download pinned server jar (sha256-verified)"
    )


def test_no_root_helper_unit_for_thp() -> None:
    base = (REPO / "ansible/roles/base/tasks/main.yml").read_text()
    assert "/etc/tmpfiles.d/mc-thp.conf" in base
    # P7 review: hosts set up before P7 get the old helper unit stopped, disabled and removed.
    tasks = yaml.safe_load((REPO / "ansible/roles/base/tasks/main.yml").read_text())
    old = [t for t in tasks if "thp-madvise.service" in str(t)]
    assert {t["name"] for t in old} == {
        "Is the old THP helper unit installed?",
        "Stop and disable the old THP helper unit",
        "Remove the old THP helper unit",
    }
    for t in old:
        svc = t.get("ansible.builtin.systemd_service")
        if svc:
            assert svc["enabled"] is False and svc["state"] == "stopped"
        f = t.get("ansible.builtin.file")
        if f:
            assert f["state"] == "absent"
    assert "w /sys/kernel/mm/transparent_hugepage/enabled - - - - madvise" in base


def test_mc_run_transient_units_are_sandboxed() -> None:
    base = (REPO / "ansible/roles/base/tasks/main.yml").read_text()
    helper = base.split("Install `mc-run` helper")[1].split("- name:")[0]
    for prop in ("NoNewPrivileges=yes", "ProtectSystem=full", "ProtectHome=yes", "PrivateDevices=yes",
                 "RestrictNamespaces=yes", "CapabilityBoundingSet=", "LockPersonality=yes"):  # fmt: skip
        assert f"-p {prop}" in helper, prop


@pytest.mark.skipif(shutil.which("sh") is None or shutil.which("date") is None, reason="needs sh and date")
def test_mc_run_names_only_the_agent_cli_unit(tmp_path: Path) -> None:
    """v1.4: `mc-run ENV mc_agent.py cli ...` (mcctl's actions) runs in a unit named mc-agent-cli-<pid>-<ns>, so its
    audit entries carry a product unit in mc-agent's D1 rows; every other command keeps systemd-run's anonymous
    unit (nothing new is shipped)."""
    base = (REPO / "ansible/roles/base/tasks/main.yml").read_text()
    content = base.split("Install `mc-run` helper")[1].split("content: |\n", 1)[1].split("    owner: root")[0]
    script = "\n".join(line[6:] for line in content.splitlines())
    script = script.replace("{{ mc_ops_user }}", "mcops").replace("{{ mc_bin_dir }}", "/opt/minecraft/bin")
    assert "{{" not in script
    (tmp_path / "mc-run").write_text(script)
    fake = tmp_path / "bin" / "systemd-run"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\nfor a in "$@"; do printf \'%s\\n\' "$a"; done\n')
    fake.chmod(0o755)
    env = {"PATH": f"{fake.parent}:/usr/bin:/bin"}

    def argv(*args: str) -> list[str]:
        out = subprocess.run(["sh", str(tmp_path / "mc-run"), "-/etc/minecraft/notify.env", *args],
                             capture_output=True, text=True, env=env, check=True).stdout  # fmt: skip
        return out.splitlines()

    cli = argv("/opt/minecraft/bin/mc_agent.py", "cli", "mcctl:x", "status")
    assert re.fullmatch(r"--unit=mc-agent-cli-\d+-\d{19}", cli[0]), cli[0]
    import mc_agent

    assert mc_agent.D1_UNITS.match(cli[0].removeprefix("--unit=") + ".service")  # its rows are the product's
    assert cli[-4:] == ["/opt/minecraft/bin/mc_agent.py", "cli", "mcctl:x", "status"]
    assert argv("/opt/minecraft/bin/mc_agent.py", "cli")[0] != cli[0]  # a new name every run
    # v1.4: mcctl's entry point (the symlink) and the Rust binary itself are the same cli
    for entry in ("/opt/minecraft/bin/mc-agent", "/opt/minecraft/bin/smallworld-agent"):
        got = argv(entry, "cli", "mcctl:x", "status")
        assert re.fullmatch(r"--unit=mc-agent-cli-\d+-\d{19}", got[0]) and got[-4:] == [
            entry,
            "cli",
            "mcctl:x",
            "status",
        ]
    for other in (["/opt/minecraft/bin/notify.py", "maintenance", "0", "deploy"],
                  ["/opt/minecraft/bin/mc_agent.py", "status"], ["/usr/bin/python3", "-c", "cli"],
                  ["/opt/minecraft/bin/mc-agent", "run"], ["/opt/minecraft/bin/mc-agent-x", "cli"],
                  ["/opt/minecraft/bin/smallworld-rcon", "countdown", "60"]):  # fmt: skip
        got = argv(*other)
        assert not any(a.startswith("--unit") for a in got), got
        assert got[0] == "--quiet" and got[-len(other) :] == other


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="systemd-analyze not installed")
@pytest.mark.parametrize(
    ("unit", "impl"), [(u, "python") for u in sorted(UNITS)] + [("mc-agent.service", "rust")]
)
def test_systemd_analyze_security_offline_gate(unit: str, impl: str, tmp_path: Path) -> None:
    """Exposure as systemd-analyze scores the unit file (the e2e run repeats it on the live unit); v1.4: the
    agent's unit in both implementations."""
    template, limit = UNITS[unit]
    path = tmp_path / unit
    path.write_text(render(template, mc_agent_implementation=impl))
    proc = subprocess.run(
        [
            "systemd-analyze",
            "security",
            "--offline=yes",
            f"--threshold={int(limit * 10)}",
            "--no-pager",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    level = re.search(r"Overall exposure level for \S+: (\d+\.\d)", proc.stdout)
    assert level, proc.stdout + proc.stderr
    assert proc.returncode == 0 and float(level.group(1)) <= limit, proc.stdout[-2000:]
    assert "Unknown key" not in proc.stderr and "Unknown section" not in proc.stderr, proc.stderr


def test_sysctls_cover_the_design_list() -> None:
    s = HARDENING["hardening_sysctls"]
    assert s["net.ipv4.tcp_syncookies"] == 1 and s["kernel.kptr_restrict"] == 2
    assert s["net.ipv4.conf.all.rp_filter"] == 2  # loose: strict breaks Tailscale
    for key in ("net.ipv4.conf.all.accept_redirects", "net.ipv6.conf.all.accept_redirects",
                "net.ipv4.conf.all.accept_source_route", "net.ipv6.conf.all.accept_source_route",
                "net.ipv4.conf.all.send_redirects", "fs.suid_dumpable"):  # fmt: skip
        assert s[key] == 0, key
    assert s["net.ipv4.conf.all.log_martians"] == 1 and s["kernel.dmesg_restrict"] == 1
    assert s["kernel.unprivileged_bpf_disabled"] == 1 and s["net.core.bpf_jit_harden"] == 2
    assert s["kernel.yama.ptrace_scope"] == 1
    assert {k for k in s if k.startswith("fs.protected_")} == {
        "fs.protected_hardlinks", "fs.protected_symlinks", "fs.protected_fifos", "fs.protected_regular",
    }  # fmt: skip
    assert HARDENING["hardening_journal_max_use"] == "1G" and HARDENING["hardening_disable_sshd"] is True


def test_sshd_goes_only_with_tailscale_ssh_up_and_hardening_runs_in_site() -> None:
    tasks = yaml.safe_load((REPO / "ansible/roles/hardening/tasks/main.yml").read_text())
    names = [t["name"] for t in tasks]
    guard = names.index("Refuse to turn sshd off unless Tailscale (with Tailscale SSH) is up")
    mask = next(i for i, t in enumerate(tasks) if t.get("ansible.builtin.systemd_service", {}).get("masked"))
    assert guard < mask and "RunSSH" in tasks[guard]["failed_when"]
    roles = [r["role"] for r in yaml.safe_load((REPO / "ansible/site.yml").read_text())[0]["roles"]]
    assert roles.index("base") < roles.index("hardening") < roles.index("firewall")
    assert "name: hardening" not in (REPO / "ansible/bootstrap.yml").read_text()  # bootstrap still needs sshd


def test_root_is_locked_only_after_tailscale_ssh_as_deploy() -> None:
    """Security review M1: root's password locked and its SSH key gone, but only in a play that came in as the
    deploy user over Tailscale SSH (never a lock-out), after the sshd guard; e2e checks the result live."""
    tasks = yaml.safe_load((REPO / "ansible/roles/hardening/tasks/main.yml").read_text())
    names = [t["name"] for t in tasks]
    ts_guard = names.index("Refuse to turn sshd off unless Tailscale (with Tailscale SSH) is up")
    guard = names.index(
        "Refuse to lock root unless this play came in through Tailscale SSH as the deploy user"
    )
    lock = next(i for i, t in enumerate(tasks) if t.get("ansible.builtin.user", {}).get("password_lock"))
    keys = next(
        i for i, t in enumerate(tasks)
        if t.get("ansible.builtin.file", {}).get("path") == "/root/.ssh/authorized_keys"
    )  # fmt: skip
    assert ts_guard < guard < lock and guard < keys
    assert tasks[lock]["ansible.builtin.user"] == {"name": "root", "password_lock": True}
    assert tasks[keys]["ansible.builtin.file"]["state"] == "absent"
    that = tasks[guard]["ansible.builtin.assert"]["that"]
    assert "ansible_user | default('') == deploy_user" in that and any("RunSSH" in c for c in that)
    assert tasks[guard]["tags"] == ["tailscale"] and HARDENING["hardening_lock_root"] is True
    for i in (guard, lock, keys):
        assert tasks[i]["when"] == "hardening_lock_root | bool"


def test_firewall_limits_match_the_design() -> None:
    assert FIREWALL == {
        "firewall_mc_new_per_minute": 20,
        "firewall_mc_new_burst": 30,
        "firewall_mc_max_concurrent": 10,
        "firewall_log_per_minute": 10,
        "firewall_set_size": 65535,
    }
    text = render(REPO / "ansible/roles/firewall/templates/nftables.conf.j2")
    assert "ip6 saddr & ffff:ffff:ffff:ffff:: limit rate over 20/minute burst 30 packets" in text
    assert (
        "ip saddr ct count over 10" in text and "ip6 saddr & ffff:ffff:ffff:ffff:: ct count over 10" in text
    )
    assert 'log prefix "mc-ratelimit: "' in text and 'log prefix "mc-connlimit: "' in text
    assert not re.search(r"^\s*flush ruleset", text, re.M)
    # Fail closed (P7 review): a source missing from a set (the set was full) is dropped, never accepted.
    chain = text.split("chain minecraft_new {")[1].split("}\n")[0]
    for check in ("ip saddr != @mc_rate_v4", "ip saddr != @mc_conn_v4",
                  "ip6 saddr & ffff:ffff:ffff:ffff:: != @mc_rate_v6",
                  "ip6 saddr & ffff:ffff:ffff:ffff:: != @mc_conn_v6"):  # fmt: skip
        assert f"{check} jump drop_full" in chain
    assert chain.rstrip().endswith("accept") and 'log prefix "mc-setfull: "' in text


def test_concurrency_limit_is_a_server_yml_option() -> None:
    from mcctl import render as mcctl_render
    from mcctl import schema

    data = yaml.safe_load((REPO / "examples/full/server.yml").read_text())
    assert mcctl_render.group_vars(schema.load_server(data))["firewall_mc_max_concurrent"] == 10
    data["firewall"] = {"max_connections_per_source": 40}
    assert mcctl_render.group_vars(schema.load_server(data))["firewall_mc_max_concurrent"] == 40
    text = render(REPO / "ansible/roles/firewall/templates/nftables.conf.j2", firewall_mc_max_concurrent=40)
    assert "ip saddr ct count over 40" in text


def _nft_usable() -> bool:
    return shutil.which("nft") is not None and hasattr(os, "geteuid") and os.geteuid() == 0


@pytest.mark.skipif(not _nft_usable(), reason="needs nft and root (the e2e run checks it)")
def test_ruleset_passes_nft_check(tmp_path: Path) -> None:
    path = tmp_path / "nftables.conf"
    path.write_text(render(REPO / "ansible/roles/firewall/templates/nftables.conf.j2"))
    proc = subprocess.run(["nft", "-c", "-f", str(path)], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr


def test_e2e_exercises_the_hardening() -> None:
    wf = (REPO / ".github/workflows/e2e-mock.yml").read_text()
    assert "tests/e2e/hardening_check.py /src/product" in wf
    assert "../tests/e2e/firewall.yml" in wf and "tests/e2e/firewall_check.py /tmp/mc-nftables.conf" in wf
    check = (REPO / "tests/e2e/hardening_check.py").read_text()
    for unit, (_, limit) in UNITS.items():
        assert f'"{unit}": {limit}' in check


def test_e2e_checks_are_deterministic() -> None:
    """p11-e2e: the live checks pause the product's timers and read only their own unit invocation's journal
    (a timer run joined by `systemctl start`, or another run's lines, made the hardening probe flaky)."""
    for name in ("hardening_check.py", "agent_check.py", "reboot_check.py", "restart_window_check.py"):
        text = (REPO / "tests/e2e" / name).read_text()
        assert "with timers_paused()" in text, name
        assert not re.search(r'"journalctl",\s*"-u"', text), f"{name}: journal reads must be per invocation"
        assert '"--since"' not in text, name
    common = (REPO / "tests/e2e/e2e_common.py").read_text()
    assert "_SYSTEMD_INVOCATION_ID=" in common and "no new invocation" in common


def test_supply_chain_downloads_are_hash_checked() -> None:
    for path in (REPO / "ansible").rglob("*.yml"):
        for task in re.findall(r"ansible\.builtin\.get_url:\n((?:\s{4,}.*\n)+)", path.read_text()):
            assert "checksum:" in task, path
    for wf in (REPO / ".github/workflows").glob("*.yml"):
        text = wf.read_text()
        if re.search(r"\bcurl [^\n]*-o ", text):
            assert "sha256sum -c" in text, f"{wf.name}: a curl download without a checksum check"
    release = (REPO / ".github/workflows/release.yml").read_text()
    assert "sha256sum" in release and "SHA256SUMS" in release


def test_worker_invocation_logs_are_off() -> None:
    tf = (REPO / "terraform/watchdog.tf").read_text()
    assert re.search(r"invocation_logs\s*=\s*false", tf)


def test_backup_cannot_reach_other_secrets_with_its_capability() -> None:
    """P8 review: CAP_DAC_READ_SEARCH reads any file, so every secret it doesn't need is hidden from mc-backup."""
    text = render(UNITS["mc-backup.service"][0])
    line = next(ln for ln in text.splitlines() if ln.startswith("InaccessiblePaths="))
    hidden = set(line.split("=", 1)[1].split())
    for path in ("/etc/minecraft", "/etc/ssh", "/var/lib/tailscale", "/var/lib/mc-agent", "/etc/shadow"):
        assert f"-{path}" in hidden, path
    assert "EnvironmentFile=/etc/minecraft/restic.env" in text  # read by systemd before the namespace


def test_agent_may_write_only_the_markers_directory() -> None:
    text = render(UNITS["mc-agent.service"][0])
    assert (
        [ln for ln in text.splitlines() if ln.startswith("ReadWritePaths=")]
        == [
            "ReadWritePaths=/var/lib/minecraft",
            "ReadWritePaths=/run/mc-verdicts",  # P13: verdicts + messages for the plugin (setgid minecraft, read-only to it)
        ]
    )
    off = render(UNITS["mc-agent.service"][0], mc_experience_enabled=False)
    assert "/run/mc-verdicts" not in off
    # v1.1: /modpack's codes go there too, so the client modpack alone keeps it writable for the agent
    client = render(UNITS["mc-agent.service"][0], mc_experience_enabled=False, mc_client_enabled=True)
    assert "ReadWritePaths=/run/mc-verdicts" in client.splitlines()


def test_the_rust_agent_unit_runs_the_binary_and_keeps_the_sandbox() -> None:
    """v1.4: agent.implementation rust runs smallworld-agent (the real path, not the mc-agent symlink) with the
    same user, groups, sandbox, limits, state directories and writable paths: the telemetry runs in-process in both
    (telemetry-id.json in mc-agent's state, the segments in mc-telemetry's, verdicts.tsv and messages.tsv in the
    verdict directory), so a switch carries the identity and the segments over."""
    py = render(UNITS["mc-agent.service"][0]).splitlines()
    rust = render(UNITS["mc-agent.service"][0], mc_agent_implementation="rust").splitlines()
    assert (
        "ExecStart=/opt/minecraft/bin/mc_agent.py" in py
        and "ExecStart=/opt/minecraft/bin/smallworld-agent run" in rust
    )
    assert "StateDirectory=mc-agent mc-telemetry" in py and "StateDirectory=mc-agent mc-telemetry" in rust
    assert not any(line.startswith("Environment=PYTHON") for line in rust)
    for lines in (py, rust):
        assert (
            "SupplementaryGroups=minecraft systemd-journal" in lines
        )  # the logs loop feeds the overhead card
        assert "ReadWritePaths=/run/mc-verdicts" in lines  # experience on (the default)

    def kept(lines: list[str]) -> list[str]:
        return [ln for ln in lines if ln and not ln.startswith(("#", "ExecStart=", "Environment="))]

    assert kept(py) == kept(rust)  # nothing else differs: sandbox, MDWE, limits, state, ReadWritePaths, fence


def test_no_telemetry_unit_of_its_own() -> None:
    """v1.4: the Rust agent runs the telemetry in-process (smallworld-agent's telemetry module), as the Python agent
    does: no mc-telemetry.service in any mode. A pre-release box's sidecar is stopped and removed before mc-agent is
    (re)started, so two telemetry writers never share telemetry-id.json and the segments; nothing restarts it."""
    assert not (REPO / "ansible/roles/agent/templates/mc-telemetry.service.j2").exists()
    tasks = yaml.safe_load((REPO / "ansible/roles/agent/tasks/main.yml").read_text())
    names = [t["name"] for t in tasks]
    assert names.index("Remove the old telemetry sidecar") < names.index("Install and run mc-agent")
    removal = tasks[names.index("Remove the old telemetry sidecar")]
    assert "when" not in removal
    steps = {t["name"]: t for t in removal["block"]}
    assert steps["Stop and disable the telemetry sidecar"]["ansible.builtin.systemd_service"] == {
        "name": "mc-telemetry.service", "state": "stopped", "enabled": False}  # fmt: skip
    assert steps["Remove the telemetry sidecar unit"]["ansible.builtin.file"]["state"] == "absent"
    text = (REPO / "ansible/roles/agent").glob("**/*")
    for f in text:
        if f.is_file() and f.suffix in (".yml", ".j2"):
            body = f.read_text()
            assert "mc_agent.py telemetry" not in body and "agent_sidecar " not in body, f


def test_agent_restarts_follow_code_switch_and_product_version() -> None:
    """v1.4: the binary, the mc-agent symlink and the product version file notify `mc-agent code changed`
    (a release that changed no script used to leave the old code running: installed != running)."""
    base = yaml.safe_load((REPO / "ansible/roles/base/tasks/main.yml").read_text())
    by_name = {t["name"]: t for t in base}
    for name in ("Install server-side scripts", "Install smallworld-agent",
                 "Point mc-agent at the selected implementation (agent.implementation)",
                 "Record the installed product version"):  # fmt: skip
        assert by_name[name].get("notify") == "mc-agent code changed", name
    content = by_name["Record the installed product version"]["ansible.builtin.copy"]["content"]
    assert "'agent': base_agent_record" in content and "'version'" in content
    pin = by_name[
        "The smallworld-agent binary is the pinned build (a mismatch fails the deploy before anything is installed)"
    ]
    assert "mc_smallworld_agent_sha256[base_rcon_arch]" in str(pin["ansible.builtin.assert"]["that"])
    install = by_name["Install smallworld-agent"]["ansible.builtin.copy"]
    assert (install["owner"], install["group"], install["mode"]) == ("root", "root", "0755")
    assert [t["name"] for t in base].index(pin["name"]) < [t["name"] for t in base].index(
        "Install smallworld-agent"
    )
    handlers = yaml.safe_load((REPO / "ansible/roles/agent/handlers/main.yml").read_text())
    listeners = [h["name"] for h in handlers if "mc-agent code changed" in str(h.get("listen", ""))]
    assert listeners == ["Is mc-agent installed (its code changed)?", "Restart mc-agent (its code changed)"]
    defaults = yaml.safe_load((REPO / "ansible/roles/base/defaults/main.yml").read_text())
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)  # noqa: S701 - not HTML
    env.filters["bool"] = bool  # Ansible's filter, for booleans
    for impl, entry in (("python", "mc_agent.py"), ("rust", "smallworld-agent")):
        assert env.from_string(defaults["base_agent_entry"]).render(mc_agent_implementation=impl) == entry
    assert "agent_sidecar" not in yaml.safe_load((REPO / "ansible/roles/agent/defaults/main.yml").read_text())


TIMELY_UNITS = (
    "mc-agent.service",
    "mc-health.service",
    "mc-backup.service",
    "mc-restart-window.service",
)  # P13 review M1: deadlines
IDLE_UNITS = ("mc-prune.service", "mc-reboot-gate.service", "mc-backup-check.service")


def _directive(lines: list[str], key: str) -> str:
    return next(ln.split("=", 1)[1] for ln in lines if ln.startswith(key + "="))


def test_p13_helpers_priority_classes_and_the_jvm_wins() -> None:
    """Idle work (prune, reboot gate) at idle priority; work with a deadline (backup's save-off window, the
    agent's freshness sample, health's heartbeat) at timely priority, not starved; the JVM outweighs both."""
    seen = set()
    for unit, (template, _) in UNITS.items():
        text = render(template, mc_helper_cpus="0").splitlines()
        if unit == "minecraft.service":
            assert (
                "CPUWeight=1000" in text
                and "IOWeight=1000" in text
                and "RuntimeDirectory=mc-telemetry" in text
            )
            assert not any(ln.startswith("AllowedCPUs=") for ln in text)  # the JVM keeps every CPU
            continue
        if unit in TIMELY_UNITS or unit in IDLE_UNITS:
            want = GROUP_VARS["mc_timely_priority" if unit in TIMELY_UNITS else "mc_helper_priority"]
            for d in want:
                assert d in text, f"{unit}: {d}"
            other = GROUP_VARS["mc_helper_priority" if unit in TIMELY_UNITS else "mc_timely_priority"]
            assert [ln for ln in text if ln in other and ln not in want] == [], unit
            assert "AllowedCPUs=0" in text
            assert len([ln for ln in text if ln.startswith("CPUWeight=")]) == 1, unit
            seen.add(unit)
    assert seen == set(TIMELY_UNITS) | set(IDLE_UNITS)
    idle, timely = GROUP_VARS["mc_helper_priority"], GROUP_VARS["mc_timely_priority"]
    assert "IOSchedulingClass=idle" in idle and "CPUSchedulingPolicy=batch" in idle
    # timely: never idle IO (it gets nothing while the JVM keeps the disk busy), never batch, not nice 19 ...
    assert "IOSchedulingClass=best-effort" in timely and not any("CPUSchedulingPolicy" in d for d in timely)
    assert 50 <= int(_directive(timely, "CPUWeight")) <= 100 and int(_directive(timely, "Nice")) <= 10
    # ... and the JVM (1000) still wins every contention, by at least 10:1.
    assert (
        int(_directive(timely, "CPUWeight")) * 10 <= 1000 and int(_directive(timely, "IOWeight")) * 10 <= 1000
    )


@pytest.mark.parametrize(
    ("borders", "plugins", "want"),
    [
        ({"minecraft:overworld": 5000, "minecraft:the_nether": 1250, "minecraft:the_end": 3000}, ["Chunky"],
         "survival,survival_nether,survival_the_end"),
        ({"minecraft:the_end": 3000, "minecraft:overworld": 5000}, ["Chunky"], "survival,survival_the_end"),
        ({"minecraft:overworld": 5000}, [], ""),
    ],
)  # fmt: skip
def test_health_env_lists_the_pregen_worlds_in_order(borders: dict, plugins: list, want: str) -> None:
    """Chunky pre-generation covers every dimension with a border, overworld -> nether -> end, by Bukkit world
    name (level-name + _nether / _the_end), and is off without Chunky."""
    text = render(
        REPO / "ansible/roles/monitoring/templates/health.env.j2",
        mc_watchdog_url="https://w", mc_secret_heartbeat_token="t", mc_secret_discord_webhook_url="",
        health_mspt_alert=45, health_mspt_alert_checks=3, health_heap_alert_pct=85, health_heap_alert_checks=6,
        health_gc_pause_alert_ms=50, health_alert_repeat_hours=6, health_disk_alert_pct=85, health_steal_alert_pct=10,
        health_steal_alert_checks=3, mc_resize_hint="x", monitoring_heal_startup_grace_s=900,
        mc_level_name="survival", mc_world_border_radius=borders,
        plugins_lock={"plugins": [{"name": n} for n in plugins]}, monitoring_pregen_enabled=True,
    )  # fmt: skip
    assert f"PREGEN_WORLDS={want}" in text.splitlines()


def test_p16_cpu_affinity_split_pins_the_jvm_off_the_helpers_cpu() -> None:
    """cpu_affinity split: the JVM gets CPUs 1..n-1 (mc_jvm_cpus from the host's count), every helper CPU 0
    (mc_helper_cpus "0" from mcctl render); the sandbox stays complete in both."""
    for unit, (template, _) in UNITS.items():
        lines = render(template, mc_helper_cpus="0", mc_jvm_cpus="1-3").splitlines()
        allowed = [ln for ln in lines if ln.startswith("AllowedCPUs=")]
        if unit == "minecraft.service":
            assert allowed == ["AllowedCPUs=1-3"]
        elif unit in TIMELY_UNITS or unit in IDLE_UNITS:
            assert allowed == ["AllowedCPUs=0"], unit
        for directive in SANDBOX:
            assert directive in lines, f"{unit}: {directive}"
    off = render(UNITS["minecraft.service"][0]).splitlines()
    assert not any(ln.startswith("AllowedCPUs=") for ln in off)
