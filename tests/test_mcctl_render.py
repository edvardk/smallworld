"""`mcctl render` (P1, P3): a config repo -> rendered Terraform/Ansible inputs; the product holds no values."""

import json
import os
import re

import pytest
import yaml
from conftest import REPO, TEST_SHA, make_config_repo

from mcctl import cli, configrepo, render, schema

CONSTANTS = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
GOLDEN = REPO / "tests/fixtures/full-render.json"
# Terraform inputs that stay secret and come from TF_VAR_* in the config repo's .env.
TF_ENV_ONLY = {
    "discord_webhook_url",
    "product_read_token",
    "panel_github_client_secret",
    # P10 review / P11: the GitHub Apps mcctl init creates (ids and base64 PEM keys in .env)
    "panel_github_app_id",
    "panel_github_app_private_key",
    "updater_github_app_id",
    "updater_github_app_private_key",
}
# Variables with a default that server.yml doesn't set (an operator's switch, see variables.tf).
TF_NOT_RENDERED = {"watchdog_probe_by_name"}
# Rendered by render() only (an absolute path into the rendered directory; files() alone leaves it out).
TF_RENDER_DIR = {"watchdog_assets_dir"}
# Added after the P3 split.
P8_TFVARS = {
    "panel_mode",
    "monitoring_metrics_interval_s",
    "monitoring_retention_days",
    "monitoring_logs_d1_days",
    "monitoring_logs_r2_days",
    "monitoring_logs_max_rows_per_day",
}
P9_TFVARS = {
    "panel_hostname",
    "panel_access_emails",
    "panel_access_idp",
    "panel_github_client_id",
    "panel_reauth_minutes",
    "panel_title",
    "panel_flavour",
    "panel_minecraft_version",
    "panel_backup_schedule",
    "panel_max_players",
}
P10R_TFVARS = {"github_protect_main"}
OIDC_TFVARS = {"github_oidc_immutable_subject"}  # GitHub's immutable OIDC sub claims (2026-07-15)
SEC_TFVARS = {"github_environments", "dns_mail_hardening"}  # security review M2 / mail hardening
P13_TFVARS = {
    "experience_enabled",
    "experience_raw_days",
    "experience_minute_days",
    "experience_hour_days",
    "experience_messages_enabled",
    "experience_message_per_player_minutes",
    "experience_broadcast_minutes",
    "experience_join_summary",
    "experience_chunk_entities_alert",
}
P13_GROUP_VARS = {
    "mc_experience_enabled",
    "mc_experience_snapshot_seconds",
    "mc_experience_budget_us",
    "mc_experience_messages",
    "mc_experience_message_per_player_s",
    "mc_experience_broadcast_s",
    "mc_experience_messages_per_hour",
    "mc_experience_segment_days",
    "mc_experience_segment_cap_mb",
    "mc_experience_backfill_window",
    "mc_helper_cpus",
}

P14_TFVARS = {"discord_commands"}
# P16: performance knobs (each default = the behaviour before P16)
P16_GROUP_VARS = {
    "mc_jvm_gc",
    "mc_cpu_affinity",
    "mc_chunk_worker_threads",
    "mc_network_compression_threshold",
    "mc_noatime",
}
# Performance profile (vanilla | balanced) and the auto heap's percentage floor
PROFILE_GROUP_VARS = {
    "mc_performance_profile",
    "mc_tuning",
    "mc_tracking_range",
    "mc_heap_reserve_pct",
    "health_alert_repeat_hours",  # alert reminders (health.py, scripts/alerts.py)
}
# The full example sets jvm.heap_mb explicitly: the role takes it as it is
EXPLICIT_HEAP = {"mc_heap_mb"}
AUTO_HEAP = {"mc_heap_reserve_mb", "mc_heap_reserve_pct"}

