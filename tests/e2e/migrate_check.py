"""P12 acceptance on the e2e-mock runner: `mcctl migrate`'s server side, container -> container, plus the
`mcctl restore` wrapper. Runs on the runner (PyYAML + this checkout), driving the containers with `docker exec`
as root instead of Tailscale SSH as the deploy user (no tailnet here); the scripts are mcctl's own
(`mcctl/serverops.py`), byte for byte what `mcctl migrate` / `mcctl restore` send.

    python tests/e2e/migrate_check.py old mc-e2e STATE.json
        fence the old box (maintenance window, timers + mc-agent disabled, admin-stopped, not enabled at boot,
        the persistent fenced marker), prove it, prove a deploy fails and nothing starts it (P12 review), unfence
        it (still stopped) and resume it (the rollback helpers) and prove the server is back, fence it again; the
        final backup's snapshot id goes to STATE.json
    python tests/e2e/migrate_check.py new mc-e2e-new STATE.json
        on the fresh box after site.yml (which restored the latest snapshot because world.expected is true):
        `.restored-from` names the old box's final snapshot, the server answers; then the restore wrapper:
        `--to` a scratch dir (verified), and `--live` that snapshot through mc-restore@ (safety snapshot first)
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from deploy_check import deploy

from mcctl import mcping, remote, serverops

failures: list[str] = []


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def sh(box: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", box, "bash", "-c", script], capture_output=True, text=True, check=False
    )


def state(box: str, unit: str, what: str) -> str:
    return sh(box, f"systemctl {what} {unit}").stdout.strip()


def server_up(timeout: float = 600) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            mcping.status("127.0.0.1", 25565, timeout=5)
            return True
        except (OSError, ValueError):
            time.sleep(5)
    return False


def snapshots(box: str) -> list[dict[str, object]]:
    return serverops.parse_snapshots(sh(box, serverops.latest_snapshot_script()).stdout)


def fence(box: str, before: set[str]) -> str | None:
    proc = sh(box, serverops.fence_script("e2e migration"))
    expect(proc.returncode == 0, f"the fence script ran ({proc.stderr.strip()[-300:]})")
    snap = serverops.latest(proc.stdout)
    expect(
        snap is not None and str(snap["id"]) not in before,
        f"the fence made a final backup ({snap and snap['id']})",
    )
    expect(state(box, "minecraft.service", "is-active") != "active", "fenced: the server is stopped")
    expect(state(box, "minecraft.service", "is-enabled") == "disabled", "fenced: not started at boot")
    for unit in serverops.FENCE_UNITS:
        expect(state(box, unit, "is-active") != "active", f"fenced: {unit} is off")
        expect(state(box, unit, "is-enabled") == "disabled", f"fenced: {unit} is disabled")
    expect(
        sh(box, "test -e /var/lib/minecraft/admin-stopped").returncode == 0, "fenced: admin-stopped marker"
    )
    expect(sh(box, f"test -e {serverops.FENCED}").returncode == 0, "fenced: the persistent fenced marker")
    return str(snap["id"]) if snap else None


def nothing_starts(box: str) -> None:
    """P12 review (M1): on a fenced box a deploy fails before changing anything, and nothing starts the server or
    its helpers (admin.py start, a stray systemctl start, a re-run of the fence)."""
    proc = deploy(box, "minecraft,backups,agent,monitoring,autoreboot")
    expect(proc.returncode != 0 and "FENCED:" in proc.stdout, "a deploy to the fenced box fails loudly")
    expect(
        "Configure the Minecraft server" in proc.stdout and "TASK [base" not in proc.stdout, "before any role"
    )
    expect(sh(box, "/opt/minecraft/bin/admin.py start").returncode == 3, "admin.py start refuses (fenced)")
    sh(box, "systemctl start minecraft.service mc-backup.service mc-agent.service || true")
    expect(state(box, "minecraft.service", "is-active") != "active", "fenced: systemctl start leaves it down")
    for unit in serverops.FENCE_UNITS:
        expect(
            state(box, unit, "is-enabled") == "disabled", f"fenced after the deploy: {unit} still disabled"
        )
    before = snapshots(box)
    proc = sh(box, serverops.fence_script("e2e migration (again)"))
    expect(proc.returncode == 0 and snapshots(box) == before, "a second fence takes no stale new snapshot")


def old(box: str, out: Path) -> None:
    first = fence(box, {str(s["id"]) for s in snapshots(box)})
    nothing_starts(box)
    proc = sh(box, serverops.unfence_script())
    expect(proc.returncode == 0, f"the unfence script (rollback) ran ({proc.stderr.strip()[-300:]})")
    expect(state(box, "minecraft.service", "is-enabled") == "enabled", "unfenced: enabled at boot again")
    expect(
        state(box, "minecraft.service", "is-active") != "active", "unfenced: still stopped (restore first)"
    )
    expect(
        state(box, "mc-backup.timer", "is-enabled") == "disabled",
        "unfenced: no backup of the stale world yet",
    )
    proc = sh(box, serverops.resume_script())
    expect(proc.returncode == 0, f"the resume script ran ({proc.stderr.strip()[-300:]})")
    for unit in serverops.FENCE_UNITS:
        expect(state(box, unit, "is-active") in ("active", "activating"), f"unfenced: {unit} runs again")
    expect(sh(box, "test -e /var/lib/minecraft/admin-stopped").returncode != 0, "unfenced: marker gone")
    expect(server_up(), "unfenced: the server answers again")
    final = fence(box, {str(s["id"]) for s in snapshots(box)} | ({first} if first else set()))
    out.write_text(json.dumps({"snapshot_id": final}))


def new(box: str, state_file: Path) -> None:
    want = json.loads(state_file.read_text())["snapshot_id"]
    marker = serverops.parse_restored_from(sh(box, serverops.restored_from_script()).stdout)
    got = (marker or {}).get("snapshot_id")
    expect(got == want, f"the new box restored the old box's final snapshot ({got} == {want})")
    expect(server_up(), "the migrated server answers a status ping")
    scratch = "/var/tmp/e2e-restore-to"  # noqa: S108 - inside the container
    proc = sh(box, serverops.restore_to_script(None, scratch))
    expect(
        proc.returncode == 0 and "VERIFIED" in proc.stdout,
        f"restore --to (verified) ({proc.stdout[-200:]}{proc.stderr[-300:]})",
    )
    sh(box, f"rm -rf {scratch}")
    before = {str(s["id"]) for s in snapshots(box)}
    proc = sh(box, serverops.restore_live_script(want[:12]))
    parts = {k: v.strip() for k, v in remote.sections(proc.stdout).items()}
    expect(
        proc.returncode == 0 and parts.get("result") == "success",
        f"restore --live via mc-restore@ ({parts.get('result')}: {proc.stderr[-300:]})",
    )
    expect(
        "STEP" in parts.get("journal", ""),
        f"the live restore's STEP lines were read back ({parts.get('journal', '')[-200:]})",
    )
    safety = [
        s for s in snapshots(box) if str(s["id"]) not in before and "pre-restore" in (s.get("tags") or [])
    ]
    expect(bool(safety), "a pre-restore safety snapshot was taken first")
    marker = serverops.parse_restored_from(sh(box, serverops.restored_from_script()).stdout)
    expect((marker or {}).get("snapshot_id") == want, ".restored-from names the restored snapshot")
    expect(server_up(), "the server answers after the live restore")


if __name__ == "__main__":
    mode, box, path = sys.argv[1], sys.argv[2], Path(sys.argv[3])
    {"old": old, "new": new}[mode](box, path)
    print(f"{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
