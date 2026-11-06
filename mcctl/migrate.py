"""`mcctl migrate --to IPV4 [--ipv6 IPV6]`, `migrate --rollback` and `mcctl retire-old` (design §4, §8 P12).

Moves the server to a new machine (another VPS or another provider) without losing a block:

  1. preflight   the old server answers over the tailnet; the new host's SSH key is compared with you, then it
                 is probed as root (OS, arch, RAM, the IPv6 really configured), like `mcctl init` step 9
  2. fence       on the old server: maintenance window, timers + mc-agent disabled, admin-stopped and not
                 started at boot, then a final backup; its snapshot id is recorded
  3. addresses   server.yml host.ipv4/ipv6 = the new addresses (committed locally, pushed in step 6)
  4. rename      the old tailnet node becomes `<name>-old` (Tailscale API, one-off API access token), so the
                 new machine can take `<name>`
  5. bootstrap   `apply -replace=tailscale_tailnet_key.server` (the DNS cutover and a fresh key, typed
                 confirmation) -> bootstrap.yml on the new host (mcctl bootstrap)
  6. deploy      push -> the CI deploy; on the empty box it restores the latest snapshot (= the fenced one)
  7. verify      the new server's `.restored-from` names the recorded snapshot
  8. smoke       Minecraft status ping on v4/v6 and the watchdog (as `mcctl init` step 12)

Each step is recorded in `.mcctl/state.json` (`migrate`: target, done steps, snapshot id, old device id; no
secrets), so a re-run resumes where it stopped. `--dry-run` prints the exact sequence and changes nothing.
The old box stays fenced, intact and on the tailnet as `<name>-old` until `mcctl retire-old` (typed
confirmation) deletes its tailnet node and reminds you to cancel it at the provider.

Rollback (`--rollback`, any time before retire-old), each sub-step recorded so a re-run resumes
(ROLLBACK_STEPS): once the new box was bootstrapped it is fenced first (its final backup becomes the latest
snapshot; unreachable only with a typed "it is off"); the new tailnet node is deleted, the old one renamed
back, host.* restored (DNS back with a typed apply), the snapshot to restore picked (the latest, if it isn't
the old box's final one), the old box unfenced but kept stopped, that snapshot restored live, and only then
the old box started; the revert is pushed iff origin/main has the address commit. Every effect goes through
init.Deps (mcctl/mock.py fakes them).
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from mcctl import bootstrap as bootstrap_mod
from mcctl import configrepo, deploy, init, remote, schema, serverops, state, tf
from mcctl.providers import ProviderError, tailscale

STATE_KEY = "migrate"
RETIRED_KEY = "retire-old"


@dataclass(frozen=True)
class Step:
    id: str
    text: str


STEPS = (
    Step("preflight", "check the old server over the tailnet; compare and probe the new host as root"),
    Step(
        "fence",
        "old server: maintenance window, timers + agent off, admin-stopped, final backup (id recorded)",
    ),
    Step("addresses", "server.yml host.ipv4/ipv6 = the new addresses (local commit)"),
    Step("rename", "Tailscale API: rename the old tailnet node {name} -> {name}-old"),
    Step(
        "bootstrap",
        "terraform apply -replace=tailscale_tailnet_key.server (DNS cutover; typed) -> bootstrap.yml",
    ),
    Step("deploy", "push -> CI deploy to {name}; the empty box restores the latest snapshot"),
    Step("verify", "new server's .restored-from == the recorded snapshot"),
    Step("smoke", "status ping IPv4/IPv6 + watchdog"),
)


class MigrateError(init.StepError):
    pass


def _old_name(name: str) -> str:
    return f"{name}-old"


def _ssh(ctx: init.Ctx, script: str, node: str, what: str, stdin: str | None = None) -> str:
    """Run a script on tailnet node `node` (its 100.x address from this laptop's `tailscale status`)."""
    cfg = ctx.repo().server
    host = bootstrap_mod.tailnet_address(node, ctx.deps.run)
    if host is None:
        raise MigrateError(f"this laptop's tailnet has no node {node}", "`tailscale status`; is it online?")
    argv = remote.ssh_argv(cfg, script, host=host)
    proc = ctx.deps.run(argv) if stdin is None else ctx.deps.run_input(argv, stdin)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-400:]
        raise MigrateError(
            f"{what} on {node} failed: {tail}", f"`mcctl ssh -C {ctx.root}` to look, then re-run"
        )
    return proc.stdout


