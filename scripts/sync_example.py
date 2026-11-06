#!/usr/bin/env python3
"""Regenerate examples/config-repo/ from template/ (the CI fixture of a config repo).

The example is the template with a fake pin (all-zero SHA, v0.0.0) and the template's placeholder values
(example.com, example-owner): `mcctl validate` passes with warnings, `mcctl render` refuses it. Product CI
calls the reusable workflow with it, and tests/test_configrepo.py fails when it drifts from the template.
Run after changing template/ (the update-lock workflow runs it after bumping the locks).
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcctl import configrepo

EXAMPLE_SHA = "0" * 40
EXAMPLE_VERSION = "v0.0.0"


def main() -> int:
    dest = configrepo.EXAMPLE_DIR
    if dest.exists():
        shutil.rmtree(dest)
    for path in configrepo.materialize(dest, sha=EXAMPLE_SHA, version=EXAMPLE_VERSION):
        print(path.relative_to(configrepo.TEMPLATE_DIR.parent))
    return 0


if __name__ == "__main__":
    sys.exit(main())
