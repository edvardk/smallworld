"""v1.3 server actions from the CLI (`restart`, `console`, `worlds`, `plugins`, `players kick|ban|pardon`).

Decision (docs/plans, v1.3 A4): the actions the panel has run **over Tailscale SSH**, like `rcon`, `stop` and
`start`, through the very code the panel uses: `mc-agent cli` on the server (v1.4: scripts/mc_agent.py or
the Rust port, server.yml agent.implementation; `mc_agent.py cli` on an older box) runs the agent's
Executor (its checks, the countdown job runner, the world library and plugin rules) as the ops user via
`sudo mc-run` (its sandbox, the polkit rules of 50-mc-ops.rules: never generic systemctl as root) and writes
the panel's audit entry (journal, MC_AUDIT=1) with the login `mcctl:<local user>`. Nothing is re-implemented
here: mcctl checks what it can before asking (a known world, a plugin in the lock, the typed confirmation,
so a typo never reaches the server), and the server checks everything again.

Destructive actions take the panel's typed confirmation: `create NAME`, `switch to NAME`, `delete NAME` for
worlds; `enable NAME` / `disable NAME` for plugins (the panel confirms these on its confirmation host). Give
it with `--confirm TEXT` (scripts) or type it when asked (a terminal); without either the command refuses.

`console` is `rcon` (the `mc` helper on the box) one command at a time, or a prompt that sends each line.
"""

from __future__ import annotations

import getpass
import json
import re
import shlex
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, get_args

from mcctl import remote, schema, ui
from mcctl.schema import ServerConfig

BIN = "/opt/minecraft/bin"  # = ansible mc_bin_dir
MC_RUN = "/usr/local/bin/mc-run"  # base role: runs a command as the ops user in a sandbox
NOTIFY_ENV = "/etc/minecraft/notify.env"  # WATCHDOG_URL + HEARTBEAT_TOKEN: the restart's maintenance window
# v1.4: the agent's entry point, a symlink to the implementation server.yml agent.implementation selects (base
# role); a box deployed before v1.4 has only the Python script.
AGENT_ENTRY = f"{BIN}/mc-agent"
LEGACY_AGENT_ENTRY = f"{BIN}/mc_agent.py"
COUNTDOWNS = (0, 10, 60, 300)  # = scripts/mc_panel.py COUNTDOWNS (the panel's choices)
DEFAULT_COUNTDOWN = 60  # = the Worker's default (countdownArg)
WORLD_NAME = re.compile(schema.WORLD_NAME)
SEED = re.compile(schema.SEED)
JAVA_NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
PLUGIN_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
WORLD_TYPES: tuple[str, ...] = get_args(schema.WorldType)
WORLD_PRESETS: tuple[str, ...] = get_args(schema.WorldPreset)
MODERATION = ("kick", "ban", "pardon")
RESULT = "RESULT "
QUIT_WORDS = ("exit", "quit", ":q")

LineStream = Callable[[Sequence[str], Callable[[str], None]], int]
Ask = Callable[[str], str]


def stream_lines(argv: Sequence[str], on_line: Callable[[str], None]) -> int:
    """Run argv, hand each stdout line to on_line as it arrives (a countdown is live), stderr straight
    through; the exit code."""
    with subprocess.Popen(list(argv), stdout=subprocess.PIPE, text=True, bufsize=1) as proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            on_line(line.rstrip("\n"))
    return proc.returncode


def interactive_ask(prompt: str) -> str:
    """A typed confirmation from a person at a terminal; "" (refused) when stdin isn't one."""
    if not sys.stdin.isatty():
        return ""
    try:
        return input(prompt)
    except (EOFError, KeyboardInterrupt):
        return ""


def cli_login(user: str | None = None) -> str:
    """`mcctl:<local user>`, the audit login (scripts/mc_agent.py CLI_LOGIN: letters, digits, . _ @ -)."""
    if user is None:
        try:
            user = getpass.getuser()
        except (OSError, KeyError, ImportError):  # no user name in the environment or the password db
            user = ""
    cleaned = re.sub(r"[^A-Za-z0-9._@-]", "_", user)[:64].strip("_")
    return f"mcctl:{cleaned or 'unknown'}"


def agent_argv(
    login: str, action: str, args: dict[str, Any] | None = None, entry: str = AGENT_ENTRY
) -> list[str]:
    """One action (or a list) through the agent's `cli` as the ops user: `sudo mc-run ENV ENTRY cli ...`."""
    argv = ["sudo", MC_RUN, f"-{NOTIFY_ENV}", entry, "cli", login, action]
    if args is not None:
        argv.append(json.dumps(args, sort_keys=True, separators=(",", ":")))
    return argv


