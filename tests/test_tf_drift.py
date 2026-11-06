"""No perpetual drift: the configured values of the resources that drifted on a live deployment (every
`mcctl plan` showed 4 in-place updates without a config change) match what provider 5.28.1 reads back from
the API (tests/fixtures/tf_drift/api_state.json, recorded from that plan).

- cloudflare_d1_database (watchdog, telemetry): `ignore_changes = [read_replication]` (the provider plans an
  update while it isn't configured, though the API reports the default).
- cloudflare_r2_bucket_lock.backups: the rules sorted by id, the order the API returns them in.
- cloudflare_workers_script.watchdog: observability spelled out to the API's defaults.

The locals are evaluated with `terraform console` (no provider, no state; skipped without terraform, which
CI's lint job installs before pytest). `mcctl doctor`'s "terraform plan" check catches anything new live.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO

TF_DIR = REPO / "terraform"
API = json.loads((REPO / "tests/fixtures/tf_drift/api_state.json").read_text())
# Optional+computed attributes: left unset, the provider keeps the API's value (no diff).
COMPUTED = {"persist", "propagation_policy"}

needs_terraform = pytest.mark.skipif(
    shutil.which("terraform") is None, reason="terraform isn't installed (CI's lint job has it)"
)


def _tf(name: str) -> str:
    return (TF_DIR / name).read_text()


def _block(text: str, header: str) -> str:
    """The top-level block starting with `header` (up to its closing brace at column 0)."""
    m = re.search(rf"\n{re.escape(header)}\n.*?\n\}}\n", "\n" + text, re.S)
    assert m, f"{header!r} not found"
    return m.group(0)


def _locals(text: str, first_key: str) -> str:
    m = re.search(rf"\nlocals \{{\n  {re.escape(first_key)} = .*?\n\}}\n", "\n" + text, re.S)
    assert m, f"the locals block starting with {first_key} moved"
    return m.group(0)


def matches_api(config: Any, api: Any, where: str = "") -> list[str]:
    """Differences that would be planned: a configured value the API reads back otherwise, or an attribute
    the API returns that the configuration leaves null (unless it is computed)."""
    if isinstance(api, dict):
        if not isinstance(config, dict):
            return [f"{where or '.'}: configured {config!r}, the API has {api!r}"]
        out: list[str] = []
        for key in sorted(set(api) | set(config)):
            c, a = config.get(key), api.get(key)
            if c is None and (a is None or key in COMPUTED):
                continue
            out += matches_api(c, a, f"{where}.{key}")
        return out
    if isinstance(api, list):
        if not isinstance(config, list) or len(config) != len(api):
            return [f"{where}: configured {config!r}, the API has {api!r}"]
        return [
            d
            for i, (c, a) in enumerate(zip(config, api, strict=True))
            for d in matches_api(c, a, f"{where}[{i}]")
        ]
    return [] if config == api else [f"{where}: configured {config!r}, the API has {api!r}"]


def test_matches_api_reports_what_terraform_would_plan() -> None:
    api = {"a": 1, "b": {"c": 1, "persist": True}, "d": None}
    assert matches_api({"a": 1, "b": {"c": 1}}, api) == []  # persist: computed
    assert matches_api({"a": 1, "b": {}}, api) == [".b.c: configured None, the API has 1"]
    assert matches_api({"a": 1, "b": {"c": 1}, "d": 2}, api) == [".d: configured 2, the API has None"]
    assert matches_api([{"id": "b"}, {"id": "a"}], [{"id": "a"}, {"id": "b"}]) != []


def _console(tmp_path: Path, blocks: list[str], expr: str) -> Any:
    (tmp_path / "main.tf").write_text(
        'variable "r2_lock_days" {\n  type    = number\n  default = 7\n}\n' + "".join(blocks)
    )
    proc = subprocess.run(
        ["terraform", "console"],
        cwd=tmp_path,
        input=f"jsonencode({expr})\n",
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(json.loads(proc.stdout.strip()))


# --- D1 ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("file", "header"),
    [
        ("watchdog.tf", 'resource "cloudflare_d1_database" "watchdog" {'),
        ("monitoring.tf", 'resource "cloudflare_d1_database" "telemetry" {'),
    ],
)
def test_d1_databases_ignore_read_replication(file: str, header: str) -> None:
    block = _block(_tf(file), header)
    assert re.search(r"\n    ignore_changes = \[read_replication\]", block), block
    # Nothing else configured that the API reads back: only the required name/account (and count).
    assert API["cloudflare_d1_database"]["read_replication"] == {"mode": "disabled"}
    for attr in ("jurisdiction", "primary_location_hint", "fields"):
        assert f"  {attr} " not in block and API["cloudflare_d1_database"][attr] is None


# --- R2 bucket lock --------------------------------------------------------------------------------------


def test_bucket_lock_uses_the_sorted_rules() -> None:
    block = _block(_tf("cloudflare.tf"), 'resource "cloudflare_r2_bucket_lock" "backups" {')
    assert re.search(r"\n  rules\s+= local\.r2_lock_rules\n", block), block
    assert "sort(keys(local.r2_lock_rules_by_id))" in _locals(_tf("cloudflare.tf"), "r2_lock_rules_by_id")


@needs_terraform
def test_bucket_lock_rules_are_in_the_apis_order(tmp_path) -> None:
    rules = _console(tmp_path, [_locals(_tf("cloudflare.tf"), "r2_lock_rules_by_id")], "local.r2_lock_rules")
    ids = [r["id"] for r in rules]
    assert ids == sorted(ids) == [r["id"] for r in API["cloudflare_r2_bucket_lock"]["rules"]]
    assert matches_api(rules, API["cloudflare_r2_bucket_lock"]["rules"]) == []


# --- Worker observability ----------------------------------------------------------------------------------


def test_worker_uses_the_observability_local() -> None:
    block = _block(_tf("watchdog.tf"), 'resource "cloudflare_workers_script" "watchdog" {')
    assert "\n  observability = local.watchdog_observability\n" in block
    # The earlier fix (Durable Object migrations follow the live tag) still holds.
    assert "\n  migrations = local.watchdog_migrations\n" in block


@needs_terraform
def test_worker_observability_matches_the_api(tmp_path) -> None:
    obs = _console(
        tmp_path, [_locals(_tf("watchdog.tf"), "watchdog_observability")], "local.watchdog_observability"
    )
    assert matches_api(obs, API["cloudflare_workers_script"]["observability"]) == []
    assert obs["logs"]["invocation_logs"] is False  # never log request headers (the heartbeat token)
