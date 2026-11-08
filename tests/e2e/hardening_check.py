"""P7 acceptance inside the e2e-mock container (run as root after site.yml; needs PyYAML).

Checks the hardening site.yml applied, on the live systemd, against the product's own definitions:
- `systemd-analyze security` exposure of every product unit (<= 2.5; minecraft <= 4.5), and that the running
  JVM really is sandboxed (NoNewPrivs, a seccomp filter);
- mc-backup, mc-backup-check (the weekly backup test) and mc-health run to success inside their sandboxes
  (restic against the e2e repository, RCON);
- mc-backup's sandbox (P8 review): with its EnvironmentFile loaded and CAP_DAC_READ_SEARCH effective, it still
  can't read the other env files, SSH host keys, Tailscale's state or the agent's state (InaccessiblePaths);
- every sysctl of the hardening role reads back as set; the journal is persistent; THP is madvise;
- OpenSSH is stopped and masked;
- the hostname resolves through /etc/hosts (127.0.1.1), so `sudo true` prints nothing on stderr;
- the admin-stopped marker: `admin.py stop` stops the server, a health run leaves it down, `admin.py start`
  brings it back;
- privilege separation (P7 review): the helpers run as mcops, and a process running as minecraft (the JVM's
  user) can't read their env files, their caches, or /proc/<pid>/environ of a running mcops process that holds
  the restic credentials, and can't forge the self-heal markers;
- a live restore: the ops user fetches the latest snapshot, mc-restore-apply.service (as minecraft, started via
  polkit) copies it in, the world is owned by minecraft and the server starts again;
- self-heal under the sandbox: the JVM is frozen (SIGSTOP), two health runs (the e2e start-up grace is short)
  dump threads and restart it through systemd/polkit, and the server answers again.

    /opt/ansible/bin/python tests/e2e/hardening_check.py /src/product          # everything (core shard)
    /opt/ansible/bin/python tests/e2e/hardening_check.py /src/product --agent  # v1.4: the agent's units only

v1.4 `--agent` (the e2e's rust shards): mc-agent and, when installed, the telemetry sidecar: the exposure gate,
running as mcops with the journal group, environment unreadable from minecraft, and the live process under its
seccomp filter with no_new_privs and MemoryDenyWriteExecute (the Rust binary in that sandbox, design §5).
"""

from __future__ import annotations

import json
import os
import pwd
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path

import yaml
from e2e_common import journal_of, run_oneshot, timers_paused

THRESHOLDS = {
    "mc-backup.service": 2.5,
    "mc-prune.service": 2.5,
    "mc-health.service": 2.5,
    "mc-restore-apply.service": 2.5,
    "mc-agent.service": 2.5,
    "mc-plugins-apply.service": 2.5,  # P10: the panel's plugin switch
    "mc-restore@abcdef12.service": 2.5,  # P10: the panel's restore (a template instance: scored, never started here)
    "mc-reboot-gate.service": 2.5,  # P11
    "mc-world.service": 2.5,  # v1.2: the world library (scored; agent_check.py runs it)
    "mc-world-apply.service": 2.5,
    "mc-backup-check.service": 2.5,  # v1.2: the weekly backup test (was the config repo's Monday Actions job)
    "mc-restart-window.service": 2.5,  # v1.3: the deferred restart (installed with maintenance.window: vars.yml)
    "mc-map.service": 2.5,  # v1.4: the world map pre-render (map_check.py runs it)
    "minecraft.service": 4.5,
}
# v1.4: units only some deployments install, scored where they are
OPTIONAL_THRESHOLDS = {
    "mc-telemetry.service": 2.5,  # the telemetry sidecar (agent.implementation rust with experience on)
}
AGENT_UNITS = ("mc-agent.service", "mc-telemetry.service")
SECRET_FILES = (
    "/etc/minecraft/restic.env",
    "/etc/minecraft/health.env",
    "/etc/minecraft/notify.env",
    "/etc/minecraft/agent.env",
)
BIN = Path("/opt/minecraft/bin")
failures: list[str] = []


