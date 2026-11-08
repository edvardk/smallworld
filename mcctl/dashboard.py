"""The dashboard, plain `mcctl` on an interactive terminal: the web panel in the terminal (Textual).

    mcctl              # the dashboard on the config repo here, or the remembered one
    mcctl -C DIR       # ... on DIR

Layout (the panel's, watchdog/panel/index.html): a header (deployment, "Smallworld vX · Purpur ...", an
online badge), tabs (1-9: Overview, Players, Worlds, Plugins, Backups, Logs, Console, Publish, Health), the
key bindings in the footer, and every action in the command palette (ctrl+p, fuzzy).

Nothing is re-implemented here:
- the status is `remote.status` (home.fetch_status: bounded, never reads the terminal) in a background thread
  every REFRESH_S seconds, never while one is still running; the UI never waits for it;
- the worlds, plugins and snapshot lists are the queries the commands use (`actions.Remote.query`,
  `serverops.parse_snapshots`), the Health tab is `doctor.run`, all in background threads;
- every action runs the existing command as a child of this same mcctl (`sys.executable -m mcctl ... -C
  ROOT`, stdin closed, plain output, no update notice), its stdout and stderr streamed line by line into the
  tab's output panel (or a modal's), where it stays, with a spinner while it runs and a clear verdict
  (exit code) at the end, until the next action or "Clear";
- questions a command would ask are asked first, in a modal, and passed as its flags: a destructive action
  asks for the very typed confirmation the command checks (`switch to NAME`, `delete NAME`, `enable NAME`,
  `restore <8 hex>`, ...), compared exactly before anything runs, then passed as `--confirm TEXT`, and the
  command (and the server) checks it again; publish and players get `--merge`/`--no-merge` and `--restart`.
- commands that need the terminal itself (ssh, `secrets rotate`, `secrets show-restic`, `doctor --fix`, an
  interactive `publish`) run with the dashboard suspended (`App.suspend`): credentials only ever go through
  getpass in the terminal, never into argv, a log or a widget.

`MCCTL_NO_UPDATE_NOTICE`: the Overview's "Update" line (mcctl/notice.py) unless it is set; the children never
print the notice. The non-interactive rules (`--plain`, MCCTL_PLAIN, NO_COLOR, CI, TERM=dumb, no terminal)
are home.wanted's: then `mcctl` stays the usage error it always was.
"""

from __future__ import annotations

import datetime
import io
import os
import shlex
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar

from rich.table import Table
from rich.text import Text
from textual import events, on, work
from textual.app import App, ComposeResult, SystemCommand
from textual.binding import Binding, BindingType
from textual.containers import Grid, Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.theme import Theme
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Input,
    Label,
    OptionList,
    RichLog,
    Select,
    Sparkline,
    Static,
    TabbedContent,
    TabPane,
)
from textual.widgets.option_list import Option

from mcctl import __version__, actions, configrepo, doctor, expirycheck, home, homecfg, remote, serverops, ui
from mcctl.schema import ServerConfig

REFRESH_S = 10.0  # the Overview's status refresh (the panel polls about as often)
HISTORY = 60  # TPS/MSPT samples kept for the sparklines (10 minutes at REFRESH_S)
MAX_LOG_LINES = 5000
SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
DOCTOR_TIMEOUT = 60

# The panel's colours (watchdog/panel/panel.css: --good, --warn, --bad, --accent), dark and light.
COLOURS = {
    True: {"ok": "#7ad65a", "warn": "#f4bf4f", "bad": "#ff6b5e", "accent": "#46d3e6", "muted": "#8d9bb0"},
    False: {"ok": "#2e8a1e", "warn": "#9a6200", "bad": "#c0352b", "accent": "#0a8fa3", "muted": "#566377"},
}
THEMES = (
    Theme(
        name="smallworld-dark",
        primary="#46d3e6",
        secondary="#7ad65a",
        accent="#46d3e6",
        success="#7ad65a",
        warning="#f4bf4f",
        error="#ff6b5e",
        foreground="#e8eef5",
        background="#0b1016",
        surface="#141c26",
        panel="#1b2633",
        dark=True,
    ),
    Theme(
        name="smallworld-light",
        primary="#0a8fa3",
        secondary="#3f8f22",
        accent="#0a8fa3",
        success="#2e8a1e",
        warning="#9a6200",
        error="#c0352b",
        foreground="#142030",
        background="#eef2f6",
        surface="#ffffff",
        panel="#dde5ee",
        dark=False,
    ),
)

TABS = (
    ("overview", "Overview"),
    ("players", "Players"),
    ("worlds", "Worlds"),
    ("plugins", "Plugins"),
    ("backups", "Backups"),
    ("logs", "Logs"),
    ("console", "Console"),
    ("publish", "Publish"),
    ("health", "Health"),
)

# --- running mcctl ------------------------------------------------------------------------------------------

LineSink = Callable[[str], None]
# (argv after `mcctl`, a sink for each output line, a callback that receives a `stop()`) -> exit code
Spawn = Callable[[Sequence[str], LineSink, Callable[[Callable[[], None]], None]], int]


def child_env(environ: dict[str, str] | None = None) -> dict[str, str]:
    """A child mcctl prints plain lines (no spinners or colour codes), unbuffered, UTF-8, no update notice."""
    from mcctl import notice

    env = dict(os.environ if environ is None else environ)
    env[ui.PLAIN_ENV] = "1"
    env[notice.OPT_OUT_ENV] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def mcctl_argv(argv: Sequence[str]) -> list[str]:
    return [sys.executable, "-m", "mcctl", *argv]


def _stopper(proc: subprocess.Popen[str]) -> Callable[[], None]:
    """Stop the child and what it started (ssh for `logs --follow`): its process group or tree."""

    def stop() -> None:
        if proc.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                    check=False,
                )
            else:
                os.killpg(proc.pid, signal.SIGTERM)
        except OSError:
            proc.terminate()

    return stop


