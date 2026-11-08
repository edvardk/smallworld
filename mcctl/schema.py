"""`server.yml` / `players.yml` schema (design: docs/design/product-v1.md §2).

The dataclasses below are the single definition. `load_server` validates a parsed YAML mapping against them
(unknown keys are errors, so typos fail), fills defaults (including derived ones such as `<name>-tfstate`)
and applies the cross-field rules. `json_schema()` generates `schema/server.schema.json` from the same
classes for editor completion; the loader stays authoritative (cross-field rules aren't in the JSON Schema).
"""

from __future__ import annotations

import dataclasses
import datetime
import difflib
import ipaddress
import json
import re
import shutil
import subprocess
import types
import zoneinfo
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Union, get_args, get_origin, get_type_hints

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
KNOWN_SCHEMA_VERSIONS = (1,)

# Patterns are written so they mean the same in Python (`re.fullmatch`) and in JSON Schema (ECMA-262).
# 2-21 chars, starts with a letter, no leading/trailing/double hyphen (derived names stay valid labels).
SLUG = r"^(?=.{2,21}$)[a-z][a-z0-9]*(-[a-z0-9]+)*$"
LABEL = r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?$"
DOMAIN = r"^([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
EMAIL = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
LOGIN = r"^[^@\s]+@[^@\s]+$"  # Tailscale login name: user@example.com or user@github
BUCKET = r"^[a-z0-9][a-z0-9-]{1,61}[a-z0-9]$"  # R2: 3-63 chars, lowercase letters, digits, hyphens
STATE_KEY = r"^(?=.{1,256}$)[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$"
GH_OWNER = r"^[A-Za-z0-9]([A-Za-z0-9-]{0,38})$"
GH_REPO = r"^[A-Za-z0-9._-]{1,100}$"
GH_APP_SLUG = (
    r"^([a-z0-9][a-z0-9-]{0,33})?$"  # a GitHub App's slug (its bot user is "<slug>[bot]"); empty = none
)
UNIX_USER = r"^[a-z_][a-z0-9_-]{0,31}$"
LEVEL_NAME = r"^[A-Za-z0-9_-][A-Za-z0-9_.-]{0,63}$"
WORLD_NAME = r"^[a-z0-9][a-z0-9_-]{0,31}$"  # = scripts/worlds.py WORLD_NAME (restic tag world:<name>)
SEED = r"^(-?[0-9]{1,19})?$"  # a numeric seed, or "" for a random one (= scripts/worlds.py SEED)
PLUGIN_NAME = r"^[A-Za-z0-9_.-]{1,64}$"
PLUGIN_PROJECT = r"^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?$"  # modrinth slug/id, or hangar Owner/Slug
ITEM_ID = r"^[a-z0-9_]+$"
GAMERULE = r"^(minecraft:)?[A-Za-z][A-Za-z0-9_]*$"
WINDOW = r"^([01][0-9]|2[0-3]):[0-5][0-9]-([01][0-9]|2[0-3]):[0-5][0-9]$"
PLAYER_NAME = r"^[A-Za-z0-9_]{3,16}$"
SNOWFLAKE = r"^[0-9]{17,20}$"  # a Discord id (quote it in YAML: a bare number would be read as an integer)
UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
# Free text written unquoted into a systemd EnvironmentFile: one line (no CR/LF or other control
# characters, which would start a new VAR= assignment), no quotes, backslashes or `$`.
ENV_TEXT = r"^[^\x00-\x20\x7f\"'\\$`][^\x00-\x1f\x7f\"'\\$`]{0,199}$"
ONE_LINE_NAME = r"^[^\x00-\x20\x7f][^\x00-\x1f\x7f]{0,63}$"  # no control characters, no leading space
# systemd OnCalendar (one unit-file line); a leading "-" would read as an option, so it is rejected outright.
CALENDAR = r"^[^\x00-\x20\x7f-][^\x00-\x1f\x7f]{0,99}$"
# P17 branding: "#RRGGBB"; an https link; a namespaced key (sound events); one line of text for players (no
# control characters: the plugin and the server list show it as plain text; no "{{", "{%" or "{#": the values
# pass through Ansible's group_vars, where those would be templated).
HEX_COLOR = r"^#[0-9A-Fa-f]{6}$"
HTTPS_URL = r"^https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/[A-Za-z0-9._~%!$&'()*+,;=:@/?#-]*)?$"
DISCORD_INVITE = r"^https://(discord\.gg|discord\.com/invite)/[A-Za-z0-9-]{2,32}$"
NAMESPACED_KEY = r"^[a-z0-9_.-]+:[a-z0-9_./-]+$"
PLAYER_TEXT = r"^(?!.*\{[{%#])[^\x00-\x1f\x7f]{1,200}$"
# A config-repo file (path relative to it), as branding.icon.
REPO_PNG = r"^[A-Za-z0-9_][A-Za-z0-9_./-]{0,127}\.png$"
# systemd OnCalendar shorthands with a fixed period (minutes), for the watchdog's backup check.
SCHEDULE_MINUTES = {"minutely": 1, "hourly": 60, "daily": 1440, "weekly": 10080}