def _api_key(ctx: init.Ctx) -> str:
    ctx.ui.info(
        "The tailnet nodes are renamed/deleted with a one-off Tailscale API access token (Terraform's OAuth\n"
        "client can only read devices). Create one with a 1 day expiry: admin console > Settings > Keys >\n"
        "Generate access token. It is used for this run only, never stored; revoke it when done."
    )

    def check(value: str) -> str | None:
        result = tailscale.check_api_key(value, ctx.deps.transport)
        return "; ".join(result.messages) if result.level == "fail" else None

    return ctx.ui.secret(
        "tailscale_api_key", "Tailscale API access token", env_name="TAILSCALE_API_KEY", check=check
    )


def plan_lines(name: str, to4: str, to6: str | None, old4: str | None, done: list[str]) -> list[str]:
    lines = [f"mcctl migrate: {old4 or '?'} -> {to4}" + (f" / {to6}" if to6 else "")]
    for n, step in enumerate(STEPS, 1):
        mark = "done" if step.id in done else "    "
        lines.append(f"  [{mark}] {n}. {step.id:<9} {step.text.format(name=name)}")
    lines += [
        "  afterwards: the old box stays fenced as "
        f"{_old_name(name)}; `mcctl retire-old` deletes its tailnet node (provider cancellation is yours)",
        "  rollback (until retire-old): `mcctl migrate --rollback`",
    ]
    return lines


def _check_target(ipv4: str, ipv6: str | None) -> tuple[str, str | None]:
    errors = schema.field_errors("host.ipv4", ipv4) + (schema.field_errors("host.ipv6", ipv6) if ipv6 else [])
    if errors:
        raise MigrateError("; ".join(errors))
    return ipv4, (str(ipaddress.IPv6Address(ipv6)) if ipv6 else None)


@dataclass
class Migration:
    ctx: init.Ctx
    record: dict[str, Any]

    @property
    def done(self) -> list[str]:
        return list(self.record.setdefault("done", []))

    def mark(self, step: str, **values: Any) -> None:  # noqa: ANN401
        self.record.update(values)
        if step not in self.record.setdefault("done", []):
            self.record["done"].append(step)
        self.save("waiting")

    def save(self, status: state.Status) -> None:
        inputs = {"to_ipv4": self.record["to_ipv4"], "to_ipv6": self.record.get("to_ipv6")}
        self.ctx.state.record(STATE_KEY, status, inputs=inputs, outputs=self.record)


def _load(ctx: init.Ctx) -> dict[str, Any] | None:
    rec = ctx.state.get(STATE_KEY)
    if rec is None or rec.get("status") not in ("waiting", "failed", "done"):
        return None
    return dict(rec.get("outputs") or {})