def run(*argv: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(argv, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise SystemExit(f"{' '.join(argv)} failed ({proc.returncode}):\n{proc.stdout}\n{proc.stderr}")
    return proc


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def show(unit: str, *props: str) -> dict[str, str]:
    out = run("systemctl", "show", unit, "-p", ",".join(props)).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def exposure(unit: str) -> float:
    out = run("systemd-analyze", "security", "--no-pager", unit, check=False).stdout
    for line in out.splitlines():
        if "Overall exposure level" in line:
            return float(line.split(":", 1)[1].split()[0])
    raise SystemExit(f"no exposure level for {unit}:\n{out}")


def rcon_up(timeout: float = 600) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            socket.create_connection(("127.0.0.1", 25575), timeout=2).close()
            return True
        except OSError:
            time.sleep(3)
    return False


def as_user(user: str, action: Callable[[], object]) -> int:
    """Run action() in a child process as `user` (its own groups only); exit 0 = it worked, 13 = EACCES/EPERM."""
    pw = pwd.getpwnam(user)
    pid = os.fork()
    if pid == 0:  # child
        code = 1
        try:
            os.setgroups(os.getgrouplist(user, pw.pw_gid))
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            action()
            code = 0
        except PermissionError:
            code = 13
        except BaseException:  # reported as "other failure"
            code = 1
        os._exit(code)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])


def read(path: str) -> Callable[[], object]:
    return lambda: Path(path).read_bytes()


def main_pid(unit: str) -> int:
    return int(show(unit, "MainPID")["MainPID"])


def installed(unit: str) -> bool:
    return show(unit, "LoadState").get("LoadState") == "loaded"


def gate(unit: str, limit: float) -> None:
    level = exposure(unit)
    expect(level <= limit, f"{unit}: systemd-analyze exposure {level} <= {limit}")
    proc = run("systemd-analyze", "security", f"--threshold={int(limit * 10)}", unit, check=False)
    expect(proc.returncode == 0, f"{unit}: systemd-analyze --threshold={int(limit * 10)} passes")


def agent_checks() -> None:
    """v1.4: the agent's units as they run (either implementation; the sidecar where installed)."""
    for unit in AGENT_UNITS:
        if not installed(unit):
            print(f"(skipped: {unit} is not installed)")
            continue
        gate(unit, {**THRESHOLDS, **OPTIONAL_THRESHOLDS}[unit])
        props = show(unit, "ActiveState", "User", "SupplementaryGroups", "MemoryDenyWriteExecute", "MainPID")
        expect(
            props.get("ActiveState") == "active" and props.get("User") == "mcops",
            f"{unit} runs as mcops ({props})",
        )
        expect(props.get("SupplementaryGroups", "").split() == ["minecraft", "systemd-journal"],
               f"{unit}: groups minecraft + systemd-journal ({props.get('SupplementaryGroups')})")  # fmt: skip
        expect(props.get("MemoryDenyWriteExecute") == "yes", f"{unit}: MemoryDenyWriteExecute")
        pid = int(props.get("MainPID", "0"))
        status = Path(f"/proc/{pid}/status").read_text() if pid else ""
        expect("NoNewPrivs:\t1" in status and "Seccomp:\t2" in status,
               f"{unit}: the live process has no_new_privs and its seccomp filter")  # fmt: skip
        expect(
            as_user("minecraft", read(f"/proc/{pid}/environ")) == 13,
            f"minecraft can't read {unit}'s environment",
        )
        exe = os.readlink(f"/proc/{pid}/exe") if pid else ""
        print(f"     {unit}: {exe}")


def main(product: Path, agent_only: bool = False) -> int:
    with timers_paused() as timers:
        print(f"(timers paused for the checks: {', '.join(timers) or 'none'})")
        if agent_only:
            agent_checks()
        else:
            checks(product)
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