Weekday = Literal["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class SchemaError(ValueError):
    """All validation errors of one file, one `path: message` string each."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("\n".join(errors))
        self.errors = errors


def opt(
    default: Any = None,
    *,
    doc: str,
    required: bool = False,
    derived: str | None = None,
    factory: Callable[[], Any] | None = None,
    **rules: Any,
) -> Any:
    """A schema field. `derived` documents a default computed from other fields (see `_derive`).

    Rules: pattern, minimum, maximum, min_items, check (callable -> error or None), items (rules for list
    items), keys (pattern for map keys), values (rules for map values), json (extra JSON Schema keywords).
    """
    meta = {"doc": doc, "required": required, "derived": derived, **rules}
    if factory is not None:
        return field(default_factory=factory, metadata=meta)
    return field(default=default, metadata=meta)


# --- custom checks ----------------------------------------------------------------------------------


def _public_ipv4(v: str) -> str | None:
    try:
        ip = ipaddress.IPv4Address(v)
    except ValueError:
        return "not an IPv4 address"
    return None if ip.is_global and not ip.is_multicast else "must be a public unicast IPv4 address"


def _public_ipv6(v: str) -> str | None:
    if "%" in v:  # ipaddress accepts zone ids (`fe80::1%eth0`); a DNS record can't carry one
        return "must not have a zone/scope id (%...)"
    try:
        ip = ipaddress.IPv6Address(v)
    except ValueError:
        return "not an IPv6 address"
    if ip not in ipaddress.IPv6Network("2000::/3") or not ip.is_global:
        return "must be a public unicast IPv6 address (2000::/3)"
    return None


def _timezone(v: str) -> str | None:
    return None if v in zoneinfo.available_timezones() else "unknown time zone (not in zoneinfo)"


def _eula(v: bool) -> str | None:
    return None if v else "must be true: running a server means accepting https://aka.ms/MinecraftEULA"


def _schema_version(v: int) -> str | None:
    if v in KNOWN_SCHEMA_VERSIONS:
        return None
    return f"unknown schema version (this mcctl knows {list(KNOWN_SCHEMA_VERSIONS)}; see `mcctl upgrade`)"


# --- server.yml -------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Admin:
    name: str = opt(doc="Your name (eula.txt, default MOTD).", required=True, pattern=ONE_LINE_NAME)
    tailscale_login: str = opt(
        doc="Tailscale login name (ACL owner, panel admin).", required=True, pattern=LOGIN
    )
    email: str | None = opt(
        doc="Email allowed to log in to the admin panel (Cloudflare Access sends one-time codes here; not "
        "your Cloudflare account login). Required when panel.mode is cloudflare.",
        pattern=EMAIL,
    )


@dataclass(frozen=True)
class GitHub:
    owner: str = opt(
        doc="Owner of the config repo.", derived="from `git remote get-url origin`", pattern=GH_OWNER
    )
    config_repo: str = opt(
        doc="Config repo name.", derived="from `git remote get-url origin`", pattern=GH_REPO
    )
    oidc_subject: Literal["immutable", "legacy"] = opt(
        "immutable",
        doc="Format of the `sub` claim in the config repo's GitHub Actions OIDC tokens, which the tailnet's "
        "CI identity must match: `immutable` (`repo:OWNER@OWNER_ID/REPO@REPO_ID:...`, GitHub's default for "
        "repositories created, renamed or transferred since 2026-07-15, or opted in) or `legacy` "
        "(`repo:OWNER/REPO:...`, older repositories). Set by `mcctl setup` from the GitHub API; `mcctl "
        "doctor` checks it.",
    )
    protect_main: bool = opt(
        True,
        doc="Rulesets on the config repo: main only via pull requests with the required checks (needs GitHub "
        "Pro for a private personal repo; false = no ruleset, see SECURITY.md).",
    )
    environments: bool = opt(
        False,
        doc="GitHub Environments on the config repo (security review M2): the deploy secrets live in "
        "environment `deploy`, the updater App's key in `update`, both usable from main only, so a workflow "
        "pushed to another branch (update/*, panel/*) can't read them. The CI identity then trusts the "
        "`environment:deploy` OIDC subject. Needs the same paid plan as rulesets for a private personal "
        "repo. New config repos set it (template); false = repository-level secrets as before.",
    )
    updater_app: str = opt(
        "",
        doc="Slug of the updater GitHub App: `server / gate` lets lock/product updates merge unattended only "
        "from its bot user (empty = they need the admin). Set by `mcctl setup`.",
        pattern=GH_APP_SLUG,
    )
    panel_app: str = opt(
        "",
        doc="Slug of the panel's GitHub App: `server / gate` lets whitelist changes merge unattended only "
        "from its bot user (empty = they need the admin). Set by `mcctl setup`.",
        pattern=GH_APP_SLUG,
    )


@dataclass(frozen=True)
class State:
    bucket: str = opt(doc="R2 bucket holding the Terraform state.", derived="<name>-tfstate", pattern=BUCKET)
    # Written into a .tfbackend (HCL) file, so a conservative character set: no quotes, `$`, spaces or `//`.
    key: str = opt("terraform.tfstate", doc="Object key of the state file.", pattern=STATE_KEY)


@dataclass(frozen=True)
class Dns:
    zone: str = opt(doc="Cloudflare zone (must exist in the account).", required=True, pattern=DOMAIN)
    hostname: str = opt("mc", doc="Record name for the server within the zone.", pattern=LABEL)
    ttl: int = opt(300, doc="TTL of the A/AAAA records in seconds.", minimum=60, maximum=86400)
    mail_hardening: bool = opt(
        True,
        doc="The zone sends and receives no mail: Terraform adds a null MX (`MX 0 .`), `v=spf1 -all` and a "
        "`p=reject` DMARC record at the apex, so nobody can spoof mail from it. Set false if the zone has "
        "mail (the plan refuses while another MX record exists).",
    )


@dataclass(frozen=True)
class CloudflareSettings:
    token_owner: Literal["user", "account"] = opt(
        "user",
        doc="Who owns the Cloudflare admin token: account (account-owned, recommended: it can't reach "
        "another account) or user. `mcctl init` sets it from the token; Terraform's R2 credential is owned "
        "the same way.",
    )


@dataclass(frozen=True)
class Host:
    ipv4: str | None = opt(doc="Public IPv4 (null until the host exists).", check=_public_ipv4)
    ipv6: str | None = opt(doc="Public IPv6 from the host's prefix (null = IPv4 only).", check=_public_ipv6)
    tailscale_name: str = opt(doc="Tailscale machine name.", derived="<name>", pattern=LABEL)
    timezone: str = opt("UTC", doc="Host time zone (zoneinfo name).", check=_timezone)
    resize_hint: str = opt(
        "a host with dedicated cores or more RAM",
        doc="Upgrade suggestion in resize alerts.",
        pattern=ENV_TEXT,
    )
    ssh_bootstrap_user: str = opt("root", doc="SSH user for the one-off bootstrap.", pattern=UNIX_USER)


@dataclass(frozen=True)
class Minecraft:
    version: str = opt(
        doc="Minecraft version; needs server-config/keys/<version>/ in the product. Human-only change.",
        required=True,
        pattern=r"^[0-9A-Za-z][0-9A-Za-z._-]*$",
    )
    flavour: Literal["purpur", "paper"] = opt("purpur", doc="Server software.")
    accept_eula: bool = opt(
        doc="Minecraft EULA; must be true.", required=True, check=_eula, json={"const": True}
    )
    motd: str = opt(doc="Server list message.", derived="<admin.name>'s server")
    max_players: int = opt(20, doc="Player slots.", minimum=1, maximum=500)
    level_name: str = opt("world", doc="World folder name.", pattern=LEVEL_NAME)
    online_mode: bool = opt(True, doc="Authenticate players with Mojang (false needs --i-know).")
    whitelist: bool = opt(True, doc="Only whitelisted players may join.")
    strict_whitelist: bool = opt(
        False,
        doc="v1.3: players.yml is the whole whitelist: a deploy removes every live player it doesn't list. "
        "Default false: players who were never in players.yml (added on the console, through Discord, or a "
        "panel add that never reached Git) are kept and announced; removals from players.yml and the panel "
        "still apply.",
    )
    gamerules: dict[str, bool | int] = opt(
        factory=dict,
        doc="Game rules set over RCON in every dimension at each deploy (26.x names: snake_case, e.g. "
        "keep_inventory; checked against keys/<version>/gamerules.txt). locator_bar and "
        "players_sleeping_percentage are gameplay.* fields.",
        keys=GAMERULE,
    )
    overrides: dict[str, dict[str, Any]] = opt(
        factory=dict,
        doc="Escape hatch: <file>.<dotted.key>: value; every key must exist in keys/<version>/<file>.txt.",
    )


@dataclass(frozen=True)
class BorderRadius:
    overworld: int | None = opt(5000, doc="Overworld border radius in blocks (null = none).", minimum=0)
    nether: int | None = opt(1250, doc="Nether border radius in blocks (null = none).", minimum=0)
    end: int | None = opt(
        3000, doc="End border radius in blocks (null = none; 3000 keeps the main island inside).", minimum=0
    )


# v1.2 world library: the types and presets scripts/worlds.py turns into server.properties keys (TYPES,
# PRESETS; tests/test_worlds.py keeps the lists equal, every key in keys/<version>/server.properties.txt).
WorldType = Literal[
    "default", "large_biomes", "amplified", "single_biome", "flat_classic", "flat_void", "flat_redstone"
]
WorldPreset = Literal["survival_smp", "creative_build", "hardcore", "farm_test", "peaceful"]
# The server.properties keys a world switch writes (scripts/worlds.py MANAGED_KEYS): never overridden by hand
# while a library is set, or every deploy would undo the active world's settings.
WORLD_PROPERTY_KEYS = (
    "difficulty", "force-gamemode", "gamemode", "generate-structures", "generator-settings", "hardcore",
    "level-seed", "level-type",
)  # fmt: skip


@dataclass(frozen=True)
class WorldEntry:
    name: str = opt(doc="World name (the panel's label and its restic tag world:<name>).", required=True,
                    pattern=WORLD_NAME)  # fmt: skip
    seed: str = opt("", doc='Seed, a whole number in quotes ("" = random); used when the world is created.',
                    pattern=SEED)  # fmt: skip
    type: WorldType = opt(
        "default", doc="World type when it is created (flat_*: superflat presets; single_biome: plains only)."
    )
    preset: WorldPreset | None = opt(
        doc="Server preset (difficulty, gamemode, force-gamemode, hardcore) written at each switch; null = "
        "leave server.properties as it is."
    )
    border_radius: BorderRadius | None = opt(doc="This world's border (null = world.border_radius).")
    pregen: bool | None = opt(
        doc="Chunky pre-generation inside the border (null = on when Chunky is locked)."
    )


@dataclass(frozen=True)
class World:
    border_radius: BorderRadius = opt(factory=BorderRadius, doc="World border per dimension, centred on 0,0.")
    expected: bool = opt(
        False, doc="true = a deploy fails rather than generate a new world when none exists."
    )
    active: str = opt(
        "world",
        doc="v1.2 world library: the world on the server (the panel's Worlds tab switches it, then a PR).",
        pattern=WORLD_NAME,
    )
    library: list[WorldEntry] = opt(
        factory=list,
        doc="v1.2: known worlds (the active one and the archived ones: restic snapshots tagged "
        "world:<name>); written by the panel, one canonical line per world.",
    )


PROFILE = "performance.profile"


@dataclass(frozen=True)
class Gameplay:
    nerf_spawner_mobs: bool = opt(False, doc="Spawner mobs have no AI (breaks most spawner farms).")
    locator_bar: bool = opt(
        True,
        doc="v1.2: vanilla's Locator Bar (game rule locator_bar): other players' direction on the XP bar, "
        "for every player, no mod needed. Set over RCON in every dimension at each deploy.",
    )
    sleeping_percentage: int = opt(
        50,
        doc="v1.2: share of online players who must sleep to skip the night (game rule "
        "players_sleeping_percentage; vanilla 100).",
        minimum=0,
        maximum=100,
    )
    redstone: Literal["VANILLA", "EIGENCRAFT", "ALTERNATE_CURRENT"] | None = opt(
        doc="Redstone implementation (vanilla profile: VANILLA, every contraption behaves as in vanilla; "
        "balanced: ALTERNATE_CURRENT, faster but its update order differs).",
        derived=PROFILE,
    )


@dataclass(frozen=True)
class SpawnLimits:
    monsters: int | None = opt(
        doc="bukkit.yml spawn-limits.monsters (vanilla 70).", derived=PROFILE, minimum=0
    )
    animals: int | None = opt(doc="bukkit.yml spawn-limits.animals (vanilla 10).", derived=PROFILE, minimum=0)
    water_animals: int | None = opt(
        doc="bukkit.yml spawn-limits.water-animals (vanilla 5).", derived=PROFILE, minimum=0
    )
    water_ambient: int | None = opt(
        doc="bukkit.yml spawn-limits.water-ambient (vanilla 20).", derived=PROFILE, minimum=0
    )
    ambient: int | None = opt(doc="bukkit.yml spawn-limits.ambient (vanilla 15).", derived=PROFILE, minimum=0)


_AR = "spigot.yml entity-activation-range.{} (0 = always active, as in vanilla)."


@dataclass(frozen=True)
class ActivationRange:
    animals: int | None = opt(doc=_AR.format("animals"), derived=PROFILE, minimum=0)
    monsters: int | None = opt(doc=_AR.format("monsters"), derived=PROFILE, minimum=0)
    raiders: int | None = opt(doc=_AR.format("raiders"), derived=PROFILE, minimum=0)
    misc: int | None = opt(doc=_AR.format("misc"), derived=PROFILE, minimum=0)
    water: int | None = opt(doc=_AR.format("water"), derived=PROFILE, minimum=0)
    villagers: int | None = opt(doc=_AR.format("villagers"), derived=PROFILE, minimum=0)
    flying_monsters: int | None = opt(doc=_AR.format("flying-monsters"), derived=PROFILE, minimum=0)


@dataclass(frozen=True)
class DespawnRange:
    soft: int | None = opt(
        doc="Monster soft despawn range in blocks (vanilla 32).", derived=PROFILE, minimum=0
    )
    hard: int | None = opt(
        doc="Monster hard despawn range in blocks (vanilla 128).", derived=PROFILE, minimum=0
    )


_TR = "spigot.yml entity-tracking-range.{} in blocks: how far away players see them (visual only; capped by "
_TR += "the view distance)."


@dataclass(frozen=True)
class TrackingRange:
    """Invisible to gameplay: the range only decides how far away a client is sent an entity (packets and the
    server's tracker work), never whether it ticks, spawns, despawns or works."""

    players: int = opt(128, doc=_TR.format("players"), minimum=8, maximum=512)
    animals: int = opt(48, doc=_TR.format("animals"), minimum=8, maximum=512)
    monsters: int = opt(64, doc=_TR.format("monsters"), minimum=8, maximum=512)
    misc: int = opt(
        48, doc=_TR.format("misc (items, XP orbs, item frames, paintings)"), minimum=8, maximum=512
    )
    display: int = opt(
        128, doc=_TR.format("display (text/block/item display entities)"), minimum=8, maximum=512
    )
    other: int = opt(64, doc=_TR.format("other"), minimum=8, maximum=512)


def _alt_item_despawn_balanced() -> dict[str, int]:
    junk = [
        "cobblestone",
        "cobbled_deepslate",
        "netherrack",
        "dirt",
        "tuff",
        "andesite",
        "diorite",
        "granite",
    ]
    ticks = dict.fromkeys(junk, 300) | dict.fromkeys(["gravel", "sand"], 600)
    return ticks | dict.fromkeys(["rotten_flesh", "spider_eye", "poisonous_potato"], 600)


@dataclass(frozen=True)
class Performance:
    profile: Literal["vanilla", "balanced"] = opt(
        "vanilla",
        doc="Tuning profile. vanilla = every game mechanic exactly as in vanilla (mob caps, spawn rates, "
        "despawn ranges, activation ranges off, villager AI, item/XP merging, cramming, redstone, TNT); only "
        "optimisations no player can notice. balanced = the product's pre-1.x tuning (lower mob caps, "
        "activation ranges, faster item despawn, Alternate Current redstone): cheaper, but farms and some "
        "contraptions behave differently. Every field below that says 'Default: performance.profile' "
        "overrides its profile value on its own (docs/operations.md 'Performance profile').",
    )
    view_distance: int | None = opt(
        doc="View distance in chunks (vanilla profile 10, balanced 8).",
        derived=PROFILE,
        minimum=2,
        maximum=32,
    )
    simulation_distance: int | None = opt(
        doc="Simulation distance in chunks (<= view_distance; vanilla profile 8: the full 128-block mob "
        "spawning sphere, balanced 5).",
        derived=PROFILE,
        minimum=2,
        maximum=32,
    )
    spawn_limits: SpawnLimits = opt(factory=SpawnLimits, doc="Mob caps per player.")
    activation_range: ActivationRange = opt(factory=ActivationRange, doc="Entity activation ranges.")
    despawn_range: DespawnRange = opt(factory=DespawnRange, doc="Monster despawn ranges.")
    tracking_range: TrackingRange = opt(factory=TrackingRange, doc="Entity tracking (visibility) ranges.")
    villager_search_radius: int | None = opt(
        doc="Villager POI search radius (vanilla profile 48 = Purpur's default, balanced 32).",
        derived=PROFILE,
        minimum=0,
    )
    helper_cpus: str | None = opt(
        doc="P13: CPUs the product's helper services (agent, health, backup, ...) may use (systemd "
        "AllowedCPUs, e.g. '0'); null = all. The JVM keeps every CPU; helpers always run at idle "
        "CPU/IO priority.",
        pattern=r"^[0-9]{1,3}([-,][0-9]{1,3}){0,15}$",
    )
    cpu_affinity: Literal["off", "split"] = opt(
        "off",
        doc="P16: split = the product's helpers (agent, health, backup, prune, reboot gate) on CPU "
        "0 and the JVM on CPUs 1..n-1 (systemd AllowedCPUs, from the host's CPU count at deploy; refused "
        "below 3 CPUs). off = the scheduler weights decide (today's behaviour).",
    )
    chunk_threads: Literal["auto"] | int = opt(
        "auto",
        doc="P16: paper-global.yml chunk-system.worker-threads (chunk generation/loading workers); auto = "
        "Paper's default (-1: half the cores, 1 on a 4-core box).",
        minimum=1,
        maximum=32,
    )
    network_compression_threshold: int = opt(
        256,
        doc="P16: server.properties network-compression-threshold (packets of at least this many bytes "
        "are compressed; -1 = off).",
        minimum=-1,
        maximum=65535,
    )
    noatime: bool = opt(
        False,
        doc="P16: mount the filesystem holding the server directory with noatime (its /etc/fstab entry + a "
        "live remount). false leaves the mount untouched.",
    )
    alt_item_despawn_ticks: dict[str, int] | None = opt(
        doc="Item id -> despawn ticks (20 ticks = 1 s); {} = off (vanilla profile: every item lasts 5 min; "
        "balanced: junk blocks 15-30 s). Replaces the whole map when set.",
        derived=PROFILE,
        keys=ITEM_ID,
        values={"minimum": 0},
    )


@dataclass(frozen=True)
class Jvm:
    heap_mb: Literal["auto"] | int = opt(
        "auto",
        doc="Heap in MB, or auto = RAM - max(2816 (3584 with onserver/grafana), 30% of RAM), rounded "
        "down to 256 MB: the JVM's own memory outside the heap measured ~1.5 GB on an 8 GB box, plus the OS "
        "and the product's helpers (docs/reference.md 'Design notes').",
        minimum=256,
    )
    gc: Literal["g1", "zgc"] = opt(
        "zgc",
        doc="P16: garbage collector. zgc = Generational ZGC (Java 25: sub-millisecond pauses, ~15% more CPU, "
        "less native memory than G1); g1 = Aikar's G1 flags (55-65 ms pause ticks in every bench).",
    )


@dataclass(frozen=True)
class Backups:
    bucket: str = opt(doc="R2 bucket for restic backups.", derived="<name>-mc-backups", pattern=BUCKET)
    # Values from the cloudflare_r2_bucket.location description in the pinned provider (5.28.1) schema.
    location: Literal["apac", "eeur", "enam", "weur", "wnam", "oc"] = opt("weur", doc="R2 location hint.")
    lock_days: int = opt(7, doc="R2 bucket lock on snapshots/ and data/ (keep_within = lock + 1).", minimum=1)
    schedule: str = opt("hourly", doc="Backup timer (systemd OnCalendar).", pattern=CALENDAR)
    prune_schedule: str = opt("Sun 03:30", doc="Prune timer (systemd OnCalendar).", pattern=CALENDAR)


@dataclass(frozen=True)
class Plugin:
    name: str = opt(doc="Plugin name (as in plugins.lock.yml).", required=True, pattern=PLUGIN_NAME)
    source: Literal["modrinth", "hangar"] = opt(doc="Where the lock tool resolves it.", required=True)
    project: str = opt(doc="Project slug/id on the source.", required=True, pattern=PLUGIN_PROJECT)


def _default_plugins() -> list[Plugin]:
    # v1.2: AxGraves (MIT): a grave keeps items and XP where a player died (server-config/plugins/)
    return [
        Plugin(name="Chunky", source="modrinth", project="chunky"),
        Plugin(name="AxGraves", source="modrinth", project="axgraves"),
    ]


@dataclass(frozen=True)
class PanelAccess:
    emails: list[str] = opt(
        doc="Emails allowed through Cloudflare Access (>= 1 in cloudflare mode).",
        derived="[admin.email]",
        items={"pattern": EMAIL},
    )
    idp: Literal["otp", "github"] = opt("otp", doc="Access login method (github needs an OAuth app).")
    github_client_id: str | None = opt(
        doc="GitHub OAuth app client id (idp github; its secret goes in TF_VAR_panel_github_client_secret).",
        pattern=r"^[A-Za-z0-9._-]{8,64}$",
    )
    reauth_minutes: int = opt(
        10,
        doc="Session length of the confirmation host's Access app (one-time PIN): destructive actions need a "
        "confirmation login younger than this.",
        minimum=1,
        maximum=60,
    )


@dataclass(frozen=True)
class Panel:
    mode: Literal["cloudflare", "onserver", "off"] = opt("cloudflare", doc="Admin panel transport.")
    hostname: str = opt("panel", doc="Panel record name (cloudflare mode): panel.<zone>.", pattern=LABEL)
    access: PanelAccess = opt(factory=PanelAccess, doc="Cloudflare Access settings.")


@dataclass(frozen=True)
class Agent:
    implementation: Literal["python", "rust"] = opt(
        "python",
        doc="v1.4: which mc-agent runs on the box (panel mode cloudflare) and answers mcctl's actions: "
        "python (scripts/mc_agent.py) or rust (smallworld-agent, the port). Either runs the experience "
        "telemetry in-process. Both read and write the same state, so switching back is a deploy. "
        "The default becomes rust in a later release, after a soak.",
    )


@dataclass(frozen=True)
class Logs:
    d1_days: int = opt(14, doc="Days of searchable logs in D1.", minimum=1)
    r2_days: int = opt(30, doc="Days of raw journal archives in R2.", minimum=1)
    max_rows_per_day: int = opt(30000, doc="Cap on log rows written to D1 per day.", minimum=0, maximum=50000)


@dataclass(frozen=True)
class Alerts:
    mspt: int = opt(45, doc="Alert when MSPT exceeds this ...", minimum=1)
    mspt_checks: int = opt(3, doc="... for this many checks in a row.", minimum=1)
    heap_pct: int = opt(
        85, doc="Post-GC heap percentage that triggers the resize hint ...", minimum=1, maximum=100
    )
    heap_checks: int = opt(6, doc="... for this many checks in a row.", minimum=1)
    gc_pause_ms: int = opt(
        50,
        doc="Alert on server freezes from Java memory management longer than this (G1 pauses; ZGC allocation "
        "stalls, ZGC's own pauses are sub-millisecond). 50 ms = one tick's budget.",
        minimum=1,
    )
    repeat_hours: int = opt(
        6,
        doc="health.py alerts: one message when a problem starts, then at most one reminder per this many "
        "hours "
        "while it lasts (with the occurrences in between), one when it clears.",
        minimum=1,
        maximum=168,
    )
    disk_pct: int = opt(85, doc="Alert when the server filesystem is this full.", minimum=1, maximum=100)
    steal_pct: int = opt(10, doc="Alert when CPU steal exceeds this ...", minimum=1, maximum=100)
    steal_checks: int = opt(3, doc="... for this many checks in a row.", minimum=1)
    chunk_entities: int = opt(
        300,
        doc="Panel + Discord: a loaded chunk with at least this many entities is flagged (Experience tab "
        "'Busiest chunks'; one alert per chunk, again after 6 h if still heavy); 0 = off.",
        minimum=0,
    )
    backup_period_minutes: int = opt(
        60,
        doc="Watchdog: expected interval between backups (match backups.schedule); alerts after it + 30 min.",
        minimum=5,
        maximum=10080,
    )


@dataclass(frozen=True)
class Monitoring:
    metrics_interval_s: int = opt(60, doc="mc-agent sample interval.", minimum=30, maximum=300)
    retention_days: int = opt(90, doc="Metric retention in D1 (5-min rollups after 7 days).", minimum=1)
    logs: Logs = opt(factory=Logs, doc="Log shipping limits.")
    grafana: bool = opt(False, doc="On-server Grafana add-on (costs ~768 MB of heap).")
    alerts: Alerts = opt(factory=Alerts, doc="health.py and watchdog thresholds.")


@dataclass(frozen=True)
class ExperienceMessages:
    enabled: bool = opt(True, doc="Private in-game messages telling a player why they lagged and what helps.")
    per_player_minutes: int = opt(30, doc="At most one message per player per this many minutes.", minimum=5)
    broadcast_minutes: int = opt(
        30, doc="Server-wide lag: at most one short broadcast per this many minutes.", minimum=5
    )
    per_hour: int = opt(20, doc="At most this many messages per hour in all.", minimum=1, maximum=200)
    join_summary: bool = opt(True, doc="On join, a short note if the player's last session lagged.")


@dataclass(frozen=True)
class ExperienceRetention:
    raw_days: int = opt(14, doc="Raw 10 s samples in D1.", minimum=1, maximum=30)
    minute_days: int = opt(180, doc="1-minute rollups in D1.", minimum=7, maximum=400)
    hour_days: int = opt(730, doc="Hourly rollups in D1 (R2 keeps the raw archive forever).", minimum=30)


@dataclass(frozen=True)
class Experience:
    enabled: bool = opt(
        True,
        doc="P13 player-experience analytics: the in-house mc-telemetry plugin (/lag) and, in cloudflare "
        "mode, mc-agent shipping its snapshots to the Worker (verdicts, history, in-game messages).",
    )
    snapshot_seconds: int = opt(10, doc="Plugin snapshot interval.", minimum=5, maximum=60)
    plugin_budget_us: int = opt(
        100,
        doc="The plugin's main-thread budget per tick in microseconds (self-measured).",
        minimum=20,
        maximum=2000,
    )
    messages: ExperienceMessages = opt(factory=ExperienceMessages, doc="In-game messages.")
    retention: ExperienceRetention = opt(factory=ExperienceRetention, doc="Cloudflare D1 retention tiers.")
    segment_days: int = opt(
        30, doc="Days of compressed segments kept on the box (redundancy, backfill).", minimum=1
    )
    segment_cap_mb: int = opt(200, doc="Disk cap of the box's segments (oldest dropped first).", minimum=10)
    backfill_window: str | None = opt(
        doc="Extra local-time window (HH:MM-HH:MM) when undelivered data may be backfilled even if the "
        "server isn't idle-healthy; never while players are online. null = only when nobody is online.",
        pattern=WINDOW,
    )


@dataclass(frozen=True)
class Firewall:
    max_connections_per_source: int = opt(
        10,
        doc=(
            "Concurrent Minecraft connections per source (IPv4 address, IPv6 /64). Players behind one "
            "address (CGNAT, a LAN party, a school) share it: raise it for them."
        ),
        minimum=1,
        maximum=1000,
    )


@dataclass(frozen=True)
class AlertChannels:
    discord: bool = opt(False, doc="Discord alerts (needs TF_VAR_discord_webhook_url).")


@dataclass(frozen=True)
class Discord:
    commands: bool = opt(
        False,
        doc="Discord slash commands on the watchdog Worker (P14; panel.mode cloudflare): /status /players "
        "/lag "
        "/perf /backups /whitelist /rejected, and /backup now, /whitelist add. `mcctl setup` fills the ids.",
    )
    application_id: str | None = opt(doc="The Discord application's id (public).", pattern=SNOWFLAKE)
    public_key: str | None = opt(
        doc="The application's public key (hex; public: it only verifies Discord's signatures).",
        pattern=r"^[0-9a-f]{64}$",
    )
    guild_id: str | None = opt(doc="The one Discord server (guild) the commands work in.", pattern=SNOWFLAKE)
    channel_ids: list[str] = opt(
        factory=list, doc="Channels the commands work in (>= 1 with commands).", items={"pattern": SNOWFLAKE}
    )
    user_ids: list[str] = opt(
        factory=list,
        doc="Discord users allowed to use them (>= 1 with commands).",
        items={"pattern": SNOWFLAKE},
    )
    helper_user_ids: list[str] = opt(
        factory=list,
        doc="Helpers (v1.0.1): Discord users who may run the read commands, /whitelist list and "
        "/whitelist add (not /backup now), in the same channels.",
        items={"pattern": SNOWFLAKE},
    )
    helper_role_id: str | None = opt(
        doc="Helpers by role: members of the guild with this role id have the helper commands.",
        pattern=SNOWFLAKE,
    )
    ephemeral: bool = opt(True, doc="Replies only the caller sees (Discord's ephemeral messages).")


@dataclass(frozen=True)
class Automation:
    update_day: Weekday = opt(
        "Tue", doc="Day of the weekly update + deploy slot (04:17 UTC; the config repo's workflow schedule)."
    )
    reboot_window: str = opt(
        "04:30-06:00", doc="Window for automatic reboots, HH:MM-HH:MM local time.", pattern=WINDOW
    )
    auto_merge: Literal["none", "builds", "builds+patch"] = opt("builds+patch", doc="What merges unattended.")


@dataclass(frozen=True)
class Maintenance:
    window: str | None = opt(
        doc="v1.3: when a deploy may restart Minecraft, HH:MM-HH:MM in `timezone` (may cross midnight, "
        "e.g. 23:00-01:00). A deploy outside it applies the files and leaves the restart pending: a timer "
        "restarts the server inside the window (see force_after_minutes_before_end; any earlier restart "
        "applies it too), and Discord says "
        "so. A forced deploy (`mcctl deploy --force-restart`, the workflow's force_restart) restarts at "
        "once. null = a deploy restarts whenever it needs to.",
        pattern=WINDOW,
    )
    timezone: str | None = opt(
        doc="Time zone of the window (zoneinfo name); null = host.timezone.", check=_timezone
    )
    force_after_minutes_before_end: int = opt(
        30,
        doc="Inside the window a pending restart waits for an empty server (RCON `list`; unknown counts as "
        "occupied); in the window's last this-many minutes it restarts anyway, with the in-game countdown. "
        "Shorter than the window; 0 = never forced (the next window tries again).",
        minimum=0,
    )


@dataclass(frozen=True)
class Deploy:
    enabled: bool = opt(False, doc="Pushes to main deploy (mcctl flips it after bootstrap).")


# P17 branding (2026-11-02): server-side only, vanilla clients, nothing that restricts gameplay or farms.
MOTD_CODES = re.compile(r"[&\u00a7][0-9a-fk-orA-FK-OR]")
MOTD_MAX_VISIBLE = 59  # the server list's line width in default-font characters (vanilla's MOTD fits)
DEFAULT_RULES = [
    "Be kind.",
    "Don't grief or steal.",
    "No cheats or hacked clients (visual mods are fine).",
    "Have fun!",
]


def _motd_lines(v: list[str]) -> str | None:
    if len(v) > 2:
        return "at most 2 lines (the server list shows two)"
    for i, line in enumerate(v):
        visible = MOTD_CODES.sub("", line)
        if len(visible) > MOTD_MAX_VISIBLE:
            n = len(visible)
            return f"line {i + 1} has {n} visible characters; the server list shows {MOTD_MAX_VISIBLE}"
        if re.search(r"[\x00-\x1f\x7f]", line):
            return f"line {i + 1} has a control character"
        if re.search(r"\{[{%#]", line):
            return f"line {i + 1} has '{{{{', '{{%' or '{{#' (Ansible would template it)"
    return None


def _rules(v: list[str]) -> str | None:
    return "at most 20 rules" if len(v) > 20 else None


def _lore(v: list[str]) -> str | None:
    return "at most 4 lore lines" if len(v) > 4 else None


# mc-brand's own commands (plugin/mc-brand/src/main/resources/plugin.yml): the drink's command can't take one.
BRAND_COMMANDS = (
    *("rules", "discord", "hud", "mcbrand", "modpack", "view"),
    *("compass", "map", "help", "commands"),  # v1.3
)


def _drink_command(v: str) -> str | None:
    return f"{v!r} is one of mc-brand's own commands" if v in BRAND_COMMANDS else None


@dataclass(frozen=True)
class BrandingColors:
    # A neutral palette: white text, sky-blue accents, grey hints; no gold.
    text: str = opt("#FFFFFF", doc="Primary text (greetings, rules, values): #RRGGBB.", pattern=HEX_COLOR)
    accent: str = opt(
        "#55AAFF", doc="Accent (the server name, player names, links): #RRGGBB.", pattern=HEX_COLOR
    )
    secondary: str = opt("#2E6FB0", doc="Secondary accent (separators): #RRGGBB.", pattern=HEX_COLOR)
    subtle: str = opt(
        "#C8D0D8", doc="Subtle text (hints, labels, lore, 'last seen'): #RRGGBB.", pattern=HEX_COLOR
    )


@dataclass(frozen=True)
class BrandingJoin:
    title: bool = opt(True, doc="Show a title (the server name) and a subtitle on join.")
    subtitle_first: str = opt("Welcome, {player}!", doc="Subtitle on a first join.", pattern=PLAYER_TEXT)
    subtitle_back: str = opt("Welcome back, {player}!", doc="Subtitle on a later join.", pattern=PLAYER_TEXT)
    first_join: str = opt(
        "Welcome to {server}, {player}! Make yourself at home.",
        doc="Chat greeting on a player's first join. Placeholders: {player}, {server}, {glyph}.",
        pattern=PLAYER_TEXT,
    )
    welcome_back: str = opt(
        "Welcome back, {player}! Last seen {ago} ago.",
        doc="Chat greeting on later joins. Placeholders: {player}, {server}, {ago} (e.g. '3 days'), {glyph}.",
        pattern=PLAYER_TEXT,
    )
    sound: str | None = opt(
        "minecraft:entity.player.levelup",
        doc="Sound event played to the joining player (a vanilla sound event id); null = none.",
        pattern=NAMESPACED_KEY,
    )


@dataclass(frozen=True)
class BrandingHud:
    enabled: bool = opt(
        True,
        doc="Players may choose a HUD with /hud top|side|off: a one-line boss bar at the top (server "
        "health, speed, tick time, their connection) or the sidebar (players + ping as well).",
    )
    default_mode: Literal["top", "side", "off"] | None = opt(
        doc="v1.0.1: the HUD of players who never used /hud: top (boss bar), side (sidebar) or off.",
        derived="side if default_on is true, else top",
    )
    default_on: bool = opt(
        False, doc="Before v1.0.1: true = the sidebar for players who never used /hud (default_mode wins)."
    )
    interval_seconds: int = opt(5, doc="Sidebar/tab list refresh (never per tick).", minimum=2, maximum=30)
    max_players: int = opt(
        8, doc="Player names shown in the sidebar (then '+N more').", minimum=1, maximum=10
    )
    tab_list: bool = opt(True, doc="Branded tab list header and footer (players online, TPS).")
    settle_seconds: int = opt(
        10,
        doc="v1.1: a player who just joined sees 'Settling in…' instead of the server's health (their ping "
        "and "
        "the chunks around them are still loading) for at least this long and until their ping reads steady.",
        minimum=3,
        maximum=30,
    )


@dataclass(frozen=True)
class BrandingDrink:
    enabled: bool = opt(
        False,
        doc="The signature drink: a never-empty potion item (one per player, /<command>) that gives a short "
        "Nausea; cosmetic only.",
    )
    name: str = opt(doc="Item name.", derived="<branding.name> Reserve", pattern=PLAYER_TEXT)
    command: str = opt(
        "drink",
        doc="v1.2: the command that hands out the drink (/<command>), 3-16 lowercase letters; not one of "
        "mc-brand's own (rules, discord, hud, mcbrand, modpack, view, compass, map, help, commands).",
        pattern=r"^[a-z]{3,16}$",
        check=_drink_command,
        json={"not": {"enum": list(BRAND_COMMANDS)}},
    )
    texture: str | None = opt(
        doc="v1.2: the drink's item texture in the resource pack: a 16x16 PNG (<= 8 KB) in this config repo "
        "(path relative to it), used byte for byte; null = the product's neutral art.",
        pattern=REPO_PNG,
    )
    lore: list[str] = opt(
        factory=lambda: ["Never runs dry."],
        doc="Item lore lines (<= 4).",
        check=_lore,
        items={"pattern": PLAYER_TEXT},
    )
    color: str = opt(
        "#3A6EA5", doc="Potion colour (shown without the resource pack): #RRGGBB.", pattern=HEX_COLOR
    )
    cooldown_seconds: int = opt(30, doc="Seconds between two sips.", minimum=5, maximum=600)
    nausea_seconds: int = opt(10, doc="Nausea per sip (0 = none).", minimum=0, maximum=30)
    give_on_first_join: bool = opt(
        True,
        doc="Every player who never had the drink gets one when they join (v1.0.1: not only on the first "
        "join, so players from before the drink get theirs).",
    )


HOLOGRAM_PLACEHOLDERS = ("server", "players", "tps", "glyph")


def _hologram_lines(v: list[str]) -> str | None:
    if not 1 <= len(v) <= 8:
        return "1 to 8 lines"
    for line in v:
        for name in re.findall(r"\{([^{}]*)\}", line):
            if name not in HOLOGRAM_PLACEHOLDERS:
                return f"unknown placeholder {{{name}}} (one of {', '.join(HOLOGRAM_PLACEHOLDERS)})"
    return None


def _holograms(v: list[Any]) -> str | None:
    return "at most 10 holograms" if len(v) > 10 else None


@dataclass(frozen=True)
class BrandingHologram:
    x: int | None = opt(doc="Block x: the text floats over the block's centre (x, y and z, or `at: spawn`).",
                        minimum=-29999984, maximum=29999984)  # fmt: skip
    y: int | None = opt(doc="Block y (the text's centre is half a block above it).", minimum=-64,
                        maximum=319)  # fmt: skip
    z: int | None = opt(doc="Block z.", minimum=-29999984, maximum=29999984)
    at: Literal["spawn"] | None = opt(
        doc="v1.3: `spawn` instead of x/y/z: 3 blocks above the dimension's world spawn (found when the "
        "hologram spawns; it follows /setworldspawn).",
    )
    world: Literal["overworld", "nether", "end"] = opt("overworld", doc="The dimension.")
    lines: list[str] = opt(
        factory=lambda: ["Welcome to {server}"],
        doc="The text, one entry per line (<= 8). Placeholders: {server}, {players} (online now), {tps}, "
        "{glyph} (the resource pack's glyph, only for players with the pack).",
        check=_hologram_lines,
        items={"pattern": PLAYER_TEXT},
    )
    health: bool = opt(
        False, doc="Add a live line with the server's health word (Smooth / A bit busy / Laggy)."
    )
    help: bool = opt(
        False,
        doc="v1.3: append the list of commands every player may use (the same list as /help: built from the "
        "enabled features, never out of sync).",
    )


@dataclass(frozen=True)
class BrandingCompass:
    enabled: bool = opt(
        True,
        doc="v1.3: the friend compass, a boss bar that turns with the player: cardinal points and online "
        "players' names at their bearing; the one faced shows the distance and up/down arrows; players in "
        "other dimensions as 'name (Nether)'. /compass on|off|hide|show|auto. false = no compass and no "
        "/compass.",
    )
    default_on: bool = opt(
        True,
        doc="v1.3: shown without /compass on, except for clients with both the Smallworld mod and Xaero's "
        "maps (their friends are on Xaero's maps). false = only for players who type /compass on.",
    )
    max_range: int = opt(
        1000, doc="v1.3: players farther away (blocks, horizontally) aren't on the compass.", minimum=16,
        maximum=100000,
    )  # fmt: skip
    distance_rounding: int = opt(
        5, doc="v1.3: the faced player's distance is rounded to a multiple of this (blocks).", minimum=1,
        maximum=100,
    )  # fmt: skip
    height_threshold: int = opt(
        8, doc="v1.3: up/down arrows when a player is more than this many blocks above/below.", minimum=2,
        maximum=64,
    )  # fmt: skip


@dataclass(frozen=True)
class BrandingPocketMap:
    enabled: bool = opt(
        True,
        doc="v1.3: /map gives each player a personal map item that re-centres on them while held (the off "
        "hand makes it a minimap), with every online player as a named cursor; /map zoom cycles the scale. "
        "Terrain fills in as the player explores (vanilla's map rendering). Free to re-get, can't be copied. "
        "Players without client mods get a one-line tip at join.",
    )


@dataclass(frozen=True)
class BrandingResourcePack:
    enabled: bool = opt(
        False,
        doc="Offer a small resource pack on join (the drink's texture, the {glyph} icon and the server icon: "
        "this config repo's own PNGs byte for byte, else the product's neutral art; built by `mcctl render`, "
        "optional for players, served by the watchdog Worker at hb.<hostname>.<zone>/pack/<sha1>.zip). Run "
        "`mcctl apply` once after turning it on or changing its art (Terraform uploads the pack).",
    )
    prompt: str = opt(
        "Textures for this server's items (optional)",
        doc="Text on the client's download prompt.",
        pattern=PLAYER_TEXT,
    )
    description: str = opt(
        "Item textures for this server's mc-brand plugin",
        doc="v1.2: the pack's description (pack.mcmeta; shown in the client's resource pack list).",
        pattern=PLAYER_TEXT,
    )
    glyph: str | None = opt(
        doc="v1.2: the {glyph} icon in chat texts: an 8x8 PNG (<= 8 KB) in this config repo (path relative "
        "to it), used byte for byte; null = the product's neutral glyph. Players without the pack see no "
        "glyph.",
        pattern=REPO_PNG,
    )


@dataclass(frozen=True)
class Branding:
    enabled: bool = opt(
        True,
        doc="P17: the in-house mc-brand plugin (join greeting, /rules, /discord, server links, tab list, "
        "/hud, the optional drink) and the server-list icon. false = none of it (the MOTD stays "
        "minecraft.motd).",
    )
    name: str = opt(
        doc="The server's name in titles, the tab list, the sidebar and the panel.",
        derived="minecraft.motd without formatting codes",
        pattern=r"^(?!.*\{[{%#])[^\x00-\x1f\x7f]{1,40}$",
    )
    colors: BrandingColors = opt(factory=BrandingColors, doc="Colours (#RRGGBB) of the in-game texts.")
    motd: list[str] = opt(
        factory=list,
        doc="Server-list message, up to 2 lines; '&' or '\u00a7' + 0-9/a-f/k-o/r are Minecraft's 16 colours "
        "and styles (the pinned server's MOTD has no hex colours). Empty = minecraft.motd.",
        check=_motd_lines,
    )
    icon: str | None = opt(
        doc="A 64x64 PNG in this config repo (path relative to it) for the server list; null = the "
        "product's icon.",
        pattern=REPO_PNG,
    )
    rules: list[str] = opt(
        factory=lambda: list(DEFAULT_RULES),
        doc="/rules, numbered (<= 20). Keep them short and friendly. {glyph} in a rule or a greeting is the "
        "resource pack's glyph (resource_pack.glyph) for players who have the pack, nothing for the rest.",
        check=_rules,
        items={"pattern": PLAYER_TEXT},
    )
    discord_invite: str | None = opt(
        doc="Discord invite (https://discord.gg/...): /discord and the pause menu's server links.",
        pattern=DISCORD_INVITE,
    )
    rules_url: str | None = opt(doc="A web page with the rules (server links).", pattern=HTTPS_URL)
    website: str | None = opt(doc="The server's website (server links).", pattern=HTTPS_URL)
    join: BrandingJoin = opt(factory=BrandingJoin, doc="Join title, greeting and sound.")
    hud: BrandingHud = opt(factory=BrandingHud, doc="The HUD (top bar or sidebar) and the tab list.")
    drink: BrandingDrink = opt(factory=BrandingDrink, doc="The signature drink item (off by default).")
    resource_pack: BrandingResourcePack = opt(
        factory=BrandingResourcePack,
        doc="The optional server resource pack (the drink's texture, the glyph).",
    )
    holograms: list[BrandingHologram] = opt(
        factory=list,
        doc="v1.2: floating texts (e.g. at spawn), text display entities for vanilla clients; at most 10.",
        check=_holograms,
    )
    compass: BrandingCompass = opt(factory=BrandingCompass, doc="v1.3: the friend compass (a boss bar).")
    pocket_map: BrandingPocketMap = opt(factory=BrandingPocketMap, doc="v1.3: /map, the pocket map.")


CLIENT_TIERS = ("low", "medium", "high", "maximum")


def _client_tiers(v: list[str]) -> str | None:
    if not v:
        return "needs at least one tier"
    if len(set(v)) != len(v):
        return "duplicate tiers"
    return None


@dataclass(frozen=True)
class ClientView:
    enabled: bool = opt(
        True,
        doc="v1.1: /view <n>: every player may raise their own view distance up to `max` chunks (mc-brand). "
        "Only while the server's health is Smooth; lowered back to the server's view distance automatically "
        "when it isn't, and raised again once it is. Lowering is always allowed. Kept per player (UUID).",
    )
    max: int = opt(16, doc="Highest /view a player may ask for (chunks).", minimum=2, maximum=32)
    check_seconds: int = opt(
        5,
        doc="How often mc-brand checks the server's health for raised view distances.",
        minimum=2,
        maximum=60,
    )


@dataclass(frozen=True)
class ClientFps:
    interval_seconds: int = opt(
        5,
        doc="v1.2: how often the Smallworld client mod reports the player's FPS (smallworld:fps; the "
        "Experience tab's 'server fine / your PC' diagnosis). Sent to the mod at every join "
        "(smallworld:config).",
        minimum=2,
        maximum=60,
    )
    hud: bool = opt(
        True,
        doc="v1.2: the client mod draws the FPS next to the top HUD bar (each player can still hide it in "
        "config/smallworld.json).",
    )


@dataclass(frozen=True)
class ClientMap:
    players: bool = opt(
        True,
        doc="v1.2: every online player shows on the others' Xaero's Minimap and World Map at any distance in "
        "the same dimension (mc-brand speaks Xaero's own tracked-player channel; spectators and invisible "
        "players are left out).",
    )
    fair_play: bool = opt(
        True,
        doc="v1.2: Xaero's fair-play mode for everyone with the mod: no cave map, no entity radar (the "
        "players above still show).",
    )


@dataclass(frozen=True)
class Client:
    enabled: bool = opt(
        False,
        doc="v1.1: the optional client modpack (Fabric + Sodium & co., four tiers, client/pack.yml) and its "
        "setup page at <hostname>.<zone>, opened with a one-time code from /modpack in game. Needs "
        "panel.mode cloudflare and branding.enabled, and a client.lock.yml (`mcctl config set "
        "client.enabled true` writes the first one; or `mcctl lock update --client`). Run `mcctl apply` "
        "once after turning it on (the Worker's client bindings, the page's host, the artifact mirror): a "
        "deploy alone isn't enough.",
    )
    hostname: str = opt(
        "play", doc="The setup page's record name: <hostname>.<zone> (e.g. play.example.com).", pattern=LABEL
    )
    prompt: bool = opt(
        True,
        doc="Remind players without the pack at every join, with a click to /modpack (v1.2: exact, the "
        "pack's client mod says hello; players with an outdated pack are told so). Each player can switch "
        "the reminder off with /modpack off.",
    )
    tiers: list[Literal["low", "medium", "high", "maximum"]] = opt(
        factory=lambda: list(CLIENT_TIERS),
        doc="The tiers offered on the setup page (Low, Medium, High, Maximum; Medium is recommended).",
        check=_client_tiers,
    )
    view: ClientView = opt(factory=ClientView, doc="Per-player view distance (/view).")
    fps: ClientFps = opt(factory=ClientFps, doc="v1.2: the client mod's FPS reports and FPS display.")
    map: ClientMap = opt(factory=ClientMap, doc="v1.2: Xaero's maps in the pack (server side, mc-brand).")


@dataclass(frozen=True)
class ServerConfig:
    schema_version: int = opt(
        doc="server.yml schema version.",
        required=True,
        check=_schema_version,
        json={"enum": list(KNOWN_SCHEMA_VERSIONS)},
    )
    name: str = opt(
        doc="Deployment slug; derives bucket, Worker, tag and host names.", required=True, pattern=SLUG
    )
    admin: Admin = opt(doc="The server's administrator.", required=True)
    dns: Dns = opt(doc="Public DNS record for the server.", required=True)
    cloudflare: CloudflareSettings = opt(factory=CloudflareSettings, doc="Cloudflare account access.")
    minecraft: Minecraft = opt(doc="Minecraft server settings.", required=True)
    github: GitHub = opt(factory=GitHub, doc="Config repo.")
    state: State = opt(factory=State, doc="Terraform state location.")
    host: Host = opt(factory=Host, doc="The machine.")
    world: World = opt(factory=World, doc="World settings.")
    gameplay: Gameplay = opt(factory=Gameplay, doc="Vanilla-behaviour switches.")
    performance: Performance = opt(factory=Performance, doc="Performance tuning.")
    jvm: Jvm = opt(factory=Jvm, doc="JVM settings.")
    backups: Backups = opt(factory=Backups, doc="restic backups to R2.")
    plugins: list[Plugin] = opt(factory=_default_plugins, doc="Plugins (the lock tool pins them).")
    panel: Panel = opt(factory=Panel, doc="Admin panel.")
    agent: Agent = opt(factory=Agent, doc="v1.4: the on-box agent's implementation.")
    monitoring: Monitoring = opt(factory=Monitoring, doc="Metrics, logs and alert thresholds.")
    experience: Experience = opt(factory=Experience, doc="Player-experience analytics (P13).")
    branding: Branding = opt(
        factory=Branding, doc="Server name, colours, MOTD, icon, greetings, rules (P17)."
    )
    firewall: Firewall = opt(factory=Firewall, doc="Host firewall (nftables) limits.")
    client: Client = opt(factory=Client, doc="v1.1: the client modpack, its setup page, /view.")
    alerts: AlertChannels = opt(factory=AlertChannels, doc="Alert channels.")
    discord: Discord = opt(factory=Discord, doc="Discord slash commands (P14).")
    automation: Automation = opt(factory=Automation, doc="Unattended updates and reboots.")
    maintenance: Maintenance = opt(
        factory=Maintenance, doc="v1.3: the window in which deploys may restart Minecraft."
    )
    deploy: Deploy = opt(factory=Deploy, doc="CI deploys.")


# --- players.yml ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Player:
    name: str = opt(doc="Minecraft name.", required=True, pattern=PLAYER_NAME)
    uuid: str = opt(doc="Player UUID (lowercase, dashed).", required=True, pattern=UUID)


@dataclass(frozen=True)
class Op:
    name: str = opt(doc="Minecraft name.", required=True, pattern=PLAYER_NAME)
    uuid: str = opt(doc="Player UUID (lowercase, dashed).", required=True, pattern=UUID)
    level: int = opt(doc="Op permission level.", required=True, minimum=1, maximum=4)


@dataclass(frozen=True)
class Players:
    whitelist: list[Player] = opt(factory=list, doc="Whitelisted players.")
    ops: list[Op] = opt(factory=list, doc="Operators (must also be whitelisted).")


# --- generic loader ---------------------------------------------------------------------------------


def _join(path: str, key: object) -> str:
    return f"{path}.{key}" if path else str(key)


def _is_union(tp: Any) -> bool:
    return get_origin(tp) in (Union, types.UnionType)


def _describe(tp: Any) -> str:
    if _is_union(tp):
        return " or ".join(_describe(a) for a in get_args(tp))
    if get_origin(tp) is Literal:
        return "one of " + ", ".join(repr(a) for a in get_args(tp))
    if dataclasses.is_dataclass(tp):
        return "a mapping"
    names = {
        str: "a string",
        int: "an integer",
        bool: "true/false",
        type(None): "null",
        list: "a list",
        Any: "a value",
    }
    return names.get(get_origin(tp) or tp, "a mapping")


def _load_dataclass(cls: Any, data: Any, path: str, errors: list[str]) -> Any:
    if not isinstance(data, dict):
        errors.append(f"{path or '<root>'}: expected a mapping, got {type(data).__name__}")
        return None
    hints = get_type_hints(cls)
    fields = {f.name: f for f in dataclasses.fields(cls)}
    for key in data:
        if key not in fields:
            hint = difflib.get_close_matches(str(key), fields, n=1)
            errors.append(
                f"{_join(path, key)}: unknown key" + (f" (did you mean {hint[0]!r}?)" if hint else "")
            )
    kwargs: dict[str, Any] = {}
    for name, fl in fields.items():
        p = _join(path, name)
        if name in data:
            if data[name] is None and fl.metadata.get("derived"):
                errors.append(f"{p}: must not be null (leave it out for the default)")
                continue
            kwargs[name] = _load_value(hints[name], data[name], p, fl.metadata, errors)
        elif fl.metadata.get("required"):
            errors.append(f"{p}: required")
    try:
        return cls(**kwargs)
    except TypeError:  # pragma: no cover - only reachable with a broken schema definition
        return None


def _load_value(tp: Any, v: Any, path: str, rules: dict[str, Any], errors: list[str]) -> Any:
    origin = get_origin(tp)
    if _is_union(tp):
        args = get_args(tp)
        if v is None:
            if type(None) in args:
                return None
            errors.append(f"{path}: must not be null")
            return None
        for arg in (a for a in args if a is not type(None)):
            sub: list[str] = []
            out = _load_value(arg, v, path, rules, sub)
            if not sub:
                return out
            if _matches_type(arg, v):  # right type, wrong value: report the specific problem
                errors.extend(sub)
                return None
        errors.append(f"{path}: expected {_describe(tp)}, got {v!r}")
        return None
    if origin is Literal:
        allowed = get_args(tp)
        if v is False and "off" in allowed:  # YAML 1.1 reads a bare `off` as false
            v = "off"
        if isinstance(v, str) and v in allowed:
            return v
        errors.append(f"{path}: expected {_describe(tp)}, got {v!r}")
        return None
    if dataclasses.is_dataclass(tp):
        return _load_dataclass(tp, v, path, errors)
    if not _matches_type(tp, v):
        hint = " (quote it)" if tp is str and isinstance(v, int | float) else ""
        errors.append(f"{path}: expected {_describe(tp)}, got {v!r}{hint}")
        return None
    if origin is list:
        (item_tp,) = get_args(tp)
        out_list = [
            _load_value(item_tp, x, f"{path}[{i}]", rules.get("items", {}), errors) for i, x in enumerate(v)
        ]
        if len(v) < rules.get("min_items", 0):
            errors.append(f"{path}: needs at least {rules['min_items']} item(s)")
        if "check" in rules and (msg := rules["check"](v)):  # a rule on the whole list (P17: lengths)
            errors.append(f"{path}: {msg}")
        return out_list
    if origin is dict:
        _, val_tp = get_args(tp)
        out_dict = {}
        for k, x in v.items():
            if not isinstance(k, str) or ("keys" in rules and not re.fullmatch(rules["keys"], k)):
                errors.append(f"{_join(path, k)}: invalid key {k!r}")
                continue
            out_dict[k] = _load_value(val_tp, x, _join(path, k), rules.get("values", {}), errors)
        return out_dict
    _check_scalar(v, path, rules, errors)
    return v


def _matches_type(tp: Any, v: Any) -> bool:
    origin = get_origin(tp) or tp
    if tp is Any:
        return v is not None
    if origin is Literal:
        return isinstance(v, str)
    if tp is bool:
        return isinstance(v, bool)
    if tp is int:
        return isinstance(v, int) and not isinstance(v, bool)
    if tp is str:
        return isinstance(v, str)
    if dataclasses.is_dataclass(tp):
        return isinstance(v, dict)
    return isinstance(v, origin)


def _check_scalar(v: Any, path: str, rules: dict[str, Any], errors: list[str]) -> None:
    if isinstance(v, str) and "pattern" in rules and not re.fullmatch(rules["pattern"], v):
        errors.append(f"{path}: {v!r} doesn't match {rules['pattern']}")
    if isinstance(v, int) and not isinstance(v, bool):
        if "minimum" in rules and v < rules["minimum"]:
            errors.append(f"{path}: must be >= {rules['minimum']}, got {v}")
        if "maximum" in rules and v > rules["maximum"]:
            errors.append(f"{path}: must be <= {rules['maximum']}, got {v}")
    if "check" in rules and (msg := rules["check"](v)):
        errors.append(f"{path}: {msg}")


# --- server.yml: derived defaults and cross-field rules ---------------------------------------------


def parse_github_remote(url: str) -> tuple[str, str] | None:
    """(owner, repo) from a GitHub remote URL (https or ssh form), else None."""
    m = re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+)/(.+?)(?:\.git)?/?", url
    )
    return (m.group(1), m.group(2)) if m else None