def run(ctx: init.Ctx, to4: str, to6: str | None, *, dry_run: bool = False) -> int:
    ui = ctx.ui
    cfg = ctx.repo().server
    to4, to6 = _check_target(to4, to6)
    rec = _load(ctx)
    if rec and rec.get("finished") and not rec.get("retired"):
        if rec.get("to_ipv4") == to4:
            ui.ok(f"already migrated to {to4}; `mcctl retire-old` when you're happy (or `--rollback`)")
            return 0
        raise MigrateError(
            f"a migration to {rec.get('to_ipv4')} finished and its old box isn't retired yet",
            "`mcctl retire-old` (or `mcctl migrate --rollback`) first",
        )
    if rec and not rec.get("retired") and (rec.get("rollback") or {}).get("done"):
        raise MigrateError(
            "a rollback of this migration is in progress",
            f"finish it: `mcctl migrate -C {ctx.root} --rollback`",
        )
    if rec and not rec.get("finished") and rec.get("to_ipv4") != to4:
        raise MigrateError(
            f"a migration to {rec.get('to_ipv4')} is in progress", "finish it, or `mcctl migrate --rollback`"
        )
    if not rec or rec.get("retired"):
        if cfg.host.ipv4 is None or not cfg.deploy.enabled:
            raise MigrateError(
                "nothing to migrate: no deployed server (host.ipv4 / deploy.enabled)", "use `mcctl init`"
            )
        if cfg.host.ipv4 == to4:
            raise MigrateError(f"{to4} is already host.ipv4", "give the new machine's address")
        rec = {
            "to_ipv4": to4,
            "to_ipv6": to6,
            "old_ipv4": cfg.host.ipv4,
            "old_ipv6": cfg.host.ipv6,
            "name": cfg.host.tailscale_name,
            "done": [],
        }
    for line in plan_lines(rec["name"], to4, to6, rec["old_ipv4"], rec.get("done", [])):
        ui.say(line)
    if dry_run:
        ui.say("(dry run: nothing was changed)")
        return 0
    if not rec.get("done") and not ui.confirm(
        "migrate_start", f"Fence {rec['name']} ({rec['old_ipv4']}) and move it to {to4}?", default=False
    ):
        return 1
    m = Migration(ctx, rec)
    steps: dict[str, Callable[[Migration], None]] = {
        "preflight": _preflight,
        "fence": _fence,
        "addresses": _addresses,
        "rename": _rename,
        "bootstrap": _bootstrap,
        "deploy": _deploy,
        "verify": _verify,
        "smoke": _smoke,
    }
    for n, step in enumerate(STEPS, 1):
        if step.id in m.done:
            continue
        done = len(m.done) if ui.rich else None
        ui.header(n, len(STEPS), step.text.format(name=rec["name"]), done=done, steps=len(STEPS))
        try:
            steps[step.id](m)
        except (init.StepError, ProviderError, deploy.DeployError, tf.TfError) as e:
            m.save("failed")
            again = f"`mcctl migrate -C {ctx.root} --to {to4}` to continue, or `--rollback`."
            if ui.rich:
                ui.fail(str(e), getattr(e, "fix", ""), f"re-run {again}")
                return 1
            ui.bad(str(e))
            if fix := getattr(e, "fix", ""):
                ui.info(f"fix: {fix}")
            ui.info(f"Re-run {again}")
            return 1
    m.record["finished"] = True
    m.save("done")
    lines = [
        f"Migrated: {rec['name']} now runs on {to4}. The old box ({rec['old_ipv4']}) stays fenced as "
        f"{_old_name(rec['name'])} (stopped, timers and agent off, intact).",
        f"When you're happy: `mcctl retire-old -C {ctx.root}`, then cancel the old machine at its provider.",
        f"Changed your mind: `mcctl migrate -C {ctx.root} --rollback`.",
    ]
    if ui.rich:
        ui.banner("Migrated", lines)
    else:
        ui.say("\n" + "\n".join(lines))
    return 0


# --- steps ---------------------------------------------------------------------------------------------


def _preflight(m: Migration) -> None:
    ctx, rec = m.ctx, m.record
    ok, detail = bootstrap_mod.ssh_works(ctx.repo(), ctx.deps.run)
    if not ok:
        raise MigrateError(detail, "the old server must answer over the tailnet to be fenced")
    ctx.ui.ok(detail)
    probe_new_host(ctx, rec["to_ipv4"], rec.get("to_ipv6"))
    m.mark("preflight")


