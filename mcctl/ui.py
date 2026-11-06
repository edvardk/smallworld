"""mcctl's one presentation layer: every command prints through it, so none of them calls rich directly.

Two renderings of the same calls:

- rich (an interactive terminal): step headers with a progress bar, panels ("Your turn" with numbered actions,
  failures with Fix/Then), status icons, tables, clickable links (OSC 8; the raw URL stays on one line so it
  can be copied), spinners with the elapsed time while mcctl waits.
- plain (`--plain`, NO_COLOR, MCCTL_PLAIN, CI, TERM=dumb, output not a terminal, rich not installed): the
  classic line format (`[ok]`, `[!!]`, `[xx]`, indented text), stable for logs, CI and the tests.

`UI` adds the conversation of `mcctl init` (design §4): prompts, secrets, an answers file. Interactive:
the default in brackets, re-asked until the answer validates; secrets through `getpass` (labelled "input
hidden", never echoed, never printed back, never logged). Non-interactive (`--answers FILE
--non-interactive`): every question is looked up by its key in the answers file (a missing key takes the
default, or fails); secrets are never in the answers file: they come from the environment variable named for
each (the same names as `.env`), or, with `--mock`, a fixed fake value.
"""

from __future__ import annotations

import contextlib
import getpass
import os
import re
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any, TextIO

try:  # rich is a controller dependency (requirements/validate.in); without it everything is plain
    from rich import box
    from rich.console import Console, Group, RenderableType
    from rich.panel import Panel
    from rich.progress import Progress, ProgressColumn, SpinnerColumn, TextColumn
    from rich.progress_bar import ProgressBar
    from rich.style import Style
    from rich.table import Table
    from rich.text import Text

    HAVE_RICH = True
except ImportError:  # pragma: no cover - the pinned requirements always have it
    HAVE_RICH = False

Check = Callable[[str], str | None]  # answer -> error message or None
# --mock: fake credentials where the shape is checked before any request (anything else: "mock-<key>").
MOCK_SECRETS = {
    "discord_webhook": "https://discord.com/api/webhooks/1/mock-webhook-token",
    "tailscale_api_key": "tskey-api-kMOCK1CNTRL-mockTailscaleApiKey",
}
PLAIN_ENV = "MCCTL_PLAIN"  # set by `--plain`, inherited by a re-executed (pinned) mcctl
MAX_WIDTH = 110  # wider lines are hard to read; the terminal wraps nothing below this
URL = re.compile(r"https?://[^\s<>\"'`]+[^\s<>\"'`.,;:!?)\]]")
ICONS = {
    "ok": ("✓", "bold green"),
    "warn": ("!", "bold yellow"),
    "bad": ("✗", "bold red"),
    "info": ("→", "cyan"),
}
PLAIN_MARKS = {"ok": "[ok]", "warn": "[!!]", "bad": "[xx]"}
LEVEL_KIND = {"ok": "ok", "warn": "warn", "fail": "bad", "bad": "bad", "info": "info", "skip": "info"}


class Abort(Exception):
    """The user ended the conversation (EOF / Ctrl-C) or an answers file can't answer a question."""


def set_plain(on: bool = True) -> None:
    """`--plain`: this process and every mcctl it starts (the pinned product) print plain text."""
    if on:
        os.environ[PLAIN_ENV] = "1"
    else:
        os.environ.pop(PLAIN_ENV, None)


