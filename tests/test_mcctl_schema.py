"""server.yml / players.yml schema: unknown keys, types, every validation rule, defaults, JSON Schema."""

import copy
import dataclasses
import json
import shutil

import pytest
import yaml
from conftest import REPO, make_config_repo

from mcctl import cli, schema, yamlio

MINIMAL = {
    "schema_version": 1,
    "name": "demo",
    "admin": {"name": "Alex", "tailscale_login": "alex@github", "email": "alex@example.org"},
    "github": {"owner": "alex", "config_repo": "demo-config"},
    "dns": {"zone": "example.org"},
    "minecraft": {"version": "26.3", "accept_eula": True},
}


NULL = object()  # explicit YAML null (None deletes the key)


def load(**changes: object) -> schema.ServerConfig:
    """MINIMAL with `a__b` path changes applied (value None deletes the key, NULL sets null)."""
    data = copy.deepcopy(MINIMAL)
    for path, value in changes.items():
        *parents, leaf = path.split("__")
        node = data
        for p in parents:
            node = node.setdefault(p, {})
        if value is None:
            node.pop(leaf, None)
        else:
            node[leaf] = None if value is NULL else value
    return schema.load_server(data)


def errors_of(**changes: object) -> list[str]:
    with pytest.raises(schema.SchemaError) as exc:
        load(**changes)
    return exc.value.errors


def assert_rejected(fragment: str, **changes: object) -> None:
    errs = errors_of(**changes)
    assert any(fragment in e for e in errs), errs


# --- defaults --------------------------------------------------------------------------------------


def test_minimal_gets_product_defaults() -> None:
    cfg = load()
    assert cfg.state.bucket == "demo-tfstate"
    assert cfg.state.key == "terraform.tfstate"
    assert cfg.backups.bucket == "demo-mc-backups"
    assert cfg.host.tailscale_name == "demo"
    assert cfg.minecraft.motd == "Alex's server"
    assert cfg.panel.mode == "cloudflare"
    assert cfg.panel.access.emails == ["alex@example.org"]
    assert cfg.dns.hostname == "mc" and cfg.dns.ttl == 300
    assert cfg.host.timezone == "UTC"
    assert cfg.jvm.heap_mb == "auto"
    assert cfg.monitoring.grafana is False
    assert cfg.automation.auto_merge == "builds+patch"
    assert [p.name for p in cfg.plugins] == ["Chunky", "AxGraves"]
    assert cfg.world.border_radius == schema.BorderRadius(overworld=5000, nether=1250, end=3000)


def test_explicit_values_beat_derived_defaults() -> None:
    cfg = load(state__bucket="my-state", host__tailscale_name="box", minecraft__motd="hi")
    assert (cfg.state.bucket, cfg.host.tailscale_name, cfg.minecraft.motd) == ("my-state", "box", "hi")


def test_partial_nested_block_keeps_other_defaults() -> None:
    cfg = load(performance={"spawn_limits": {"monsters": 40}})
    assert cfg.performance.spawn_limits.monsters == 40
    assert cfg.performance.spawn_limits.animals == 10  # the vanilla profile's
    assert cfg.performance.view_distance == 10


