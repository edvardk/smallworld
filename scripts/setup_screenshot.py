#!/usr/bin/env python3
"""Render a sample `mcctl setup --mock` screen as SVG for the docs (docs/images/setup-mock.svg).

Runs the real wizard (mcctl.init) against the mock providers (mcctl.mock, the same fakes the tests and the
e2e-mock workflow use) with tests/e2e/answers.yml, through a recording rich UI of a fixed width, and keeps
the steps from FIRST to LAST (default: the Cloudflare admin token through the GitHub tokens: the panels and
permission tables). Nothing real is contacted; every credential is a mock value and none is ever printed.

    python scripts/setup_screenshot.py [-o docs/images/setup-mock.svg] [--first 4] [--last 7]
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcctl import init, mock, ui, yamlio

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "docs" / "images" / "setup-mock.svg"
MOCK_SHA = "0" * 40  # the pin shown (the mock never fetches it)


class Recorder(ui.UI):
    """Records only the steps first..last (a header starts each step)."""

    def __init__(self, first: int, last: int, **kw: Any) -> None:  # noqa: ANN401
        super().__init__(rich=True, record=True, **kw)
        self.first, self.last = first, last
        self.console.record = False

    def header(self, n: int, total: int, title: str, **kw: Any) -> None:  # noqa: ANN401
        if n == self.first:
            self.console.record = True
        elif n == self.last + 1:
            self.console.record = False
        super().header(n, total, title, **kw)


def render(first: int, last: int, width: int) -> str:
    answers = yamlio.load((ROOT / "tests/e2e/answers.yml").read_text())
    with tempfile.TemporaryDirectory(prefix="mcctl-shot-") as tmp, open("/dev/null", "w") as sink:
        root = Path(tmp) / "mc-config"
        talk = Recorder(first, last, answers=answers, mock=True, env={}, out=sink, width=width)
        opts = init.Options(mock=True, product_sha=MOCK_SHA)
        rc = init.run(root, talk, mock.deps(root), opts, product_root=ROOT)
        if rc != 0:
            raise SystemExit(f"the mock setup failed (exit {rc})")
        svg = talk.export_svg("mcctl setup --mock")
        # The temporary folder shows as ~/; the spans it shortens lose their fixed textLength (monospace).
        prefix = str(Path(tmp).resolve()) + "/"
        return re.sub(
            r'(<text [^>]*?) textLength="[0-9.]+"([^>]*>[^<]*?' + re.escape(prefix) + ")",
            r"\1\2",
            svg,
        ).replace(prefix, "~/")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--output", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--first", type=int, default=4, help="first step recorded (default 4)")
    ap.add_argument("--last", type=int, default=7, help="last step recorded (default 7)")
    ap.add_argument("--width", type=int, default=100, help="terminal columns (default 100)")
    args = ap.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(args.first, args.last, args.width))
    print(args.output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
