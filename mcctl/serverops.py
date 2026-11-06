"""Server-side command lines for the P12 commands (`migrate`, `restore`, `secrets rotate`, `upgrade`).

Pure functions that build the shell text mcctl sends over Tailscale SSH as the deploy user (`sudo` is
allowed for it; design §4) and parse what comes back. No command carries a secret: credentials stay in the
server's root-only env files, read by `mc-restic`/`mc-run` there, and the one value mcctl hands over
(`restic key add`'s new password) goes on stdin, never into the command text.

Output sections are marked `@@mcctl:<name>` lines (remote.sections), so a command's own chatter can't be
mistaken for a value. tests/e2e/migrate_check.py runs these very scripts inside the e2e container.
"""

from __future__ import annotations

import json
import re
import shlex
from typing import Any

from mcctl import remote

BIN = "/opt/minecraft/bin"  # = ansible mc_bin_dir
SERVER_DIR = "/opt/minecraft/server"  # = ansible mc_server_dir
NOTIFY_ENV = "/etc/minecraft/notify.env"
# Timer-driven and long-running helpers a fenced box must not run: a stale backup must never become the
# latest snapshot the new box restores, its agent must not answer the panel, nothing may restart the server.
FENCE_UNITS = (
    "mc-backup.timer",
    "mc-prune.timer",
    "mc-backup-check.timer",
    "mc-health.timer",
    "mc-reboot-gate.timer",
    "mc-agent.service",
)
SNAPSHOT = re.compile(r"[0-9a-f]{8,64}")
S = remote.SECTION


def _maintenance(minutes: int, reason: str) -> str:
    argv = [
        "sudo",
        "/usr/local/bin/mc-run",
        f"-{NOTIFY_ENV}",
        f"{BIN}/notify.py",
        "maintenance",
        str(minutes),
        reason,
    ]
    return shlex.join(argv) + " || true"  # the watchdog being unreachable never blocks a migration


def _each_unit(action: str, units: tuple[str, ...]) -> str:
    """`systemctl <action>` for each unit that exists on this box (older deployments lack some)."""
    names = " ".join(shlex.quote(u) for u in units)
    present = 'systemctl cat "$u" >/dev/null 2>&1'
    return f'for u in {names}; do if {present}; then sudo systemctl {action} "$u"; fi; done'


def latest_snapshot_script() -> str:
    return f"echo '{S}snapshots'; sudo mc-restic restic snapshots --json"


STATE_DIR = "/var/lib/minecraft"  # = ansible mc_state_dir
FENCED = f"{STATE_DIR}/fenced"  # = scripts/notify.py FENCED_FILE
RESTORE_LOCK = f"{STATE_DIR}/restore.lock"
RESTORE_WAIT_S = 1800


def _wait_for_restore() -> str:
    """Wait (up to RESTORE_WAIT_S) while a restore runs: its unit is active or restore.py holds restore.lock
    (backup.py would skip with exit 0 meanwhile, so a "final" snapshot could predate the restored world). Then
    refuse."""
    tries = RESTORE_WAIT_S // 5
    busy = (
        "systemctl list-units --no-legend --plain --state=active,activating,deactivating "
        "'mc-restore@*' mc-restore-apply.service | grep -q . || "
        f"{{ [ -e {RESTORE_LOCK} ] && ! sudo flock --nonblock --shared {RESTORE_LOCK} true; }}"
    )
    return (
        f"n=0; while {busy}; do n=$((n+1)); if [ $n -gt {tries} ]; then "
        f"echo 'a restore is still running after {RESTORE_WAIT_S} s: not fencing now' >&2; exit 75; fi; "
        "sleep 5; done"
    )


def fence_script(reason: str) -> str:
    """Fence a server: maintenance window, helpers disabled, any running restore waited for, admin-stopped,
    not started at boot, one last backup (after any running one) whose snapshot is printed, then the
    persistent `fenced` marker (the units' ConditionPathExists=!, admin.py/mc-agent start, self-heal, the
    reboot gate and site.yml all honour it). Safe to run again: on a fenced box the backup unit doesn't run,
    so the latest snapshot is still the first run's final one."""
    why = " ".join(reason.split())[:200] or "fenced by mcctl"
    marker = json.dumps({"reason": why, "by": "mcctl"})
    return "\n".join(
        [
            "set -e",
            _maintenance(240, "migrate"),
            _each_unit("disable --now", FENCE_UNITS),
            _wait_for_restore(),
            shlex.join(["sudo", f"{BIN}/admin.py", "stop", why]),
            "sudo systemctl disable minecraft.service",
            "while systemctl is-active --quiet mc-backup.service; do sleep 5; done",
            "sudo systemctl start mc-backup.service",
            f"if ! sudo test -e {FENCED}; then printf '%s\\n' {shlex.quote(marker)} | "
            f"sudo tee {FENCED}.tmp >/dev/null && sudo chmod 0644 {FENCED}.tmp && "
            f"sudo mv {FENCED}.tmp {FENCED}; fi",
            latest_snapshot_script(),
            f"echo '{S}end'",
        ]
    )


def unfence_script() -> str:
    """Undo the fence marker (a rollback) and enable the server at boot again, but start nothing: the server
    stays admin-stopped and the timers/agent off, so a restore can replace the stale world before anything
    runs or backs it up (resume_script then starts it all)."""
    return "\n".join(
        [
            "set -e",
            f"sudo rm -f {FENCED}",
            "sudo systemctl enable minecraft.service",
            f"echo '{S}end'",
        ]
    )