# P17: branding (the mc-brand plugin's settings, the resource-pack keys, on/off)
P17_GROUP_VARS = {"mc_brand_enabled", "mc_brand", "mc_brand_pack"}
# v1.1: the client modpack (the setup page's host, the artifact mirror; mc-agent's code hand-off)
V11_TFVARS = {"client_enabled", "client_hostname", "client_title", "client_colors"}
V11_GROUP_VARS = {"mc_client_enabled"}

P6_TFVARS = (
    {"deployment_name", "watchdog_backup_period_minutes", "watchdog_discord"}
    | P14_TFVARS
    | P8_TFVARS
    | P9_TFVARS
    | P13_TFVARS
    | P10R_TFVARS
    | OIDC_TFVARS
    | SEC_TFVARS
    | V11_TFVARS
)
P6_GROUP_VARS = {
    *P13_GROUP_VARS,
    *P16_GROUP_VARS,
    *PROFILE_GROUP_VARS,
    *P17_GROUP_VARS,
    *V11_GROUP_VARS,
    "mc_watchdog_url",
    "firewall_mc_max_concurrent",
    "mc_panel_mode",
    "mc_config_repo",  # P10 (the agent's whitelist pull requests)
    "mc_metrics_interval_s",
    "mc_logs_max_rows_per_day",
    "mc_reboot_window",  # P11
}

# Every variable `render` must produce (explicit, so a dropped variable fails the test instead of shrinking
# the comparison). Adding a server.yml consumer means adding its variable here.
EXPECTED_TFVARS = {
    "deployment_name",
    "watchdog_backup_period_minutes",
    "watchdog_discord",
    "cloudflare_token_owner",
    "zone_name",
    "hostname",
    "dns_ttl",
    "github_owner",
    "github_repository",
    "github_protect_main",
    "github_oidc_immutable_subject",
    *SEC_TFVARS,
    "tailscale_admin_login",
    "r2_backup_bucket",
    "r2_location",
    "r2_lock_days",
    "product_repository",
    *P8_TFVARS,
    *P9_TFVARS,
    *P13_TFVARS,
    *P14_TFVARS,
    *V11_TFVARS,
}
EXPECTED_TFVARS_WITH_IPS = EXPECTED_TFVARS | {"server_ipv4", "server_ipv6"}
EXPECTED_GROUP_VARS = {
    "mc_expect_ipv6",
    "mc_world_expected",
    "mc_hostname",
    "timezone",
    "mc_heap_reserve_mb",
    "mc_accept_eula",
    "mc_level_name",
    "mc_motd",
    "mc_max_players",
    "mc_online_mode",
    "mc_whitelist",
    "mc_world_border_radius",
    "mc_world_active",  # v1.2 world library
    "mc_world_library",
    "mc_nerf_spawner_mobs",
    "mc_redstone_implementation",
    "mc_gamerules",  # v1.2: gameplay.locator_bar / sleeping_percentage + minecraft.gamerules
    "mc_view_distance",
    "mc_simulation_distance",
    "mc_spawn_limits",
    "mc_activation_range",
    "mc_despawn_range",
    "mc_villager_search_radius",
    "mc_alt_item_despawn_ticks",
    "mc_backup_schedule",
    "mc_prune_schedule",
    "mc_backup_keep_within_days",
    "mc_resize_hint",
    "mc_watchdog_url",
    "firewall_mc_max_concurrent",
    "mc_panel_mode",
    "mc_config_repo",
    "mc_metrics_interval_s",
    "mc_logs_max_rows_per_day",
    "mc_reboot_window",
    "health_mspt_alert",
    "health_mspt_alert_checks",
    "health_heap_alert_pct",
    "health_heap_alert_checks",
    "health_gc_pause_alert_ms",
    "health_disk_alert_pct",
    "health_steal_alert_pct",
    "health_steal_alert_checks",
    *P13_GROUP_VARS,
    *P16_GROUP_VARS,
    *PROFILE_GROUP_VARS,
    *P17_GROUP_VARS,
    *V11_GROUP_VARS,
}
# ansible/group_vars/all/main.yml: product constants only (P3), disjoint from the rendered variables.
GROUP_VARS_CONSTANTS = {
    "mc_systemd_sandbox",
    "deploy_user",
    "deploy_authorized_keys",
    "tailscale_version",
    "mc_tailscale_server_tag",
    "temurin_version",
    "restic_version",
    "restic_sha256",
    "base_zram_enabled",
    "base_zram_fraction",
    "base_swappiness",
    "mc_root",
    "mc_bin_dir",
    "mc_jar_dir",
    "mc_server_dir",
    "mc_etc_dir",
    "mc_state_dir",
    "mc_restore_dir",
    "mc_worlds_library_file",  # v1.2 world library
    "mc_worlds_state_file",
    "mc_panel_pending_file",
    "mc_ops_user",
    "mc_reboot_target",  # P11
    "mc_rcon_port",
    "mc_restart_countdown_seconds",
    "mc_deploy_maintenance_minutes",
    "mc_telemetry_jar_sha256",  # P13
    "mc_brand_jar_sha256",  # P17
    "mc_brand_icon_sha256",
    "mc_helper_priority",
    "mc_timely_priority",  # P13 review M1
    "mc_telemetry_dir",
    "mc_verdict_dir",
    "mc_client_request_dir",  # v1.1
    "mc_client_code_file",
    "mc_mirror_url",
    "mc_mirror_timeout",
}
REAL_ADMIN = {"name": "Sam", "tailscale_login": "sam@github", "email": "alex@mail.mcctl.internal"}
FULL = REPO / "examples/full/server.yml"


