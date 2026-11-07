"""P17 acceptance on the e2e-mock runner: the mc-brand plugin on the real Purpur in the container.

Runs on the runner, drives the container with `docker exec` (RCON through the box's own mc_rcon.py):
  - the plugin is enabled (journal "Enabling mc-brand" and its summary line), its settings file is the deploy's
    (root:minecraft 0640: the plugin can't change it), the jar and the server-list icon are the pinned ones;
  - the status ping carries a 64x64 icon (the server re-encodes server-icon.png, so not byte-equal);
  - commands over RCON: /rules (the default four, numbered), /discord (no invite set), /mcbrand selftest (the
    rendered first-join and welcome-back greetings with their placeholders filled, the title, the tab list, the
    sidebar lines, the drink built and read back through the API: PDC tag, custom_model_data string, use
    cooldown group, colour), /mcbrand status (the HUD/tab task ran, and its time per run);
  - a player-only command from the console is refused politely (no stack trace in the journal), /drink and
    v1.2's branding.drink.command alike (the e2e config sets it: registered through the CommandMap).
The join flow itself (title, greeting, sound, the first drink) needs a logged-in client: online-mode is on and the
whitelist is enforced in the e2e, so it is checked through selftest's rendering, not with a real client.

    python tests/e2e/brand_check.py BOX
"""

from __future__ import annotations

import base64
import hashlib
import re
import struct
import subprocess
import sys
import time
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from mcctl import mcping  # noqa: E402

SERVER = "/opt/minecraft/server"
failures: list[str] = []


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def sh(box: str, script: str, user: str | None = None) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "exec", box] + (["runuser", "-u", user, "--"] if user else []) + ["sh", "-c", script]
    return subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)


def rcon(box: str, command: str) -> str:
    return sh(box, f"/opt/minecraft/bin/mc_rcon.py cmd {command}", user="minecraft").stdout


