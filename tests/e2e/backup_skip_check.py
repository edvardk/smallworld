"""v1.5.1: the smooth-backup skip, live (stdlib only; agent_check.py's `backup-skip` round, shard agent-b).

scripts/backup.py may skip only a run the timer started (TRIGGER_UNIT=mc-backup.timer) and only when nobody is
online and nothing the backup reads changed since the last successful backup; the skip is recorded in
/var/lib/mc-snapshots/backup-state.json and reported as an OK heartbeat (no /start, no /fail), so the
watchdog's freshness check holds. This round proves that on the real unit and sandbox:

1. A drop-in gives mc-backup.service a second EnvironmentFile (read after restic.env, so it wins):
   TRIGGER_UNIT=mc-backup.timer (what systemd sets for a timer start), the skip on and deferral off (whatever
   the example config says), and the heartbeats aimed at a local HTTPS mock of the watchdog
   (WATCHDOG_URL=https://127.0.0.1:PORT, its self-signed certificate as SSL_CERT_FILE; notify.py posts to
   https only). The product's timers stay stopped (agent_check.py: timers_paused), so every run is ours.
2. Timer-style runs until one skips. Why more than one: the e2e server keeps ticking until it has been empty
   for pause-when-empty-seconds (60 s, the product leaves the default) and writes chunks, player data or an
   autosave until then, so an earlier run may still back up (and record what it saw). Once the empty server
   paused, nothing writes, so the run after a backup must skip. Several attempts, no fixed sleep, no reliance
   on when exactly the pause began: not timing-flaky. Then the skip is checked: the journal line, the state
   file (a skip counted, the last backup unchanged), no new snapshot, exactly one OK heartbeat.
3. A world file is touched (a new file, owned by minecraft) and the next timer-style run must back up: the
   start and ok heartbeats, a new `auto` snapshot, the state file's new last backup with the skip count reset.

The pure parts (the drop-in, the verdicts on a run's journal, heartbeats and state) are unit-tested in
tests/test_backup.py, against what scripts/backup.py really prints and sends.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import pwd
import ssl
import subprocess
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from e2e_common import run_oneshot, timers_paused

UNIT = "mc-backup.service"
DROPIN_DIR = Path("/run/systemd/system/mc-backup.service.d")  # runtime: gone with the box, removed anyway
DROPIN = DROPIN_DIR / "e2e-backup-skip.conf"
ETC = Path(
    "/etc/mc-e2e-backup-skip"
)  # readable in the unit's sandbox (ProtectSystem=strict keeps /etc readable)
ENV_FILE = ETC / "backup.env"
CERT, KEY = ETC / "watchdog.pem", ETC / "watchdog.key"
STATE_FILE = Path("/var/lib/mc-snapshots/backup-state.json")
SERVER = Path("/opt/minecraft/server")
TOKEN = "e2e-backup-skip-not-a-secret"
SKIPPED = "backup skipped: "
ATTEMPTS = 6  # timer-style runs until one skips (the server pauses 60 s after it is empty)


@dataclass(frozen=True)
class Hit:
    path: str  # /hb/backup, /hb/backup/start, /hb/backup/fail
    body: str
    auth: str


def env_file(url: str, cert: Path, token: str = TOKEN) -> str:
    """The drop-in's EnvironmentFile: a timer start, the skip on, no deferral, heartbeats to the mock."""
    return "\n".join([
        "TRIGGER_UNIT=mc-backup.timer",
        "BACKUP_SKIP_WHEN_IDLE=1",
        "BACKUP_DEFER_WHEN_BUSY=0",
        f"WATCHDOG_URL={url}",
        f"HEARTBEAT_TOKEN={token}",
        f"SSL_CERT_FILE={cert}",
        "",
    ])  # fmt: skip


def dropin(env: Path = ENV_FILE) -> str:
    """A second EnvironmentFile= is read after restic.env's, so its settings win (systemd.exec)."""
    return f"[Service]\nEnvironmentFile={env}\n"


def outcome(journal: str) -> str:
    """skipped | backed-up | failed, from one run's own journal lines."""
    if SKIPPED in journal:
        return "skipped"
    if any(mark in journal for mark in ("backup failed", "Traceback", "(start failed:")):
        return "failed"
    return "backed-up"