def full_data() -> dict:
    return yaml.safe_load(FULL.read_text())


def declared_tf_variables() -> dict[str, bool]:
    """variable name -> has a default, from terraform/variables.tf."""
    out = {}
    for name, body in re.findall(
        r'^variable "(\w+)" \{\n(.*?)^\}', (REPO / "terraform/variables.tf").read_text(), re.M | re.S
    ):
        out[name] = re.search(r"^  default\s*=", body, re.M) is not None
    return out


@pytest.fixture(scope="module")
def full() -> schema.ServerConfig:
    return schema.load_server(full_data())


def test_rendered_key_sets_are_exact(full: schema.ServerConfig) -> None:
    assert set(render.tfvars(full, "o/r")) == EXPECTED_TFVARS
    assert set(render.group_vars(full)) == EXPECTED_GROUP_VARS - AUTO_HEAP | EXPLICIT_HEAP
    data = full_data()
    data["host"] |= {"ipv4": "9.9.9.9", "ipv6": "3ffe:ffff:1:2::1"}
    assert set(render.tfvars(schema.load_server(data), "o/r")) == EXPECTED_TFVARS_WITH_IPS


def test_product_has_constants_only() -> None:
    # A deployment value added to group_vars/all/main.yml (or a constant shadowing a rendered one) fails here.
    assert set(CONSTANTS) == GROUP_VARS_CONSTANTS
    assert not GROUP_VARS_CONSTANTS & (EXPECTED_GROUP_VARS | {"server_lock", "plugins_lock"})
    assert CONSTANTS["deploy_user"] == render.DEPLOY_USER


def test_tfvars_cover_every_terraform_variable() -> None:
    declared = declared_tf_variables()
    assert EXPECTED_TFVARS_WITH_IPS | TF_ENV_ONLY | TF_NOT_RENDERED | TF_RENDER_DIR == set(declared)
    # Everything without a default is rendered, so no variable is left for an interactive prompt.
    assert {n for n, has_default in declared.items() if not has_default} <= EXPECTED_TFVARS
    assert all(declared[n] for n in TF_ENV_ONLY | TF_NOT_RENDERED | TF_RENDER_DIR)


def golden() -> dict:
    full = schema.load_server(full_data())
    return {"tfvars": render.tfvars(full, "owner/product"), "group_vars": render.group_vars(full)}


