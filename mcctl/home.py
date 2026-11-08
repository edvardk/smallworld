"""Plain `mcctl` on an interactive terminal: when the dashboard opens, and the status it shows.

v1.3 opened a questionary menu here; now it opens the dashboard (mcctl/dashboard.py, Textual). This module
keeps what decides and feeds it:

- `wanted`: `mcctl` or `mcctl -C DIR` (nothing else) on an interactive terminal. Anything non-interactive
  keeps today's behaviour (an argparse usage error, exit code 2): `--plain`, MCCTL_PLAIN, NO_COLOR, CI,
  TERM=dumb, stdin or stdout not a terminal, or Textual not importable;
- `_initial`: the config repo it opens (`-C`, else the current directory if it is one, else the remembered
  one: mcctl/homecfg.py);
- `fetch_status` / `quiet_run`: `remote.status` bounded per call and never reading the terminal (the dashboard
  owns it), and `StatusProbe` (a time budget and a cache, for callers that must not wait long);
- `server_summary`, `deploy_summary`, `_pin_text`: one-line texts of the status and the pin.
"""

from __future__ import annotations

import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from mcctl import configrepo, homecfg, remote, ui
from mcctl.schema import ServerConfig

try:  # a controller dependency (requirements/validate.in); without it `mcctl` alone stays a usage error
    import textual  # noqa: F401

    HAVE_TEXTUAL = True
except ImportError:  # pragma: no cover - the pinned requirements always have it
    HAVE_TEXTUAL = False

STATUS_TIMEOUT = 20  # seconds per ssh/gh call of the background probe (ssh: ConnectTimeout=10)


# --- when ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Launch:
    config_dir: Path | None  # `mcctl -C DIR` opens the dashboard on DIR


def interactive(
    environ: Mapping[str, str] | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None
) -> bool:
    """A person at a terminal, with rich and Textual (else: today's plain behaviour)."""
    if not HAVE_TEXTUAL or not ui.rich_wanted(stdout or sys.stdout, environ):
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


# --- the status -----------------------------------------------------------------------------------


def quiet_run(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    """remote.Runner for the background probe: never reads the terminal (the dashboard owns it), bounded."""
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
        except Exception as e:  # shown as "unknown (why)", never raised into the caller
            self.error = e


class StatusProbe:
    """`remote.status` in a background thread: waits at most `budget` seconds, caches for `ttl` seconds. A
    probe still running after the budget keeps going; the next `get` picks it up."""

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
            return f"no answer within {self.budget:g} s; still asking"
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


def _initial(launch: Launch, cwd: Path) -> tuple[Path | None, str]:
    if launch.config_dir is not None:
        return launch.config_dir.expanduser().resolve(), "-C"
    if homecfg.looks_like_config_repo(cwd):
        return cwd.resolve(), "current directory"
    if (remembered := homecfg.remembered()) is not None:
        return remembered, "remembered"
    return None, ""


def main(launch: Launch) -> int:
    from mcctl import dashboard

    return dashboard.main(launch)
