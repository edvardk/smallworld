"""`mcctl render` (P1, P3): a config repo -> rendered Terraform/Ansible inputs; the product holds no values."""

import json
import os
import re
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import REPO, TEST_SHA, make_config_repo

from mcctl import cli, configrepo, fingerprint, render, schema

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
# v1.5: smooth backups (server.yml backups.*) and performance.vm's helper limits (role variables, defaults = the
# schema's and the default preset's)
V15_BACKUP_VARS = {
    "backups_skip_when_idle",
    "backups_defer_when_busy",
    "backups_max_defer_minutes",
    "backups_gentle_save",
    "backups_upload_limit_kib",
}
V15_VM_VARS = {
    "backups_restic_gomaxprocs",
    "backups_restic_read_concurrency",
    "backups_restic_s3_connections",
    "backups_restic_memory_limit_mb",
    "backups_memory_high_mb",
    "backups_memory_max_mb",
    "monitoring_health_memory_max_mb",
    "map_memory_max_mb",
}

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
    "mc_agent_implementation",  # v1.4: python or rust
    "mc_config_repo",  # P10 (the agent's whitelist pull requests)
    "mc_metrics_interval_s",
    "mc_logs_max_rows_per_day",
    "mc_reboot_window",  # P11
    "mc_maintenance_window",  # v1.3
    "mc_map_enabled",  # v1.4: the world map pre-render (role map)
    "mc_map_schedule",
    "mc_map_dimensions",
    "mc_map_threads",
    "mc_maintenance_timezone",
    "mc_maintenance_force_minutes",
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
    "mc_strict_whitelist",  # v1.3 whitelist guard
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
    "mc_agent_implementation",  # v1.4
    "mc_config_repo",
    "mc_metrics_interval_s",
    "mc_logs_max_rows_per_day",
    "mc_reboot_window",
    "mc_maintenance_window",  # v1.3: deploys restart only inside it
    "mc_map_enabled",  # v1.4: role map
    "mc_map_schedule",
    "mc_map_dimensions",
    "mc_map_threads",
    "mc_maintenance_timezone",
    "mc_maintenance_force_minutes",
    "health_mspt_alert",
    "health_mspt_alert_checks",
    "health_heap_alert_pct",
    "health_heap_alert_checks",
    "health_gc_pause_alert_ms",
    "health_disk_alert_pct",
    "health_steal_alert_pct",
    "health_steal_alert_checks",
    # v1.4: world.pregenerate (the monitoring role's defaults = the schema's)
    "monitoring_pregen_enabled",
    "monitoring_pregen_frontier",
    "monitoring_pregen_margin",
    "monitoring_pregen_max_radius",
    *P13_GROUP_VARS,
    *P16_GROUP_VARS,
    *PROFILE_GROUP_VARS,
    *P17_GROUP_VARS,
    *V11_GROUP_VARS,
    *V15_BACKUP_VARS,
    *V15_VM_VARS,
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
    "mc_product_version_file",  # v1.3: what each deploy installed (mc-agent reports it)
    "mc_ops_user",
    "mc_reboot_target",  # P11
    "mc_restart_pending_dir",  # v1.3: a restart deferred to maintenance.window
    "mc_rcon_port",
    "mc_restart_countdown_seconds",
    "mc_deploy_maintenance_minutes",
    "mc_telemetry_jar_sha256",  # P13
    "mc_brand_jar_sha256",  # P17
    "mc_smallworld_rcon_sha256",  # v1.4: rust/dist/ binaries (scripts/build_rust.py)
    "mc_smallworld_map_sha256",  # v1.4: the world map pre-renderer (role map)
    "mc_map_user",
    "mc_map_dir",
    "mc_map_palette",
    "mc_map_colors",
    "mc_smallworld_agent_sha256",  # v1.4: the agent port (installed by the integration step)
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
        "mc_worker_fingerprint",  # v1.5.2: what this release's Worker runs (mcctl/fingerprint.py)
    }
    assert gv["mc_worker_fingerprint"] == fingerprint.worker_fingerprint(REPO)
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
    data["branding"]["map_room"]["view"] = "spawn"  # view: border needs an overworld border
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_world_border_radius"] == {"minecraft:the_nether": 1250, "minecraft:the_end": 300}
    # v1.4: the map renders without a radius where there is no border
    assert gv["mc_map_dimensions"] == [
        {"id": "minecraft:overworld", "radius": None},
        {"id": "minecraft:the_end", "radius": 300},
    ]


