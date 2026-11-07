"""v1.3: plain `mcctl` on an interactive terminal opens a home screen: a status header, an arrow-key menu.

The header is cheap and never blocks: the deployment, the config repo, the pinned product against this mcctl,
then the server (online, players, TPS/MSPT, last deploy, open panel PRs) from `remote.status` in a background
thread with a time budget, cached for `StatusProbe.ttl` seconds. Whatever fails reads "unknown (why)".

Every menu item runs an existing command through `cli.run` (the same parser, the same `cmd_*` function, `-C`
added), so subcommands, scripts, CI and tests are unchanged; then the menu comes back. Esc, Ctrl-C or "Quit"
leave with exit code 0. Items marked "(v1.3, coming)" are shown disabled until their phase lands.

Anything non-interactive keeps today's behaviour (an argparse usage error, exit code 2): `--plain`,
MCCTL_PLAIN, NO_COLOR, CI, TERM=dumb, stdin or stdout not a terminal, or questionary not importable.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from mcctl import __version__, configrepo, homecfg, remote, ui
from mcctl.schema import ServerConfig

try:  # a controller dependency (requirements/validate.in); without it `mcctl` alone stays a usage error
    import questionary
    from prompt_toolkit.key_binding import KeyBindings, merge_key_bindings

    HAVE_QUESTIONARY = True
except ImportError:  # pragma: no cover - the pinned requirements always have it
    HAVE_QUESTIONARY = False

COMING = "v1.3, coming"
STATUS_TIMEOUT = 20  # seconds per ssh/gh call of the header's background probe (ssh: ConnectTimeout=10)
SWITCH_PATH, SWITCH_BACK = "\x00path", "\x00back"


@dataclass(frozen=True)
class Item:
    key: str
    title: str
    argv: tuple[str, ...] = ()  # the command (the home screen adds `-C <repo>`)
    coming: bool = False
    ask: str = ""  # a question whose answer is appended to argv (`config get PATH`)


def _coming(key: str, title: str) -> Item:
    return Item(key, title, coming=True)


# Phase 1: read-only items that already exist as commands; the rest are shown disabled.
MENU: tuple[tuple[str, tuple[Item, ...]], ...] = (
    (
        "Server",
        (
            Item("status", "Status: server, players, TPS/MSPT, backups, deploys", ("status",)),
            Item("logs", "Logs: the last 100 lines", ("logs",)),
            Item("logs-follow", "Live logs (Ctrl-C returns here)", ("logs", "--follow")),
            Item("backups", "Backups: list snapshots", ("backup", "list")),
            _coming("console", "Console"),
            _coming("start-stop", "Start, stop, restart"),
            _coming("backup-now", "Back up now"),
            _coming("restore", "Restore"),
        ),
    ),
    (
        "Players",
        (
            Item("players", "Whitelist and operators", ("players", "list")),
            _coming("players-edit", "Add, remove, op, kick, ban"),
        ),
    ),
    ("Worlds", (_coming("worlds", "List, create, switch worlds"),)),
    (
        "Config",
        (
            Item("validate", "Validate the config repo", ("validate",)),
            Item(
                "config-get",
                "Show a setting (config get)",
                ("config", "get"),
                ask="Setting (e.g. minecraft.motd)",
            ),
            _coming("settings", "Edit settings"),
            Item("switch", "Switch config repo"),
        ),
    ),
    ("Publish", (_coming("publish", "Publish changes (preview, pull request)"),)),
    ("Health", (Item("doctor", "Doctor: tools, credentials, DNS, tailnet, watchdog", ("doctor",)),)),
    (
        "Updates",
        (
            Item("upgrade-preview", "Product update: preview (dry run)", ("upgrade", "--dry-run")),
            Item("lock-preview", "Mod lock update: preview (dry run)", ("lock", "update", "--dry-run")),
            _coming("minecraft", "Minecraft version"),
        ),
    ),
    (
        "Secrets",
        (
            Item("dashlane", "Dashlane items (show)", ("secrets", "dashlane")),
            _coming("rotate", "Rotate a secret"),
        ),
    ),
    ("Metrics", (Item("metrics", "Player experience: last 7 days", ("metrics", "summary")),)),
)
EXTRA = (Item("refresh", "Refresh status"), Item("quit", "Quit"))
ITEMS = {item.key: item for _, items in MENU for item in items} | {item.key: item for item in EXTRA}


# --- when ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    config_dir: Path | None  # `mcctl -C DIR` opens the home screen on DIR


def interactive(
    environ: Mapping[str, str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None
) -> bool:
    """A person at a terminal, with rich and questionary (else: today's plain behaviour)."""
    if not HAVE_QUESTIONARY or not ui.rich_wanted(stdout or sys.stdout, environ):
        return False
    try:
        return bool((stdin or sys.stdin).isatty())
    except (AttributeError, ValueError, OSError):
        return False


def wanted(argv: Sequence[str], *, is_interactive: Callable[[], bool] | None = None) -> Launch | None:
    """`mcctl` or `mcctl -C DIR` (nothing else) on an interactive terminal."""
    if not argv:
        launch = Launch(None)
    elif len(argv) == 2 and argv[0] in ("-C", "--config-dir"):
        launch = Launch(Path(argv[1]))
    elif len(argv) == 1 and argv[0].startswith("--config-dir="):
        launch = Launch(Path(argv[0].split("=", 1)[1]))
    else:
        return None
    return launch if (is_interactive or interactive)() else None


# --- the status header -----------------------------------------------------------------------------------


def quiet_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """remote.Runner for the background probe: never reads the terminal (the menu owns it), bounded."""
    try:
        return subprocess.run(
            list(argv),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            check=False,
            timeout=STATUS_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(list(argv), 124, "", f"no answer within {STATUS_TIMEOUT} s")
    except OSError as e:
        return subprocess.CompletedProcess(list(argv), 127, "", str(e))


def fetch_status(cfg: ServerConfig) -> remote.Status:
    return remote.status(cfg, run=quiet_run)


class _Job:
    def __init__(self, fn: Callable[[], remote.Status]) -> None:
        self.result: remote.Status | None = None
        self.error: BaseException | None = None
        self.thread = threading.Thread(target=self._run, args=(fn,), daemon=True)
        self.thread.start()

    def _run(self, fn: Callable[[], remote.Status]) -> None:
        try:
            self.result = fn()
        except Exception as e:  # shown as "unknown (why)", never raised into the menu
            self.error = e


class StatusProbe:
    """`remote.status` in a background thread: waits at most `budget` seconds, caches for `ttl` seconds. A
    probe still running after the budget keeps going; the next header (Refresh status) picks it up."""

    def __init__(
        self,
        fetch: Callable[[ServerConfig], remote.Status] = fetch_status,
        *,
        ttl: float = 30.0,
        budget: float = 8.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.fetch, self.ttl, self.budget, self.clock = fetch, ttl, budget, clock
        self._cache: dict[Path, tuple[float, remote.Status]] = {}
        self._jobs: dict[Path, _Job] = {}

    def get(self, root: Path, cfg: ServerConfig, *, force: bool = False) -> remote.Status | str:
        hit = self._cache.get(root)
        if hit is not None and not force and self.clock() - hit[0] < self.ttl:
            return hit[1]
        job = self._jobs.get(root)
        if job is None:
            job = self._jobs[root] = _Job(lambda: self.fetch(cfg))
        job.thread.join(self.budget)
        if job.thread.is_alive():
            return f"no answer within {self.budget:g} s; still asking (Refresh status)"
        del self._jobs[root]
        if job.error is not None or job.result is None:
            return f"{type(job.error).__name__}: {job.error}" if job.error else "no result"
        self._cache[root] = (self.clock(), job.result)
        return job.result


def _num(v: float | None) -> str:
    return "?" if v is None else f"{v:.1f}"


def server_summary(st: remote.Status | str) -> str:
    if isinstance(st, str):
        return f"unknown ({st})"
    if not st.reachable:
        return f"unknown ({st.error or 'unreachable'})"
    state = "online" if st.server == "active" else (st.server or "unknown state")
    if st.admin_stopped:
        state += " (stopped by an admin)"
    parts = [state]
    if st.players_online is not None:
        parts.append(f"{st.players_online}/{st.players_max} players")
    if st.tps is not None or st.mspt is not None:
        parts.append(f"TPS {_num(st.tps)}, MSPT {_num(st.mspt)}")
    return " · ".join(parts)


def deploy_summary(st: remote.Status) -> str:
    run = st.last_main_run
    if not run:
        notes = [n for n in st.notes if n.startswith("last deploy")]
        return f"unknown ({notes[0].split(': ', 1)[-1]})" if notes else "none found"
    result = run.get("conclusion") or run.get("status") or "?"
    return f"{result} ({run.get('createdAt', '?')})"


def _pin_text(root: Path) -> tuple[configrepo.Pin | None, str]:
    try:
        text = (root / configrepo.WORKFLOW).read_text(encoding="utf-8")
    except OSError as e:
        return None, f"unknown ({e.strerror or e})"
    pin, errors = configrepo.read_pin(text)
    if pin is None:
        return None, f"unknown ({errors[0] if errors else 'no pin'})"
    return pin, pin.version or pin.sha[:12]


def header_rows(
    root: Path | None, source: str, probe: StatusProbe, out: ui.Out, *, force: bool = False
) -> list[tuple[str, str]]:
    if root is None:
        return [
            ("Config repo", "none: “Switch config repo”, or create one with `mcctl init`"),
            ("mcctl", __version__),
        ]
    rows: list[tuple[str, str]] = []
    cfg: ServerConfig | None = None
    try:
        cfg = configrepo.load(root).server
        rows.append(("Deployment", cfg.name))
    except configrepo.ConfigError as e:
        rows.append(("Deployment", f"unknown ({len(e.errors)} error(s): “Validate the config repo”)"))
    except (OSError, ValueError) as e:
        rows.append(("Deployment", f"unknown ({e})"))
    rows.append(("Config repo", f"{root} ({source})"))
    pin, shown = _pin_text(root)
    product = f"pinned {shown} · this mcctl {__version__}"
    if pin is not None and pin.version and pin.version.lstrip("v") != __version__:
        product += " (commands that deploy run the pinned version)"
    rows.append(("Product", product))
    if cfg is None:
        rows.append(("Server", "unknown (the config repo doesn't load)"))
        return rows
    with out.spinner(f"asking {cfg.host.tailscale_name} over the tailnet, and GitHub"):
        st = probe.get(root, cfg, force=force)
    rows.append(("Server", server_summary(st)))
    if isinstance(st, remote.Status):
        rows.append(("Last deploy", deploy_summary(st)))
        prs = len(st.pending_panel_prs)
        rows.append(("Panel PRs", f"{prs} open" if prs else "none open"))
    return rows


# --- prompts (questionary; tests pass prompt_toolkit's pipe input and DummyOutput) -------------------------


def _choices(items: Sequence[tuple[str, Sequence[Item]]], extra: Sequence[Item]) -> list[Any]:
    out: list[Any] = []
    for group, group_items in items:
        out.append(questionary.Separator(f"── {group} "))
        for item in group_items:
            disabled = COMING if item.coming else None
            out.append(questionary.Choice(item.title, value=item.key, disabled=disabled))
    out.append(questionary.Separator("──"))
    out += [questionary.Choice(item.title, value=item.key) for item in extra]
    return out


def _esc_quits(question: Any) -> Any:  # noqa: ANN401 - a questionary Question
    keys = KeyBindings()

    @keys.add("escape", eager=True)
    def _(event: Any) -> None:  # noqa: ANN401
        event.app.exit(result=None)

    app = question.application
    app.key_bindings = merge_key_bindings([app.key_bindings, keys])
    return question


@dataclass
class Prompter:
    """questionary with optional prompt_toolkit input/output (tests)."""

    io: dict[str, Any] = field(default_factory=dict)

    def select(self, message: str, choices: list[Any], default: str | None = None) -> str | None:
        q = questionary.select(
            message, choices=choices, default=default, instruction="(arrows, Enter; Esc quits)", **self.io
        )
        try:
            return _esc_quits(q).unsafe_ask()
        except KeyboardInterrupt:
            return None

    def text(self, message: str) -> str | None:
        try:
            return _esc_quits(questionary.text(message, **self.io)).unsafe_ask()
        except KeyboardInterrupt:
            return None


# --- the loop ----------------------------------------------------------------------------------------------


def _initial(launch: Launch, cwd: Path) -> tuple[Path | None, str]:
    if launch.config_dir is not None:
        return launch.config_dir.expanduser().resolve(), "-C"
    if homecfg.looks_like_config_repo(cwd):
        return cwd.resolve(), "current directory"
    if (remembered := homecfg.remembered()) is not None:
        return remembered, "remembered"
    return None, ""


class Home:
    def __init__(
        self,
        launch: Launch,
        *,
        out: ui.Out | None = None,
        prompter: Prompter | None = None,
        runner: Callable[[list[str]], int] | None = None,
        probe: StatusProbe | None = None,
        cwd: Path | None = None,
    ) -> None:
        if runner is None:
            from mcctl import cli

            runner = cli.run
        self.out = out or ui.stdout()
        self.prompter = prompter or Prompter()
        self.runner = runner
        self.probe = probe or StatusProbe()
        self.root, self.source = _initial(launch, cwd or Path.cwd())

    def show_header(self, *, force: bool = False) -> None:
        rows = header_rows(self.root, self.source, self.probe, self.out, force=force)
        self.out.say()
        self.out.kv(rows, title=f"mcctl {__version__}: home")
        self.out.say()

    def loop(self) -> int:
        self.show_header()
        last = "status"
        choices = _choices(MENU, EXTRA)
        while True:
            key = self.prompter.select("What next?", choices, default=last)
            if key is None or key == "quit":
                return 0
            last = key
            if key == "refresh":
                self.show_header(force=True)
            elif key == "switch":
                if self.switch():
                    self.show_header()
            else:
                self.run_item(ITEMS[key])

    def run_item(self, item: Item) -> int | None:
        if self.root is None:
            self.out.warn("no config repo yet: “Switch config repo”, or create one with `mcctl init`")
            return None
        argv = list(item.argv)
        if item.ask:
            answer = self.prompter.text(item.ask)
            if not answer or not answer.strip():
                return None
            argv.append(answer.strip())
        shown = "mcctl " + " ".join(argv)
        self.out.dim(f"$ {shown}")
        try:
            code = self.runner([*argv, "-C", str(self.root)])
        except SystemExit as e:  # argparse and _repo() exit; the menu stays
            code = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        except KeyboardInterrupt:
            code = 130
        except Exception:  # a crash in one command must not end the session; show it
            traceback.print_exc()
            code = 1
        self.out.dim(f"{shown}: exit {code}")
        return code

    def switch(self) -> bool:
        """Pick a remembered config repo, or enter a path; remembered as the current one."""
        settings = homecfg.load()
        if settings.problem:
            self.out.warn(f"ignoring {settings.problem}")
        choices: list[Any] = []
        for repo in settings.repos:
            ok = homecfg.looks_like_config_repo(repo)
            mark = " (current)" if repo == self.root else ""
            choices.append(
                questionary.Choice(f"{repo}{mark}", value=str(repo), disabled=None if ok else "not found")
            )
        choices += [
            questionary.Choice("Enter a path…", value=SWITCH_PATH),
            questionary.Choice("Back", value=SWITCH_BACK),
        ]
        picked = self.prompter.select("Config repo", choices, default=SWITCH_PATH)
        if picked is None or picked == SWITCH_BACK:
            return False
        if picked == SWITCH_PATH:
            typed = self.prompter.text("Path of the config repo")
            if not typed or not typed.strip():
                return False
            picked = typed.strip()
        path = Path(picked).expanduser().resolve()
        if not homecfg.looks_like_config_repo(path):
            self.out.warn(f"{path} isn't a config repo (no server.yml and .github/workflows/server.yml)")
            return False
        try:
            homecfg.remember(path)
        except OSError as e:
            self.out.warn(f"couldn't remember {path}: {e}")
        self.root, self.source = path, "remembered"
        return True


def main(launch: Launch) -> int:
    try:
        return Home(launch).loop()
    except (KeyboardInterrupt, EOFError):
        return 0
