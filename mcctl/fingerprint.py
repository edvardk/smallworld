"""The Worker fingerprint (v1.5.2): what `mcctl apply` changes for the Worker, as one sha256.

The panel used to compare the Worker's version label (`mcctl render`'s version.json) with the server's
installed version, so every release asked for a local `mcctl render` + `mcctl apply` just to move the label.
Now both sides carry a fingerprint of the product files that decide what the Worker runs and what Terraform
applies, computed by this one function from the release's own files:

- watchdog/worker.js (the script Terraform uploads) and watchdog/panel/ (the static assets `mcctl render`
  stages and Terraform uploads; not panel/pack/, which render builds from the config's art);
- terraform/: every file Terraform reads in the root module (*.tf, *.tf.json, *.tftpl, the provider lock
  file): the Worker's bindings, its TARGET config, compatibility date, cron, Access, and every other resource
  a release could change, so "fingerprint changed" = "this release needs a local apply".

Not in it, on purpose:

- the product version and the config repo's pin (labels: a release that changes none of the files above
  leaves the Worker as it is);
- the config repo's rendered values (tfvars, the brand pack): deployment values the operator changes and
  applies themselves; the deploy (server side) and `mcctl render` + `mcctl apply` (Worker side) would
  otherwise have to agree on one server.yml at one moment, which they don't (a push deploys before anyone
  applies). Product files alone give both sides the same value for the same release;
- mcctl/render.py (it changes for Ansible variables, docs and comments far more often than for the Worker;
  v1.5.1 changed a comment in it). A render change that alters an existing Terraform variable's value for
  the same server.yml without touching terraform/ is the one case this misses: `mcctl doctor`'s
  `terraform plan` row shows it.

`mcctl render` writes it into the Worker's version.json (what the Worker runs) and into the rendered
group_vars (mc_worker_fingerprint: each deploy records it in product-version.json, the agent reports it);
`mcctl product-update` and the deploy summary compare it between two releases (productupdate.worker_diff).
Changing this definition changes every fingerprint once: the panel then asks for one apply.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

PRODUCT_ROOT = Path(__file__).resolve().parent.parent
WORKER_SCRIPT = "watchdog/worker.js"
PANEL_DIR = "watchdog/panel"
PANEL_SKIP = ("watchdog/panel/pack",)  # render's own pack (from the config's art), never the product's
TERRAFORM_DIR = "terraform"
TERRAFORM_SUFFIXES = (".tf", ".tf.json", ".tftpl")
TERRAFORM_LOCK = ".terraform.lock.hcl"
SKIP_PARTS = {".terraform", "__pycache__", "node_modules", ".pytest_cache"}
PREFIX = b"smallworld-worker-fingerprint/1\n"  # the definition's version


def worker_inputs(root: Path = PRODUCT_ROOT) -> dict[str, str] | None:
    """{relative path: sha256} of the files the fingerprint covers (None: not a product checkout)."""
    script = root / WORKER_SCRIPT
    if not script.is_file() or not (root / TERRAFORM_DIR).is_dir():
        return None
    paths = [script]
    if (root / PANEL_DIR).is_dir():
        paths += sorted(p for p in (root / PANEL_DIR).rglob("*") if p.is_file())
    paths += sorted(
        p
        for p in (root / TERRAFORM_DIR).iterdir()
        if p.is_file() and (p.name.endswith(TERRAFORM_SUFFIXES) or p.name == TERRAFORM_LOCK)
    )
    out: dict[str, str] = {}
    for path in paths:
        rel = path.relative_to(root).as_posix()
        if SKIP_PARTS & set(rel.split("/")) or any(rel.startswith(d + "/") for d in PANEL_SKIP):
            continue
        out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def of_inputs(inputs: dict[str, str]) -> str:
    """The fingerprint of worker_inputs()' result (sha256 hex)."""
    h = hashlib.sha256(PREFIX)
    for rel in sorted(inputs):
        h.update(f"{rel}\0{inputs[rel]}\n".encode())
    return h.hexdigest()


def worker_fingerprint(root: Path = PRODUCT_ROOT) -> str | None:
    """The Worker fingerprint of a product checkout (sha256 hex), None when it isn't one."""
    inputs = worker_inputs(root)
    return None if inputs is None else of_inputs(inputs)
