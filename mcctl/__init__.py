"""mcctl: deploy and operate a Minecraft server described by server.yml (docs/design/product-v1.md)."""

from __future__ import annotations

import sys

# The product version. Releases tag `v<__version__>`; pyproject.toml and CHANGELOG.md must agree
# (scripts/release.py check, tests/test_release.py).
__version__ = "1.5.2"

# The controller (mcctl, Terraform/Ansible driver) needs Python >= 3.12: the hash-pinned ansible-core in
# requirements/deploy.txt requires it, and mcctl builds the pinned product's venv with its own interpreter.
# The on-server scripts (scripts/{mc_rcon,notify,backup,restore,health}.py) are a separate world: stdlib
# only, Python >= 3.11 (Debian 12's python3). = pyproject.toml requires-python (tests/test_consistency.py).
MIN_PYTHON = (3, 12)


def python_problem(version: tuple[int, ...] = tuple(sys.version_info[:3])) -> str | None:
    """A message if this interpreter is too old for mcctl, else None."""
    if tuple(version[:2]) >= MIN_PYTHON:
        return None
    have = ".".join(map(str, version[:3]))
    want = ".".join(map(str, MIN_PYTHON))
    return (
        f"mcctl needs Python >= {want} (its pinned ansible-core requires it); this is Python {have} "
        f"({sys.executable}). Install a newer Python and run mcctl with it, e.g. "
        f"`pipx install --python python{want} ...` or `python{want} -m mcctl ...`. (The scripts that run "
        "on the server itself still only need Python 3.11.)"
    )
