"""`mcctl upgrade` (design §4, §8 P12): the product pin, and the Minecraft version.

Product: `mcctl upgrade [--to vX.Y.Z [--sha SHA]] [--dry-run]`
  1. the target release: `--to` (its tag's commit from the GitHub API unless `--sha`), else releases/latest;
     no downgrades, no re-pin to the same version
  2. on a branch `upgrade/product-vX.Y.Z`: the workflow's `uses:` line moved to that commit (`# vX.Y.Z`)
  3. the **target** product's `mcctl schema-migrate --from <old>` (server.yml migrations, mcctl/migrations.py)
     and its `mcctl validate`, run from its pinned checkout (`~/.cache/mcctl/<sha>`, as every pinned command)
  4. commit, push the branch, open a pull request. Who merges (P12 review): only a **pure patch bump** (the
     migration changed nothing but the `uses:` line, vX.Y.Z -> vX.Y.Z+n, `automation.auto_merge:
     builds+patch`) goes on `update/product-vX.Y.Z` with auto-merge on, exactly the updater's product PR, so
     `server / gate`'s product policy (update/* branch, workflow only, patch, tag -> SHA) lets it merge;
     anything else (minor/major, or server.yml migrated) stays on `upgrade/product-vX.Y.Z` and the admin
     merges it (gate: "needs the admin")

Minecraft: `mcctl upgrade --minecraft-version V [--dry-run]` (= `mcctl lock update --minecraft-version V`;
without a version, `lock update` refreshes the builds of the current version)
  1. V must be newer than server.yml's version (a world can't be downgraded) and the pinned product must ship
     `server-config/keys/V/` (its overlays are checked against those keys; a maintainer generates them with
     scripts/config_keys.py, statically, never running the jar): otherwise upgrade the product first
  2. a typed confirmation: changing the Minecraft version is the owner's explicit choice
  3. (P12 review) no backup now: the PR may merge days later. The **deploy** of the merge takes the
     pre-upgrade backup right before the new jar can touch the world (minecraft role; its id is printed in the
     deploy log, recorded in /var/lib/minecraft/pre-upgrade.json and marked in `mcctl restore --list`)
  4. on a branch `upgrade/minecraft-V`: the pinned product's `scripts/update_lock.py --minecraft-version V`
     rewrites both locks; it fails without writing anything if **any locked plugin has no release for V**
     (the per-plugin compatibility check), then server.yml `minecraft.version: V` (comment-preserving), and
     the whole config repo is validated (lock vs version, overlay keys)
  5. commit, push, pull request: the reusable workflow's `boot-test` job boots the new jar with every locked
     plugin on the PR (staged boot test, `server / boot-test`); the gate leaves the merge to the admin
  Rollback: before the merge, close the PR. After it deployed: revert the merge commit (old jar/locks) **and**
  `mcctl restore --live <pre-upgrade snapshot>` (the deploy's, see 3; the new version has rewritten the world
  and the old jar can't load it).
"""

from __future__ import annotations

import datetime
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from mcctl import configrepo, gate, init, migrations, product, schema, updates, yamledit
from mcctl.providers import ProviderError

ProductRunner = Callable[[configrepo.Pin, Sequence[str]], int]
# (product root, config root, Minecraft version, allow Paper, v1.2: create client.lock.yml if missing)
Locker = Callable[[Path, Path, str, bool, bool], subprocess.CompletedProcess[str]]
MC_VERSION = re.compile(r"\d+(?:\.\d+){1,3}")


def run_in_product(pin: configrepo.Pin, args: Sequence[str]) -> int:
    """`mcctl <args>` as the product at `pin` runs it (its cached checkout and venv)."""
    prod = product.resolve(pin, os.environ)
    argv = product.reexec_argv(prod, list(args))
    if argv is None:
        from mcctl import cli

        return cli.main(list(args))
    return subprocess.run(argv, env=prod.env(os.environ), check=False).returncode


def update_lock(
    product_root: Path, config_root: Path, version: str, allow_paper: bool, client: bool = False
) -> subprocess.CompletedProcess[str]:
    argv = [
        sys.executable,
        str(product_root / "scripts" / "update_lock.py"),
        "--minecraft-version",
        version,
        "--lock-dir",
        str(config_root),
        *(["--allow-paper"] if allow_paper else []),
        *(["--client"] if client else []),
    ]
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def _gh_token(ctx: init.Ctx) -> str:
    proc = ctx.deps.run(["gh", "auth", "token"])  # captured: never printed
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _clean(ctx: init.Ctx) -> str:
    if ctx.git("status", "--porcelain").stdout.strip():
        raise init.StepError("the config repo has uncommitted changes", "commit or stash them first")
    return ctx.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _branch(ctx: init.Ctx, branch: str) -> None:
    ctx.git("checkout", "-q", "-B", branch)


