"""Terraform from mcctl (design §4 `plan` / `apply`, used by `deploy` and `bootstrap`).

The product's root module runs with the config repo's rendered inputs:
`init -backend-config=<rendered state.tfbackend>`, `plan -var-file=<rendered tfvars>`, state data in
`<config>/.mcctl/terraform` (TF_DATA_DIR). Credentials come from the shell plus the config repo's `.env`.

`apply` always applies a plan file that was just shown, and only after the deployment's name is typed: there
is no `--yes`. The plan file (it can hold sensitive values) is mode 0600 under the git-ignored `.mcctl/` and
is deleted afterwards.

Output (mcctl.ui): in a plain run `plan`/`apply` stream Terraform's own output. In a rich run it goes to a
log file (`<config>/.mcctl/logs/terraform-<command>-<time>.log`, mode 0600) while a spinner shows the line
Terraform just printed; a failure shows only Terraform's `Error:` blocks and the log's path. The plan is
summarised as a table (action, resource) before the typed confirmation.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcctl import envfile, ui

# The Terraform CLI the product is tested with (= ci.yml setup-terraform, ~> constraint in versions.tf).
TERRAFORM_VERSION = "1.17.1"
PLAN_FILE = "tfplan"
LOG_DIR = "logs"  # <config>/.mcctl/logs (next to TF_DATA_DIR)
TAILSCALE_OAUTH_VARS = ("TAILSCALE_OAUTH_CLIENT_ID", "TAILSCALE_OAUTH_CLIENT_SECRET")


class TfError(RuntimeError):
    """A Terraform step failed; the message says how to fix it."""


def exe(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise TfError(f"`{name}` not found on PATH")
    return path


def data_dir(config_root: Path) -> Path:
    return config_root / ".mcctl" / "terraform"


def environment(config_root: Path, shell: Mapping[str, str] | None = None) -> dict[str, str]:
    """Shell + .env credentials, this config repo's TF_DATA_DIR. A one-off TAILSCALE_API_KEY (the first ACL
    apply, README) replaces the OAuth client: the provider refuses both at once."""
    try:
        env = envfile.merged(config_root, shell)
    except envfile.EnvFileError as e:
        raise TfError(str(e)) from None
    if env.get("TAILSCALE_API_KEY"):
        for var in TAILSCALE_OAUTH_VARS:
            env.pop(var, None)
    return env | {"TF_DATA_DIR": str(data_dir(config_root))}


@dataclass
class Plan:
    """What a saved plan would do (from `terraform show -json`; addresses and actions only, no values)."""

    create: list[str] = field(default_factory=list)
    update: list[str] = field(default_factory=list)
    replace: list[str] = field(default_factory=list)
    delete: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.create or self.update or self.replace or self.delete)

    def summary(self) -> str:
        parts = [
            f"{len(self.create)} to add",
            f"{len(self.update)} to change",
            f"{len(self.replace)} to replace",
            f"{len(self.delete)} to destroy",
        ]
        lines = [", ".join(parts)]
        for label, items in (("replace", self.replace), ("DESTROY", self.delete)):
            lines += [f"  {label}: {a}" for a in items]
        return "\n".join(lines)

    def rows(self) -> list[list[str]]:
        """Action / resource rows for the plan table (destroys first: they matter most)."""
        out = [["fail: destroy", a] for a in self.delete]
        out += [["warn: replace", a] for a in self.replace]
        out += [["info: change", a] for a in self.update]
        out += [["ok: add", a] for a in self.create]
        return out


def show_plan(plan: Plan, verb: str = "will") -> None:
    """The plan before a confirmation: plain, the classic one-line summary; rich, a table and the totals."""
    out = ui.stdout()
    if not out.rich:
        out.say(f"\nTerraform {verb}: {plan.summary()}")
        return
    out.say()
    out.table(["Action", "Resource"], plan.rows(), title=f"Terraform {verb}", indent=2)
    out.say(f"  {plan.summary().splitlines()[0]}")


ERROR_START = re.compile(r"^(?:[╷│╵]\s*)?Error: ")


def error_blocks(text: str, limit: int = 3, lines_each: int = 12) -> list[str]:
    """Terraform's `Error:` blocks (the actionable part of a failure), without the box drawing."""
    blocks: list[list[str]] = []
    current: list[str] | None = None
    for raw in text.splitlines():
        line = re.sub(r"^[╷│╵]\s?", "", raw.rstrip())
        if ERROR_START.match(raw.strip()) or line.startswith("Error: "):
            current = [line]
            blocks.append(current)
        elif current is not None:
            if raw.strip() in ("╵", "") and len(current) > 1 and current[-1] == "":
                current = None
            elif len(current) < lines_each:
                current.append(line)
    return ["\n".join(b).strip() for b in blocks[:limit]]


def log_path(env: Mapping[str, str], command: str, now: datetime.datetime | None = None) -> Path:
    """A new log file for one Terraform command, mode 0600, under <config>/.mcctl/logs."""
    base = Path(env.get("TF_DATA_DIR") or ".mcctl/terraform").parent / LOG_DIR
    stamp = (now or datetime.datetime.now()).strftime("%Y%m%d-%H%M%S")
    path, n = base / f"terraform-{command}-{stamp}.log", 1
    while path.exists():
        n += 1
        path = base / f"terraform-{command}-{stamp}-{n}.log"
    private_file(path)
    return path