def checks(product: Path) -> None:
    # --- systemd sandbox ----------------------------------------------------------------------------
    for unit, limit in THRESHOLDS.items():
        gate(unit, limit)
    for unit, limit in OPTIONAL_THRESHOLDS.items():
        if installed(unit):
            gate(unit, limit)
    agent_checks()
    pid = show("minecraft.service", "MainPID")["MainPID"]
    status = Path(f"/proc/{pid}/status").read_text()
    expect("NoNewPrivs:\t1" in status, "the running JVM has no_new_privs")
    expect("Seccomp:\t2" in status, "the running JVM runs under a seccomp filter (SystemCallFilter)")
    expect("CapEff:\t0000000000000000" in status, "the running JVM has no capabilities")
    backup_sandbox_probe()
    res, _ = run_oneshot("mc-backup.service")
    expect(res.get("Result") == "success", f"mc-backup.service runs to success in its sandbox ({res})")
    # v1.2: the weekly backup test against the snapshot just taken (restic check, restore into its private
    # /var/tmp, file list + checksums + a usable world)
    res, journal = run_oneshot(
        "mc-backup-check.service", until=lambda t: "backup test passed" in t, timeout=60
    )
    expect(res.get("Result") == "success" and "VERIFIED" in journal,
           f"mc-backup-check.service runs to success in its sandbox ({res}; {journal.strip()[-300:]})")  # fmt: skip
    res, journal = run_oneshot("mc-health.service", until=lambda t: "status=" in t)
    expect(res.get("Result") == "success", f"mc-health.service runs to success in its sandbox ({res})")
    expect("status=ok" in journal, f"health.py saw the server alive over RCON ({journal.strip()[-200:]})")

    # --- sysctls, journal, THP -------------------------------------------------------------------------
    defaults = yaml.safe_load((product / "ansible/roles/hardening/defaults/main.yml").read_text())
    for key, want in defaults["hardening_sysctls"].items():
        got = Path("/proc/sys", *key.split(".")).read_text().strip()
        expect(got == str(want), f"sysctl {key} = {got} (want {want})")
    expect(Path("/etc/systemd/journald.conf.d/50-minecraft.conf").is_file(), "journald drop-in installed")
    expect(Path("/var/log/journal").is_dir(), "the journal is persistent (/var/log/journal exists)")
    thp = Path("/sys/kernel/mm/transparent_hugepage/enabled").read_text()
    expect("[madvise]" in thp, f"THP is madvise ({thp.strip()})")

    # --- hostname (first real setup: every sudo printed "unable to resolve host") ------------------------
    name = socket.gethostname()
    hosts = run("getent", "hosts", name, check=False)
    expect(
        hosts.stdout.split()[:1] == ["127.0.1.1"], f"{name} resolves to 127.0.1.1 ({hosts.stdout.strip()!r})"
    )
    sudo = run("sudo", "true", check=False)
    expect(
        sudo.returncode == 0 and sudo.stderr == "", f"`sudo true` is silent (stderr {sudo.stderr.strip()!r})"
    )

    # --- OpenSSH off ---------------------------------------------------------------------------------
    for unit in ("ssh.service", "ssh.socket"):
        props = show(unit, "LoadState", "ActiveState")
        if props.get("LoadState") == "not-found":
            continue
        expect(
            props == {"LoadState": "masked", "ActiveState": "inactive"},
            f"{unit} masked and stopped ({props})",
        )

    # --- root: password locked, bootstrap key gone (security review M1) ---------------------------------
    status = run("passwd", "-S", "root", check=False).stdout.split()
    expect(status[1:2] == ["L"], f"root's password is locked (passwd -S: {' '.join(status)})")
    expect(not Path("/root/.ssh/authorized_keys").exists(), "/root/.ssh/authorized_keys removed")

    # --- admin-stopped marker ----------------------------------------------------------------------------
    run(str(Path("/opt/minecraft/bin/admin.py")), "stop", "e2e")
    expect(show("minecraft.service", "ActiveState")["ActiveState"] == "inactive", "admin.py stop stopped it")
    res, journal = run_oneshot("mc-health.service", until=lambda t: "status=" in t)
    expect(res.get("Result") == "success" and "heal=none:admin-stopped" in journal,
           f"self-heal stood down ({journal.strip()[-200:]})")  # fmt: skip
    expect(show("minecraft.service", "ActiveState")["ActiveState"] == "inactive", "... and left it down")
    run(str(Path("/opt/minecraft/bin/admin.py")), "start")
    expect(not Path("/var/lib/minecraft/admin-stopped").exists(), "admin.py start removed the marker")
    expect(rcon_up(), "the server is back (RCON answers)")

    privilege_separation()
    live_restore()
    self_heal_after_sigstop()


