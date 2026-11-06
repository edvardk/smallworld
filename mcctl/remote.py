"""The server over the tailnet (design §4: `status`, `rcon`, `ssh`, `logs`, `backup now/list`).

Everything goes through Tailscale SSH as the deploy user to `host.tailscale_name`, like CI. Remote commands
are built with shlex quoting; none carries a secret (RCON and restic credentials stay in the server's
root-only env files, read by `mc` / `mc-restic` there).
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from mcctl import render
from mcctl.schema import ServerConfig

Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
UNIT = re.compile(r"[A-Za-z0-9@._-]{1,128}")
SSH_OPTIONS = ("-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new")
SECTION = "@@mcctl:"


def capture(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def target(cfg: ServerConfig) -> str:
    return f"{render.DEPLOY_USER}@{cfg.host.tailscale_name}"


def ssh_argv(
    cfg: ServerConfig,
    command: str | None = None,
    *,
    batch: bool = True,
    tty: bool = False,
    host: str | None = None,
) -> list[str]:
    """ssh to the deploy user on the server; `host` overrides the MagicDNS name (e.g. its 100.x address)."""
    argv = ["ssh", *SSH_OPTIONS]
    if batch:
        argv += ["-o", "BatchMode=yes"]
    if tty:
        argv.append("-t")
    argv.append(f"{render.DEPLOY_USER}@{host}" if host else target(cfg))
    if command is not None:
        argv += ["--", command]
    return argv


# Security review (LOW): the admin's Tailscale SSH rule is `check` (terraform/tailscale-policy.json.tftpl): a
# fresh tailnet login at most every 12 h. Tailscale holds the session and prints a login URL into it, which a
# captured session (Ansible, mcctl's probes) never shows: login_check runs one plain session first.
CHECK_NOTE = "Tailscale SSH check: if a login.tailscale.com URL appears, open it and log in (valid for 12 h)"


def login_check(cfg: ServerConfig, *, timeout: float = 300) -> int:
    """`ssh deploy@server true` with ssh's own output on the terminal (not captured), so Tailscale's check URL
    is visible; its exit code (124 after `timeout` seconds). Callers go on either way: the real session
    reports a real failure."""
    try:
        return subprocess.run(ssh_argv(cfg, "true"), check=False, timeout=timeout).returncode
    except subprocess.TimeoutExpired:
        return 124


def rcon_command(args: Sequence[str]) -> str:
    if not args:
        raise ValueError("give a console command, e.g. `mcctl rcon list`")
    return "mc " + shlex.join(args)


def logs_command(unit: str, lines: int, follow: bool) -> str:
    if not UNIT.fullmatch(unit):
        raise ValueError(f"not a systemd unit name: {unit!r}")
    return shlex.join(
        ["sudo", "journalctl", "-u", unit, "-n", str(lines), "--no-pager", *(["-f"] if follow else [])]
    )


def backup_now_command() -> str:
    """Run mc-backup.service now and report how it ended (a oneshot: `systemctl start` returns once it
    finished). The verdict is the unit's own Result/ExecMainStatus (section `backup`, see backup_outcome),
    never stderr (sudo may warn there, e.g. "unable to resolve host") nor `systemctl status`'s exit code (3: a
    finished oneshot is inactive). Exits with `systemctl start`'s code; the journal tail follows."""
    return "\n".join(
        [
            "sudo systemctl start mc-backup.service; rc=$?",
            f"echo '{SECTION}backup'",
            'echo "StartExit=$rc"',
            "systemctl show mc-backup.service --property=Result,ExecMainStatus,ConditionResult",
            f"echo '{SECTION}journal'",
            "sudo journalctl -u mc-backup.service -n 8 --no-pager -o cat",
            "exit $rc",
        ]
    )


def backup_outcome(proc: subprocess.CompletedProcess[str]) -> tuple[bool, str]:
    """(succeeded, detail) of backup_now_command's run, from the unit's properties alone."""
    found = sections(proc.stdout or "")
    props = dict(line.split("=", 1) for line in found.get("backup", "").splitlines() if "=" in line)
    if not props:
        err = (proc.stderr or "").strip()[-300:]
        return False, f"no result from the server (ssh exit {proc.returncode}): {err}"
    if props.get("ConditionResult") == "no":
        return False, "mc-backup.service was skipped: a start condition failed (is the box fenced?)"
    result, status, start = props.get("Result"), props.get("ExecMainStatus"), props.get("StartExit")
    if result == "success" and status == "0" and start == "0":
        return True, "mc-backup.service finished: Result=success"
    tail = found.get("journal", "")[-300:]
    return False, f"mc-backup.service Result={result} ExecMainStatus={status} (start exit {start}); {tail}"


ADMIN_SCRIPT = "/opt/minecraft/bin/admin.py"  # = ansible mc_bin_dir/admin.py