def test_world_map_settings() -> None:
    """v1.4: map.* -> the map role's variables (radius: map.radius, else the active world's border, else
    world.border_radius); branding.map_room -> mc-brand's map_room; the checks on the map room."""
    data = full_data()
    gv = render.group_vars(schema.load_server(data))
    assert (gv["mc_map_enabled"], gv["mc_map_schedule"], gv["mc_map_threads"]) == (True, "*-*-* 02:41", 1)
    room = gv["mc_brand"]["map_room"]
    assert room["enabled"] and room["at"] == "spawn" and room["offset"] == [0, 0, -6] and "x" not in room
    assert gv["mc_brand"]["pocket_map"] == {"enabled": True, "prerender": True}
    data["world"] = {
        "active": "world",
        "library": [{"name": "world", "border_radius": {"overworld": 700, "nether": None, "end": None}}],
    }
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_map_dimensions"][0] == {"id": "minecraft:overworld", "radius": 700}
    assert gv["mc_brand"]["map_room"]["radius"] == 700
    data["map"]["radius"] = 2000
    assert render.group_vars(schema.load_server(data))["mc_map_dimensions"][1]["radius"] == 2000
    # Off: no pre-render in the pocket map, no map room.
    data["map"]["enabled"] = False
    data["branding"]["map_room"]["enabled"] = False
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_brand"]["pocket_map"]["prerender"] is False and gv["mc_map_enabled"] is False
    for change, message in [
        ({"enabled": True}, "needs map.enabled"),
        ({"enabled": True, "x": 1}, "both given"),
        ({"enabled": True, "at": None}, "needs x, y and z"),
        ({"enabled": True, "offset": [0, 0, 99]}, "-64..64"),
        ({"enabled": True, "width": 9}, "width"),
    ]:
        d = full_data()
        d["branding"]["map_room"].update(change)
        if change != {"enabled": True}:
            d["map"]["enabled"] = True
        else:
            d["map"]["enabled"] = False
        with pytest.raises(schema.SchemaError, match=message):
            schema.load_server(d)
    d = full_data()
    d["map"]["dimensions"] = ["overworld", "nether"]
    with pytest.raises(schema.SchemaError, match=r"map\.dimensions"):
        schema.load_server(d)


def test_pregenerate_maps_onto_the_monitoring_role() -> None:
    """v1.4: world.pregenerate sets the monitoring role's variables (one path: health.env's PREGEN_*); its
    defaults are the role's, so a server.yml without it behaves as before (on)."""
    gv = render.group_vars(schema.load_server(full_data()))
    role = yaml.safe_load((REPO / "ansible/roles/monitoring/defaults/main.yml").read_text())
    keys = ("monitoring_pregen_enabled", "monitoring_pregen_frontier", "monitoring_pregen_margin",
            "monitoring_pregen_max_radius")  # fmt: skip
    assert {k: gv[k] for k in keys} == {k: role[k] for k in keys} == {
        "monitoring_pregen_enabled": True, "monitoring_pregen_frontier": True,
        "monitoring_pregen_margin": 1024, "monitoring_pregen_max_radius": 10000,
    }  # fmt: skip
    data = full_data()
    data["world"] = {"pregenerate": {"enabled": False, "frontier": False, "margin": 512, "max_radius": 4000}}
    gv = render.group_vars(schema.load_server(data))
    assert [gv[k] for k in keys] == [False, False, 512, 4000]


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


def test_agent_implementation_defaults_to_rust() -> None:
    """v1.4.0: the Rust agent is the default (CHANGELOG "Changed"); python stays a supported fallback."""
    data = full_data()
    data.pop("agent", None)
    assert render.group_vars(schema.load_server(data))["mc_agent_implementation"] == "rust"
    data["agent"] = {"implementation": "python"}
    assert render.group_vars(schema.load_server(data))["mc_agent_implementation"] == "python"
    assert schema.field_errors("agent.implementation", "perl")  # neither


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


def test_render_stamps_the_product_version_for_the_worker(config_repo, tmp_path) -> None:
    """v1.3: watchdog/version.json in the staged assets (uploaded by `mcctl apply` with worker.js), never served."""
    import mcctl

    repo = configrepo.load(config_repo)
    out = tmp_path / "out"
    written = render.render(repo, out)
    assert out / render.VERSION_STAMP not in written  # staged with the product's files, not a rendered input
    stamp = json.loads((out / render.VERSION_STAMP).read_text())
    assert stamp == {"product": mcctl.__version__, "pin": {"sha": TEST_SHA, "version": "v0.0.0"},
                     "repository": configrepo.PRODUCT_REPOSITORY,
                     "fingerprint": fingerprint.worker_fingerprint(REPO)}  # fmt: skip
    assert re.fullmatch(r"[0-9a-f]{64}", stamp["fingerprint"])
    worker = (REPO / "watchdog/worker.js").read_text()
    assert "version.json" not in worker.split("const ASSET_PATHS = ", 1)[1].split("\n", 1)[0]
    assert '"https://assets.invalid/version.json"' in worker