def probe_new_host(ctx: init.Ctx, ipv4: str, ipv6: str | None) -> None:
    """init step 9 against an address that isn't in server.yml yet: host key compared, root probe."""
    cfg = ctx.repo().server
    init._host_key(ctx, ipv4)
    user = cfg.host.ssh_bootstrap_user
    probe = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "StrictHostKeyChecking=yes",
        f"{user}@{ipv4}",
        "--",
        init.PROBE_SCRIPT,
    ]
    proc = ctx.deps.run(probe)
    if proc.returncode != 0:
        raise MigrateError(
            f"ssh {user}@{ipv4} failed: {(proc.stderr or '').strip()[-200:] or proc.returncode}",
            f"add your SSH public key for {user} at the new provider, then re-run",
        )
    if problems := init.check_host(proc.stdout, ipv6):
        raise MigrateError("the new host isn't suitable:\n  - " + "\n  - ".join(problems))
    ctx.ui.ok(f"{user}@{ipv4}: supported OS, architecture and RAM" + (f"; {ipv6} configured" if ipv6 else ""))


def _fence(m: Migration) -> None:
    ctx, rec = m.ctx, m.record
    out = _ssh(ctx, serverops.fence_script(f"migrating to {rec['to_ipv4']}"), rec["name"], "fencing")
    snap = serverops.latest(out)
    if snap is None:
        raise MigrateError(
            "the final backup left no snapshot to restore", "`mcctl logs mc-backup` on the old box"
        )
    short = str(snap.get("short_id") or str(snap["id"])[:8])
    ctx.ui.ok(f"old server fenced; final snapshot {short} ({snap.get('time')})")
    m.mark("fence", snapshot_id=str(snap["id"]), snapshot_time=str(snap.get("time", "")))


def _addresses(m: Migration) -> None:
    ctx, rec = m.ctx, m.record
    values: dict[str, Any] = {"host.ipv4": rec["to_ipv4"], "host.ipv6": rec.get("to_ipv6")}
    ctx.set(values)
    try:
        ctx.repo()
    except init.StepError:
        ctx.set({"host.ipv4": rec["old_ipv4"], "host.ipv6": rec.get("old_ipv6")})
        raise
    sha = ctx.commit(f"mcctl migrate: host addresses {rec['to_ipv4']}", [configrepo.SERVER_YML], push=False)
    m.mark("addresses", addresses_commit=sha)


def _rename(m: Migration) -> None:
    ctx, rec = m.ctx, m.record
    key = _api_key(ctx)
    old = tailscale.find_device(key, rec["name"], ctx.deps.transport)
    if old is None:
        renamed = tailscale.find_device(key, _old_name(rec["name"]), ctx.deps.transport)
        if renamed is None:
            raise MigrateError(f"no tailnet device named {rec['name']}", "check the admin console (Machines)")
        old = renamed
    else:
        tailscale.rename_device(key, str(old["id"]), _old_name(rec["name"]), ctx.deps.transport)
    ctx.ui.ok(f"tailnet node {old['id']} is now {_old_name(rec['name'])}")
    m.mark("rename", old_device_id=str(old["id"]))


def _bootstrap(m: Migration) -> None:
    ctx = m.ctx
    rc = ctx.deps.bootstrap(ctx.repo(), ctx.product_root, ctx.ui.typed("confirm_apply"))
    if rc != 0:
        raise MigrateError(
            "bootstrap of the new host didn't finish (see above)", "fix what it reports, re-run"
        )
    m.mark("bootstrap")


def _deploy(m: Migration) -> None:
    out = init.step_deploy(m.ctx)  # pushes the address commit; waits for that run
    m.mark("deploy", deploy_run=out.get("run_id"))


