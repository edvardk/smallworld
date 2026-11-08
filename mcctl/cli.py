"""mcctl command line (design §4). Each `cmd_*` here parses arguments and calls the module that does the work:

  (none: the dashboard)             home, dashboard, homecfg (the remembered config repo)
  validate, render, schema          configrepo, render, schema
  deploy, plan, apply, bootstrap    deploy, tf, bootstrap (re-executed from the pin: product)
  doctor, metrics                   doctor (+ pinmove, wfsecrets), metrics
  status, rcon, ssh, logs, backup,  remote (over the tailnet)
  stop, start
  restart, console, worlds,         actions (v1.3: the panel's actions over the tailnet, audited)
  plugins, players kick|ban|pardon
  init, setup                       init (mock for --mock)
  product-setup, config             productsetup, configedit
  migrate, retire-old               migrate
  upgrade, lock, schema-migrate     upgrade (+ migrations)
  secrets, discord                  rotate, integrations (+ dashlane)
  players, restore                  players, restorecmd
  publish                           publish (v1.3: preview, validate, PR / main / deploy)
  CI: update-prs, expiry-check,     updates, expirycheck, panelpr, gate, clientpublish
  panel-pr-check, pr-gate, client-publish

docs/map.md has the tests and docs per command. `-C DIR` names the config repo (default: the current
directory if it is one, else the remembered one: mcctl/homecfg.py). Commands that run the product's
Terraform or Ansible (plan, apply, deploy, bootstrap) re-run themselves from the pinned product commit
(mcctl/product.py).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from mcctl import (
    __version__,
    actions,
    configrepo,
    doctor,
    http,
    panelpr,
    product,
    python_problem,
    remote,
    schema,
    secrets,
    state,
    tf,
    ui,
)
from mcctl import bootstrap as bootstrap_mod
from mcctl import deploy as deploy_mod
from mcctl import render as render_mod

if TYPE_CHECKING:
    from mcctl import init

SCHEMA_FILE = Path("schema") / "server.schema.json"  # relative to the working directory (product checkout)
# `-C` not given (identity-compared): the current directory, or the remembered config repo (mcctl/homecfg.py).
DEFAULT_DIR = Path(".")
RESTART_ANSWERS = ("now", "wait", "cancel")  # = publish.RESTART_CHOICES (`publish --restart`)


def _repo(args: argparse.Namespace) -> configrepo.ConfigRepo:
    try:
        return configrepo.load(args.config_dir, i_know=args.i_know)
    except configrepo.ConfigError as e:
        print(f"{args.config_dir}: {len(e.errors)} error(s)", file=sys.stderr)
        for err in e.errors:
            print(f"  {err}", file=sys.stderr)
        raise SystemExit(1) from None


def ci_outputs(repo: configrepo.ConfigRepo) -> dict[str, str]:
    """Non-secret values the reusable workflow needs from server.yml (validate job outputs)."""
    cfg = repo.server
    return {
        "deploy_enabled": "true" if cfg.deploy.enabled else "false",
        "tailscale_name": cfg.host.tailscale_name,
        "server_address": f"{cfg.dns.hostname}.{cfg.dns.zone}",
        # Security review M2: the jobs' GitHub Environments ("" = none: repository-level secrets).
        "deploy_environment": secrets.DEPLOY_ENVIRONMENT if cfg.github.environments else "",
        "update_environment": secrets.UPDATE_ENVIRONMENT if cfg.github.environments else "",
        "product_repository": repo.pin.repository,
        "product_version": repo.pin.version or "",
    }


def repository_mismatch(cfg: schema.ServerConfig, expected: str) -> str | None:
    """CI: server.yml github.owner/config_repo must name the repository the workflow runs in (GitHub names
    are case-insensitive). Terraform writes this deployment's secrets and CI identity there."""
    actual = f"{cfg.github.owner}/{cfg.github.config_repo}"
    if actual.lower() == expected.lower():
        return None
    return (
        f"server.yml github.owner/config_repo is {actual!r} but this workflow runs in {expected!r}: "
        "set github.owner and github.config_repo to this repository"
    )


def cmd_validate(args: argparse.Namespace) -> int:
    repo = _repo(args)
    if args.expect_repository and (problem := repository_mismatch(repo.server, args.expect_repository)):
        print(f"{args.config_dir}: {problem}", file=sys.stderr)
        return 1
    render_mod.files(repo)  # every rendered file can be produced (placeholders are only warned about here)
    for warning in schema.placeholders(repo.server):
        print(f"{args.config_dir}: warning: {warning} before `render`/`apply`", file=sys.stderr)
    if args.github_output is not None:
        with args.github_output.open("a") as fh:
            for key, value in ci_outputs(repo).items():
                if "\n" in value or "\r" in value:  # pattern-checked already; never split an output
                    raise SystemExit(f"mcctl: {key} contains a newline")
                fh.write(f"{key}={value}\n")
    print(f"{args.config_dir}: valid (product {repo.pin.repository}@{repo.pin.sha[:12]})")
    return 0


def cmd_panel_pr_check(args: argparse.Namespace) -> int:
    reasons = panelpr.check(args.config_dir, args.base, args.head)
    if reasons:
        print(
            f"{args.config_dir}: not a whitelist or world-library panel change, left for a human:",
            file=sys.stderr,
        )
        for r in reasons:
            print(f"  {r}", file=sys.stderr)
        return 1
    print(f"{args.config_dir}: panel change ({args.base[:12]}..{args.head[:12]}): ok to auto-merge")
    return 0


def cmd_pr_gate(args: argparse.Namespace) -> int:
    from mcctl import gate

    verdict = gate.evaluate(
        args.config_dir,
        args.base,
        args.head,
        product_token=os.environ.get("PRODUCT_TOKEN", ""),
        head_ref=args.head_ref,
        author=args.author,
        sender=args.sender,
    )
    if not verdict.ok:
        print(f"{args.config_dir}: not merged unattended ({verdict.policy}):", file=sys.stderr)
        for r in verdict.reasons:
            print(f"  {r}", file=sys.stderr)
        return 1
    span = f"{args.base[:12]}..{args.head[:12]}"
    print(f"{args.config_dir}: {verdict.policy} change ({span}): may merge unattended")
    if args.github_output:
        with args.github_output.open("a") as fh:
            fh.write(f"policy={verdict.policy}\n")
    return 0


