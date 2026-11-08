"""Every key the overlays set must exist in the pinned server/plugin version (keys/<mc_version>/)."""

import json
import os
import subprocess

import pytest
import yaml
from conftest import REPO, make_config_repo

from mcctl import configrepo, render

MC_VERSION = yaml.safe_load((REPO / "template/server.lock.yml").read_text())["minecraft_version"]
KEYS = REPO / "server-config" / "keys" / MC_VERSION
KEYFILE = {
    "server.properties.yml.j2": "server.properties",
    "bukkit.yml.j2": "bukkit",
    "spigot.yml.j2": "spigot",
    "purpur.yml.j2": "purpur",
    "paper-global.yml.j2": "paper-global",
    "paper-world-defaults.yml.j2": "paper-world-defaults",
    "plugins/Chunky.yml.j2": "plugin-Chunky",
    "plugins/AxGraves.yml.j2": "plugin-AxGraves",
}


def render_overlays(tmp_path_factory: pytest.TempPathFactory, profile: str | None) -> dict[str, dict]:
    out = tmp_path_factory.mktemp("overlays")
    # The template's server.yml (product defaults), rendered like a deploy would.
    server = None
    if profile is not None:
        from conftest import example_server

        server = example_server()
        server["performance"] = server.get("performance", {}) | {"profile": profile}
    repo = configrepo.load(make_config_repo(tmp_path_factory.mktemp("config"), server, renderable=True))
    rendered_dir = tmp_path_factory.mktemp("rendered")
    render.render(repo, rendered_dir)
    env = os.environ | {"ANSIBLE_LOCALHOST_WARNING": "false", "ANSIBLE_INVENTORY_UNPARSED_WARNING": "false"}
    subprocess.run(
        [
            "ansible-playbook",
            str(REPO / "tests" / "render_overlays.yml"),
            "-e",
            f"out_dir={out}",
            "-e",
            f"rendered_vars={rendered_dir / render.GROUP_VARS_FILE}",
        ],
        check=True,
        capture_output=True,
        env=env,
    )
    out_files = {t: json.loads((out / (t.replace("/", "__") + ".json")).read_text()) for t in KEYFILE}
    brand = yaml.safe_load((out / "mc-brand.yml").read_text())
    return out_files | {
        "mc-brand": brand,
        "_vars": yaml.safe_load((rendered_dir / render.GROUP_VARS_FILE).read_text()),
    }


@pytest.fixture(scope="module")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return render_overlays(tmp_path_factory, None)


@pytest.fixture(scope="module")
def rendered_balanced(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict]:
    return render_overlays(tmp_path_factory, "balanced")


def leaf_paths(d: dict, prefix: str = "") -> list[str]:
    out = []
    for k, v in d.items():
        p = f"{prefix}{k}"
        out += leaf_paths(v, p + ".") if isinstance(v, dict) else [p]
    return out


def valid(path: str, keys: set[str]) -> bool:
    if path in keys:
        return True
    # Children of a map-typed setting (e.g. alt-item-despawn-rate.items.<item>) aren't enumerable:
    # accept them if an ancestor is a known key that has no known children.
    parts = path.split(".")
    for i in range(len(parts) - 1, 0, -1):
        anc = ".".join(parts[:i])
        if anc in keys:
            return not any(k.startswith(anc + ".") for k in keys)
    return False


def test_keys_dir_exists_for_pinned_version() -> None:
    assert KEYS.is_dir(), f"run scripts/config_keys.py for {MC_VERSION}"


@pytest.mark.parametrize("template", sorted(KEYFILE))
def test_overlay_keys_exist(rendered: dict[str, dict], template: str) -> None:
    keys = set((KEYS / f"{KEYFILE[template]}.txt").read_text().split())
    data = rendered[template]
    paths = list(data) if template == "server.properties.yml.j2" else leaf_paths(data)
    bad = [p for p in paths if not valid(p, keys)]
    assert not bad, f"{template}: keys not in {MC_VERSION}: {bad}"


@pytest.mark.parametrize("template", sorted(KEYFILE))
def test_balanced_overlay_keys_exist(rendered_balanced: dict[str, dict], template: str) -> None:
    keys = set((KEYS / f"{KEYFILE[template]}.txt").read_text().split())
    data = rendered_balanced[template]
    paths = list(data) if template == "server.properties.yml.j2" else leaf_paths(data)
    assert not [p for p in paths if not valid(p, keys)]


def test_both_profiles_manage_the_same_keys(
    rendered: dict[str, dict], rendered_balanced: dict[str, dict]
) -> None:
    # A key one profile sets and the other drops would keep the old value on a box that switches profile
    # (overlays only add keys): both must set the same keys. The only exception is the alt-item-despawn map,
    # which is ignored while alt-item-despawn-rate.enabled is false.
    for t in KEYFILE:
        a, b = set(leaf_paths(rendered[t])), set(leaf_paths(rendered_balanced[t]))
        items = "entities.spawning.alt-item-despawn-rate.items."
        assert {p for p in a ^ b if not p.startswith(items)} == set(), t