def _verify(m: Migration) -> None:
    ctx, rec = m.ctx, m.record
    marker = serverops.parse_restored_from(
        _ssh(ctx, serverops.restored_from_script(), rec["name"], "reading .restored-from")
    )
    got = (marker or {}).get("snapshot_id")
    if got != rec["snapshot_id"]:
        raise MigrateError(
            f"the new server restored {got or 'nothing'}, not the fenced snapshot {rec['snapshot_id'][:8]}",
            "another box wrote a newer snapshot after the fence, or the restore picked an older one: compare "
            "`mcctl restore --list` (times, hosts) before anything else; never restore the older fenced "
            "snapshot over newer play. Unsure: `mcctl migrate --rollback`",
        )
    ctx.ui.ok(f"the new server restored snapshot {got[:8]} (the old server's final backup)")
    m.mark("verify")


def _smoke(m: Migration) -> None:
    init.step_smoke(m.ctx)
    m.mark("smoke")


# --- rollback, retire ----------------------------------------------------------------------------------


ROLLBACK_STEPS = (
    "fence-new",  # bootstrap done: the new box may run Minecraft and back up to the same repository
    "delete-new",  # the new tailnet node `<name>` (not the old device)
    "rename-back",  # `<name>-old` -> `<name>`
    "revert-addresses",  # server.yml host.* back (local commit)
    "dns",  # bootstrap done: the typed apply puts DNS back
    "pick",  # the snapshot the old box restores: the repository's latest, if it isn't the old box's final one
    "unfence-old",  # marker removed, enabled at boot; still admin-stopped, timers and agent off
    "restore-old",  # the picked snapshot restored live (safety snapshot first) BEFORE anything starts
    "resume-old",  # timers + agent on, server started, maintenance closed
    "push",  # the revert pushed iff origin/main contains the address commit
)


def _rollback_plan(rec: dict[str, Any]) -> list[tuple[str, str]]:
    done, name = rec["done"], rec["name"]
    plan: list[tuple[str, str]] = []
    if "bootstrap" in done:
        plan.append(
            ("fence-new", f"fence the new server {name} (its final backup becomes the latest snapshot)")
        )
    if "addresses" in done:
        plan.append(("delete-new", f"delete the new tailnet node {name}, if there is one"))
        plan.append(("rename-back", f"rename {_old_name(name)} back to {name}"))
        plan.append(("revert-addresses", f"server.yml host.* back to {rec['old_ipv4']} (local commit)"))
        # also when the bootstrap step failed after its apply: an empty plan applies nothing
        plan.append(
            ("dns", "terraform plan (typed apply if it changes anything): DNS back to the old server")
        )
    if "fence" in done:
        plan.append(
            ("pick", "pick the snapshot to restore: the latest, if it isn't the old server's final one")
        )
        plan.append(("unfence-old", f"unfence the old server ({rec['old_ipv4']}); it stays stopped"))
        plan.append(
            ("restore-old", "restore the picked snapshot live on the old server (if any), before it starts")
        )
        plan.append(("resume-old", "old server: timers + agent on, Minecraft started"))
    if "addresses" in done:
        plan.append(("push", "push the revert, if main already has the address commit"))
    return plan


def _on_origin_main(ctx: init.Ctx, sha: str) -> bool:
    """Whether the config repo's origin/main contains `sha` (fetched now; never guessed)."""
    if ctx.git("fetch", "-q", "origin", "main", check=False).returncode != 0:
        raise MigrateError(
            "couldn't fetch origin/main to tell whether the address commit was pushed",
            "check `gh auth status` / the network, then re-run `mcctl migrate --rollback`",
        )
    return ctx.git("merge-base", "--is-ancestor", sha, "FETCH_HEAD", check=False).returncode == 0