def _abandon(ctx: init.Ctx, start: str, branch: str) -> None:
    ctx.git("checkout", "-q", "-f", start, check=False)
    ctx.git("branch", "-q", "-D", branch, check=False)


def _publish(ctx: init.Ctx, branch: str, title: str, body: str, paths: Sequence[str], start: str) -> str:
    ctx.git("add", "--", *paths)
    ctx.git("commit", "-q", "-m", title)
    proc = ctx.git("push", "-q", "-u", "origin", f"{branch}:{branch}", check=False)
    if proc.returncode != 0:
        raise init.StepError(
            f"`git push` of {branch} failed: {(proc.stderr or '').strip()}",
            "check `gh auth status`; if the branch exists already (the updater's PR?), use that PR",
        )
    cfg = ctx.repo().server
    pr = ctx.gh(
        "pr",
        "create",
        "-R",
        f"{cfg.github.owner}/{cfg.github.config_repo}",
        "--base",
        "main",
        "--head",
        branch,
        "--title",
        title,
        "--body",
        body,
    )
    ctx.git("checkout", "-q", start, check=False)
    if pr.returncode != 0:
        raise init.StepError(f"pushed {branch}, but `gh pr create` failed: {(pr.stderr or '').strip()}")
    return pr.stdout.strip()


# --- product -------------------------------------------------------------------------------------------


def product_upgrade(
    ctx: init.Ctx,
    to: str | None,
    sha: str | None,
    *,
    dry_run: bool = False,
    runner: ProductRunner = run_in_product,
) -> int:
    ui = ctx.ui
    repo = ctx.repo()
    pin = repo.pin
    token = _gh_token(ctx) if (to is None or sha is None) else ""
    if to is None:
        latest = updates.latest_release(pin.repository, token, ctx.deps.transport)
        if latest is None:
            raise init.StepError(f"{pin.repository} has no release to upgrade to", "pass --to vX.Y.Z")
        to, sha = latest
    if not updates.RELEASE.fullmatch(to):
        raise init.StepError(f"--to must be a release tag vX.Y.Z, not {to!r}")
    if not updates.newer(to, pin.version):
        raise init.StepError(f"{to} isn't newer than the pinned {pin.version or pin.sha[:12]}: no downgrades")
    if sha is None:
        sha = gate.tag_sha(pin.repository, to, token, ctx.deps.transport)
    if not sha or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise init.StepError(f"couldn't resolve {to} to a commit of {pin.repository}", "pass --sha")
    branch = f"upgrade/product-{to}"
    pending = migrations.pending(pin.version, to.lstrip("v")) if (pin.version and to) else []
    ui.say(f"mcctl upgrade: {pin.repository} {pin.version or pin.sha[:12]} -> {to} ({sha[:12]})")
    for n, line in enumerate(
        [
            f"branch {branch}: .github/workflows/server.yml `uses:` -> @{sha[:12]} # {to}",
            f"{to}'s `mcctl schema-migrate --from {pin.version or 'start'}` on server.yml"
            + (f" (this mcctl knows {len(pending)} pending)" if pending else ""),
            f"{to}'s `mcctl validate` on the result",
            "commit, push, pull request: a pure patch bump (workflow only, builds+patch) on "
            f"update/product-{to} merges itself via `server / gate`; else {branch}: your merge",
        ],
        1,
    ):
        ui.say(f"  {n}. {line}")
    if dry_run:
        ui.say("(dry run: nothing was changed)")
        return 0
    start = _clean(ctx)
    _branch(ctx, branch)
    new_pin = configrepo.Pin(pin.repository, sha, to)
    try:
        wf = ctx.root / configrepo.WORKFLOW
        bumped = updates.bump_pin(wf.read_text(), pin.repository, sha, to)
        wf.write_text(bumped)
        root = str(ctx.root)
        if runner(new_pin, ["schema-migrate", "-C", root, "--from", pin.version or "0.0.0"]) != 0:
            raise init.StepError(f"{to}'s schema migration failed (see above)")
        if runner(new_pin, ["validate", "-C", root]) != 0:
            raise init.StepError(
                f"the config repo doesn't validate with {to} (see above)", "fix server.yml on main first"
            )
        changed = sorted(
            line[3:].strip() for line in ctx.git("status", "--porcelain").stdout.splitlines() if line.strip()
        )
        unattended = (
            changed == [configrepo.WORKFLOW]
            and wf.read_text()
            == bumped  # v1.2: schema-migrate may add secrets to it (the gate wants `uses:` only)
            and updates.is_patch(pin.version, to)
            and repo.server.automation.auto_merge == "builds+patch"
        )
        if unattended:  # the same branch and checks as the updater's product PR (gate: product policy)
            ctx.git("branch", "-q", "-M", f"update/product-{to}")
            branch = f"update/product-{to}"
        who = (
            "A pure patch bump (only the `uses:` line): `server / gate` checks the tag and merges it by "
            "itself (auto-merge on)."
            if unattended
            else "Needs the admin's merge (`server / gate` only auto-merges a workflow-only patch bump"
            + (
                ": this one also migrates server.yml)."
                if changed != [configrepo.WORKFLOW]
                else ": this one also adds secrets to the workflow)."
                if wf.read_text() != bumped
                else ")."
            )
        )
        url = _publish(
            ctx,
            branch,
            f"mcctl upgrade: product {to}",
            f"Moves the product pin from {pin.version or pin.sha[:12]} to {to} ({sha}).\n\n"
            f"server.yml migrated and validated by the target release. {who}",
            [configrepo.WORKFLOW, configrepo.SERVER_YML],
            start,
        )
        if unattended:
            merge = ctx.gh("pr", "merge", url, "--auto", "--squash")
            if merge.returncode != 0:
                ui.warn(
                    f"couldn't turn on auto-merge ({(merge.stderr or '').strip()}): "
                    "merge it once the gate passed"
                )
    except (init.StepError, ProviderError):
        _abandon(ctx, start, branch)
        raise
    ui.ok(f"pull request: {url}")
    return 0