def summarize(show: Mapping[str, Any]) -> Plan:
    plan = Plan()
    for rc in show.get("resource_changes") or []:
        actions = list((rc.get("change") or {}).get("actions") or [])
        addr = str(rc.get("address"))
        if sorted(actions) == ["create", "delete"]:
            plan.replace.append(addr)
        elif actions == ["create"]:
            plan.create.append(addr)
        elif actions == ["update"]:
            plan.update.append(addr)
        elif actions == ["delete"]:
            plan.delete.append(addr)
    return plan


@dataclass
class Terraform:
    tf_dir: Path
    env: dict[str, str]

    def _argv(self, *args: str) -> list[str]:
        return [exe("terraform"), f"-chdir={self.tf_dir}", *args]

    def _run(self, *args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
        # umask 077: whatever Terraform creates (plan files, local state copies, lock info) is owner-only.
        return subprocess.run(
            self._argv(*args), capture_output=capture, text=True, env=self.env, check=False, umask=0o077
        )

    def _run_shown(self, command: str, *args: str) -> tuple[int, str, Path | None]:
        """A long command (plan, apply): streamed (plain), or logged behind a spinner (rich). Returns the
        exit code, the captured output (rich) and the log file (rich)."""
        out = ui.stdout()
        if not out.rich:
            return self._run(command, *args, capture=False).returncode, "", None
        log = log_path(self.env, command)
        lines: list[str] = []
        with out.spinner(f"terraform {command}") as sp, log.open("w") as fh:
            proc = subprocess.Popen(
                self._argv(command, "-no-color", *args),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                text=True,
                env=self.env,
                umask=0o077,
            )
            assert proc.stdout is not None
            for line in proc.stdout:
                fh.write(line)
                lines.append(line)
                if line.strip():
                    sp.update(f"terraform {command}: {line.strip()}")
            rc = proc.wait()
        return rc, "".join(lines), log

    def _failed(self, what: str, text: str, log: Path | None, fix: str) -> TfError:
        if log is None:
            return TfError(f"`terraform {what}` failed (see above). {fix}".strip())
        errors = error_blocks(text) or [line for line in text.strip().splitlines()[-8:]]
        return TfError(
            f"`terraform {what}` failed:\n" + "\n".join(errors) + f"\nFull output: {log}. {fix}".rstrip()
        )

    def version(self) -> str | None:
        proc = self._run("version", "-json")
        try:
            return str(json.loads(proc.stdout)["terraform_version"])
        except (ValueError, KeyError, TypeError):
            return None

    def init(self, backend_file: Path) -> None:
        proc = self._run("init", "-input=false", "-lockfile=readonly", f"-backend-config={backend_file}")
        if proc.returncode != 0:
            raise TfError(
                "`terraform init` failed. Check the state credentials in the config repo's .env (AWS_*).\n"
                + (proc.stderr.strip() or proc.stdout.strip())
            )

    def outputs(self) -> dict[str, Any]:
        """Output name -> value (sensitive values included: never log them)."""
        proc = self._run("output", "-json")
        if proc.returncode != 0:
            raise TfError(f"`terraform output -json` failed.\n{proc.stderr.strip()}")
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            raise TfError("`terraform output -json` didn't return JSON") from None
        if not isinstance(data, dict):
            raise TfError("`terraform output -json` didn't return a JSON object")
        return {k: v.get("value") if isinstance(v, dict) else None for k, v in data.items()}

    def plan(
        self, var_file: Path, out: Path, *, targets: Iterable[str] = (), replaces: Iterable[str] = ()
    ) -> Plan:
        """Run and show `terraform plan -out` (its own output, sensitive values redacted by Terraform)."""
        private_file(out)
        args = ["plan", "-input=false", f"-var-file={var_file}", f"-out={out}"]
        args += [f"-target={t}" for t in targets] + [f"-replace={r}" for r in replaces]
        rc, text, log = self._run_shown(*args)
        if rc != 0:
            out.unlink(missing_ok=True)
            raise self._failed("plan", text, log, "")
        if log is not None:
            ui.stdout().dim(f"terraform plan output: {log}")
        if out.exists():
            out.chmod(0o600)
        show = self._run("show", "-json", str(out))
        try:
            return summarize(json.loads(show.stdout))
        except (ValueError, AttributeError):
            raise TfError("`terraform show -json` of the plan failed") from None

    def drift(self, var_file: Path, out: Path) -> Plan:
        """`mcctl doctor`'s read-only plan: `plan -detailed-exitcode -lock=false`, output captured (never
        shown: it would only repeat what `mcctl plan` prints), the saved plan summarised and deleted."""
        private_file(out)
        try:
            proc = self._run(
                "plan",
                "-input=false",
                "-lock=false",
                "-detailed-exitcode",
                f"-var-file={var_file}",
                f"-out={out}",
            )
            if proc.returncode == 0:
                return Plan()
            if proc.returncode != 2:
                errors = error_blocks(proc.stderr + proc.stdout, limit=1, lines_each=3)
                raise TfError(errors[0] if errors else f"`terraform plan` exited {proc.returncode}")
            show = self._run("show", "-json", str(out))
            try:
                return summarize(json.loads(show.stdout))
            except (ValueError, AttributeError):
                raise TfError("`terraform show -json` of the plan failed") from None
        finally:
            out.unlink(missing_ok=True)

    def state_list(self) -> list[str] | None:
        """Addresses in the state (`terraform state list`), or None if it can't be read."""
        proc = self._run("state", "list")
        return proc.stdout.split() if proc.returncode == 0 else None

    def apply(self, plan_file: Path) -> None:
        try:
            rc, text, log = self._run_shown("apply", "-input=false", str(plan_file))
        finally:
            plan_file.unlink(missing_ok=True)
        if rc != 0:
            raise self._failed("apply", text, log, "Fix it and run the command again.")
        if log is not None:
            ui.stdout().dim(f"terraform apply output: {log}")


def apply_outcome(plan: Plan, in_state: Iterable[str] | None) -> list[list[str]] | None:
    """After a failed apply, per planned resource: created / NOT created / maybe changed (None: no state)."""
    if in_state is None:
        return None
    have = set(in_state)
    rows = [["ok: created" if a in have else "fail: NOT created", a] for a in plan.create + plan.replace]
    return rows + [["warn: maybe changed", a] for a in plan.update]


def failed_apply_report(plan: Plan, in_state: Iterable[str] | None) -> list[str]:
    """After a failed apply: which of the plan's new resources exist now and which don't (failed, or never
    reached because something they depend on failed). Addresses only."""
    if in_state is None:
        return ["couldn't read the state to tell what was created (`terraform state list`)"]
    have = set(in_state)
    made = [a for a in plan.create + plan.replace if a in have]
    missing = [a for a in plan.create + plan.replace if a not in have]
    lines = [f"created ({len(made)}): " + (", ".join(made) or "nothing")]
    lines += [f"NOT created ({len(missing)}): " + (", ".join(missing) or "nothing")]
    if plan.update:
        lines.append("planned changes (may or may not have been applied): " + ", ".join(plan.update))
    lines.append(
        "Terraform keeps what it created in the state (R2): re-running plans only what is still missing or "
        "different, so fixing the cause and re-running is safe (no manual cleanup)."
    )
    return lines


def private_file(path: Path) -> None:
    """Create (or truncate) `path` as mode 0600 in a 0700 directory BEFORE anything secret is written to it:
    a plan file can hold sensitive values, and chmod after the write would leave a window."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    path.chmod(0o600)


def confirm(plan: Plan, name: str, ask: Callable[[str], str] | None = None) -> bool:
    """Typed confirmation: the deployment's name. EOF (no terminal) means no."""
    ask = ask or input
    out = ui.stdout()
    show_plan(plan)
    if plan.delete:
        if out.rich:
            out.warn("this plan DESTROYS resources (listed above)")
        else:
            out.say("WARNING: this plan DESTROYS resources (listed above).")
    try:
        answer = ask(f"Type the deployment name ({name}) to apply, anything else aborts: ")
    except EOFError:
        out.say("\nno terminal to confirm on: not applied")
        return False
    return answer.strip() == name


# Security review M2: moving the Actions secrets from repository level into GitHub Environments. Terraform
# destroys orphaned resource instances before it creates anything (their order comes from the state), so a
# single apply would leave a moment with neither secret. First the environments and their secrets (a targeted
# apply), then the rest (the repository secrets go). terraform/github.tf documents the same two steps.
ENVIRONMENT_SECRETS = "github_actions_environment_secret.this"
# a deploy secret's resource address while it is repository-level (an address, not a value)
_REPOSITORY_SECRET = 'github_actions_secret.this["RCON_PASSWORD"]'  # noqa: S105


def environments_migration(environments: bool, in_state: Iterable[str] | None) -> bool:
    """True when the next apply moves the deploy secrets from repository level into environments."""
    listed = set(in_state or ())
    return (
        environments
        and _REPOSITORY_SECRET in listed
        and not any(a.startswith(ENVIRONMENT_SECRETS + "[") for a in listed)
    )


def apply_flow(
    tf: Terraform,
    config_root: Path,
    var_file: Path,
    name: str,
    *,
    targets: Iterable[str] = (),
    replaces: Iterable[str] = (),
    ask: Callable[[str], str] | None = None,
) -> bool:
    """plan -> summary -> typed confirmation -> apply that plan. Returns True if something was applied."""
    plan_file = data_dir(config_root) / PLAN_FILE
    plan = tf.plan(var_file, plan_file, targets=targets, replaces=replaces)
    if plan.empty:
        plan_file.unlink(missing_ok=True)
        ui.stdout().say("No changes: nothing to apply.")
        return False
    if not confirm(plan, name, ask):
        plan_file.unlink(missing_ok=True)
        ui.stdout().say("Aborted: nothing was applied.")
        return False
    tf.apply(plan_file)
    return True