def agent_command(login: str, action: str, args: dict[str, Any] | None = None) -> str:
    """The SSH command for one action: `mc-agent cli` (v1.4: whichever implementation the box runs), or
    `mc_agent.py cli` on a box deployed before v1.4, which has no mc-agent. The check needs sudo as well: the
    deploy user can't look into the bin directory (root:minecraft 0750)."""
    new = shlex.join(agent_argv(login, action, args))
    old = shlex.join(agent_argv(login, action, args, LEGACY_AGENT_ENTRY))
    return f"if sudo test -x {shlex.quote(AGENT_ENTRY)}; then exec {new}; else exec {old}; fi"


def confirm_phrase(action: str, name: str, enabled: bool = False) -> str:
    """= scripts/mc_agent.py cli_confirm (and the Worker's for the world library)."""
    return {
        "world_create": f"create {name}",
        "world_switch": f"switch to {name}",
        "world_delete": f"delete {name}",
        "plugin": f"{'enable' if enabled else 'disable'} {name}",
    }[action]


class ActionError(Exception):
    """Refused before anything was sent to the server (exit code 2)."""


@dataclass
class Remote:
    """Everything an action needs; tests replace the runners (no SSH, no network)."""

    cfg: ServerConfig
    out: ui.Out = field(default_factory=ui.stdout)
    capture: remote.Runner = remote.capture
    stream: LineStream = stream_lines
    ask: Ask = interactive_ask
    login: str = field(default_factory=cli_login)

    def ssh(self, command: str) -> list[str]:
        return remote.ssh_argv(self.cfg, command)

    def query(self, action: str) -> Any:  # noqa: ANN401 - the server's JSON
        """A read-only list from the server (`worlds`, `plugins`)."""
        proc = self.capture(self.ssh(agent_command(self.login, action)))
        lines = (proc.stdout or "").strip().splitlines()
        if proc.returncode != 0 or not lines:
            why = (proc.stderr or proc.stdout or "").strip()[-400:] or f"exit {proc.returncode}"
            raise ActionError(f"couldn't ask the server for its {action}: {why}")
        try:
            return json.loads(lines[-1])
        except ValueError as e:
            raise ActionError(f"the server's {action} list isn't JSON ({e})") from None

    def run_action(self, action: str, args: dict[str, Any]) -> int:
        """Stream the action's progress; 0 when the server says `RESULT ok`."""
        result: list[str] = []

        def on_line(line: str) -> None:
            if line.startswith(RESULT):
                result.append(line[len(RESULT) :])
            elif line.strip():
                self.out.info(line)

        code = self.stream(self.ssh(agent_command(self.login, action, args)), on_line)
        verdict = result[-1] if result else ""
        if code == 0 and verdict.startswith("ok:"):
            self.out.ok(verdict[3:].strip() or f"{action}: done")
            return 0
        self.out.bad(verdict or f"{action}: no result from the server (ssh exit {code})")
        return 1

    def confirm(self, want: str, given: str | None, what: str) -> None:
        typed = given if given is not None else self.ask(f"Type `{want}` to {what}: ")
        if typed.strip() != want:
            raise ActionError(f'not confirmed: type exactly "{want}" (or pass --confirm "{want}")')


def _countdown(value: int) -> int:
    if value not in COUNTDOWNS:
        raise ActionError(f"--countdown must be one of {', '.join(map(str, COUNTDOWNS))} seconds")
    return value


def guarded(rem: Remote, fn: Callable[[], int]) -> int:
    try:
        return fn()
    except ActionError as e:
        rem.out.bad(str(e))
        return 2


# --- restart ------------------------------------------------------------------------------------------------


def restart(rem: Remote, countdown: int = DEFAULT_COUNTDOWN, backup: bool = False, wait: bool = False) -> int:
    """The panel's restart: in-game countdown (skipped when nobody is online, unless `wait`), an optional
    backup first, then minecraft.service restarted by the ops user (polkit) and RCON awaited."""
    args = {"countdown": _countdown(countdown), "backup": backup, "skip_if_empty": not wait}
    rem.out.say(f"restart with a {countdown} s countdown" + (", backup first" if backup else ""))
    return rem.run_action("restart", args)