def rich_wanted(stream: TextIO, environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    if not HAVE_RICH or env.get(PLAIN_ENV) or env.get("NO_COLOR") or env.get("CI"):
        return False
    if env.get("TERM") == "dumb":
        return False
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def short_url(url: str, width: int = 60) -> str:
    """What a link shows in a panel: host and path, never the (long, pre-filled) query."""
    rest = url.split("://", 1)[-1]
    base, _, query = rest.partition("?")
    text = base.rstrip("/") + ("?…" if query else "")
    return text if len(text) <= width else text[: width - 1] + "…"


class Out:
    """Output only: what every command prints (stdout or stderr)."""

    def __init__(
        self,
        out: TextIO | None = None,
        *,
        rich: bool | None = None,
        width: int | None = None,
        record: bool = False,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self.out = out or sys.stdout
        self.rich = (rich_wanted(self.out, environ) if rich is None else rich) and HAVE_RICH
        self.console: Any = None
        if self.rich:
            if rich is None and width is None and not record:
                self.console = _shared_console(self.out)
            else:  # forced (tests, the docs screenshot): exactly this width, no terminal detection
                self.console = Console(
                    file=self.out,
                    force_terminal=True,
                    width=width or MAX_WIDTH,
                    record=record,
                    highlight=False,
                    emoji=False,
                    color_system="truecolor",
                    legacy_windows=False,
                    _environ={},
                )

    # --- primitives -----------------------------------------------------------------------------------

    def _print(self, *items: Any, **kw: Any) -> None:  # noqa: ANN401
        self.console.print(*items, **kw)

    def _linked(self, text: str, style: str = "") -> Text:
        """`text` with every URL clickable (OSC 8)."""
        t = Text(text, style=style)
        for m in URL.finditer(text):
            t.stylize(Style(link=m.group(0), underline=True), m.start(), m.end())
        return t

    def _row(self, kind: str, text: str, indent: int = 2) -> None:
        icon, style = ICONS[kind]
        body = self._linked(text, "red" if kind == "bad" else "")
        if URL.search(text):  # never wrap a URL: the terminal wraps it whole and it stays clickable/copyable
            self._print(Text(" " * indent) + Text(icon + " ", style=style) + body, soft_wrap=True)
            return
        grid = Table.grid(padding=(0, 1))
        grid.add_column(width=indent - 1 if indent > 1 else 0)
        grid.add_column(width=1)
        grid.add_column(ratio=1, overflow="fold")
        grid.add_row("", Text(icon, style=style), body)
        self._print(grid)

    # --- lines ----------------------------------------------------------------------------------------

    def say(self, text: str = "") -> None:
        if not self.rich:
            print(text, file=self.out, flush=True)
            return
        self._print(self._linked(text), soft_wrap=bool(URL.search(text)))

    def _mark(self, kind: str, text: str) -> None:
        if self.rich:
            self._row(kind, text)
        elif kind == "info":
            self.info(text)
        else:
            self.say(f"  {PLAIN_MARKS[kind]} {text}")

    def ok(self, text: str) -> None:
        self._mark("ok", text)

    def warn(self, text: str) -> None:
        self._mark("warn", text)

    def bad(self, text: str) -> None:
        self._mark("bad", text)

    def note(self, text: str) -> None:
        """A side remark (→); plain: an indented line like info."""
        self._mark("info", text)

    def level(self, level: str, text: str) -> None:
        """ok / warn / fail (a doctor or credential level)."""
        {"ok": self.ok, "warn": self.warn, "info": self.note}.get(level, self.bad)(text)

    def info(self, text: str) -> None:
        if not self.rich:
            for line in text.splitlines():
                self.say(f"       {line}" if line else "")
            return
        for line in text.splitlines() or [""]:
            if URL.search(line):
                self._print(Text("    ") + self._linked(line.strip()), soft_wrap=True)
            else:
                grid = Table.grid()
                grid.add_column(width=4)
                grid.add_column(ratio=1, overflow="fold")
                grid.add_row("", Text(line.strip()))
                self._print(grid)

    def dim(self, text: str) -> None:
        """Secondary detail (a log file's path): rich only, dimmed; plain prints it as info."""
        if not self.rich:
            self.info(text)
            return
        self._print(Text("    ") + self._linked(text, "dim"), soft_wrap=True)

    # --- structure ------------------------------------------------------------------------------------

    def banner(self, title: str, lines: Sequence[str] = ()) -> None:
        """The opening of a long command (setup, migrate): a title and a few facts."""
        if not self.rich:
            for line in lines:
                self.say(line)
            return
        body = Group(*(self._linked(line) for line in lines)) if lines else Text("")
        self._print(
            Panel(
                body,
                title=Text(title, style="bold"),
                title_align="left",
                border_style="cyan",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )

    def header(
        self,
        n: int,
        total: int,
        title: str,
        *,
        done: int | None = None,
        name: str | None = None,
        steps: int | None = None,
    ) -> None:
        """Step n/total, 1-based (setup shows [1/17]..[17/17]; `steps` defaults to total). Rich: a rule with
        the title, then the overall progress (`done` of `steps`)."""
        if not self.rich:
            self.say(f"\n[{n}/{total}] {title}")
            return
        self._print()
        label = Text.assemble((f"Step {n}/{total}", "bold cyan"), ("  ·  ", "dim"), (title, "bold"))
        self.console.rule(label, align="left", style="cyan")
        if done is not None:
            grid = Table.grid(padding=(0, 1))
            grid.add_column(width=1)
            grid.add_column(width=24)
            grid.add_column()
            count = total if steps is None else steps
            facts = f"{done}/{count} steps done" + (f"  ·  {name}" if name else "")
            grid.add_row("", ProgressBar(total=count, completed=done, width=24), Text(facts, style="dim"))
            self._print(grid)

    def section(self, title: str) -> None:
        """A sub-heading inside a step or command."""
        if not self.rich:
            self.say(f"{title}:")
            return
        self._print(Text(title, style="bold"))

    def why(self, text: str) -> None:
        """What a step is for (rich only: plain runs keep their classic transcript)."""
        if self.rich and text:
            grid = Table.grid()
            grid.add_column(width=2)
            grid.add_column(ratio=1)
            grid.add_row("", Text(text, style="italic dim"))
            self._print(grid)

    def todo(self, actions: Sequence[str], *, title: str = "Your turn", intro: str = "") -> None:
        """What the user does now, numbered. URLs in an action: a short clickable label in the panel and the
        full URL on one line below it (to copy). Plain: the same text as indented lines."""
        if not self.rich:
            if intro:
                self.info(intro)
            for i, action in enumerate(actions, 1):
                first, *rest = action.splitlines() or [""]
                self.info(f"{i}. {first}")
                for line in rest:
                    self.info(f"   {line}")
            return
        urls: list[str] = []
        rows = Table.grid(padding=(0, 1))
        rows.add_column(justify="right", style="bold magenta")
        rows.add_column(ratio=1)
        for i, action in enumerate(actions, 1):
            text = Text()
            pos = 0
            for m in URL.finditer(action):
                text.append(action[pos : m.start()])
                url = m.group(0)
                if url not in urls:
                    urls.append(url)
                ref = urls.index(url) + 1
                text.append(short_url(url), Style(link=url, underline=True, color="blue"))
                text.append(f" [{ref}]", "dim")
                pos = m.end()
            text.append(action[pos:])
            rows.add_row(f"{i}.", text)
        body: RenderableType = Group(Text(intro), Text(""), rows) if intro else rows
        self._print(
            Panel(
                body,
                title=Text(title, style="bold magenta"),
                title_align="left",
                border_style="magenta",
                box=box.ROUNDED,
                padding=(0, 1),
            )
        )
        for ref, url in enumerate(urls, 1):
            self._print(Text(f"  [{ref}] ", style="dim") + self._linked(url, "blue"), soft_wrap=True)

    def code(self, lines: Sequence[str]) -> None:
        """Commands to run, one per line, never wrapped (copyable). Plain: indented like info."""
        if not self.rich:
            for line in lines:
                self.info(f"  {line}")
            return
        for line in lines:
            self._print(Text("    $ ", style="dim") + Text(line, style="bold cyan"), soft_wrap=True)

    def link(self, url: str, label: str = "") -> None:
        """A URL on its own line (clickable in rich, with `label` before it)."""
        if not self.rich:
            self.info(f"{label} {url}".strip() if label else url)
            return
        lead = Text("    ") + (Text(label + " ", style="bold") if label else Text(""))
        self._print(lead + self._linked(url, "blue"), soft_wrap=True)

    def fail(self, cause: str, fix: str = "", then: str = "") -> None:
        """A failure: what went wrong, the fix, what to run after it. Plain: [xx] cause / fix: / then ..."""
        if not self.rich:
            self.bad(cause)
            if fix:
                self.info(f"fix: {fix}")
            if then:
                self.info(f"then {then}")
            return
        grid = Table.grid(padding=(0, 1))
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column(ratio=1)
        first, *more = cause.splitlines() or [""]
        grid.add_row(Text("✗", style="bold red"), Text(first, style="bold red"))
        for line in more:
            grid.add_row("", self._linked(line.strip()))
        if fix:
            grid.add_row(Text("Fix", style="bold yellow"), self._linked(fix))
        if then:
            grid.add_row(Text("Then", style="bold cyan"), self._linked(then))
        self._print(Panel(grid, border_style="red", box=box.ROUNDED, padding=(0, 1)))

    def table(
        self,
        columns: Sequence[str],
        rows: Sequence[Sequence[str]],
        *,
        title: str = "",
        styles: Sequence[str] = (),
        indent: int = 7,
    ) -> None:
        """A table. Cells may start with a level (`ok:`/`warn:`/`fail:`/`info:`) to get its icon. Plain:
        columns padded with two spaces (the title as a line before them)."""
        if not self.rich:
            if title:
                self.say(" " * (indent - 2) + title + ":")
            cells = [[_plain_cell(c) for c in row] for row in rows]
            widths = [max([len(columns[i])] + [len(r[i]) for r in cells]) for i in range(len(columns))]
            pad = " " * indent
            for row in [list(columns), *cells]:
                self.say((pad + "  ".join(c.ljust(w) for c, w in zip(row, widths, strict=True))).rstrip())
            return
        t = Table(
            title=Text(title, style="bold") if title else None,
            title_justify="left",
            box=box.SIMPLE_HEAD,
            header_style="bold",
            show_edge=False,
            pad_edge=False,
        )
        for i, col in enumerate(columns):
            t.add_column(col, style=styles[i] if i < len(styles) else "", overflow="fold")
        for row in rows:
            t.add_row(*(self._cell(c) for c in row))
        grid = Table.grid()
        grid.add_column(width=2)
        grid.add_column()
        grid.add_row("", t)
        self._print(grid)

    def _cell(self, cell: str) -> Text:
        level, sep, rest = cell.partition(":")
        if sep and level in LEVEL_KIND and not rest.startswith("//"):
            icon, style = ICONS[LEVEL_KIND[level]]
            return Text(icon + " ", style=style) + self._linked(rest.strip())
        return self._linked(cell)

    def kv(self, rows: Sequence[tuple[str, str]], *, title: str = "") -> None:
        """Label/value pairs (a summary). Plain: `  label   value` lines under `title:`."""
        if not self.rich:
            if title:
                self.say(f"{title}:")
            width = max((len(k) for k, _ in rows), default=0)
            for k, v in rows:
                self.say(f"  {k:<{width}}  {v}".rstrip())
            return
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="bold", no_wrap=True)
        grid.add_column(ratio=1, overflow="fold")
        for k, v in rows:
            grid.add_row(k, self._cell(v))
        if title:
            self._print(Text(title, style="bold"))
        outer = Table.grid()
        outer.add_column(width=2)
        outer.add_column(ratio=1)
        outer.add_row("", grid)
        self._print(outer)

    # --- waits ----------------------------------------------------------------------------------------

    @contextlib.contextmanager
    def spinner(self, text: str) -> Iterator[_Spinner]:
        """While mcctl waits (an API, Terraform, CI, the browser): a spinner with the elapsed time (rich), or
        nothing (plain). Prompts pause it."""
        sp = _Spinner(self, text, nested=any(s.running for s in _SPINNERS))
        _SPINNERS.append(sp)
        sp.start()
        try:
            yield sp
        finally:
            sp.stop()
            _SPINNERS.remove(sp)

    @contextlib.contextmanager
    def paused(self) -> Iterator[None]:
        """No spinner while the user types (whichever Out started it)."""
        active = [s for s in _SPINNERS if s.running]
        for s in active:
            s.stop()
        try:
            yield
        finally:
            for s in active:
                s.start()

    def copy(self, text: str) -> bool:
        """Put a public value (an SSH public key, a command) on the clipboard; interactive rich runs only."""
        if not self.rich:
            return False
        from mcctl import desktop

        return desktop.copy(text)

    def export_svg(self, title: str) -> str:
        """A recorded rich run as SVG (docs screenshots)."""
        return str(self.console.export_svg(title=title))

    def export_text(self) -> str:
        return str(self.console.export_text())


class _Spinner:
    def __init__(self, out: Out, text: str, *, nested: bool = False) -> None:
        self.out, self.text, self.nested = out, text, nested
        self.started = time.monotonic()
        self.running = False
        self._progress: Any = None
        self._task: Any = None

    def start(self) -> None:
        # one live display at a time; none in a recording (docs screenshots: no spinner frames)
        if not self.out.rich or self.running or self.nested or self.out.console.record:
            return
        self._progress = Progress(
            TextColumn("  "),
            SpinnerColumn(style="cyan"),
            TextColumn("{task.description}"),
            _elapsed_column(self),
            console=self.out.console,
            transient=True,
        )
        self._task = self._progress.add_task(self.text, total=None)
        self._progress.start()
        self.running = True

    def update(self, text: str) -> None:
        """New text (e.g. the line Terraform just printed)."""
        self.text = text
        if self.running:
            self._progress.update(self._task, description=Text(text[:200]))

    def stop(self) -> None:
        if self.running:
            self._progress.stop()
            self.running = False

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


def _elapsed_column(sp: _Spinner) -> Any:  # noqa: ANN401 - a rich ProgressColumn
    """Elapsed time since the spinner first started (a prompt pausing it doesn't reset it)."""

    class Elapsed(ProgressColumn):
        def render(self, task: Any) -> Text:  # noqa: ANN401
            seconds = int(sp.elapsed)
            return Text(f"{seconds // 60}:{seconds % 60:02d}", style="yellow")

    return Elapsed()


def _plain_cell(cell: str) -> str:
    level, sep, rest = cell.partition(":")
    if sep and level in LEVEL_KIND and not rest.startswith("//"):
        return f"[{level}] {rest.strip()}" if level != "info" else rest.strip()
    return cell


_SPINNERS: list[_Spinner] = []
_CONSOLES: dict[int, tuple[TextIO, Any]] = {}
_DEFAULT: list[Out] = []


def _shared_console(stream: TextIO) -> Any:  # noqa: ANN401
    """One rich Console per terminal stream, so a spinner and every line printed meanwhile (by any Out)
    go through the same live display."""
    hit = _CONSOLES.get(id(stream))
    if hit is not None and hit[0] is stream:
        return hit[1]
    probe = Console(file=stream)
    console = Console(
        file=stream,
        force_terminal=True,
        width=min(probe.width, MAX_WIDTH),
        highlight=False,
        emoji=False,
        legacy_windows=False,
    )
    _CONSOLES[id(stream)] = (stream, console)
    return console


@contextlib.contextmanager
def using(out: Out) -> Iterator[Out]:
    """Make `out` what `stdout()` returns meanwhile (`mcctl init`: Terraform's plan table and spinners land in
    the same conversation, and its recording)."""
    _DEFAULT.append(out)
    try:
        yield out
    finally:
        _DEFAULT.remove(out)


def stdout() -> Out:
    """Output for the current sys.stdout (rich or plain, decided now), or the Out installed by `using`."""
    return _DEFAULT[-1] if _DEFAULT else Out(sys.stdout)


def stderr() -> Out:
    return Out(sys.stderr)


class UI(Out):
    """Output plus the conversation (prompts, secrets, an answers file)."""

    def __init__(
        self,
        *,
        answers: Mapping[str, Any] | None = None,
        mock: bool = False,
        env: Mapping[str, str] | None = None,
        out: TextIO | None = None,
        read: Callable[[str], str] = input,
        read_secret: Callable[[str], str] = getpass.getpass,
        rich: bool | None = None,
        width: int | None = None,
        record: bool = False,
    ) -> None:
        super().__init__(out, rich=rich, width=width, record=record)
        self.answers = answers
        self.mock = mock
        self.env = os.environ if env is None else env
        self.read = read
        self.read_secret = read_secret

    @property
    def interactive(self) -> bool:
        return self.answers is None

    # --- input ----------------------------------------------------------------------------------------

    def _from_answers(self, key: str, default: Any) -> Any:  # noqa: ANN401
        assert self.answers is not None
        if key in self.answers:
            return self.answers[key]
        if default is not None:
            return default
        raise Abort(f"the answers file has no `{key}` (and there is no default)")

    def _prompt(self, prompt: str, suffix: str = "", reader: Callable[[str], str] | None = None) -> str:
        """Read one line. Plain: `  ? prompt suffix: ` through `reader`; rich: the prompt styled, then
        `reader("")` (like rich's own Console.input)."""
        reader = reader or self.read
        if not self.rich:
            return reader(f"  ? {prompt}{suffix}: ")
        with self.paused():
            self._print(Text.assemble(("  ? ", "bold cyan"), (prompt, "bold"), (suffix, "dim"), ": "), end="")
            return reader("")

    def _interactive(self, prompt: str, default: str | None) -> str:
        suffix = f" [{default}]" if default not in (None, "") else ""
        try:
            raw = self._prompt(prompt, suffix).strip()
        except (EOFError, KeyboardInterrupt):
            raise Abort("no answer (input closed)") from None
        return raw if raw else (default or "")

    def ask(self, key: str, prompt: str, *, default: str | None = None, check: Check | None = None) -> str:
        if not self.interactive:
            value = str(self._from_answers(key, default))
            if check and (err := check(value)):
                raise Abort(f"answers file: `{key}`: {err}")
            return value
        while True:
            value = self._interactive(prompt, default)
            err = check(value) if check else None
            if err is None:
                return value
            self.bad(err)

    def secret(self, key: str, prompt: str, *, env_name: str, check: Check | None = None) -> str:
        """A credential: getpass interactively; else $env_name (or a fake value with --mock)."""
        if not self.interactive:
            value = self.env.get(env_name) or (MOCK_SECRETS.get(key, f"mock-{key}") if self.mock else "")
            if not value:
                raise Abort(f"non-interactive: set {env_name} in the environment for `{key}`")
            if check and (err := check(value)):
                raise Abort(f"{env_name}: {err}")
            return value
        while True:
            try:
                value = self._prompt(prompt, " (input hidden)", self.read_secret).strip()
            except (EOFError, KeyboardInterrupt):
                raise Abort("no answer (input closed)") from None
            err = "empty" if not value else (check(value) if check else None)
            if err is None:
                return value
            self.bad(err)

    def confirm(self, key: str, prompt: str, *, default: bool = False) -> bool:
        if not self.interactive:
            value = self._from_answers(key, default)
            if not isinstance(value, bool):
                raise Abort(f"answers file: `{key}` must be true or false")
            return value
        hint = "Y/n" if default else "y/N"
        while True:
            raw = self._interactive(f"{prompt} ({hint})", None).lower()
            if not raw:
                return default
            if raw in ("y", "yes"):
                return True
            if raw in ("n", "no"):
                return False
            self.bad("answer y or n")

    def choose(
        self,
        key: str,
        prompt: str,
        options: Sequence[str],
        *,
        default: str | None = None,
        descriptions: Mapping[str, str] | None = None,
    ) -> str:
        """One of `options` (by number or name); rich shows a small menu with the default marked."""

        def check(v: str) -> str | None:
            return None if v in options else f"choose one of: {', '.join(options)}"

        if self.interactive:
            descriptions = descriptions or {}
            if self.rich:
                menu = Table.grid(padding=(0, 1))
                menu.add_column(width=4)
                menu.add_column(justify="right", style="bold magenta")
                menu.add_column(style="bold")
                menu.add_column()
                for i, opt in enumerate(options, 1):
                    mark = Text(" (recommended)", style="green") if opt == default else Text("")
                    menu.add_row("", f"{i}.", opt, Text(descriptions.get(opt, ""), style="dim") + mark)
                self._print(menu)
            else:
                for i, opt in enumerate(options, 1):
                    extra = f"  {descriptions[opt]}" if opt in descriptions else ""
                    self.say(f"      {i}. {opt}{extra}")

            def pick(v: str) -> str | None:
                return None if v.isdigit() and 1 <= int(v) <= len(options) else check(v)

            value = self.ask(key, prompt, default=default, check=pick)
            return options[int(value) - 1] if value.isdigit() and value not in options else value
        return self.ask(key, prompt, default=default, check=check)

    def typed(self, key: str) -> Callable[[str], str]:
        """An `ask(prompt)` callable for a typed confirmation (Terraform apply: the deployment name)."""

        def ask(prompt: str) -> str:
            if not self.interactive:
                return str(self._from_answers(key, ""))
            try:
                if not self.rich:
                    return self.read(f"  ? {prompt}")
                with self.paused():
                    self._print(Text.assemble(("  ? ", "bold red"), (prompt.rstrip(), "bold"), " "), end="")
                    return self.read("")
            except KeyboardInterrupt:
                raise EOFError from None

        return ask
