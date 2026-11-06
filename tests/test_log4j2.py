"""v1.0.1: server-config/log4j2.xml is the pinned server jar's own log4j2.xml (server-config/keys/<version>/,
extracted by scripts/config_keys.py) plus two Logger entries whose RegexFilters drop exactly the per-connection
RCON thread lines; the unit points the JVM at it."""

import re
import xml.etree.ElementTree as ET

from conftest import REPO

from mcctl import yamlio

MC_VERSION = str(yamlio.load((REPO / "template" / "server.lock.yml").read_text())["minecraft_version"])
KEYS = REPO / "server-config" / "keys" / MC_VERSION
OURS = REPO / "server-config" / "log4j2.xml"
RCON_LOGGERS = {
    "net.minecraft.server.rcon.thread.GenericThread": "Thread {} started",
    "net.minecraft.server.rcon.thread.RconClient": "Thread {} shutting down",
}


def ours() -> ET.Element:
    return ET.fromstring(OURS.read_bytes())  # noqa: S314 - the repo's own file


def added(root: ET.Element) -> list[ET.Element]:
    loggers = root.find("Loggers")
    assert loggers is not None
    return [e for e in loggers if e.tag == "Logger"]


def test_everything_but_the_rcon_filters_is_the_jars_own_config() -> None:
    root = ours()
    loggers = root.find("Loggers")
    assert loggers is not None
    for e in added(root):
        loggers.remove(e)
    bundled = ET.fromstring((KEYS / "log4j2.xml").read_bytes())  # noqa: S314 - extracted, committed
    assert ET.canonicalize(ET.tostring(root), strip_text=True) == ET.canonicalize(
        ET.tostring(bundled), strip_text=True
    )


def test_the_filters_drop_only_the_per_connection_lines() -> None:
    known = {tuple(line.split("\t", 1)) for line in (KEYS / "rcon-log.txt").read_text().splitlines()}
    loggers = added(ours())
    assert {e.get("name") for e in loggers} == set(RCON_LOGGERS)
    for e in loggers:
        name = e.get("name")
        assert e.get("level") == "info" and e.get("additivity") in (None, "true")  # still reaches Root
        assert not e.findall("AppenderRef")  # Root's appenders (and its MarkerFilter), nothing else
        (f,) = list(e)
        assert f.tag == "RegexFilter" and (f.get("onMatch"), f.get("onMismatch")) == ("DENY", "NEUTRAL")
        assert f.get("useRawMsg") in (None, "false")  # matched against the formatted message
        msg = RCON_LOGGERS[name]
        assert (name, msg) in known, f"{name} no longer logs {msg!r} in {MC_VERSION}"
        rx = re.compile(f.get("regex") or "")
        # log4j's RegexFilter uses Matcher.matches(): the whole message (Python's fullmatch)
        for client in ("/127.0.0.1", "localhost/127.0.0.1", "/0:0:0:0:0:0:0:1"):
            assert rx.fullmatch(msg.replace("{}", f"RCON Client {client}"))
        for other in ("RCON Listener", "Query Listener #1"):
            assert not rx.fullmatch(msg.replace("{}", other))
        for real in ("Exception whilst parsing RCON input", "Failed to close socket", "Thread {} stopped"):
            assert not rx.fullmatch(real.replace("{}", "RCON Client /127.0.0.1"))


def test_the_unit_and_the_role_use_it() -> None:
    unit = (REPO / "ansible/roles/minecraft/templates/minecraft.service.j2").read_text()
    assert "-Dlog4j.configurationFile={{ mc_root }}/log4j2.xml -jar server.jar" in unit
    tasks = (REPO / "ansible/roles/minecraft/tasks/main.yml").read_text()
    assert 'src: "{{ mc_config_dir }}/log4j2.xml"' in tasks and 'dest: "{{ mc_root }}/log4j2.xml"' in tasks