def test_vanilla_profile_values(rendered: dict[str, dict]) -> None:
    """The product default (vanilla) renders the pinned jar's own vanilla values (generated default configs of
    Purpur 26.3 build 2655), plus the documented invisible optimisations."""
    props, bukkit = rendered["server.properties.yml.j2"], rendered["bukkit.yml.j2"]
    spigot = rendered["spigot.yml.j2"]["world-settings"]["default"]
    paper, purpur = rendered["paper-world-defaults.yml.j2"], rendered["purpur.yml.j2"]
    assert (props["view-distance"], props["simulation-distance"], props["sync-chunk-writes"]) == (
        10,
        8,
        False,
    )
    assert bukkit["spawn-limits"] == {
        "monsters": 70,
        "animals": 10,
        "water-animals": 5,
        "water-ambient": 20,
        "ambient": 15,
    }
    assert bukkit["ticks-per"] == {
        "monster-spawns": 1,
        "animal-spawns": 400,
        "water-spawns": 1,
        "water-ambient-spawns": 1,
        "ambient-spawns": 1,
    }
    ar = spigot["entity-activation-range"]
    assert ar.pop("tick-inactive-villagers") is True and set(ar.values()) == {0}
    assert spigot["merge-radius"] == {"item": 0.5, "exp": -1.0} and spigot["max-tnt-per-tick"] == 0
    assert spigot["mob-spawn-range"] == 8 and spigot["entity-tracking-range"]["animals"] == 48
    sp = paper["entities"]["spawning"]
    assert sp["despawn-ranges"]["monster"] == {"soft": 32, "hard": 128}
    assert sp["alt-item-despawn-rate"] == {"enabled": False} and sp["per-player-mob-spawns"] is True
    assert paper["entities"]["armor-stands"] == {"tick": True, "do-collision-entity-lookups": True}
    assert paper["collisions"] == {"max-entity-collisions": 8, "fix-climbing-bypassing-cramming-rule": False}
    assert set(paper["chunks"]["entity-per-chunk-save-limit"].values()) == {-1}
    assert paper["chunks"]["prevent-moving-into-unloaded-chunks"] is False
    assert paper["misc"]["redstone-implementation"] == "VANILLA"
    assert paper["environment"]["optimize-explosions"] is False
    assert paper["environment"]["treasure-maps"]["find-already-discovered"] == {
        "loot-tables": "default",
        "villager-trade": False,
    }
    assert paper["tick-rates"] == {"grass-spread": 1, "container-update": 1, "mob-spawner": 1}
    assert paper["hopper"] == {"disable-move-event": True, "ignore-occluding-blocks": False}
    mobs = purpur["world-settings"]["default"]["mobs"]
    assert (
        mobs["villager"]["lobotomize"]["enabled"] is False
        and mobs["villager"]["search-radius"]["acquire-poi"] == 48
    )
    assert mobs["dolphin"]["disable-treasure-searching"] is False
    assert mobs["zombie"]["aggressive-towards-villager-when-lagging"] is True


def test_balanced_profile_is_the_previous_tuning(rendered_balanced: dict[str, dict]) -> None:
    paper = rendered_balanced["paper-world-defaults.yml.j2"]
    spigot = rendered_balanced["spigot.yml.j2"]["world-settings"]["default"]
    assert rendered_balanced["server.properties.yml.j2"]["simulation-distance"] == 5
    assert paper["misc"]["redstone-implementation"] == "ALTERNATE_CURRENT"
    assert paper["entities"]["spawning"]["alt-item-despawn-rate"]["items"]["cobblestone"] == 300
    assert paper["environment"]["treasure-maps"]["find-already-discovered"]["loot-tables"] == "true"
    assert spigot["merge-radius"] == {"item": 3.5, "exp": 4.0} and spigot["max-tnt-per-tick"] == 100


def test_every_plugin_template_is_wired_up() -> None:
    defaults = yaml.safe_load((REPO / "ansible/roles/minecraft/defaults/main.yml").read_text())
    wired = {o["template"] for o in defaults["mc_overlays_plugins"]}
    on_disk = {f"plugins/{p.name}" for p in (REPO / "server-config/plugins").glob("*.yml.j2")}
    assert wired == on_disk
    assert set(KEYFILE) >= wired


def test_brand_settings_render_as_mcctl_wrote_them(rendered: dict[str, dict]) -> None:
    """P17: plugins/mc-brand/config.yml is exactly `mcctl render`'s mc_brand (to_nice_yaml round-trips it)."""
    want = json.loads(json.dumps(rendered["_vars"]["mc_brand"]))
    # v1.1: the template adds the product's /modpack hand-off paths
    want["client"] |= {
        "request_dir": "/run/mc-telemetry/client",
        "code_file": "/run/mc-verdicts/client-codes.tsv",
        "packs": {},  # v1.2: mc_client_packs (rendered from the client lock; none in this fixture)
    }
    # v1.4: and the world map pre-render's output and colours (mc_map_dir, mc_map_colors)
    want["pocket_map"] |= {
        "prerender_dir": "/var/lib/mc-map",
        "colors_file": "/opt/minecraft/share/map-colors.tsv",
    }
    assert rendered["mc-brand"] == want
    props = rendered["server.properties.yml.j2"]
    # the template's defaults: branding on, the resource pack off (its keys managed and empty)
    assert props["resource-pack"] == "" and props["resource-pack-sha1"] == ""
    assert props["require-resource-pack"] is False