# --- console ------------------------------------------------------------------------------------------------


def console(rem: Remote, words: Sequence[str], read: Callable[[str], str] = input) -> int:
    """One console command (= `mcctl rcon`), or a prompt sending each line until exit/quit/EOF/Ctrl-C."""
    if words:
        return subprocess_code(rem, remote.rcon_command(list(words)))
    rem.out.say(f"console of {rem.cfg.host.tailscale_name} (over RCON); `exit` or Ctrl-D leaves")
    code = 0
    while True:
        try:
            line = read("> ").strip()
        except (EOFError, KeyboardInterrupt):
            rem.out.say()
            return code
        if not line:
            continue
        if line.lower() in QUIT_WORDS:
            return code
        code = subprocess_code(rem, remote.rcon_command([line.removeprefix("/")]))


def subprocess_code(rem: Remote, command: str) -> int:
    """A command whose output goes straight to the terminal (like `mcctl rcon`)."""
    return rem.stream(rem.ssh(command), rem.out.say)


# --- worlds -------------------------------------------------------------------------------------------------


def _size(n: object) -> str:
    if not isinstance(n, int):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024  # type: ignore[assignment]
    return ""


def worlds_list(rem: Remote) -> int:
    inv = rem.query("worlds")
    active = str(inv.get("active") or "?")
    rem.out.kv(
        [
            ("active", f"ok: {active}" + (f" (since {inv['since']})" if inv.get("since") else "")),
            ("data version", str(inv.get("data_version") or "?")),
        ],
        title="Worlds",
    )
    rows = [
        [str(a.get("name")), str(a.get("time") or ""), _size(a.get("size")), str(a.get("data_version") or "")]
        for a in inv.get("archives") or []
    ]
    if rows:
        rem.out.table(["Archived world", "Archived", "Size", "Data version"], rows, indent=2)
    else:
        rem.out.say("  no archived worlds: `mcctl worlds create NAME` archives the active one first")
    library = [e for e in inv.get("library") or [] if e.get("name") != active]
    if library:
        rows = [
            [
                str(e.get("name")),
                str(e.get("type")),
                str(e.get("preset") or ""),
                str(e.get("seed") or "random"),
            ]
            for e in library
        ]
        rem.out.table(["Library entry", "Type", "Preset", "Seed"], rows, indent=2)
    if inv.get("deleted"):
        rem.out.note(f"recently deleted (names not reusable yet): {', '.join(inv['deleted'])}")
    pend = inv.get("pending")
    if pend:
        pr = f" (PR #{pend['pr']})" if pend.get("pr") else ""
        rem.out.warn(f"pending for server.yml: {pend.get('what')}{pr} {pend.get('error') or ''}".rstrip())
    if inv.get("storage_note"):
        rem.out.warn("the archives are above R2's free storage (10 GB): delete worlds you don't need")
    return 0


def _world_name(name: str) -> str:
    if not WORLD_NAME.fullmatch(name):
        raise ActionError(
            "a world name is 1-32 lowercase letters, digits, _ or - (starting with a letter/digit)"
        )
    return name


def world_create(
    rem: Remote,
    name: str,
    *,
    seed: str = "",
    wtype: str = "default",
    preset: str = "survival_smp",
    pregen: bool | None = None,
    countdown: int = DEFAULT_COUNTDOWN,
    confirm: str | None = None,
) -> int:
    _world_name(name)
    if not SEED.fullmatch(seed):
        raise ActionError("the seed must be a whole number (or empty for a random one)")
    if wtype not in WORLD_TYPES:
        raise ActionError(f"--type must be one of {', '.join(WORLD_TYPES)}")
    if preset not in WORLD_PRESETS:
        raise ActionError(f"--preset must be one of {', '.join(WORLD_PRESETS)}")
    _countdown(countdown)
    inv = rem.query("worlds")
    taken = {str(inv.get("active"))} | {str(a.get("name")) for a in inv.get("archives") or []}
    if name in taken:
        raise ActionError(f"world {name} already exists (`mcctl worlds switch {name}`)")
    if name in (inv.get("deleted") or []):
        raise ActionError(
            f"world {name} was deleted recently (its archive isn't forgotten yet): pick another"
        )
    rem.out.say(
        f"create world {name} ({wtype}, {preset}, seed {seed or 'random'}): the active world "
        f"{inv.get('active')} is archived first, then the server restarts into the new one"
    )
    rem.confirm(confirm_phrase("world_create", name), confirm, "create it")
    args: dict[str, Any] = {
        "name": name,
        "seed": seed,
        "type": wtype,
        "preset": preset,
        "countdown": countdown,
    }
    if pregen is not None:
        args["pregen"] = pregen
    return rem.run_action("world_create", args | {"confirm": confirm_phrase("world_create", name)})