def test_profile_defaults_and_field_overrides() -> None:
    # The product default is vanilla: the pinned jar's own (vanilla) values; balanced = the pre-profile tuning.
    p = load().performance
    assert p.profile == "vanilla" and (p.view_distance, p.simulation_distance) == (10, 8)
    assert dataclasses.asdict(p.spawn_limits) == schema.PROFILES["vanilla"]["spawn_limits"]
    assert set(dataclasses.asdict(p.activation_range).values()) == {0}  # never inactive
    assert (p.despawn_range.soft, p.despawn_range.hard, p.villager_search_radius) == (32, 128, 48)
    assert p.alt_item_despawn_ticks == {} and load().gameplay.redstone == "VANILLA"
    b = load(performance={"profile": "balanced"})
    assert (b.performance.view_distance, b.performance.simulation_distance) == (8, 5)
    assert b.performance.spawn_limits == schema.SpawnLimits(50, 8, 3, 1, 1)
    assert b.performance.activation_range.animals == 16 and b.gameplay.redstone == "ALTERNATE_CURRENT"
    assert b.performance.alt_item_despawn_ticks["cobblestone"] == 300
    # every field still overrides its profile value on its own
    o = load(
        performance={
            "profile": "balanced",
            "activation_range": {"animals": 0},
            "despawn_range": {"hard": 128},
        },
        gameplay={"redstone": "VANILLA"},
    )
    assert o.performance.activation_range.animals == 0 and o.performance.activation_range.monsters == 24
    assert o.performance.despawn_range == schema.DespawnRange(30, 128) and o.gameplay.redstone == "VANILLA"
    # only the view distance set: the profile's simulation distance follows it down, never above it
    assert load(performance={"view_distance": 6}).performance.simulation_distance == 6
    assert load(performance={"view_distance": 12}).performance.simulation_distance == 8
    assert_rejected("performance.profile", performance={"profile": "fast"})
    assert_rejected("must not be null", performance={"view_distance": None})


def test_github_derived_from_git_remote() -> None:
    data = copy.deepcopy(MINIMAL)
    del data["github"]
    cfg = schema.load_server(data, git_origin=("alex", "cfg"))
    assert (cfg.github.owner, cfg.github.config_repo) == ("alex", "cfg")
    with pytest.raises(schema.SchemaError, match="github: owner/config_repo not set"):
        schema.load_server(data)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:octocat/mc-config.git", ("octocat", "mc-config")),
        ("https://github.com/octocat/mc-config", ("octocat", "mc-config")),
        ("https://github.com/octocat/mc-config.git", ("octocat", "mc-config")),
        ("ssh://git@github.com/octocat/my.mc", ("octocat", "my.mc")),
        ("https://gitlab.com/a/b", None),
    ],
)
def test_parse_github_remote(url: str, expected: tuple[str, str] | None) -> None:
    assert schema.parse_github_remote(url) == expected


# --- structure: unknown keys, required, types ------------------------------------------------------


def test_unknown_top_level_key_with_suggestion() -> None:
    assert_rejected("minecarft: unknown key (did you mean 'minecraft'?)", minecarft={})


def test_unknown_nested_key() -> None:
    assert_rejected("performance.view_distnace: unknown key", performance={"view_distnace": 6})
    assert_rejected("panel.access.foo: unknown key", panel={"access": {"foo": 1}})


@pytest.mark.parametrize("path", ["schema_version", "name", "admin", "dns", "minecraft"])
def test_required_top_level(path: str) -> None:
    assert_rejected(f"{path}: required", **{path: None})


@pytest.mark.parametrize(
    "path",
    ["admin__name", "admin__tailscale_login", "dns__zone", "minecraft__version", "minecraft__accept_eula"],
)
def test_required_nested(path: str) -> None:
    assert_rejected(f"{path.replace('__', '.')}: required", **{path: None})


def test_root_must_be_mapping() -> None:
    with pytest.raises(schema.SchemaError, match="<root>: expected a mapping"):
        schema.load_server([1, 2])


@pytest.mark.parametrize(
    ("changes", "fragment"),
    [
        ({"minecraft__version": 26.3}, "expected a string, got 26.3 (quote it)"),
        ({"minecraft__max_players": "20"}, "expected an integer"),
        ({"minecraft__max_players": True}, "expected an integer"),
        ({"minecraft__whitelist": "yes"}, "expected true/false"),
        ({"dns": "example.org"}, "dns: expected a mapping"),
        ({"plugins": {"name": "x"}}, "plugins: expected a list"),
        ({"state__bucket": NULL}, "state.bucket: must not be null"),
        ({"dns__ttl": NULL}, "dns.ttl: expected an integer"),
    ],
)
def test_type_errors(changes: dict, fragment: str) -> None:
    assert_rejected(fragment, **changes)


def test_all_errors_reported_at_once() -> None:
    errs = errors_of(name="Bad Name", dns__ttl=5, minecraft__max_players=0)
    assert len(errs) == 3


