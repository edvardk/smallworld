"""v1.3: "a newer Smallworld is out" on interactive commands and the home screen.

When the product has a newer MINOR or MAJOR release than the config repo's pin (its workflow's `# vX.Y.Z`
comment), mcctl says so once per command, on stderr after the command's own output, with the fix:
`mcctl upgrade` (a pull request; a minor/major release may migrate server.yml, P12). Patch releases are left
out: the weekly update task already opens their pull request (and merges it with `automation.auto_merge:
builds+patch`).

- Source: GitHub's releases/latest of the pinned product repository (`updates.latest_tag`, the same lookup
  `mcctl upgrade` starts from), anonymous, through `mcctl.http` with a short timeout (TIMEOUT). A private
  product (404) or any failure reads as "nothing to say", silently.
- Cache: `${XDG_CACHE_HOME:-~/.cache}/mcctl/update-notice.json` (next to the pinned checkouts, product.py),
  per repository, TTL_S (24 h), failures included, so the network is asked at most once a day.
- When: only for a person at a terminal (home.interactive: stdin and stdout terminals, no --plain/MCCTL_PLAIN,
  NO_COLOR, CI, TERM=dumb), never for the CI-only commands (QUIET_COMMANDS), never in a re-executed pinned
  mcctl (the first process sets OPT_OUT_ENV for its children). `MCCTL_NO_UPDATE_NOTICE=1` turns it off.
- The lookup runs in a background thread started before the command, so it costs the command nothing unless
  the command is quicker than BUDGET_S.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

from mcctl import configrepo, http, product, updates

OPT_OUT_ENV = "MCCTL_NO_UPDATE_NOTICE"
CACHE_FILE = "update-notice.json"
TTL_S = 24 * 3600
TIMEOUT = 3.0  # seconds per socket operation of the one request
BUDGET_S = 3.0  # how long a finished command waits for a lookup still running
# CI and machine-facing commands (their output is parsed or logged), and `upgrade` itself (it is the fix).
QUIET_COMMANDS = frozenset(
    {"update-prs", "expiry-check", "panel-pr-check", "pr-gate", "client-publish", "schema", "schema-migrate",
     "render", "upgrade"}
)  # fmt: skip
VERSION = re.compile(r"v(\d+)\.(\d+)\.(\d+)")

Lookup = Callable[[str], str | None]  # repository -> latest release tag (or None)


def minor_or_major_newer(latest: str | None, pinned: str | None) -> bool:
    """`latest` (vX.Y.Z) has a higher X.Y than the pin comment (vX.Y.Z or vX.Y.Z-N-g<sha>); unknown: False."""
    ml, mp = VERSION.fullmatch(latest or ""), VERSION.match(pinned or "")
    if not ml or not mp:
        return False
    return (int(ml[1]), int(ml[2])) > (int(mp[1]), int(mp[2]))


def message(latest: str, pin: configrepo.Pin) -> str:
    return (
        f"Smallworld {latest} is out (this config repo pins {pin.version}): `mcctl upgrade` opens the pull "
        "request (a minor/major release may migrate server.yml)"
    )


def cache_path(environ: Mapping[str, str] | None = None) -> Path:
    return product.cache_root(os.environ if environ is None else environ) / CACHE_FILE


def _load(path: Path) -> dict[str, dict[str, object]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def cached_latest(
    repository: str,
    *,
    transport: http.Transport = http.urllib_transport,
    path: Path | None = None,
    clock: Callable[[], float] = time.time,
) -> str | None:
    """The product's latest release tag, from the cache when it is younger than TTL_S, else asked (and cached,
    a failure too). Never raises."""
    path = path or cache_path()
    data = _load(path)
    hit = data.get(repository)
    now = clock()
    if isinstance(hit, dict) and isinstance(hit.get("checked"), int | float) and now - hit["checked"] < TTL_S:
        latest = hit.get("latest")
        return latest if isinstance(latest, str) and latest else None
    try:
        latest = updates.latest_tag(repository, "", transport, timeout=TIMEOUT)
    except (http.HttpError, OSError, ValueError):
        latest = None
    data[repository] = {"checked": now, "latest": latest or ""}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass  # a read-only cache only means asking again next time
    return latest


def for_pin(pin: configrepo.Pin | None, lookup: Lookup | None = None) -> str | None:
    """The notice for this pin, or None (no version comment, no newer minor/major, or the lookup failed)."""
    if pin is None or not pin.version or not VERSION.match(pin.version):
        return None
    latest = (lookup or cached_latest)(pin.repository)
    return message(latest, pin) if latest and minor_or_major_newer(latest, pin.version) else None


def read_pin(root: Path) -> configrepo.Pin | None:
    try:
        text = (root / configrepo.WORKFLOW).read_text(encoding="utf-8")
    except OSError:
        return None
    return configrepo.read_pin(text)[0]


def wanted(
    command: str | None,
    environ: Mapping[str, str] | None = None,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
) -> bool:
    from mcctl import home

    env = os.environ if environ is None else environ
    if env.get(OPT_OUT_ENV) or command in QUIET_COMMANDS:
        return False
    return home.interactive(env, stdin, stdout)


@dataclass
class Check:
    """A lookup started before the command; `finish` shows its notice afterwards."""

    pin: configrepo.Pin
    lookup: Lookup
    started: float = field(default_factory=time.monotonic)
    result: str | None = None
    thread: threading.Thread | None = None

    def run(self) -> None:
        try:
            self.result = for_pin(self.pin, self.lookup)
        except Exception:  # a notice must never break a command
            self.result = None

    def finish(self, show: Callable[[str], None] | None = None) -> str | None:
        if self.thread is not None:
            self.thread.join(max(0.0, BUDGET_S - (time.monotonic() - self.started)))
            if self.thread.is_alive():
                return None  # too slow this time: the cache answers next time
        if self.result:
            (show or _show)(self.result)
        return self.result


def _show(text: str) -> None:
    from mcctl import ui

    err = ui.stderr()
    if err.rich:
        err.info(text)
    else:
        print(f"mcctl: {text}", file=sys.stderr)


def start(command: str | None, root: Path | None, lookup: Lookup | None = None) -> Check | None:
    """Begin the lookup for the config repo at `root` when a notice is wanted (see the module doc)."""
    if root is None or not wanted(command):
        return None
    os.environ[OPT_OUT_ENV] = "1"  # a re-executed (pinned) mcctl stays quiet: this process speaks
    pin = read_pin(root)
    if pin is None or not pin.version:
        return None
    check = Check(pin, lookup or cached_latest)
    check.thread = threading.Thread(target=check.run, name="update-notice", daemon=True)
    check.thread.start()
    return check