PROBE = """
import os, sys
fails = []
if "RESTIC_PASSWORD" not in os.environ:
    fails.append("EnvironmentFile not loaded")
cap = int(next(l for l in open("/proc/self/status") if l.startswith("CapEff:")).split()[1], 16)
if not cap & (1 << 2):
    fails.append("no CAP_DAC_READ_SEARCH")
for path in sys.argv[1:]:
    try:
        open(path, "rb").read(1)
        fails.append("read " + path)
    except (PermissionError, FileNotFoundError):
        pass
print("PROBE " + ("ok" if not fails else "; ".join(fails)))
sys.exit(1 if fails else 0)
"""
# Files the probe must not read from inside mc-backup's sandbox; all of them exist and are readable with its
# capability outside of it (checked first, so the probe proves InaccessiblePaths and not a missing file).
HIDDEN_FROM_BACKUP = (
    "/etc/minecraft/agent.env",
    "/etc/minecraft/health.env",
    "/etc/minecraft/restic.env",
    "/etc/ssh/e2e_host_key",
    "/var/lib/tailscale/tailscaled.state",
    "/var/lib/mc-agent/nonces.json",
)


def backup_sandbox_probe() -> None:
    """Runs a probe as mc-backup's ExecStart (same unit, same sandbox, same EnvironmentFile and capability)."""
    created = []
    for path in HIDDEN_FROM_BACKUP:
        f = Path(path)
        if not f.exists():
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("e2e probe target\n")
            f.chmod(0o600)
            created.append(f)
    Path("/run/mc-e2e-probe.py").write_text(PROBE)
    dropin = Path("/run/systemd/system/mc-backup.service.d/zz-e2e-probe.conf")
    dropin.parent.mkdir(parents=True, exist_ok=True)
    dropin.write_text("[Service]\nExecStart=\nExecStart=/usr/bin/python3 /run/mc-e2e-probe.py "
                      + " ".join(HIDDEN_FROM_BACKUP) + "\n")  # fmt: skip
    try:
        run("systemctl", "daemon-reload")
        # The probe's own invocation only (run_oneshot fails if the start joined another run of the unit).
        res, out = run_oneshot("mc-backup.service", until=lambda t: "PROBE " in t)
        probe = [line for line in out.splitlines() if line.startswith("PROBE ")]
        expect(res.get("Result") == "success" and probe == ["PROBE ok"],
               f"mc-backup (env loaded, CAP_DAC_READ_SEARCH) can't read {', '.join(HIDDEN_FROM_BACKUP)} ({out[-300:]})")  # fmt: skip
    finally:
        dropin.unlink(missing_ok=True)
        run("systemctl", "daemon-reload")
        run("systemctl", "reset-failed", "mc-backup.service", check=False)
        for f in created:
            f.unlink(missing_ok=True)


def privilege_separation() -> None:
    groups = run("id", "-nG", "mcops").stdout.split()
    expect(sorted(groups) == ["mcops", "minecraft"], f"mcops is in mcops + minecraft only ({groups})")
    expect("mcops" not in run("id", "-nG", "minecraft").stdout.split(), "minecraft is not in group mcops")
    for unit in ("mc-backup.service", "mc-prune.service", "mc-health.service", "mc-agent.service",
                 "mc-backup-check.service"):  # fmt: skip
        expect(show(unit, "User")["User"] == "mcops", f"{unit} runs as mcops")
    for path in SECRET_FILES:
        expect(as_user("minecraft", read(path)) == 13, f"minecraft can't read {path}")
        expect(
            as_user("mcops", read(path)) == 13, f"mcops can't read {path} either (systemd reads it as root)"
        )
    expect(
        as_user("minecraft", lambda: os.listdir("/var/cache/mc-restic")) == 13,
        "minecraft can't read restic's cache",
    )
    marker = "/var/lib/minecraft/admin-stopped"
    expect(
        as_user("minecraft", lambda: Path(marker).write_text("{}")) == 13,
        "minecraft can't forge admin-stopped",
    )
    expect(not Path(marker).exists(), "... and no marker appeared")
    expect(
        as_user("mcops", read("/opt/minecraft/server/server.properties")) == 0,
        "mcops reads server.properties",
    )
    # A running helper as mcops with the restic credentials in its environment (what mc-backup is while it runs).
    unit = "mc-e2e-environ.service"
    run("systemctl", "stop", unit, check=False)
    run("systemctl", "reset-failed", unit, check=False)
    run("systemd-run", f"--unit={unit}", "--uid=mcops", "--gid=mcops", "-p", "SupplementaryGroups=minecraft",
        "-p", "EnvironmentFile=/etc/minecraft/restic.env", "/bin/sleep", "300")  # fmt: skip
    time.sleep(1)
    pid = main_pid(unit)
    environ = f"/proc/{pid}/environ"
    expect(
        pid > 0 and b"RESTIC_PASSWORD=" in Path(environ).read_bytes(),
        "the mcops helper holds the credentials",
    )
    expect(as_user("minecraft", read(environ)) == 13, f"minecraft can't read {environ} of the mcops helper")
    expect(as_user("minecraft", lambda: os.kill(pid, 0)) == 13, "minecraft can't signal the mcops helper")
    run("systemctl", "stop", unit, check=False)
    jvm = main_pid("minecraft.service")
    expect(
        as_user("mcops", read(f"/proc/{jvm}/environ")) == 13, "mcops can't read the JVM's environment either"
    )