# --- field rules (design §2 table) -----------------------------------------------------------------


@pytest.mark.parametrize("value", [2, 0])
def test_schema_version_known(value: int) -> None:
    assert_rejected("unknown schema version", schema_version=value)


@pytest.mark.parametrize("value", ["Lh", "e", "1ek", "ek_mc", "a" * 22, "lh mc", "lh-", "lh--mc", "e-"])
def test_name_slug(value: str) -> None:
    assert_rejected("name:", name=value)


def test_name_slug_ok() -> None:
    assert load(name="lh-mc2").name == "lh-mc2"
    assert load(name="a" * 21).name == "a" * 21


def test_derived_values_are_validated() -> None:
    # Derived names must satisfy their own field's rules; a 21-char name still fits every derivation.
    cfg = load(name="a" * 21)
    assert cfg.host.tailscale_name == "a" * 21 and cfg.backups.bucket == "a" * 21 + "-mc-backups"
    errors: list[str] = []
    bad = schema.dataclasses.replace(cfg, host=schema.dataclasses.replace(cfg.host, tailscale_name="lh-"))
    schema._check_derived(bad, errors)
    assert errors and "host.tailscale_name" in errors[0] and "derived" in errors[0]


def test_github_block_must_be_mapping(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_config_repo(tmp_path / "c", MINIMAL)
    f = root / "server.yml"
    for value in ("[a, b]", "x", "3"):
        f.write_text(
            yaml.safe_dump({k: v for k, v in MINIMAL.items() if k != "github"}) + f"github: {value}\n"
        )
        with pytest.raises(SystemExit) as exc:
            cli.main(["validate", "-C", str(root)])
        assert exc.value.code == 1
        assert "github: expected a mapping" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("admin__tailscale_login", "alex"),
        ("admin__email", "alex@localhost"),
        ("admin__name", ""),
        ("github__owner", "-bad"),
        ("github__config_repo", "a/b"),
        ("state__bucket", "Bad_Bucket"),
        ("state__key", "/abs"),
        ("state__key", 'a"b'),
        ("state__key", "a//b"),
        ("state__key", "${var.x}"),
        ("state__key", "a b"),
        ("dns__zone", "Example.org"),
        ("dns__zone", "localhost"),
        ("dns__hostname", "-mc"),
        ("host__tailscale_name", "MC"),
        ("host__ssh_bootstrap_user", "Root"),
        ("minecraft__level_name", "../world"),
        ("backups__bucket", "x"),
        ("panel__hostname", "panel.x"),
        ("automation__reboot_window", "4:30-06:00"),
        ("automation__reboot_window", "04:30-24:00"),
    ],
)
def test_pattern_rules(path: str, value: str) -> None:
    assert_rejected(f"{path.replace('__', '.')}:", **{path: value})


@pytest.mark.parametrize(
    ("path", "bad", "good"),
    [
        ("dns__ttl", [59, 86401], [60, 86400]),
        ("minecraft__max_players", [0, 501], [1, 500]),
        ("performance__view_distance", [1, 33], [32]),
        ("performance__simulation_distance", [1, 33], [2]),
        ("performance__villager_search_radius", [-1], [0]),
        ("backups__lock_days", [0], [1]),
        ("monitoring__metrics_interval_s", [29, 301], [30, 300]),
        ("monitoring__retention_days", [0], [1]),
        ("jvm__heap_mb", [255], [256, "auto"]),
        ("panel__access__reauth_minutes", [0], [1]),
        ("monitoring__alerts__disk_pct", [0, 101], [100]),
        ("monitoring__alerts__mspt_checks", [0], [1]),
    ],
)
def test_ranges(path: str, bad: list, good: list) -> None:
    for v in bad:
        assert_rejected(path.replace("__", "."), **{path: v})
    for v in good:
        load(**{path: v})