# Profile values of the fields marked `derived=PROFILE` (design §8 "Performance profile"). vanilla = the
# pinned server's own defaults wherever they are vanilla (verified against a default config the pinned jar
# generated), activation ranges 0 (= never inactive: io.papermc.paper.entity.activation.ActivationRange
# initializeEntityActivationState, range <= 0). balanced = the product's values before the profile existed.
PROFILES: dict[str, dict[str, Any]] = {
    "vanilla": {
        "view_distance": 10,
        "simulation_distance": 8,
        "spawn_limits": {
            "monsters": 70,
            "animals": 10,
            "water_animals": 5,
            "water_ambient": 20,
            "ambient": 15,
        },
        "activation_range": dict.fromkeys(
            ("animals", "monsters", "raiders", "misc", "water", "villagers", "flying_monsters"), 0
        ),
        "despawn_range": {"soft": 32, "hard": 128},
        "villager_search_radius": 48,
        "alt_item_despawn_ticks": {},
        "redstone": "VANILLA",
    },
    "balanced": {
        "view_distance": 8,
        "simulation_distance": 5,
        "spawn_limits": {"monsters": 50, "animals": 8, "water_animals": 3, "water_ambient": 1, "ambient": 1},
        "activation_range": {
            "animals": 16,
            "monsters": 24,
            "raiders": 48,
            "misc": 8,
            "water": 8,
            "villagers": 16,
            "flying_monsters": 48,
        },
        "despawn_range": {"soft": 30, "hard": 56},
        "villager_search_radius": 32,
        "alt_item_despawn_ticks": _alt_item_despawn_balanced(),
        "redstone": "ALTERNATE_CURRENT",
    },
}