def rollback(ctx: init.Ctx, *, dry_run: bool = False) -> int:
    """Undo a migration. Each sub-step is recorded (`migrate.rollback.done` in state.json) as it finishes, so
    a re-run after a failure continues where it stopped and never repeats one that would now hit the wrong box
    (e.g. fencing `<name>` again after it was renamed back to the old box)."""
    ui = ctx.ui
    rec = _load(ctx)
    if not rec or rec.get("retired") or not rec.get("done"):
        ui.say("No migration to roll back.")
        return 0
    name = rec["name"]
    rb: dict[str, Any] = rec.setdefault("rollback", {"done": []})
    plan = _rollback_plan(rec)
    ui.say("mcctl migrate --rollback:")
    for n, (sid, line) in enumerate(plan, 1):
        ui.say(f"  [{'done' if sid in rb['done'] else '    '}] {n}. {line}")
    if dry_run:
        ui.say("(dry run: nothing was changed)")
        return 0
    if not rb["done"] and not ui.confirm("rollback_start", "Roll the migration back?", default=False):
        return 1

    def save(sid: str | None = None, **values: Any) -> None:  # noqa: ANN401
        rb.update(values)
        if sid is not None and sid not in rb["done"]:
            rb["done"].append(sid)
        ctx.state.record(STATE_KEY, "failed", outputs=rec)

    key = ""

    def api_key() -> str:
        nonlocal key
        key = key or _api_key(ctx)
        return key

    try:
        for sid, _line in plan:
            if sid in rb["done"]:
                continue
            if sid == "fence-new":
                _fence_new(ctx, rec, save)
            elif sid == "delete-new":
                new = tailscale.find_device(api_key(), name, ctx.deps.transport)
                if new is not None and str(new["id"]) != rec.get("old_device_id"):
                    tailscale.delete_device(api_key(), str(new["id"]), ctx.deps.transport)
                    ui.ok(f"deleted the new tailnet node {new['id']}")
            elif sid == "rename-back":
                old_id = rec.get("old_device_id")
                if not old_id:  # the rename may have happened without being recorded
                    dev = tailscale.find_device(api_key(), _old_name(name), ctx.deps.transport)
                    old_id = str(dev["id"]) if dev else None
                if old_id:
                    tailscale.rename_device(api_key(), old_id, name, ctx.deps.transport)
                    ui.ok(f"tailnet node {old_id} is {name} again")
            elif sid == "revert-addresses":
                ctx.set({"host.ipv4": rec["old_ipv4"], "host.ipv6": rec.get("old_ipv6")})
                ctx.commit(
                    "mcctl migrate --rollback: host addresses back", [configrepo.SERVER_YML], push=False
                )
            elif sid == "dns":
                init._plan_apply(ctx)
            elif sid == "pick":
                snap = serverops.latest(
                    _ssh(ctx, serverops.latest_snapshot_script(), name, "listing snapshots")
                )
                latest = str(snap["id"]) if snap else None
                target = latest if latest and latest != rec.get("snapshot_id") else None
                ui.ok(
                    f"the old server will restore {target[:8]} (newer than its final backup)"
                    if target
                    else "no snapshot newer than the old server's final backup: its world is current"
                )
                save(sid, restore=target)
                continue
            elif sid == "unfence-old":
                _ssh(ctx, serverops.unfence_script(), name, "unfencing the old server")
                ui.ok("old server unfenced (still stopped)")
            elif sid == "restore-old":
                if rb.get("restore"):
                    ui.info(f"restoring {rb['restore'][:8]} on the old server before it starts ...")
                    _ssh(ctx, shell_admin_stop("restore after a migration rollback"), name, "admin stop")
                    _ssh(ctx, serverops.restore_live_script(rb["restore"][:12]), name, "live restore")
            elif sid == "resume-old":
                _ssh(ctx, serverops.resume_script(), name, "starting the old server")
                ui.ok("old server running again (timers and agent on)")
            elif sid == "push":
                sha = rec.get("addresses_commit")
                if sha and _on_origin_main(ctx, sha):
                    ctx.commit("mcctl migrate --rollback: host addresses back", [configrepo.SERVER_YML])
                else:
                    ui.ok("main never had the address commit: nothing to push")
            save(sid)
    except (init.StepError, ProviderError, tf.TfError) as e:
        save()
        ui.bad(str(e))
        if fix := getattr(e, "fix", ""):
            ui.info(f"fix: {fix}")
        ui.info("The rollback stopped halfway: fix it and run `mcctl migrate --rollback` again (it resumes).")
        return 1
    ctx.state.record(STATE_KEY, "done", outputs=rec | {"rolled_back": True, "retired": True})
    if "bootstrap" in rec["done"]:
        ui.info(
            f"Reinstall or cancel the new machine ({rec['to_ipv4']}): it is fenced and off the tailnet now."
        )
    ui.ok(f"rolled back: {name} runs on {rec['old_ipv4']} again")
    return 0