def schema_migrate(root: Path, old: str, product_root: Path | None = None) -> list[str]:
    """This product's migrations from `old` applied to root/server.yml (idempotent), and (v1.2) the secrets
    its template passes added to the config repo's workflow. Returns what changed."""
    from mcctl import wfsecrets

    path = root / configrepo.SERVER_YML
    text, applied = migrations.migrate(path.read_text(), None if old in ("", "0.0.0") else old)
    if applied:
        yamledit.get_value(text, "name")  # still parses
        path.write_text(text)
    wf = root / configrepo.WORKFLOW
    if wf.is_file():
        current = wf.read_text()
        try:
            absent = wfsecrets.missing(current, wfsecrets.template_text(product_root or schema.PRODUCT_ROOT))
        except wfsecrets.WorkflowError as e:
            raise init.StepError(str(e)) from None
        if absent:
            wf.write_text(wfsecrets.add(current, absent))
            applied.append(f"{configrepo.WORKFLOW}: passes {', '.join(absent)} (the product's template does)")
    return applied


# --- Minecraft -----------------------------------------------------------------------------------------


def mc_version(text: str) -> tuple[int, ...]:
    if not MC_VERSION.fullmatch(text):
        raise init.StepError(f"not a Minecraft release version: {text!r}")
    return tuple(int(p) for p in text.split("."))