def world_switch(
    rem: Remote, name: str, *, countdown: int = DEFAULT_COUNTDOWN, confirm: str | None = None
) -> int:
    _world_name(name)
    _countdown(countdown)
    inv = rem.query("worlds")
    if name == inv.get("active"):
        raise ActionError(f"{name} is already the active world")
    if name not in {str(a.get("name")) for a in inv.get("archives") or []}:
        known = ", ".join(str(a.get("name")) for a in inv.get("archives") or []) or "none"
        raise ActionError(f"unknown world {name} (archived worlds: {known}; `mcctl worlds create {name}`)")
    rem.out.say(f"switch to {name}: {inv.get('active')} is archived first (the safety snapshot)")
    rem.confirm(confirm_phrase("world_switch", name), confirm, "switch")
    args = {"name": name, "countdown": countdown, "confirm": confirm_phrase("world_switch", name)}
    return rem.run_action("world_switch", args)


def world_delete(rem: Remote, name: str, *, confirm: str | None = None) -> int:
    _world_name(name)
    inv = rem.query("worlds")
    if name == inv.get("active"):
        raise ActionError(f"{name} is the active world; switch to another one first")
    if name not in {str(a.get("name")) for a in inv.get("archives") or []}:
        raise ActionError(f"unknown world {name}: not an archived world (`mcctl worlds list`)")
    rem.out.say(f"delete {name}: its archive snapshots are forgotten (once the bucket lock allows)")
    rem.confirm(confirm_phrase("world_delete", name), confirm, "delete it")
    return rem.run_action("world_delete", {"name": name, "confirm": confirm_phrase("world_delete", name)})


# --- plugins ------------------------------------------------------------------------------------------------


def plugins_list(rem: Remote) -> int:
    plugins = rem.query("plugins")
    if not plugins:
        rem.out.say("no plugins in the deployed lock")
        return 0
    rows = [
        [
            str(p.get("name")),
            str(p.get("version") or ""),
            "ok: enabled" if p.get("enabled") else "warn: disabled",
        ]
        for p in plugins
    ]
    rem.out.table(["Plugin", "Version", "State"], rows, indent=2)
    return 0


def plugin_toggle(
    rem: Remote, name: str, enabled: bool, *, countdown: int = DEFAULT_COUNTDOWN, confirm: str | None = None
) -> int:
    """The panel's plugin switch: the override file, the jar moved (mc-plugins-apply.service), a countdown
    restart."""
    if not PLUGIN_NAME.fullmatch(name):
        raise ActionError(f"not a plugin name: {name!r}")
    _countdown(countdown)
    plugins = {str(p.get("name")): p for p in rem.query("plugins")}
    if name not in plugins:
        raise ActionError(
            f"plugin {name} isn't in the deployed plugins.lock.yml ({', '.join(sorted(plugins)) or 'none'})"
        )
    if bool(plugins[name].get("enabled")) == enabled:
        raise ActionError(f"plugin {name} is already {'enabled' if enabled else 'disabled'}")
    verb = "enable" if enabled else "disable"
    rem.out.say(f"{verb} {name}: the server restarts after a {countdown} s countdown (skipped when empty)")
    want = confirm_phrase("plugin", name, enabled)
    rem.confirm(want, confirm, verb)
    return rem.run_action(
        "plugin", {"name": name, "enabled": enabled, "countdown": countdown, "confirm": want}
    )


# --- players: live moderation -------------------------------------------------------------------------------


def moderate(rem: Remote, verb: str, target: str, reason: str = "") -> int:
    """kick / ban / pardon over RCON through the panel's moderation action (its console shapes, its audit).
    Live only: bans stay in the server's banned-players.json, not in players.yml."""
    if verb not in MODERATION:
        raise ActionError(f"verb must be one of {', '.join(MODERATION)}")
    if not JAVA_NAME.fullmatch(target):
        raise ActionError("a Java name is 3-16 letters, digits or _")
    args: dict[str, Any] = {"verb": verb, "target": target}
    if reason.strip() and verb != "pardon":
        args["reason"] = " ".join(reason.split())[:100]
    return rem.run_action("moderate", args)
