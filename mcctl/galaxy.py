"""The pinned Ansible collections (`ansible/requirements.yml`) for every playbook mcctl runs.

`bootstrap.yml` and `site.yml` use modules from galaxy collections (ansible.posix, community.general),
which ansible-core doesn't ship. mcctl never relies on whatever happens to be in the user's ~/.ansible: before
it runs a playbook it makes sure the exact pinned versions are installed in a directory of its own and points
Ansible at that directory only (ANSIBLE_COLLECTIONS_PATH, no sys.path scan).

Where (`location`):

- a cached pinned product (`~/.cache/mcctl/<sha>/src`, mcctl/product.py): its `collections/` beside src/;
- any other checkout (the running one when it is the pin, or MCCTL_PRODUCT_DIR):
  `${XDG_CACHE_HOME:-~/.cache}/mcctl/collections/<sha256 of requirements.yml, 16 hex>/`, shared by every
  checkout with the same requirements and outside the git tree.

`ensure` verifies the installed versions (each collection's MANIFEST.json) against the pins, installs with
`ansible-galaxy collection install -r ... -p <tmp>` when they don't match (built aside, then renamed, under a
lock), verifies again and fails with the mismatch otherwise.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import yaml

from mcctl import product, yamlio

REQUIREMENTS = Path("ansible") / "requirements.yml"
MANUAL = "ansible-galaxy collection install -r ansible/requirements.yml"

# (argv, env) -> completed process (output captured); tests replace it with a fake ansible-galaxy.
Runner = Callable[[Sequence[str], Mapping[str, str]], subprocess.CompletedProcess[str]]


class GalaxyError(RuntimeError):
    """The pinned collections aren't (and couldn't be made) available; the message says how to fix it."""


def _run(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, env=dict(env))
    except OSError as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def _find(env: Mapping[str, str]) -> str | None:
    """ansible-galaxy beside this interpreter (mcctl's venv, even when not activated), else from PATH."""
    beside = Path(sys.executable).parent / "ansible-galaxy"
    return str(beside) if beside.is_file() else shutil.which("ansible-galaxy", path=env.get("PATH"))


# What ansible-galaxy may inherit: the basics, proxy/CA settings and ANSIBLE_*; never admin credentials.
_GALAXY_ENV = frozenset(
    {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LANGUAGE", "TZ", "SSL_CERT_FILE", "SSL_CERT_DIR",
     "REQUESTS_CA_BUNDLE", "HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "NO_PROXY", "no_proxy"}
)  # fmt: skip


def galaxy_env(env: Mapping[str, str]) -> dict[str, str]:
    return {k: v for k, v in env.items() if k in _GALAXY_ENV or k.startswith(("ANSIBLE_", "LC_"))}


def pins(product_root: Path) -> dict[str, str]:
    """{namespace.name: version} from the product's ansible/requirements.yml (each must pin a version)."""
    path = product_root / REQUIREMENTS
    try:
        data = yamlio.load(path.read_text())
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise GalaxyError(f"can't read {path}: {e}") from None
    items = data.get("collections") if isinstance(data, dict) else None
    out: dict[str, str] = {}
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict) or not item.get("name") or not item.get("version"):
            raise GalaxyError(f"{path}: every collection needs a name and a pinned version ({item!r})")
        out[str(item["name"])] = str(item["version"])
    return out


def installed(path: Path) -> dict[str, str]:
    """{namespace.name: version} of the collections installed under path (ansible_collections/<ns>/<name>)."""
    out: dict[str, str] = {}
    for manifest in sorted(path.glob("ansible_collections/*/*/MANIFEST.json")):
        try:
            info = json.loads(manifest.read_text())["collection_info"]
            out[f"{info['namespace']}.{info['name']}"] = str(info["version"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
    return out


def problems(want: Mapping[str, str], have: Mapping[str, str]) -> list[str]:
    out = []
    for name, version in sorted(want.items()):
        found = have.get(name)
        if found is None:
            out.append(f"{name} {version} not installed")
        elif found != version:
            out.append(f"{name} is {found}, requirements.yml pins {version}")
    return out


def location(product_root: Path, env: Mapping[str, str]) -> Path:
    root = product_root.resolve()
    if root.name == "src" and (root.parent / product.READY).is_file():
        return root.parent / "collections"  # a cached pinned product (built with its collections)
    digest = hashlib.sha256((root / REQUIREMENTS).read_bytes()).hexdigest()[:16]
    return product.cache_root(env) / "collections" / digest


def status(product_root: Path, env: Mapping[str, str]) -> tuple[Path, dict[str, str], list[str]]:
    """(directory, pins, problems) without installing anything."""
    want = pins(product_root)
    dest = location(product_root, env)
    return dest, want, problems(want, installed(dest))


def playbook_env(path: Path) -> dict[str, str]:
    """What ansible-playbook needs to use exactly these collections (never ~/.ansible or site-packages)."""
    return {"ANSIBLE_COLLECTIONS_PATH": str(path), "ANSIBLE_COLLECTIONS_SCAN_SYS_PATH": "False"}


def describe(want: Mapping[str, str]) -> str:
    return ", ".join(f"{k} {v}" for k, v in sorted(want.items()))


def ensure(
    product_root: Path,
    env: Mapping[str, str] | None = None,
    *,
    run: Runner | None = None,
    galaxy: str | None = None,
) -> Path:
    """The directory holding exactly the pinned collections (installing them first if needed)."""
    env = dict(os.environ if env is None else env)
    run = run or _run
    dest, want, wrong = status(product_root, env)
    if not wrong:
        return dest
    with product.build_lock(dest):
        dest, want, wrong = status(product_root, env)
        if not wrong:  # installed by another mcctl while we waited
            return dest
        exe = galaxy or _find(env)
        if exe is None:
            raise GalaxyError(
                "`ansible-galaxy` not found on PATH (it comes with ansible-core: "
                "pip install --require-hashes --no-deps -r requirements/deploy.txt)"
            )
        tmp = dest.with_name(f"{dest.name}.tmp-{os.getpid()}")
        shutil.rmtree(tmp, ignore_errors=True)
        argv = [exe, "collection", "install", "-r", str(product_root / REQUIREMENTS), "-p", str(tmp)]
        proc = run(argv, galaxy_env(env) | {"ANSIBLE_COLLECTIONS_PATH": str(tmp)})
        try:
            if proc.returncode != 0:
                raise GalaxyError(
                    f"installing the pinned Ansible collections ({describe(want)}) failed:\n"
                    f"{(proc.stderr or proc.stdout).strip()}"
                )
            if still := problems(want, installed(tmp)):
                raise GalaxyError("ansible-galaxy didn't install the pinned collections: " + "; ".join(still))
            shutil.rmtree(dest, ignore_errors=True)
            tmp.rename(dest)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    return dest
