"""`mcctl restore --list | --to DIR [--snapshot ID] | --live SNAPSHOT` (design §4; P10 units, P12 CLI).

- `--list`: restic's snapshots (newest last) over Tailscale SSH.
- `--to DIR`: restore a snapshot (default: the latest) into a scratch directory **on the server** and verify
  it (`restore.py --verify-into` for the latest; `restic restore --verify` for a given id). Never live.
- `--live SNAPSHOT`: the panel's restore, `mc-restore@<id>.service` (as the ops user: maintenance window ->
  **safety snapshot** (tag pre-restore) -> stop -> fetch -> mc-restore-apply.service as minecraft -> start),
  started with `sudo systemctl` by the deploy user, so no new polkit rule. It takes the same typed
  confirmation as the panel (`restore <first 8 hex>`; `--confirm TEXT` gives it up front: scripts, the
  dashboard), shows who is online first, and prints the unit's STEP lines and the safety snapshot to undo
  it with.
"""

from __future__ import annotations

from mcctl import init, remote, serverops


def _run(ctx: init.Ctx, script: str, what: str) -> str:
    cfg = ctx.repo().server
    with ctx.ui.spinner(f"{what} (over the tailnet)"):
        proc = ctx.deps.run(remote.ssh_argv(cfg, script, host=init.tailnet_target(ctx, cfg)))
    if proc.returncode != 0:
        raise init.StepError(f"{what} failed: {(proc.stderr or proc.stdout or '').strip()[-600:]}")
    return proc.stdout


def snapshots(ctx: init.Ctx) -> list[dict[str, object]]:
    return serverops.parse_snapshots(_run(ctx, serverops.latest_snapshot_script(), "listing snapshots"))


def list_snapshots(ctx: init.Ctx) -> int:
    out = _run(
        ctx, serverops.latest_snapshot_script() + "\n" + serverops.pre_upgrade_script(), "listing snapshots"
    )
    snaps = serverops.parse_snapshots(out)
    pre = serverops.parse_pre_upgrade(out)
    pre_id = str((pre or {}).get("snapshot_id") or "")
    if not snaps:
        ctx.ui.say("no snapshots")
    if ctx.ui.rich and snaps:
        rows = []
        for s in snaps:
            tags = ",".join(str(t) for t in s.get("tags") or []) if isinstance(s.get("tags"), list) else ""
            note = (
                "warn: pre-upgrade (the last Minecraft upgrade's rollback point)"
                if str(s["id"]) == pre_id
                else ""
            )
            rows.append([str(s.get("short_id") or str(s["id"])[:8]), str(s.get("time")), tags, note])
        ctx.ui.table(["Snapshot", "Time", "Tags", ""], rows, title="Snapshots (newest last)", indent=2)
        snaps = []
    for s in snaps:
        tags = ",".join(str(t) for t in s.get("tags") or []) if isinstance(s.get("tags"), list) else ""
        mark = (
            "  <- pre-upgrade (the last Minecraft upgrade's rollback point)" if str(s["id"]) == pre_id else ""
        )
        ctx.ui.say(f"{s.get('short_id') or str(s['id'])[:8]!s}  {s.get('time')}  {tags}{mark}")
    if pre:
        ctx.ui.info(
            f"pre-upgrade snapshot {pre_id[:8]}: taken by the deploy of Minecraft {pre.get('from')} -> "
            f"{pre.get('to')} right before the new jar started"
        )
    return 0


def to_dir(ctx: init.Ctx, target: str, snapshot: str | None) -> int:
    try:
        script = serverops.restore_to_script(snapshot, target)
    except ValueError as e:
        ctx.ui.bad(str(e))
        return 2
    ctx.ui.say(_run(ctx, script, "restore into a scratch directory").rstrip())
    ctx.ui.ok(f"restored into {target} on the server (the live world is untouched); remove it when done")
    return 0


def live(ctx: init.Ctx, snapshot: str, *, dry_run: bool = False, confirm: str | None = None) -> int:
    """`confirm`: the typed confirmation given up front (`--confirm`, the dashboard); None: asked."""
    ui = ctx.ui
    try:
        serverops.check_snapshot(snapshot)
    except ValueError as e:
        ui.bad(str(e))
        return 2
    match = [s for s in snapshots(ctx) if str(s["id"]).startswith(snapshot)]
    if len(match) != 1:
        ui.bad(f"snapshot {snapshot}: {'not found' if not match else 'ambiguous'} (`mcctl restore --list`)")
        return 1
    snap = match[0]
    sid = str(snap["id"])
    online = _run(ctx, "mc list 2>&1 || true", "asking who is online").strip()
    steps = [
        "maintenance window (the watchdog stays quiet)",
        "safety snapshot of the current world (tag pre-restore): the undo",
        "stop the server -> fetch it as the ops user -> mc-restore-apply.service copies it in as minecraft",
        "start the server (unless it was admin-stopped before)",
    ]
    if ui.rich:
        ui.todo(steps, title=f"mcctl restore --live {sid[:8]} ({snap.get('time')}): what happens")
        ui.note(f"now: {online or 'server not answering'}")
    else:
        ui.say(f"mcctl restore --live {sid[:8]} ({snap.get('time')}):")
        for n, line in enumerate(steps, 1):
            ui.say(f"  {n}. {line}")
        ui.say(f"  now: {online or 'server not answering'}")
    if dry_run:
        ui.say("(dry run: nothing was changed)")
        return 0
    want = f"restore {sid[:8]}"
    if confirm is None:
        confirm = ui.typed("confirm_restore")(f"Type `{want}` to replace the live world: ")
    typed = confirm.strip()
    if typed != want:
        ui.say("Aborted: nothing was changed.")
        return 1
    out = _run(ctx, serverops.restore_live_script(sid[:12]), "the live restore")
    parts = remote.sections(out)
    for line in parts.get("journal", "").splitlines():
        if line.startswith(("STEP", "RESTORED", "safety")) or "snapshot" in line:
            ui.info(line)
    result = parts.get("result", "").strip()
    if result != "success":
        ui.bad(
            f"mc-restore@{sid[:12]} ended with {result or 'no result'}: `mcctl logs mc-restore@{sid[:12]}`"
        )
        return 1
    ui.ok(f"restored {sid[:8]}; the pre-restore safety snapshot is the undo (`mcctl restore --list`)")
    return 0
