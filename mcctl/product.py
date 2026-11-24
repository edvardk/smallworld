"""The pinned product (design §4): laptop and CI run the same code.

A config repo pins the product by commit SHA (its workflow's `uses:` line). Commands that run the product's
Terraform or Ansible (`plan`, `apply`, `deploy`, `bootstrap`) must run that commit, so when the mcctl that was
started isn't it, mcctl re-executes itself from a cached checkout:

    ${XDG_CACHE_HOME:-~/.cache}/mcctl/<sha>/
        src/            git checkout of <owner>/<repo> at <sha> (fetched with `gh auth git-credential`)
        venv/           python -m venv, then pip install --require-hashes --no-deps
                        -r src/requirements/deploy.txt
        collections/    ansible-galaxy collections from src/ansible/requirements.yml (pinned versions;
                        mcctl/galaxy.py checks them before each playbook, also for a checkout that is the pin)
        .ready          written last; a cache without it is rebuilt

`MCCTL_PRODUCT_DIR=<checkout>` overrides the lookup (development: run a working tree as if pinned).

The re-executed child is `<venv python> -P -c BOOTSTRAP <argv>`: `-P` keeps the current directory (which may
be another, dirty product checkout) off sys.path, and BOOTSTRAP puts the pinned checkout first, so the child
imports the pinned code and nothing else. The child gets MCCTL_PRODUCT_DIR set to that checkout; a process
that has it set and still isn't running that checkout fails hard instead of re-executing (cli._pinned), so
a re-exec can never loop.

The cache is verified on every use (`.ready` names the commit, the venv's scripts can start and the checkout
is clean at it), and builds are serialised per commit with a lock file, so two mcctl processes never build
into each other. It is built in place, never aside and renamed: pip writes the venv's absolute path into
every console script (ansible-playbook, ansible-galaxy).
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from mcctl import python_problem, schema
from mcctl.configrepo import Pin

ENV_OVERRIDE = "MCCTL_PRODUCT_DIR"
READY = ".ready"
REQUIREMENTS = Path("requirements") / "deploy.txt"
GH_CREDENTIAL_HELPER = "!gh auth git-credential"  # what `gh auth setup-git` configures
# What the re-executed child runs (`python -P -c BOOTSTRAP ARGS...`): the pinned checkout first on sys.path.
BOOTSTRAP = (
    "import os, sys; sys.path.insert(0, os.environ['MCCTL_PRODUCT_DIR']); "
    "from mcctl.cli import main; sys.exit(main())"
)


class ProductError(RuntimeError):
    pass


@dataclass(frozen=True)
class Product:
    root: Path  # the product checkout (has terraform/, ansible/, mcctl/)
    bin_dir: Path | None  # the cached venv's bin/ (None = use the current PATH)
    collections: Path | None

    def env(self, base: Mapping[str, str]) -> dict[str, str]:
        """Environment for a re-executed mcctl: the venv first on PATH, the pinned collections."""
        env = {k: v for k, v in base.items() if k not in ("PYTHONPATH", "PYTHONHOME")}
        env[ENV_OVERRIDE] = str(self.root)
        if self.bin_dir is not None:
            env["PATH"] = f"{self.bin_dir}{os.pathsep}{base.get('PATH', '')}"
            env["VIRTUAL_ENV"] = str(self.bin_dir.parent)
        if self.collections is not None:
            env["ANSIBLE_COLLECTIONS_PATH"] = str(self.collections)
        return env

    @property
    def python(self) -> str:
        return str(self.bin_dir / "python") if self.bin_dir is not None else sys.executable


def cache_root(env: Mapping[str, str]) -> Path:
    base = env.get("XDG_CACHE_HOME") or str(Path(env.get("HOME", str(Path.home()))) / ".cache")
    return Path(base) / "mcctl"


def checkout_sha(root: Path) -> str | None:
    """HEAD of a clean git checkout at root, else None (not a checkout, or uncommitted changes)."""
    try:
        head = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None
    return None if dirty else head


def _run(argv: Sequence[str], what: str) -> None:
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise ProductError(f"{what} failed:\n{(proc.stderr or proc.stdout).strip()}")


def build(pin: Pin, dest: Path) -> Product:
    """Fetch the pinned commit and build its venv + collections into dest. Built in place (the caller holds
    build_lock): a venv can't be moved, pip writes its absolute path into every console script's shebang
    (v1.5.5: a cache built aside and renamed had ansible-galaxy/ansible-playbook pointing at the temporary
    directory, "No such file or directory"). `.ready`, written last, marks it finished; a failed build is
    removed."""
    git = shutil.which("git")
    if git is None:
        raise ProductError("`git` not found on PATH")
    if problem := python_problem():
        raise ProductError(problem)
    shutil.rmtree(dest, ignore_errors=True)
    src = dest / "src"
    src.mkdir(parents=True)
    url = f"https://github.com/{pin.repository}.git"
    try:
        _run([git, "-C", str(src), "init", "-q"], "git init")
        _run(
            [git, "-C", str(src), "-c", "credential.helper=",
             "-c", f"credential.helper={GH_CREDENTIAL_HELPER}", "fetch", "-q", "--depth", "1", url, pin.sha],
            f"fetching {pin.repository}@{pin.sha[:12]} (private product: `gh auth login` first)",
        )  # fmt: skip
        _run([git, "-C", str(src), "checkout", "-q", "--detach", "FETCH_HEAD"], "git checkout")
        if checkout_sha(src) != pin.sha:
            raise ProductError(f"fetched commit isn't {pin.sha}")
        if not (src / REQUIREMENTS).is_file():
            raise ProductError(
                f"the pinned product ({pin.sha[:12]}) has no {REQUIREMENTS}: it predates the "
                "cached-checkout layout. Run mcctl from a product checkout at that commit, or set "
                f"{ENV_OVERRIDE}"
            )
        _run([sys.executable, "-m", "venv", str(dest / "venv")], "python -m venv")
        _run(
            [str(dest / "venv/bin/pip"), "install", "-q", "--require-hashes", "--no-deps",
             "-r", str(src / REQUIREMENTS)],
            "pip install (hash-pinned)",
        )  # fmt: skip
        _run(
            [str(dest / "venv/bin/ansible-galaxy"), "collection", "install", "-r",
             str(src / "ansible/requirements.yml"), "-p", str(dest / "collections")],
            "ansible-galaxy collection install",
        )  # fmt: skip
        (dest / READY).write_text(pin.sha + "\n")
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return cached(dest)


def cached(dest: Path) -> Product:
    dest = dest.resolve()
    return Product(dest / "src", dest / "venv" / "bin", dest / "collections")


def cache_ok(dest: Path, sha: str) -> bool:
    """A finished cache for `sha`: `.ready` names it, the venv exists, its scripts can start and src/ is a
    clean checkout at it."""
    try:
        ready = (dest / READY).read_text().strip()
    except OSError:
        return False
    bin_dir = dest / "venv" / "bin"
    return (
        ready == sha
        and (bin_dir / "python").exists()
        and scripts_ok(bin_dir)
        and checkout_sha(dest / "src") == sha
    )


def scripts_ok(bin_dir: Path) -> bool:
    """Every console script in a venv's bin/ names an interpreter that exists. False for a venv that was moved
    after pip installed into it (a cache built by mcctl before v1.5.5): it is rebuilt."""
    try:
        files = [f for f in bin_dir.iterdir() if f.is_file() and not f.is_symlink()]
    except OSError:
        return False
    for f in files:
        try:
            with f.open("rb") as fh:
                first = fh.readline(4096)
        except OSError:
            return False
        if not first.startswith(b"#!/"):
            continue
        interpreter = first[2:].split(maxsplit=1)[0].decode("utf-8", "replace")
        if "/venv/bin/" in interpreter and not Path(interpreter).exists():
            return False
    return True


@contextlib.contextmanager
def build_lock(dest: Path) -> Iterator[None]:
    """Exclusive per-commit lock (`<sha>.lock` next to the cache): one build at a time."""
    import fcntl  # POSIX only: imported here so mcctl itself imports on Windows (and says to use WSL2)

    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest.with_name(f"{dest.name}.lock"), "a") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"mcctl: waiting for another mcctl preparing {dest} ...", file=sys.stderr)
            fcntl.flock(fh, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def resolve(pin: Pin, env: Mapping[str, str], *, running: Path = schema.PRODUCT_ROOT) -> Product:
    """The product checkout to run for this pin (building the cache if needed)."""
    override = env.get(ENV_OVERRIDE)
    if override:
        root = Path(override).resolve()
        if not (root / "mcctl").is_dir() or not (root / "terraform").is_dir():
            raise ProductError(f"{ENV_OVERRIDE}={override} isn't a product checkout")
        return Product(root, None, None)
    if checkout_sha(running) == pin.sha:
        return Product(running.resolve(), None, None)
    dest = cache_root(env) / pin.sha
    if cache_ok(dest, pin.sha):
        return cached(dest)
    with build_lock(dest):
        if cache_ok(dest, pin.sha):  # built by another mcctl while we waited
            return cached(dest)
        what = "rebuilding (incomplete or modified)" if dest.exists() else "preparing"
        print(
            f"mcctl: {what} the pinned product {pin.repository}@{pin.sha[:12]} in {dest} ...", file=sys.stderr
        )
        return build(pin, dest)


def reexec_argv(product: Product, argv: Sequence[str]) -> list[str] | None:
    """argv to re-run this command in `product`, or None if this process already is it."""
    if product.root.resolve() == schema.PRODUCT_ROOT.resolve():
        return None
    return [product.python, "-P", "-c", BOOTSTRAP, *argv]