def _fill(obj: Any, values: dict[str, Any]) -> Any:
    """obj with every None field taken from values (nested dataclasses field by field)."""
    changes: dict[str, Any] = {}
    for fl in dataclasses.fields(obj):
        if fl.name not in values:
            continue
        cur = getattr(obj, fl.name)
        if dataclasses.is_dataclass(cur):
            changes[fl.name] = _fill(cur, values[fl.name])
        elif cur is None:
            v = values[fl.name]
            changes[fl.name] = dict(v) if isinstance(v, dict) else v
    return dataclasses.replace(obj, **changes)


def _derive_profile(cfg: ServerConfig) -> ServerConfig:
    perf = cfg.performance
    values = dict(PROFILES[perf.profile])
    if perf.view_distance is not None and perf.simulation_distance is None:
        # only the view distance set: the profile's simulation distance never exceeds it
        values["simulation_distance"] = min(values["simulation_distance"], perf.view_distance)
    gameplay = cfg.gameplay
    if gameplay.redstone is None:
        gameplay = dataclasses.replace(gameplay, redstone=values["redstone"])
    return dataclasses.replace(cfg, performance=_fill(perf, values), gameplay=gameplay)


def _derive(cfg: ServerConfig, git_origin: tuple[str, str] | None, errors: list[str]) -> ServerConfig:
    gh = cfg.github
    if gh.owner is None or gh.config_repo is None:
        if git_origin is None:
            errors.append(
                "github: owner/config_repo not set and no GitHub `origin` remote to derive them from"
            )
        else:
            gh = dataclasses.replace(
                gh, owner=gh.owner or git_origin[0], config_repo=gh.config_repo or git_origin[1]
            )
    access = cfg.panel.access
    if access.emails is None:
        access = dataclasses.replace(access, emails=[cfg.admin.email] if cfg.admin.email else [])
    cfg = dataclasses.replace(
        cfg,
        github=gh,
        state=dataclasses.replace(cfg.state, bucket=cfg.state.bucket or f"{cfg.name}-tfstate"),
        host=dataclasses.replace(
            cfg.host,
            tailscale_name=cfg.host.tailscale_name or cfg.name,
            # Compressed form, so `2001:DB8:0::1` and `2001:db8::1` render the same AAAA record (no DNS diff).
            ipv6=None if cfg.host.ipv6 is None else str(ipaddress.IPv6Address(cfg.host.ipv6)),
        ),
        minecraft=dataclasses.replace(
            cfg.minecraft,
            motd=f"{cfg.admin.name}'s server" if cfg.minecraft.motd is None else cfg.minecraft.motd,
        ),
        backups=dataclasses.replace(cfg.backups, bucket=cfg.backups.bucket or f"{cfg.name}-mc-backups"),
        panel=dataclasses.replace(cfg.panel, access=access),
        branding=_derive_branding(cfg),
    )
    return _derive_profile(cfg)