def skip_problems(
    journal: str, hits: list[Hit], before: dict[str, Any], after: dict[str, Any], new_snapshots: int
) -> list[str]:
    """What is wrong with a run that should have been a recorded skip with one OK heartbeat ([] = nothing)."""
    out = []
    if f"{SKIPPED}nobody played and nothing changed" not in journal:
        out.append(f"the run didn't log the skip: {journal.strip()[-300:]!r}")
    if [h.path for h in hits] != ["/hb/backup"]:
        out.append(f"heartbeats {[h.path for h in hits]}, expected only the OK one (/hb/backup)")
    elif not hits[0].body.startswith(SKIPPED) or hits[0].auth != f"Bearer {TOKEN}":
        out.append(
            f"the OK heartbeat says {hits[0].body[:80]!r} (auth ok: {hits[0].auth == f'Bearer {TOKEN}'})"
        )
    if int(after.get("skips") or 0) != int(before.get("skips") or 0) + 1 or not after.get("last_skip"):
        out.append(f"backup-state.json didn't count the skip ({before.get('skips')} -> {after.get('skips')})")
    if after.get("last_backup") != before.get("last_backup") or after.get("fingerprint") != before.get(
        "fingerprint"
    ):
        out.append("backup-state.json's last backup changed on a skip")
    if not str(after.get("last_skip_reason", "")).startswith("nobody played"):
        out.append(f"backup-state.json's skip reason: {after.get('last_skip_reason')!r}")
    if new_snapshots:
        out.append(f"a skipped run made {new_snapshots} snapshot(s)")
    return out


def backup_problems(
    journal: str, hits: list[Hit], before: dict[str, Any], after: dict[str, Any], new_snapshots: int
) -> list[str]:
    """What is wrong with a run that should have backed up (start + ok heartbeats, a snapshot, recorded)."""
    out = []
    if outcome(journal) != "backed-up":
        out.append(f"the run didn't back up ({outcome(journal)}): {journal.strip()[-300:]!r}")
    paths = [h.path for h in hits]
    if paths != ["/hb/backup/start", "/hb/backup"] or hits[-1].body != "backup ok":
        out.append(f"heartbeats {[(h.path, h.body[:40]) for h in hits]}, expected start then `backup ok`")
    if new_snapshots < 1:
        out.append("no new snapshot")
    last, prev = after.get("last_backup"), before.get("last_backup")
    if not isinstance(last, int | float) or (isinstance(prev, int | float) and last <= prev):
        out.append(f"backup-state.json's last backup didn't move ({prev} -> {last})")
    if after.get("skips") != 0:
        out.append(f"backup-state.json's skip count wasn't reset ({after.get('skips')})")
    return out


def finished(journal: str) -> bool:
    """One run's journal is complete enough to judge: the skip line, a backup's last line (`save-off window:`,
    printed after restic finished), or a failure. backup.py's stdout reaches journald when it exits."""
    return any(mark in journal for mark in (SKIPPED, "save-off window:", "Traceback"))


def level_name(properties: str) -> str:
    for line in properties.splitlines():
        if line.startswith("level-name="):
            return line.split("=", 1)[1].strip() or "world"
    return "world"


# --- live ------------------------------------------------------------------------------------------------


@dataclass
class Watchdog:
    """An HTTPS mock of the watchdog's /hb endpoints on 127.0.0.1 (a self-signed cert for that address)."""

    hits: list[Hit] = field(default_factory=list)
    port: int = 0

    @contextlib.contextmanager
    def serving(self) -> Iterator[Watchdog]:
        mock = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode(errors="replace")
                mock.hits.append(Hit(self.path, body, self.headers.get("Authorization", "")))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                return

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(CERT, KEY)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        self.port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield self
        finally:
            server.shutdown()
            server.server_close()


def _sh(*argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, capture_output=True, text=True, check=False)