@pytest.mark.parametrize(
    ("path", "bad"),
    [
        ("minecraft__flavour", "spigot"),
        ("gameplay__redstone", "FAST"),
        ("backups__location", "eu"),
        ("panel__mode", "grafana"),
        ("panel__access__idp", "google"),
        ("automation__update_day", "Tuesday"),
        ("automation__auto_merge", "all"),
        ("jvm__heap_mb", "4G"),
    ],
)
def test_enums(path: str, bad: str) -> None:
    assert_rejected("expected one of", **{path: bad})


def test_panel_mode_off_unquoted_yaml() -> None:
    data = yaml.safe_load(yaml.safe_dump(MINIMAL) + "panel: {mode: off}\n")
    assert data["panel"]["mode"] is False
    assert schema.load_server(data).panel.mode == "off"


@pytest.mark.parametrize(
    "ip", ["10.0.0.1", "192.168.1.1", "127.0.0.1", "203.0.113.10", "224.0.0.1", "nope", "::1"]
)
def test_ipv4_public_unicast(ip: str) -> None:
    assert_rejected("host.ipv4:", host__ipv4=ip)


@pytest.mark.parametrize("ip", ["2001:db8::1", "fe80::1", "fd00::1", "::1", "ff02::1", "10.0.0.1", "3fff::1"])
def test_ipv6_public_unicast(ip: str) -> None:
    assert_rejected("host.ipv6:", host__ipv6=ip)


@pytest.mark.parametrize("ip", ["3ffe:ffff:1:2::1%eth0", "fe80::1%eth0", "3ffe:ffff:1:2::1%1"])
def test_ipv6_zone_id_rejected(ip: str) -> None:
    assert_rejected("host.ipv6:", host__ipv6=ip)


def test_public_ips_ok() -> None:
    cfg = load(host__ipv4="9.9.9.9", host__ipv6="3ffe:ffff:1:2::1")
    assert cfg.host.ipv6 == "3ffe:ffff:1:2::1"


def test_ipv6_normalised_to_compressed_form() -> None:
    assert load(host__ipv6="3FFE:FFFF:0001:0002:0000:0000:0000:0001").host.ipv6 == "3ffe:ffff:1:2::1"


def test_timezone() -> None:
    assert_rejected("unknown time zone", host__timezone="Europe/Londn")
    assert load(host__timezone="America/New_York").host.timezone == "America/New_York"


def test_minecraft_version_needs_keys_dir() -> None:
    assert_rejected("isn't supported by this product version (have ['26.3'])", minecraft__version="1.21.4")


def test_eula_must_be_true() -> None:
    assert_rejected("minecraft.accept_eula: must be true", minecraft__accept_eula=False)


def test_online_mode_false_needs_i_know() -> None:
    assert_rejected("pass --i-know", minecraft__online_mode=False)
    data = copy.deepcopy(MINIMAL)
    data["minecraft"]["online_mode"] = False
    assert schema.load_server(data, i_know=True).minecraft.online_mode is False


def test_gamerules() -> None:
    cfg = load(minecraft__gamerules={"keep_inventory": True, "minecraft:respawn_radius": 0})
    assert cfg.minecraft.gamerules == {"keep_inventory": True, "minecraft:respawn_radius": 0}
    assert_rejected("invalid key", minecraft__gamerules={"keep inventory": True})
    assert_rejected(
        "minecraft.gamerules.keep_inventory: expected true/false or an integer",
        minecraft__gamerules={"keep_inventory": "on"},
    )


def test_overrides_checked_against_keys() -> None:
    cfg = load(minecraft__overrides={"purpur": {"settings": {"use-alternate-keepalive": True}}})
    assert cfg.minecraft.overrides["purpur"]["settings"]["use-alternate-keepalive"] is True
    load(minecraft__overrides={"server.properties": {"spawn-protection": 0}})
    assert_rejected("not a key of purpur", minecraft__overrides={"purpur": {"settings": {"no-such-key": 1}}})
    assert_rejected("unknown config file", minecraft__overrides={"bogus": {"a": 1}})
    assert_rejected("expected a mapping", minecraft__overrides={"purpur": 3})
    assert_rejected("minecraft.overrides.purpur: empty mapping", minecraft__overrides={"purpur": {}})
    assert_rejected(
        "minecraft.overrides.purpur.settings: empty mapping",
        minecraft__overrides={"purpur": {"settings": {}}},
    )