def test_full_example_renders_its_golden_values() -> None:
    # Frozen rendering of examples/full (explicit values, so a changed product default doesn't move it):
    # a change here changes every deployment that looks like it. Intended changes: regenerate with
    # `MCCTL_UPDATE_GOLDEN=1 pytest -q tests/test_mcctl_render.py -k golden` and say why in the CHANGELOG.
    text = json.dumps(golden(), sort_keys=True, indent=1) + "\n"
    if os.environ.get("MCCTL_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(text)
    assert text == GOLDEN.read_text()


def test_render_writes_every_file(config_repo, tmp_path) -> None:
    repo = configrepo.load(config_repo)
    out = tmp_path / "out"
    written = render.render(repo, out)
    assert sorted(p.relative_to(out) for p in written) == sorted(render.FILES)
    tf = json.loads((out / render.TFVARS_FILE).read_text())
    # v1.2: the watchdog's static assets, staged next to the rendered files (no pack: resource_pack is off)
    assets = out / render.WATCHDOG_ASSETS_DIR
    assert tf.pop("watchdog_assets_dir") == str(assets.resolve())
    assert (assets / "index.html").is_file() and not (assets / "pack").exists()
    assert tf == render.tfvars(repo.server, configrepo.PRODUCT_REPOSITORY)
    assert tf["product_repository"] == repo.pin.repository == configrepo.PRODUCT_REPOSITORY
    assert tf["github_repository"] == "tests-mc-config"
    backend = (out / render.BACKEND_FILE).read_text()
    assert backend.endswith('bucket = "example-tfstate"\nkey    = "terraform.tfstate"\n')
    inv = yaml.safe_load((out / render.INVENTORY_FILE).read_text())
    assert inv == {"all": {"hosts": {"example": {"ansible_user": "deploy"}}}}
    gv = yaml.safe_load((out / render.GROUP_VARS_FILE).read_text())
    assert set(gv) == EXPECTED_GROUP_VARS | {
        "server_lock",
        "plugins_lock",
        "mc_players_whitelist",
        "mc_players_ops",
        "mc_brand_icon_file",  # P17 (the config repo's own icon, rendered next to the inventory)
        "mc_brand_pack_sha1",  # v1.2: the pack is built from the config's art
        "mc_client_packs",  # v1.2: the current pack version per tier (from the client lock)
    }
    assert gv["server_lock"] == repo.server_lock and gv["plugins_lock"] == repo.plugins_lock
    assert gv["mc_hostname"] == "example"
    assert gv["mc_watchdog_url"] == "https://hb.mc.tests.mcctl.internal"
    assert (tf["deployment_name"], tf["watchdog_backup_period_minutes"], tf["watchdog_discord"]) == (
        repo.server.name,
        60,
        False,
    )
    for rel in (render.BACKEND_FILE, render.INVENTORY_FILE, render.GROUP_VARS_FILE):
        assert (out / rel).read_text().startswith("# Generated by `mcctl render`")


def test_product_repository_comes_from_the_pin(tmp_path) -> None:
    root = make_config_repo(tmp_path / "c", renderable=True)
    wf = root / configrepo.WORKFLOW
    wf.write_text(wf.read_text().replace("edvardk/smallworld/", "someone/fork/"))
    repo = configrepo.load(root)
    assert repo.pin.repository == "someone/fork"
    assert json.loads(render.files(repo)[render.TFVARS_FILE])["product_repository"] == "someone/fork"


def test_state_from_server_yml(tmp_path) -> None:
    data = yaml.safe_load((REPO / "template/server.yml").read_text())
    data["state"] = {"bucket": "my-state", "key": "envs/prod/terraform.tfstate"}
    repo = configrepo.load(make_config_repo(tmp_path / "c", data, renderable=True))
    assert render.backend(repo.server).endswith(
        'bucket = "my-state"\nkey    = "envs/prod/terraform.tfstate"\n'
    )


def test_rendered_files_round_trip(config_repo) -> None:
    repo = configrepo.load(config_repo)
    files = render.files(repo)
    assert yaml.safe_load(files[render.GROUP_VARS_FILE]) == render.ansible_vars(repo)
    assert json.loads(files[render.TFVARS_FILE]) == render.tfvars(repo.server, repo.pin.repository)


def test_expect_ipv6_follows_host(full: schema.ServerConfig) -> None:
    data = full_data()
    data["host"] |= {"ipv4": "9.9.9.9", "ipv6": "3ffe:ffff:1:2::1"}
    cfg = schema.load_server(data)
    assert render.group_vars(cfg)["mc_expect_ipv6"] is True
    assert render.tfvars(cfg, "o/r")["server_ipv4"] == "9.9.9.9"
    assert render.tfvars(cfg, "o/r")["server_ipv6"] == "3ffe:ffff:1:2::1"
    assert render.group_vars(full)["mc_expect_ipv6"] is False


def test_heap(full: schema.ServerConfig) -> None:
    data = full_data()
    assert render.group_vars(full)["mc_heap_mb"] == 6144  # the example's explicit heap
    data.pop("jvm")
    gv = render.group_vars(schema.load_server(data))
    assert (gv["mc_heap_reserve_mb"], gv["mc_heap_reserve_pct"]) == (2816, 30)
    gv = render.group_vars(schema.load_server(data | {"monitoring": {"grafana": True}}))
    assert gv["mc_heap_reserve_mb"] == 2816 + 768
    gv = render.group_vars(
        schema.load_server(
            data | {"panel": {"mode": "onserver"}, "client": {"enabled": False}, "discord": {}}
        )
    )  # the client modpack and Discord commands need cloudflare mode
    assert gv["mc_heap_reserve_mb"] == 2816 + 768
    gv = render.group_vars(schema.load_server(data | {"jvm": {"heap_mb": 4096}}))
    assert gv["mc_heap_mb"] == 4096
    assert "mc_heap_reserve_mb" not in gv and "mc_heap_reserve_pct" not in gv


@pytest.mark.parametrize(
    ("ram_mb", "reserve", "heap"),
    [
        (7990, 2816, 5120),  # an 8 GB box: what an admin measured their way to by hand
        (4096, 2816, 1280),  # a small box keeps the OS and the JVM's native memory out of swap
        (16384, 4915, 11264),  # 30 % of RAM once that is more than the floor
    ],
)
@pytest.mark.posix_only  # runs ansible-playbook
def test_auto_heap_formula(ram_mb: int, reserve: int, heap: int, tmp_path) -> None:
    # The minecraft role's mc_heap_mb expression, evaluated by Ansible's own Jinja (as on the box).
    import subprocess

    defaults = REPO / "ansible/roles/minecraft/defaults/main.yml"
    expr = yaml.safe_load(defaults.read_text())["mc_heap_mb"]
    play = tmp_path / "p.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"ansible_facts": {"memtotal_mb": ram_mb}, "mc_heap_reserve_mb": render.HEAP_RESERVE_MB,
                 "mc_heap_reserve_pct": render.HEAP_RESERVE_PCT, "heap": expr},
        "tasks": [{"ansible.builtin.debug": {"msg": "HEAP={{ heap }}"}}],
    }]))  # fmt: skip
    env = {"ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
    import os

    out = subprocess.run(
        ["ansible-playbook", str(play)], capture_output=True, text=True, env=os.environ | env
    )
    assert f"HEAP={heap}" in out.stdout, out.stdout + out.stderr
    assert max(render.HEAP_RESERVE_MB, ram_mb * render.HEAP_RESERVE_PCT // 100) == reserve


def test_border_omits_null_dimensions() -> None:
    data = full_data()
    data["world"] = {"border_radius": {"overworld": None, "end": 300}}
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_world_border_radius"] == {"minecraft:the_nether": 1250, "minecraft:the_end": 300}


def test_cli_render(config_repo, tmp_path) -> None:
    out = tmp_path / "out"
    assert cli.main(["render", "-C", str(config_repo), "-o", str(out)]) == 0
    tf = json.loads((out / render.TFVARS_FILE).read_text())
    assert tf["tailscale_admin_login"] == "alex@github"
    # default output: <config>/.mcctl/rendered (git-ignored by the template)
    assert cli.main(["render", "-C", str(config_repo)]) == 0
    assert (config_repo / ".mcctl/rendered" / render.INVENTORY_FILE).is_file()
    assert ".mcctl/" in (config_repo / ".gitignore").read_text().split()


def test_render_refuses_placeholders(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    # A placeholder would reach the tailnet ACL / Cloudflare Access.
    root = make_config_repo(tmp_path / "c")  # the template's example.com admin
    out = tmp_path / "out"
    assert cli.main(["render", "-C", str(root), "-o", str(out)]) == 1
    assert "admin.tailscale_login: 'alex@example.com' is a placeholder" in capsys.readouterr().err
    assert not out.exists()
    with pytest.raises(render.RenderError):
        render.render(configrepo.load(root), out)
    data = full_data() | {"admin": REAL_ADMIN | {"tailscale_login": "sam@corp.example"}}
    with pytest.raises(render.RenderError, match="tailscale_login"):
        render.render(configrepo.load(make_config_repo(tmp_path / "d", data)), out)
    assert TEST_SHA in (root / configrepo.WORKFLOW).read_text()


def test_backup_retention_follows_lock_days() -> None:
    data = full_data()
    data["backups"] |= {"lock_days": 14, "schedule": "*:0/30", "prune_schedule": "Sat 02:15"}
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_backup_keep_within_days"] == 15
    assert (gv["mc_backup_schedule"], gv["mc_prune_schedule"]) == ("*:0/30", "Sat 02:15")


def test_panel_title_strips_formatting_codes(full: schema.ServerConfig) -> None:
    import dataclasses

    mc = dataclasses.replace(full.minecraft, motd="\u00a7aSam's \u00a7lserver\\nline two")
    unbranded = dataclasses.replace(full.branding, enabled=False)
    assert render.panel_title(dataclasses.replace(full, minecraft=mc, branding=unbranded)) == "Sam's server"
    assert render.panel_title(full) == "Lighthouse"  # P17: branding.name when branding is on
    # the derived name strips the codes the same way
    data = yaml.safe_load(FULL.read_text())
    data["minecraft"]["motd"] = "\u00a7aSam's \u00a7lserver\\nline two"
    data.pop("branding")
    assert schema.load_server(data).branding.name == "Sam's server"
    assert render.tfvars(full, "o/r")["panel_access_emails"] == list(full.panel.access.emails or [])


def test_p16_performance_knobs_defaults() -> None:
    data = full_data()
    data.pop("jvm"), data.pop("performance")
    cfg = schema.load_server(data)
    gv = render.group_vars(cfg)
    # Changed default (CHANGELOG): ZGC (G1 paused 55-65 ms in every bench); the CPU split stays opt-in.
    assert cfg.jvm.gc == "zgc" and cfg.performance.cpu_affinity == "off"
    assert {k: gv[k] for k in P16_GROUP_VARS} == {
        "mc_jvm_gc": "zgc",
        "mc_cpu_affinity": "off",
        "mc_chunk_worker_threads": -1,  # Paper's own default (keys/26.3: chunk-system.worker-threads)
        "mc_network_compression_threshold": 256,  # the overlay's value before P16
        "mc_noatime": False,
    }
    assert gv["mc_helper_cpus"] == "" and gv["mc_heap_reserve_mb"] == 2816
    gv = render.group_vars(schema.load_server(full_data()))  # the full example: ZGC, explicit heap, CPU split
    assert (gv["mc_jvm_gc"], gv["mc_heap_mb"], gv["mc_cpu_affinity"], gv["mc_helper_cpus"]) == (
        "zgc",
        6144,
        "split",
        "0",
    )


def test_p16_knobs_render(full: schema.ServerConfig) -> None:
    data = full_data()
    data["jvm"] = {"gc": "g1"}
    data["performance"] = {
        "cpu_affinity": "split",
        "chunk_threads": 2,
        "network_compression_threshold": 512,
        "noatime": True,
    }
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_jvm_gc"] == "g1" and gv["mc_heap_reserve_mb"] == 2816  # same auto heap (design §8 P16)
    assert gv["mc_cpu_affinity"] == "split" and gv["mc_helper_cpus"] == "0"  # helpers on CPU 0
    assert gv["mc_chunk_worker_threads"] == 2 and gv["mc_network_compression_threshold"] == 512
    assert gv["mc_noatime"] is True
    data["jvm"] = {"gc": "g1", "heap_mb": 4096}  # an explicit heap is taken as it is
    assert render.group_vars(schema.load_server(data))["mc_heap_mb"] == 4096


@pytest.mark.parametrize(
    ("perf", "error"),
    [
        ({"cpu_affinity": "split", "helper_cpus": "0"}, "performance.helper_cpus: leave it unset"),
        ({"cpu_affinity": "pinned"}, "performance.cpu_affinity"),
        ({"chunk_threads": 0}, "performance.chunk_threads"),
        ({"chunk_threads": "many"}, "performance.chunk_threads"),
        ({"network_compression_threshold": -2}, "performance.network_compression_threshold"),
    ],
)
def test_p16_knobs_are_validated(perf: dict, error: str) -> None:
    data = full_data()
    data["performance"] = perf
    with pytest.raises(schema.SchemaError) as e:
        schema.load_server(data)
    assert any(err.startswith(error) for err in e.value.errors), e.value.errors


def test_p16_gc_is_validated() -> None:
    data = full_data()
    data["jvm"] = {"gc": "shenandoah"}
    with pytest.raises(schema.SchemaError) as e:
        schema.load_server(data)
    assert any(err.startswith("jvm.gc") for err in e.value.errors), e.value.errors


@pytest.mark.parametrize("profile", ["vanilla", "balanced"])
def test_profile_renders_its_tuning(profile: str) -> None:
    data = full_data()
    data["performance"] = {"profile": profile}
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_performance_profile"] == profile and gv["mc_tuning"] == render.TUNING[profile]
    assert set(render.TUNING["vanilla"]) == set(render.TUNING["balanced"])  # every key in both profiles
    v = render.TUNING["vanilla"]
    if profile == "vanilla":
        # the vanilla mechanics the farm review asked for (verified against the pinned jar's own defaults)
        assert v["ticks_per"] == {"monster": 1, "animal": 400, "water": 1, "water_ambient": 1, "ambient": 1}
        assert v["tick_rates"] == {"grass_spread": 1, "mob_spawner": 1, "container_update": 1}
        assert v["merge_radius"] == {"item": 0.5, "exp": -1.0} and v["max_tnt_per_tick"] == 0
        assert set(v["entity_per_chunk_save_limit"].values()) == {-1}
        assert gv["mc_redstone_implementation"] == "VANILLA" and gv["mc_alt_item_despawn_ticks"] == {}
        assert (gv["mc_view_distance"], gv["mc_simulation_distance"]) == (10, 8)
    else:
        assert gv["mc_redstone_implementation"] == "ALTERNATE_CURRENT" and gv["mc_simulation_distance"] == 5
    # tracking ranges are visual only: the same in both profiles
    assert gv["mc_tracking_range"] == {
        "players": 128,
        "animals": 48,
        "monsters": 64,
        "misc": 48,
        "display": 128,
        "other": 64,
    }