def _product_copy(dest: Path) -> Path:
    """The fingerprinted parts of this checkout (and a file outside them) in a scratch product tree."""
    shutil.copytree(REPO / "watchdog", dest / "watchdog", ignore=shutil.ignore_patterns("node_modules"))
    shutil.copytree(REPO / "terraform", dest / "terraform", ignore=shutil.ignore_patterns(".terraform"))
    (dest / "mcctl").mkdir()
    shutil.copy2(REPO / "mcctl" / "__init__.py", dest / "mcctl" / "__init__.py")
    shutil.copy2(REPO / "mcctl" / "render.py", dest / "mcctl" / "render.py")
    return dest


def test_worker_fingerprint_follows_the_code_not_the_version(tmp_path) -> None:
    """v1.5.2: the fingerprint covers worker.js, the panel's files and terraform/; a version-only release
    (mcctl/__init__.py, CHANGELOG, render.py comments, tests) leaves it as it is, so the panel doesn't ask for an
    apply; a change to the Worker's code or assets does."""
    root = _product_copy(tmp_path / "p")
    base = fingerprint.worker_fingerprint(root)
    assert base == fingerprint.worker_fingerprint(REPO) and re.fullmatch(r"[0-9a-f]{64}", base or "")
    init = root / "mcctl" / "__init__.py"
    init.write_text(init.read_text().replace(f'"{mcctl_version()}"', '"9.9.9"'))
    (root / "mcctl" / "render.py").write_text((root / "mcctl/render.py").read_text() + "# a comment\n")
    (root / "watchdog/test/worker.test.mjs").write_text("// another test\n")
    (root / "CHANGELOG.md").write_text("## [9.9.9]\n")
    assert fingerprint.worker_fingerprint(root) == base
    for rel in ("watchdog/worker.js", "watchdog/panel/panel.js", "watchdog/panel/fonts/OFL.txt",
                "terraform/watchdog.tf"):  # fmt: skip
        f = root / rel
        before = f.read_bytes()
        f.write_bytes(before + b"\n")
        assert fingerprint.worker_fingerprint(root) != base, rel
        f.write_bytes(before)
    assert fingerprint.worker_fingerprint(root) == base
    (root / "watchdog/panel/new.svg").write_text("<svg/>")
    assert fingerprint.worker_fingerprint(root) != base
    assert fingerprint.worker_fingerprint(tmp_path / "nothing") is None


def mcctl_version() -> str:
    import mcctl

    return mcctl.__version__


# --- v1.5: smooth backups, the small-VM preset --------------------------------------------------------------------


def _role_defaults(role: str) -> dict:
    return yaml.safe_load((REPO / f"ansible/roles/{role}/defaults/main.yml").read_text())


def test_smooth_backups_map_onto_the_backups_role() -> None:
    """Without backups.* the rendered values are the role's defaults (= the schema's): skip, defer 15 min, gentle
    save, no upload limit; explicit values come through."""
    data = full_data()
    data["backups"] = {"bucket": "lighthouse-backups"}
    gv = render.group_vars(schema.load_server(data))
    role = _role_defaults("backups")
    assert {k: gv[k] for k in V15_BACKUP_VARS} == {k: role[k] for k in V15_BACKUP_VARS} == {
        "backups_skip_when_idle": True, "backups_defer_when_busy": True, "backups_max_defer_minutes": 15,
        "backups_gentle_save": True, "backups_upload_limit_kib": 0,
    }  # fmt: skip
    data["backups"] |= {"skip_when_idle": False, "defer_when_busy": False, "max_defer_minutes": 5,
                        "gentle_save": False, "upload_limit_kib": 4096}  # fmt: skip
    gv = render.group_vars(schema.load_server(data))
    assert [gv[k] for k in sorted(V15_BACKUP_VARS)] == [False, False, 5, False, 4096]  # sorted by name


@pytest.mark.parametrize(
    ("backups", "alerts", "ok"),
    [
        ({}, {}, True),  # hourly, period 60: room for 20 minutes
        ({"max_defer_minutes": 20}, {}, True),
        ({"max_defer_minutes": 21}, {}, False),
        ({"max_defer_minutes": 60}, {"backup_period_minutes": 100}, True),
        ({"max_defer_minutes": 60, "defer_when_busy": False}, {}, True),  # off: no limit
        ({"max_defer_minutes": 60, "schedule": "*-*-* 03:00"}, {}, True),  # no fixed period known
        ({"max_defer_minutes": 0}, {}, False),
    ],
)
def test_max_defer_leaves_room_in_the_watchdogs_grace(backups: dict, alerts: dict, ok: bool) -> None:
    data = full_data()
    data["backups"] = {"bucket": "lighthouse-backups", **backups}
    data.setdefault("monitoring", {})["alerts"] = alerts
    if ok:
        schema.load_server(data)
        return
    with pytest.raises(schema.SchemaError) as e:
        schema.load_server(data)
    assert any(err.startswith("backups.max_defer_minutes") for err in e.value.errors), e.value.errors