def _fence_new(ctx: init.Ctx, rec: dict[str, Any], save: Callable[..., None]) -> None:
    """Fence the new box (bootstrap ran, so a deploy may have started Minecraft there and its backups go to
    the same repository). Unreachable: only a typed statement that it is off lets the rollback go on."""
    name = rec["name"]
    try:
        _ssh(ctx, serverops.fence_script("migration rolled back"), name, "fencing the new server")
        ctx.ui.ok("new server fenced (final backup taken)")
        save("fence-new", new_fenced=True)
    except MigrateError as e:
        ctx.ui.bad(str(e))
        want = f"{rec['to_ipv4']} is off"
        ctx.ui.warn(
            "The new server can't be fenced. If Minecraft ever ran there, it keeps backing up to the same\n"
            "restic repository (split brain) and whatever was played on it since its last backup is lost.\n"
            "Power it off or wipe it at the provider first."
        )
        typed = ctx.ui.typed("confirm_new_unreachable")(f"Type `{want}` once it is: ").strip()
        if typed != want:
            raise MigrateError(
                "the new server isn't fenced", "fix its reachability, or power it off and re-run"
            ) from e
        save("fence-new", new_fenced=False)


def shell_admin_stop(reason: str) -> str:
    return remote.admin_command("stop", reason)


def retire_old(ctx: init.Ctx, *, dry_run: bool = False) -> int:
    ui = ctx.ui
    rec = _load(ctx)
    if not rec or not rec.get("finished") or rec.get("retired"):
        ui.say("Nothing to retire: no finished migration with an old box left (`mcctl migrate` first).")
        return 0
    if (rec.get("rollback") or {}).get("done"):
        ui.bad("a rollback is in progress: the old box is the live one again (`mcctl migrate --rollback`)")
        return 1
    old = _old_name(rec["name"])
    ui.say(
        f"mcctl retire-old: delete tailnet node {old} ({rec.get('old_device_id')}), "
        f"old IPv4 {rec['old_ipv4']}"
    )
    if dry_run:
        ui.say("(dry run: nothing was changed)")
        return 0
    typed = ctx.ui.typed("confirm_retire")(f"Type {old} to delete the old box's tailnet node: ").strip()
    if typed != old:
        ui.say("Aborted: nothing was deleted.")
        return 1
    try:
        key = _api_key(ctx)
        if rec.get("old_device_id"):
            tailscale.delete_device(key, rec["old_device_id"], ctx.deps.transport)
    except (ProviderError, init.StepError) as e:
        ui.bad(str(e))
        return 1
    ctx.state.record(STATE_KEY, "done", outputs=rec | {"retired": True})
    ctx.state.record(
        RETIRED_KEY, "done", outputs={"old_ipv4": rec["old_ipv4"], "device_id": rec.get("old_device_id")}
    )
    ui.ok(f"deleted tailnet node {old}")
    ui.info(
        f"Now cancel the old machine ({rec['old_ipv4']}) at its provider; its disk still holds the world "
        f"as of snapshot {rec.get('snapshot_id', '?')[:8]} and the server's env files: wipe/reinstall it\n"
        "before cancelling if the provider doesn't. Revoke the Tailscale API access token you used\n"
        "(admin console > Settings > Keys)."
    )
    return 0


def context(root: Path, ui: init.UI, deps: init.Deps, product_root: Path) -> init.Ctx:
    return init.Ctx(root=root.resolve(), ui=ui, deps=deps, opts=init.Options(), product_root=product_root)
