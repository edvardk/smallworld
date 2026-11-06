"""`mcctl secrets rotate NAME [--dry-run]` (design §3 "Rotation", §8 P12).

| NAME         | Terraform resource replaced      | then                                                 |
|--------------|----------------------------------|------------------------------------------------------|
| rcon         | random_password.rcon             | deploy (server.properties + the ops env files)       |
| heartbeat    | random_password.heartbeat        | deploy (the Worker got the new token with the apply) |
| agent-token  | random_password.agent_token      | deploy                                               |
| command-hmac | random_password.command_hmac_key | deploy                                               |
| r2           | cloudflare_api_token.r2_backups  | deploy (the old token is deleted by the apply); the  |
|              | (account_token if account-owned) | address follows cloudflare.token_owner               |
| restic-key   | random_password.restic           | deploy (it adds the new password as a key first) ->  |
|              |                                  | key remove of the old key                            |

The apply shows its plan and takes the typed deployment name (tf.apply_flow); it also updates the config
repo's Actions secrets and the Worker's bindings. The deploy is the local one (`mcctl deploy`: secrets from
`terraform output`, same as CI). No value is ever printed, put on argv or stored.

restic-key (P12 review): after the apply, Terraform and the Actions secret only know the new password, so any
deploy may be the first to bring it. Every deploy therefore checks it before writing restic.env (backups role,
scripts/restic_rekey.py as the ops user): if the repository doesn't accept it but the current restic.env does,
it adds it with `restic key add --new-password-file /dev/stdin` (the password on stdin, never argv); otherwise
it fails without touching restic.env. The rotation is resumable (`.mcctl/state.json` `rotate-restic`: old key
id and phase only) and safe whichever deploy comes first. The R2 bucket lock keeps `keys/` forever
(terraform/cloudflare.tf), so the final `key remove` is refused there: the old password then still opens the
repository, and mcctl says so.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from mcctl import configrepo, deploy, init, remote, render, serverops, tf

R2_RESOURCE = {
    "user": "cloudflare_api_token.r2_backups[0]",
    "account": "cloudflare_account_token.r2_backups[0]",
}
RESOURCES = {
    "rcon": "random_password.rcon",
    "heartbeat": "random_password.heartbeat",
    "agent-token": "random_password.agent_token",
    "command-hmac": "random_password.command_hmac_key",
    "r2": R2_RESOURCE["user"],  # the account-owned one when cloudflare.token_owner is account (resource())
    "restic-key": "random_password.restic",
}
STATE_KEY = "rotate-restic"
Deployer = Callable[[configrepo.ConfigRepo, Path], int]


def resource(name: str, owner: str = "user") -> str:
    """The Terraform address `-replace` targets (the R2 token is owned like the admin token)."""
    return R2_RESOURCE.get(owner, R2_RESOURCE["user"]) if name == "r2" else RESOURCES[name]


def plan_lines(name: str, owner: str = "user") -> list[str]:
    res = resource(name, owner)
    lines = [f"mcctl secrets rotate {name}:"]
    if name == "restic-key":
        lines += [
            "  1. server: note the current restic key id (`restic key list`)",
            f"  2. terraform apply -replace={res} (plan + typed name; Actions secret updated)",
            "  3. mcctl deploy: adds the new password as a key (stdin only) if the repository lacks it, then"
            " restic.env gets it (any other deploy in between does the same) -> check the repository opens",
            "  4. server: `restic key remove <old id>` (a bucket lock may refuse: old key stays)",
            "  5. store the new password in your password manager (`mcctl secrets show-restic`)",
        ]
    else:
        lines += [
            f"  1. terraform apply -replace={res} (plan + typed name; Actions secrets, Worker bindings)",
            "  2. mcctl deploy (the server's env files get the new value; services restart where needed)",
        ]
    return lines


def _apply(ctx: init.Ctx, resource: str) -> tuple[object, Path]:
    repo = ctx.repo()
    terraform, out = ctx.deps.terraform(repo, ctx.product_root, {})
    applied = tf.apply_flow(
        terraform,
        repo.root,
        out / render.TFVARS_FILE,
        repo.server.name,
        replaces=[resource],  # type: ignore[arg-type]
        ask=ctx.ui.typed("confirm_apply"),
    )
    if not applied:
        raise init.StepError("nothing was applied", "re-run and type the deployment name")
    return terraform, out


def _ssh(ctx: init.Ctx, script: str, what: str, stdin: str | None = None) -> str:
    cfg = ctx.repo().server
    argv = remote.ssh_argv(cfg, script, host=init.tailnet_target(ctx, cfg))
    proc = ctx.deps.run(argv) if stdin is None else ctx.deps.run_input(argv, stdin)
    if proc.returncode != 0:
        raise init.StepError(f"{what} failed: {(proc.stderr or proc.stdout or '').strip()[-400:]}")
    return proc.stdout


def run(ctx: init.Ctx, name: str, deployer: Deployer, *, dry_run: bool = False) -> int:
    if name not in RESOURCES:
        ctx.ui.bad(f"unknown secret {name!r}: one of {', '.join(RESOURCES)}")
        return 2
    owner = ctx.repo().server.cloudflare.token_owner
    for line in plan_lines(name, owner):
        ctx.ui.say(line)
    if dry_run:
        ctx.ui.say("(dry run: nothing was changed)")
        return 0
    try:
        if name == "restic-key":
            return _restic(ctx, deployer)
        _apply(ctx, resource(name, owner))
        if deployer(ctx.repo(), ctx.product_root) != 0:
            raise init.StepError("the deploy failed (see above)", f"`mcctl deploy -C {ctx.root}` again")
    except (init.StepError, deploy.DeployError, tf.TfError) as e:
        ctx.ui.bad(str(e))
        if fix := getattr(e, "fix", ""):
            ctx.ui.info(f"fix: {fix}")
        return 1
    ctx.ui.ok(f"{name} rotated and deployed")
    return 0


def _restic(ctx: init.Ctx, deployer: Deployer) -> int:
    """noted -> applied -> deployed -> done, each phase recorded. The deploy adds the new key itself (the
    backups role's restic_rekey.py: the new password on stdin), so it doesn't matter which deploy comes first
    after the apply (CI's schedule, a panel merge, a push, or this one): every one of them leaves the
    repository opening with the new password, or fails without touching restic.env."""
    st = ctx.state
    prev = st.get(STATE_KEY) or {}
    rec = dict(prev.get("outputs") or {}) if prev.get("status") == "waiting" else {}
    if rec.get("phase") == "key-added":  # an older run's phase name: same position
        rec["phase"] = "applied"
    if not rec:
        old = serverops.current_key(_ssh(ctx, serverops.key_list_script(), "restic key list"))
        if old is None:
            raise init.StepError(
                "couldn't tell the current restic key id", "`mcctl ssh`, `sudo mc-restic restic key list`"
            )
        rec = {"old_key": old, "phase": "noted"}
        st.record(STATE_KEY, "waiting", outputs=rec)
    if rec["phase"] == "noted":
        _apply(ctx, RESOURCES["restic-key"])
        rec["phase"] = "applied"
        st.record(STATE_KEY, "waiting", outputs=rec)
    if rec["phase"] == "applied":
        if deployer(ctx.repo(), ctx.product_root) != 0:
            raise init.StepError("the deploy failed (see above)", "re-run `mcctl secrets rotate restic-key`")
        if "REPO_OK" not in _ssh(ctx, serverops.REPO_CHECK, "opening the repository with the new password"):
            raise init.StepError("the server can't open the repository with the new password")
        rec["phase"] = "deployed"
        st.record(STATE_KEY, "waiting", outputs=rec)
        ctx.ui.ok("the server's restic.env has the new password, and it opens the repository")
    if rec["phase"] == "deployed":
        listing = _ssh(ctx, serverops.key_list_script(), "restic key list")
        current = serverops.current_key(listing)
        if current == rec["old_key"]:
            raise init.StepError(
                "the server still opens the repository with the old key",
                "re-run `mcctl secrets rotate restic-key`",
            )
        if rec["old_key"] in serverops.key_ids(listing):
            cfg = ctx.repo().server
            argv = remote.ssh_argv(
                cfg, serverops.key_remove_script(rec["old_key"]), host=init.tailnet_target(ctx, cfg)
            )
            if ctx.deps.run(argv).returncode == 0:
                ctx.ui.ok(f"old key {rec['old_key'][:8]} removed")
            else:
                ctx.ui.warn(
                    f"old key {rec['old_key'][:8]} couldn't be removed (the bucket lock keeps keys/ "
                    "forever): the old password still opens the repository. Treat it as compromised "
                    "storage access only together with "
                    "the R2 keys (rotate `r2` too if both leaked)."
                )
    st.record(STATE_KEY, "done", outputs={"old_key": rec["old_key"], "phase": "done"})
    ctx.ui.info(
        "Store the new restic password in your password manager now:\n"
        f"  mcctl secrets show-restic -C {ctx.root}"
    )
    ctx.ui.ok("restic-key rotated")
    return 0