def plain_motd(motd: str) -> str:
    """The first line of a MOTD without Minecraft formatting codes."""
    return MOTD_CODES.sub("", motd).replace("\\n", "\n").split("\n")[0].strip()


def _derive_branding(cfg: ServerConfig) -> Branding:
    b = cfg.branding
    motd = f"{cfg.admin.name}'s server" if cfg.minecraft.motd is None else cfg.minecraft.motd
    name = b.name if b.name is not None else (plain_motd(motd)[:40] or f"{cfg.admin.name}'s server"[:40])
    drink = b.drink
    if drink.name is None:
        drink = dataclasses.replace(drink, name=f"{name} Reserve"[:200])
    hud = b.hud
    if hud.default_mode is None:
        hud = dataclasses.replace(hud, default_mode="side" if hud.default_on else "top")
    return dataclasses.replace(b, name=name, drink=drink, hud=hud)


def _check_derived(cfg: ServerConfig, errors: list[str]) -> None:
    """Re-check every derived field against its own rules (e.g. `<name>-mc-backups` must be a bucket name)."""
    for parent_path, parent in (
        ("github", cfg.github),
        ("state", cfg.state),
        ("host", cfg.host),
        ("backups", cfg.backups),
        ("panel.access", cfg.panel.access),
        ("branding", cfg.branding),
        ("branding.drink", cfg.branding.drink),
    ):
        for fl in dataclasses.fields(parent):
            value = getattr(parent, fl.name)
            if not fl.metadata.get("derived") or value is None:
                continue
            path = f"{parent_path}.{fl.name}"
            values = value if isinstance(value, list) else [value]
            for v in values:
                sub: list[str] = []
                _check_scalar(v, path, fl.metadata.get("items", fl.metadata), sub)
                errors.extend(f"{e} (derived; set {path} explicitly)" for e in sub)