def main() -> int:
    box = sys.argv[1]
    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    journal = sh(box, "journalctl -u minecraft --no-pager -o cat").stdout
    expect("Enabling mc-brand" in journal, "mc-brand is enabled on the real server")
    expect(re.search(r"branding .+: 4 rules, discord not set, hud available", journal) is not None,
           "the plugin read the deploy's settings (4 rules, no Discord, HUD available)")  # fmt: skip
    expect(not re.search(r"Error occurred while enabling mc-brand|mc-brand.*Exception", journal),
           "no mc-brand error in the journal")  # fmt: skip
    # mode only: a restore (the hardening check's drill, agent_check's round trip) rewrites the tree as minecraft
    st = sh(box, f"stat -c '%a' {SERVER}/plugins/mc-brand/config.yml").stdout.strip()
    conf = sh(box, f"cat {SERVER}/plugins/mc-brand/config.yml").stdout
    expect(st == "640" and "first_join:" in conf, f"plugins/mc-brand/config.yml is the deploy's ({st})")
    jar = sh(box, f"sha256sum {SERVER}/plugins/mc-brand.jar").stdout.split()[:1]
    expect(jar == [gv["mc_brand_jar_sha256"]], "plugins/mc-brand.jar is the pinned build")
    icon = (REPO / "plugin/mc-brand/dist/server-icon.png").read_bytes()
    got = sh(box, f"sha256sum {SERVER}/server-icon.png").stdout.split()[:1]
    expect(
        got == [hashlib.sha256(icon).hexdigest()] == [gv["mc_brand_icon_sha256"]], "server-icon.png is pinned"
    )

    # the server re-encodes the PNG (ImageIO) for the status JSON: compare the decoded size, not the bytes
    status = mcping.status("127.0.0.1", 25565, timeout=10)
    fav = status.get("favicon", "").replace("\n", "")
    png = (
        base64.b64decode(fav.removeprefix("data:image/png;base64,"))
        if fav.startswith("data:image/png")
        else b""
    )
    size = struct.unpack(">II", png[16:24]) if png[:8] == b"\x89PNG\r\n\x1a\n" else None
    expect(size == (64, 64), f"the status ping carries a 64x64 icon ({size})")
    rules = rcon(box, "rules")
    expect(
        all(f"{i}. " in rules for i in range(1, 5)) and "Be kind." in rules,
        f"/rules lists 4 rules: {rules!r}",
    )
    expect("No Discord link yet" in rcon(box, "discord"), "/discord without an invite says so")
    plugin_conf = yaml.safe_load(conf) or {}
    drink = plugin_conf.get("drink", {})
    command = drink.get("command", "drink")
    for cmd in sorted({"drink", command}):
        expect("Only players" in rcon(box, cmd), f"/{cmd} from the console is refused politely")

    self_test = rcon(box, "mcbrand selftest")
    print(self_test, flush=True)
    for needle, what in (
        ("first join: Welcome to", "the first-join greeting"),
        ("Steve", "the {player} placeholder"),
        ("Last seen 3 days ago", "welcome back with last seen (the {ago} placeholder)"),
        ("glass true", "the drink carries its PDC tag"),
        ("model=[mcbrand:wine_glass]", "custom_model_data strings (the resource pack's select)"),
        ("cooldown=30.0s group=mc-brand:wine_glass", "its own use_cooldown group"),
        (f"color={str(drink.get('color', '#3A6EA5')).upper()}", "the potion colour (branding.drink.color)"),
        ("stack=1", "one per stack"),
        ("sidebar lines", "the sidebar's lines build"),
        (
            "drink command /drink" if command == "drink" else f"drink command /{command} true + /drink",
            "v1.2: the drink's own command is registered (CommandMap) next to /drink",
        ),
        ("write errors 0", "v1.0.1: no HUD write failed"),
        (
            "glyph mcbrand:icons true, no emoji true, without pack true",
            "v1.0.1: the {glyph} icon only with the pack",
        ),
        ("default mode top", "v1.0.1: the boss bar is the default HUD"),
        (
            "legacy hud 1->top/side 0->off",
            "v1.1: a v1.0.0 'HUD on' follows the server default, 'off' stays off",
        ),
        ("smoothing Smooth/Laggy/Smooth", "v1.1: one bad reading doesn't flip the health word, two do"),
        ("settle false/false/false/true", "v1.1: the join grace period needs the time and a steady ping"),
        ("settling bar: ", "v1.1: the settling boss bar"),
        (
            "modpack prompt no pack true, pack false, off false, parse true/true",
            "v1.2: the join prompt for players without the pack (exact: the mod's hello), /modpack off, the "
            "agent's answer format",
        ),
        (
            "smallworld hello true, pack CURRENT/OUTDATED/NONE/UNKNOWN, config 010501",
            "v1.2: WIRE.md's worked example decodes, the outdated-pack rule, the config payload",
        ),
        ("pack is out of date", "v1.2: the outdated-pack line"),
        (
            "xaero tracked 020a01000172000b000169000000040123456789abcdeffedcba9876543210060001783ff8000000000000"
            "0600017940500000000000000600017ac0020000000000000800016400136d696e6563726166743a6f766572776f726c6400"
            " untracked 020a01000172010b000169000000040123456789abcdeffedcba987654321000 handshake 0100000003",
            "v1.2: Xaero's tracked-player packets (tests/test_social.py encodes them independently)",
        ),
        ("fair play code true", "v1.2: Xaero's fair-play code as literal text in a component"),
        (
            " | 3 online at 20.0 TPS | Server: Smooth, glyph true/true",
            "v1.2: the hologram text (lines, placeholders, the health line; the glyph only in the pack variant)",
        ),
        (
            "view decide raise/wait/lower/invalid, guard r-l-----r",
            "v1.1: /view raises only while Smooth, lowering always; the guard lowers after 2, raises after 6",
        ),
        ("Your personal setup link: ", "v1.1: /modpack's answer with the link"),
        ("Settling in\u2026", "v1.1: 'Settling in…' instead of a health word"),
        (" online · ", "v1.0.1: the boss bar's line"),
        ("/20 TPS", "v1.0.1: speed with its scale"),
        (" of 50 ms", "v1.0.1: tick time with its budget"),
    ):
        expect(needle in self_test, f"selftest: {what}")
    # v1.0.1: every sidebar line is a score the client shows (v1.0.0 used '#'-entries: hidden, title only)
    m = re.search(r"sidebar scores (\d+)/(\d+) visible", self_test)
    expect(m is not None and int(m.group(2)) >= 5 and m.group(1) == m.group(2),
           f"selftest: every sidebar line is set and visible ({m.group(0) if m else self_test[:200]!r})")  # fmt: skip
    expect(re.search(r"boss bar: .* · (Smooth|A bit busy|Laggy) · ", self_test) is not None,
           "selftest: the boss bar says how the server is doing in words")  # fmt: skip
    time.sleep(6)  # one HUD/tab task period (5 s default)
    st2 = rcon(box, "mcbrand status")
    m = re.search(r"runs=(\d+) last=(\d+)us max=(\d+)us", st2)
    expect(m is not None and int(m.group(1)) > 0, f"the HUD/tab task runs: {st2!r}")
    if m:
        print(f"  task time: last {m.group(2)} us, max {m.group(3)} us (nobody online)", flush=True)
    journal2 = sh(box, "journalctl -u minecraft --no-pager -o cat --since '-2min'").stdout
    expect("Exception" not in journal2, "no exception after the commands")
    for f in failures:
        print("FAILED:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