def resume_script() -> str:
    """After unfence (and any restore): the helpers enabled and started, the server started, the window
    closed."""
    return "\n".join(
        [
            "set -e",
            _each_unit("enable --now", FENCE_UNITS),
            shlex.join(["sudo", f"{BIN}/admin.py", "start"]),
            _maintenance(0, "migrate"),
            f"echo '{S}end'",
        ]
    )


def parse_snapshots(text: str) -> list[dict[str, Any]]:
    """restic's `snapshots --json` from the `snapshots` section (newest last), or []."""
    raw = remote.sections(text).get("snapshots", "").strip()
    try:
        data = json.loads(raw) if raw else []
    except ValueError:
        return []
    snaps = [s for s in data if isinstance(s, dict) and SNAPSHOT.fullmatch(str(s.get("id", "")))]
    return sorted(snaps, key=lambda s: str(s.get("time", "")))


def latest(text: str) -> dict[str, Any] | None:
    snaps = parse_snapshots(text)
    return snaps[-1] if snaps else None


# written by the minecraft role's pre-upgrade backup (P12 review)
PRE_UPGRADE = f"{STATE_DIR}/pre-upgrade.json"


def pre_upgrade_script() -> str:
    return f"echo '{S}preupgrade'; cat {PRE_UPGRADE} 2>/dev/null || true"


def parse_pre_upgrade(text: str) -> dict[str, Any] | None:
    raw = remote.sections(text).get("preupgrade", "").strip()
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) and SNAPSHOT.fullmatch(str(data.get("snapshot_id", ""))) else None


def restored_from_script() -> str:
    return f"echo '{S}restored'; sudo cat {SERVER_DIR}/.restored-from 2>/dev/null || true"


def parse_restored_from(text: str) -> dict[str, Any] | None:
    raw = remote.sections(text).get("restored", "").strip()
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


# --- restore (P10's units through the CLI) ---------------------------------------------------------------


def check_snapshot(snapshot: str) -> str:
    if not SNAPSHOT.fullmatch(snapshot):
        raise ValueError(f"not a snapshot id (8-64 lowercase hex): {snapshot!r}")
    return snapshot


def restore_live_script(snapshot: str) -> str:
    """The panel's restore unit (as mcops; safety snapshot -> stop -> fetch -> apply -> start) started by root
    (`sudo systemctl`; no new polkit rule), then the STEP lines this run logged (unit instance + start
    time; polled until journald has them)."""
    unit = f"mc-restore@{check_snapshot(snapshot)}.service"
    return "\n".join(
        [
            f"sudo systemctl reset-failed {unit} 2>/dev/null || true",
            f"t0=$(date +%s); rc=0; sudo systemctl start {unit} || rc=$?",
            # this run's lines: the instance name is the snapshot, t0 the start; journald may still be
            # forwarding the last lines, so sync and retry for a few seconds
            "for _ in 1 2 3 4 5 6 7 8 9 10; do sudo journalctl --sync 2>/dev/null || true;"
            f" out=$(sudo journalctl -u {unit} --since @$t0 -o cat --no-pager 2>/dev/null);"
            ' case "$out" in *STEP*) break ;; esac; sleep 1; done',
            f"echo '{S}journal'",
            'printf "%s\\n" "$out"',
            f"echo '{S}result'; systemctl show -p Result --value {unit}",
            'exit "$rc"',
        ]
    )


def restore_to_script(snapshot: str | None, target: str) -> str:
    """Restore a snapshot (default: latest) into a scratch directory on the server and verify it there
    (restore.py --verify-into: file count, checksum sample, a usable world). Never the live server dir."""
    # restic runs as the ops user in mc-run's sandbox (ProtectHome=yes, ProtectSystem=full) without write
    # access to /srv: only the world-writable scratch dirs work.
    if not re.fullmatch(r"/(var/tmp|tmp)/[A-Za-z0-9._/-]{1,200}", target) or ".." in target:
        raise ValueError(f"--to must be a new directory under /var/tmp or /tmp: {target!r}")
    if snapshot is None:
        return shlex.join(["sudo", "mc-restic", f"{BIN}/restore.py", "--verify-into", target])
    return shlex.join(
        ["sudo", "mc-restic", "restic", "restore", check_snapshot(snapshot), "--target", target, "--verify"]
    )


# --- restic key rotation ---------------------------------------------------------------------------------


def key_list_script() -> str:
    return f"echo '{S}keys'; sudo mc-restic restic key list --json"


def current_key(text: str) -> str | None:
    raw = remote.sections(text).get("keys", "").strip()
    try:
        keys = json.loads(raw) if raw else []
    except ValueError:
        return None
    for k in keys if isinstance(keys, list) else []:
        if isinstance(k, dict) and k.get("current") and SNAPSHOT.fullmatch(str(k.get("id", ""))):
            return str(k["id"])
    return None


def key_ids(text: str) -> list[str]:
    raw = remote.sections(text).get("keys", "").strip()
    try:
        keys = json.loads(raw) if raw else []
    except ValueError:
        return []
    return [str(k["id"]) for k in keys if isinstance(k, dict) and SNAPSHOT.fullmatch(str(k.get("id", "")))]


def key_remove_script(key_id: str) -> str:
    return shlex.join(["sudo", "mc-restic", "restic", "key", "remove", check_snapshot(key_id)])


REPO_CHECK = "sudo mc-restic restic cat config >/dev/null && echo REPO_OK"
