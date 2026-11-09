"""`mcctl rollout [--to vX.Y.Z] [--dry-run]` (v1.5): a product release from "tagged" to "running".

Patch releases need none of this: the weekly update task opens the pin PR and `server / gate` merges it,
which deploys. `rollout` is for minor and major releases (the admin merges those) and for releases that
change the Worker (Terraform: `mcctl apply` from your computer). In the config repo:

1. **Sync.** The working tree must be clean and on `main`; `origin/main` is fetched and local main
   fast-forwarded to it. Unpushed commits or a diverged main are refused with the fix (a stale main is how a
   plan came out empty before).
2. **Pin PR.** The target is `--to` or the product's latest release. Already pinned there: straight to 5. An
   open pin PR for it (the weekly updater's `update/product-vX.Y.Z`, or an earlier `upgrade/product-vX.Y.Z`)
   is used; otherwise `mcctl upgrade --to` opens one (`upgrade.product_upgrade_pr`: the target's migration
   and validation, a pure patch bump on update/* with auto-merge).
3. **Checks and merge.** Its checks are followed (`publish.wait_checks`). A PR the gate merges (auto-merge
   on) is reported as auto-merged; any other one is merged by the admin (`gh pr merge --merge --admin`, the
   gate says "needs the admin") after typing the release tag.
4. **Deploy.** The deploy run of the merge commit is watched (`publish.follow_deploy`); a failed deploy stops.
5. **Terraform.** main is pulled again, then the *new* pin's `mcctl render` and `mcctl plan` run
   (re-executed from the pinned product, as `mcctl plan` itself does), and, after a yes, its `mcctl apply`,
   which shows the plan again and keeps its own typed confirmation (there is no `--yes`).

`--dry-run` resolves the target and prints these steps; it changes nothing (no fetch, no branch, no PR).
Everything outside goes through `ctx.deps` (git, gh, the clock, sleep, `gh run watch`, the HTTP transport)
and `publish.Tools` (the status probe, the pinned product's commands), so tests use temporary repositories
and fakes.
"""

from __future__ import annotations

import json

from mcctl import init, publish, updates, upgrade

MERGE_WAIT_S = 10 * 60  # the gate's auto-merge after the checks passed


def _token(ctx: init.Ctx) -> str:
    proc = ctx.deps.run(["gh", "auth", "token"])  # captured: never printed
    return proc.stdout.strip() if proc.returncode == 0 else ""


def sync(ctx: init.Ctx) -> None:
    """Clean, on main, fast-forwarded to the fetched origin/main; else a StepError with the fix."""
    if ctx.git("status", "--porcelain").stdout.strip():
        raise init.StepError("the config repo has uncommitted changes", "commit or stash them first")
    branch = ctx.git("symbolic-ref", "-q", "--short", "HEAD", check=False).stdout.strip()
    if branch != "main":
        raise init.StepError(
            f"the config repo is on {branch or 'a detached HEAD'}, not main", "`git switch main`"
        )
    fetched = ctx.git("fetch", "-q", "origin", "main", check=False)
    if fetched.returncode != 0:
        raise init.StepError(
            f"`git fetch origin main` failed: {(fetched.stderr or '').strip()}", "check `gh auth status`"
        )
    head = ctx.git("rev-parse", "HEAD").stdout.strip()
    remote = ctx.git("rev-parse", "origin/main").stdout.strip()
    if head == remote:
        return
    if ctx.git("merge-base", "--is-ancestor", head, remote, check=False).returncode == 0:
        ctx.git("merge", "-q", "--ff-only", "origin/main")
        ctx.ui.ok(f"main fast-forwarded to origin/main ({remote[:12]})")
        return
    if ctx.git("merge-base", "--is-ancestor", remote, head, check=False).returncode == 0:
        raise init.StepError(
            "local main has commits origin/main doesn't", "publish them first (`mcctl publish`), or push them"
        )
    raise init.StepError(
        "local main and origin/main have diverged",
        "keep your commits on a branch (`git branch keep`), then `git reset --hard origin/main`",
    )


def open_pr(ctx: init.Ctx, repository: str, to: str) -> upgrade.Upgraded | None:
    """An open pin PR for `to` (the updater's, or an earlier upgrade's), or None."""
    for branch, unattended in ((f"update/product-{to}", True), (f"upgrade/product-{to}", False)):
        url = upgrade.open_pin_pr(ctx, repository, [branch])
        if url is not None:
            return upgrade.Upgraded(url, branch, to, "", unattended)
    return None


def pr_state(ctx: init.Ctx, url: str) -> str:
    proc = ctx.gh("pr", "view", url, "--json", "state")
    try:
        data = json.loads(proc.stdout) if proc.returncode == 0 else {}
    except ValueError:
        data = {}
    return str((data or {}).get("state", "")) if isinstance(data, dict) else ""