def test_default_vm_preset_changes_nothing() -> None:
    data = full_data()
    data.pop("jvm"), data.pop("performance")
    cfg = schema.load_server(data)
    gv = render.group_vars(cfg)
    assert cfg.performance.vm == "default"
    assert (gv["mc_heap_reserve_mb"], gv["mc_heap_reserve_pct"], gv["mc_chunk_worker_threads"]) == (
        2816,
        30,
        -1,
    )
    assert (gv["mc_view_distance"], gv["mc_simulation_distance"]) == (10, 8)
    defaults = {**_role_defaults("backups"), **_role_defaults("monitoring"), **_role_defaults("map")}
    assert {k: gv[k] for k in V15_VM_VARS} == {k: defaults[k] for k in V15_VM_VARS}
    assert gv["map_memory_max_mb"] == 512 and not any(gv[k] for k in V15_VM_VARS - {"map_memory_max_mb"})


def test_small_vm_preset() -> None:
    data = full_data()
    data.pop("jvm")
    data["performance"] = {"vm": "small"}
    gv = render.group_vars(schema.load_server(data))
    assert (gv["mc_heap_reserve_mb"], gv["mc_heap_reserve_pct"]) == (1024, 40)
    assert gv["mc_chunk_worker_threads"] == 1  # auto pinned to one worker: pre-generation's pace
    assert (gv["mc_view_distance"], gv["mc_simulation_distance"]) == (8, 8)  # vanilla's 10/8, view capped
    assert {k: gv[k] for k in V15_VM_VARS} == {
        "backups_restic_gomaxprocs": 1, "backups_restic_read_concurrency": 1, "backups_restic_s3_connections": 2,
        "backups_restic_memory_limit_mb": 192, "backups_memory_high_mb": 256, "backups_memory_max_mb": 512,
        "monitoring_health_memory_max_mb": 128, "map_memory_max_mb": 128,
    }  # fmt: skip
    # explicit settings win over the preset
    data["performance"] = {"vm": "small", "profile": "balanced", "view_distance": 12, "chunk_threads": 2}
    data["map"] = {"threads": 3}
    gv = render.group_vars(schema.load_server(data))
    assert (gv["mc_view_distance"], gv["mc_simulation_distance"], gv["mc_chunk_worker_threads"]) == (12, 5, 2)
    assert gv["map_memory_max_mb"] == 64 * 4
    data["jvm"] = {"heap_mb": 2048}
    gv = render.group_vars(schema.load_server(data))
    assert gv["mc_heap_mb"] == 2048 and "mc_heap_reserve_mb" not in gv
    data.pop("jvm")
    data["panel"] = {"mode": "onserver"}
    data["client"], data["discord"] = {"enabled": False}, {}
    assert render.group_vars(schema.load_server(data))["mc_heap_reserve_mb"] == 1024 + 768


@pytest.mark.parametrize(
    ("ram_mb", "heap"),
    [
        (3915, 2304),  # a "4 GB" VPS (3.9 GB usable): 40 % = 1566 MB reserved
        (2930, 1536),  # 3 GB
        (1950, 768),  # 2 GB: the 1024 MB floor; the bare minimum
    ],
)
@pytest.mark.posix_only  # runs ansible-playbook
def test_small_vm_heap_formula(ram_mb: int, heap: int, tmp_path) -> None:
    """The minecraft role's own expression with the small preset's reserve (as test_auto_heap_formula)."""
    import subprocess

    expr = yaml.safe_load((REPO / "ansible/roles/minecraft/defaults/main.yml").read_text())["mc_heap_mb"]
    play = tmp_path / "p.yml"
    play.write_text(yaml.safe_dump([{
        "hosts": "localhost", "connection": "local", "gather_facts": False,
        "vars": {"ansible_facts": {"memtotal_mb": ram_mb}, "mc_heap_reserve_mb": render.SMALL_HEAP_RESERVE_MB,
                 "mc_heap_reserve_pct": render.SMALL_HEAP_RESERVE_PCT, "heap": expr},
        "tasks": [{"ansible.builtin.debug": {"msg": "HEAP={{ heap }}"}}],
    }]))  # fmt: skip
    env = {"ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
    out = subprocess.run(
        ["ansible-playbook", str(play)], capture_output=True, text=True, env=os.environ | env
    )
    assert f"HEAP={heap}" in out.stdout, out.stdout + out.stderr


def test_vm_is_validated() -> None:
    assert schema.field_errors("performance.vm", "tiny")
    assert not schema.field_errors("performance.vm", "small")
