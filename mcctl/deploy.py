"""`mcctl deploy`: run site.yml from this machine with the same secrets CI uses (design §3, §4).

Run from a product checkout against a config repo (`-C <config repo>`):

1. `mcctl render` into <config>/.mcctl/rendered (refuses placeholders).
2. `terraform init -backend-config=<rendered state.tfbackend>` and `terraform output -json` in the product's
   terraform/, with TF_DATA_DIR=<config>/.mcctl/terraform so each config repo keeps its own backend state.
   Credentials come from the shell plus the config repo's .env (mcctl.envfile; the shell wins).
3. `ansible-playbook -i <rendered inventory> site.yml` in the product's ansible/, with an allowlisted
   environment plus the MC_SECRET_* values. Secrets are captured, never printed, never on a command line.
   The pinned galaxy collections come from mcctl's own directory (mcctl/galaxy.py), never ~/.ansible.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mcctl import galaxy, remote, render, tf, ui
from mcctl.configrepo import ConfigRepo
from mcctl.schema import ServerConfig
from mcctl.secrets import ENV_PREFIX, SECRETS


class DeployError(RuntimeError):
    """A precondition of the deploy failed; the message says how to fix it."""


def exe(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise DeployError(f"`{name}` not found on PATH")
    return path


def terraform_for(repo: ConfigRepo, product_root: Path, purpose: str = "deploy") -> tuple[tf.Terraform, Path]:
    """Render (refusing placeholders), then `terraform init` with the rendered backend. Returns the
    initialised Terraform and the rendered directory."""
    out = repo.root / ".mcctl" / "rendered"
    try:
        render.render(repo, out)
    except render.RenderError as e:
        raise DeployError(f"refusing to {purpose} placeholder values:\n  " + "\n  ".join(e.errors)) from None
    try:
        terraform = tf.Terraform(product_root / "terraform", tf.environment(repo.root))
        terraform.init(out / render.BACKEND_FILE)
    except tf.TfError as e:
        raise DeployError(str(e)) from None
    return terraform, out


def outputs(terraform: tf.Terraform) -> dict[str, Any]:
    try:
        return terraform.outputs()
    except tf.TfError as e:
        raise DeployError(str(e)) from None


def secret_env(outputs: Mapping[str, Any]) -> dict[str, str]:
    """MC_SECRET_* variables from Terraform outputs. Unset optional secrets become ""."""
    env: dict[str, str] = {}
    missing: list[str] = []
    for s in SECRETS:
        value = outputs.get(s.output)
        if value is None or value == "":
            if s.required:
                missing.append(s.output)
                continue
            value = ""
        if not isinstance(value, str):
            raise DeployError(f"Terraform output {s.output} isn't a string")
        env[s.env] = value
    if missing:
        raise DeployError(
            f"Terraform outputs missing or empty: {', '.join(missing)}. Has `terraform apply` run?"
        )
    return env


# What ansible-playbook may inherit from the shell. Everything else (CLOUDFLARE_API_TOKEN, GITHUB_TOKEN,
# AWS_*, TAILSCALE_* and the other admin credentials from .env) stays out of its environment, so no role,
# lookup or plugin can read or leak them.
ENV_ALLOW = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TERM",
        "TMPDIR",
        "LANG",
        "LANGUAGE",
        "TZ",
        "SSH_AUTH_SOCK",
        "VIRTUAL_ENV",
        "PYTHONPATH",
        "NO_COLOR",
        "COLUMNS",
    }
)
ENV_ALLOW_PREFIXES = ("ANSIBLE_", "LC_")


def playbook_env(
    shell: Mapping[str, str], secrets: Mapping[str, str], collections: Path | None = None
) -> dict[str, str]:
    """Allowlisted shell variables plus the MC_SECRET_* values (stale MC_SECRET_* from the shell dropped);
    with `collections`, Ansible uses exactly that collections directory (galaxy.playbook_env)."""
    keep = {
        k: v
        for k, v in shell.items()
        if (k in ENV_ALLOW or k.startswith(ENV_ALLOW_PREFIXES)) and not k.startswith(ENV_PREFIX)
    }
    pinned = galaxy.playbook_env(collections) if collections is not None else {}
    return keep | pinned | dict(secrets)


def collections(product_root: Path) -> Path:
    """The pinned Ansible collections for product_root (installed if needed), as a DeployError on failure."""
    try:
        return galaxy.ensure(product_root)
    except galaxy.GalaxyError as e:
        raise DeployError(f"{e}\n  (by hand: cd {product_root} && {galaxy.MANUAL})") from None


def playbook_argv(inventory: Path, check: bool) -> list[str]:
    return [
        exe("ansible-playbook"),
        "-i",
        str(inventory),
        "site.yml",
        *(["--check", "--diff"] if check else []),
    ]


def run(
    repo: ConfigRepo,
    product_root: Path,
    *,
    check: bool = False,
    pinned_collections: Callable[[Path], Path] = collections,
    login: Callable[[ServerConfig], int] = remote.login_check,
) -> int:
    """Render, read the secrets from Terraform, run site.yml; returns ansible-playbook's exit code."""
    say = ui.stdout()
    with say.spinner("the pinned Ansible collections (ansible/requirements.yml)"):
        coll = pinned_collections(product_root)
    with say.spinner("render, terraform init and terraform output (the deploy's secrets)"):
        terraform, out = terraform_for(repo, product_root)
        env = secret_env(outputs(terraform))
    argv = playbook_argv(out / render.INVENTORY_FILE, check)
    if say.rich:
        say.ok(f"secrets from terraform output: {', '.join(sorted(env))} (values not shown)")
        say.section(f"ansible-playbook site.yml{' --check --diff' if check else ''}")
    else:
        print(f"secrets from terraform output: {', '.join(sorted(env))} (values not shown)", flush=True)
    # Ansible captures ssh's output: Tailscale's check URL (admin SSH rule `check`) would never show.
    if say.rich:
        say.dim(remote.CHECK_NOTE)
    else:
        print(remote.CHECK_NOTE, flush=True)
    if login(repo.server) != 0:
        print(f"(`ssh {remote.target(repo.server)} true` failed; ansible-playbook will say why)", flush=True)
    rc = subprocess.run(
        argv, cwd=product_root / "ansible", env=playbook_env(os.environ, env, coll), check=False
    ).returncode
    if say.rich:
        if rc == 0:
            say.ok("deploy finished (ansible-playbook exit 0)")
        else:
            say.fail(
                f"ansible-playbook failed (exit {rc})",
                "the first `fatal:` / `failed:` task above says why",
                f"fix it and run `mcctl deploy -C {repo.root}` again",
            )
    return rc
