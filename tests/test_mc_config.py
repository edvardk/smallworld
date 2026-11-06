import mc_config
import yaml


def test_yaml_merge_idempotent_after_server_rewrite() -> None:
    original = "# comment\nsettings:\n  allow-end: true\nspawn-limits:\n  monsters: 70\n  animals: 10\n"
    changed, out, keys = mc_config.apply_yaml(original, {"spawn-limits": {"monsters": 50}})
    assert changed and keys == ["spawn-limits.monsters"]
    assert yaml.safe_load(out) == {
        "settings": {"allow-end": True},
        "spawn-limits": {"monsters": 50, "animals": 10},
    }
    # The server re-saves with its own formatting/comments: values equal -> no change.
    rewritten = (
        "# Paper header\nspawn-limits:\n    animals: 10\n    monsters: 50\nsettings:\n    allow-end: true\n"
    )
    assert mc_config.apply_yaml(rewritten, {"spawn-limits": {"monsters": 50}})[0] is False


def test_yaml_float_vs_int() -> None:
    assert mc_config.apply_yaml("merge-radius:\n  exp: 4.0\n", {"merge-radius": {"exp": 4.0}})[0] is False


def test_properties_update_and_append() -> None:
    text = "#Minecraft server properties\n#Thu Oct 30 2026\nview-distance=10\nmotd=A Minecraft Server\n"
    changed, out, keys = mc_config.apply_properties(
        text, {"view-distance": 8, "enable-rcon": True, "motd": "Sam's: server"}
    )
    assert changed and keys == ["enable-rcon", "motd", "view-distance"]
    assert "view-distance=8" in out and "enable-rcon=true" in out and "motd=Sam's\\: server" in out
    assert out.startswith("#Minecraft server properties\n#Thu")
    # MC rewrites the header timestamp: values unchanged -> idempotent.
    again = out.replace("#Thu Oct 30 2026", "#Fri Oct 31 2026")
    assert (
        mc_config.apply_properties(again, {"view-distance": 8, "enable-rcon": True, "motd": "Sam's: server"})[
            0
        ]
        is False
    )


def test_properties_unicode_roundtrip() -> None:
    _, out, _ = mc_config.apply_properties("", {"motd": "Café"})
    assert "motd=Caf\\u00e9" in out
    assert mc_config.apply_properties(out, {"motd": "Café"})[0] is False