def minecraft_upgrade(
    ctx: init.Ctx,
    version: str | None,
    *,
    dry_run: bool = False,
    allow_paper: bool = False,
    client: bool = False,
    locker: Locker = update_lock,
    today: datetime.date | None = None,
) -> int:
    ui = ctx.ui
    repo = ctx.repo()
    cfg = repo.server
    current = cfg.minecraft.version
    target = version or current
    changes_version = target != current
    new_client_lock = client and repo.client_lock is None  # v1.2: `lock update --client`
    if changes_version:
        if mc_version(target) < mc_version(current):
            raise init.StepError(f"{target} is older than {current}: a world can't be downgraded")
        if not (ctx.product_root / "server-config" / "keys" / target).is_dir():
            raise init.StepError(
                f"the pinned product has no config keys for Minecraft {target} "
                f"(server-config/keys/{target}/)",
                "upgrade the product first (`mcctl upgrade`); a product maintainer generates the keys with "
                "scripts/config_keys.py (static analysis of the jar)",
            )
    deployed = cfg.deploy.enabled and cfg.host.ipv4 is not None
    stamp = (today or datetime.datetime.now(datetime.UTC).date()).isoformat()
    branch = f"upgrade/minecraft-{target}" if changes_version else f"update/lock-{stamp}"
    title = (
        f"mcctl upgrade: Minecraft {current} -> {target}"
        if changes_version
        else f"mcctl lock update: Minecraft {current} builds"
    )
    plan = [
        *(
            [f"typed confirmation: Minecraft {current} -> {target} is your explicit choice"]
            if changes_version
            else []
        ),
        f"branch {branch}: scripts/update_lock.py --minecraft-version {target} "
        "(fails if any plugin has no release)"
        + (", and a first client.lock.yml (--client)" if new_client_lock else ""),
        *(
            [f"server.yml minecraft.version: {target}; validate the config repo"]
            if changes_version
            else ["validate"]
        ),
        "commit, push, pull request: CI boot-tests the new jar + plugins; you merge it (that deploys)",
        *(
            ["the deploy backs the world up right before the new jar starts (pre-upgrade snapshot)"]
            if changes_version and deployed
            else []
        ),
    ]
    ui.say(title)
    for n, line in enumerate(plan, 1):
        ui.say(f"  {n}. {line}")
    ui.say(
        "  rollback: close the PR; after the deploy, revert it and "
        "`mcctl restore --live <pre-upgrade snapshot>` (marked in `mcctl restore --list`)"
    )
    if dry_run:
        ui.say("(dry run: nothing was changed)")
        return 0
    if changes_version:
        typed = ui.typed("confirm_minecraft_version")(
            f"Type {target} to upgrade {cfg.name} to Minecraft {target}: "
        )
        if typed.strip() != target:
            ui.say("Aborted: nothing was changed.")
            return 1
    start = _clean(ctx)
    _branch(ctx, branch)
    try:
        proc = locker(ctx.product_root, ctx.root, target, allow_paper, client)
        for line in (proc.stdout or "").splitlines():
            ui.info(line)
        if proc.returncode != 0:
            raise init.StepError(
                f"update_lock.py refused: {(proc.stderr or proc.stdout).strip()[-500:]}",
                "a plugin without a release for this version blocks the upgrade: wait for it, or remove it "
                "from plugins.lock.yml (and server.yml plugins) in a separate change",
            )
        if changes_version:
            ctx.set({"minecraft.version": target})
        ctx.repo()
        if not ctx.git("status", "--porcelain").stdout.strip():
            _abandon(ctx, start, branch)
            ui.ok(f"the locks already pin the newest builds for Minecraft {target}: nothing to do")
            return 0
        body = "\n".join(
            [
                f"Minecraft {current} -> {target} (`mcctl upgrade --minecraft-version {target}`)."
                if changes_version
                else f"Newest builds for Minecraft {current} (`mcctl lock update`).",
                "",
                "Pre-upgrade snapshot: the deploy of this merge backs the world up right before the new jar "
                "starts (deploy log `PRE-UPGRADE SNAPSHOT <id>`, `/var/lib/minecraft/pre-upgrade.json`, "
                "marked in `mcctl restore --list`)."
                if changes_version
                else "Same Minecraft version: no world conversion.",
                "",
                "- every locked plugin has a release for this version (update_lock.py checked it)",
                *(
                    [
                        "- a first client.lock.yml (`--client`): a new client lock needs the admin's merge; "
                        "then `mcctl config set client.enabled true` and `mcctl apply`"
                    ]
                    if new_client_lock
                    else []
                ),
                "- `server / boot-test` boots the new jar + every plugin on this PR: merge after it passed",
                "- merging deploys it; the first start converts the world (no way back without the snapshot)",
                "",
                "Rollback after the deploy: revert this PR's merge commit **and** "
                "`mcctl restore --live <pre-upgrade snapshot>`.",
            ]
        )
        url = _publish(
            ctx,
            branch,
            title,
            body,
            [
                configrepo.SERVER_LOCK,
                configrepo.PLUGINS_LOCK,
                configrepo.SERVER_YML,
                # v1.2: the client lock too (v1.1 refreshed it on disk but left it out of the commit)
                *([configrepo.CLIENT_LOCK] if (ctx.root / configrepo.CLIENT_LOCK).is_file() else []),
            ],
            start,
        )
    except init.StepError:
        _abandon(ctx, start, branch)
        raise
    ctx.state.record(
        "upgrade-minecraft",
        "done",
        inputs={"target": target},
        outputs={"from": current, "to": target, "pr": url},
    )
    ui.ok(f"pull request: {url}")
    return 0