def key_is_known(path: str, keys: set[str]) -> bool:
    """Same rule as tests/test_overlays.py: children of a map-typed setting with no listed children pass."""
    if path in keys:
        return True
    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        anc = ".".join(parts[:i])
        if anc in keys:
            return not any(k.startswith(anc + ".") for k in keys)
    return False


def _leaf_paths(d: dict[str, Any], prefix: str = "") -> list[str]:
    """Dotted paths of the leaves; an empty mapping is a leaf too (reported as such by the caller)."""
    out: list[str] = []
    for k, v in d.items():
        p = f"{prefix}{k}"
        out += _leaf_paths(v, p + ".") if isinstance(v, dict) and v else [p]
    return out


def _value_at(d: dict[str, Any], path: str) -> Any:
    for part in path.split("."):
        d = d[part]
    return d


# Free-plan budget (§5a). Validation rejects settings above ~half of a daily cap; 0.55 leaves room for the
# spec's max_rows_per_day ceiling (50000) at the fastest sample interval (30 s).
BUDGET_FRACTION = 0.55
WORKER_REQUESTS_PER_DAY = 100_000
D1_ROWS_WRITTEN_PER_DAY = 100_000
LOG_BATCHES_PER_DAY = 288  # one per 5 min (mc-agent LOG_BATCH_S): an R2 archive upload + a D1 rows post each
AGENT_POLLS_PER_DAY = 5760  # worst case: the WebSocket is down all day and the agent polls every 15 s
ROLLUP_ROWS_PER_DAY = 2 * 288  # the Worker's 5-min rollup: one row written, one expired row deleted per 5 min
HEARTBEATS_PER_DAY = 340
PANEL_REQUESTS_PER_DAY = 3000  # generous: one admin browsing (P9: one poll per 15 s while a tab is open)
INVENTORY_PER_DAY = 144  # mc-agent's whitelist/bans/plugins snapshot every 10 min (P9): a request + an upsert
AUDIT_WATCHDOG_ROWS_PER_DAY = 1000
# P10: job reports (kept in the Control Durable Object, no D1 rows) and the inventory pushes after panel
# actions
# (one D1 upsert each, at most one per 5 s): = mc_agent.PANEL_REQUESTS / PANEL_ROWS.
PANEL_ACTIONS_PER_DAY = 400
PANEL_ACTION_ROWS_PER_DAY = 100
# P13 (experience.enabled, cloudflare mode): one minute row + one segment upload per 5 min (requests); per
# minute the raw row + the 1-minute summary, plus verdict changes and lag events (capped at 500/day each, with
# their daily counter), sessions, messages and the hourly rollups (~600). = mc_agent.EXPERIENCE_REQUESTS /
# _ROWS.
EXPERIENCE_REQUESTS_PER_DAY = 1440 + 288
EXPERIENCE_ROWS_PER_DAY = 2 * 1440 + 2 * 2 * 500 + 600
# v1.1 (client.enabled): mc-agent's one-time codes (rate-limited per player), the setup page and its downloads
# (each pack download: the page, a few API calls, ~10 mod files through the mirror), the codes, sessions, rate
# counters and the download audit (generous for a friends' server). = mc_agent.CLIENT_REQUESTS / _ROWS for the
# agent's share.
CLIENT_REQUESTS_PER_DAY = 3000
CLIENT_ROWS_PER_DAY = 1500


