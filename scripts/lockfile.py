"""Shared helpers for the lock files (server.lock.yml, plugins.lock.yml). Controller-side only."""

from __future__ import annotations

import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
# The product's reference locks: what new config repos start with (template/), and what the product's
# config keys (server-config/keys/) and tests are checked against. A config repo has its own copies.
LOCK_DIR = REPO_ROOT / "template"
SERVER_LOCK = LOCK_DIR / "server.lock.yml"
PLUGINS_LOCK = LOCK_DIR / "plugins.lock.yml"
USER_AGENT = "edvardk/smallworld (lock updater)"


def http_get(url: str, timeout: float = 60) -> bytes:
    if not url.startswith("https://"):
        raise ValueError(f"refusing non-https URL: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})  # noqa: S310 - https enforced above
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
        return resp.read()


def get_json(url: str) -> Any:  # noqa: ANN401 - arbitrary JSON
    return json.loads(http_get(url))


def digest(data: bytes, algo: str) -> str:
    return hashlib.new(algo, data).hexdigest()


def load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text())


def dump_yaml(path: Path, data: dict[str, Any], header: str) -> None:
    body = yaml.safe_dump(data, sort_keys=False, default_flow_style=False)
    path.write_text(f"{header.rstrip()}\n{body}")


def download_verified(url: str, expected: dict[str, str]) -> bytes:
    """Download and check every given {algo: hexdigest}. Raises on any mismatch."""
    data = http_get(url, timeout=300)
    for algo, want in expected.items():
        got = digest(data, algo)
        if got != want:
            raise ValueError(f"{algo} mismatch for {url}: expected {want}, got {got}")
    return data
