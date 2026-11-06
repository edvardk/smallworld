import pytest
import yaml
from conftest import EXAMPLE

from mcctl import yamledit

DOC = """\
# top comment
name: lh   # trailing comment
admin:
  name: Sam
  # inner comment
  email: sam@x.internal
dns: {zone: x.internal, hostname: mc}
host:
  # ipv4 / ipv6: later
  timezone: Europe/Dublin
empty:
nothing: null
"""


@pytest.mark.parametrize(
    ("path", "value", "fragment"),
    [
        ("name", "mc", "name: mc   # trailing comment"),
        ("admin.name", "Sam K", "  name: Sam K\n"),
        ("admin.email", "e@y.internal", "  email: e@y.internal\n"),
        ("dns.zone", "y.dev", "dns: {zone: y.dev, hostname: mc}"),
        ("dns.ttl", 60, "dns: {zone: x.internal, hostname: mc, ttl: 60}"),
        ("host.ipv4", "198.51.100.1", "  timezone: Europe/Dublin\n  ipv4: 198.51.100.1\n"),
        ("host.ipv6", "2001:db8::1", "  ipv6: 2001:db8::1\n"),
        ("admin.name", "10:30", '  name: "10:30"\n'),
        ("deploy.enabled", True, "nothing: null\ndeploy:\n  enabled: true\n"),
        ("world.border_radius.nether", 1250, "world:\n  border_radius:\n    nether: 1250\n"),
        ("empty", 5, "empty: 5\n"),
        ("empty.x", "y", "empty: {x: y}\n"),
        ("nothing.a.b", False, "nothing: {a: {b: false}}\n"),
        ("admin.name", "yes", '  name: "yes"\n'),
        ("admin.name", "a # b", '  name: "a # b"\n'),
        ("minecraft.version", "26.3", 'minecraft:\n  version: "26.3"\n'),
        ("dns.hostname", None, "hostname: null}"),
        ("admin.extra", ["a", "b c"], "  extra: [a, b c]\n"),
    ],
)
def test_set_value_preserves_everything_else(path, value, fragment) -> None:
    new = yamledit.set_value(DOC, path, value)
    assert fragment in new
    assert yamledit.get_value(new, path) == value
    for comment in ("# top comment", "# trailing comment", "# inner comment", "# ipv4 / ipv6: later"):
        assert comment in new
    # nothing else changed
    before, after = yaml.safe_load(DOC), yaml.safe_load(new)
    top = path.split(".")[0]
    assert {k: v for k, v in before.items() if k != top} == {k: v for k, v in after.items() if k != top}


def test_set_value_errors() -> None:
    with pytest.raises(yamledit.YamlEditError, match="isn't a mapping"):
        yamledit.set_value(DOC, "name.x", 1)
    with pytest.raises(yamledit.YamlEditError, match="block mapping"):
        yamledit.set_value(DOC, "admin", "x")
    with pytest.raises(yamledit.YamlEditError, match="bad path"):
        yamledit.set_value(DOC, "a..b", 1)


def test_set_value_on_empty_and_unterminated_documents() -> None:
    assert yamledit.set_value("", "a.b", 1) == "a:\n  b: 1\n"
    assert yamledit.set_value("x: 1", "y", 2) == "x: 1\ny: 2\n"


def test_get_value_missing() -> None:
    assert yamledit.get_value(DOC, "host.ipv4") is yamledit.MISSING
    assert yamledit.get_value(DOC, "dns.zone") == "x.internal"


def test_edits_the_template_server_yml() -> None:
    text = (EXAMPLE / "server.yml").read_text()
    for path, value in (
        ("admin.tailscale_login", "sam@github"),
        ("github.owner", "octocat"),
        ("host.ipv4", "198.51.100.7"),
        ("host.ipv6", "3ffe:ffff::1"),
        ("world.border_radius.overworld", 4000),
        ("jvm.heap_mb", 1024),
        ("deploy.enabled", True),
    ):
        text = yamledit.set_value(text, path, value)
        assert yamledit.get_value(text, path) == value
    assert "# `mcctl init` writes this file" in text


def test_new_key_lands_before_the_next_sections_comment() -> None:
    doc = "world:\n  border_radius:\n    nether: 1250\n\n# --- performance ---\nperformance:\n  view_distance: 8\n"
    new = yamledit.set_value(doc, "world.expected", True)
    assert new == (
        "world:\n  border_radius:\n    nether: 1250\n  expected: true\n\n# --- performance ---\n"
        "performance:\n  view_distance: 8\n"
    )
    seq = "plugins:\n  - name: a\n    source: modrinth\n# next\nother: 1\n"
    assert yamledit.set_value(seq, "zzz", 1).endswith("other: 1\nzzz: 1\n")
    nested = "a:\n  b:\n    - 1\n    - 2\n\n# c\nc: 1\n"
    assert yamledit.set_value(nested, "a.d", 3) == "a:\n  b:\n    - 1\n    - 2\n  d: 3\n\n# c\nc: 1\n"