def budget(cfg: ServerConfig) -> dict[str, tuple[int, int]]:
    """Estimated daily use vs cap for the cloudflare-mode Free-plan resources, as built in P8 (mc-agent +
    Worker ingest; tests/test_agent.py keeps this >= mc_agent.daily_budget). Sample and log day tables are
    dropped whole after retention, which writes no rows."""
    samples = 86400 // cfg.monitoring.metrics_interval_s
    requests = (
        samples
        + 2 * LOG_BATCHES_PER_DAY
        + AGENT_POLLS_PER_DAY
        + HEARTBEATS_PER_DAY
        + PANEL_REQUESTS_PER_DAY
        + INVENTORY_PER_DAY
        + PANEL_ACTIONS_PER_DAY
        + (EXPERIENCE_REQUESTS_PER_DAY if cfg.experience.enabled else 0)
        + (CLIENT_REQUESTS_PER_DAY if cfg.client.enabled else 0)
    )
    rows = (
        samples
        + cfg.monitoring.logs.max_rows_per_day
        + LOG_BATCHES_PER_DAY  # the per-day row counter
        + INVENTORY_PER_DAY
        + ROLLUP_ROWS_PER_DAY
        + AUDIT_WATCHDOG_ROWS_PER_DAY
        + PANEL_ACTION_ROWS_PER_DAY
        + (EXPERIENCE_ROWS_PER_DAY if cfg.experience.enabled else 0)
        + (CLIENT_ROWS_PER_DAY if cfg.client.enabled else 0)
    )
    return {
        "Worker requests/day": (requests, WORKER_REQUESTS_PER_DAY),
        "D1 rows written/day": (rows, D1_ROWS_WRITTEN_PER_DAY),
    }


def _calendar_ok(spec: str) -> bool | None:
    """systemd-analyze's verdict on an OnCalendar spec; None when systemd-analyze isn't installed."""
    if spec.startswith("-"):  # never let a value act as an option (the pattern rejects it too)
        return False
    exe = shutil.which("systemd-analyze")
    if exe is None:
        return None
    return subprocess.run([exe, "calendar", "--", spec], capture_output=True, check=False).returncode == 0


NOT_CONFIG_FILES = {"gamerules", "rcon-log"}  # keys/<version>/*.txt that aren't overridable config files
GAMEPLAY_RULES = {"locator_bar": "locator_bar", "players_sleeping_percentage": "sleeping_percentage"}


def _cross_validate(cfg: ServerConfig, product_root: Path, i_know: bool, errors: list[str]) -> None:
    mc, perf = cfg.minecraft, cfg.performance
    keys_dir = product_root / "server-config" / "keys" / mc.version
    if not keys_dir.is_dir():
        have = sorted(p.name for p in (product_root / "server-config" / "keys").glob("*") if p.is_dir())
        errors.append(
            f"minecraft.version: {mc.version!r} isn't supported by this product version (have {have})"
        )
    else:
        for file, settings in mc.overrides.items():
            keyfile = keys_dir / f"{file}.txt"
            if not keyfile.is_file() or file in NOT_CONFIG_FILES:
                files = sorted(p.stem for p in keys_dir.glob("*.txt") if p.stem not in NOT_CONFIG_FILES)
                errors.append(f"minecraft.overrides.{file}: unknown config file (one of {files})")
                continue
            if not isinstance(settings, dict):
                errors.append(f"minecraft.overrides.{file}: expected a mapping")
                continue
            if not settings:
                errors.append(f"minecraft.overrides.{file}: empty mapping (set a key or remove it)")
                continue
            known = set(keyfile.read_text().split())
            for p in _leaf_paths(settings):
                if _value_at(settings, p) == {}:
                    errors.append(f"minecraft.overrides.{file}.{p}: empty mapping (set a key or remove it)")
                elif not key_is_known(p, known):
                    errors.append(f"minecraft.overrides.{file}.{p}: not a key of {file} in {mc.version}")
        # v1.2: game rules are applied now (RCON), so their names must exist in the pinned jar
        rules_file = keys_dir / "gamerules.txt"
        known_rules = set(rules_file.read_text().split()) if rules_file.is_file() else None
        for name in mc.gamerules:
            bare = name.removeprefix("minecraft:")
            if bare in GAMEPLAY_RULES:
                errors.append(f"minecraft.gamerules.{name}: set it with gameplay.{GAMEPLAY_RULES[bare]}")
            elif known_rules is not None and bare not in known_rules:
                errors.append(
                    f"minecraft.gamerules.{name}: not a game rule of {mc.version} (26.x: snake_case)"
                )
    if not mc.online_mode and not i_know:
        errors.append(
            "minecraft.online_mode: false lets anyone join under any name; pass --i-know to allow it"
        )
    if perf.simulation_distance > perf.view_distance:
        errors.append("performance.simulation_distance: must be <= performance.view_distance")
    if perf.cpu_affinity == "split" and perf.helper_cpus is not None:
        errors.append(
            "performance.helper_cpus: leave it unset with cpu_affinity split (helpers get CPU 0, the JVM "
            "the rest)"
        )
    for name in ("schedule", "prune_schedule"):
        spec = getattr(cfg.backups, name)
        if _calendar_ok(spec) is False:
            errors.append(f"backups.{name}: systemd-analyze calendar rejects {spec!r}")
    shorthand = SCHEDULE_MINUTES.get(cfg.backups.schedule.strip().lower())
    if shorthand is not None and cfg.monitoring.alerts.backup_period_minutes < shorthand:
        errors.append(
            f"monitoring.alerts.backup_period_minutes: backups.schedule {cfg.backups.schedule!r} runs every "
            f"{shorthand} minutes; the watchdog would report every backup as late (set >= {shorthand})"
        )
    start, end = cfg.automation.reboot_window.split("-")
    if start == end:
        errors.append("automation.reboot_window: start and end are the same")
    if cfg.maintenance.window is not None:
        length = window_minutes(cfg.maintenance.window)
        if length == 0:
            errors.append("maintenance.window: start and end are the same")
        elif cfg.maintenance.force_after_minutes_before_end >= length:
            errors.append(
                f"maintenance.force_after_minutes_before_end: must be shorter than the window ({length} min)"
            )
    if cfg.panel.mode == "cloudflare":
        if cfg.admin.email is None:
            errors.append("admin.email: required when panel.mode is cloudflare (Access identity)")
        if not cfg.panel.access.emails:
            errors.append("panel.access.emails: needs at least 1 email when panel.mode is cloudflare")
        if cfg.panel.access.idp == "github" and not cfg.panel.access.github_client_id:
            errors.append("panel.access.github_client_id: required when panel.access.idp is github")
        for what, (use, cap) in budget(cfg).items():
            if use > cap * BUDGET_FRACTION:
                errors.append(
                    f"monitoring: ~{use} {what} exceeds {BUDGET_FRACTION:.0%} of the Free cap ({cap})"
                )
    d = cfg.discord
    if d.commands:
        if cfg.panel.mode != "cloudflare":
            errors.append("discord.commands: needs panel.mode cloudflare (the Worker answers them from D1)")
        for name in ("application_id", "public_key", "guild_id"):
            if getattr(d, name) is None:
                errors.append(
                    f"discord.{name}: required when discord.commands is true (`mcctl setup` fills it)"
                )
        for name in ("channel_ids", "user_ids"):
            if not getattr(d, name):
                errors.append(f"discord.{name}: needs at least 1 id when discord.commands is true")
    c = cfg.client
    if c.enabled:
        if cfg.panel.mode != "cloudflare":
            errors.append(
                "client.enabled: needs panel.mode cloudflare (the Worker serves the page and the codes)"
            )
        if not cfg.branding.enabled:
            errors.append("client.enabled: needs branding.enabled (mc-brand gives players their code)")
        if c.hostname in {cfg.panel.hostname, f"{cfg.panel.hostname}-confirm", cfg.dns.hostname}:
            errors.append(f"client.hostname: {c.hostname!r} is already used by another record")
    for i, h in enumerate(cfg.branding.holograms):
        coords = [h.x, h.y, h.z]
        if h.at is None and None in coords:
            errors.append(f"branding.holograms[{i}]: needs x, y and z, or at: spawn")
        elif h.at is not None and coords != [None, None, None]:
            errors.append(f"branding.holograms[{i}]: at: {h.at} and x/y/z both given (use one)")
    names = [p.name for p in cfg.plugins]
    if len(set(names)) != len(names):
        errors.append("plugins: duplicate names")
    _check_worlds(cfg, errors)


