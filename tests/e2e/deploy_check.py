"""P12 review acceptance on the e2e-mock runner: what a deploy must do on its own, whoever runs it.

Runs on the runner (this checkout), driving the container with `docker exec` as root, the way the workflow runs
site.yml (same inventory, extra vars, MC_SECRET_* fakes passed by name from the job's environment).

    python tests/e2e/deploy_check.py BOX
        restic-key rotation by any deploy (H2): a deploy whose MC_SECRET_RESTIC_PASSWORD the repository doesn't
        know adds it as a key (restic_rekey.py, the password on stdin) before writing restic.env, a backup works
        with it, the next deploy changes nothing; a deploy whose password neither the repository nor the current
        restic.env opens fails and leaves restic.env as it was; the original password still works afterwards
        (its key was never removed), so the later checks run unchanged.
        pre-upgrade backup (M3): when the Minecraft version that last ran differs from the lock's, the deploy
        backs the world up before the new jar and records the snapshot in /var/lib/minecraft/pre-upgrade.json.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from mcctl import serverops
from mcctl.secrets import SECRETS

failures: list[str] = []
ENV_FILE = "/etc/minecraft/restic.env"
STATE = "/var/lib/minecraft"


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def sh(box: str, script: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "exec", box, "bash", "-c", script], capture_output=True, text=True, check=False
    )


def deploy(box: str, tags: str, **override: str) -> subprocess.CompletedProcess[str]:
    """site.yml as the workflow runs it, limited to `tags`; `override` replaces MC_SECRET_* values (by name)."""
    env = os.environ | override
    argv = ["docker", "exec", "-w", "/src/product/ansible"]
    for s in SECRETS:
        argv += ["-e", s.env]
    argv += [box, "/opt/ansible/bin/ansible-playbook", "-i", "/src/rendered/ansible/inventory.yml", "site.yml",
             "--skip-tags", "tailscale,firewall", "--tags", tags, "-e", "@/src/product/tests/e2e/vars.yml"]  # fmt: skip
    proc = subprocess.run(argv, capture_output=True, text=True, check=False, env=env)
    print(proc.stdout[-3000:])
    return proc


def env_sum(box: str) -> str:
    return sh(box, f"sha256sum {ENV_FILE}").stdout.split(" ")[0]


def backup_works(box: str) -> bool:
    before = {
        str(s["id"]) for s in serverops.parse_snapshots(sh(box, serverops.latest_snapshot_script()).stdout)
    }
    if sh(box, "systemctl start mc-backup.service").returncode != 0:
        return False
    after = serverops.parse_snapshots(sh(box, serverops.latest_snapshot_script()).stdout)
    return any(str(s["id"]) not in before for s in after)


def rekey(box: str) -> None:
    original = os.environ["MC_SECRET_RESTIC_PASSWORD"]
    rotated = "e2e-not-a-secret-rotated-restic-password"
    keys = len(json.loads(sh(box, "mc-restic restic key list --json").stdout or "[]"))
    proc = deploy(box, "backups", MC_SECRET_RESTIC_PASSWORD=rotated)
    expect(proc.returncode == 0, "a deploy with a password the repository doesn't know succeeds")
    expect("Make sure the new restic password opens the repository" in proc.stdout, "the rekey task ran")
    expect(
        len(json.loads(sh(box, "mc-restic restic key list --json").stdout or "[]")) == keys + 1,
        "the deploy added the new password as a key",
    )
    expect(sh(box, f"grep -qx 'RESTIC_PASSWORD={rotated}' {ENV_FILE}").returncode == 0, "restic.env has it")
    expect(rotated not in proc.stdout + proc.stderr, "the password never shows in the deploy output")
    expect(backup_works(box), "a backup works with the rotated password")
    proc = deploy(box, "backups", MC_SECRET_RESTIC_PASSWORD=rotated)
    expect(proc.returncode == 0 and "changed=0" in proc.stdout, "the next deploy changes nothing")

    # neither the new password nor the current restic.env opens the repository: fail, restic.env untouched
    sh(box, f"cp -p {ENV_FILE} /root/restic.env.keep && sed -i 's/^RESTIC_PASSWORD=.*/RESTIC_PASSWORD=bogus/' "
            f"{ENV_FILE}")  # fmt: skip
    before = env_sum(box)
    proc = deploy(box, "backups", MC_SECRET_RESTIC_PASSWORD="e2e-unknown-password-nobody-has")
    expect(
        proc.returncode != 0 and "restic.env was left as it is" in proc.stdout, "an unknown password fails"
    )
    expect(env_sum(box) == before, "the failed deploy left restic.env as it was")
    sh(box, f"mv /root/restic.env.keep {ENV_FILE}")

    proc = deploy(box, "backups", MC_SECRET_RESTIC_PASSWORD=original)
    expect(proc.returncode == 0, "the original password still opens the repository (its key wasn't removed)")
    expect(backup_works(box), "a backup works with the original password again")


def pre_upgrade(box: str) -> None:
    sh(box, f"echo 0.0.1 > {STATE}/minecraft-version && rm -f {STATE}/pre-upgrade.json")
    proc = deploy(box, "minecraft")
    expect(proc.returncode == 0, "the deploy of a 'new' Minecraft version ran")
    expect("PRE-UPGRADE SNAPSHOT" in proc.stdout, "it announced the pre-upgrade snapshot")
    pre = serverops.parse_pre_upgrade(sh(box, serverops.pre_upgrade_script()).stdout)
    latest = serverops.latest(sh(box, serverops.latest_snapshot_script()).stdout)
    expect(
        pre is not None
        and latest is not None
        and pre["snapshot_id"] == latest["id"]
        and pre["from"] == "0.0.1",
        f"pre-upgrade.json names the snapshot the deploy took ({pre})",
    )
    version = sh(box, f"cat {STATE}/minecraft-version").stdout.strip()
    expect(version not in ("", "0.0.1"), f"the version marker is the lock's now ({version})")
    proc = deploy(box, "minecraft")
    expect(proc.returncode == 0 and "PRE-UPGRADE SNAPSHOT" not in proc.stdout, "the same version: no backup")


def chunky(box: str) -> None:
    """health.py's pre-generation matches Chunky's replies over RCON (lang/en.json): check the real plugin answers."""
    reply = sh(box, "runuser -u minecraft -- /opt/minecraft/bin/mc_rcon.py cmd chunky progress").stdout
    expect("No tasks running" in reply, f"Chunky answers `chunky progress` over RCON ({reply.strip()[:120]})")
    env = sh(box, "grep '^PREGEN_WORLDS=' /etc/minecraft/health.env").stdout.strip()
    expect(env == "PREGEN_WORLDS=", f"pre-generation is off in e2e ({env})")


def social(box: str) -> None:
    """v1.2: AxGraves is enabled with its overlay, and the social game rules are set in every dimension."""
    rcon = "runuser -u minecraft -- /opt/minecraft/bin/mc_rcon.py cmd "
    plugins = sh(box, rcon + "plugins").stdout
    expect("AxGraves" in plugins, f"AxGraves is enabled ({plugins.strip()[:200]})")
    cfg = sh(
        box,
        "grep -E '^(despawn-time-seconds|interact-only-own):' /opt/minecraft/server/plugins/AxGraves/config.yml",
    )
    expect("despawn-time-seconds: 900" in cfg.stdout, f"AxGraves overlay applied ({cfg.stdout.strip()})")
    for dim in ("minecraft:overworld", "minecraft:the_nether", "minecraft:the_end"):
        for rule, want in (("locator_bar", "true"), ("players_sleeping_percentage", "50")):
            reply = sh(box, rcon + f"execute in {dim} run gamerule {rule}").stdout.strip()
            expect(reply.endswith(f"set to {want}"), f"{dim} {rule} = {want} ({reply[:120]})")


if __name__ == "__main__":
    box = sys.argv[1]
    chunky(box)
    social(box)
    rekey(box)
    pre_upgrade(box)
    print(f"{len(failures)} failure(s)")
    sys.exit(1 if failures else 0)