def test_empty_motd_is_kept() -> None:
    assert load(minecraft__motd="").minecraft.motd == ""
    assert_rejected("minecraft.motd: must not be null", minecraft__motd=NULL)


def test_border_radius() -> None:
    assert load(world={"border_radius": {"end": 100, "nether": None}}).world.border_radius.nether is None
    assert_rejected("world.border_radius.overworld: must be >= 0", world={"border_radius": {"overworld": -1}})
    assert_rejected("unknown key", world={"border_radius": {"minecraft:overworld": 10}})


def test_simulation_le_view_distance() -> None:
    assert_rejected(
        "simulation_distance: must be <=", performance={"view_distance": 6, "simulation_distance": 7}
    )
    load(performance={"view_distance": 6, "simulation_distance": 6})


def test_spawn_activation_despawn_non_negative() -> None:
    for block in ("spawn_limits", "activation_range", "despawn_range"):
        field = {"spawn_limits": "monsters", "activation_range": "animals", "despawn_range": "soft"}[block]
        assert_rejected(f"performance.{block}.{field}: must be >= 0", performance={block: {field: -1}})


def test_alt_item_despawn_ticks() -> None:
    assert load(performance={"alt_item_despawn_ticks": {"dirt": 100}}).performance.alt_item_despawn_ticks == {
        "dirt": 100
    }
    assert_rejected("must be >= 0", performance={"alt_item_despawn_ticks": {"dirt": -1}})
    assert_rejected("invalid key", performance={"alt_item_despawn_ticks": {"minecraft:dirt": 1}})


@pytest.mark.skipif(shutil.which("systemd-analyze") is None, reason="needs systemd-analyze")
def test_backup_schedules_parsed_by_systemd() -> None:
    load(backups={"schedule": "*:0/30", "prune_schedule": "Sat 02:00"})
    assert_rejected("systemd-analyze calendar rejects", backups={"schedule": "every hour"})
    assert_rejected("backups.prune_schedule", backups={"prune_schedule": "Sun 25:00"})


def test_backup_schedule_cannot_be_an_option(monkeypatch: pytest.MonkeyPatch) -> None:
    # `--help` used to "validate" because systemd-analyze read it as an option.
    assert_rejected("backups.schedule:", backups={"schedule": "--help"})
    calls: list[list[str]] = []
    monkeypatch.setattr(schema.shutil, "which", lambda _exe: "/usr/bin/systemd-analyze")
    monkeypatch.setattr(
        schema.subprocess,
        "run",
        lambda argv, **_kw: calls.append(argv) or schema.subprocess.CompletedProcess(argv, 0),
    )
    assert schema._calendar_ok("-h") is False and calls == []
    assert schema._calendar_ok("hourly") is True
    assert calls == [["/usr/bin/systemd-analyze", "calendar", "--", "hourly"]]


def test_resize_hint_is_env_file_safe() -> None:
    assert (
        load(host__resize_hint="netcup RS (dedicated cores)").host.resize_hint
        == "netcup RS (dedicated cores)"
    )
    for bad in ('say "hi"', "$HOME", "a\\b", " lead"):
        assert_rejected("host.resize_hint:", host__resize_hint=bad)


@pytest.mark.parametrize(
    "bad",
    ["x\nDISCORD_WEBHOOK_URL=https://evil", "x\rY=1", "x\n", "tab\there", "nul\x00", "del\x7f", "\nlead"],
)
def test_env_text_rejects_control_characters(bad: str) -> None:
    # A newline in an EnvironmentFile value would start a new VAR= line (env-file injection).
    assert_rejected("host.resize_hint:", host__resize_hint=bad)


@pytest.mark.parametrize("bad", ["hourly\nOnBootSec=1", "hourly\r", "Sun\x0003:30", "\thourly"])
def test_calendar_rejects_control_characters(bad: str) -> None:
    assert_rejected("backups.schedule:", backups__schedule=bad)