def _check_worlds(cfg: ServerConfig, errors: list[str]) -> None:
    """v1.2 world library: unique names, the active world among them, no hand overrides of its keys."""
    w = cfg.world
    if not w.library:
        return
    worlds = [e.name for e in w.library]
    if len(set(worlds)) != len(worlds):
        errors.append("world.library: duplicate names")
    if w.active not in worlds:
        errors.append(f"world.active: {w.active!r} isn't in world.library ({', '.join(worlds)})")
    props = cfg.minecraft.overrides.get("server.properties")
    for key in sorted(set(props or {}) & set(WORLD_PROPERTY_KEYS)):
        errors.append(
            f"minecraft.overrides.server.properties.{key}: set per world while world.library is used "
            "(its preset/type); every deploy would undo the active world's value"
        )


def load_server(
    data: Any,
    *,
    product_root: Path = PRODUCT_ROOT,
    git_origin: tuple[str, str] | None = None,
    i_know: bool = False,
) -> ServerConfig:
    """Validate parsed server.yml data; returns the config with every default filled in."""
    errors: list[str] = []
    cfg = _load_dataclass(ServerConfig, data, "", errors)
    if errors or cfg is None:
        raise SchemaError(errors)
    cfg = _derive(cfg, git_origin, errors)
    _check_derived(cfg, errors)
    _cross_validate(cfg, product_root, i_know, errors)
    if errors:
        raise SchemaError(errors)
    return cfg


def field_errors(path: str, value: Any, cls: Any = None) -> list[str]:
    """Problems with one server.yml field on its own (type and per-field rules; no cross-field rules).
    `mcctl init` checks every answer with it, `mcctl config set` every value before the whole file."""
    cls = cls or ServerConfig
    parts = path.split(".")
    for i, part in enumerate(parts):
        fields = {f.name: f for f in dataclasses.fields(cls)}
        here = ".".join(parts[: i + 1])
        if part not in fields:
            hint = difflib.get_close_matches(part, fields, n=1)
            return [f"{here}: unknown key" + (f" (did you mean {hint[0]!r}?)" if hint else "")]
        tp, fl = get_type_hints(cls)[part], fields[part]
        if i == len(parts) - 1:
            errors: list[str] = []
            if value is None and (fl.metadata.get("required") or fl.metadata.get("derived")):
                return [f"{path}: must not be null"]
            _load_value(tp, value, path, fl.metadata, errors)
            return errors
        if not dataclasses.is_dataclass(tp):
            return [f"{here}: not a section (it has no keys)"]
        cls = tp
    return []  # pragma: no cover


def load_players(data: Any) -> Players:
    """Validate parsed players.yml data: names, UUIDs, no duplicates, ops are whitelisted."""
    errors: list[str] = []
    players = _load_dataclass(Players, data, "", errors)
    if errors or players is None:
        raise SchemaError(errors)
    for section in ("whitelist", "ops"):
        uuids = [p.uuid for p in getattr(players, section)]
        if len(set(uuids)) != len(uuids):
            errors.append(f"{section}: duplicate uuid")
    listed = {p.uuid for p in players.whitelist}
    for op in players.ops:
        if op.uuid not in listed:
            errors.append(f"ops: {op.name} ({op.uuid}) isn't on the whitelist")
    if errors:
        raise SchemaError(errors)
    return players


def dump_players(players: Players) -> str:
    """The canonical players.yml text: sorted by name (case-insensitive), one flow mapping per entry.

    Stdlib-only on purpose (the panel rewrites the file with the same rules). Strings are JSON-quoted so
    names such as `yes`, `null` or `1234` stay strings.
    """
    lines: list[str] = []
    sections: tuple[tuple[str, list[Any], tuple[str, ...]], ...] = (
        ("whitelist", players.whitelist, ("name", "uuid")),
        ("ops", players.ops, ("name", "uuid", "level")),
    )
    for section, items, names in sections:
        if not items:
            lines.append(f"{section}: []")
            continue
        lines.append(f"{section}:")
        for item in sorted(items, key=lambda x: (x.name.lower(), x.uuid)):
            pairs = ", ".join(f"{n}: {json.dumps(getattr(item, n))}" for n in names)
            lines.append(f"  - {{{pairs}}}")
    return "\n".join(lines) + "\n"


# RFC 2606 / RFC 6761 reserved names, as an email/login domain and as a DNS zone.
PLACEHOLDER_DOMAIN = r"@(.+\.)?(example\.(com|org|net)|example|test|invalid|localhost)$"
PLACEHOLDER_ZONE = r"^(.+\.)?(example\.(com|org|net)|example|test|invalid|localhost)$"
# The template's GitHub names (example-owner / example-mc-config) and anything shaped like them.
PLACEHOLDER_GITHUB = r"(?i)^example([-_.].*)?$"


# --- v1.3 maintenance window -------------------------------------------------------------------------------


def in_window(window: str, t: datetime.time) -> bool:
    """`t` inside `HH:MM-HH:MM` (start included, end excluded; a window may cross midnight). Same rule as
    scripts/restart_window.py, scripts/reboot_gate.py and the deploy's check (site.yml)."""
    start, end = window.split("-")
    now = f"{t.hour:02d}:{t.minute:02d}"
    return start <= now < end if start <= end else (now >= start or now < end)


def window_minutes(window: str) -> int:
    """Length of `HH:MM-HH:MM` in minutes (crossing midnight counts on; 0 = start equals end)."""
    (h1, m1), (h2, m2) = (map(int, part.split(":")) for part in window.split("-"))
    return (h2 * 60 + m2 - h1 * 60 - m1) % 1440


def maintenance_timezone(cfg: ServerConfig) -> str:
    return cfg.maintenance.timezone or cfg.host.timezone


def restart_deferred(cfg: ServerConfig, now: datetime.datetime) -> str | None:
    """v1.3: the window ("04:00-07:00 <time zone>") when a deploy needing a restart at `now` (aware) would
    leave it to maintenance.window; None when it restarts at once (no window, or inside it)."""
    window = cfg.maintenance.window
    if window is None:
        return None
    tz = maintenance_timezone(cfg)
    local = now.astimezone(zoneinfo.ZoneInfo(tz))
    return None if in_window(window, local.time()) else f"{window} {tz}"


def placeholders(cfg: ServerConfig) -> list[str]:
    """Values left at documentation placeholders, one message each: RFC 2606 reserved domains in logins,
    emails and dns.zone, and the template's `example-*` GitHub names.

    `mcctl validate` reports them as warnings; anything that produces deployable output (`mcctl render`)
    refuses to run while one is left, because a placeholder would win over the real value from the
    environment once the rendered files are used (e.g. terraform.auto.tfvars.json beats TF_VAR_*), or would
    point Terraform at a zone or repository that isn't this deployment's.
    """
    out = []
    for path, value in (
        ("admin.tailscale_login", cfg.admin.tailscale_login),
        ("admin.email", cfg.admin.email),
        *((f"panel.access.emails[{i}]", e) for i, e in enumerate(cfg.panel.access.emails or [])),
    ):
        if value and re.search(PLACEHOLDER_DOMAIN, value):
            out.append(f"{path}: {value!r} is a placeholder; set the real value")
    if re.search(PLACEHOLDER_ZONE, cfg.dns.zone):
        out.append(f"dns.zone: {cfg.dns.zone!r} is a placeholder; set your Cloudflare zone")
    for path, value in (("github.owner", cfg.github.owner), ("github.config_repo", cfg.github.config_repo)):
        if value and re.search(PLACEHOLDER_GITHUB, value):
            out.append(f"{path}: {value!r} is a placeholder; set this config repo's owner/name")
    return out


# --- JSON Schema ------------------------------------------------------------------------------------


def _js_type(tp: Any, rules: dict[str, Any]) -> dict[str, Any]:
    origin = get_origin(tp)
    if _is_union(tp):
        branches = [_js_type(a, rules) for a in get_args(tp)]
        return {"anyOf": branches}
    if origin is Literal:
        return {"enum": list(get_args(tp))}
    if tp is type(None):
        return {"type": "null"}
    if tp is Any:
        return {}
    if dataclasses.is_dataclass(tp):
        return _js_object(tp)
    if origin is list:
        out: dict[str, Any] = {"type": "array", "items": _js_type(get_args(tp)[0], rules.get("items", {}))}
        if "min_items" in rules:
            out["minItems"] = rules["min_items"]
        return out
    if origin is dict:
        out = {"type": "object", "additionalProperties": _js_type(get_args(tp)[1], rules.get("values", {}))}
        if "keys" in rules:
            out["propertyNames"] = {"pattern": rules["keys"]}
        return out
    out = {"type": {str: "string", int: "integer", bool: "boolean"}[tp]}
    if tp is str and "pattern" in rules:
        out["pattern"] = rules["pattern"]
    if tp is int:
        out |= {k: rules[k] for k in ("minimum", "maximum") if k in rules}
    return out


def _js_default(v: Any) -> Any:
    if dataclasses.is_dataclass(v):
        return None
    if isinstance(v, list):
        return [dataclasses.asdict(x) if dataclasses.is_dataclass(x) else x for x in v]
    return v


def _js_object(cls: Any) -> dict[str, Any]:
    hints = get_type_hints(cls)
    props: dict[str, Any] = {}
    required = []
    for fl in dataclasses.fields(cls):
        meta, tp = fl.metadata, hints[fl.name]
        if meta.get("derived") and _is_union(tp):
            tp = next(a for a in get_args(tp) if a is not type(None))
        prop = _js_type(tp, meta)
        doc = meta["doc"] + (f" Default: {meta['derived']}." if meta.get("derived") else "")
        prop = {"description": doc, **prop, **meta.get("json", {})}
        if meta.get("required"):
            required.append(fl.name)
        elif not meta.get("derived"):
            default = fl.default_factory() if fl.default_factory is not dataclasses.MISSING else fl.default
            if (d := _js_default(default)) is not None or default is None:
                prop["default"] = d
        props[fl.name] = prop
    out: dict[str, Any] = {"type": "object", "additionalProperties": False, "properties": props}
    if required:
        out["required"] = required
    return out


def json_schema() -> dict[str, Any]:
    """JSON Schema (draft 2020-12) for server.yml, generated from the dataclasses."""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "mcctl server.yml",
        "description": "Generated by `mcctl schema` from mcctl/schema.py; mcctl validate is authoritative.",
        **_js_object(ServerConfig),
    }