def admin_command(action: str, reason: str = "") -> str:
    """Stop (admin-stopped marker + graceful stop) or start the server on purpose (scripts/admin.py)."""
    if action == "stop":
        return shlex.join(["sudo", ADMIN_SCRIPT, "stop", " ".join(reason.split())[:200] or "mcctl stop"])
    if action == "start":
        return shlex.join(["sudo", ADMIN_SCRIPT, "start"])
    raise ValueError(f"unknown action {action!r}")


def backup_list_command(latest: int) -> str:
    return shlex.join(["sudo", "mc-restic", "restic", "snapshots", "--latest", str(latest)])


# --- status -------------------------------------------------------------------------------------------

STATUS_SCRIPT = "\n".join(
    [
        f"echo '{SECTION}active'; systemctl is-active minecraft.service",
        f"echo '{SECTION}backup'; systemctl show mc-backup.service --property=Result,ExecMainExitTimestamp",
        f"echo '{SECTION}list'; mc list 2>&1",
        f"echo '{SECTION}tps'; mc tps 2>&1",
        f"echo '{SECTION}mspt'; mc mspt 2>&1",
        f"echo '{SECTION}disabled'; cat /var/lib/minecraft/plugins-disabled.json 2>/dev/null",
        f"echo '{SECTION}stopped'; test -e /var/lib/minecraft/admin-stopped && echo yes",
        f"echo '{SECTION}reboot'; test -e /var/run/reboot-required && echo yes",
        "true",
    ]
)

_FORMAT = re.compile(r"§[0-9a-fk-or]", re.I)
_TRIPLE = re.compile(r"(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)/(\d+(?:\.\d+)?)")
_FLOAT = re.compile(r"\*?(\d+(?:\.\d+)?)")
_LIST = re.compile(r"There are (\d+) of a max of (\d+) players online:?\s*(.*)", re.S)


