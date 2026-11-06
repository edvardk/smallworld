"""End to end: re-executing into the pinned product (mcctl/product.py, cli._pinned). Nothing is mocked.

A real `python -m mcctl plan` runs from inside a dirty product checkout (its current directory, which `-m` puts
first on sys.path) against a config repo pinned to another commit, whose cache is ready. It must re-execute
exactly once, into the pinned code, and never import the dirty checkout again (the P4 bug: an endless loop).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from conftest import REPO, TEST_SHA, make_config_repo

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
LOG_LINE = (
    "\nimport os as _os\n"
    "if _os.environ.get('MCCTL_TEST_LOG'):\n"
    "    with open(_os.environ['MCCTL_TEST_LOG'], 'a') as _fh:\n"
    "        _fh.write({tag!r} + ' ' + __file__ + '\\n')\n"
)


def copy_product(dest: Path) -> None:
    """The working tree as it is now (tracked + untracked, not ignored), as a fresh one-commit git repo."""
    files = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "-z", "-co", "--exclude-standard"],
        capture_output=True,
        check=True,
    ).stdout.split(b"\0")
    for raw in files:
        rel = raw.decode()
        if not rel or "__pycache__" in rel or not (REPO / rel).is_file():
            continue
        (dest / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO / rel, dest / rel)
    subprocess.run([*GIT, "-C", str(dest), "init", "-q"], check=True)
    subprocess.run([*GIT, "-C", str(dest), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(dest), "commit", "-q", "-m", "copy"], check=True)


def patch(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"{path}: anchor not found"
    path.write_text(text.replace(old, new, 1))


def pinned_cache(tmp_path: Path) -> tuple[Path, str]:
    """A ready cache entry (~/.cache/mcctl/<sha>) holding a marked copy of the product at a fake pinned commit."""
    src = tmp_path / "pinned-src"
    src.mkdir()
    copy_product(src)
    (src / "mcctl/__init__.py").write_text(
        (src / "mcctl/__init__.py").read_text() + LOG_LINE.format(tag="pinned")
    )
    # The pinned code proves it ran: plan stops right after _pinned() decided not to re-execute.
    patch(
        src / "mcctl/cli.py",
        "    def go() -> int:\n        terraform, out = deploy_mod.terraform_for",
        '    print(f"PINNED-PLAN-RAN {root}")\n    return 0\n\n'
        "    def go() -> int:\n        terraform, out = deploy_mod.terraform_for",
    )
    subprocess.run([*GIT, "-C", str(src), "commit", "-q", "-am", "pinned"], check=True)
    sha = subprocess.run(
        ["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    entry = tmp_path / "cache" / "mcctl" / sha
    entry.mkdir(parents=True)
    src.rename(entry / "src")
    (entry / "venv" / "bin").mkdir(parents=True)
    python = entry / "venv" / "bin" / "python"  # stands in for the cached venv (this interpreter has PyYAML)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    (entry / "collections").mkdir()
    (entry / ".ready").write_text(sha + "\n")
    return entry, sha


def dirty_checkout(tmp_path: Path) -> Path:
    dirty = tmp_path / "dirty"
    dirty.mkdir()
    copy_product(dirty)
    init = dirty / "mcctl/__init__.py"
    init.write_text(init.read_text() + LOG_LINE.format(tag="dirty"))  # uncommitted: the checkout is dirty
    return dirty


def config_pinned_to(tmp_path: Path, sha: str) -> Path:
    config = make_config_repo(tmp_path / "config", renderable=True)
    wf = config / ".github/workflows/server.yml"
    wf.write_text(wf.read_text().replace(TEST_SHA, sha))
    return config


def run_mcctl(cwd: Path, args: list[str], tmp_path: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {
        k: v for k, v in os.environ.items() if k not in ("MCCTL_PRODUCT_DIR", "PYTHONPATH", "PYTHONSAFEPATH")
    }
    env |= {
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "MCCTL_TEST_LOG": str(tmp_path / "imports.log"),
        **extra,
    }
    return subprocess.run(
        [sys.executable, "-m", "mcctl", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=60
    )


def imports(tmp_path: Path) -> list[str]:
    log = tmp_path / "imports.log"
    return log.read_text().splitlines() if log.exists() else []


def test_plan_from_a_dirty_checkout_reexecs_once_into_the_pinned_code(tmp_path) -> None:
    entry, sha = pinned_cache(tmp_path)
    dirty = dirty_checkout(tmp_path)
    config = config_pinned_to(tmp_path, sha)

    proc = run_mcctl(dirty, ["plan", "-C", str(config)], tmp_path)

    assert proc.returncode == 0, proc.stderr
    pinned_root = (entry / "src").resolve()
    assert f"PINNED-PLAN-RAN {pinned_root}" in proc.stdout
    # Exactly one re-exec: the dirty mcctl started, then the pinned one, and nothing else ever ran.
    assert imports(tmp_path) == [
        f"dirty {dirty / 'mcctl/__init__.py'}",
        f"pinned {pinned_root / 'mcctl/__init__.py'}",
    ]


def test_a_child_that_isnt_the_pinned_code_fails_instead_of_looping(tmp_path) -> None:
    entry, sha = pinned_cache(tmp_path)
    dirty = dirty_checkout(tmp_path)
    config = config_pinned_to(tmp_path, sha)

    # What a re-executed child looks like when it still imports other code: MCCTL_PRODUCT_DIR names the
    # pinned checkout, but the code running is the dirty one (the old `python -m` child did exactly this).
    proc = run_mcctl(dirty, ["plan", "-C", str(config)], tmp_path, MCCTL_PRODUCT_DIR=str(entry / "src"))

    assert proc.returncode == 1
    assert "refusing to re-execute again" in proc.stderr
    assert imports(tmp_path) == [f"dirty {dirty / 'mcctl/__init__.py'}"]


def test_a_modified_cache_is_not_used(tmp_path) -> None:
    from mcctl import product
    from mcctl.configrepo import Pin

    entry, sha = pinned_cache(tmp_path)
    assert product.cache_ok(entry, sha)
    (entry / "src/mcctl/cli.py").write_text("tampered\n")
    assert not product.cache_ok(entry, sha)  # .ready alone isn't trusted: the checkout must be clean at sha
    (entry / "src/mcctl/cli.py").unlink()
    subprocess.run(["git", "-C", str(entry / "src"), "checkout", "-q", "--", "mcctl/cli.py"], check=True)
    (entry / ".ready").write_text("0" * 40)
    assert not product.cache_ok(entry, sha)
    assert (
        product.resolve(Pin("o/r", sha, None), {product.ENV_OVERRIDE: str(entry / "src")}).root
        == (entry / "src").resolve()
    )