def live_restore() -> None:
    snaps = json.loads(run("/usr/local/bin/mc-restic", "restic", "snapshots", "--json").stdout)
    expect(bool(snaps), f"the backup made a snapshot ({len(snaps)})")
    if not snaps:
        return
    snap = max(snaps, key=lambda s: s["time"])
    run(str(BIN / "admin.py"), "stop", "e2e restore")
    level = Path("/opt/minecraft/server/world/level.dat")
    level.unlink(missing_ok=True)  # damage the world: the restore must bring it back
    proc = run(
        "/usr/local/bin/mc-restic", str(BIN / "restore.py"), "--snapshot", snap["short_id"], check=False
    )
    expect(
        proc.returncode == 0 and "RESTORED" in proc.stdout,
        f"restore.py --snapshot ({proc.stdout[-300:]}{proc.stderr[-500:]})",
    )
    apply = show("mc-restore-apply.service", "Result", "User")
    expect(
        apply == {"Result": "success", "User": "minecraft"},
        f"mc-restore-apply.service ran as minecraft ({apply})",
    )
    expect(
        level.is_file() and level.stat().st_uid == pwd.getpwnam("minecraft").pw_uid,
        "level.dat is back, owned by minecraft",
    )
    marker = Path("/opt/minecraft/server/.restored-from")
    expect(
        marker.is_file() and json.loads(marker.read_text())["snapshot_id"] == snap["id"],
        ".restored-from names the snapshot",
    )
    expect(not Path("/var/lib/mc-restore/staging").exists(), "the staging dir was cleaned up")
    run(str(BIN / "admin.py"), "start")
    expect(rcon_up(), "the restored server answers RCON")


def self_heal_after_sigstop() -> None:
    expect(rcon_up(), "the server is up before the heal test")
    grace = 60  # tests/e2e/vars.yml monitoring_heal_startup_grace_s
    out = run(
        "systemctl", "show", "--timestamp=unix", "-p", "ActiveEnterTimestamp", "--value", "minecraft.service"
    )
    stamp = out.stdout.strip().lstrip("@")
    entered = int(stamp) if stamp.isdigit() else 0
    wait = entered + grace + 5 - time.time()
    if wait > 0:
        time.sleep(wait)
    pid = main_pid("minecraft.service")
    frozen = show("minecraft.service", "InvocationID")["InvocationID"]
    os.kill(pid, signal.SIGSTOP)  # hung JVM: the process is there, RCON never answers
    try:
        journal = ""
        for _ in range(2):
            res, out = run_oneshot("mc-health.service", until=lambda t: "status=" in t)
            journal += out
            expect(res.get("Result") == "success", f"mc-health run during the hang ({res})")
        expect("AUDIT self-heal action=dump-restart" in journal, "self-heal decided dump + restart")
        expect("self-heal restart failed" not in journal and "thread dump failed" not in journal,
               "polkit allowed the SIGQUIT and the restart to mcops")  # fmt: skip
        deadline = time.monotonic() + 300
        while time.monotonic() < deadline and main_pid("minecraft.service") in (pid, 0):
            time.sleep(3)
        expect(main_pid("minecraft.service") not in (pid, 0), "systemd replaced the frozen JVM")
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
    expect(rcon_up(), "the healed server answers RCON again")
    deadline = time.monotonic() + 30
    while "Full thread dump" not in (dump := journal_of(frozen)) and time.monotonic() < deadline:
        time.sleep(1)
    expect("Full thread dump" in dump, "the frozen JVM wrote a thread dump to the journal")


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]), agent_only=sys.argv[2:3] == ["--agent"]))