def merge(ctx: init.Ctx, pr: upgrade.Upgraded) -> bool:
    """Wait for the checks; the gate's auto-merge, or a merge after a typed confirmation. True: merged."""
    ui = ctx.ui
    if pr_state(ctx, pr.url) != "MERGED":
        verdict = publish.wait_checks(ctx, pr.url)
        if verdict != "pass":
            ui.bad("a check failed" if verdict == "fail" else "the checks are still running")
            ui.say(f"  not merged: {pr.url} (run `mcctl rollout` again once it is green)")
            return False
    if pr.unattended:  # the gate merges a pure patch bump by itself: give it a few minutes
        deadline = ctx.deps.now().timestamp() + MERGE_WAIT_S
        while pr_state(ctx, pr.url) != "MERGED" and ctx.deps.now().timestamp() < deadline:
            ctx.deps.sleep(publish.POLL_S)
    if pr_state(ctx, pr.url) == "MERGED":
        ui.ok(f"merged by `server / gate` (auto-merge): {pr.url}")
        return True
    typed = ui.typed("confirm_rollout_merge")(f"Type {pr.to} to merge {pr.url} (merging deploys it): ")
    if typed.strip() != pr.to:
        ui.say(f"  not merged (not confirmed): {pr.url} waits for you")
        return False
    merged = ctx.gh("pr", "merge", pr.url, "--merge", "--admin")
    if merged.returncode != 0:
        raise init.StepError(
            f"`gh pr merge` failed: {(merged.stderr or '').strip()}", f"merge {pr.url} on GitHub"
        )
    ui.ok("merged")
    return True


def terraform(ctx: init.Ctx, tools: publish.Tools) -> int:
    """The new pin's render and plan; its apply (own typed confirmation) after a yes."""
    ui = ctx.ui
    repo = ctx.repo()  # the pulled main: the new pin
    pin, root = repo.pin, str(ctx.root)
    ui.section(f"Terraform with the pinned product {pin.version or pin.sha[:12]}")
    if tools.product(pin, ["render", "-C", root]) != 0:
        raise init.StepError("`mcctl render` failed (see above)")
    if tools.product(pin, ["plan", "-C", root]) != 0:
        raise init.StepError("`mcctl plan` failed (see above)")
    if not ui.confirm(
        "rollout_apply",
        "Run `mcctl apply` now? (it shows the plan again and asks you to type the deployment name)",
        default=ui.interactive,
    ):
        ui.say("  not applied: run `mcctl apply` when you're ready (a plan with no changes needs none)")
        return 0
    return tools.product(pin, ["apply", "-C", root])


def run(
    ctx: init.Ctx, to: str | None = None, *, dry_run: bool = False, tools: publish.Tools | None = None
) -> int:
    ui = ctx.ui
    tools = tools or publish.Tools()
    publish.need_gh(ctx, "rollout follows the pin PR and the deploy run")
    if not dry_run:
        sync(ctx)  # first: the pin and server.yml as on origin/main (a dry run reads them as they are)
    repo = ctx.repo()
    pin, cfg = repo.pin, repo.server
    repository = publish.repository(cfg)
    sha: str | None = None
    if to is None:
        latest = updates.latest_release(pin.repository, _token(ctx), ctx.deps.transport)
        if latest is None:
            raise init.StepError(f"{pin.repository} has no release", "pass --to vX.Y.Z")
        to, sha = latest
    if not updates.RELEASE.fullmatch(to):
        raise init.StepError(f"--to must be a release tag vX.Y.Z, not {to!r}")
    pinned = pin.version == to
    if not pinned and not updates.newer(to, pin.version):
        raise init.StepError(f"{to} is older than the pinned {pin.version}: no downgrades")
    ui.say(f"mcctl rollout: {pin.repository} {pin.version or pin.sha[:12]} -> {to}")
    if not pinned and updates.is_patch(pin.version, to):
        ui.say(
            "  a patch release rolls out by itself (weekly update + `server / gate`); rollout adds Terraform"
        )
    steps = [
        "fetch, fast-forward main (refused when dirty, ahead or diverged)",
        *(
            [f"already pinned to {to}: no pull request"]
            if pinned
            else [
                f"the open pin PR for {to}, or `mcctl upgrade --to {to}`",
                "follow its checks; the gate's auto-merge, or your merge after typing the tag",
                "watch the deploy of the merge commit",
            ]
        ),
        "pull main; the new pin's `mcctl render` and `mcctl plan`",
        "`mcctl apply` after a yes (its own typed confirmation)",
    ]
    for n, line in enumerate(steps, 1):
        ui.say(f"  {n}. {line}")
    if dry_run:
        ui.say("(dry run: nothing was changed; local main as it is, a real run fetches first)")
        return 0
    if not pinned:
        ui.section(f"Pin PR for {to}")
        pr = open_pr(ctx, repository, to)
        if pr is not None:
            ui.ok(f"using the open pull request {pr.url}")
        else:
            made = upgrade.product_upgrade_pr(ctx, to, sha, runner=tools.product)
            assert made is not None  # not a dry run
            pr = made
        if not merge(ctx, pr):
            return 1
        commit = publish.merge_commit(ctx, pr.url)
        if not commit:
            raise init.StepError("couldn't read the merge commit", "follow the deploy in the Actions tab")
        if publish.follow_deploy(ctx, cfg, commit, tools) != 0:
            ui.say("  stopped before Terraform: fix the deploy, then `mcctl rollout` again")
            return 1
        publish.sync_main(ctx)
        sync(ctx)  # the merge is on main now, or the fix is said
    return terraform(ctx, tools)
