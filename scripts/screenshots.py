#!/usr/bin/env python3
"""Regenerate every image under docs/images/ from mock and test runs only (nothing real is contacted).

    python scripts/screenshots.py            # all of them
    python scripts/screenshots.py --only panel brand

Sources, all against the neutral fixtures ("Lighthouse"):
    panel   watchdog/test/ui.smoke.mjs (the panel against a local mock API, desktop + phone, scale 2) with
            PANEL_SHOTS=after: every tab as panel-after-<tab>-<desktop|phone>.png and panel-perf-after.json,
            plus the feature shots in FEATURE_SHOTS (docs/features.md); its client-smoke.mjs run gives the
            client setup page (client-setup-*.png). Needs node and Playwright with Chromium.
    brand   scripts/brand_art.py build (brand-server-icon.png, brand-drink.png; it also rebuilds the
            committed art, byte-reproducibly: a no-op unless a pixel grid changed)
    setup   scripts/setup_screenshot.py (setup-mock.svg: `mcctl setup --mock` with tests/e2e/answers.yml)
    dashboard  scripts/dashboard_screenshot.py (mcctl-dashboard-{overview,confirm,palette}.svg: the terminal
            dashboard at 120x36 against the tests' fakes, tests/dashboard_fakes.py; every tab on demand with
            `scripts/dashboard_screenshot.py --all -o DIR`)
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "docs" / "images"
# ui.smoke.mjs's screenshot-dir shots that docs/features.md shows
FEATURE_SHOTS = (
    "panel-desktop.png",
    "panel-phone.png",
    "panel-console.png",
    "panel-worlds-desktop.png",
    "panel-seeds-desktop.png",
    "panel-restore-desktop.png",
    "panel-confirm-desktop.png",
    "client-setup-desktop.png",
    "client-setup-phone.png",
)
SOURCES = ("panel", "brand", "setup", "dashboard")


def panel() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "PANEL_SHOTS": "after", "PANEL_SHOTS_DIR": str(IMAGES)}
        smoke = ["--import", "./watchdog/test/register.mjs", "watchdog/test/ui.smoke.mjs"]
        cmd = ["node", "--no-warnings", *smoke, tmp]
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        if proc.returncode != 0 or "0 failure(s)" not in proc.stdout:
            sys.exit(f"ui.smoke.mjs failed:\n{proc.stdout[-4000:]}{proc.stderr[-4000:]}")
        for name in FEATURE_SHOTS:
            shutil.copyfile(Path(tmp) / name, IMAGES / name)


def brand() -> None:
    subprocess.run([sys.executable, "scripts/brand_art.py", "build"], cwd=ROOT, check=True)


def setup() -> None:
    out = IMAGES / "setup-mock.svg"
    # the wizard's prerequisites step looks for the venv's tools (ansible-playbook) on PATH
    path = os.pathsep.join([str(Path(sys.executable).parent), os.environ.get("PATH", "")])
    env = {**os.environ, "PATH": path}
    cmd = [sys.executable, "scripts/setup_screenshot.py", "-o", str(out)]
    subprocess.run(cmd, cwd=ROOT, env=env, check=True)


def dashboard() -> None:
    cmd = [sys.executable, "scripts/dashboard_screenshot.py", "-o", str(IMAGES)]
    subprocess.run(cmd, cwd=ROOT, check=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--only", nargs="+", choices=SOURCES, default=list(SOURCES), help="sources to regenerate")
    args = ap.parse_args(argv)
    IMAGES.mkdir(parents=True, exist_ok=True)
    for source in args.only:
        {"panel": panel, "brand": brand, "setup": setup, "dashboard": dashboard}[source]()
        print(f"{source}: done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