def test_admin_name_is_one_line() -> None:
    assert_rejected("admin.name:", admin__name="Sam\nmotd=x")
    assert load(admin__name="Sam K.").admin.name == "Sam K."


def test_plugins() -> None:
    cfg = load(plugins=[{"name": "Foo", "source": "hangar", "project": "Owner/Foo"}])
    assert cfg.plugins == [schema.Plugin(name="Foo", source="hangar", project="Owner/Foo")]
    assert_rejected(
        "plugins[0].source: expected one of", plugins=[{"name": "a", "source": "curse", "project": "a"}]
    )
    assert_rejected("plugins[0].project: required", plugins=[{"name": "a", "source": "modrinth"}])
    dup = {"name": "a", "source": "modrinth", "project": "a"}
    assert_rejected("plugins: duplicate names", plugins=[dup, dup])


def test_cloudflare_mode_needs_admin_email() -> None:
    assert_rejected("admin.email: required when panel.mode is cloudflare", admin__email=None)
    assert load(admin__email=None, panel={"mode": "onserver"}).panel.access.emails == []
    assert load(admin__email=None, panel={"mode": "off"}).admin.email is None


def test_panel_access_emails() -> None:
    cfg = load(panel={"access": {"emails": ["a@example.org", "b@example.org"]}})
    assert cfg.panel.access.emails == ["a@example.org", "b@example.org"]
    assert_rejected("needs at least 1 email", panel={"access": {"emails": []}})
    assert_rejected("panel.access.emails[0]:", panel={"access": {"emails": ["nope"]}})


def test_logs_limits() -> None:
    assert_rejected("max_rows_per_day: must be <= 50000", monitoring={"logs": {"max_rows_per_day": 50001}})
    assert_rejected("d1_days: must be >= 1", monitoring={"logs": {"d1_days": 0}})


def test_budget_at_defaults_and_extremes() -> None:
    for what, (use, cap) in schema.budget(load()).items():
        assert use <= cap * 0.5, what
    load(
        monitoring={"metrics_interval_s": 30, "logs": {"max_rows_per_day": 50000}},
        experience={"enabled": False},
    )
    # P13: experience telemetry adds its rows; the log ceiling then has to come down
    assert_rejected(
        "D1 rows written/day exceeds",
        monitoring={"metrics_interval_s": 30, "logs": {"max_rows_per_day": 50000}},
    )
    load(monitoring={"logs": {"max_rows_per_day": 40000}})


def test_budget_rejects_over_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(schema, "AUDIT_WATCHDOG_ROWS_PER_DAY", 10_000)
    assert_rejected("D1 rows written/day exceeds", monitoring={"logs": {"max_rows_per_day": 50000}})
    load(panel={"mode": "onserver"}, monitoring={"logs": {"max_rows_per_day": 50000}})  # no Cloudflare budget


def test_reboot_window_not_empty() -> None:
    assert_rejected("start and end are the same", automation__reboot_window="04:00-04:00")


def test_placeholders() -> None:
    cfg = load(admin={"name": "A", "tailscale_login": "a@example.com", "email": "a@example.net"})
    # panel.access.emails is derived from admin.email, so it carries the placeholder too
    assert [m.split(":")[0] for m in schema.placeholders(cfg)] == [
        "admin.tailscale_login",
        "admin.email",
        "panel.access.emails[0]",
        "dns.zone",
    ]
    real = {"admin__email": "alex@mail.internal", "dns__zone": "mail.internal"}
    assert schema.placeholders(load(**real)) == []
    assert schema.placeholders(load(**real, admin__tailscale_login="a@x.test"))