def make_cert() -> None:
    ETC.mkdir(mode=0o755, exist_ok=True)
    proc = _sh("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1", "-subj", "/CN=127.0.0.1",
               "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", str(KEY), "-out", str(CERT))  # fmt: skip
    if proc.returncode != 0:
        raise SystemExit(
            f"openssl couldn't make the mock watchdog's certificate: {proc.stderr.strip()[-300:]}"
        )
    CERT.chmod(0o644)  # the backup process (mcops) trusts it; the key stays root's
    KEY.chmod(0o600)


@contextlib.contextmanager
def timer_style(url: str) -> Iterator[None]:
    """The drop-in and its EnvironmentFile while the block runs; both removed (and systemd reloaded) after."""
    ENV_FILE.write_text(env_file(url, CERT))
    ENV_FILE.chmod(0o644)
    DROPIN_DIR.mkdir(parents=True, exist_ok=True)
    DROPIN.write_text(dropin())
    _sh("systemctl", "daemon-reload")
    try:
        yield
    finally:
        DROPIN.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            DROPIN_DIR.rmdir()
        _sh("systemctl", "daemon-reload")
        for path in (ENV_FILE, CERT, KEY):
            path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            ETC.rmdir()


def load_state() -> dict[str, Any]:
    try:
        data = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def run_unit() -> str:
    """mc-backup.service once (a new invocation; returns when the oneshot finished): its own journal lines."""
    return run_oneshot(UNIT, until=finished, timeout=30)[1]


def check(
    expect: Callable[[bool, str], None],
    snapshots: Callable[[], set[str]],
    run_unit: Callable[[], str] = run_unit,
) -> None:
    """The round. `expect(ok, what)` records a check; `snapshots()` = restic's snapshot ids now; `run_unit()`
    starts mc-backup.service once (a new invocation) and returns that invocation's journal lines."""
    make_cert()
    watchdog = Watchdog()
    with watchdog.serving(), timer_style(f"https://127.0.0.1:{watchdog.port}"):
        skipped = False
        for attempt in range(1, ATTEMPTS + 1):
            before, ids = load_state(), snapshots()
            watchdog.hits.clear()
            journal = run_unit()
            got = outcome(journal)
            print(f"backup-skip: timer-style run {attempt}: {got}")
            if got == "failed":
                expect(False, f"a timer-style backup run failed: {journal.strip()[-400:]!r}")
                return
            if got == "skipped":
                problems = skip_problems(
                    journal, list(watchdog.hits), before, load_state(), len(snapshots() - ids)
                )
                expect(not problems, f"an idle, unchanged timer run is skipped and recorded ({problems})")
                skipped = True
                break
            problems = backup_problems(
                journal, list(watchdog.hits), before, load_state(), len(snapshots() - ids)
            )
            expect(not problems, f"a timer run that found a change backed up ({problems})")
        expect(skipped, f"a timer-style run skipped within {ATTEMPTS} runs on the empty server")
        if not skipped:
            return
        world = SERVER / level_name((SERVER / "server.properties").read_text())
        marker = world / "e2e-backup-skip.txt"
        marker.write_text("changed after the skipped run")
        pw = pwd.getpwnam("minecraft")
        os.chown(marker, pw.pw_uid, pw.pw_gid)
        try:
            before, ids = load_state(), snapshots()
            watchdog.hits.clear()
            journal = run_unit()
            problems = backup_problems(
                journal, list(watchdog.hits), before, load_state(), len(snapshots() - ids)
            )
            expect(not problems, f"after a world file changed, the next timer run backs up ({problems})")
        finally:
            marker.unlink(missing_ok=True)


def main() -> int:
    """Standalone: `python3 tests/e2e/backup_skip_check.py` in the box (timers paused, restic via mc-restic)."""
    failures: list[str] = []

    def expect(ok: bool, what: str) -> None:
        print(("ok   " if ok else "FAIL ") + what)
        if not ok:
            failures.append(what)

    def snapshots() -> set[str]:
        proc = _sh("/usr/local/bin/mc-restic", "restic", "snapshots", "--json")
        return {s["id"] for s in json.loads(proc.stdout or "[]")} if proc.returncode == 0 else set()

    with timers_paused():
        check(expect, snapshots)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