def cmd_update_prs(args: argparse.Namespace) -> int:
    """P11: the weekly update task's pull requests (the updater App's token from the step env only)."""
    from mcctl import updates
    from mcctl.providers import ProviderError

    try:
        lines = updates.main_from_env(args.config_dir, os.environ)
    except (ProviderError, http.HttpError) as e:
        print(f"mcctl: update pull requests: {e}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def cmd_expiry_check(args: argparse.Namespace) -> int:
    """P11: expiry.yml + RDAP + the watchdog's /status, no admin token; problems -> Discord + exit 1."""
    import datetime

    from mcctl import expirycheck

    repo = configrepo.load(args.config_dir)
    today = datetime.datetime.now(datetime.UTC).date()
    findings = expirycheck.check(repo, today, http.urllib_transport)
    for note in findings.notes:
        print(f"ok    {note}")
    for problem in findings.problems:
        print(f"PROBLEM {problem}")
    expirycheck.report(
        repo,
        findings,
        os.environ.get("DISCORD_WEBHOOK_URL", ""),
        expirycheck.discord_post(http.urllib_transport),
        expirycheck.run_url(os.environ),
    )
    return 1 if findings.problems else 0


def cmd_render(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out: Path = args.out if args.out is not None else repo.root / ".mcctl" / "rendered"
    try:
        written = render_mod.render(repo, out)
    except render_mod.RenderError as e:
        print(f"{args.config_dir}: refusing to render deployable output:", file=sys.stderr)
        for err in e.errors:
            print(f"  {err}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


def cmd_client_publish(args: argparse.Namespace) -> int:
    from mcctl import clientpublish

    return clientpublish.run(_repo(args), dict(os.environ))


def cmd_schema(args: argparse.Namespace) -> int:
    out: Path = args.output
    text = json.dumps(schema.json_schema(), indent=2) + "\n"
    if args.check:
        current = out.read_text() if out.is_file() else ""
        if current != text:
            print(f"{out} is stale: run `mcctl schema`", file=sys.stderr)
            return 1
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text)
    print(out)
    return 0


def _pinned(args: argparse.Namespace, repo: configrepo.ConfigRepo | configrepo.Pin) -> Path | int:
    """The pinned product's root if this process runs it; otherwise re-run the command there (exit code)."""
    pin = repo if isinstance(repo, configrepo.Pin) else repo.pin
    try:
        with ui.stdout().spinner(f"preparing the pinned product {pin.sha[:12]} (first use: fetch + venv)"):
            prod = product.resolve(pin, os.environ)
    except product.ProductError as e:
        print(f"mcctl: {e}", file=sys.stderr)
        return 1
    argv = product.reexec_argv(prod, args.argv)
    if argv is None:
        return prod.root
    if override := os.environ.get(product.ENV_OVERRIDE):
        # Either a re-executed child that still imported other code (it would loop), or a developer
        # override naming another checkout than the one running: never re-execute from here.
        print(
            f"mcctl: {product.ENV_OVERRIDE}={override} but this mcctl runs from {schema.PRODUCT_ROOT}: "
            "refusing to re-execute again. Run mcctl from that checkout (`cd <it> && python3 -m mcctl ...`) "
            f"or unset {product.ENV_OVERRIDE}.",
            file=sys.stderr,
        )
        return 1
    return _reexec(argv, prod.env(os.environ))


def _reexec(argv: list[str], env: dict[str, str]) -> int:
    return subprocess.run(argv, env=env, check=False).returncode


def _error(name: str, e: BaseException, fix: str = "") -> None:
    """A command's failure: plain `mcctl NAME: cause` (+ `  fix: ...`) on stderr; rich, a Fix box."""
    err = ui.stderr()
    if err.rich:
        err.fail(f"mcctl {name}: {e}", fix)
        return
    print(f"mcctl {name}: {e}", file=sys.stderr)
    if fix:
        print(f"  fix: {fix}", file=sys.stderr)


def _guard(name: str, fn: object) -> int:
    try:
        return int(fn())  # type: ignore[operator]
    except (deploy_mod.DeployError, tf.TfError, state.StateError) as e:
        _error(name, e)
        return 1


def cmd_deploy(args: argparse.Namespace) -> int:
    repo = _repo(args)
    root = _pinned(args, repo)
    if isinstance(root, int):
        return root
    force = getattr(args, "force_restart", False)
    return _guard("deploy", lambda: deploy_mod.run(repo, root, check=args.check, force_restart=force))


def _plan_or_apply(args: argparse.Namespace, apply: bool) -> int:
    repo = _repo(args)
    root = _pinned(args, repo)
    if isinstance(root, int):
        return root

    def go() -> int:
        terraform, out = deploy_mod.terraform_for(repo, root, purpose="apply" if apply else "plan with")
        var_file = out / render_mod.TFVARS_FILE
        if not apply:
            plan_file = tf.data_dir(repo.root) / "tfplan.preview"
            try:
                plan = terraform.plan(var_file, plan_file, targets=args.target, replaces=args.replace)
            finally:
                plan_file.unlink(missing_ok=True)
            tf.show_plan(plan, "would")
            return 0
        st = state.load(repo.root)
        if not (args.target or args.replace) and tf.environments_migration(
            repo.server.github.environments, terraform.state_list()
        ):
            ui.stdout().say(
                "github.environments: first the environments and their secrets (the repository-level "
                "secrets stay until the next step), then the rest."
            )
            if not tf.apply_flow(
                terraform, repo.root, var_file, repo.server.name, targets=[tf.ENVIRONMENT_SECRETS]
            ):
                return 1
        applied = tf.apply_flow(
            terraform, repo.root, var_file, repo.server.name, targets=args.target, replaces=args.replace
        )
        if applied:
            st.record("apply", "done", inputs={"targets": args.target, "replaces": args.replace})
        return 0 if applied else 1

    return _guard("apply" if apply else "plan", go)


def cmd_plan(args: argparse.Namespace) -> int:
    return _plan_or_apply(args, apply=False)


def cmd_apply(args: argparse.Namespace) -> int:
    return _plan_or_apply(args, apply=True)


def cmd_bootstrap(args: argparse.Namespace) -> int:
    repo = _repo(args)
    root = _pinned(args, repo)
    if isinstance(root, int):
        return root
    return _guard("bootstrap", lambda: bootstrap_mod.run(repo, root, force=args.force))


def cmd_doctor(args: argparse.Namespace) -> int:
    out = ui.stdout()
    with out.spinner("checking tools, credentials (live), DNS, the tailnet and the watchdog"):
        if args.cloudflare_permissions:
            report = doctor.cloudflare_permissions(args.config_dir)
        else:
            report = doctor.run(args.config_dir, fix=args.fix, i_know=args.i_know)
    doctor.show(report, out)
    if (
        args.fix
        and not args.cloudflare_permissions
        and not os.environ.get(product.ENV_OVERRIDE)  # a dev override isn't a pin to move
        and any(r.name == "product pin" and r.level == "warn" for r in report.results)
    ):
        from mcctl import pinmove

        try:  # P15: the stale-pin offer (typed yes), outside the spinner
            pinmove.offer(args.config_dir.resolve(), ui.UI(), doctor.Deps().run)
        except (pinmove.PinMoveError, ui.Abort, EOFError) as e:
            print(f"mcctl doctor: {e}", file=sys.stderr)
            return 1
    return 1 if report.failed else 0


def cmd_metrics(args: argparse.Namespace, transport: http.Transport | None = None) -> int:
    """P13: player-experience summary / CSV export from the Worker's TELEMETRY D1 database (no SSH)."""
    import time

    from mcctl import envfile, metrics
    from mcctl.providers.cloudflare import CloudflareError

    repo = _repo(args)
    env = envfile.merged(args.config_dir)
    token = env.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        print("CLOUDFLARE_API_TOKEN isn't set (the config repo's .env)", file=sys.stderr)
        return 1
    try:
        db = metrics.connect(
            token, repo.server.dns.zone, repo.server.name, transport or http.urllib_transport
        )
        if args.action == "export":
            if not args.since:
                raise ValueError("export needs --since YYYY-MM-DD")
            since = metrics.parse_day(args.since)
            until = metrics.parse_day(args.until) if args.until else int(time.time() * 1000)
            text = metrics.export_csv(db, args.table, since, until)
            if args.output:
                Path(args.output).write_text(text)
                print(f"wrote {args.output} ({text.count(chr(10)) - 1} rows)", file=sys.stderr)
            else:
                sys.stdout.write(text)
        else:
            out = ui.stdout()
            with out.spinner("reading the hourly rollups from D1"):
                data = metrics.summarize(db, int(time.time() * 1000), args.days)
            metrics.show_summary(data, out)
    except (CloudflareError, http.HttpError, ValueError) as e:
        _error("metrics", e)
        return 1
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    repo = _repo(args)
    out = ui.stdout()
    with out.spinner(f"asking {repo.server.host.tailscale_name} over the tailnet, and GitHub"):
        st = remote.status(repo.server)
    if args.json:
        print(json.dumps(st.as_dict(), indent=2, sort_keys=True))
    elif out.rich:
        out.kv(remote.status_rows(st), title=repo.server.name)
    else:
        print(remote.format_status(st, repo.server.name))
    return 0 if st.reachable else 1


def _ssh(args: argparse.Namespace, command: str | None, *, batch: bool = True) -> int:
    repo = _repo(args)
    return subprocess.run(remote.ssh_argv(repo.server, command, batch=batch), check=False).returncode


def cmd_rcon(args: argparse.Namespace) -> int:
    try:
        command = remote.rcon_command(args.command)
    except ValueError as e:
        print(f"mcctl rcon: {e}", file=sys.stderr)
        return 2
    return _ssh(args, command)


def cmd_ssh(args: argparse.Namespace) -> int:
    return _ssh(args, None, batch=False)


def cmd_logs(args: argparse.Namespace) -> int:
    try:
        command = remote.logs_command(args.unit, args.lines, args.follow)
    except ValueError as e:
        print(f"mcctl logs: {e}", file=sys.stderr)
        return 2
    return _ssh(args, command)


def cmd_backup(args: argparse.Namespace) -> int:
    if args.action == "now":
        proc = remote.capture(remote.ssh_argv(_repo(args).server, remote.backup_now_command()))
        ok, detail = remote.backup_outcome(proc)
        journal = remote.sections(proc.stdout or "").get("journal", "")
        print(journal + ("\n" if journal else "") + detail, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 1
    return _ssh(args, remote.backup_list_command(args.latest))


def cmd_stop(args: argparse.Namespace) -> int:
    return _ssh(args, remote.admin_command("stop", args.reason))


def cmd_start(args: argparse.Namespace) -> int:
    return _ssh(args, remote.admin_command("start"))


def _remote(args: argparse.Namespace) -> actions.Remote:
    return actions.Remote(_repo(args).server)


def cmd_restart(args: argparse.Namespace) -> int:
    rem = _remote(args)
    return actions.guarded(rem, lambda: actions.restart(rem, args.countdown, args.backup, args.wait))


def cmd_console(args: argparse.Namespace) -> int:
    rem = _remote(args)
    return actions.guarded(rem, lambda: actions.console(rem, args.command))


def cmd_worlds(args: argparse.Namespace) -> int:
    rem = _remote(args)
    if args.action == "list":
        return actions.guarded(rem, lambda: actions.worlds_list(rem))
    if args.action == "create":
        return actions.guarded(
            rem,
            lambda: actions.world_create(
                rem, args.name, seed=args.seed, wtype=args.type, preset=args.preset, pregen=args.pregen,
                countdown=args.countdown, confirm=args.confirm,
            ),
        )  # fmt: skip
    if args.action == "switch":
        return actions.guarded(
            rem, lambda: actions.world_switch(rem, args.name, countdown=args.countdown, confirm=args.confirm)
        )
    return actions.guarded(rem, lambda: actions.world_delete(rem, args.name, confirm=args.confirm))


def cmd_plugins(args: argparse.Namespace) -> int:
    rem = _remote(args)
    if args.action == "list":
        return actions.guarded(rem, lambda: actions.plugins_list(rem))
    return actions.guarded(
        rem,
        lambda: actions.plugin_toggle(
            rem, args.name, args.action == "enable", countdown=args.countdown, confirm=args.confirm
        ),
    )


def cmd_init(args: argparse.Namespace) -> int:
    from mcctl import init, mock, pinmove, ui, yamlio

    answers = None
    if args.answers is not None:
        try:
            answers = yamlio.load(args.answers.read_text())
        except (OSError, yamlio.yaml.YAMLError) as e:
            print(f"mcctl init: --answers: {e}", file=sys.stderr)
            return 2
        if not isinstance(answers, dict):
            print("mcctl init: --answers must be a YAML mapping", file=sys.stderr)
            return 2
    if args.non_interactive and answers is None:
        print("mcctl init: --non-interactive needs --answers FILE", file=sys.stderr)
        return 2
    if answers is not None and not args.non_interactive:
        print(
            "mcctl init: --answers needs --non-interactive (answers are never mixed with prompts)",
            file=sys.stderr,
        )
        return 2
    if answers is not None and not args.mock and (banned := sorted(set(answers) & init.HUMAN_ONLY_ANSWERS)):
        print(
            f"mcctl init: --answers can't pre-approve {', '.join(banned)} outside --mock: an apply, a host "
            "key or a revoked token must be confirmed by a person (run without --non-interactive)",
            file=sys.stderr,
        )
        return 2
    talk = ui.UI(answers=answers, mock=args.mock, env={} if args.mock else None)
    root = args.config_dir
    if root is None:
        try:
            root = Path(
                talk.ask(
                    "dir", "Local folder for the config repo (created if missing)", default="~/mc-config"
                )
            )
        except ui.Abort as e:
            print(f"mcctl init: {e}", file=sys.stderr)
            return 1
    root = root.expanduser()
    workflow = root / configrepo.WORKFLOW
    deps = mock.deps(root) if args.mock else init.Deps()
    if workflow.is_file() and not args.product_sha and not os.environ.get(product.ENV_OVERRIDE):
        # P15: a pin behind this checkout is offered to move forward first (typed yes), so the re-exec
        # below runs the code the user just pulled instead of the old pinned one. Not with --product-sha or
        # MCCTL_PRODUCT_DIR: then the running code isn't the clean checkout the pin would move to.
        try:
            pinmove.offer(root, talk, deps.run, require_published=not args.mock)
        except (pinmove.PinMoveError, ui.Abort, EOFError) as e:
            print(f"mcctl init: {e}", file=sys.stderr)
            return 1
    if workflow.is_file() and not args.mock:  # an existing config repo: run its pinned product
        pin, _errors = configrepo.read_pin(workflow.read_text())
        if pin is not None:
            args.argv = [*args.argv, "-C", str(root)] if args.config_dir is None else args.argv
            prod_root = _pinned(args, pin)
            if isinstance(prod_root, int):
                return prod_root
    seed = args.seed.expanduser().resolve() if args.seed else None
    if seed is not None and not seed.is_file():
        print(f"mcctl init: --seed {seed}: no such file", file=sys.stderr)
        return 2
    opts = init.Options(
        mock=args.mock,
        product_sha=args.product_sha,
        from_step=args.from_step,
        seed=seed,
        reconfigure=tuple(args.reconfigure),
    )
    try:
        return init.run(root, talk, deps, opts)
    except KeyboardInterrupt:
        print(f"\nStopped. Re-run `mcctl init -C {root}` to continue where you left off.", file=sys.stderr)
        return 130


def cmd_product_setup(args: argparse.Namespace) -> int:
    from mcctl import productsetup, ui

    talk = ui.UI()

    def ask() -> str:
        return talk.secret(
            "product_admin_token", "GitHub token with admin on the product repo", env_name="GH_TOKEN"
        )

    try:
        token = productsetup.token(os.environ, subprocess_run, ask)
        if not token:
            print("mcctl product-setup: no GitHub token (gh auth login, or GH_TOKEN)", file=sys.stderr)
            return 1
        out = ui.stdout()
        with out.spinner(f"reading {args.repo}'s settings"):
            p = productsetup.plan(token, args.repo, http.urllib_transport)
        if out.rich:
            out.table(["", "Setting", "Now", "Wanted"], productsetup.plan_rows(p), title=args.repo, indent=2)
        else:
            for line in productsetup.format_plan(p):
                print(line)
        if not p.changes:
            print(f"{args.repo}: in sync")
            return 0
        if args.check:
            print(f"{args.repo}: {len(p.changes)} setting(s) differ (run without --check to apply)")
            return 1
        if not talk.confirm(
            "product_setup_apply", f"Apply these {len(p.changes)} change(s) to {args.repo}?", default=True
        ):
            return 1
        for name in productsetup.apply(p):
            if out.rich:
                out.ok(f"{name} changed")
            else:
                print(f"[ok] {name} changed")
    except (productsetup.SetupError, http.HttpError, ui.Abort) as e:
        _error("product-setup", e, getattr(e, "fix", ""))
        return 1
    return 0


def subprocess_run(argv: list[str] | tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """A captured run for tools whose output is a credential (`gh auth token`): never echoed."""
    try:
        return subprocess.run(list(argv), capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def cmd_config(args: argparse.Namespace) -> int:
    from mcctl import configedit

    try:
        if args.action == "get":
            print(configedit.get(args.config_dir, args.path, i_know=args.i_know))
            return 0
        writer = None
        if args.path == "client.enabled":  # v1.2: may write the first client.lock.yml (the pinned product's)
            root = _pinned_root(args)
            if isinstance(root, int):
                return root
            writer = configedit.client_lock_writer(root)
        msg = configedit.set_and_commit(
            args.config_dir,
            args.path,
            args.value,
            commit=not args.no_commit,
            i_know=args.i_know,
            lock_writer=writer,
        )
    except configedit.ConfigEditError as e:
        print(f"mcctl config: {e}", file=sys.stderr)
        return 1
    print(msg)
    return 0


# --- P12: migrate, retire-old, upgrade, lock update, secrets rotate, players, restore -----------------------


def _ctx(args: argparse.Namespace, product_root: Path | None = None) -> init.Ctx:
    from mcctl import init, ui

    root = Path(args.config_dir).expanduser().resolve()
    return init.Ctx(
        root=root,
        ui=ui.UI(),
        deps=init.Deps(),
        opts=init.Options(),
        product_root=product_root or schema.PRODUCT_ROOT,
    )


def _p12(name: str, fn: object) -> int:
    from mcctl import envfile, init, ui
    from mcctl.providers import ProviderError

    try:
        return int(fn())  # type: ignore[operator]
    except (
        envfile.EnvFileError,  # (P14: a Dashlane reference that doesn't resolve is one)
        init.StepError,
        ProviderError,
        ui.Abort,
        deploy_mod.DeployError,
        tf.TfError,
        state.StateError,
        http.HttpError,
    ) as e:
        _error(name, e, getattr(e, "fix", ""))
        return 1
    except KeyboardInterrupt:
        print(f"\nmcctl {name}: stopped; re-run to continue", file=sys.stderr)
        return 130


def _pinned_root(args: argparse.Namespace) -> Path | int:
    repo = _repo(args)
    return _pinned(args, repo)


def cmd_migrate(args: argparse.Namespace) -> int:
    from mcctl import migrate

    root = _pinned_root(args)
    if isinstance(root, int):
        return root
    ctx = _ctx(args, root)
    if args.rollback:
        return _p12("migrate", lambda: migrate.rollback(ctx, dry_run=args.dry_run))
    if not args.to:
        print("mcctl migrate: --to IPV4 (or --rollback)", file=sys.stderr)
        return 2
    return _p12("migrate", lambda: migrate.run(ctx, args.to, args.ipv6, dry_run=args.dry_run))


def cmd_retire_old(args: argparse.Namespace) -> int:
    from mcctl import migrate

    return _p12("retire-old", lambda: migrate.retire_old(_ctx(args), dry_run=args.dry_run))


def cmd_upgrade(args: argparse.Namespace) -> int:
    from mcctl import upgrade

    if args.minecraft_version is not None:
        if args.to or args.sha:
            print("mcctl upgrade: --minecraft-version can't be combined with --to/--sha", file=sys.stderr)
            return 2
        return _lock_update(args, args.minecraft_version)
    return _p12(
        "upgrade", lambda: upgrade.product_upgrade(_ctx(args), args.to, args.sha, dry_run=args.dry_run)
    )


def _lock_update(args: argparse.Namespace, version: str | None) -> int:
    from mcctl import upgrade

    root = _pinned_root(args)
    if isinstance(root, int):
        return root
    ctx = _ctx(args, root)
    return _p12(
        "upgrade",
        lambda: upgrade.minecraft_upgrade(
            ctx,
            version,
            dry_run=args.dry_run,
            allow_paper=args.allow_paper,
            client=getattr(args, "client", False),
        ),
    )


def cmd_lock(args: argparse.Namespace) -> int:
    return _lock_update(args, args.minecraft_version)


def cmd_schema_migrate(args: argparse.Namespace) -> int:
    from mcctl import upgrade

    for line in upgrade.schema_migrate(Path(args.config_dir).resolve(), args.from_version):
        print(f"migrated: {line}")
    return 0


def cmd_secrets(args: argparse.Namespace) -> int:
    from mcctl import rotate

    if (
        args.action == "dashlane"
    ):  # P14: which Dashlane items to create; --convert swaps values for references
        from mcctl import integrations

        return _p12("secrets", lambda: integrations.secrets_dashlane(_ctx(args), args.convert))
    if args.action == "show-restic" and not sys.stdout.isatty():  # refused before anything is read
        print(
            "mcctl secrets show-restic: stdout isn't a terminal (the password is only printed to one)",
            file=sys.stderr,
        )
        return 2
    root = _pinned_root(args)
    if isinstance(root, int):
        return root
    ctx = _ctx(args, root)
    if args.action == "show-restic":  # P14 review: hand-run hints use this, never `. .env` (dl:// refs)
        from mcctl import integrations

        return _p12("secrets", lambda: integrations.show_restic(ctx))
    return _p12(
        "secrets",
        lambda: rotate.run(
            ctx,
            args.name,
            lambda repo, r: deploy_mod.run(repo, r),
            dry_run=args.dry_run,
        ),
    )


def cmd_discord(args: argparse.Namespace) -> int:
    """P14: register the slash commands and set the interactions endpoint (after a change, or a new token)."""
    from mcctl import integrations

    def go() -> int:
        integrations.sync(_ctx(args), register=not args.endpoint_only)
        return 0

    return _p12("discord", go)


def cmd_players(args: argparse.Namespace) -> int:
    from mcctl import players

    if args.action != "list" and not args.name:
        print(f"mcctl players {args.action}: give a player NAME", file=sys.stderr)
        return 2
    if args.action in actions.MODERATION:  # v1.3: live moderation over RCON, never players.yml
        rem = _remote(args)
        return actions.guarded(rem, lambda: actions.moderate(rem, args.action, args.name, args.reason))
    ctx = _ctx(args)
    _conversation(ctx, args)  # unattended: the pull request is left unmerged unless --merge
    return _p12(
        "players",
        lambda: players.change(
            ctx,
            args.action,
            args.name or "",
            uuid=args.uuid,
            level=args.level,
            route="push" if args.push else "commit" if args.no_push else "pr",
            follow=not args.no_follow,
        ),
    )


def _conversation(ctx: init.Ctx, args: argparse.Namespace) -> None:
    """Without a terminal on stdin, publish's (and players') questions take their defaults (the safe answers:
    don't merge, wait for an empty server, no Terraform apply); with one, it asks. `--merge`/`--no-merge` and
    `--restart` answer the merge and the players-online questions up front (the dashboard collects them
    first): then nothing is asked and the other questions take their safe defaults too."""
    from mcctl import ui as ui_mod

    preset: dict[str, object] = {}
    if getattr(args, "merge", None) is not None:
        preset["publish_merge"] = args.merge
    if getattr(args, "restart", None):
        preset["publish_restart"] = args.restart
    if preset or not sys.stdin.isatty():
        ctx.ui = ui_mod.UI(answers=preset)


def cmd_publish(args: argparse.Namespace) -> int:
    """v1.3: runs in this mcctl (not re-executed from the pin); `apply` and the deploy route run the pinned
    product's own commands (upgrade.run_in_product)."""
    from mcctl import publish

    ctx = _ctx(args)
    _conversation(ctx, args)
    tools = publish.Tools(max_wait_minutes=args.max_wait)
    return _p12(
        "publish",
        lambda: publish.run(
            ctx,
            route=args.route,
            dry_run=args.dry_run,
            revert=args.revert,
            wait_empty=args.wait_empty,
            follow=not args.no_follow,
            message=args.message,
            tools=tools,
        ),
    )


def cmd_restore(args: argparse.Namespace) -> int:
    from mcctl import restorecmd

    ctx = _ctx(args)
    if args.live:
        return _p12(
            "restore", lambda: restorecmd.live(ctx, args.live, dry_run=args.dry_run, confirm=args.confirm)
        )
    if args.to:
        return _p12("restore", lambda: restorecmd.to_dir(ctx, args.to, args.snapshot))
    return _p12("restore", lambda: restorecmd.list_snapshots(ctx))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcctl", description="Manage a Minecraft server defined by server.yml.")
    p.add_argument("--version", action="version", version=f"mcctl {__version__}")
    p.add_argument(
        "--plain",
        action="store_true",
        help="plain text output, no colours/panels/spinners (anywhere on the command line; also NO_COLOR, "
        "CI, or output that isn't a terminal)",
    )
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def with_config(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "-C",
            "--config-dir",
            type=Path,
            default=DEFAULT_DIR,
            help="the config repo (default: ., else the remembered one: ~/.config/mcctl/config.toml)",
        )
        sp.add_argument("--i-know", action="store_true", help="allow risky settings (online_mode: false)")

    sp = sub.add_parser("validate", help="check a config repo: server.yml, players.yml, locks, expiry, pin")
    with_config(sp)
    sp.add_argument("--github-output", type=Path, help="append CI values (key=value) to this file")
    sp.add_argument(
        "--expect-repository", metavar="OWNER/REPO", help="fail unless server.yml github.* names it (CI)"
    )
    sp.set_defaults(func=cmd_validate)

    sp = sub.add_parser(
        "panel-pr-check",
        help="CI: may a panel pull request (whitelist, or the world library) be merged automatically?",
    )
    with_config(sp)
    sp.add_argument("--base", required=True, help="the pull request's base commit")
    sp.add_argument("--head", default="HEAD", help="the pull request's head commit (default HEAD)")
    sp.set_defaults(func=cmd_panel_pr_check)

    sp = sub.add_parser(
        "pr-gate", help="CI (`server / gate`): may this pull request merge without the admin? (P10 review)"
    )
    with_config(sp)
    sp.add_argument("--base", required=True, help="the pull request's base commit")
    sp.add_argument("--head", default="HEAD", help="the pull request's head commit (default HEAD)")
    sp.add_argument(
        "--head-ref", default="", help="the head branch (panel/* or update/*; empty for a fork: admin only)"
    )
    sp.add_argument(
        "--author", default="", help="the pull request's author login (github.event.pull_request.user)"
    )
    sp.add_argument("--sender", default="", help="who sent the event the gate runs for (github.event.sender)")
    sp.add_argument(
        "--github-output", type=Path, help="append policy=<whitelist|worlds|builds|product> to this file"
    )
    sp.set_defaults(func=cmd_pr_gate)

    sp = sub.add_parser("update-prs", help="CI (P11 update task): open the lock/product update pull requests")
    with_config(sp)
    sp.set_defaults(func=cmd_update_prs)

    sp = sub.add_parser("expiry-check", help="CI (P11): credential/domain expiry and watchdog freshness")
    with_config(sp)
    sp.set_defaults(func=cmd_expiry_check)

    sp = sub.add_parser("render", help="write the Terraform/Ansible inputs from a config repo")
    with_config(sp)
    sp.add_argument("-o", "--out", type=Path, help="default: <config>/.mcctl/rendered")
    sp.set_defaults(func=cmd_render)

    sp = sub.add_parser(
        "client-publish",
        help="CI (v1.1): fill the artifact mirror, publish the client packs (ARTIFACT_PUBLISH_TOKEN)",
    )
    with_config(sp)
    sp.set_defaults(func=cmd_client_publish)

    sp = sub.add_parser("schema", help="regenerate the JSON Schema of server.yml")
    sp.add_argument("-o", "--output", type=Path, default=SCHEMA_FILE, help=f"default: ./{SCHEMA_FILE}")
    sp.add_argument("--check", action="store_true", help="fail if the file is stale")
    sp.set_defaults(func=cmd_schema)

    sp = sub.add_parser("deploy", help="run site.yml from here, secrets from `terraform output` (same as CI)")
    with_config(sp)
    sp.add_argument("--check", action="store_true", help="dry run (ansible-playbook --check --diff)")
    sp.add_argument(
        "--force-restart",
        action="store_true",
        help="v1.3: restart Minecraft now if needed, even outside server.yml maintenance.window",
    )
    sp.set_defaults(func=cmd_deploy)

    sp = sub.add_parser("doctor", help="check tools, .env, credentials (writes expiry.yml), DNS, tailnet")
    with_config(sp)
    sp.add_argument("--fix", action="store_true", help="fix what is safe to fix (.env mode)")
    sp.add_argument(
        "--cloudflare-permissions",
        action="store_true",
        help="only: the permission group names the token form offers for mcctl's rows, and the token's audit",
    )
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser(
        "metrics", help="player experience from Cloudflare (D1): summary, or `metrics export --since D` (CSV)"
    )
    with_config(sp)
    sp.add_argument("action", nargs="?", choices=["summary", "export"], default="summary")
    sp.add_argument("--days", type=int, default=7, help="summary: how many days back (default 7)")
    sp.add_argument("--since", default="", help="export: first day, YYYY-MM-DD (UTC)")
    sp.add_argument("--until", default="", help="export: end (exclusive), default now")
    sp.add_argument("--table", choices=["minute", "hour", "players", "sessions", "events"], default="minute")
    sp.add_argument("--format", choices=["csv"], default="csv", help="export format (csv)")
    sp.add_argument("-o", "--output", help="export: write to this file instead of stdout")
    sp.set_defaults(func=cmd_metrics)

    sp = sub.add_parser("status", help="server, players, TPS/MSPT, last backup, last deploy, drift")
    with_config(sp)
    sp.add_argument("--json", action="store_true", help="machine-readable output")
    sp.set_defaults(func=cmd_status)

    for name, func, text in (
        ("plan", cmd_plan, "terraform plan with the rendered inputs"),
        ("apply", cmd_apply, "terraform apply (shows the plan, asks for the deployment name)"),
    ):
        sp = sub.add_parser(name, help=text)
        with_config(sp)
        sp.add_argument("--target", action="append", default=[], metavar="ADDR", help="-target (repeatable)")
        sp.add_argument(
            "--replace", action="append", default=[], metavar="ADDR", help="-replace (repeatable)"
        )
        sp.set_defaults(func=func)

    sp = sub.add_parser(
        "bootstrap", help="fresh Tailscale key (apply), bootstrap.yml, verify (setup step 14)"
    )
    with_config(sp)
    sp.add_argument("--force", action="store_true", help="run again although already bootstrapped")
    sp.set_defaults(func=cmd_bootstrap)

    sp = sub.add_parser("rcon", help="run a console command over the tailnet (`mc` on the server)")
    with_config(sp)
    sp.add_argument("command", nargs=argparse.REMAINDER, help="e.g. list, or whitelist add NAME")
    sp.set_defaults(func=cmd_rcon)

    sp = sub.add_parser("ssh", help="Tailscale SSH to the server as the deploy user")
    with_config(sp)
    sp.set_defaults(func=cmd_ssh)

    sp = sub.add_parser("logs", help="journal of a unit on the server")
    with_config(sp)
    sp.add_argument("unit", nargs="?", default="minecraft", help="default: minecraft")
    sp.add_argument("-n", "--lines", type=int, default=100, help="default: 100")
    sp.add_argument("-f", "--follow", action="store_true", help="keep following")
    sp.set_defaults(func=cmd_logs)

    sp = sub.add_parser("backup", help="start a backup now, or list snapshots")
    with_config(sp)
    sp.add_argument("action", choices=["now", "list"])
    sp.add_argument("--latest", type=int, default=10, help="list: snapshots per host/path (default 10)")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("stop", help="stop the server on purpose (self-heal, watchdog, deploys leave it)")
    with_config(sp)
    sp.add_argument("--reason", default="", help="recorded in the admin-stopped marker")
    sp.set_defaults(func=cmd_stop)

    sp = sub.add_parser("start", help="start an admin-stopped server again (removes the marker)")
    with_config(sp)
    sp.set_defaults(func=cmd_start)

    countdown_help = "seconds of in-game countdown: 0, 10, 60 (default) or 300; skipped when nobody is online"
    sp = sub.add_parser("restart", help="restart the server like the panel: in-game countdown, then restart")
    with_config(sp)
    sp.add_argument("--countdown", type=int, default=actions.DEFAULT_COUNTDOWN, help=countdown_help)
    sp.add_argument("--backup", action="store_true", help="back up first")
    sp.add_argument("--wait", action="store_true", help="count down even when nobody is online")
    sp.set_defaults(func=cmd_restart)

    sp = sub.add_parser("console", help="server console over RCON: a prompt, or one COMMAND (= rcon)")
    with_config(sp)
    sp.add_argument("command", nargs=argparse.REMAINDER, help="one command (e.g. list); none: a prompt")
    sp.set_defaults(func=cmd_console)

    confirm_help = "the typed confirmation (scripts); without it a terminal asks, anything else refuses"
    merge_help = (
        "answer the merge question up front: --merge merges once the checks pass (that deploys), --no-merge "
        "leaves the pull request to you (the default without a terminal)"
    )
    sp = sub.add_parser("worlds", help="the world library (like the panel): list, create, switch, delete")
    wsub = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    w = wsub.add_parser("list", help="the active world, archived worlds, the library")
    with_config(w)
    w = wsub.add_parser("create", help="a new world (the active one is archived first); typed `create NAME`")
    with_config(w)
    w.add_argument("name")
    w.add_argument("--seed", default="", help="a whole number (default: random)")
    w.add_argument("--type", default="default", choices=actions.WORLD_TYPES)
    w.add_argument("--preset", default="survival_smp", choices=actions.WORLD_PRESETS)
    w.add_argument("--pregen", action=argparse.BooleanOptionalAction, default=None, help="pre-generate")
    w.add_argument("--countdown", type=int, default=actions.DEFAULT_COUNTDOWN, help=countdown_help)
    w.add_argument("--confirm", metavar="TEXT", help=confirm_help)
    w = wsub.add_parser("switch", help="switch to an archived world; typed `switch to NAME`")
    with_config(w)
    w.add_argument("name")
    w.add_argument("--countdown", type=int, default=actions.DEFAULT_COUNTDOWN, help=countdown_help)
    w.add_argument("--confirm", metavar="TEXT", help=confirm_help)
    w = wsub.add_parser("delete", help="forget an archived world (never the active one); typed `delete NAME`")
    with_config(w)
    w.add_argument("name")
    w.add_argument("--confirm", metavar="TEXT", help=confirm_help)
    sp.set_defaults(func=cmd_worlds)

    sp = sub.add_parser("plugins", help="locked plugins: list, enable/disable (countdown restart, typed)")
    psub = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    w = psub.add_parser("list", help="the deployed lock's plugins and whether each is enabled")
    with_config(w)
    for verb in ("enable", "disable"):
        w = psub.add_parser(
            verb, help=f"{verb} a locked plugin (restart after a countdown); typed `{verb} NAME`"
        )
        with_config(w)
        w.add_argument("name")
        w.add_argument("--countdown", type=int, default=actions.DEFAULT_COUNTDOWN, help=countdown_help)
        w.add_argument("--confirm", metavar="TEXT", help=confirm_help)
    sp.set_defaults(func=cmd_plugins)

    for name, text in (
        ("setup", "guided end-to-end setup (= init): resumable, with a checklist of what's done and pending"),
        ("init", "the onboarding wizard: config repo, tokens, cloud, host, first deploy"),
    ):
        sp = sub.add_parser(name, help=text)
        sp.add_argument(
            "-C", "--config-dir", type=Path, help="the config repo's local folder (asked if omitted)"
        )
        sp.add_argument(
            "--from",
            dest="from_step",
            metavar="STEP",
            help="re-run from this step (its number 1-17 as shown, or its name)",
        )
        sp.add_argument(
            "--answers", type=Path, metavar="FILE", help="answers file (YAML) for --non-interactive"
        )
        sp.add_argument(
            "--non-interactive", action="store_true", help="never prompt; secrets from the environment"
        )
        sp.add_argument("--mock", action="store_true", help="fake providers and tools (tests, e2e-mock CI)")
        sp.add_argument(
            "--product-sha", metavar="SHA", help="pin this product commit (default: this checkout)"
        )
        sp.add_argument(
            "--seed",
            type=Path,
            metavar="FILE",
            help="start server.yml from this file (e.g. a server.yml kept from an earlier deployment)",
        )
        sp.add_argument(
            "--reconfigure",
            action="append",
            default=[],
            metavar="STEP",
            help="re-run this step and ask again what it remembers (e.g. discord); repeatable",
        )
        sp.set_defaults(func=cmd_init)

    sp = sub.add_parser(
        "product-setup", help="the product repo's GitHub settings (auto-merge, ruleset, access)"
    )
    sp.add_argument("--repo", default=configrepo.PRODUCT_REPOSITORY, help="owner/name (default: %(default)s)")
    sp.add_argument("--check", action="store_true", help="only show what differs (exit 1 if anything does)")
    sp.set_defaults(func=cmd_product_setup)

    sp = sub.add_parser("config", help="get or set one server.yml value (schema-checked, comments kept)")
    csub = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    g = csub.add_parser("get", help="print the effective value (defaults included)")
    with_config(g)
    g.add_argument("path", help="dotted path, e.g. minecraft.motd")
    s_ = csub.add_parser("set", help="set a value, validate the whole repo, commit")
    with_config(s_)
    s_.add_argument("path", help="dotted path, e.g. performance.view_distance")
    s_.add_argument("value", help="a YAML value: 10, true, null, 'text', [a, b]")
    s_.add_argument("--no-commit", action="store_true", help="only edit server.yml")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("migrate", help="move the server to a new machine (fence, cutover, restore, verify)")
    with_config(sp)
    sp.add_argument("--to", metavar="IPV4", help="the new machine's public IPv4")
    sp.add_argument("--ipv6", metavar="IPV6", help="one public IPv6 address of the new machine")
    sp.add_argument("--rollback", action="store_true", help="undo a migration (until retire-old)")
    sp.add_argument("--dry-run", action="store_true", help="print the exact sequence, change nothing")
    sp.set_defaults(func=cmd_migrate)

    sp = sub.add_parser("retire-old", help="after a migration: delete the old box's tailnet node (typed)")
    with_config(sp)
    sp.add_argument("--dry-run", action="store_true", help="print what would be deleted")
    sp.set_defaults(func=cmd_retire_old)

    sp = sub.add_parser(
        "upgrade", help="move the product pin (PR), or the Minecraft version (--minecraft-version)"
    )
    with_config(sp)
    sp.add_argument("--to", metavar="vX.Y.Z", help="product release (default: the latest)")
    sp.add_argument("--sha", help="the release's commit (default: from the GitHub API)")
    sp.add_argument("--minecraft-version", metavar="V", help="upgrade Minecraft to V (typed confirmation)")
    sp.add_argument("--allow-paper", action="store_true", help="Minecraft: Paper if Purpur has no build")
    sp.add_argument("--dry-run", action="store_true", help="print the exact sequence, change nothing")
    sp.set_defaults(func=cmd_upgrade)

    sp = sub.add_parser("lock", help="lock files: `lock update [--minecraft-version V]` (a PR)")
    lsub = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    lu = lsub.add_parser("update", help="newest builds (same version), or a new Minecraft version")
    with_config(lu)
    lu.add_argument("--minecraft-version", metavar="V", help="a new Minecraft version (typed confirmation)")
    lu.add_argument("--allow-paper", action="store_true", help="Paper if Purpur has no build")
    lu.add_argument(
        "--client",
        action="store_true",
        help="also create client.lock.yml if there is none (before `config set client.enabled true`)",
    )
    lu.add_argument("--dry-run", action="store_true", help="print the exact sequence, change nothing")
    sp.set_defaults(func=cmd_lock)

    sp = sub.add_parser(
        "schema-migrate", help="(used by `upgrade`) apply this release's server.yml migrations"
    )
    with_config(sp)
    sp.add_argument(
        "--from", dest="from_version", required=True, metavar="X.Y.Z", help="the previous release"
    )
    sp.set_defaults(func=cmd_schema_migrate)

    sp = sub.add_parser(
        "secrets",
        help="`secrets rotate NAME`: replace a generated secret; `secrets dashlane`: Dashlane items; "
        "`secrets show-restic`: the restic password (terminal only)",
    )
    ssub = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    sr = ssub.add_parser("rotate", help="rcon | heartbeat | agent-token | command-hmac | r2 | restic-key")
    with_config(sr)
    sr.add_argument("name", choices=["rcon", "heartbeat", "agent-token", "command-hmac", "r2", "restic-key"])
    sr.add_argument("--dry-run", action="store_true", help="print the exact sequence, change nothing")
    sd = ssub.add_parser("dashlane", help="the Dashlane items for .env's credentials (dl:// references)")
    with_config(sd)
    sd.add_argument(
        "--convert", action="store_true", help="replace values with references that read back the same value"
    )
    ss = ssub.add_parser(
        "show-restic", help="print the restic password once, to the terminal only (for a password manager)"
    )
    with_config(ss)
    sp.set_defaults(func=cmd_secrets)

    sp = sub.add_parser("discord", help="`discord sync`: register the slash commands, set the endpoint")
    dsub = sp.add_subparsers(dest="action", required=True, metavar="ACTION")
    ds = dsub.add_parser("sync", help="register the guild commands and point the endpoint at the Worker")
    with_config(ds)
    ds.add_argument("--endpoint-only", action="store_true", help="only set the interactions endpoint")
    sp.set_defaults(func=cmd_discord)

    sp = sub.add_parser(
        "players",
        help="whitelist/ops in players.yml as a pull request (--push: to main); kick/ban/pardon: live",
    )
    with_config(sp)
    sp.add_argument("action", choices=["list", "add", "remove", "op", "deop", *actions.MODERATION])
    sp.add_argument("name", nargs="?", help="Minecraft name")
    sp.add_argument("--uuid", help="skip the Mojang lookup")
    sp.add_argument("--level", type=int, default=4, help="op: permission level 1-4 (default 4)")
    g = sp.add_mutually_exclusive_group()
    g.add_argument(
        "--push", action="store_true", help="commit and push to main (deploys) instead of a pull request"
    )
    g.add_argument("--no-push", action="store_true", help="only commit (`mcctl publish` lands it later)")
    sp.add_argument(
        "--no-follow", action="store_true", help="open the pull request, then stop (no checks, merge, deploy)"
    )
    sp.add_argument("--reason", default="", help="kick/ban: shown to the player (one line, 100 characters)")
    sp.add_argument("--merge", action=argparse.BooleanOptionalAction, default=None, help=merge_help)
    sp.set_defaults(func=cmd_players)

    sp = sub.add_parser(
        "publish", help="preview a config change (restart? Terraform?), validate, land it: a PR by default"
    )
    with_config(sp)
    sp.add_argument(
        "--route",
        choices=["pr", "main", "deploy"],
        default="pr",
        help="pr (default): pull request, checks, your merge; main: push to main; deploy: emergency local "
        "deploy (skips CI)",
    )
    sp.add_argument("--dry-run", action="store_true", help="the preview and validation only")
    sp.add_argument("--revert", action="store_true", help="a PR reverting the last deploy's commits")
    sp.add_argument(
        "--wait-empty", action="store_true", help="a restart with players online waits for an empty server"
    )
    sp.add_argument("--max-wait", type=int, default=120, metavar="MIN", help="wait at most MIN minutes")
    sp.add_argument("--no-follow", action="store_true", help="open the PR (or push), then stop")
    sp.add_argument("-m", "--message", help="the commit message / PR title (default: a summary)")
    sp.add_argument("--merge", action=argparse.BooleanOptionalAction, default=None, help=merge_help)
    sp.add_argument(
        "--restart",
        choices=RESTART_ANSWERS,
        help="answer the players-online question up front: now (countdown), wait (= --wait-empty), cancel",
    )
    sp.set_defaults(func=cmd_publish)

    sp = sub.add_parser("restore", help="snapshots: --list, --to DIR (scratch, verified), --live ID (typed)")
    with_config(sp)
    g = sp.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true", help="list snapshots (default)")
    g.add_argument("--to", metavar="DIR", help="restore into a new scratch directory on the server")
    g.add_argument("--live", metavar="SNAPSHOT", help="replace the live world (safety snapshot first)")
    sp.add_argument("--snapshot", help="--to: this snapshot instead of the latest")
    sp.add_argument("--dry-run", action="store_true", help="--live: show the plan only")
    sp.add_argument(
        "--confirm", metavar="TEXT", help="--live: the typed confirmation (`restore <8 hex>`) up front"
    )
    sp.set_defaults(func=cmd_restore)
    return p


# Commands whose trailing words go to the server, verbatim (`mcctl rcon say --plain` says "--plain").
_VERBATIM = ("rcon", "console")


def strip_plain(argv: list[str]) -> tuple[list[str], bool]:
    """`--plain` anywhere before `--` (and before an rcon command's own words) -> (argv without it, True)."""
    out, plain, seen_command = [], False, False
    for i, arg in enumerate(argv):
        if arg == "--":
            return out + argv[i:], plain
        if arg == "--plain" and not (seen_command and out and out[0] in _VERBATIM):
            plain = True
            continue
        if not arg.startswith("-"):
            seen_command = True
        out.append(arg)
    return out, plain


def _command_words(args: argparse.Namespace) -> int:
    """How many leading words of argv name the command (`config get`, `lock update`: 2)."""
    return 2 if getattr(args, "action", None) and args.command in _NESTED else 1


_NESTED = ("config", "lock", "secrets", "discord", "worlds", "plugins")


def resolve_config_dir(args: argparse.Namespace) -> None:
    """`-C` not given and the current directory isn't a config repo: use the remembered one (and pass it on
    to a re-executed pinned mcctl, which may be older and not know about remembering)."""
    from mcctl import homecfg

    if getattr(args, "config_dir", None) is not DEFAULT_DIR or homecfg.looks_like_config_repo(Path.cwd()):
        return
    remembered = homecfg.remembered()
    if remembered is None:
        return
    args.config_dir = remembered
    at = _command_words(args)
    args.argv = [*args.argv[:at], "-C", str(remembered), *args.argv[at:]]
    print(f"mcctl: config repo {remembered} (remembered; -C DIR for another)", file=sys.stderr)


def remember_config_dir(args: argparse.Namespace) -> None:
    """After a successful command with an explicit `-C` naming a config repo: remember it (not in CI)."""
    from mcctl import homecfg

    given = getattr(args, "config_dir", None)
    if given is None or given is DEFAULT_DIR or os.environ.get("CI"):
        return
    root = Path(given).expanduser()
    if not homecfg.looks_like_config_repo(root):
        return
    try:
        homecfg.remember(root)
    except OSError as e:  # never fails the command that just succeeded
        print(f"mcctl: couldn't remember {root}: {e}", file=sys.stderr)


def run(argv: list[str], *, update_notice: bool = False) -> int:
    """Parse and run one command (argv without --plain). `update_notice`
    (main only; the dashboard has its own line): v1.3, say when a newer minor/major release is out."""
    args = build_parser().parse_args(argv)
    args.argv = list(argv)  # without --plain: an older pinned product doesn't know the flag
    resolve_config_dir(args)
    check = None
    if update_notice and getattr(args, "config_dir", None) is not None:
        from mcctl import notice

        check = notice.start(args.command, Path(args.config_dir).expanduser())
    code = int(args.func(args))
    if code == 0:
        remember_config_dir(args)
    if check is not None:
        check.finish()
    return code


def main(argv: list[str] | None = None) -> int:
    if problem := python_problem():
        print(f"mcctl: {problem}", file=sys.stderr)
        return 1
    argv = list(sys.argv[1:] if argv is None else argv)
    argv, plain = strip_plain(argv)
    if plain:
        ui.set_plain()  # MCCTL_PLAIN=1: this process and a re-executed (pinned) mcctl
    from mcctl import home

    if (opened := home.wanted(argv)) is not None:  # plain `mcctl` on a terminal: the dashboard
        return home.main(opened)
    return run(argv, update_notice=True)


if __name__ == "__main__":
    sys.exit(main())