def sections(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines():
        if line.startswith(SECTION):
            current = line[len(SECTION) :].strip()
            out[current] = ""
        elif current is not None:
            out[current] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def parse_tps(text: str) -> float | None:
    """1-minute TPS (Purpur `TPS from last 5s, 1m, 5m, 15m: ...`, Paper `... 1m, 5m, 15m: ...`)."""
    head, sep, tail = _FORMAT.sub("", text).partition(":")
    labels = [w.strip() for w in head.rsplit("last", 1)[-1].split(",")]
    values = [float(v) for v in _FLOAT.findall(tail)]
    if not sep or "1m" not in labels or len(values) != len(labels):
        return None
    return values[labels.index("1m")]


def parse_mspt(text: str) -> float | None:
    """1-minute average MSPT (last avg/min/max triple of `mspt`)."""
    triples = _TRIPLE.findall(_FORMAT.sub("", text))
    return float(triples[-1][0]) if triples else None


@dataclass
class Status:
    reachable: bool
    error: str | None = None
    server: str | None = None  # systemd ActiveState of minecraft.service
    admin_stopped: bool = False
    players_online: int | None = None
    players_max: int | None = None
    players: list[str] = field(default_factory=list)
    tps: float | None = None
    mspt: float | None = None
    last_backup: str | None = None
    last_backup_result: str | None = None
    reboot_required: bool = False
    disabled_plugins: list[str] = field(default_factory=list)
    last_main_run: dict[str, Any] | None = None
    pending_panel_prs: list[dict[str, Any]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_status(text: str) -> Status:
    s = sections(text)
    st = Status(reachable=True, server=s.get("active") or None)
    st.admin_stopped = s.get("stopped") == "yes"
    st.reboot_required = s.get("reboot") == "yes"
    if m := _LIST.search(_FORMAT.sub("", s.get("list", ""))):
        st.players_online, st.players_max = int(m.group(1)), int(m.group(2))
        st.players = [p.strip() for p in m.group(3).split(",") if p.strip()]
    st.tps, st.mspt = parse_tps(s.get("tps", "")), parse_mspt(s.get("mspt", ""))
    props = dict(line.split("=", 1) for line in s.get("backup", "").splitlines() if "=" in line)
    st.last_backup = props.get("ExecMainExitTimestamp") or None
    st.last_backup_result = props.get("Result") or None
    try:
        disabled = json.loads(s.get("disabled") or "null")
    except ValueError:
        disabled = None
    if isinstance(disabled, list):
        st.disabled_plugins = [str(d) for d in disabled]
    elif isinstance(disabled, dict):
        st.disabled_plugins = sorted(str(k) for k in disabled)
    return st


def github_status(st: Status, repository: str, run: Runner) -> None:
    """Last run of the config repo's workflow on main, and open `panel/*` PRs (via the gh CLI)."""
    runs = run(
        ["gh", "run", "list", "-R", repository, "--workflow", "server.yml", "--branch", "main",
         "--limit", "10", "--json", "status,conclusion,event,createdAt,headSha,url"]
    )  # fmt: skip
    try:
        items = json.loads(runs.stdout) if runs.returncode == 0 else None
    except ValueError:
        items = None
    if items is None:
        st.notes.append("last deploy: `gh run list` failed (is gh logged in?)")
    else:
        main_runs = [r for r in items if r.get("event") in ("push", "workflow_dispatch")]
        st.last_main_run = main_runs[0] if main_runs else None
    prs = run(["gh", "pr", "list", "-R", repository, "--state", "open", "--limit", "100",
               "--json", "number,title,headRefName,url"])  # fmt: skip
    try:
        open_prs = json.loads(prs.stdout) if prs.returncode == 0 else None
    except ValueError:
        open_prs = None
    if open_prs is None:
        st.notes.append("pending panel PRs: `gh pr list` failed")
    else:
        st.pending_panel_prs = [p for p in open_prs if str(p.get("headRefName", "")).startswith("panel/")]


def status(cfg: ServerConfig, run: Runner = capture) -> Status:
    proc = run(ssh_argv(cfg, STATUS_SCRIPT))
    if proc.returncode != 0:
        why = (proc.stderr or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        st = Status(reachable=False, error=f"ssh {target(cfg)}: {why[0]}")
    else:
        st = parse_status(proc.stdout)
    github_status(st, f"{cfg.github.owner}/{cfg.github.config_repo}", run)
    return st


def status_rows(st: Status) -> list[tuple[str, str]]:
    """format_status as label/value rows, values with a level (`ok:`/`warn:`/`fail:`) for mcctl.ui."""

    def num(v: float | None, fmt: str = "{:.1f}") -> str:
        return fmt.format(v) if v is not None else "n/a"

    rows: list[tuple[str, str]] = []
    if not st.reachable:
        rows.append(("server", f"fail: UNREACHABLE ({st.error})"))
    else:
        level = "warn" if st.admin_stopped else ("ok" if st.server == "active" else "warn")
        rows.append(("server", f"{level}: {st.server}" + (" (admin-stopped)" if st.admin_stopped else "")))
        who = f" ({', '.join(st.players)})" if st.players else ""
        rows.append(("players", f"{num(st.players_online, '{}')}/{num(st.players_max, '{}')}{who}"))
        tps = f"{num(st.tps)} / {num(st.mspt)} (1 min)"
        rows.append(("TPS / MSPT", (f"warn: {tps}" if st.tps is not None and st.tps < 19 else tps)))
        backup = f"{st.last_backup or 'never'} ({st.last_backup_result or 'n/a'})"
        good = st.last_backup_result in ("success", "ok")
        rows.append(("last backup", f"{'ok' if good else 'warn'}: {backup}"))
        if st.reboot_required:
            rows.append(("reboot", "warn: required (/var/run/reboot-required)"))
    if st.last_main_run:
        r = st.last_main_run
        result = r.get("conclusion") or r.get("status")
        level = "ok" if result == "success" else "warn"
        rows.append(
            (
                "last deploy",
                f"{level}: {r.get('createdAt')} {r.get('event')} {str(r.get('headSha', ''))[:12]}: {result}",
            )
        )
    drift = []
    if st.disabled_plugins:
        drift.append(f"disabled plugins: {', '.join(st.disabled_plugins)}")
    if st.pending_panel_prs:
        drift.append("pending panel PRs: " + ", ".join(f"#{p.get('number')}" for p in st.pending_panel_prs))
    rows.append(("drift", f"warn: {'; '.join(drift)}" if drift else "ok: none"))
    rows += [("note", n) for n in st.notes]
    return rows


def format_status(st: Status, name: str) -> str:
    def num(v: float | None, fmt: str = "{:.1f}") -> str:
        return fmt.format(v) if v is not None else "n/a"

    lines = [f"{name}:"]
    if not st.reachable:
        lines.append(f"  server        UNREACHABLE ({st.error})")
    else:
        stopped = " (admin-stopped)" if st.admin_stopped else ""
        lines.append(f"  server        {st.server}{stopped}")
        who = f" ({', '.join(st.players)})" if st.players else ""
        lines.append(f"  players       {num(st.players_online, '{}')}/{num(st.players_max, '{}')}{who}")
        lines.append(f"  TPS / MSPT    {num(st.tps)} / {num(st.mspt)} (1 min)")
        lines.append(f"  last backup   {st.last_backup or 'never'} ({st.last_backup_result or 'n/a'})")
        if st.reboot_required:
            lines.append("  reboot        required (/var/run/reboot-required)")
    if st.last_main_run:
        r = st.last_main_run
        result = r.get("conclusion") or r.get("status")
        lines.append(
            f"  last deploy   {r.get('createdAt')} {r.get('event')} "
            f"{str(r.get('headSha', ''))[:12]}: {result}"
        )
    drift = []
    if st.disabled_plugins:
        drift.append(f"disabled plugins: {', '.join(st.disabled_plugins)}")
    if st.pending_panel_prs:
        drift.append("pending panel PRs: " + ", ".join(f"#{p.get('number')}" for p in st.pending_panel_prs))
    lines.append(f"  drift         {'; '.join(drift) if drift else 'none'}")
    lines += [f"  note: {n}" for n in st.notes]
    return "\n".join(lines)