def spawn_mcctl(
    argv: Sequence[str], on_line: LineSink, on_start: Callable[[Callable[[], None]], None]
) -> int:
    """Run `mcctl ARGV` (this interpreter), stdin closed, stdout+stderr line by line into on_line."""
    extra: dict[str, Any] = (
        {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
        if os.name == "nt"
        else {"start_new_session": True}
    )
    try:
        proc = subprocess.Popen(
            mcctl_argv(argv),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=child_env(),
            **extra,
        )
    except OSError as e:
        on_line(f"couldn't start mcctl: {e}")
        return 127
    on_start(_stopper(proc))
    with proc:
        assert proc.stdout is not None
        for line in proc.stdout:
            on_line(line.rstrip("\r\n"))
    return proc.returncode


def quiet_capture(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return home.quiet_run(argv)


def query_server(cfg: ServerConfig, what: str) -> Any:  # noqa: ANN401 - the server's JSON
    """`worlds` / `plugins` from the agent, as `mcctl worlds list` asks (actions.Remote.query)."""
    rem = actions.Remote(cfg, out=ui.Out(io.StringIO(), rich=False), capture=quiet_capture)
    return rem.query(what)


def list_snapshots(cfg: ServerConfig) -> list[dict[str, Any]]:
    """restic's snapshots (newest last), as `mcctl restore --list` reads them."""
    proc = quiet_capture(remote.ssh_argv(cfg, serverops.latest_snapshot_script()))
    if proc.returncode != 0:
        why = (proc.stderr or proc.stdout or "").strip()[-300:] or f"exit {proc.returncode}"
        raise RuntimeError(f"listing snapshots failed: {why}")
    return serverops.parse_snapshots(proc.stdout)


def _doctor_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCTOR_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def run_doctor(root: Path) -> doctor.Report:
    """`mcctl doctor` (read-only; no --fix), its tools never reading the terminal."""
    return doctor.run(root, deps=doctor.Deps(run=_doctor_run))


@dataclass
class Services:
    """Everything the dashboard reaches outside the process; tests replace each (no SSH, no network)."""

    status: Callable[[ServerConfig], remote.Status] = home.fetch_status
    query: Callable[[ServerConfig, str], Any] = query_server
    snapshots: Callable[[ServerConfig], list[dict[str, Any]]] = list_snapshots
    doctor: Callable[[Path], doctor.Report] = run_doctor
    spawn: Spawn = spawn_mcctl
    update: Callable[[configrepo.Pin | None], str | None] | None = None  # main(): notice.for_pin
    terminal: Callable[[Sequence[str]], int] | None = None  # None: the real terminal (App.suspend)


# --- text helpers -------------------------------------------------------------------------------------------


def _num(v: float | None, fmt: str = "{:.1f}") -> str:
    return "-" if v is None else fmt.format(v)


def server_state(st: remote.Status | str | None) -> tuple[str, str]:
    """(badge text, level) of the header's badge."""
    if st is None:
        return "CHECKING", "muted"
    if isinstance(st, str) or not st.reachable:
        return "UNREACHABLE", "bad"
    if st.admin_stopped:
        return "STOPPED", "warn"
    if st.server == "active":
        return "ONLINE", "ok"
    return (st.server or "unknown").upper(), "bad" if st.server in ("failed", "inactive") else "warn"


def expiring(repo: configrepo.ConfigRepo, today: datetime.date) -> list[tuple[str, str]]:
    """(level, text) of credentials that expire within expirycheck.WARN_DAYS (expiry.yml, from doctor)."""
    out: list[tuple[str, str]] = []
    for c in repo.expiry:
        try:
            exp = expirycheck._date(c.get("expires_on"))
        except ValueError:
            continue
        if exp is None:
            continue
        days = (exp - today).days
        if days < 0:
            out.append(("bad", f"{c.get('name')} expired on {exp}: renew it, then `mcctl doctor`"))
        elif days <= expirycheck.WARN_DAYS:
            out.append(("warn", f"{c.get('name')} expires in {days} days ({exp})"))
    return out


def server_software(repo: configrepo.ConfigRepo | None) -> str:
    if repo is None:
        return ""
    srv = repo.server_lock.get("server") if isinstance(repo.server_lock, dict) else None
    if not isinstance(srv, dict):
        return f"{repo.server.minecraft.flavour.title()} {repo.server.minecraft.version}"
    build = f" (build {srv['build']})" if srv.get("build") else ""
    return f"{str(srv.get('project') or repo.server.minecraft.flavour).title()} {srv.get('version')}{build}"


# --- widgets ------------------------------------------------------------------------------------------------


class OutputPanel(Vertical):
    """An action's output: a state line (spinner while running, then ok / failed with the exit code), a Stop
    button for long jobs, and the scrolling log. The output stays until the next action or Clear."""

    DEFAULT_CSS = """
    OutputPanel { height: 1fr; min-height: 6; border: round $panel-lighten-2; }
    OutputPanel > .ohead { height: 1; }
    OutputPanel .ostate { width: 1fr; padding: 0 1; }
    OutputPanel .ostate.-ok { color: $success; text-style: bold; }
    OutputPanel .ostate.-bad { color: $error; text-style: bold; }
    OutputPanel .ostate.-running { color: $warning; text-style: bold; }
    OutputPanel .ohead Button { min-width: 9; height: 1; border: none; margin: 0 0 0 1; }
    OutputPanel RichLog { height: 1fr; background: $surface; padding: 0 1; }
    """

    def __init__(self, title: str = "Output", *, idle: str = "", id: str | None = None) -> None:
        super().__init__(id=id)
        self.title = title
        self.idle = idle or "nothing has run yet: the output of an action stays here"
        self.busy = False
        self.stopped = False  # the last job was ended with Stop
        self.code: int | None = None
        self.running = ""
        self._stop: Callable[[], None] | None = None
        self._frame = 0

    def compose(self) -> ComposeResult:
        with Horizontal(classes="ohead"):
            yield Static(self.idle, classes="ostate")
            yield Button("Stop", classes="ostop", disabled=True)
            yield Button("Clear", classes="oclear")
        yield RichLog(wrap=True, markup=False, highlight=False, max_lines=MAX_LOG_LINES, auto_scroll=True)

    def on_mount(self) -> None:
        self.border_title = self.title
        self.set_interval(0.12, self._spin)

    @property
    def log_widget(self) -> RichLog:
        return self.query_one(RichLog)

    @property
    def state_widget(self) -> Static:
        return self.query_one(".ostate", Static)

    def _set_state(self, text: str | Text, cls: str) -> None:
        state = self.state_widget
        state.update(text)
        state.set_classes(f"ostate {cls}".strip())

    def _spin(self) -> None:
        if self.busy:
            self._frame = (self._frame + 1) % len(SPINNER)
            self._set_state(f"{SPINNER[self._frame]} running: {self.running}", "-running")

    def start(self, title: str, *, clear: bool = True) -> None:
        if clear:
            self.log_widget.clear()
        self.busy, self.code, self.running, self._stop, self.stopped = True, None, title, None, False
        self.log_widget.write(Text(f"$ {title}", style="bold"))
        self._set_state(f"{SPINNER[0]} running: {title}", "-running")

    def line(self, text: str) -> None:
        self.log_widget.write(_styled_line(text))

    def note(self, text: str, level: str = "muted") -> None:
        self.log_widget.write(Text(text, style=colour(self.app, level)))

    def set_stop(self, stop: Callable[[], None]) -> None:
        self._stop = stop
        self.query_one(".ostop", Button).disabled = False

    def finish(self, code: int) -> None:
        self.busy, self.code, self._stop = False, code, None
        self.query_one(".ostop", Button).disabled = True
        if self.stopped:
            self._set_state(f"■ stopped: {self.running}", "")
            self.note(f"── {self.running}: stopped", "warn")
        elif code == 0:
            self._set_state(f"✓ done: {self.running}", "-ok")
            self.note(f"── {self.running}: done", "ok")
        else:
            self._set_state(f"✗ failed (exit {code}): {self.running}", "-bad")
            self.note(f"── {self.running}: failed: exit {code}", "bad")

    def stop(self) -> None:
        if self._stop is not None:
            self.stopped = True
            self.note("── stopping…", "warn")
            self._stop()

    @on(Button.Pressed, ".ostop")
    def _on_stop(self, event: Button.Pressed) -> None:
        event.stop()
        self.stop()

    @on(Button.Pressed, ".oclear")
    def _on_clear(self, event: Button.Pressed) -> None:
        event.stop()
        self.log_widget.clear()
        if not self.busy:
            self._set_state(self.idle, "")


def _styled_line(text: str) -> Text:
    """A child's plain line: its level marks ([ok] / [!!] / [xx], `fail:`, `warn:`) coloured as the panel."""
    stripped = text.strip()
    style = ""
    if stripped.startswith(("[ok]", "[  ok]", "ok:")):
        style = "ok"
    elif stripped.startswith(("[!!]", "[warn]", "warn:", "warning:")):
        style = "warn"
    elif stripped.startswith(("[xx]", "[fail]", "fail:", "error:", "Traceback")) or "error:" in stripped[:40]:
        style = "bad"
    return Text(text, style=COLOURS[True].get(style, "") if style else "")


def colour(app: App[Any] | None, level: str) -> str:
    dark = True
    if app is not None:
        try:
            dark = app.current_theme.dark
        except Exception:
            dark = True
    return COLOURS[dark].get(level, "")


class Card(Static):
    """One panel-like card: a titled box of rich text."""

    DEFAULT_CSS = """
    Card { border: round $panel-lighten-2; padding: 0 1; height: auto; min-height: 5; background: $surface; }
    """

    def __init__(self, title: str, *, id: str | None = None) -> None:
        super().__init__("…", id=id)
        self.card_title = title

    def on_mount(self) -> None:
        self.border_title = self.card_title


# --- modals -------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    choices: tuple[tuple[str, str], ...] = ()  # (label, value): a pick list
    default: str = ""
    optional: bool = False
    placeholder: str = ""
    check: Callable[[str], str | None] | None = None


def countdown_field() -> Field:
    choices = tuple(
        (f"{s} s" + (" (default)" if s == actions.DEFAULT_COUNTDOWN else ""), str(s))
        for s in (60, 10, 300, 0)
    )
    return Field("countdown", "In-game countdown (skipped when nobody is online)", choices, "60")


def merge_field() -> Field:
    return Field(
        "merge",
        "When the pull request's checks pass",
        (("merge it (that deploys)", "yes"), ("leave the pull request to me", "no")),
        "yes",
    )


def _regex_check(pattern: Any, what: str) -> Callable[[str], str | None]:  # noqa: ANN401 - re.Pattern
    return lambda v: None if pattern.fullmatch(v) else what


JAVA_NAME_CHECK = _regex_check(actions.JAVA_NAME, "a Java name is 3-16 letters, digits or _")
WORLD_NAME_CHECK = _regex_check(
    actions.WORLD_NAME, "1-32 lowercase letters, digits, _ or - (letter/digit first)"
)
PLUGIN_CHECK = _regex_check(actions.PLUGIN_NAME, "not a plugin name")


def _snapshot_check(v: str) -> str | None:
    try:
        serverops.check_snapshot(v)
    except ValueError as e:
        return str(e)
    return None if len(v) >= 8 else "give at least the first 8 hex digits of the snapshot id"


class FormModal(ModalScreen[dict[str, str] | None]):
    """An action's questions (its arguments); Run returns the answers, Esc / Cancel None."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, title: str, fields: Sequence[Field], *, intro: str = "", ok: str = "Run") -> None:
        super().__init__()
        self.form_title, self.fields, self.intro, self.ok = title, list(fields), intro, ok

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            yield Label(self.form_title, classes="dtitle")
            if self.intro:
                yield Static(self.intro, classes="dintro")
            for f in self.fields:
                yield Label(f.label + (" (optional)" if f.optional else ""), classes="flabel")
                if f.choices:
                    yield Select(
                        list(f.choices),
                        value=f.default or f.choices[0][1],
                        allow_blank=False,
                        id=f"f-{f.key}",
                    )
                else:
                    yield Input(value=f.default, placeholder=f.placeholder, id=f"f-{f.key}")
            yield Static("", classes="derror")
            with Horizontal(classes="dbuttons"):
                yield Button("Cancel", id="cancel")
                yield Button(self.ok, id="ok", variant="primary")

    def on_mount(self) -> None:
        if self.fields:
            self.query_one(f"#f-{self.fields[0].key}").focus()
        else:
            self.query_one("#ok", Button).focus()

    def values(self) -> dict[str, str]:
        out = {}
        for f in self.fields:
            w = self.query_one(f"#f-{f.key}")
            out[f.key] = str(w.value).strip() if isinstance(w, Input | Select) else ""
        return out

    def submit(self) -> None:
        values = self.values()
        for f in self.fields:
            v = values[f.key]
            err = None
            if not v and not f.optional:
                err = "required"
            elif v and f.check is not None:
                err = f.check(v)
            if err:
                self.query_one(".derror", Static).update(f"{f.label}: {err}")
                self.query_one(f"#f-{f.key}").focus()
                return
        self.dismiss(values)

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.submit()

    @on(Input.Submitted)
    def _enter(self) -> None:
        self.submit()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmModal(ModalScreen[str | None]):
    """A destructive action's typed confirmation: the exact text the command checks, or nothing happens."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, want: str, title: str, body: str = "") -> None:
        super().__init__()
        self.want, self.confirm_title, self.body = want, title, body

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.confirm_title, classes="dtitle")
            if self.body:
                yield Static(self.body, classes="dintro")
            yield Static(
                Text.assemble("Type ", (self.want, "bold"), " to confirm (Esc: back, nothing changes)")
            )
            yield Input(placeholder=self.want, id="typed")
            yield Static("", classes="derror")
            with Horizontal(classes="dbuttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Confirm", id="ok", variant="error")

    def on_mount(self) -> None:
        self.query_one("#typed", Input).focus()

    def submit(self) -> None:
        typed = self.query_one("#typed", Input).value.strip()
        if typed != self.want:  # = actions.Remote.confirm: exactly, after trimming
            self.query_one(".derror", Static).update(f'not confirmed: type exactly "{self.want}"')
            return
        self.dismiss(typed)

    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        self.submit()

    @on(Input.Submitted)
    def _enter(self) -> None:
        self.submit()

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class RepoModal(ModalScreen[Path | None]):
    """Switch config repo: a remembered one, or a path (remembered as the current one)."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "cancel", "Cancel")]

    def __init__(self, current: Path | None) -> None:
        super().__init__()
        self.current = current
        self.settings = homecfg.load()

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Config repo", classes="dtitle")
            options = []
            for repo in self.settings.repos:
                ok = homecfg.looks_like_config_repo(repo)
                mark = " (current)" if repo == self.current else ("" if ok else " (not found)")
                options.append(Option(f"{repo}{mark}", id=str(repo), disabled=not ok))
            if options:
                yield OptionList(*options, id="repos")
            else:
                yield Static("no remembered config repos yet", classes="dintro")
            yield Label("…or a path", classes="flabel")
            yield Input(placeholder="path of the config repo", id="path")
            msg = f"ignoring {self.settings.problem}" if self.settings.problem else ""
            yield Static(msg, classes="derror")
            with Horizontal(classes="dbuttons"):
                yield Button("Cancel", id="cancel")
                yield Button("Open", id="ok", variant="primary")

    def on_mount(self) -> None:
        self.query_one("#path", Input).focus()

    def pick(self, raw: str) -> None:
        path = Path(raw).expanduser().resolve()
        if not homecfg.looks_like_config_repo(path):
            self.query_one(".derror", Static).update(
                f"{path} isn't a config repo (no server.yml and .github/workflows/server.yml)"
            )
            return
        self.dismiss(path)

    @on(OptionList.OptionSelected)
    def _picked(self, event: OptionList.OptionSelected) -> None:
        self.pick(str(event.option.id))

    @on(Input.Submitted)
    @on(Button.Pressed, "#ok")
    def _ok(self) -> None:
        typed = self.query_one("#path", Input).value.strip()
        if typed:
            self.pick(typed)
        else:
            self.query_one(".derror", Static).update("pick a config repo, or type its path")

    @on(Button.Pressed, "#cancel")
    def action_cancel(self) -> None:
        self.dismiss(None)


class OutputModal(ModalScreen[None]):
    """A command without a tab of its own (metrics, previews): its output in a modal until closed."""

    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "close", "Close")]
    DEFAULT_CSS = """
    OutputModal .dialog { width: 90%; height: 80%; }
    OutputModal OutputPanel { height: 1fr; }
    """

    def __init__(self, title: str) -> None:
        super().__init__()
        self.modal_title = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.modal_title, classes="dtitle")
            yield OutputPanel(self.modal_title)
            with Horizontal(classes="dbuttons"):
                yield Button("Close", id="close", variant="primary")

    @property
    def panel(self) -> OutputPanel:
        return self.query_one(OutputPanel)

    @on(Button.Pressed, "#close")
    def action_close(self) -> None:
        if self.panel.busy:
            self.panel.stop()
        self.dismiss(None)


# --- the tabs -----------------------------------------------------------------------------------------------


class Pane(TabPane):
    """A tab: its key bindings call the app's actions; `focus_first` makes them active (footer)."""

    KEYS: ClassVar[tuple[tuple[str, str, str], ...]] = ()  # (key, action key, footer text)

    def __init__(self, title: str, *, id: str) -> None:
        super().__init__(title, id=id)

    def action_act(self, name: str) -> None:
        self.app.trigger(name)  # type: ignore[attr-defined]

    def _on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Only the shown tab follows focus: a widget of a hidden tab that keeps or regains focus while the
        tab changes must not switch back to it (TabPane would)."""
        if not self.shown:
            event.prevent_default()

    @property
    def shown(self) -> bool:
        return getattr(self.parent, "current", self.id) == self.id

    def focus_first(self) -> None:
        if not self.shown:
            return
        for selector in ("DataTable", "Input", ".actions Button", "Button"):  # a table, a prompt, an action
            for w in self.query(selector):
                if w.focusable:
                    w.focus()
                    return

    @property
    def output(self) -> OutputPanel:
        return self.query_one(OutputPanel)


def _keys(keys: Iterable[tuple[str, str, str]]) -> list[BindingType]:
    return [Binding(key, f"act('{name}')", text) for key, name, text in keys]


def _buttons(*items: tuple[str, str, str]) -> ComposeResult:
    """(action key, label, variant) buttons: pressing one runs the action."""
    for name, label, variant in items:
        yield Button(label, name=name, variant=variant)  # type: ignore[arg-type]


class OverviewPane(Pane):
    KEYS = (
        ("r", "restart", "Restart"),
        ("s", "stop", "Stop"),
        ("t", "start", "Start"),
        ("b", "backup-now", "Back up"),
    )
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        with Grid(id="cards"):
            yield Card("Server", id="card-server")
            with Vertical(classes="perf", id="perf-tps"):
                yield Card("", id="card-tps")
                yield Sparkline([], id="spark-tps", summary_function=min)
            with Vertical(classes="perf", id="perf-mspt"):
                yield Card("", id="card-mspt")
                yield Sparkline([], id="spark-mspt", summary_function=max)
            yield Card("Backups and deploys", id="card-ops")
            yield Card("Versions", id="card-versions")
            yield Card("Needs attention", id="card-attention")
        with Horizontal(classes="actions"):
            yield from _buttons(
                ("restart", "Restart…", "primary"),
                ("stop", "Stop…", "error"),
                ("start", "Start", "default"),
                ("backup-now", "Back up now", "default"),
                ("refresh", "Refresh status", "default"),
            )
        yield OutputPanel("Server actions")

    def on_mount(self) -> None:
        self.query_one("#perf-tps").border_title = "TPS (1 min)"
        self.query_one("#perf-mspt").border_title = "MSPT (1 min)"


class PlayersPane(Pane):
    KEYS = (
        ("a", "player-add", "Whitelist"),
        ("x", "player-remove", "Unlist"),
        ("o", "player-op", "Op"),
        ("d", "player-deop", "Deop"),
        ("k", "kick", "Kick"),
        ("b", "ban", "Ban"),
        ("p", "pardon", "Pardon"),
    )
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        yield DataTable(id="players-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="players-note", classes="note")
        with Horizontal(classes="actions"):
            yield from _buttons(
                ("player-add", "Whitelist…", "primary"),
                ("player-remove", "Remove…", "default"),
                ("player-op", "Op…", "default"),
                ("player-deop", "Deop…", "default"),
                ("kick", "Kick…", "warning"),
                ("ban", "Ban…", "error"),
                ("pardon", "Pardon…", "default"),
            )
        yield OutputPanel("Player actions")


class WorldsPane(Pane):
    KEYS = (
        ("l", "worlds", "Reload"),
        ("c", "world-create", "Create"),
        ("w", "world-switch", "Switch"),
        ("d", "world-delete", "Delete"),
    )
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        yield DataTable(id="worlds-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="worlds-note", classes="note")
        with Horizontal(classes="actions"):
            yield from _buttons(
                ("worlds", "Reload", "default"),
                ("world-create", "Create…", "primary"),
                ("world-switch", "Switch…", "warning"),
                ("world-delete", "Delete…", "error"),
            )
        yield OutputPanel("World actions")


class PluginsPane(Pane):
    KEYS = (("l", "plugins", "Reload"), ("e", "plugin-enable", "Enable"), ("d", "plugin-disable", "Disable"))
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        yield DataTable(id="plugins-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="plugins-note", classes="note")
        with Horizontal(classes="actions"):
            yield from _buttons(
                ("plugins", "Reload", "default"),
                ("plugin-enable", "Enable…", "primary"),
                ("plugin-disable", "Disable…", "warning"),
            )
        yield OutputPanel("Plugin actions")


class BackupsPane(Pane):
    KEYS = (("l", "backups", "Reload"), ("b", "backup-now", "Back up now"), ("e", "restore", "Restore"))
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        yield DataTable(id="backups-table", cursor_type="row", zebra_stripes=True)
        yield Static("", id="backups-note", classes="note")
        with Horizontal(classes="actions"):
            yield from _buttons(
                ("backups", "Reload", "default"),
                ("backup-now", "Back up now", "primary"),
                ("restore", "Restore…", "error"),
            )
        yield OutputPanel("Backup actions")


class LogsPane(Pane):
    KEYS = (("f", "logs-follow", "Follow"), ("s", "logs-stop", "Stop"))
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="actions"):
            yield from _buttons(
                ("logs-follow", "Follow", "primary"),
                ("logs-stop", "Stop", "default"),
            )
            yield Static(
                "  the minecraft unit's journal over the tailnet (`mcctl logs --follow`)", classes="note"
            )
        yield OutputPanel("Live log: minecraft", idle="not following: Follow (f) starts the live tail")


class ConsolePane(Pane):
    BINDINGS: ClassVar[list[BindingType]] = [Binding("escape", "leave", "Leave the prompt (then 1-9)")]

    def action_leave(self) -> None:
        self.output.log_widget.focus()

    def compose(self) -> ComposeResult:
        yield OutputPanel("Console (RCON)", idle="type a console command below (e.g. list); Enter sends it")
        yield Input(
            placeholder="console command, e.g. list  (sent over RCON, like `mcctl rcon`)", id="console-input"
        )
        yield Static(
            "Each line runs `mcctl rcon …` on the server (audited there). stop, restart: the Overview's "
            "buttons, which warn players first; whitelist: the Players tab.",
            classes="note",
        )


class PublishPane(Pane):
    KEYS = (
        ("v", "publish-preview", "Preview"),
        ("c", "validate", "Validate"),
        ("u", "publish", "Publish"),
        ("w", "publish-wait", "When empty"),
        ("z", "publish-revert", "Revert"),
    )
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="actions"):
            yield from _buttons(
                ("publish-preview", "Preview", "default"),
                ("validate", "Validate", "default"),
                ("publish", "Publish (pull request)…", "primary"),
                ("publish-wait", "Publish, restart when empty…", "default"),
                ("publish-revert", "Revert the last deploy…", "warning"),
            )
        yield OutputPanel(
            "Publish",
            idle="Preview shows what your uncommitted and unpushed changes would do (restart? Terraform?)",
        )


class HealthPane(Pane):
    KEYS = (("h", "doctor", "Run doctor"),)
    BINDINGS = _keys(KEYS)

    def compose(self) -> ComposeResult:
        with Horizontal(classes="actions"):
            yield from _buttons(("doctor", "Run doctor", "primary"))
            yield Static("", id="health-summary", classes="note")
        yield DataTable(id="health-table", cursor_type="row", zebra_stripes=True)


# --- the actions --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Action:
    key: str
    title: str  # the command palette's text (fuzzy-searched)
    help: str
    tab: str = ""  # "": a modal (or the terminal)


ACTIONS: tuple[Action, ...] = (
    Action("refresh", "Status: refresh now", "ask the server and GitHub again", "overview"),
    Action("restart", "Server: restart", "in-game countdown, then a restart (like the panel)", "overview"),
    Action(
        "stop",
        "Server: stop on purpose",
        "stays down until Start (self-heal and deploys leave it)",
        "overview",
    ),
    Action("start", "Server: start", "after a stop on purpose", "overview"),
    Action(
        "backup-now", "Server: back up now", "mc-backup.service now; players are not interrupted", "backups"
    ),
    Action("player-add", "Players: whitelist a player", "players.yml through a pull request", "players"),
    Action(
        "player-remove", "Players: remove from the whitelist", "players.yml through a pull request", "players"
    ),
    Action("player-op", "Players: make operator", "players.yml through a pull request", "players"),
    Action("player-deop", "Players: remove operator", "players.yml through a pull request", "players"),
    Action("kick", "Players: kick", "live, over RCON", "players"),
    Action("ban", "Players: ban", "live, over RCON (banned-players.json)", "players"),
    Action("pardon", "Players: pardon (unban)", "live, over RCON", "players"),
    Action("worlds", "Worlds: reload the list", "the active world, archives, the library", "worlds"),
    Action(
        "world-create",
        "Worlds: create a world",
        "typed `create NAME`; the active world is archived first",
        "worlds",
    ),
    Action("world-switch", "Worlds: switch world", "typed `switch to NAME`; countdown restart", "worlds"),
    Action("world-delete", "Worlds: delete an archived world", "typed `delete NAME`", "worlds"),
    Action("plugins", "Plugins: reload the list", "the deployed lock's plugins", "plugins"),
    Action("plugin-enable", "Plugins: enable a plugin", "typed `enable NAME`; countdown restart", "plugins"),
    Action(
        "plugin-disable", "Plugins: disable a plugin", "typed `disable NAME`; countdown restart", "plugins"
    ),
    Action("backups", "Backups: list snapshots", "restic's snapshots (newest last)", "backups"),
    Action(
        "restore",
        "Backups: restore a snapshot",
        "typed `restore <8 hex>`; a safety snapshot first",
        "backups",
    ),
    Action("logs-follow", "Logs: follow the server log", "`mcctl logs --follow`", "logs"),
    Action("logs-stop", "Logs: stop following", "", "logs"),
    Action("console", "Console: send an RCON command", "", "console"),
    Action("publish-preview", "Publish: preview changes", "restart? Terraform? who is online", "publish"),
    Action("validate", "Publish: validate the config repo", "`mcctl validate`", "publish"),
    Action(
        "publish",
        "Publish: open the pull request",
        "preview, validate, pull request, checks, your merge",
        "publish",
    ),
    Action(
        "publish-wait",
        "Publish: restart only when the server is empty",
        "`mcctl publish --wait-empty`",
        "publish",
    ),
    Action("publish-revert", "Publish: revert the last deploy", "a revert pull request", "publish"),
    Action(
        "doctor",
        "Health: run doctor",
        "tools, credentials (live), DNS, tailnet, watchdog, versions",
        "health",
    ),
    Action("metrics", "Metrics: player experience, last 7 days", "`mcctl metrics summary`"),
    Action("upgrade-preview", "Updates: product update preview", "`mcctl upgrade --dry-run`"),
    Action(
        "rollout-preview", "Updates: rollout preview", "`mcctl rollout --dry-run`: the latest release's steps"
    ),
    Action("lock-preview", "Updates: mod lock update preview", "`mcctl lock update --dry-run`"),
    Action("config-get", "Config: show a setting", "`mcctl config get PATH`"),
    Action("dashlane", "Secrets: Dashlane items", "which items .env's credentials need (names only)"),
    Action("switch-repo", "Config repo: switch", "a remembered config repo, or a path"),
    Action("term-ssh", "Terminal: ssh to the server", "Tailscale SSH (the dashboard waits)"),
    Action(
        "term-rotate",
        "Terminal: rotate a secret",
        "`mcctl secrets rotate NAME` (typed confirmations, Terraform)",
    ),
    Action("term-restic", "Terminal: show the restic password", "printed to the terminal only, once"),
    Action(
        "term-doctor-fix",
        "Terminal: doctor --fix",
        "fixes what is safe; may offer to move a stale pin (typed)",
    ),
    Action(
        "term-publish",
        "Terminal: publish interactively",
        "`mcctl publish` with its own questions (Terraform apply)",
    ),
    Action("term-console", "Terminal: console prompt", "`mcctl console`: a prompt sending each line"),
    Action(
        "term-rollout",
        "Terminal: roll out the latest release",
        "`mcctl rollout`: pin PR, your typed merge, the deploy, then plan and apply (typed)",
    ),
)
ACTION_KEYS = {a.key: a for a in ACTIONS}
NEEDS_SERVER = {
    "restart", "stop", "start", "backup-now", "kick", "ban", "pardon", "worlds", "world-create",
    "world-switch", "world-delete", "plugins", "plugin-enable", "plugin-disable", "backups", "restore",
    "logs-follow", "console",
    "term-ssh", "term-console",
}  # fmt: skip
ROTATE_NAMES = ("rcon", "heartbeat", "agent-token", "command-hmac", "r2", "restic-key")


# --- the app ------------------------------------------------------------------------------------------------


class Dashboard(App[int]):
    TITLE = "mcctl"
    CSS = """
    Screen { background: $background; }
    #top { height: 3; padding: 0 1; background: $panel; border-bottom: tall $primary; }
    #top .ident { width: 1fr; height: 3; }
    #top #name { text-style: bold; color: $primary; }
    #top #sub { color: $text-muted; }
    #top #badge { width: auto; min-width: 14; height: 1; margin: 1 1 0 0; padding: 0 1; text-style: bold;
                  content-align: center middle; background: $boost; color: $text-muted; }
    #top #badge.-ok { background: $success 25%; color: $success; }
    #top #badge.-warn { background: $warning 25%; color: $warning; }
    #top #badge.-bad { background: $error 25%; color: $error; }
    #top #repo { width: auto; max-width: 50%; color: $text-muted; margin: 1 0 0 0; }
    TabbedContent { height: 1fr; }
    TabPane { padding: 0 1; }
    #cards { grid-size: 3 2; grid-columns: 1fr 1fr 1fr; grid-rows: auto auto; grid-gutter: 0 1;
             height: auto; }
    .perf { height: auto; min-height: 5; border: round $panel-lighten-2; background: $surface; }
    .perf Card { border: none; min-height: 1; }
    .perf Sparkline { height: 2; margin: 0 1; }
    #spark-tps > .sparkline--max-color { color: $success; }
    #spark-tps > .sparkline--min-color { color: $warning; }
    #spark-mspt > .sparkline--max-color { color: $warning; }
    #spark-mspt > .sparkline--min-color { color: $success; }
    .actions { height: auto; margin: 1 0 0 0; }
    .actions Button { margin-right: 1; min-width: 10; height: 1; border: none; padding: 0 1; }
    .note { color: $text-muted; height: auto; }
    DataTable { height: 1fr; min-height: 5; }
    #health-table { height: 1fr; }
    #console-input { margin-top: 1; }
    FormModal, ConfirmModal, RepoModal, OutputModal { align: center middle; background: $background 60%; }
    .dialog { width: 84; max-width: 100%; height: auto; max-height: 90%; border: thick $primary;
              background: $surface; padding: 1 2; }
    .dialog .dtitle { text-style: bold; color: $primary; margin-bottom: 1; }
    .dialog .dintro { margin-bottom: 1; }
    .dialog .flabel { margin-top: 1; color: $text-muted; }
    .dialog .derror { color: $error; text-style: bold; height: auto; margin-top: 1; }
    .dialog .dbuttons { height: 3; margin-top: 1; align-horizontal: right; }
    .dialog .dbuttons Button { margin-left: 2; }
    .dialog .want { color: $warning; text-style: bold; }
    """
    BINDINGS: ClassVar[list[BindingType]] = [
        *(Binding(str(i), f"tab('{tid}')", label, show=False) for i, (tid, label) in enumerate(TABS, 1)),
        # alt+1-9 work from anywhere, a focused input (the console) too: priority, and no input uses them
        *(
            Binding(
                f"alt+{i}",
                f"tab('{tid}')",
                "Tabs" if i == 1 else label,
                show=i == 1,
                key_display="alt+1-9" if i == 1 else None,
                priority=True,
            )
            for i, (tid, label) in enumerate(TABS, 1)
        ),
        Binding("ctrl+r", "act('refresh')", "Refresh"),
        Binding("ctrl+o", "act('switch-repo')", "Config repo"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        launch: home.Launch,
        *,
        services: Services | None = None,
        cwd: Path | None = None,
        refresh_s: float = REFRESH_S,
    ) -> None:
        super().__init__()
        for theme in THEMES:
            self.register_theme(theme)
        self.theme = THEMES[0].name
        self.services = services or Services()
        self.refresh_s = refresh_s
        self.root, self.source = home._initial(launch, cwd or Path.cwd())
        self.repo: configrepo.ConfigRepo | None = None
        self.repo_error = ""
        self.pin: configrepo.Pin | None = None
        self.status: remote.Status | str | None = None
        self.probing = False
        self.tps: deque[float] = deque(maxlen=HISTORY)
        self.mspt: deque[float] = deque(maxlen=HISTORY)
        self.update_text: str | None = None
        self.versions_row: doctor.Result | None = None  # the last doctor's "versions" (the Worker's stamp)
        self.loaded: set[str] = set()  # tabs whose lists were loaded once
        self.worlds_inv: dict[str, Any] = {}
        self.plugin_list: list[dict[str, Any]] = []
        self.snapshot_list: list[dict[str, Any]] = []
        self.doctor_report: doctor.Report | None = None
        self.load_repo()

    # --- layout -----------------------------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="top"):
            with Vertical(classes="ident"):
                yield Static("", id="name")
                yield Static("", id="sub")
            yield Static("", id="repo")
            yield Static("CHECKING", id="badge")
        with TabbedContent(initial="overview"):
            yield OverviewPane("1 Overview", id="overview")
            yield PlayersPane("2 Players", id="players")
            yield WorldsPane("3 Worlds", id="worlds")
            yield PluginsPane("4 Plugins", id="plugins")
            yield BackupsPane("5 Backups", id="backups")
            yield LogsPane("6 Logs", id="logs")
            yield ConsolePane("7 Console", id="console")
            yield PublishPane("8 Publish", id="publish")
            yield HealthPane("9 Health", id="health")
        yield Footer()

    def on_mount(self) -> None:
        self._setup_tables()
        self.render_all()
        self.set_interval(self.refresh_s, self.refresh_status)
        self.refresh_status()
        if self.services.update is not None:
            self.check_update()
        self.call_after_refresh(self.pane("overview").focus_first)

    def _setup_tables(self) -> None:
        cols = {
            "players-table": ("Player", "Whitelist", "Operator", "Online", "UUID"),
            "worlds-table": (
                "World",
                "State",
                "Since / archived",
                "Size",
                "Data version",
                "Type · preset · seed",
            ),
            "plugins-table": ("Plugin", "Version", "State"),
            "backups-table": ("Snapshot", "Time", "Tags", "Note"),
            "health-table": ("", "Check", "Detail"),
        }
        for tid, names in cols.items():
            self.query_one(f"#{tid}", DataTable).add_columns(*names)

    def pane(self, tab: str) -> Pane:
        return self.query_one(f"#{tab}", Pane)

    # --- the config repo -----------------------------------------------------------------------------------

    def load_repo(self) -> None:
        self.repo, self.repo_error, self.pin = None, "", None
        if self.root is None:
            self.repo_error = "no config repo: ctrl+o (switch), or create one with `mcctl init`"
            return
        self.pin, _ = home._pin_text(self.root)
        try:
            self.repo = configrepo.load(self.root)
        except configrepo.ConfigError as e:
            self.repo_error = f"the config repo doesn't load ({len(e.errors)} error(s)): Publish > Validate"
        except (OSError, ValueError) as e:
            self.repo_error = f"the config repo doesn't load: {e}"

    def open_repo(self, path: Path) -> None:
        try:
            homecfg.remember(path)
        except OSError as e:
            self.notify(f"couldn't remember {path}: {e}", severity="warning")
        self.root, self.source = path, "remembered"
        self.load_repo()
        self.status, self.versions_row, self.doctor_report = None, None, None
        self.tps.clear()
        self.mspt.clear()
        self.loaded.clear()
        self.worlds_inv, self.plugin_list, self.snapshot_list = {}, [], []
        for tid in ("worlds-table", "plugins-table", "backups-table", "health-table"):
            self.query_one(f"#{tid}", DataTable).clear()
        self.render_all()
        self.refresh_status(force=True)
        self.notify(f"config repo: {path}")

    @property
    def cfg(self) -> ServerConfig | None:
        return self.repo.server if self.repo is not None else None

    # --- rendering ----------------------------------------------------------------------------------------

    def c(self, level: str) -> str:
        return colour(self, level)

    def render_all(self) -> None:
        self.render_header()
        self.render_overview()
        self.render_players()

    def render_header(self) -> None:
        name = self.repo.server.name if self.repo else ("no config repo" if self.root is None else "?")
        self.query_one("#name", Static).update(name)
        product = (
            f"Smallworld {self.pin.version or self.pin.sha[:12]}" if self.pin else "Smallworld (pin unknown)"
        )
        sub = " · ".join(p for p in (product, server_software(self.repo), f"mcctl {__version__}") if p)
        self.query_one("#sub", Static).update(sub if not self.repo_error else f"{sub} · {self.repo_error}")
        self.query_one("#repo", Static).update(f"{self.root} ({self.source})" if self.root else "")
        text, level = server_state(self.status)
        badge = self.query_one("#badge", Static)
        badge.update(f"● {text}")
        badge.set_classes(f"-{level}" if level != "muted" else "")
        self.sub_title = name

    def _kv(self, rows: Iterable[tuple[str, str | Text]]) -> Table:
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style=self.c("muted"), no_wrap=True)
        grid.add_column()
        for k, v in rows:
            grid.add_row(k, v)
        return grid

    def _lv(self, level: str, text: str) -> Text:
        return Text(text, style=self.c(level))

    def render_overview(self) -> None:
        st = self.status
        q = self.query_one
        if self.repo is None:
            msg = self.repo_error
            for cid in ("card-server", "card-tps", "card-mspt", "card-ops", "card-versions"):
                q(f"#{cid}", Card).update(Text(msg if cid == "card-server" else "-", style=self.c("muted")))
            self.render_attention()
            return
        if st is None:
            server: list[tuple[str, str | Text]] = [
                ("state", self._lv("muted", "asking the server over the tailnet…"))
            ]
        elif isinstance(st, str):
            server = [("state", self._lv("bad", f"unknown ({st})"))]
        elif not st.reachable:
            server = [("state", self._lv("bad", f"unreachable ({st.error})"))]
        else:
            text, level = server_state(st)
            server = [
                (
                    "state",
                    self._lv(level, text.lower() + (" (stopped by an admin)" if st.admin_stopped else "")),
                )
            ]
            count = f"{_num(st.players_online, '{}')}/{_num(st.players_max, '{}')}"
            server.append(("players", count))
            server.append(("online", ", ".join(st.players) if st.players else self._lv("muted", "nobody")))
            if st.reboot_required:
                server.append(("reboot", self._lv("warn", "required")))
        q("#card-server", Card).update(self._kv(server))
        live = isinstance(st, remote.Status) and st.reachable
        tps = st.tps if live else None  # type: ignore[union-attr]
        mspt = st.mspt if live else None  # type: ignore[union-attr]
        tps_level = "muted" if tps is None else ("ok" if tps >= 19 else "warn" if tps >= 15 else "bad")
        mspt_level = "muted" if mspt is None else ("ok" if mspt < 40 else "warn" if mspt < 50 else "bad")
        q("#card-tps", Card).update(
            Text.assemble((_num(tps), f"bold {self.c(tps_level)}"), ("  of 20", self.c("muted")))
        )
        q("#card-mspt", Card).update(
            Text.assemble(
                (_num(mspt), f"bold {self.c(mspt_level)}"), (" ms  (under 50 = full speed)", self.c("muted"))
            )
        )
        q("#spark-tps", Sparkline).data = list(self.tps)
        q("#spark-mspt", Sparkline).data = list(self.mspt)
        ops: list[tuple[str, str | Text]] = []
        if isinstance(st, remote.Status):
            if st.reachable:
                good = st.last_backup_result in ("success", "ok")
                when = st.last_backup or "never"
                ops.append(
                    (
                        "last backup",
                        self._lv("ok" if good else "warn", f"{when} ({st.last_backup_result or 'n/a'})"),
                    )
                )
            run = st.last_main_run
            if run:
                result = run.get("conclusion") or run.get("status") or "?"
                ops.append(("last deploy", self._lv("ok" if result == "success" else "warn", f"{result}")))
                ops.append(
                    ("", self._lv("muted", f"{run.get('createdAt', '?')} {str(run.get('headSha', ''))[:12]}"))
                )
            else:
                ops.append(("last deploy", self._lv("muted", home.deploy_summary(st))))
        else:
            ops.append(("", self._lv("muted", "waiting for the status…")))
        q("#card-ops", Card).update(self._kv(ops))
        self.render_versions()
        self.render_attention()

    def render_versions(self) -> None:
        st = self.status
        rows: list[tuple[str, str | Text]] = []
        pin = self.pin
        rows.append(("pin", f"{pin.version or '?'} ({pin.sha[:12]})" if pin else self._lv("warn", "unknown")))
        product = st.product if isinstance(st, remote.Status) else None
        if product:
            drift = remote.product_drift(product, pin)
            rows.append(("server", self._lv("warn" if drift else "ok", remote.product_text(product))))
            if drift:
                rows.append(("", self._lv("warn", f"⚠ {drift}")))
        elif isinstance(st, remote.Status) and st.reachable:
            rows.append(("server", self._lv("warn", "no version (deployed before v1.3? deploy)")))
        else:
            rows.append(("server", self._lv("muted", "-")))
        if self.versions_row is not None:
            detail = self.versions_row.detail
            worker = detail.split("Worker ", 1)[1].split(",", 1)[0] if "Worker " in detail else "?"
            rows.append(("Worker", self._lv("ok" if self.versions_row.level == "ok" else "warn", worker)))
            if self.versions_row.level != "ok" and "; " in detail:
                rows.append(("", self._lv("warn", "⚠ " + detail.split("; ", 1)[1])))
        else:
            rows.append(("Worker", self._lv("muted", "Health > Run doctor reads its stamp")))
        mark = ""
        if pin and pin.version and pin.version.lstrip("v") != __version__:
            mark = " (deploys run the pinned version)"
        rows.append(("this mcctl", f"{__version__}{mark}"))
        self.query_one("#card-versions", Card).update(self._kv(rows))

    def render_attention(self) -> None:
        items: list[Text] = []
        st = self.status
        if self.repo_error:
            items.append(self._lv("bad", self.repo_error))
        if isinstance(st, remote.Status):
            for pr in st.pending_panel_prs:
                items.append(
                    self._lv(
                        "warn", f"panel PR #{pr.get('number')}: {pr.get('title') or pr.get('headRefName')}"
                    )
                )
            if st.disabled_plugins:
                items.append(self._lv("warn", f"disabled plugins: {', '.join(st.disabled_plugins)}"))
            items += [self._lv("muted", n) for n in st.notes]
        if self.repo is not None:
            items += [
                self._lv(level, f"credential {text}")
                for level, text in expiring(self.repo, datetime.date.today())
            ]
        if self.update_text:
            items.append(self._lv("accent", self.update_text))
        if not items:
            items.append(self._lv("ok", "nothing: no panel PRs, nothing expiring, no update"))
        body = Text("\n").join(Text("• ") + t for t in items)
        self.query_one("#card-attention", Card).update(body)

    def render_players(self) -> None:
        table = self.query_one("#players-table", DataTable)
        table.clear()
        note = self.query_one("#players-note", Static)
        if self.repo is None:
            note.update(self.repo_error)
            return
        online = set(self.status.players) if isinstance(self.status, remote.Status) else set()
        known = isinstance(self.status, remote.Status) and self.status.reachable
        ops = {o.name.lower(): o for o in self.repo.players.ops}
        names: dict[str, str] = {}
        for p in self.repo.players.whitelist:
            names[p.name.lower()] = p.name
        for o in self.repo.players.ops:
            names.setdefault(o.name.lower(), o.name)
        for n in online:
            names.setdefault(n.lower(), n)
        listed = {p.name.lower(): p for p in self.repo.players.whitelist}
        order = sorted(names, key=lambda k: (k not in {n.lower() for n in online}, k))
        for key in order:
            p = listed.get(key)
            op = ops.get(key)
            on = names[key] in online
            table.add_row(
                Text(names[key], style="bold" if on else ""),
                Text("yes", style=self.c("ok")) if p else Text("no", style=self.c("warn")),
                f"level {op.level}" if op else "",
                Text("● online", style=self.c("ok"))
                if on
                else Text("-" if known else "?", style=self.c("muted")),
                Text(p.uuid if p else "", style=self.c("muted")),
                key=key,
            )
        wl, n_ops = len(self.repo.players.whitelist), len(self.repo.players.ops)
        note.update(
            f"{wl} whitelisted, {n_ops} operator(s), {len(online)} online · players.yml changes open a pull "
            "request (like `mcctl players`); kick, ban and pardon act live"
        )

    def render_worlds(self) -> None:
        table = self.query_one("#worlds-table", DataTable)
        table.clear()
        inv = self.worlds_inv
        active = str(inv.get("active") or "?")
        table.add_row(
            Text(active, style="bold"),
            Text("● active", style=self.c("ok")),
            str(inv.get("since") or ""),
            "",
            str(inv.get("data_version") or ""),
            "",
            key=f"active:{active}",
        )
        for a in inv.get("archives") or []:
            table.add_row(
                str(a.get("name")),
                "archived",
                str(a.get("time") or ""),
                actions._size(a.get("size")),
                str(a.get("data_version") or ""),
                "",
                key=f"archive:{a.get('name')}",
            )
        for e in inv.get("library") or []:
            if e.get("name") == active:
                continue
            detail = f"{e.get('type')} · {e.get('preset') or '-'} · {e.get('seed') or 'random'}"
            table.add_row(
                str(e.get("name")),
                Text("library", style=self.c("muted")),
                "",
                "",
                "",
                detail,
                key=f"library:{e.get('name')}",
            )
        notes = []
        if inv.get("deleted"):
            notes.append(f"recently deleted (names not reusable yet): {', '.join(inv['deleted'])}")
        if pend := inv.get("pending"):
            notes.append(
                f"pending for server.yml: {pend.get('what')}"
                + (f" (PR #{pend['pr']})" if pend.get("pr") else "")
            )
        if inv.get("storage_note"):
            notes.append("the archives are above R2's free storage (10 GB): delete worlds you don't need")
        self.query_one("#worlds-note", Static).update(
            " · ".join(notes) or "switch and delete take the selected row's name"
        )

    def render_plugins(self) -> None:
        table = self.query_one("#plugins-table", DataTable)
        table.clear()
        for p in self.plugin_list:
            on = bool(p.get("enabled"))
            table.add_row(
                str(p.get("name")),
                str(p.get("version") or ""),
                Text("● enabled", style=self.c("ok")) if on else Text("○ disabled", style=self.c("warn")),
                key=str(p.get("name")),
            )
        self.query_one("#plugins-note", Static).update(
            f"{len(self.plugin_list)} plugin(s) in the deployed lock · enable/disable restart the server "
            "after a countdown"
            if self.plugin_list
            else "no plugins in the deployed lock"
        )

    def render_backups(self) -> None:
        table = self.query_one("#backups-table", DataTable)
        table.clear()
        for s in reversed(self.snapshot_list):  # newest first
            tags = s.get("tags")
            tag_text = ",".join(str(t) for t in tags) if isinstance(tags, list) else ""
            note = (
                Text("safety snapshot (undo of a restore)", style=self.c("warn"))
                if "pre-restore" in tag_text
                else ""
            )
            table.add_row(
                str(s.get("short_id") or str(s["id"])[:8]),
                str(s.get("time") or ""),
                tag_text,
                note,
                key=str(s["id"]),
            )
        self.query_one("#backups-note", Static).update(
            f"{len(self.snapshot_list)} snapshot(s), newest first · Restore takes the selected one (typed)"
        )

    def render_health(self) -> None:
        table = self.query_one("#health-table", DataTable)
        table.clear()
        report = self.doctor_report
        if report is None:
            return
        for r in report.results:
            level = {"ok": "ok", "warn": "warn", "fail": "bad"}.get(r.level, "muted")
            mark = {"ok": "✓ ok", "warn": "! warn", "fail": "✗ fail"}.get(r.level, r.level)
            table.add_row(Text(mark, style=f"bold {self.c(level)}"), Text(r.name, style="bold"), r.detail)
        summary = self.query_one("#health-summary", Static)
        summary.update(Text("  " + report.counts(), style=self.c("bad" if report.failed else "ok")))

    # --- the status ----------------------------------------------------------------------------------------

    def refresh_status(self, force: bool = False) -> None:
        cfg = self.cfg
        if cfg is None or self.probing:
            return
        self.probing = True
        self._probe(cfg)

    @work(thread=True, group="status")
    def _probe(self, cfg: ServerConfig) -> None:
        try:
            st: remote.Status | str = self.services.status(cfg)
        except Exception as e:
            st = f"{type(e).__name__}: {e}"
        self._from_thread(self._status_arrived, cfg, st)

    def _status_arrived(self, cfg: ServerConfig, st: remote.Status | str) -> None:
        self.probing = False
        if cfg is not self.cfg:  # the config repo changed meanwhile
            return
        self.status = st
        if isinstance(st, remote.Status) and st.reachable:
            if st.tps is not None:
                self.tps.append(st.tps)
            if st.mspt is not None:
                self.mspt.append(st.mspt)
        self.render_header()
        self.render_overview()
        self.render_players()

    @work(thread=True, group="update")
    def check_update(self) -> None:
        assert self.services.update is not None
        try:
            text = self.services.update(self.pin)
        except Exception:
            text = None
        self._from_thread(self._update_arrived, text)

    def _update_arrived(self, text: str | None) -> None:
        self.update_text = text
        self.render_attention()

    def _from_thread(self, fn: Callable[..., Any], *args: Any) -> None:  # noqa: ANN401
        try:
            self.call_from_thread(fn, *args)
        except RuntimeError:  # the app has exited meanwhile
            pass

    # --- tabs ------------------------------------------------------------------------------------------

    def action_tab(self, tab: str) -> None:
        self.query_one(TabbedContent).active = tab

    @on(TabbedContent.TabActivated)
    def _activated(self, event: TabbedContent.TabActivated) -> None:
        pane = event.pane
        tab = str(pane.id)
        if isinstance(pane, Pane):
            self.call_after_refresh(pane.focus_first)
        if tab in self.loaded:
            return
        first = {"worlds": "worlds", "plugins": "plugins", "backups": "backups", "publish": "publish-preview"}
        if tab in first and self._can_run(first[tab], quiet=True):
            self.loaded.add(tab)
            self.trigger(first[tab])

    @on(Button.Pressed)
    def _button(self, event: Button.Pressed) -> None:
        if event.button.name in ACTION_KEYS:
            event.stop()
            self.trigger(event.button.name)

    def action_act(self, name: str) -> None:
        self.trigger(name)

    # --- the command palette ---------------------------------------------------------------------------

    def get_system_commands(self, screen: Screen[Any]) -> Iterable[SystemCommand]:
        for a in ACTIONS:
            yield SystemCommand(a.title, a.help or a.title, lambda key=a.key: self.trigger(key))
        for i, (tid, label) in enumerate(TABS, 1):
            yield SystemCommand(
                f"Go to: {label}", f"tab {i}", lambda tid=tid: self.action_tab(tid), discover=False
            )
        yield from super().get_system_commands(screen)

    # --- actions ---------------------------------------------------------------------------------------

    def _can_run(self, key: str, *, quiet: bool = False) -> bool:
        problem = ""
        if self.root is None:
            problem = "no config repo yet: ctrl+o (switch), or create one with `mcctl init`"
        elif key in NEEDS_SERVER and self.cfg is None:
            problem = self.repo_error or "the config repo doesn't load"
        if problem and not quiet:
            self.notify(
                problem, severity="error", title=ACTION_KEYS[key].title if key in ACTION_KEYS else key
            )
        return not problem

    def trigger(self, key: str) -> None:
        action = ACTION_KEYS[key]
        if key == "switch-repo":
            self.push_screen(RepoModal(self.root), lambda p: self.open_repo(p) if p else None)
            return
        if not self._can_run(key):
            return
        if action.tab and self.query_one(TabbedContent).active != action.tab:
            self.action_tab(action.tab)
        getattr(self, "do_" + key.replace("-", "_"))()

    def run_cli(
        self,
        argv: Sequence[str],
        panel: OutputPanel,
        *,
        title: str | None = None,
        clear: bool = True,
        after: Callable[[int], None] | None = None,
    ) -> bool:
        """Stream `mcctl ARGV -C ROOT` into panel (refused while the panel still runs something)."""
        if panel.busy:
            self.notify("still running: wait for it to finish, or Stop it", severity="warning")
            return False
        assert self.root is not None
        shown = title or "mcctl " + shlex.join(argv)
        panel.start(shown, clear=clear)
        self._stream(panel, [*argv, "-C", str(self.root)], after)
        return True

    @work(thread=True, group="actions")
    def _stream(self, panel: OutputPanel, argv: list[str], after: Callable[[int], None] | None) -> None:
        def on_line(line: str) -> None:
            self._from_thread(panel.line, line)

        def on_start(stop: Callable[[], None]) -> None:
            self._from_thread(panel.set_stop, stop)

        try:
            code = self.services.spawn(argv, on_line, on_start)
        except Exception as e:
            on_line(f"{type(e).__name__}: {e}")
            code = 1
        self._from_thread(panel.finish, code)
        if after is not None:
            self._from_thread(after, code)

    def ask(
        self,
        title: str,
        fields: Sequence[Field],
        then: Callable[[dict[str, str]], None],
        *,
        intro: str = "",
        ok: str = "Run",
    ) -> None:
        self.push_screen(
            FormModal(title, fields, intro=intro, ok=ok), lambda v: then(v) if v is not None else None
        )

    def confirm_then(self, want: str, title: str, body: str, then: Callable[[str], None]) -> None:
        def done(typed: str | None) -> None:
            if typed is None:
                self.notify("not confirmed: nothing was changed", severity="warning")
            else:
                then(typed)

        self.push_screen(ConfirmModal(want, title, body), done)

    def selected(self, table_id: str) -> str:
        """The row key of the table's cursor row ("" when empty)."""
        table = self.query_one(f"#{table_id}", DataTable)
        if table.row_count == 0:
            return ""
        try:
            key = table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value
        except Exception:
            return ""
        return str(key or "")

    def modal_output(self, title: str, argv: Sequence[str]) -> None:
        modal = OutputModal(title)
        self.push_screen(modal)
        self.call_after_refresh(lambda: self.run_cli(argv, modal.panel))

    # Overview: server controls

    def do_refresh(self) -> None:
        if self.probing:
            self.notify("still asking the server…")
        self.refresh_status(force=True)

    def do_restart(self) -> None:
        fields = [countdown_field(), Field("backup", "Back up first", (("no", "no"), ("yes", "yes")), "no")]

        def go(v: dict[str, str]) -> None:
            argv = ["restart", "--countdown", v["countdown"], *(["--backup"] if v["backup"] == "yes" else [])]
            self.run_cli(argv, self.pane("overview").output, after=lambda _: self.refresh_status())

        self.ask(
            "Restart the server",
            fields,
            go,
            intro="Players see the in-game countdown, then the server restarts.",
        )

    def do_stop(self) -> None:
        def go(v: dict[str, str]) -> None:
            argv = ["stop", *(["--reason", v["reason"]] if v["reason"] else [])]
            self.run_cli(argv, self.pane("overview").output, after=lambda _: self.refresh_status())

        self.ask(
            "Stop the server on purpose",
            [Field("reason", "Reason (recorded in the admin-stopped marker)", optional=True)],
            go,
            intro="It stays down until Start: self-heal, the watchdog and deploys leave it stopped.",
            ok="Stop",
        )

    def do_start(self) -> None:
        self.run_cli(["start"], self.pane("overview").output, after=lambda _: self.refresh_status())

    def do_backup_now(self) -> None:
        def after(code: int) -> None:
            if code == 0:
                self.loaded.discard("backups")
                self.do_backups()

        self.run_cli(
            ["backup", "now"], self.pane("backups").output, title="mcctl backup now (15-40 s)", after=after
        )

    # Players

    def _player_field(self) -> Field:
        return Field("name", "Minecraft name", default=self.selected_player(), check=JAVA_NAME_CHECK)

    def selected_player(self) -> str:
        key = self.selected("players-table")
        if not key or self.repo is None:
            return ""
        for p in [*self.repo.players.whitelist, *self.repo.players.ops]:
            if p.name.lower() == key:
                return p.name
        if isinstance(self.status, remote.Status):
            return next((n for n in self.status.players if n.lower() == key), "")
        return ""

    def _players_pr(self, verb: str, title: str, extra: Sequence[Field] = ()) -> None:
        def go(v: dict[str, str]) -> None:
            argv = ["players", verb, v["name"]]
            if verb == "op":
                argv += ["--level", v["level"]]
            argv.append("--merge" if v["merge"] == "yes" else "--no-merge")

            def after(code: int) -> None:
                self.load_repo()
                self.render_players()

            self.run_cli(argv, self.pane("players").output, after=after)

        self.ask(
            title,
            [self._player_field(), *extra, merge_field()],
            go,
            intro="players.yml on a players/* branch, a pull request, its checks; the deploy applies it.",
        )

    def do_player_add(self) -> None:
        self._players_pr("add", "Whitelist a player")

    def do_player_remove(self) -> None:
        self._players_pr("remove", "Remove from the whitelist (and the operators)")

    def do_player_op(self) -> None:
        level = Field("level", "Permission level", (("4", "4"), ("3", "3"), ("2", "2"), ("1", "1")), "4")
        self._players_pr("op", "Make operator (whitelists them too)", [level])

    def do_player_deop(self) -> None:
        self._players_pr("deop", "Remove operator")

    def _moderate(self, verb: str, title: str) -> None:
        fields = [self._player_field()]
        if verb != "pardon":
            fields.append(Field("reason", "Reason (shown to the player)", optional=True))

        def go(v: dict[str, str]) -> None:
            argv = ["players", verb, v["name"], *(["--reason", v["reason"]] if v.get("reason") else [])]
            self.run_cli(argv, self.pane("players").output, after=lambda _: self.refresh_status())

        self.ask(
            title,
            fields,
            go,
            intro="Live, over RCON (audited on the server); not in players.yml.",
            ok=verb.title(),
        )

    def do_kick(self) -> None:
        self._moderate("kick", "Kick a player")

    def do_ban(self) -> None:
        self._moderate("ban", "Ban a player")

    def do_pardon(self) -> None:
        self._moderate("pardon", "Pardon (unban) a player")

    # Worlds

    def do_worlds(self) -> None:
        cfg = self.cfg
        assert cfg is not None
        self.loaded.add("worlds")
        self.query_one("#worlds-note", Static).update("asking the server for its worlds…")
        self._load_list("worlds", cfg)

    @work(thread=True, group="lists")
    def _load_list(self, what: str, cfg: ServerConfig) -> None:
        try:
            data: Any = self.services.snapshots(cfg) if what == "backups" else self.services.query(cfg, what)
            error = ""
        except Exception as e:
            data, error = None, str(e)
        self._from_thread(self._list_arrived, what, data, error)

    def _list_arrived(self, what: str, data: Any, error: str) -> None:  # noqa: ANN401
        if error:
            self.loaded.discard(what)
            self.query_one(f"#{what}-note", Static).update(
                Text(f"couldn't load: {error}", style=self.c("bad"))
            )
            return
        if what == "worlds":
            self.worlds_inv = data if isinstance(data, dict) else {}
            self.render_worlds()
        elif what == "plugins":
            self.plugin_list = [p for p in data or [] if isinstance(p, dict)]
            self.render_plugins()
        else:
            self.snapshot_list = list(data or [])
            self.render_backups()

    def selected_world(self) -> str:
        key = self.selected("worlds-table")
        return key.split(":", 1)[1] if ":" in key else ""

    def _world_done(self, code: int) -> None:
        self.do_worlds()

    def do_world_create(self) -> None:
        fields = [
            Field("name", "New world name (lowercase letters, digits, _ or -)", check=WORLD_NAME_CHECK),
            Field("type", "World type", tuple((t, t) for t in actions.WORLD_TYPES), "default"),
            Field("preset", "Preset", tuple((p, p) for p in actions.WORLD_PRESETS), "survival_smp"),
            Field(
                "seed",
                "Seed (empty: random)",
                optional=True,
                check=_regex_check(actions.SEED, "a whole number"),
            ),
            countdown_field(),
        ]

        def go(v: dict[str, str]) -> None:
            name = v["name"]
            want = actions.confirm_phrase("world_create", name)
            argv = ["worlds", "create", name, "--type", v["type"], "--preset", v["preset"]]
            argv += [*(["--seed", v["seed"]] if v["seed"] else []), "--countdown", v["countdown"]]
            body = f"The active world is archived first, then the server restarts into {name}."
            self.confirm_then(
                want,
                f"Create world {name}",
                body,
                lambda typed: self.run_cli(
                    [*argv, "--confirm", typed], self.pane("worlds").output, after=self._world_done
                ),
            )

        self.ask("Create a world", fields, go, ok="Next")

    def do_world_switch(self) -> None:
        def go(v: dict[str, str]) -> None:
            name = v["name"]
            argv = ["worlds", "switch", name, "--countdown", v["countdown"]]
            active = self.worlds_inv.get("active") or ""
            body = f"The active world {active} is archived first (the safety snapshot)."
            self.confirm_then(
                actions.confirm_phrase("world_switch", name),
                f"Switch to {name}",
                body,
                lambda typed: self.run_cli(
                    [*argv, "--confirm", typed], self.pane("worlds").output, after=self._world_done
                ),
            )

        name = Field("name", "Archived world", default=self.selected_world(), check=WORLD_NAME_CHECK)
        self.ask("Switch world", [name, countdown_field()], go, ok="Next")

    def do_world_delete(self) -> None:
        def go(v: dict[str, str]) -> None:
            name = v["name"]
            self.confirm_then(
                actions.confirm_phrase("world_delete", name),
                f"Delete world {name}",
                "Its archive snapshots are forgotten (once the bucket lock allows). The active world "
                "can't be deleted.",
                lambda typed: self.run_cli(
                    ["worlds", "delete", name, "--confirm", typed],
                    self.pane("worlds").output,
                    after=self._world_done,
                ),
            )

        name = Field("name", "Archived world", default=self.selected_world(), check=WORLD_NAME_CHECK)
        self.ask("Delete an archived world", [name], go, ok="Next")

    # Plugins

    def do_plugins(self) -> None:
        cfg = self.cfg
        assert cfg is not None
        self.loaded.add("plugins")
        self.query_one("#plugins-note", Static).update("asking the server for its plugins…")
        self._load_list("plugins", cfg)

    def _plugin(self, enabled: bool) -> None:
        verb = "enable" if enabled else "disable"

        def go(v: dict[str, str]) -> None:
            name = v["name"]
            argv = ["plugins", verb, name, "--countdown", v["countdown"]]
            self.confirm_then(
                actions.confirm_phrase("plugin", name, enabled),
                f"{verb.title()} {name}",
                f"The server restarts after a {v['countdown']} s countdown (skipped when nobody is online).",
                lambda typed: self.run_cli(
                    [*argv, "--confirm", typed],
                    self.pane("plugins").output,
                    after=lambda _: self.do_plugins(),
                ),
            )

        name = Field("name", "Plugin", default=self.selected("plugins-table"), check=PLUGIN_CHECK)
        self.ask(f"{verb.title()} a plugin", [name, countdown_field()], go, ok="Next")

    def do_plugin_enable(self) -> None:
        self._plugin(True)

    def do_plugin_disable(self) -> None:
        self._plugin(False)

    # Backups

    def do_backups(self) -> None:
        cfg = self.cfg
        assert cfg is not None
        self.loaded.add("backups")
        self.query_one("#backups-note", Static).update("asking restic for the snapshots…")
        self._load_list("backups", cfg)

    def do_restore(self) -> None:
        def go(v: dict[str, str]) -> None:
            sid = v["snapshot"].lower()
            want = f"restore {sid[:8]}"  # = restorecmd.live's (and the panel's) typed confirmation
            body = (
                "Maintenance window → a safety snapshot of the current world (the undo) → stop → fetch → "
                "copy in → start. Everyone online is disconnected."
            )
            self.confirm_then(
                want,
                f"Replace the live world with {sid[:8]}",
                body,
                lambda typed: self.run_cli(
                    ["restore", "--live", sid, "--confirm", typed],
                    self.pane("backups").output,
                    after=lambda _: self.do_backups(),
                ),
            )

        snap = Field(
            "snapshot",
            "Snapshot id (8+ hex digits)",
            default=self.selected("backups-table")[:8],
            check=_snapshot_check,
        )
        self.ask("Restore a snapshot (live)", [snap], go, ok="Next")

    # Logs and console

    def do_logs_follow(self) -> None:
        panel = self.pane("logs").output
        self.run_cli(["logs", "--follow", "-n", "200"], panel, title="mcctl logs --follow (Stop ends it)")

    def do_logs_stop(self) -> None:
        panel = self.pane("logs").output
        if panel.busy:
            panel.stop()

    def do_console(self) -> None:
        self.query_one("#console-input", Input).focus()

    @on(Input.Submitted, "#console-input")
    def _console(self, event: Input.Submitted) -> None:
        line = event.value.strip().removeprefix("/")
        if not line or not self._can_run("console"):
            return
        if line.lower() in actions.QUIT_WORDS:
            self.notify("this console stays open: switch tabs, or q to quit")
            event.input.value = ""
            return
        try:
            words = shlex.split(line)
        except ValueError:
            words = line.split()
        panel = self.pane("console").output
        if self.run_cli(["rcon", *words], panel, title=f"rcon {line}", clear=False):
            event.input.value = ""

    # Publish

    def do_publish_preview(self) -> None:
        self.loaded.add("publish")
        self.run_cli(["publish", "--dry-run"], self.pane("publish").output)

    def do_validate(self) -> None:
        def after(code: int) -> None:
            self.load_repo()
            self.render_all()

        self.run_cli(["validate"], self.pane("publish").output, after=after)

    def _publish(self, title: str, base: Sequence[str], *, restart: bool) -> None:
        fields = [merge_field()]
        if restart:
            fields.append(
                Field(
                    "restart",
                    "If the deploy restarts Minecraft with players online",
                    (
                        ("wait until the server is empty (checked each minute)", "wait"),
                        ("deploy now (the in-game countdown)", "now"),
                        ("cancel (nothing is merged)", "cancel"),
                    ),
                    "wait",
                )
            )

        def go(v: dict[str, str]) -> None:
            argv = [*base, "--merge" if v["merge"] == "yes" else "--no-merge"]
            if v.get("restart"):
                argv += ["--restart", v["restart"]]
            self.run_cli(argv, self.pane("publish").output, after=lambda _: self.refresh_status())

        intro = (
            "Preview, validate, a pull request, its checks (shown as they run). A Terraform change needs its "
            "typed apply: use Terminal: publish interactively (ctrl+p)."
        )
        self.ask(title, fields, go, intro=intro, ok="Publish")

    def do_publish(self) -> None:
        self._publish("Publish changes (pull request)", ["publish"], restart=True)

    def do_publish_wait(self) -> None:
        self._publish(
            "Publish; restart only when the server is empty", ["publish", "--wait-empty"], restart=False
        )

    def do_publish_revert(self) -> None:
        self._publish("Revert the last deploy (a pull request)", ["publish", "--revert"], restart=True)

    # Health

    def do_doctor(self) -> None:
        assert self.root is not None
        self.query_one("#health-summary", Static).update(
            Text(
                "  running doctor: tools, credentials (live), DNS, the tailnet, the watchdog…",
                style=self.c("warn"),
            )
        )
        self._doctor(self.root)

    @work(thread=True, exclusive=True, group="doctor")
    def _doctor(self, root: Path) -> None:
        try:
            report: doctor.Report | str = self.services.doctor(root)
        except Exception as e:
            report = f"{type(e).__name__}: {e}"
        self._from_thread(self._doctor_done, report)

    def _doctor_done(self, report: doctor.Report | str) -> None:
        if isinstance(report, str):
            self.query_one("#health-summary", Static).update(
                Text(f"  doctor failed: {report}", style=self.c("bad"))
            )
            return
        self.doctor_report = report
        self.versions_row = next((r for r in report.results if r.name == "versions"), None)
        self.render_health()
        self.render_versions()

    # Modal outputs

    def do_metrics(self) -> None:
        self.modal_output("Player experience: last 7 days", ["metrics", "summary"])

    def do_upgrade_preview(self) -> None:
        self.modal_output("Product update: preview", ["upgrade", "--dry-run"])

    def do_rollout_preview(self) -> None:
        self.modal_output("Rollout: preview", ["rollout", "--dry-run"])

    def do_lock_preview(self) -> None:
        self.modal_output("Mod lock update: preview", ["lock", "update", "--dry-run"])

    def do_dashlane(self) -> None:
        self.modal_output("Dashlane items", ["secrets", "dashlane"])

    def do_config_get(self) -> None:
        self.ask(
            "Show a setting",
            [Field("path", "Dotted path", placeholder="e.g. minecraft.motd")],
            lambda v: self.modal_output(f"config get {v['path']}", ["config", "get", v["path"]]),
            ok="Show",
        )

    # The terminal (suspended dashboard): anything that asks for a credential or needs a real terminal

    def in_terminal(self, argv: Sequence[str]) -> None:
        assert self.root is not None
        full = [*argv, "-C", str(self.root)]
        if self.services.terminal is not None:
            code = self.services.terminal(full)
        else:
            code = self._suspended(full)
        word = "done" if code == 0 else f"exit {code}"
        self.notify(f"mcctl {shlex.join(argv)}: {word}", severity="information" if code == 0 else "error")
        self.load_repo()
        self.render_all()

    def _suspended(self, argv: Sequence[str]) -> int:
        try:
            with self.suspend():
                print(f"\n$ mcctl {shlex.join(argv)}\n", flush=True)
                try:
                    code = subprocess.run(mcctl_argv(argv), check=False).returncode
                except KeyboardInterrupt:
                    code = 130
                try:
                    input(f"\n(exit {code}) Enter returns to the dashboard ")
                except (EOFError, KeyboardInterrupt):
                    pass
                return code
        except Exception as e:
            self.notify(f"can't use the terminal here ({e}): run it outside the dashboard", severity="error")
            return 1

    def do_term_ssh(self) -> None:
        self.in_terminal(["ssh"])

    def do_term_console(self) -> None:
        self.in_terminal(["console"])

    def do_term_rotate(self) -> None:
        self.ask(
            "Rotate a secret (in the terminal)",
            [Field("name", "Secret", tuple((n, n) for n in ROTATE_NAMES), "rcon")],
            lambda v: self.in_terminal(["secrets", "rotate", v["name"]]),
            ok="Open the terminal",
        )

    def do_term_restic(self) -> None:
        self.in_terminal(["secrets", "show-restic"])

    def do_term_doctor_fix(self) -> None:
        self.in_terminal(["doctor", "--fix"])

    def do_term_publish(self) -> None:
        self.in_terminal(["publish"])

    def do_term_rollout(self) -> None:
        self.in_terminal(["rollout"])

    # --- leaving -------------------------------------------------------------------------------------------

    def action_quit(self) -> None:  # type: ignore[override]
        self.stop_children()
        self.exit(0)

    def stop_children(self) -> None:
        for panel in self.query(OutputPanel):
            if panel.busy:
                panel.stop()


def main(launch: home.Launch) -> int:
    from mcctl import notice

    update = None if os.environ.get(notice.OPT_OUT_ENV) else notice.for_pin
    app = Dashboard(launch, services=Services(update=update))
    try:
        code = app.run()
    except KeyboardInterrupt:
        code = 0
    finally:
        app.stop_children()
    return int(code or 0)