@pytest.mark.parametrize("zone", ["example.com", "mc.example.org", "box.test", "a.invalid", "x.localhost"])
def test_placeholder_zones(zone) -> None:
    msgs = schema.placeholders(load(admin__email="alex@mail.internal", dns__zone=zone))
    assert [m.split(":")[0] for m in msgs] == ["dns.zone"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        ("github__owner", "example-owner"),
        ("github__config_repo", "example-mc-config"),
        ("github__owner", "Example"),
    ],
)
def test_placeholder_github_names(path, value) -> None:
    msgs = schema.placeholders(
        load(admin__email="alex@mail.internal", dns__zone="mail.internal", **{path: value})
    )
    assert [m.split(":")[0] for m in msgs] == [path.replace("__", ".")]
    # A real name that merely contains "example" is fine.
    assert (
        schema.placeholders(
            load(admin__email="a@mail.internal", dns__zone="mail.internal", github__owner="myexample")
        )
        == []
    )


# --- players.yml -------------------------------------------------------------------------------------

U1, U2 = "069a79f4-44e9-4726-a5be-fca90e38aaf5", "853c80ef-3c37-49fd-aa49-938b674adae6"


def test_players_valid() -> None:
    p = schema.load_players(
        {"whitelist": [{"name": "Notch", "uuid": U1}], "ops": [{"name": "Notch", "uuid": U1, "level": 4}]}
    )
    assert p.ops[0].level == 4
    assert schema.load_players({}) == schema.Players()


@pytest.mark.parametrize(
    ("data", "fragment"),
    [
        ({"whitelist": [{"name": "ab", "uuid": U1}]}, "whitelist[0].name"),
        ({"whitelist": [{"name": "abc-d", "uuid": U1}]}, "whitelist[0].name"),
        ({"whitelist": [{"name": "abc", "uuid": U1.upper()}]}, "whitelist[0].uuid"),
        (
            {"whitelist": [{"name": "abc", "uuid": U1}, {"name": "abd", "uuid": U1}]},
            "whitelist: duplicate uuid",
        ),
        (
            {"whitelist": [{"name": "abc", "uuid": U1}], "ops": [{"name": "abd", "uuid": U2, "level": 4}]},
            "isn't on the whitelist",
        ),
        (
            {"whitelist": [{"name": "abc", "uuid": U1}], "ops": [{"name": "abc", "uuid": U1, "level": 5}]},
            "ops[0].level",
        ),
        ({"bans": []}, "bans: unknown key"),
    ],
)
def test_players_rules(data: dict, fragment: str) -> None:
    with pytest.raises(schema.SchemaError) as exc:
        schema.load_players(data)
    assert any(fragment in e for e in exc.value.errors), exc.value.errors


# --- the full example, JSON Schema, CLI ---------------------------------------------------------------------


def test_full_example_is_valid() -> None:
    cfg = schema.load_server(yaml.safe_load((REPO / "examples/full/server.yml").read_text()))
    assert cfg.name == "lighthouse" and cfg.panel.mode == "cloudflare"
    assert cfg.host.timezone == "UTC" and cfg.client.enabled and cfg.discord.commands
    assert cfg.branding.drink.enabled and cfg.branding.resource_pack.enabled
    assert schema.placeholders(cfg) == []  # renderable: no documentation placeholder left


def test_json_schema_file_is_current() -> None:
    assert (REPO / "schema/server.schema.json").read_text() == json.dumps(
        schema.json_schema(), indent=2
    ) + "\n"
    assert cli.main(["schema", "--check", "-o", str(REPO / "schema/server.schema.json")]) == 0


