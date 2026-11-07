#!/usr/bin/env python3
"""Render the mcctl dashboard's tabs as SVG for the docs (docs/images/mcctl-dashboard-<tab>.svg).

Runs the real dashboard (mcctl/dashboard.py) headless at 120x36 against a temporary config repo and the
tests' fakes (tests/dashboard_fakes.py: a fixed status, lists, a doctor report, canned command output), so
nothing real is contacted and no credential exists.

    python scripts/dashboard_screenshot.py [-o docs/images]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from textual.widgets import Static

from mcctl import dashboard, home
from tests import dashboard_fakes as fakes
from tests.conftest import RENDERABLE, example_server, make_config_repo

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "images"
SIZE = (120, 36)
TPS = [19.9, 20.0, 19.97, 19.8, 19.95, 18.7, 17.9, 19.2, 19.9, 20.0, 19.99, 19.94] * 3
MSPT = [11.8, 12.0, 12.9, 14.2, 13.1, 31.5, 44.8, 22.0, 12.6, 12.1, 11.9, 12.3] * 3


class Shot(dashboard.Dashboard):
    """The header shows a stable config repo path (the real one is a temporary directory)."""

    def render_header(self) -> None:
        super().render_header()
        self.query_one("#repo", Static).update("~/lighthouse (remembered)")


async def shoot(out: Path, root: Path) -> list[Path]:
    spawn = fakes.FakeSpawn(hold={"logs"})
    services = fakes.services(
        spawn, update=lambda pin: "Smallworld v1.4.0 is out (this config repo pins v0.0.0): `mcctl upgrade`"
    )
    app = Shot(home.Launch(root), services=services, refresh_s=3600)
    written: list[Path] = []

    async with app.run_test(size=SIZE) as pilot:

        async def settle() -> None:
            await pilot.pause(0.2)
            await app.workers.wait_for_complete()
            await pilot.pause(0.3)

        def save(name: str) -> None:
            path = out / f"mcctl-dashboard-{name}.svg"
            path.write_text(app.export_screenshot(title=f"mcctl: {name}"), encoding="utf-8")
            written.append(path)

        await settle()
        app.tps.extend(TPS)
        app.mspt.extend(MSPT)
        app.render_overview()
        await pilot.press("r")
        await pilot.pause(0.2)
        await pilot.click("#ok")
        await settle()
        save("overview")

        await pilot.press("2")
        await settle()
        await pilot.press("k")
        await pilot.pause(0.2)
        await pilot.click("#ok")
        await settle()
        save("players")

        await pilot.press("3")
        await settle()
        await pilot.press("down")
        await pilot.press("w")
        await pilot.pause(0.2)
        await pilot.click("#ok")
        await pilot.pause(0.3)
        await pilot.press(*"switch to cr")
        await pilot.pause(0.2)
        save("confirm")
        await pilot.press(*"eative", "enter")
        await settle()
        save("worlds")

        for key, name in (("4", "plugins"), ("5", "backups")):
            await pilot.press(key)
            await settle()
            save(name)

        await pilot.press("6")
        await settle()
        await pilot.press("f")
        await pilot.pause(0.5)
        save("logs")
        spawn.release.set()
        await settle()

        await pilot.press("7")
        await settle()
        await pilot.press(*"list", "enter")
        await settle()
        save("console")

        await pilot.press("escape", "8")  # out of the console's prompt first
        await settle()
        save("publish")

        await pilot.press("9")
        await settle()
        await pilot.press("h")
        await settle()
        save("health")

        await pilot.press("1")
        await settle()
        await pilot.press("ctrl+p")
        await pilot.pause(0.3)
        await pilot.press(*"world")
        await pilot.pause(0.6)
        save("palette")
    return written


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", type=Path, default=DEFAULT_OUT, help="output directory")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        server = example_server() | RENDERABLE | {"name": "lighthouse"}
        root = make_config_repo(Path(tmp) / "lighthouse", server)
        fakes.write_players(root)
        for path in asyncio.run(shoot(args.out, root)):
            print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