def test_cli_schema_writes_relative_to_cwd(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    assert cli.main(["schema"]) == 0
    assert (tmp_path / "schema/server.schema.json").is_file()
    assert cli.main(["schema", "--check"]) == 0
    assert cli.main(["schema", "-o", "x.json"]) == 0 and (tmp_path / "x.json").is_file()


def test_json_schema_agrees_with_loader() -> None:
    jsonschema = pytest.importorskip("jsonschema")
    js = schema.json_schema()
    jsonschema.Draft202012Validator.check_schema(js)
    validator = jsonschema.Draft202012Validator(js)
    for path in ("examples/full/server.yml", "template/server.yml"):
        assert not list(validator.iter_errors(yaml.safe_load((REPO / path).read_text())))
    assert not list(validator.iter_errors(MINIMAL))
    for bad in ({**MINIMAL, "typo": 1}, {**MINIMAL, "name": "Bad"}, {**MINIMAL, "schema_version": 2}):
        assert list(validator.iter_errors(bad)), bad


def test_cli_validate(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_config_repo(tmp_path / "c", MINIMAL)
    good = root / "server.yml"
    assert cli.main(["validate", "-C", str(root)]) == 0
    (root / "players.yml").write_text("whitelist: [{name: ab, uuid: x}]\n")
    with pytest.raises(SystemExit) as exc:
        cli.main(["validate", "-C", str(root)])
    assert exc.value.code == 1
    assert "players.yml: whitelist[0].name" in capsys.readouterr().err
    (root / "players.yml").write_text("whitelist: []\nops: []\n")
    good.write_text(yaml.safe_dump({**MINIMAL, "typo": 1}))
    with pytest.raises(SystemExit) as exc:
        cli.main(["validate", "-C", str(root)])
    assert exc.value.code == 1
    assert "server.yml: typo: unknown key" in capsys.readouterr().err


def test_cli_usage_errors_exit_2() -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["validate", "--no-such-flag"])
    assert exc.value.code == 2


def test_duplicate_keys_rejected(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_config_repo(tmp_path / "c", MINIMAL)
    (root / "server.yml").write_text(yaml.safe_dump(MINIMAL) + "name: other\n")
    with pytest.raises(SystemExit):
        cli.main(["validate", "-C", str(root)])
    assert "duplicate key 'name'" in capsys.readouterr().err
    with pytest.raises(yaml.YAMLError, match="duplicate key"):
        yamlio.load("a: {b: 1, b: 2}\n")
    assert yamlio.load("a: [{b: 1}, {b: 2}]\n") == {"a": [{"b": 1}, {"b": 2}]}


def test_players_canonical(tmp_path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_config_repo(tmp_path / "c", MINIMAL)
    players = schema.Players(
        whitelist=[schema.Player(name="zed", uuid=U2), schema.Player(name="Notch", uuid=U1)],
        ops=[schema.Op(name="Notch", uuid=U1, level=4)],
    )
    text = schema.dump_players(players)
    assert text == (
        "whitelist:\n"
        f'  - {{name: "Notch", uuid: "{U1}"}}\n'
        f'  - {{name: "zed", uuid: "{U2}"}}\n'
        "ops:\n"
        f'  - {{name: "Notch", uuid: "{U1}", level: 4}}\n'
    )
    assert schema.load_players(yaml.safe_load(text)) == schema.Players(
        whitelist=sorted(players.whitelist, key=lambda p: p.name.lower()), ops=players.ops
    )
    assert schema.dump_players(schema.Players()) == "whitelist: []\nops: []\n"
    # names YAML would misread stay strings
    odd = schema.Players(whitelist=[schema.Player(name="null", uuid=U1), schema.Player(name="1234", uuid=U2)])
    assert [p.name for p in schema.load_players(yaml.safe_load(schema.dump_players(odd))).whitelist] == [
        "1234",
        "null",
    ]
    (root / "players.yml").write_text(text)
    assert cli.main(["validate", "-C", str(root)]) == 0
    (root / "players.yml").write_text(yaml.safe_dump({"whitelist": [{"name": "Notch", "uuid": U1}]}))
    with pytest.raises(SystemExit):
        cli.main(["validate", "-C", str(root)])
    assert "not in canonical form" in capsys.readouterr().err


@pytest.mark.parametrize(
    "zone", ["example.stream", "example.photography", "a-b.co.uk", "xn--bcher-kva.example.dev"]
)
def test_zone_accepts_new_gtlds(zone: str) -> None:
    """A real zone may be on a newer gTLD (.stream): no TLD list, only the DNS label syntax."""
    assert schema.field_errors("dns.zone", zone) == []


@pytest.mark.parametrize("zone", ["stream", "exmpl.", "exmpl.s", "-example.stream", "EXMPL.stream"])
def test_zone_rejects_malformed(zone: str) -> None:
    assert schema.field_errors("dns.zone", zone)
