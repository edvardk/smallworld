#!/usr/bin/env python3
"""mc-agent: the server's outbound link to the Cloudflare Worker (design §5a, §8 P8). Stdlib only.

Runs as the ops user (never as minecraft: it holds the agent token and the command key), as
mc-agent.service. Outbound HTTPS only; every request carries `Authorization: Bearer <AGENT_TOKEN>`.

- Metrics: every METRICS_INTERVAL_S (60) one wide sample (TPS, MSPT, players, heap and GC from the GC
  log, JVM memory, CPU, steal, load, memory, disk, network) -> POST /ingest/metrics. Samples that can't be
  delivered are spooled on disk (STATE_DIRECTORY/spool.jsonl, up to 24 h) and sent oldest first once the
  Worker answers.
- Logs: every LOG_BATCH_S (300) the journal since the saved cursor, streamed (only the fields kept) in batches
  of at most 5000 entries / 8 MB; a backlog drains batch by batch. Only the product's own units (minecraft,
  mc-*, the agent's audit entries) and the firewall's drop lines leave the box, scrubbed of the secrets the
  agent knows and of well-known secret shapes: gzipped NDJSON to R2 (POST /ingest/archive) and, capped at
  LOG_MAX_ROWS_PER_DAY per UTC day, to D1 (POST /ingest/logs). The cursor only moves once both are
  delivered, so the journal itself is the buffer (1 GB, hardening role).
- Commands: a WebSocket (RFC 6455, minimal client; keepalive pings, reconnects after 2 min of silence) to the
  Control Durable Object (GET /agent/ws); when it can't be held, a poll every POLL_S (15) of
  GET /agent/commands, results to POST /agent/results. A command is
  {"payload": "<JSON text>", "sig": "<hex HMAC-SHA256 of payload with COMMAND_HMAC_KEY>"}; the payload is
  {id, action, args, login, iat, nonce}. The agent checks the signature, |now - iat| <= 60 s and that the
  nonce is new (kept for 10 min, also across restarts; an unreadable nonce store refuses every command for
  10 min), then the action allowlist below and the console allowlist (CONSOLE_ALLOWLIST), and runs it.
  Every command, accepted or not, is written to the local journal (MC_AUDIT=1), which the Worker can't
  alter.

Allowlist: status, console {command}, start, stop {reason, countdown, backup}, restart {countdown, backup},
backup, tail (P8/P9); P10 (mc_panel.py): cancel, whitelist {op, name}, rejected_dismiss {name}, moderate
{verb, target, reason}, plugin {name, enabled, countdown}, restore {snapshot, countdown}; P13 (telemetry.py):
player_message {to, kind, text, hover, id} (re-checked rate limits, queued for the mc-telemetry plugin);
v1.3: selftest (the Panel -> Git self-test: mc_panel.selftest, read-only, at most once a minute on demand,
daily on its own, Discord after two failures in a row), lookup {name} (Java name -> UUID from this box:
api.mojang.com, then api.minecraftservices.com, 3 s timeout each, 20 per minute, cached 10 min; only the name
is sent). The inventory carries the installed product version (PRODUCT_VERSION_FILE, written by each deploy)
and the one this process started with.
Systemd actions go through polkit (base role: 50-mc-ops.rules): minecraft start/stop/restart, mc-backup,
mc-plugins-apply and mc-restore@<snapshot> start. Nothing runs as root.

v1.3: `mc_agent.py cli LOGIN ACTION [ARGS_JSON]` runs one panel action for `mcctl` over Tailscale SSH (as
the ops user through `sudo mc-run`, see cli_main): the same Executor checks and job runner (countdown, one job
at a time across processes: mc_panel.JOB_LOCK_FILE), audited like a panel command with login `mcctl:<user>`.

v1.1 (CLIENT=1): /modpack's one-time codes. mc-brand (minecraft) writes CLIENT_REQUEST_DIR/<uuid>.req
(`mc-client-request 1` + uuid, name, nonce, ts); this agent POSTs /agent/client-code {uuid, name} (bearer
token: the Worker mints a 15-minute single-use code bound to the UUID) and writes the answer to
CLIENT_CODE_FILE (`mc-client-codes 1` + `C <uuid> <nonce> <code> <expires-ms>` or `E <uuid> <nonce>
<reason>`, mode 0640 in the setgid-minecraft verdict directory: the plugin reads it, never writes). The code
itself is never logged.

Env: INGEST_URL (https://hb.<host>), AGENT_TOKEN, COMMAND_HMAC_KEY, METRICS_INTERVAL_S, LOG_BATCH_S,
     LOG_MAX_ROWS_PER_DAY, POLL_S, STATE_DIRECTORY (systemd). http:// only to a loopback host (tests).
     P10: CONFIG_REPO (+ CONFIG_BRANCH, GITHUB_API_URL) for the whitelist PRs; their GitHub token is minted by
     the Worker (POST /agent/github-token, the panel's GitHub App; P10 review) and never stored,
     DISCORD_WEBHOOK_URL (expired pending changes), WATCHDOG_URL + HEARTBEAT_TOKEN (maintenance windows).
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import hmac
import io
import ipaddress
import json
import os
import re
import secrets
import select
import shutil
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import health
import mc_panel
import mc_rcon
import notify
import telemetry
import worlds

VERSION = "1"
USER_AGENT = "smallworld mc-agent"
UNIT = "minecraft.service"
BACKUP_UNIT = "mc-backup.service"
MAX_AGE_S = 60  # a command older (or newer) than this is refused
NONCE_TTL_S = 600
SPOOL_MAX_AGE_S = 86400  # design §5a: buffer up to 24 h
METRICS_BATCH = 200  # samples per request
LOG_ROW_MAX_CHARS = 2000
LOG_BATCH_MAX_ENTRIES = 5000  # per journalctl read; the next batch continues from the cursor
LOG_BATCH_MAX_BYTES = 8 << 20  # message text per batch (MemoryMax=64M)
LOG_ENTRY_MAX_CHARS = 8000  # longer journal messages are cut (the archive keeps the first 8000 chars)
LOG_CATCHUP_BATCHES = 20  # full batches shipped back to back before waiting for the next round
# P13 review M1: the log path (parse + scrub, CPU-bound) cedes the CPU and the GIL after every slice, so the
# metrics thread (the Worker's "host alive" signal) never queues behind a backlog on a CPU-starved box.
WORK_SLICE_S = 0.02
WORK_PAUSE_S = 0.005
JOURNAL_FIELDS = (
    "_SYSTEMD_UNIT,SYSLOG_IDENTIFIER,PRIORITY,MESSAGE,MC_AUDIT,UNIT"  # + cursor/timestamps (always)
)
CONSOLE_MAX = 256
MAX_CORES = 32  # per-core CPU fields in a sample (cpu0_pct ...)
INVENTORY_S = 600  # whitelist/ops/bans/plugins snapshot for the panel
INVENTORY_MAX = 1000  # entries per list
TAIL_MAX_S = 180
TAIL_BATCH = 50  # console lines per WebSocket message
PLAYERS = re.compile(r"There are (\d+) of a max(?: of)? (\d+)")
D1_UNITS = re.compile(r"^(minecraft\.service|mc-[a-z0-9@._-]+\.service)$")
D1_IDENTIFIERS = re.compile(r"^mc-[a-z0-9-]+$")
ACTIONS = (
    "status", "console", "start", "stop", "restart", "backup", "tail",
    "cancel", "whitelist", "rejected_dismiss", "moderate", "plugin", "restore",
    "player_message",
    "world_create", "world_switch", "world_delete",
    "selftest", "lookup",
)  # fmt: skip
# v1.3 "versions everywhere": what the last deploy installed (base role, root-owned, world-readable)
PRODUCT_VERSION_FILE = Path("/opt/minecraft/product-version.json")  # = ansible mc_product_version_file
# v1.3 Panel -> Git self-test: daily (first run a few minutes after start), at most once a minute on demand
SELFTEST_S = 86400
SELFTEST_FIRST_S = 300
SELFTEST_MIN_GAP_S = 60
# v1.3 Mojang lookup from the box (api.mojang.com refuses Cloudflare Workers' egress): the endpoints the
# Worker tries (watchdog/worker.js MOJANG_LOOKUPS), each with a short timeout; cached like the Worker's
MOJANG_LOOKUPS = (
    "https://api.mojang.com/users/profiles/minecraft/",
    "https://api.minecraftservices.com/minecraft/profile/lookup/name/",
)
LOOKUP_TIMEOUT_S = 3.0
LOOKUP_TTL_S = 600
LOOKUP_PER_MINUTE = 20  # lookups that reach Mojang per minute (cache hits don't count)
INVENTORY_MIN_GAP_S = 5  # a state change pushes the inventory at once, but not more often than this
RESTORE_TIMEOUT_S = 7200

# --- settings -----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Settings:
    ingest_url: str
    token: str
    hmac_key: bytes
    metrics_interval_s: int = 60
    log_batch_s: int = 300
    log_max_rows_per_day: int = 30000
    poll_s: int = 15
    state_dir: Path = Path("/var/lib/mc-agent")
    # P10's whitelist -> Git loop (only the e2e run shortens them)
    panel_sync_s: int = 30
    panel_batch_s: int = 60
    panel_pr_check_s: int = 120
    # P13: player-experience telemetry (the mc-telemetry plugin's snapshots -> minute rows -> Worker)
    experience: bool = False
    # v1.1: /modpack's one-time codes (mc-brand -> tmpfs -> here -> Worker -> the verdict directory)
    client: bool = False
    client_request_dir: Path = Path("/run/mc-telemetry/client")
    client_code_file: Path = Path("/run/mc-verdicts/client-codes.tsv")

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        url = e.get("INGEST_URL", "").rstrip("/")
        check_url(url)
        token, key = e.get("AGENT_TOKEN", ""), e.get("COMMAND_HMAC_KEY", "")
        if len(token) < 32 or len(key) < 32:
            raise ValueError("AGENT_TOKEN and COMMAND_HMAC_KEY must be set (>= 32 characters)")
        return cls(
            ingest_url=url,
            token=token,
            hmac_key=key.encode(),
            metrics_interval_s=min(300, max(30, int(e.get("METRICS_INTERVAL_S") or 60))),
            log_batch_s=max(10, int(e.get("LOG_BATCH_S") or 300)),
            log_max_rows_per_day=max(0, int(e.get("LOG_MAX_ROWS_PER_DAY") or 30000)),
            poll_s=max(5, int(e.get("POLL_S") or 15)),
            # StateDirectory= entries arrive joined by ":" (P13: mc-agent + mc-telemetry); ours is first
            state_dir=Path((e.get("STATE_DIRECTORY") or "/var/lib/mc-agent").split(":")[0]),
            panel_sync_s=max(1, int(e.get("PANEL_SYNC_S") or 30)),
            panel_batch_s=max(1, int(e.get("PANEL_BATCH_S") or 60)),
            panel_pr_check_s=max(1, int(e.get("PANEL_PR_CHECK_S") or 120)),
            experience=e.get("EXPERIENCE", "0") == "1",
            client=e.get("CLIENT", "0") == "1",
            client_request_dir=Path(e.get("CLIENT_REQUEST_DIR") or "/run/mc-telemetry/client"),
            client_code_file=Path(e.get("CLIENT_CODE_FILE") or "/run/mc-verdicts/client-codes.tsv"),
        )


def check_url(url: str) -> None:
    """https only; http only to a loopback address (the e2e mock ingest)."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https" and parts.hostname:
        return
    if parts.scheme == "http" and parts.hostname:
        with contextlib.suppress(ValueError):
            if parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname).is_loopback:
                return
    raise ValueError("INGEST_URL must be https:// (http:// only for a loopback address)")


# --- audit --------------------------------------------------------------------------------------------------

JOURNAL_SOCKET = "/run/systemd/journal/socket"


def audit(message: str, **fields: str) -> None:
    """One structured journal entry (SYSLOG_IDENTIFIER=mc-agent, MC_AUDIT=1); stderr if the socket is gone."""
    entry = {"MESSAGE": message, "SYSLOG_IDENTIFIER": "mc-agent", "MC_AUDIT": "1", "PRIORITY": "5"}
    entry |= {f"MC_{k.upper()}": v for k, v in fields.items()}
    data = b""
    for key, value in entry.items():
        raw = value.encode("utf-8", "replace")
        if b"\n" in raw:  # journal native protocol: binary-safe form for multi-line values
            data += key.encode() + b"\n" + struct.pack("<Q", len(raw)) + raw + b"\n"
        else:
            data += key.encode() + b"=" + raw + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as s:
            s.sendto(data, JOURNAL_SOCKET)
    except OSError:
        print(f"AUDIT {message}", file=sys.stderr)


class Warnings:
    """One journal line when something starts failing and one when it works again, not one per attempt (the
    journal is shipped too: an outage must not fill the day's D1 rows with the agent's own retries)."""

    def __init__(self) -> None:
        self.failing: dict[str, str] = {}

    def fail(self, key: str, text: str, *, relog: bool = False) -> None:
        """relog: one more line when the text changes (use only with a fixed set of texts, never per-attempt
        details)."""
        if key not in self.failing or (relog and self.failing[key] != text):
            print(f"{key}: {text} (retrying quietly)", file=sys.stderr)
        self.failing[key] = text

    def ok(self, key: str) -> None:
        if self.failing.pop(key, None) is not None:
            print(f"{key}: working again", file=sys.stderr)


WARN = Warnings()


# --- HTTP ---------------------------------------------------------------------------------------------------


class IngestError(Exception):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def request(
    s: Settings,
    method: str,
    path: str,
    body: bytes | None = None,
    content_type: str = "application/json",
    timeout: float = 20.0,
) -> Any:  # noqa: ANN401 - the Worker's JSON reply
    """One request to the Worker. Raises IngestError (never with the token or a body in the message)."""
    url = s.ingest_url + path
    headers = {"Authorization": f"Bearer {s.token}", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = content_type
    req = urllib.request.Request(url, data=body, headers=headers, method=method)  # noqa: S310 - checked
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            raw = resp.read(1 << 20)
    except urllib.error.HTTPError as e:
        raise IngestError(f"{method} {path}: HTTP {e.code}", e.code) from None
    except (OSError, urllib.error.URLError) as e:
        raise IngestError(f"{method} {path}: {type(e).__name__}") from None
    try:
        return json.loads(raw or b"{}")
    except ValueError:
        raise IngestError(f"{method} {path}: reply isn't JSON") from None


def post_json(s: Settings, path: str, payload: object) -> Any:  # noqa: ANN401
    return request(s, "POST", path, json.dumps(payload, separators=(",", ":")).encode())


# --- metrics ------------------------------------------------------------------------------------------------


@dataclass
class Counters:
    """Previous raw counters, for rates between two samples."""

    cpu: list[int] = field(default_factory=list)
    cores: list[list[int]] = field(default_factory=list)
    net: tuple[int, int] | None = None
    at: float = 0.0
    gc_uptime: float = 0.0


def parse_net_dev(text: str) -> tuple[int, int]:
    """Total received/transmitted bytes over all interfaces but lo (/proc/net/dev)."""
    rx = tx = 0
    for line in text.splitlines()[2:]:
        name, _, rest = line.partition(":")
        cols = rest.split()
        if name.strip() == "lo" or len(cols) < 9:
            continue
        rx, tx = rx + int(cols[0]), tx + int(cols[8])
    return rx, tx


def parse_meminfo(text: str) -> float | None:
    """Memory used % (MemTotal - MemAvailable)."""
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            info[key] = int(rest.split()[0])
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    return round(100.0 * (total - avail) / total, 1) if total and avail is not None else None


def cpu_busy_pct(prev: list[int], cur: list[int]) -> float | None:
    if len(prev) < 8 or len(cur) < 8:
        return None
    delta = [c - p for c, p in zip(cur[:8], prev[:8], strict=True)]
    total = sum(delta)
    if total <= 0 or any(d < 0 for d in delta):
        return None
    return round(100.0 * (total - delta[3] - delta[4]) / total, 1)  # minus idle and iowait


def last_heap(lines: list[str]) -> tuple[int, int] | None:
    """(heap used after the last GC, heap capacity) in MB from the GC log (G1 or ZGC: health.last_heap)."""
    return health.last_heap(lines)


def read(path: str) -> str:
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def unit_memory_mb(unit: str = UNIT) -> int | None:
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "MemoryCurrent", "--value", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    return int(out) // (1 << 20) if out.isdigit() else None


def unit_uptime_s(unit: str = UNIT) -> int | None:
    """Seconds since the unit became active (systemd's monotonic clock = time.monotonic()); None if not."""
    try:
        out = subprocess.run(
            ["systemctl", "show", "-p", "ActiveState", "-p", "ActiveEnterTimestampMonotonic", unit],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    usec = props.get("ActiveEnterTimestampMonotonic", "")
    if props.get("ActiveState") != "active" or not usec.isdigit() or int(usec) == 0:
        return None
    return max(0, int(time.monotonic() - int(usec) / 1e6))


def parse_cores(text: str) -> list[list[int]]:
    """Per-core counters (the cpuN lines of /proc/stat), in core order."""
    cores = []
    for line in text.splitlines():
        parts = line.split()
        if parts and parts[0].startswith("cpu") and parts[0][3:].isdigit():
            cores.append([int(x) for x in parts[1:]])
    return cores


def parse_meminfo_mb(text: str) -> tuple[int, int] | None:
    """(used, total) MB: MemTotal - MemAvailable, MemTotal."""
    info = {}
    for line in text.splitlines():
        key, _, rest = line.partition(":")
        if rest.split():
            info[key] = int(rest.split()[0])
    total, avail = info.get("MemTotal"), info.get("MemAvailable")
    return ((total - avail) // 1024, total // 1024) if total and avail is not None else None


def parse_tps_all(text: str) -> dict[str, float]:
    """{"1m": .., "5m": .., "15m": ..} (whatever the server reports) from `tps` output."""
    head, sep, tail = mc_rcon.strip_formatting(text).partition(":")
    labels = [w.strip() for w in head.rsplit("last", 1)[-1].split(",")]
    values = [float(v) for v in health.FLOAT.findall(tail)]
    return dict(zip(labels, values, strict=True)) if sep and len(labels) == len(values) else {}


def player_names(list_reply: str) -> list[str]:
    """The names after `list`'s colon (vanilla/Paper: "There are 2 of a max of 20 players online: a, b")."""
    _, sep, tail = mc_rcon.strip_formatting(list_reply).partition(":")
    names = [n.strip() for n in tail.split(",")] if sep else []
    return [n for n in names if re.fullmatch(r"[A-Za-z0-9_]{1,16}", n)][:100]


# One kept RCON connection per server dir (fix v1.0.1): a connection per poll made the server log "Thread RCON
# Client /127.0.0.1 started" / "shutting down" every minute. The metrics loop and `status` share it (locked).
RCON_TIMEOUT_S = 5.0
_RCON_CLIENTS: dict[Path, mc_rcon.PersistentRcon] = {}
_RCON_CLIENTS_LOCK = threading.Lock()


def stats_rcon(server_dir: Path) -> mc_rcon.PersistentRcon:
    with _RCON_CLIENTS_LOCK:
        client = _RCON_CLIENTS.get(server_dir)
        if client is None:
            client = _RCON_CLIENTS[server_dir] = mc_rcon.PersistentRcon(
                lambda: mc_rcon.connect(server_dir, timeout=RCON_TIMEOUT_S)
            )
        return client


def rcon_stats(server_dir: Path | None = None) -> dict[str, Any]:
    """Liveness, MSPT, TPS and players over the kept RCON connection (None where unknown)."""
    out: dict[str, Any] = {"up": None, "mspt": None, "tps": None, "players": None, "max_players": None}
    out |= {"tps5": None, "tps15": None, "players_online": []}
    cmds = ("mspt", "tps", "list")
    try:
        texts = dict(zip(cmds, stats_rcon(server_dir or mc_rcon.SERVER_DIR).commands(cmds), strict=True))
    except mc_rcon.RconSettingsError:
        return out
    except (mc_rcon.RconAuthError, mc_rcon.RconReplyError):
        out["up"] = True
        return out
    except (OSError, mc_rcon.RconError):
        out["up"] = False
        return out
    out["up"] = True
    for key, parse in (("mspt", health.parse_mspt), ("tps", health.parse_tps)):
        with contextlib.suppress(ValueError):
            out[key] = round(parse(texts[key]), 2)
    tps = parse_tps_all(texts["tps"])
    out["tps5"], out["tps15"] = tps.get("5m"), tps.get("15m")
    m = PLAYERS.search(mc_rcon.strip_formatting(texts["list"]))
    if m:
        out["players"], out["max_players"] = int(m.group(1)), int(m.group(2))
        out["players_online"] = player_names(texts["list"])
    return out


def collect(counters: Counters, now: float | None = None, server_dir: Path | None = None) -> dict[str, Any]:
    """One wide sample (design §5a). Updates `counters` in place."""
    now = time.time() if now is None else now
    sdir = server_dir or mc_rcon.SERVER_DIR
    sample: dict[str, Any] = {"ts": int(now * 1000)}
    sample |= rcon_stats(sdir)
    lines = health.read_gc_log(sdir)
    heap = last_heap(lines)
    sample["heap_used_mb"], sample["heap_max_mb"] = heap if heap else (None, None)
    gc = health.parse_gc(lines, counters.gc_uptime, 100.0)
    counters.gc_uptime = gc.last_uptime
    sample["gc_max_pause_ms"] = round(max([gc.max_pause_ms, *gc.stalls]), 1)
    sample["jvm_mem_mb"] = unit_memory_mb()
    sample["uptime_s"] = unit_uptime_s() if sample["up"] else None
    stat = read("/proc/stat")
    try:
        cpu = health.parse_proc_stat(stat)
    except ValueError:
        cpu = []
    cores = parse_cores(stat)
    sample["cpu_cores"] = len(cores) or None
    for i, core in enumerate(cores[:MAX_CORES]):
        prev = counters.cores[i] if i < len(counters.cores) else []
        sample[f"cpu{i}_pct"] = cpu_busy_pct(prev, core)
    counters.cores = cores
    sample["cpu_pct"] = cpu_busy_pct(counters.cpu, cpu)
    steal = health.steal_pct(counters.cpu, cpu)
    sample["steal_pct"] = round(steal, 1) if steal is not None else None
    counters.cpu = cpu
    load = read("/proc/loadavg").split()
    sample["load1"] = float(load[0]) if load else None
    sample["load5"] = float(load[1]) if len(load) > 2 else None
    sample["load15"] = float(load[2]) if len(load) > 2 else None
    meminfo = read("/proc/meminfo")
    sample["mem_pct"] = parse_meminfo(meminfo)
    mem = parse_meminfo_mb(meminfo)
    sample["mem_used_mb"], sample["mem_total_mb"] = mem if mem else (None, None)
    disk = health.disk_used_pct(sdir)
    sample["disk_pct"] = round(disk, 1) if disk is not None else None
    try:
        usage = shutil.disk_usage(sdir)
        sample["disk_used_gb"] = round(usage.used / (1 << 30), 2)
        sample["disk_total_gb"] = round(usage.total / (1 << 30), 2)
    except OSError:
        sample["disk_used_gb"] = sample["disk_total_gb"] = None
    net = parse_net_dev(read("/proc/net/dev"))
    dt = now - counters.at
    if counters.net and counters.at and dt > 0 and net[0] >= counters.net[0] and net[1] >= counters.net[1]:
        sample["net_rx_bps"] = int((net[0] - counters.net[0]) / dt)
        sample["net_tx_bps"] = int((net[1] - counters.net[1]) / dt)
    else:
        sample["net_rx_bps"] = sample["net_tx_bps"] = None
    counters.net, counters.at = net, now
    sample["admin_stopped"] = notify.admin_stopped()
    return sample


class Spool:
    """Undelivered samples, one JSON object per line, oldest first, at most SPOOL_MAX_AGE_S old."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = threading.Lock()

    def _load(self) -> list[dict[str, Any]]:
        out = []
        try:
            text = self.path.read_text()
        except OSError:
            return out
        for line in text.splitlines():
            with contextlib.suppress(ValueError):
                item = json.loads(line)
                if isinstance(item, dict) and isinstance(item.get("ts"), int):
                    out.append(item)
        return out

    def _save(self, items: list[dict[str, Any]]) -> None:
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text("".join(json.dumps(i, separators=(",", ":")) + "\n" for i in items))
        os.replace(tmp, self.path)

    def add(self, sample: dict[str, Any], now: float | None = None) -> None:
        with self.lock:
            cutoff = int(((time.time() if now is None else now) - SPOOL_MAX_AGE_S) * 1000)
            items = [i for i in self._load() if i["ts"] >= cutoff] + [sample]
            self._save(items)

    def flush(self, send: Callable[[list[dict[str, Any]]], None], now: float | None = None) -> int:
        """Send everything in batches, oldest first; stop at the first failure. Returns the number sent."""
        with self.lock:
            cutoff = int(((time.time() if now is None else now) - SPOOL_MAX_AGE_S) * 1000)
            items = [i for i in self._load() if i["ts"] >= cutoff]
            sent = 0
            try:
                while sent < len(items):
                    send(items[sent : sent + METRICS_BATCH])
                    sent += len(items[sent : sent + METRICS_BATCH])
            finally:
                if items[sent:] or self.path.exists():
                    self._save(items[sent:])
            return sent

    def __len__(self) -> int:
        with self.lock:
            return len(self._load())


def push_metrics(s: Settings, spool: Spool, sample: dict[str, Any]) -> int:
    """Queue the sample and deliver the spool. Returns how many samples went out (0: buffered)."""
    spool.add(sample)

    def send(batch: list[dict[str, Any]]) -> None:
        post_json(s, "/ingest/metrics", {"agent": VERSION, "samples": batch})

    try:
        sent = spool.flush(send)
    except IngestError as e:
        WARN.fail("metrics", f"buffering on disk: {e}")
        return 0
    WARN.ok("metrics")
    return sent


# --- logs ---------------------------------------------------------------------------------------------------


def message_text(value: object) -> str:
    if isinstance(value, list):  # journald exports non-UTF-8 messages as byte arrays
        with contextlib.suppress(TypeError, ValueError):
            return bytes(value[:LOG_ENTRY_MAX_CHARS]).decode("utf-8", "replace")
        return ""
    return value[:LOG_ENTRY_MAX_CHARS] if isinstance(value, str) else ""


def journal_priority(prio: object) -> int:
    """PRIORITY as an int, 6 (info) when it isn't one. v1.4 security fix: any local process sets the journal
    fields it sends, and digits `int()` refuses ("\u00b2" passes isdigit(); over 4300 digits) used to raise
    out of read_journal, so every log round failed at that entry and its cursor never moved past it (shipping
    stalled for good). Such a value is now the default, like any other malformed field."""
    if isinstance(prio, str) and prio.isdigit():
        with contextlib.suppress(ValueError):
            return int(prio)
    return 6


def journal_entry(raw: dict[str, Any]) -> dict[str, Any] | None:
    """The fields we keep from one `journalctl -o json` line."""
    try:
        ts = int(raw["__REALTIME_TIMESTAMP"]) // 1000
    except (KeyError, TypeError, ValueError):
        return None
    unit = raw.get("_SYSTEMD_UNIT") or raw.get("SYSLOG_IDENTIFIER") or ""
    return {
        "ts": ts,
        "unit": str(unit)[:100],
        "ident": str(raw.get("SYSLOG_IDENTIFIER") or "")[:100],
        "prio": journal_priority(raw.get("PRIORITY")),
        "msg": message_text(raw.get("MESSAGE")),
        "audit": 1 if raw.get("MC_AUDIT") == "1" else 0,
        # P13: systemd's own lines name the unit they are about ("mc-backup.service: Consumed 2.1s CPU time")
        "sd_unit": str(raw.get("UNIT") or "")[:100],
    }


def for_d1(entry: dict[str, Any]) -> bool:
    return bool(D1_UNITS.match(entry["unit"]) or D1_IDENTIFIERS.match(entry["ident"]))


FIREWALL_PREFIXES = ("mc-setfull:", "mc-ratelimit:", "mc-connlimit:")  # firewall role's log prefixes


def for_archive(entry: dict[str, Any]) -> bool:
    """P8 review: only the product's own units leave the box (minecraft, mc-*, mc-agent's audit entries) and
    the firewall's drop lines; not the whole journal (sudo, sshd, Ansible module arguments, other units)."""
    return for_d1(entry) or (entry["ident"] == "kernel" and entry["msg"].startswith(FIREWALL_PREFIXES))


# Secret shapes (P9 review): a name boundary is "no letter or digit before", not \b (which `_` defeats, so
# RESTIC_PASSWORD= or HEARTBEAT_TOKEN= slipped through); any *_KEY / *_SECRET / *_TOKEN / *_PASSWORD name;
# plain, quoted and JSON values; Basic and Bearer credentials; URL userinfo; GitHub, Tailscale, AWS shapes.
_NB = r"(?<![A-Za-z0-9])"
_SECRET_NAME = (
    rf"{_NB}(?:(?:[A-Za-z0-9]+[_.-])*(?:password|passwd|pass|pwd|token|secret|api[_-]?key)"
    r"|(?:[A-Za-z0-9]+[_.-])+key)"
)
SECRET_PATTERNS = [
    re.compile(r"tskey-[A-Za-z0-9_-]+"),
    re.compile(r"(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(rf"(?i)({_NB}bearer\s+)[^\s\"',;]+"),
    re.compile(rf"(?i)({_NB}basic\s+)[A-Za-z0-9+/=._-]{{6,}}"),
    re.compile(rf"""(?i)({_SECRET_NAME}["']?\s*[=:]\s*)("[^"]*"|'[^']*'|[^\s"',;&}}\]]+)"""),
    re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])"),
    re.compile(r"(?i)https://(?:[a-z0-9-]+\.)?discord(?:app)?\.com/api/webhooks/\S+"),
    re.compile(r"(?i)((?<![a-z0-9+.-])[a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+(?=@)"),
]
# Environment variables whose values the scrubber removes wherever they appear (every secret this agent can
# read: its own env file and the RCON password from server.properties).
SECRET_ENV = (
    "AGENT_TOKEN", "COMMAND_HMAC_KEY", "HEARTBEAT_TOKEN", "DISCORD_WEBHOOK_URL",
)  # fmt: skip
SECRET_SUFFIXES = ("_TOKEN", "_KEY", "_SECRET", "_PASSWORD", "_WEBHOOK_URL")


class Slicer:
    """Time-slices CPU-bound work in a thread (P13 review M1): call it once per item; after `slice_s` of wall
    time it sleeps `pause_s`, which releases the GIL at once (instead of waiting out the interpreter's switch
    interval each time another thread wants it) and gives the CPU to the process's other threads. Wall time,
    not CPU time: on a starved box the slices get shorter in CPU terms, which is what we want."""

    def __init__(
        self,
        slice_s: float = WORK_SLICE_S,
        pause_s: float = WORK_PAUSE_S,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.slice_s, self.pause_s, self.clock, self.sleep = slice_s, pause_s, clock, sleep
        self.start = clock()
        self.pauses = 0

    def __call__(self) -> None:
        if self.clock() - self.start >= self.slice_s:
            self.sleep(self.pause_s)
            self.pauses += 1
            self.start = self.clock()


class Scrubber:
    """Removes secrets from log text before it leaves the box: the exact values this agent knows (its
    token, the command key, the heartbeat token, the Discord webhook, the panel's GitHub token, the RCON
    password) and well-known secret shapes."""

    def __init__(self, known: Iterable[str]) -> None:
        self.known = sorted({k for k in known if len(k) >= 8}, key=len, reverse=True)

    def __call__(self, text: str) -> str:
        for value in self.known:
            if value in text:
                text = text.replace(value, "<redacted>")
        for pat in SECRET_PATTERNS:
            text = pat.sub(lambda m: (m.group(1) if m.groups() else "") + "<redacted>", text)
        return text


def known_secrets(s: Settings, env: dict[str, str] | None = None) -> list[str]:
    """Every secret value this agent can read: its settings, the listed variables and any *_TOKEN/_KEY/... in
    its environment, and the RCON password."""
    e = os.environ if env is None else env
    known = [s.token, s.hmac_key.decode("utf-8", "replace")]
    known += [v for k, v in e.items() if k in SECRET_ENV or k.endswith(SECRET_SUFFIXES)]
    with contextlib.suppress(OSError, KeyError, ValueError):
        known.append(mc_rcon.read_properties(mc_rcon.SERVER_DIR / "server.properties")["rcon.password"])
    return [k for k in known if k]


def scrubber_for(s: Settings) -> Scrubber:
    return Scrubber(known_secrets(s))


def journal_argv(cursor: str | None) -> list[str]:
    argv = ["journalctl", "-o", "json", "--no-pager", f"--output-fields={JOURNAL_FIELDS}"]
    return argv + ([f"--after-cursor={cursor}"] if cursor else ["--since=-5min"])


def read_journal(
    cursor: str | None,
    limit: int = LOG_BATCH_MAX_ENTRIES,
    max_bytes: int = LOG_BATCH_MAX_BYTES,
    argv: list[str] | None = None,
    cede: Callable[[], None] | None = None,
) -> tuple[list[dict[str, Any]], str | None, bool]:
    """Up to `limit` entries after `cursor` (or the last 5 minutes on the first run), the cursor of the last
    entry read, and whether the batch was cut short (more are waiting). Streams journalctl's output line by
    line (only the fields we keep) and stops reading at the cap, so a large backlog never sits in memory."""
    cmd = argv or journal_argv(cursor)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    except OSError as e:
        raise IngestError(f"journalctl failed: {type(e).__name__}") from None
    entries: list[dict[str, Any]] = []
    last, size, full = cursor, 0, False
    cede = cede or Slicer()
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            cede()
            try:
                raw = json.loads(line)
            except ValueError:
                continue
            if not isinstance(raw, dict):
                continue
            last = raw.get("__CURSOR", last)
            entry = journal_entry(raw)
            if entry:
                entries.append(entry)
                size += len(entry["msg"])
            if len(entries) >= limit or size >= max_bytes:
                full = True
                break
    finally:
        if proc.poll() is None:
            proc.kill()
        if proc.stdout is not None:
            proc.stdout.close()
        proc.wait(timeout=30)
    if not full and proc.returncode not in (0, None) and not entries:
        raise IngestError(f"journalctl failed: exit {proc.returncode}")
    return entries, last, full


@dataclass
class LogState:
    cursor: str | None = None
    day: str = ""  # UTC YYYYMMDD of `rows`
    rows: int = 0  # D1 rows delivered that day

    @classmethod
    def load(cls, path: Path) -> LogState:
        try:
            data = json.loads(path.read_text())
            return cls(data.get("cursor"), str(data.get("day", "")), int(data.get("rows", 0)))
        except (OSError, ValueError, TypeError, AttributeError):
            return cls()

    def save(self, path: Path) -> None:
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"cursor": self.cursor, "day": self.day, "rows": self.rows}))
        os.replace(tmp, path)


def utc_day(ms: int) -> str:
    return time.strftime("%Y%m%d", time.gmtime(ms / 1000))


def select_rows(entries: Iterable[dict[str, Any]], st: LogState, cap: int) -> list[dict[str, Any]]:
    """The D1 rows of this batch within today's cap (the cap counts per UTC day; updates `st`)."""
    rows = []
    for e in entries:
        if not for_d1(e):
            continue
        day = utc_day(e["ts"])
        if day != st.day:
            st.day, st.rows = day, 0
        if st.rows >= cap:
            continue
        st.rows += 1
        row = {k: e[k] for k in ("ts", "unit", "prio", "audit")}
        row["msg"] = e["msg"][:LOG_ROW_MAX_CHARS]
        rows.append(row)
    return rows


def archive_bytes(entries: list[dict[str, Any]]) -> bytes:
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        for e in entries:
            gz.write((json.dumps(e, separators=(",", ":")) + "\n").encode())
    return buf.getvalue()


def ship_logs(
    s: Settings,
    path: Path,
    read_fn: Callable[..., Any] = read_journal,
    scrub: Callable[[str], str] | None = None,
    observe: Callable[[list[dict[str, Any]]], None] | None = None,
    observe_all: Callable[[list[dict[str, Any]]], None] | None = None,
    cede: Callable[[], None] | None = None,
) -> tuple[int, bool]:
    """One log batch: archive the product's units, D1 the important ones, both scrubbed. Returns the entries
    shipped and whether more are waiting (the batch hit its cap). The cursor moves only after delivery;
    `observe` (P10: rejected logins) sees minecraft.service's entries once they are delivered."""
    st = LogState.load(path)
    entries, cursor, more = read_fn(st.cursor)
    if (
        observe_all
    ):  # P13: systemd's per-run resource lines for the overhead card (read-only, before scrubbing)
        observe_all(entries)
    seen = [dict(e) for e in entries if e["unit"] == UNIT] if observe else []
    scrub = scrub or scrubber_for(s)
    cede = cede or Slicer()
    kept = []
    for e in entries:
        cede()
        if for_archive(e):
            e["msg"] = scrub(e["msg"])
            kept.append(e)
    if not kept:
        if cursor != st.cursor:
            st.cursor = cursor
            st.save(path)
        if observe and seen:
            observe(seen)
        return 0, more
    batch = hashlib.sha256((cursor or "").encode()).hexdigest()[:32]
    request(s, "POST", f"/ingest/archive?batch={batch}", archive_bytes(kept), "application/gzip", 60.0)
    trial = LogState(st.cursor, st.day, st.rows)
    rows = select_rows(kept, trial, s.log_max_rows_per_day)
    if rows:
        post_json(s, "/ingest/logs", {"agent": VERSION, "rows": rows})
    trial.cursor = cursor
    trial.save(path)
    if observe and seen:
        observe(seen)
    return len(kept), more


# --- inventory (P9: the panel's players, bans and plugins views) --------------------------------------------


def read_json_obj(path: Path) -> dict[str, Any]:
    try:  # the server dir is minecraft's: no FIFO, no symlink, capped (mc_rcon.read_untrusted)
        data = json.loads(mc_rcon.read_untrusted(path))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def read_json_list(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(mc_rcon.read_untrusted(path))
    except (OSError, ValueError):
        return []
    return [d for d in data if isinstance(d, dict)][:INVENTORY_MAX] if isinstance(data, list) else []


def _clip(entry: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in keys:
        v = entry.get(k)
        if isinstance(v, bool | int):
            out[k] = v
        elif isinstance(v, str):
            out[k] = v[:200]
    return out


def collect_inventory(server_dir: Path | None = None) -> dict[str, Any]:
    """What the server's own files say: whitelist, ops, bans, plugin jars (enabled = in plugins/, disabled =
    in plugins/disabled/). Read with the ops user's group read on the server dir; nothing secret in here."""
    sdir = server_dir or mc_rcon.SERVER_DIR
    plugins = []
    for sub, enabled in (("plugins", True), ("plugins/disabled", False)):
        with contextlib.suppress(OSError):
            for jar in sorted((sdir / sub).glob("*.jar"))[:INVENTORY_MAX]:
                plugins.append({"file": jar.name[:200], "name": jar.stem[:100], "enabled": enabled})
    # v1.3 whitelist guard: the players the last deploy kept although players.yml doesn't list them (the panel
    # tags them "not in Git"); written as minecraft by the players role, so read like every file in there
    try:
        guard = json.loads(mc_rcon.read_untrusted(sdir / mc_panel.PLAYERS_STATE_FILE))
    except (OSError, ValueError):
        guard = {}
    listed = guard.get("kept") if isinstance(guard, dict) else None
    kept = {u for u in listed if isinstance(u, str)} if isinstance(listed, list) else set()
    whitelist = []
    for e in read_json_list(sdir / "whitelist.json"):
        item = _clip(e, ("name", "uuid"))
        if isinstance(e.get("uuid"), str) and e["uuid"].lower() in kept:
            item["unmanaged"] = True
        whitelist.append(item)
    return {
        "ts": int(time.time() * 1000),
        "whitelist": whitelist,
        "ops": [_clip(e, ("name", "uuid", "level")) for e in read_json_list(sdir / "ops.json")],
        "bans": [
            _clip(e, ("name", "uuid", "created", "source", "expires", "reason"))
            for e in read_json_list(sdir / "banned-players.json")
        ],
        "ip_bans": [
            _clip(e, ("ip", "created", "source", "expires", "reason"))
            for e in read_json_list(sdir / "banned-ips.json")
        ],
        "plugins": plugins,
    }


class ConsoleTail:
    """P9 live console: follows minecraft.service's journal and sends its lines over the WebSocket while a
    panel viewer asked for it (signed `tail` command, renewed by the panel while the Console tab is open)."""

    def __init__(self, agent: Agent) -> None:
        self.agent = agent
        self.until = 0.0
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None

    def request(self, seconds: int) -> str:
        if self.agent.ws is None:
            raise Rejected("tail: the live console needs the WebSocket (the agent is polling)")
        with self.lock:
            self.until = max(self.until, time.monotonic() + seconds)
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self.run, name="console-tail", daemon=True)
                self.thread.start()
        return f"live console for {seconds} s"

    def argv(self) -> list[str]:
        return ["journalctl", "-f", "-n", "50", "-o", "json", "--no-pager", f"--unit={UNIT}",
                "--output-fields=MESSAGE,PRIORITY"]  # fmt: skip

    def run(self) -> None:
        scrub = scrubber_for(self.agent.s)
        try:
            proc = subprocess.Popen(self.argv(), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError as e:
            print(f"console tail: journalctl failed: {type(e).__name__}", file=sys.stderr)
            return
        assert proc.stdout is not None
        fd, pending = proc.stdout.fileno(), b""
        try:
            while time.monotonic() < self.until and not self.agent.stop.is_set():
                ready, _, _ = select.select([fd], [], [], 0.5)
                chunk = os.read(fd, 65536) if ready else b""
                if ready and not chunk:
                    break  # journalctl ended
                pending += chunk
                *lines, pending = pending.split(b"\n")
                pending = pending[-65536:]
                batch = [e for e in map(self.line, lines) if e]
                for e in batch:
                    e["msg"] = scrub(e["msg"])
                ws = self.agent.ws
                if ws is None:
                    break
                for i in range(0, len(batch), TAIL_BATCH):
                    ws.send_text(json.dumps({"type": "console", "lines": batch[i : i + TAIL_BATCH]}))
        except (OSError, WebSocketError) as e:
            print(f"console tail ended: {type(e).__name__}", file=sys.stderr)
        finally:
            proc.kill()
            proc.wait(timeout=10)
            proc.stdout.close()

    @staticmethod
    def line(raw: bytes) -> dict[str, Any] | None:
        with contextlib.suppress(ValueError):
            data = json.loads(raw)
            if isinstance(data, dict):
                e = journal_entry(data)
                if e:
                    return {"ts": e["ts"], "prio": e["prio"], "msg": e["msg"][:LOG_ROW_MAX_CHARS]}
        return None


def read_product(path: Path | None = None) -> dict[str, str] | None:
    """v1.3: the product version the last deploy installed ({version, commit}), or None (no file yet: a box
    deployed before v1.3, or not deployed by the product)."""
    data = read_json_obj(path or PRODUCT_VERSION_FILE)
    version = data.get("version")
    if not isinstance(version, str) or not re.fullmatch(r"[0-9A-Za-z.+-]{1,40}", version):
        return None
    commit = data.get("commit")
    commit = commit if isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}", commit) else ""
    return {"version": version, "commit": commit}


def lookup_urls(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """MOJANG_LOOKUPS, or MOJANG_LOOKUP_URLS (comma-separated; the e2e run's loopback mock, http:// for
    loopback only, like INGEST_URL)."""
    raw = (os.environ if env is None else env).get("MOJANG_LOOKUP_URLS", "")
    if not raw:
        return MOJANG_LOOKUPS
    urls = tuple(u.strip() for u in raw.split(",") if u.strip())
    for u in urls:
        check_url(u)
    return urls


class Lookup:
    """`lookup {name}` (v1.3): a Java name -> UUID from this box, not the Worker (Mojang answers Cloudflare's
    shared egress with 403). Only the name leaves the box; answers cached for LOOKUP_TTL_S, at most
    LOOKUP_PER_MINUTE requests to Mojang per minute; no answer from either endpoint is "unverified", never an
    error."""

    def __init__(
        self,
        urls: Iterable[str] = MOJANG_LOOKUPS,
        opener: Callable[[urllib.request.Request, float], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.urls = tuple(urls)
        self.opener = opener or (lambda req, timeout: urllib.request.urlopen(req, timeout=timeout))  # noqa: S310
        self.clock = clock
        self.cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.recent: list[float] = []
        self.lock = threading.Lock()

    def _get(self, url: str) -> tuple[int, bytes]:
        req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": USER_AGENT})  # noqa: S310
        try:
            with self.opener(req, LOOKUP_TIMEOUT_S) as resp:
                return int(getattr(resp, "status", 200)), resp.read(65536)
        except urllib.error.HTTPError as e:
            return e.code, b""

    def __call__(self, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or not re.fullmatch(NAME, name):
            raise Rejected("lookup: a Java name is 3-16 letters, digits or _")
        key, now = name.lower(), self.clock()
        with self.lock:
            hit = self.cache.get(key)
            if hit and now - hit[0] < LOOKUP_TTL_S:
                return hit[1] | {"cached": True}
            self.recent = [t for t in self.recent if now - t < 60]
            if len(self.recent) >= LOOKUP_PER_MINUTE:
                raise Rejected(f"lookup: rate limit ({LOOKUP_PER_MINUTE} per minute)")
            self.recent.append(now)
        result: dict[str, Any] | None = None
        failures = []
        for base in self.urls:
            host = urllib.parse.urlsplit(base).hostname or "?"
            try:
                status, body = self._get(base + urllib.parse.quote(name))
            except (OSError, ValueError) as e:  # URLError, timeouts
                failures.append(f"{host}: {type(e).__name__}")
                continue
            if status in (204, 404):
                result = {"found": False, "via": host}
                break
            if status == 200:
                try:
                    data = json.loads(body or b"{}")
                except ValueError:
                    data = None
                pid = str(data.get("id") or "").lower() if isinstance(data, dict) else ""
                if re.fullmatch(r"[0-9a-f]{32}", pid):
                    uuid = f"{pid[:8]}-{pid[8:12]}-{pid[12:16]}-{pid[16:20]}-{pid[20:]}"
                    got = data.get("name") if isinstance(data, dict) else None
                    shown = got if isinstance(got, str) and re.fullmatch(NAME, got) else name
                    result = {"found": True, "name": shown, "uuid": uuid, "via": host}
                    break
                failures.append(f"{host}: unexpected answer")
                continue
            failures.append(f"{host}: HTTP {status}")
        if result is None:  # not cached: the next try may get through
            return {"found": None, "unverified": True, "detail": "; ".join(failures)[:160]}
        with self.lock:
            if len(self.cache) > 200:
                self.cache.pop(next(iter(self.cache)))
            self.cache[key] = (now, result)
        return result


def push_inventory(s: Settings, server_dir: Path | None = None, extra: dict[str, Any] | None = None) -> None:
    inventory = collect_inventory(server_dir) | (extra or {})
    post_json(s, "/ingest/inventory", {"agent": VERSION, "inventory": inventory})


# --- commands -----------------------------------------------------------------------------------------------


class Rejected(Exception):
    """A command that fails a check (signature, age, replay, action or console allowlist)."""


class Nonces:
    """Nonces seen in the last NONCE_TTL_S, persisted so a restart doesn't reopen the replay window.

    Fail closed (P8 review): if the file exists but can't be read or parsed, the nonces it held are unknown,
    so every command is refused for NONCE_TTL_S (any of them could be a replay), then the store starts
    afresh. A nonce that can't be persisted refuses its command too."""

    def __init__(self, path: Path | None, now: float | None = None) -> None:
        self.path = path
        self.seen: dict[str, float] = {}
        self.blocked_until = 0.0
        if not path:
            return
        try:
            data = json.loads(path.read_text())
            if not isinstance(data, dict):
                raise ValueError("not an object")
            self.seen = {str(k): float(v) for k, v in data.items()}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as e:
            self.blocked_until = (time.time() if now is None else now) + NONCE_TTL_S
            msg = f"nonce store {path.name} unreadable ({type(e).__name__}): commands refused {NONCE_TTL_S} s"
            print(f"mc-agent: {msg}", file=sys.stderr)
            audit(msg, action="nonces", result="fail-closed")

    def use(self, nonce: str, now: float) -> bool:
        """Record the nonce; False if it was seen already. Raises Rejected while failing closed."""
        if now < self.blocked_until:
            raise Rejected("nonce store was unreadable: refusing commands until the replay window has passed")
        self.seen = {n: t for n, t in self.seen.items() if now - t < NONCE_TTL_S}
        if nonce in self.seen:
            return False
        self.seen[nonce] = now
        if self.path:
            tmp = self.path.with_name(self.path.name + ".tmp")
            try:
                tmp.write_text(json.dumps(self.seen))
                os.replace(tmp, self.path)
            except OSError as e:
                raise Rejected(f"nonce store can't be written ({type(e).__name__})") from None
        return True


def sign(key: bytes, payload: str) -> str:
    return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()


def verify(message: object, key: bytes, nonces: Nonces, now: float | None = None) -> dict[str, Any]:
    """Check signature, age and nonce of {"payload", "sig"}; returns the command. Raises Rejected."""
    now = time.time() if now is None else now
    if not isinstance(message, dict):
        raise Rejected("not a command object")
    payload, sig = message.get("payload"), message.get("sig")
    if not isinstance(payload, str) or not isinstance(sig, str) or len(payload) > 8192:
        raise Rejected("malformed command")
    if not hmac.compare_digest(sign(key, payload), sig.lower()):
        raise Rejected("bad signature")
    try:
        cmd = json.loads(payload)
    except ValueError:
        raise Rejected("payload isn't JSON") from None
    if not isinstance(cmd, dict):
        raise Rejected("payload isn't an object")
    for name, kind in (("id", str), ("action", str), ("login", str), ("nonce", str), ("args", dict)):
        if not isinstance(cmd.get(name), kind):
            raise Rejected(f"missing or bad field: {name}")
    iat = cmd.get("iat")
    if not isinstance(iat, int | float) or isinstance(iat, bool):
        raise Rejected("missing or bad field: iat")
    if abs(now - iat / 1000) > MAX_AGE_S:  # iat in ms, like the Worker's Date.now()
        raise Rejected("too old (or from the future)")
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", cmd["nonce"]) or not re.fullmatch(
        r"[A-Za-z0-9_-]{1,64}", cmd["id"]
    ):
        raise Rejected("bad id or nonce")
    if not nonces.use(cmd["nonce"], now):
        raise Rejected("replayed nonce")
    return cmd


# --- console allowlist (P8 review: a denylist can't be complete) --------------------------------------------
#
# A console line runs only if its first word is on this list and its arguments fit that command's shape. All
# else is refused: `execute`, `return`, `function` (they run other commands), op/deop, stop/restart/reload,
# save-off, any namespaced head (`minecraft:op`, `bukkit:reload`, ...), selectors where a player is expected,
# and anything that changes what Git or the deploy owns (gamerules and the world border are read only,
# whitelist changes go through the panel's Git flow). The line is NFKC-normalised first (full-width letters
# become ASCII), control/format characters and non-space whitespace are refused, and the normalised text is
# what reaches RCON, so what was checked is what runs.

NAME = r"[A-Za-z0-9_]{3,16}"
IP = r"(?:\d{1,3}(?:\.\d{1,3}){3}|[0-9A-Fa-f:]*:[0-9A-Fa-f:.]+)"
DURATION = r"\d{1,9}[dst]?"
BAD_CATEGORIES = {"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"}
REFUSED_HEADS = {
    "execute": "`execute` runs other commands",
    "return": "`return` runs other commands",
    "function": "functions run other commands",
    "op": "operators are managed in Git (players.yml)",
    "deop": "operators are managed in Git (players.yml)",
    "stop": "use the panel's Stop (countdown, admin-stopped marker)",
    "restart": "use the panel's Restart",
    "reload": "/reload breaks plugins; restart instead",
    "rl": "/reload breaks plugins; restart instead",
    "save-off": "turning saving off would break backups",
}


def _shape(*patterns: str) -> re.Pattern[str]:
    """The allowed argument strings (space-joined) of one command, as one anchored regex."""
    return re.compile("|".join(f"(?:{p})" for p in patterns), re.ASCII)


# head -> the allowed argument text ("" = no arguments). Aliases share a shape.
CONSOLE_ALLOWLIST: dict[str, re.Pattern[str]] = {
    "list": _shape("", "uuids"),
    "say": _shape(r".+"),
    "me": _shape(r".+"),
    "tell": _shape(rf"(?:{NAME}|@[apr]) .+"),
    "msg": _shape(rf"(?:{NAME}|@[apr]) .+"),
    "w": _shape(rf"(?:{NAME}|@[apr]) .+"),
    "kick": _shape(rf"{NAME}(?: .+)?"),
    "ban": _shape(rf"{NAME}(?: .+)?"),
    "ban-ip": _shape(rf"(?:{IP}|{NAME})(?: .+)?"),
    "pardon": _shape(NAME),
    "pardon-ip": _shape(IP),
    "banlist": _shape("", "players", "ips"),
    "whitelist": _shape("list", "on", "reload"),
    "save-all": _shape("", "flush"),
    "save-on": _shape(""),
    "seed": _shape(""),
    "tps": _shape(""),
    "mspt": _shape(""),
    "plugins": _shape(""),
    "pl": _shape(""),
    "version": _shape("", r"[A-Za-z0-9_.-]{1,64}"),
    "ver": _shape("", r"[A-Za-z0-9_.-]{1,64}"),
    "help": _shape("", r"[A-Za-z0-9_-]{1,32}"),
    "time": _shape(
        "query (?:daytime|gametime|day)", rf"set (?:day|night|noon|midnight|{DURATION})", f"add {DURATION}"
    ),
    "weather": _shape(rf"(?:clear|rain|thunder)(?: {DURATION})?"),
    "difficulty": _shape("", "peaceful|easy|normal|hard"),
    "gamerule": _shape(r"[A-Za-z0-9_.:]{1,64}"),  # read only: setting one is server.yml's job
    "worldborder": _shape("get"),
    "spark": _shape("tps", "gc", "ping", r"health(?: --(?:memory|network))*"),  # no profiler/upload
    "chunky": _shape("progress", "pause", "continue", "selection", "help"),
}


FREE_TEXT = {"say", "me", "tell", "msg", "w", "kick", "ban", "ban-ip"}  # keep the case of their message
# P9 review: a selector inside a chat message (`say @e`, `me @a[distance=..5]`) is expanded by the server into
# every matching entity's name: cosmetic at best, a chat flood (and work) with `@e` on a busy world. Messages
# from the panel are refused if a word is a selector; `tell`'s *target* may still be @a/@p/@r.
SELECTOR_WORD = re.compile(r"@[aeprsn](?:\[.*)?", re.ASCII)
MESSAGE_FROM = {"say": 1, "me": 1, "tell": 2, "msg": 2, "w": 2}


def normalise_console(command: object) -> list[str]:
    """The words of one console line after NFKC normalisation; raises Rejected for a malformed line."""
    if not isinstance(command, str) or not command.strip():
        raise Rejected("console: empty command")
    if len(command) > CONSOLE_MAX:
        raise Rejected(f"console: one line of at most {CONSOLE_MAX} characters")
    text = unicodedata.normalize("NFKC", command)
    if len(text) > CONSOLE_MAX:  # P9 review: NFKC can expand a character into many (U+FDFA is 18)
        raise Rejected(f"console: one line of at most {CONSOLE_MAX} characters (after normalisation)")
    for ch in text:
        if unicodedata.category(ch) in BAD_CATEGORIES or (ch.isspace() and ch != " "):
            raise Rejected("console: one line of printable text (no control characters)")
    words = text.strip(" ").lstrip("/").split()
    if not words:
        raise Rejected("console: empty command")
    words[0] = words[0].lower()
    return words


def check_console(command: object) -> str:
    """The line to send over RCON if it is on the allowlist; raises Rejected otherwise."""
    words = normalise_console(command)
    head, args = words[0], " ".join(words[1:])
    if ":" in head:
        raise Rejected(f"console: namespaced commands (`{head[:40]}`) are not allowed from the panel")
    if head in REFUSED_HEADS:
        raise Rejected(f"console: `{head}` is not allowed from the panel ({REFUSED_HEADS[head]})")
    if head == "whitelist" and words[1:2] and words[1].lower() in ("add", "remove"):
        raise Rejected("console: use the panel's whitelist flow (it keeps players.yml in Git)")
    shape = CONSOLE_ALLOWLIST.get(head)
    if shape is None:
        raise Rejected(f"console: `{head[:40]}` is not allowed from the panel (not on the console allowlist)")
    if not shape.fullmatch(args if head in FREE_TEXT else args.lower()):
        raise Rejected(f"console: these `{head}` arguments are not allowed from the panel")
    if head in MESSAGE_FROM and any(SELECTOR_WORD.fullmatch(w) for w in words[MESSAGE_FROM[head] :]):
        raise Rejected(f"console: selectors (@a, @e, ...) inside a `{head}` message are not allowed here")
    return " ".join(words)


def running_units() -> list[str]:
    """P13 lag context: which of our heavier jobs run right now (backup, restore)."""
    names = {
        "mc-backup.service": "backup",
        "mc-restore-apply.service": "restore",
        "mc-prune.service": "prune",
    }
    try:
        out = subprocess.run(
            ["systemctl", "is-active", *names], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    states = out.split()
    return [
        label
        for (unit, label), st in zip(names.items(), states, strict=False)
        if st in ("active", "activating")
    ]


def systemctl(*args: str, timeout: float = 300) -> None:
    subprocess.run(
        ["systemctl", "--no-ask-password", *args], check=True, timeout=timeout, capture_output=True
    )


@dataclass
class Panel:
    """P10 state the executor needs beyond P8's: the job runner, pending whitelist changes, rejected logins,
    and `changed()` to push the inventory now (the panel shows the new state without waiting 10 minutes)."""

    jobs: mc_panel.Jobs
    pending: mc_panel.Pending
    rejected: mc_panel.RejectedLogins
    changed: Callable[[], None] = lambda: None
    server_dir: Path | None = None
    plugins_lock: Path | None = None
    plugins_disabled: Path | None = None
    snapshots: Path | None = None
    worlds_state: Path | None = None  # v1.2: /var/lib/mc-snapshots/worlds.json (archives, active)
    worlds_library: Path | None = (
        None  # v1.2: /opt/minecraft/worlds-library.json (the deploy's Git + pending)
    )
    world_request: Path | None = None  # v1.2: /var/lib/minecraft/world-request.json (for mc-world.service)
    clock: Callable[[], float] = time.time
    # v1.3: the Panel -> Git self-test (runs it, returns the result) and the box-side Mojang lookup
    selftest: Callable[[str], dict[str, Any]] | None = None
    lookup: Callable[[str], dict[str, Any]] | None = None


def _flag(args: dict[str, Any], key: str, default: bool) -> bool:
    v = args.get(key, default)
    if not isinstance(v, bool):
        raise Rejected(f"{key} must be true or false")
    return v


def _countdown(args: dict[str, Any]) -> int:
    v = args.get("countdown", 0)
    if not isinstance(v, int) or isinstance(v, bool) or v not in mc_panel.COUNTDOWNS:
        raise Rejected(f"countdown must be one of {', '.join(map(str, mc_panel.COUNTDOWNS))} seconds")
    return v


def _one_line(text: object, limit: int = 100) -> str:
    """A reason/message for the server: printable, one line (any whitespace becomes a space), collapsed."""
    t = unicodedata.normalize("NFKC", str(text or ""))
    t = "".join(" " if ch.isspace() else ch for ch in t)
    t = "".join(ch for ch in t if unicodedata.category(ch) not in BAD_CATEGORIES)
    return " ".join(t.split())[:limit]


class Executor:
    """The allowlist. Each action returns its output text; Rejected for a refused command."""

    def __init__(
        self,
        sample: Callable[[], dict[str, Any]],
        tail: Callable[[int], str] | None = None,
        panel: Panel | None = None,
        telemetry_: telemetry.Telemetry | None = None,
    ) -> None:
        self.sample = sample
        self.tail = tail
        self.panel = panel
        self.telemetry = telemetry_
        self.login = ""
        self.cmd_id = ""

    def run(self, cmd: dict[str, Any]) -> str:
        action, args = cmd["action"], cmd["args"]
        if action not in ACTIONS:
            raise Rejected(f"unknown action: {action}")
        self.login, self.cmd_id = str(cmd.get("login", ""))[:200], str(cmd.get("id", ""))
        try:
            return getattr(self, f"do_{action}")(args)
        except mc_panel.ActionError as e:
            raise Rejected(f"{action}: {e}") from None

    def need_panel(self, action: str) -> Panel:
        if self.panel is None:
            raise Rejected(f"{action}: not available in this agent")
        return self.panel

    # --- P8 actions, P10 countdowns ----------------------------------------------------------------------

    def do_status(self, args: dict[str, Any]) -> str:
        return json.dumps(self.sample(), separators=(",", ":"))

    # --- v1.3: Panel -> Git self-test, Mojang lookup from the box ------------------------------------------

    def do_selftest(self, args: dict[str, Any]) -> str:
        """The Panel -> Git self-test now (at most once per SELFTEST_MIN_GAP_S; a sooner request gets the last
        result). The full result goes into the inventory; the output is one line for the panel's toast."""
        panel = self.need_panel("selftest")
        if panel.selftest is None:
            raise Rejected("selftest: not available in this agent")
        r = panel.selftest("panel")
        panel.changed()
        if r.get("ok"):
            steps = ", ".join(s["step"] for s in r.get("steps") or [])
            return ("passed (an earlier run: one per minute)" if r.get("cached") else "passed") + f": {steps}"
        return f"failed at {r.get('error') or 'an unknown step'}"

    def do_lookup(self, args: dict[str, Any]) -> str:
        """A Java name -> UUID through Mojang, from this box (the panel's name check, v1.3); JSON output."""
        panel = self.need_panel("lookup")
        if panel.lookup is None:
            raise Rejected("lookup: not available in this agent")
        return json.dumps(panel.lookup(args.get("name")), separators=(",", ":"))

    def do_console(self, args: dict[str, Any]) -> str:
        line = check_console(args.get("command"))
        with mc_rcon.connect() as r:
            return r.command(line)[:4000]

    def _backup_first(self, job: mc_panel.Job) -> None:
        job.set_step("backing up first (mc-backup.service)")
        try:
            systemctl("start", BACKUP_UNIT, timeout=1800)
        except (OSError, subprocess.SubprocessError) as e:
            job.set_step(f"the backup failed ({type(e).__name__}); continuing")

    def _stop_now(self, reason: str) -> str:
        record = {
            "since": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "reason": reason,
            "by": f"panel ({self.login})" if self.login else "panel",
        }
        tmp = notify.ADMIN_STOPPED_FILE.with_name(notify.ADMIN_STOPPED_FILE.name + ".tmp")
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
        tmp.chmod(0o644)
        os.replace(tmp, notify.ADMIN_STOPPED_FILE)  # marker first: self-heal won't undo the stop
        systemctl("stop", UNIT)
        return "stopped (admin-stopped until start)"

    def _restart_now(self, job: mc_panel.Job | None = None, why: str = "panel restart") -> str:
        notify.maintenance(15, why)
        try:
            if job:
                job.set_step("restarting minecraft.service")
            systemctl("restart", UNIT)
            if job is None:
                return "restarted"
            job.set_step("waiting for the server to answer RCON")
            if not mc_panel.wait_for_rcon():
                raise mc_panel.ActionError("restarted, but RCON didn't answer within 5 minutes")
        finally:
            notify.maintenance(0, why)
        return "restarted; the server answers again"

    def do_stop(self, args: dict[str, Any]) -> str:
        reason = _one_line(args.get("reason") or "stopped from the panel", 200)
        countdown, backup = _countdown(args), _flag(args, "backup", False)
        if self.panel is None or (countdown == 0 and not backup):
            return self._stop_now(reason)

        def work(job: mc_panel.Job) -> str:
            if backup:
                self._backup_first(job)
            job.set_step("stopping")
            return self._stop_now(reason)

        job = self.panel.jobs.start(self.cmd_id, "stop", "Server stopping", self.login, countdown, work,
                                    skip_if_empty=_flag(args, "skip_if_empty", True))  # fmt: skip
        return f"stop scheduled (countdown {countdown} s, job {job.id})"

    def do_start(self, args: dict[str, Any]) -> str:
        if self.panel and self.panel.jobs.busy():
            raise Rejected("start: another action is running")
        if notify.fenced():
            raise Rejected("start: this box is fenced (mcctl migrate moved the world to another box)")
        notify.ADMIN_STOPPED_FILE.unlink(missing_ok=True)
        systemctl("start", UNIT)
        return "started"

    def do_restart(self, args: dict[str, Any]) -> str:
        if notify.admin_stopped() or notify.fenced():
            raise Rejected("restart: the server is admin-stopped (use start)")
        countdown, backup = _countdown(args), _flag(args, "backup", False)
        if self.panel is None or (countdown == 0 and not backup):
            return self._restart_now()

        def work(job: mc_panel.Job) -> str:
            if backup:
                self._backup_first(job)
            return self._restart_now(job)

        job = self.panel.jobs.start(self.cmd_id, "restart", "Restart", self.login, countdown, work,
                                    skip_if_empty=_flag(args, "skip_if_empty", True))  # fmt: skip
        return f"restart scheduled (countdown {countdown} s, job {job.id})"

    def do_backup(self, args: dict[str, Any]) -> str:
        systemctl("start", "--no-block", BACKUP_UNIT)
        if self.panel:  # P10: once it finished, push the inventory (its new snapshot list) for the panel
            changed = self.panel.changed

            def wait() -> None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    systemctl("start", BACKUP_UNIT, timeout=3600)  # a running oneshot: waits for it
                changed()

            threading.Thread(target=wait, name="backup-wait", daemon=True).start()
        return "backup started"

    def do_tail(self, args: dict[str, Any]) -> str:
        """P9 live console: stream minecraft.service's journal over the WebSocket for `seconds` (10-180)."""
        seconds = args.get("seconds", 120)
        if not isinstance(seconds, int) or isinstance(seconds, bool):
            raise Rejected("tail: seconds must be an integer")
        if self.tail is None:
            raise Rejected("tail: the live console needs the WebSocket")
        return self.tail(min(TAIL_MAX_S, max(10, seconds)))

    # --- P10 ----------------------------------------------------------------------------------------------

    def do_cancel(self, args: dict[str, Any]) -> str:
        try:
            return self.need_panel("cancel").jobs.cancel(self.login)
        except mc_panel.ActionError as e:
            raise Rejected(f"cancel: {e}") from None

    def _server_list(self, name: str) -> list[dict[str, Any]]:
        return read_json_list(((self.panel and self.panel.server_dir) or mc_rcon.SERVER_DIR) / name)

    def _whitelisted(self, name: str) -> dict[str, Any] | None:
        return next((e for e in self._server_list("whitelist.json")
                     if str(e.get("name", "")).lower() == name.lower()), None)  # fmt: skip

    def do_whitelist(self, args: dict[str, Any]) -> str:
        """Live via RCON (the server resolves the name with Mojang), confirmed in whitelist.json, then
        recorded for the config repo (pending.json -> pull request)."""
        panel = self.need_panel("whitelist")
        op, name = args.get("op"), args.get("name")
        if op not in ("add", "remove") or not isinstance(name, str) or not mc_panel.NAME.fullmatch(name):
            raise Rejected("whitelist: needs op add|remove and a Java name (3-16 letters, digits, _)")
        before = self._whitelisted(name)
        if op == "remove":
            if before is None:
                raise Rejected(f"whitelist: {name} isn't on the whitelist")
            if any(str(o.get("uuid")) == before.get("uuid") for o in self._server_list("ops.json")):
                raise Rejected(
                    f"whitelist: {name} is an operator; operators are managed in players.yml (Git)"
                )
        with mc_rcon.connect() as r:
            reply = mc_rcon.strip_formatting(r.command(f"whitelist {op} {name}"))[:300]
        entry = None
        for _ in range(10):  # the server writes whitelist.json right after the command
            entry = self._whitelisted(name)
            if (entry is not None) == (op == "add"):
                break
            time.sleep(0.5)
        if op == "add" and entry is None:
            raise Rejected(f"whitelist: the server didn't add {name} ({reply or 'no reply'})")
        if op == "remove" and entry is not None:
            raise Rejected(f"whitelist: the server didn't remove {name} ({reply or 'no reply'})")
        source = entry if op == "add" else before
        assert source is not None
        uuid, real = str(source.get("uuid", "")), str(source.get("name") or name)
        if not mc_panel.UUID.fullmatch(uuid):
            raise Rejected(f"whitelist: no UUID for {name} in whitelist.json")
        panel.pending.record(op, real, uuid, self.login, panel.clock())
        panel.rejected.drop(real)
        panel.changed()
        return f"{reply} (live now; recorded for players.yml in Git)"

    def do_rejected_dismiss(self, args: dict[str, Any]) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not mc_panel.NAME.fullmatch(name):
            raise Rejected("rejected_dismiss: needs a Java name")
        panel = self.need_panel("rejected_dismiss")
        found = panel.rejected.drop(name)
        panel.changed()
        return f"{name} dismissed" if found else f"{name} wasn't in the list"

    def do_moderate(self, args: dict[str, Any]) -> str:
        """kick/ban/pardon (players) and ban-ip/pardon-ip, checked against the console allowlist's shapes."""
        verb, target = args.get("verb"), args.get("target")
        if verb not in ("kick", "ban", "pardon", "ban-ip", "pardon-ip") or not isinstance(target, str):
            raise Rejected("moderate: verb kick|ban|pardon|ban-ip|pardon-ip and a target")
        reason = _one_line(args.get("reason"))
        line = check_console(
            f"{verb} {target}" + (f" {reason}" if reason and verb in ("kick", "ban", "ban-ip") else "")
        )
        with mc_rcon.connect() as r:
            reply = mc_rcon.strip_formatting(r.command(line))[:300]
        if self.panel:
            self.panel.changed()
        return reply or f"{verb} {target}"

    def do_plugin(self, args: dict[str, Any]) -> str:
        panel = self.need_panel("plugin")
        name, enabled = args.get("name"), args.get("enabled")
        lock = mc_panel.read_lock(panel.plugins_lock)
        if not isinstance(name, str) or name not in {p["name"] for p in lock}:
            raise Rejected("plugin: not a plugin in plugins.lock.yml")
        if not isinstance(enabled, bool):
            raise Rejected("plugin: enabled must be true or false")
        disabled = set(mc_panel.read_disabled(panel.plugins_disabled))
        if (name not in disabled) == enabled:
            raise Rejected(f"plugin: {name} is already {'enabled' if enabled else 'disabled'}")
        countdown = _countdown(args)
        want = disabled - {name} if enabled else disabled | {name}

        def work(job: mc_panel.Job) -> str:
            mc_panel.write_disabled(want, self.login, panel.plugins_disabled)
            job.set_step(f"moving the {name} jar (mc-plugins-apply.service)")
            systemctl("start", mc_panel.PLUGINS_APPLY_UNIT)
            panel.changed()
            if notify.admin_stopped():
                return (
                    f"{name} {'enabled' if enabled else 'disabled'}; the server is stopped (applies at start)"
                )
            self._restart_now(job, f"plugin {name}")
            panel.changed()
            return f"{name} {'enabled' if enabled else 'disabled'}; the server restarted"

        job = panel.jobs.start(self.cmd_id, "plugin", "Restart", self.login, countdown, work,
                               skip_if_empty=_flag(args, "skip_if_empty", True))  # fmt: skip
        verb = "enable" if enabled else "disable"
        return f"plugin {name} {verb} scheduled (countdown {countdown} s, job {job.id})"

    # --- P13 ----------------------------------------------------------------------------------------------

    def do_player_message(self, args: dict[str, Any]) -> str:
        """An in-game message the Worker's verdict engine decided on: private to one player (`to` = UUID) or
        one
        broadcast (`to` = "*"). The agent re-checks the rate limits with the live state (online, joined when,
        tips on) and queues it for the mc-telemetry plugin, which shows it (clickable /lag)."""
        t = self.telemetry
        if t is None:
            raise Rejected("player_message: experience telemetry is off on this server")
        to, kind = args.get("to"), args.get("kind")
        msg_id = args.get("id")
        if not isinstance(to, str) or not (to == "*" or telemetry.UUID.match(to)):
            raise Rejected("player_message: to must be a player UUID or *")
        if kind not in ("lag", "server", "join", "tip"):
            raise Rejected("player_message: kind must be lag, server, join or tip")
        if not isinstance(msg_id, str) or not re.fullmatch(r"[a-z]{3,6}-[0-9a-f]{8,32}", msg_id):
            raise Rejected("player_message: bad id")
        text, hover = _one_line(args.get("text"), 256), _one_line(args.get("hover"), 256)
        if not text:
            raise Rejected("player_message: empty text")
        now = time.time()
        online = t.online()  # lock-free: never waits for the telemetry thread's file reads or HTTP
        why = t.messages.check(to, kind, now, online)
        if why:
            raise Rejected(f"player_message: not sent ({why})")
        t.messages.add(msg_id, to, kind, text, hover, now)
        who = "everyone" if to == "*" else online[to]["n"]
        return f"queued for {who}"

    def do_restore(self, args: dict[str, Any]) -> str:
        """mc-restore@<id>.service does the work as the ops user (safety snapshot, stop, fetch, apply as
        minecraft, start); this follows its journal and reports the steps."""
        panel = self.need_panel("restore")
        snap = args.get("snapshot")
        if not isinstance(snap, str) or not mc_panel.SNAPSHOT_ID.fullmatch(snap):
            raise Rejected("restore: snapshot must be a restic snapshot id (8-64 hex)")
        known = mc_panel.read_snapshots(panel.snapshots)
        match = next((s for s in known if s["id"].startswith(snap) or s.get("short_id") == snap), None)
        if match is None:
            raise Rejected(f"restore: {snap} isn't in the snapshot list (refresh it with a backup)")
        tags = [str(t) for t in match.get("tags") or []]
        archived = worlds.tag_value(tags, "world:") if worlds.ARCHIVE_TAG in tags else None
        if archived and archived != self._worlds(panel)[2]:
            raise Rejected(
                f"restore: {snap} is the archive of the world {archived}; switch in the Worlds tab"
            )
        countdown = _countdown(args)
        unit = f"mc-restore@{snap}.service"

        def work(job: mc_panel.Job) -> str:
            since = time.strftime("%Y-%m-%d %H:%M:%S")
            done = threading.Event()

            def follow() -> None:
                last = ""
                while not done.wait(3):
                    steps = mc_panel.unit_steps(unit, since)
                    if steps and steps[-1] != last:
                        last = steps[-1]
                        job.set_step(last)

            job.set_step(f"starting {unit}")
            watcher = threading.Thread(target=follow, name="restore-follow", daemon=True)
            watcher.start()
            try:
                systemctl("start", unit, timeout=RESTORE_TIMEOUT_S)
            except subprocess.CalledProcessError:
                steps = mc_panel.unit_steps(unit, since)
                raise mc_panel.ActionError(
                    f"restore failed ({steps[-1] if steps else 'see journalctl -u ' + unit})"
                ) from None
            finally:
                done.set()
            panel.changed()
            steps = mc_panel.unit_steps(unit, since)
            return steps[-1] if steps else f"restored {snap}"

        job = panel.jobs.start(self.cmd_id, "restore", "Restore", self.login, countdown, work,
                               skip_if_empty=_flag(args, "skip_if_empty", True))  # fmt: skip
        return f"restore of {snap} scheduled (countdown {countdown} s, job {job.id})"

    # --- v1.2: the world library (panel only, confirmation host; scripts/worlds.py does the work) -----------

    def _worlds(self, panel: Panel) -> tuple[dict[str, Any], dict[str, Any], str]:
        """(worlds.json state, the library (the pending change, else the deploy's file), the active name)."""
        state = worlds.load_state(panel.worlds_state)
        lib = worlds.read_library(panel.worlds_library)
        pend = panel.pending.load().get("world") or {}
        if pend.get("library") is not None:
            entries = []
            for raw in pend["library"]:
                with contextlib.suppress(worlds.WorldError):
                    entries.append(worlds.check_entry(raw))
            lib = {"active": pend.get("active") or lib["active"], "library": entries}
        active = state["active"] or lib["active"] or "world"
        return state, lib, active

    def _world_name(self, args: dict[str, Any], action: str) -> str:
        name = args.get("name")
        if not isinstance(name, str) or not worlds.WORLD_NAME.fullmatch(name):
            raise Rejected(f"{action}: a world name is 1-32 lowercase letters, digits, _ or -")
        return name

    def _world_job(
        self,
        action: str,
        label: str,
        name: str,
        op: str,
        entry: dict[str, Any] | None,
        countdown: int,
        args: dict[str, Any],
    ) -> str:
        panel = self.need_panel(action)
        _, lib, active = self._worlds(panel)
        login = self.login

        def work(job: mc_panel.Job) -> str:
            request = {
                "op": op,
                "name": name,
                "entry": entry,
                "id": job.id,
                "login": login,
                "ts": time.time(),
            }
            path = panel.world_request or worlds.REQUEST_FILE
            mc_panel.atomic_write(path, json.dumps(request, sort_keys=True) + "\n", 0o640)
            unit = worlds.PANEL_UNIT
            since = time.strftime("%Y-%m-%d %H:%M:%S")
            done = threading.Event()

            def follow() -> None:
                last = ""
                while not done.wait(3):
                    steps = mc_panel.unit_steps(unit, since)
                    if steps and steps[-1] != last:
                        last = steps[-1]
                        job.set_step(last)

            job.set_step(f"starting {unit}")
            threading.Thread(target=follow, name="world-follow", daemon=True).start()
            failed = ""
            try:
                systemctl("start", unit, timeout=RESTORE_TIMEOUT_S)
            except subprocess.CalledProcessError:
                steps = mc_panel.unit_steps(unit, since)
                failed = steps[-1] if steps else f"see journalctl -u {unit}"
            finally:
                done.set()
            self._record_world(panel, op, name, entry, active, lib, login)
            panel.changed()
            if failed:
                raise mc_panel.ActionError(f"{action} failed ({failed})")
            steps = mc_panel.unit_steps(unit, since)
            return steps[-1] if steps else f"{action} {name}: done"

        job = panel.jobs.start(self.cmd_id, action, label, login, countdown, work,
                               skip_if_empty=_flag(args, "skip_if_empty", True))  # fmt: skip
        return f"{action.replace('_', ' ')} {name} scheduled (countdown {countdown} s, job {job.id})"

    def _record_world(
        self,
        panel: Panel,
        op: str,
        name: str,
        entry: dict[str, Any] | None,
        old_active: str,
        lib: dict[str, Any],
        login: str,
    ) -> None:
        """What the server now runs -> pending.json (-> a pull request for server.yml; deploys honour it).
        Decided by worlds.json after the unit: a switch that happened but whose start failed still counts."""
        state = worlds.load_state(panel.worlds_state)
        library = {e["name"]: e for e in lib["library"]}
        if op == "delete":
            if any(a["name"] == name for a in state["archives"]):
                return  # not deleted
            library.pop(name, None)
            what, active = f"delete {name}", state["active"] or old_active
        else:
            if state["active"] != name:
                return  # the switch didn't happen
            library.setdefault(
                old_active,
                {
                    "name": old_active,
                    "seed": "",
                    "type": "default",
                    "preset": None,
                    "border_radius": None,
                    "pregen": None,
                },
            )
            assert entry is not None
            library[name] = library.get(name, entry) if op == "switch" else entry
            what, active = f"{'create' if op == 'create' else 'switch to'} {name}", name
        panel.pending.record_world(
            active, sorted(library.values(), key=lambda e: e["name"]), what, login, panel.clock()
        )

    def do_world_create(self, args: dict[str, Any]) -> str:
        """A new world from a seed, type and preset; the active one is archived first (worlds.py)."""
        panel = self.need_panel("world_create")
        name = self._world_name(args, "world_create")
        state, _, active = self._worlds(panel)
        if name == active or any(a["name"] == name for a in state["archives"]):
            raise Rejected(f"world_create: {name} already exists")
        if any(d["name"] == name for d in state["deleted"]):
            raise Rejected(
                f"world_create: {name} was deleted recently (its archive isn't forgotten yet): pick another"
            )
        border = args.get("border_radius")
        try:
            entry = worlds.check_entry(
                {
                    "name": name,
                    "seed": args.get("seed", ""),
                    "type": args.get("type", "default"),
                    "preset": args.get("preset", "survival_smp"),
                    "border_radius": border,
                    "pregen": args.get("pregen"),
                }
            )
        except worlds.WorldError as e:
            raise Rejected(f"world_create: {e}") from None
        return self._world_job("world_create", "World switch", name, "create", entry, _countdown(args), args)

    def do_world_switch(self, args: dict[str, Any]) -> str:
        """Switch to an archived world (its library settings); the active one is archived first."""
        panel = self.need_panel("world_switch")
        name = self._world_name(args, "world_switch")
        state, lib, active = self._worlds(panel)
        if name == active:
            raise Rejected(f"world_switch: {name} is already the active world")
        archive = next((a for a in state["archives"] if a["name"] == name), None)
        if archive is None:
            raise Rejected(f"world_switch: {name} isn't an archived world (create it instead)")
        if why := worlds.version_check(archive.get("data_version"), state["data_version"]):
            raise Rejected(f"world_switch: {why}")
        entry = next((e for e in lib["library"] if e["name"] == name), None) or worlds.check_entry(
            {"name": name}
        )
        return self._world_job("world_switch", "World switch", name, "switch", entry, _countdown(args), args)

    def do_world_delete(self, args: dict[str, Any]) -> str:
        """Forget an archived world (never the active one); the server isn't touched."""
        panel = self.need_panel("world_delete")
        name = self._world_name(args, "world_delete")
        state, _, active = self._worlds(panel)
        if name == active:
            raise Rejected(f"world_delete: {name} is the active world; switch to another one first")
        if not any(a["name"] == name for a in state["archives"]):
            raise Rejected(f"world_delete: {name} isn't an archived world")
        return self._world_job(
            "world_delete", "World delete", name, "delete", None, 0, {"skip_if_empty": True}
        )


def worlds_inventory(panel: Panel, executor: Executor) -> dict[str, Any]:
    """The panel's Worlds tab: active world, archives (newest snapshot each), library, the pending change."""
    state, lib, active = executor._worlds(panel)
    pend = panel.pending.load().get("world") or {}
    archives = [
        {k: a.get(k) for k in ("name", "short_id", "time", "size", "data_version")}
        | {"count": len(a.get("snapshots") or [])}
        for a in state["archives"]
        if a.get("name") != active
    ][:100]
    total = sum(a["size"] or 0 for a in archives if isinstance(a.get("size"), int))
    open_pr = pend.get("open_pr") or {}
    return {
        "active": active,
        "since": state["since"],
        "data_version": state["data_version"],
        "archives": archives,
        "library": lib["library"][:100],
        "deleted": [d["name"] for d in state["deleted"]][:100],
        "archive_bytes": total,
        "storage_note": total > worlds.STORAGE_NOTE_BYTES,
        "pending": {"what": str(pend.get("what") or ""), "error": str(pend.get("error") or ""),
                    "pr": open_pr.get("number"), "url": str(open_pr.get("url") or "")} if pend else None,
    }  # fmt: skip


def handle_command(message: object, s: Settings, nonces: Nonces, executor: Executor) -> dict[str, Any]:
    """Verify, check, run, audit. Always returns a result object (never raises)."""
    cmd_id, login, action = "", "", ""
    try:
        cmd = verify(message, s.hmac_key, nonces)
        cmd_id, login, action = cmd["id"], cmd["login"][:200], cmd["action"]
        output = executor.run(cmd)
        result = {"id": cmd_id, "ok": True, "output": output}
    except Rejected as e:
        result = {"id": cmd_id, "ok": False, "error": str(e)}
    except (OSError, subprocess.SubprocessError, mc_rcon.RconError) as e:
        result = {"id": cmd_id, "ok": False, "error": f"{action} failed: {type(e).__name__}"}
    args = ""
    with contextlib.suppress(Exception):
        args = json.dumps(json.loads(message["payload"]).get("args", {}), sort_keys=True)[:500]  # type: ignore[index]
    audit(
        f"agent action={action or '?'} ok={result['ok']} login={login or '?'}",
        action=action,
        login=login,
        args=args,
        result="ok" if result["ok"] else str(result.get("error", "")),
    )
    result["finished_at"] = int(time.time() * 1000)
    return result


# --- WebSocket (RFC 6455, client side, the subset the Durable Object uses) ----------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA
WS_MAX_MESSAGE = 1 << 20
WS_RECV_TIMEOUT_S = 15.0
WS_PING_S = 45.0  # idle this long: ping
WS_DEAD_S = 120.0  # nothing received this long (pings unanswered): reconnect


class WebSocketError(Exception):
    pass


def ws_accept(key: str) -> str:
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # noqa: S324 - RFC 6455


def encode_frame(opcode: int, payload: bytes, mask: bytes | None = None) -> bytes:
    """One final frame; clients must mask (RFC 6455 5.3)."""
    mask = secrets.token_bytes(4) if mask is None else mask
    head = bytes([0x80 | opcode])
    n = len(payload)
    if n < 126:
        head += bytes([0x80 | n])
    elif n < 1 << 16:
        head += bytes([0x80 | 126]) + struct.pack(">H", n)
    else:
        head += bytes([0x80 | 127]) + struct.pack(">Q", n)
    masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return head + mask + masked


class WebSocket:
    def __init__(self, sock: socket.socket) -> None:
        self.sock = sock
        self.buf = b""
        self.parts: list[bytes] | None = None  # a fragmented message being received
        self.last_rx = time.monotonic()
        self.send_lock = threading.Lock()  # the session thread and the console tail both send

    @classmethod
    def connect(cls, url: str, headers: dict[str, str], timeout: float = 20.0) -> WebSocket:
        parts = urllib.parse.urlsplit(url)
        secure = parts.scheme in ("https", "wss")
        host = parts.hostname or ""
        port = parts.port or (443 if secure else 80)
        raw = socket.create_connection((host, port), timeout=timeout)
        sock: socket.socket = raw
        if secure:
            tls = ssl.create_default_context()
            tls.minimum_version = ssl.TLSVersion.TLSv1_2  # the default since 3.10, made explicit
            sock = tls.wrap_socket(raw, server_hostname=host)
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        path = parts.path or "/"
        if parts.query:
            path += "?" + parts.query
        lines = [
            f"GET {path} HTTP/1.1",
            f"Host: {parts.netloc}",
            "Upgrade: websocket",
            "Connection: Upgrade",
            f"Sec-WebSocket-Key: {key}",
            "Sec-WebSocket-Version: 13",
            *(f"{k}: {v}" for k, v in headers.items()),
        ]
        sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
        ws = cls(sock)
        head = ws._read_until(b"\r\n\r\n", 16384).decode("latin-1")
        status, *fields = head.split("\r\n")
        if not re.match(r"HTTP/1\.1 101\b", status):
            sock.close()
            raise WebSocketError(f"upgrade refused: {status[:40]}")
        got = {k.strip().lower(): v.strip() for k, _, v in (f.partition(":") for f in fields)}
        if got.get("sec-websocket-accept") != ws_accept(key):
            sock.close()
            raise WebSocketError("bad Sec-WebSocket-Accept")
        return ws

    def _read_until(self, marker: bytes, limit: int) -> bytes:
        while marker not in self.buf:
            if len(self.buf) > limit:
                raise WebSocketError("response header too large")
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("connection closed during the handshake")
            self.buf += chunk
        head, _, self.buf = self.buf.partition(marker)
        return head

    def _parse(self) -> tuple[bool, int, bytes] | None:
        """One complete frame from the buffer (consumed), or None if more bytes are needed. Nothing is
        consumed before the whole frame is there, so a receive timeout never loses framing."""
        buf = self.buf
        if len(buf) < 2:
            return None
        b0, b1 = buf[0], buf[1]
        if b0 & 0x70:
            raise WebSocketError("reserved bits set (no extension was negotiated)")
        if b1 & 0x80:
            raise WebSocketError("masked frame from the server (RFC 6455 5.1)")
        op, n, pos = b0 & 0x0F, b1 & 0x7F, 2
        if n == 126:
            if len(buf) < 4:
                return None
            (n,) = struct.unpack(">H", buf[2:4])
            pos = 4
        elif n == 127:
            if len(buf) < 10:
                return None
            (n,) = struct.unpack(">Q", buf[2:10])
            pos = 10
        if n > WS_MAX_MESSAGE:
            raise WebSocketError("frame too large")
        if op >= 0x8 and (n > 125 or not b0 & 0x80):
            raise WebSocketError("bad control frame")
        if len(buf) < pos + n:
            return None
        self.buf = buf[pos + n :]
        return bool(b0 & 0x80), op, buf[pos : pos + n]

    def _frame(self) -> tuple[bool, int, bytes]:
        while True:
            frame = self._parse()
            if frame is not None:
                self.last_rx = time.monotonic()
                return frame
            chunk = self.sock.recv(65536)  # TimeoutError leaves self.buf intact
            if not chunk:
                raise WebSocketError("connection closed")
            self.buf += chunk

    def send_text(self, text: str) -> None:
        with self.send_lock:
            self.sock.sendall(encode_frame(OP_TEXT, text.encode()))

    def _send(self, opcode: int, payload: bytes) -> None:
        with self.send_lock:
            self.sock.sendall(encode_frame(opcode, payload))

    def recv(self) -> str | None:
        """The next text message; None when the server closed. Pings are answered on the way; a fragmented
        message survives receive timeouts (the parts are kept until its final frame)."""
        while True:
            fin, op, data = self._frame()
            if op == OP_PING:
                self._send(OP_PONG, data)
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                with contextlib.suppress(OSError):
                    self._send(OP_CLOSE, data[:2])
                return None
            if op in (OP_TEXT, OP_BIN):
                if self.parts is not None:
                    raise WebSocketError("new message inside a fragmented one")
                self.parts = []
            elif op == OP_CONT:
                if self.parts is None:
                    raise WebSocketError("continuation without a message")
            else:
                raise WebSocketError(f"unknown opcode {op}")
            self.parts.append(data)
            if sum(map(len, self.parts)) > WS_MAX_MESSAGE:
                raise WebSocketError("message too large")
            if fin:
                text = b"".join(self.parts).decode("utf-8", "replace")
                self.parts = None
                return text

    def ping(self) -> None:
        self._send(OP_PING, b"")

    def close(self) -> None:
        with contextlib.suppress(OSError):
            self._send(OP_CLOSE, struct.pack(">H", 1000))
        self.sock.close()


# --- loops --------------------------------------------------------------------------------------------------


# --- v1.1: /modpack's one-time codes ------------------------------------------------------------------------

CLIENT_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
CLIENT_NAME = re.compile(r"^[A-Za-z0-9_]{1,16}$")
CLIENT_NONCE = re.compile(r"^[0-9a-f]{32}$")
CLIENT_CODE = re.compile(r"^[0-9]{6}$")
CLIENT_POLL_S = 1.0
CLIENT_KEEP_S = 120  # an answer stays in the code file this long (the plugin waits at most 30 s)
CLIENT_MAX_REQUESTS = 50  # request files read per scan (a flood never blocks the loop)
CLIENT_PER_HOUR = 20  # Worker requests per hour from this box (the Worker limits per player too)
CLIENT_REASONS = {
    429: "you asked a few times already, try again in a while",
    404: "the client page isn't set up on this server yet",
}
# v1.2: the operator's side of a refused code request, for the journal (the players' side: the code file).
# The v1.1 rollout: client.enabled deployed without `mcctl apply` left the Worker without CLIENT/CLIENT_KEY.
CLIENT_FIXES = {
    404: "the Worker has no client set up (client.enabled needs `mcctl apply`; `mcctl doctor` checks it)",
    401: "the Worker refused the agent token (a deploy gives the server the current one)",
    429: "the Worker's rate limit for codes",
}


def parse_client_request(text: str) -> tuple[str, str, str] | None:
    lines = text.splitlines()
    if len(lines) != 2 or lines[0] != "mc-client-request 1":
        return None
    f = lines[1].split("\t")
    if (
        len(f) != 4
        or not CLIENT_UUID.match(f[0])
        or not CLIENT_NAME.match(f[1])
        or not CLIENT_NONCE.match(f[2])
    ):
        return None
    return f[0], f[1], f[2]


@dataclass
class ClientCodes:
    """Request files -> Worker -> the code file. One thread; state in memory only (a restart forgets which
    requests were answered: the plugin deletes answered requests, and an unanswered one is answered again)."""

    s: Settings
    post: Callable[[str, dict[str, Any]], Any]
    clock: Callable[[], float] = time.time
    answered: dict[tuple[str, str], float] = field(default_factory=dict)  # (uuid, nonce) -> when
    lines: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)  # -> (line, when)
    sent: list[float] = field(default_factory=list)

    def scan(self) -> int:
        """One pass: returns how many requests were answered."""
        now = self.clock()
        self.answered = {k: v for k, v in self.answered.items() if now - v < 600}
        self.lines = {k: v for k, v in self.lines.items() if now - v[1] < CLIENT_KEEP_S}
        self.sent = [t for t in self.sent if now - t < 3600]
        try:
            names = sorted(os.listdir(self.s.client_request_dir))[:CLIENT_MAX_REQUESTS]
        except OSError:
            names = []
        done = 0
        for name in names:
            if not name.endswith(".req") or name.startswith("."):
                continue
            path = self.s.client_request_dir / name
            try:
                if not path.is_file() or path.stat().st_size > 4096:
                    continue
                req = parse_client_request(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError):
                continue
            if req is None or req[0] + ".req" != name or (req[0], req[2]) in self.answered:
                continue
            uuid, player, nonce = req
            self.answered[(uuid, nonce)] = now
            self.lines[(uuid, nonce)] = (self.answer(uuid, player, nonce, now), now)
            done += 1
        if done or any(now - when >= CLIENT_KEEP_S - CLIENT_POLL_S for _, when in self.lines.values()):
            self.write()
        return done

    def answer(self, uuid: str, player: str, nonce: str, now: float) -> str:
        if len(self.sent) >= CLIENT_PER_HOUR:
            return f"E\t{uuid}\t{nonce}\t{CLIENT_REASONS[429]}"
        self.sent.append(now)
        try:
            reply = self.post("/agent/client-code", {"uuid": uuid, "name": player})
        except IngestError as e:
            reason = CLIENT_REASONS.get(
                e.status or 0, "the server couldn't reach its web side, try again soon"
            )
            fix = CLIENT_FIXES.get(e.status or 0, "the Worker is unreachable or failing")
            # one plain line per distinct cause (IngestError never carries the token, a body or the code)
            text = f"/modpack code request failed ({e}): {fix}; players see: {reason}"
            WARN.fail("client", text, relog=True)
            return f"E\t{uuid}\t{nonce}\t{reason}"
        code, expires = str((reply or {}).get("code", "")), (reply or {}).get("expires_ms")
        if not CLIENT_CODE.match(code) or not isinstance(expires, int):
            return f"E\t{uuid}\t{nonce}\tthe web side gave an odd answer, try again soon"
        WARN.ok("client")
        audit("client code issued", player=player, uuid=uuid)  # never the code
        return f"C\t{uuid}\t{nonce}\t{code}\t{expires}"

    def write(self) -> None:
        text = "mc-client-codes 1\n" + "".join(line + "\n" for line, _ in self.lines.values())
        path = self.s.client_code_file
        tmp = path.with_name(f".{path.name}.tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.chmod(tmp, 0o640)  # the directory is setgid minecraft: mc-brand may read it
            os.replace(tmp, path)
        except OSError as e:
            WARN.fail("client-file", f"can't write {path.name}: {type(e).__name__}")

    def run(self, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                self.scan()
            except Exception as e:  # the hand-off must outlive any single bad pass
                WARN.fail("client", f"{type(e).__name__}: {e}")
            stop.wait(CLIENT_POLL_S)


def make_telemetry(
    s: Settings,
    stats: Callable[[], dict[str, dict[str, int | None]]] = telemetry.unit_stats,
    cls: type[telemetry.Telemetry] = telemetry.Telemetry,
) -> telemetry.Telemetry:
    """P13: snapshots -> minute rows (live to the Worker), segments (R2 + the box's copy), backfill. The
    agent runs it in-process (as smallworld-agent does); `mc_agent.py telemetry` on its own."""
    e = os.environ
    cfg = telemetry.Config.from_env(dict(e), mc_rcon.SERVER_DIR, e.get("LEVEL_NAME") or "world")
    msgs = telemetry.Messages(
        cfg.verdict_dir,
        per_player_s=max(60, int(e.get("MESSAGE_PER_PLAYER_S") or 1800)),
        broadcast_s=max(60, int(e.get("MESSAGE_BROADCAST_S") or 1800)),
        per_hour=max(1, int(e.get("MESSAGE_PER_HOUR") or 20)),
    )
    gc_counters = Counters()

    def extra() -> dict[str, Any]:
        lines = health.read_gc_log(mc_rcon.SERVER_DIR)
        heap = last_heap(lines)
        gc = health.parse_gc(lines, gc_counters.gc_uptime, 100.0)
        gc_counters.gc_uptime = gc.last_uptime
        return {
            "gc_heap_after_mb": heap[0] if heap else None,
            "gc_max_pause_ms": round(max([gc.max_pause_ms, *gc.stalls]), 1),
        }

    def put(name: str, data: bytes) -> Any:  # noqa: ANN401
        return request(
            s, "PUT", f"/ingest/segment?name={urllib.parse.quote(name)}", data, "application/gzip", 60.0
        )

    return cls(
        cfg, s.state_dir, post=lambda path, body: post_json(s, path, body), put=put,
        running=running_units, stats=stats, extra=extra, messages=msgs,
    )  # fmt: skip


class Agent:
    def __init__(self, s: Settings) -> None:
        self.s = s
        self.stop = threading.Event()
        self.counters = Counters()
        self.spool = Spool(s.state_dir / "spool.jsonl")
        self.nonces = Nonces(s.state_dir / "nonces.json")
        self.tail = ConsoleTail(self)
        self.inventory_due = threading.Event()  # P10: a state change pushes the inventory now
        self.github = mc_panel.github_from_env(fetch_token=self.github_token)
        # v1.3: the code this process runs (read once at start; a deploy that installs new code restarts the
        # agent, so a difference from the file means it didn't), the self-test's state, the Mojang lookup
        self.product_started = read_product()
        self.selftest_state = mc_panel.SelfTestState(s.state_dir / "selftest.json")
        self.selftest_lock = threading.Lock()
        self.panel = Panel(
            jobs=mc_panel.Jobs(self.report_job, lock_path=mc_panel.JOB_LOCK_FILE),
            pending=mc_panel.Pending(s.state_dir / "pending.json"),
            rejected=mc_panel.RejectedLogins(s.state_dir / "rejected.json"),
            changed=self.inventory_due.set,
            selftest=self.run_selftest,
            lookup=Lookup(lookup_urls()),
        )
        self.telemetry = self.make_telemetry() if s.experience else None
        self.overhead_seen: Callable[[list[dict[str, Any]]], None] | None = (
            self.telemetry.overhead.observe_journal if self.telemetry else None
        )
        self.executor = Executor(lambda: collect(Counters()), self.tail.request, self.panel, self.telemetry)
        self.ws_connected = False
        self.ws_established = False
        self.ws: WebSocket | None = None

    def make_telemetry(self) -> telemetry.Telemetry:
        """P13: snapshots -> minute rows (live to the Worker), segments (R2 + the box's copy), backfill."""
        return make_telemetry(self.s)

    def github_token(self) -> Any:  # noqa: ANN401 - the Worker's JSON {token, expires_at}
        """A short-lived installation token of the panel's GitHub App, minted by the Worker (which alone holds
        the App's private key) for this agent's bearer token. Kept in memory only (mc_panel.TokenCache)."""
        return post_json(self.s, "/agent/github-token", {"agent": VERSION})

    def run_selftest(self, trigger: str = "daily") -> dict[str, Any]:
        """v1.3: the Panel -> Git self-test (mc_panel.selftest), recorded in selftest.json; two failures in a
        row alert on Discord once (and once when it passes again). On demand at most once per
        SELFTEST_MIN_GAP_S."""
        with self.selftest_lock:
            # on demand at most once a minute, counted from the last on-demand run (a daily run in between
            # doesn't lift the limit)
            prev = self.selftest_state.load()["last_panel"]
            if trigger == "panel" and prev:
                ts = prev.get("ts")
                if isinstance(ts, int) and time.time() * 1000 - ts < SELFTEST_MIN_GAP_S * 1000:
                    return prev | {"cached": True}
            gh = self.github
            result = mc_panel.selftest(self.github_token if gh else None, gh, time.time())
            result["trigger"] = trigger
            self.selftest_state.record(result, alert=notify.discord)
            audit(
                f"panel selftest ok={result['ok']} trigger={trigger}",
                action="selftest",
                result="ok" if result["ok"] else str(result["error"])[:300],
            )
        return result

    def selftest_loop(self) -> None:
        """Daily, the first run SELFTEST_FIRST_S after the agent starts (a deploy restarts it: a broken
        release shows within minutes); only when the panel has a config repo (CONFIG_REPO)."""
        wait = SELFTEST_FIRST_S
        while not self.stop.wait(wait):
            wait = SELFTEST_S
            if self.github is None:
                continue
            try:
                self.run_selftest("daily")
                self.inventory_due.set()
            except Exception as e:  # the loop must outlive one bad round
                print(f"selftest failed: {type(e).__name__}: {e}", file=sys.stderr)

    def product_state(self) -> dict[str, Any] | None:
        """v1.3: the installed product version (the deploy's file) and the one this process started with."""
        installed = read_product()
        if installed is None:
            return None
        started = self.product_started or {}
        return installed | {"running": str(started.get("version") or ""),
                            "running_commit": str(started.get("commit") or "")}  # fmt: skip

    def report_job(self, job: dict[str, Any]) -> None:
        """A job's state for the panel (countdown, steps, result); failures are only logged (the inventory
        carries the job too)."""
        try:
            post_json(self.s, "/ingest/job", {"agent": VERSION, "job": job})
        except IngestError as e:
            WARN.fail("job", str(e))
        else:
            WARN.ok("job")
        self.inventory_due.set()

    def panel_state(self) -> dict[str, Any]:
        """The P10 part of the inventory: pending whitelist changes and their PR, rejected logins, the plugin
        lock and what is disabled, restic's snapshot list, the current job."""
        p = self.panel
        pend = p.pending.load()
        job = p.jobs.current
        return {
            "pending": [
                {k: e.get(k) for k in ("uuid", "name", "op", "ts", "login", "pr")} for e in pend["entries"]
            ],
            "whitelist_sync": {
                "configured": self.github is not None,
                "pr": pend.get("pr") or None,
                "error": pend.get("error", ""),
                "expired": pend.get("expired", [])[-10:],
            },
            "rejected": p.rejected.entries(),
            "plugins_lock": mc_panel.read_lock(p.plugins_lock),
            "plugins_disabled": mc_panel.read_disabled(p.plugins_disabled),
            "snapshots": [
                s | {"tags": ",".join(s.get("tags") or [])} for s in mc_panel.read_snapshots(p.snapshots)
            ],
            "job": job.public() if job else None,
            "worlds": worlds_inventory(p, self.executor),
            "product": self.product_state(),
            "selftest": self.selftest_state.public(),
        }

    def panel_loop(self) -> None:
        """The whitelist -> Git flow (pending.json -> PR, merged -> done, 24 h -> expired)."""
        s = self.s
        while not self.stop.wait(s.panel_sync_s):
            try:
                if mc_panel.sync_pending(self.panel.pending, self.github, time.time(), log=audit,
                                         batch_s=s.panel_batch_s, pr_check_s=s.panel_pr_check_s):  # fmt: skip
                    self.inventory_due.set()
            except Exception as e:  # the loop must outlive one bad round
                print(f"panel sync failed: {type(e).__name__}: {e}", file=sys.stderr)

    def observe_logs(self, entries: list[dict[str, Any]]) -> None:
        if self.panel.rejected.feed((e["ts"], e["msg"]) for e in entries):
            self.inventory_due.set()

    def metrics_loop(self) -> None:
        while not self.stop.is_set():
            started = time.monotonic()
            try:
                sample = collect(self.counters)
                sample["ws"] = self.ws_connected
                push_metrics(self.s, self.spool, sample)
            except Exception as e:  # one bad sample must not end the loop
                print(f"metrics round failed: {type(e).__name__}: {e}", file=sys.stderr)
            self.stop.wait(max(1.0, self.s.metrics_interval_s - (time.monotonic() - started)))

    def inventory_loop(self) -> None:
        while not self.stop.is_set():
            wait = INVENTORY_S
            self.inventory_due.clear()
            try:
                push_inventory(self.s, extra=self.panel_state())
                WARN.ok("inventory")
            except IngestError as e:
                WARN.fail("inventory", str(e))
                wait = 60  # the Worker was unreachable: try again soon, not in 10 minutes
            except Exception as e:
                print(f"inventory round failed: {type(e).__name__}: {e}", file=sys.stderr)
            self.inventory_due.wait(wait)
            self.stop.wait(INVENTORY_MIN_GAP_S)

    def logs_loop(self) -> None:
        path = self.s.state_dir / "logs.json"
        while not self.stop.wait(self.s.log_batch_s):
            try:
                for _ in range(LOG_CATCHUP_BATCHES):  # a backlog drains batch by batch, cursor per batch
                    _, more = ship_logs(
                        self.s, path, observe=self.observe_logs, observe_all=self.overhead_seen
                    )
                    if not more or self.stop.is_set():
                        break
                WARN.ok("logs")
            except IngestError as e:
                WARN.fail("logs", f"deferred, the journal keeps them: {e}")
            except Exception as e:
                print(f"log round failed: {type(e).__name__}: {e}", file=sys.stderr)

    def answer(self, message: object) -> dict[str, Any]:
        return handle_command(message, self.s, self.nonces, self.executor)

    def ws_session(self) -> None:
        """One WebSocket session until the server closes it or it goes quiet. Liveness (P8 review): every
        WS_PING_S without traffic the agent sends a text "ping" (the Durable Object's auto-response answers
        "pong" without waking it) and a protocol ping; nothing received for WS_DEAD_S means a dead path (a NAT
        dropped it, the DO went away), so the session ends and the control loop reconnects."""
        url = self.s.ingest_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1) + "/agent/ws"
        ws = WebSocket.connect(url, {"Authorization": f"Bearer {self.s.token}", "User-Agent": USER_AGENT})
        ws.sock.settimeout(WS_RECV_TIMEOUT_S)
        self.ws = ws
        self.ws_connected = self.ws_established = True
        WARN.ok("websocket")
        last_ping = time.monotonic()
        try:
            ws.send_text(json.dumps({"type": "hello", "agent": VERSION}))
            while not self.stop.is_set():
                try:
                    text = ws.recv()
                except TimeoutError:
                    now = time.monotonic()
                    if now - ws.last_rx > WS_DEAD_S:
                        raise WebSocketError(f"nothing received for {WS_DEAD_S} s") from None
                    if now - max(ws.last_rx, last_ping) >= WS_PING_S:
                        ws.send_text("ping")
                        ws.ping()
                        last_ping = now
                    continue
                if text is None:
                    return
                if text == "pong":
                    continue
                with contextlib.suppress(ValueError):
                    msg = json.loads(text)
                    if isinstance(msg, dict) and msg.get("type") == "command":
                        ws.send_text(json.dumps({"type": "result", **self.answer(msg)}))
        finally:
            self.ws_connected = False
            self.ws = None
            ws.close()

    def poll_once(self) -> int:
        reply = request(self.s, "GET", "/agent/commands")
        commands = reply.get("commands", []) if isinstance(reply, dict) else []
        results = [self.answer(c) for c in commands[:20]]
        if results:
            post_json(self.s, "/agent/results", {"results": results})
        return len(results)

    def control_loop(self) -> None:
        """WebSocket while it holds; otherwise poll every POLL_S and try the socket again every 5 minutes."""
        next_ws = 0.0
        while not self.stop.is_set():
            try:
                if time.monotonic() >= next_ws:
                    self.ws_established = False
                    try:
                        self.ws_session()
                        next_ws = time.monotonic() + 5  # a clean close: reconnect soon
                        continue
                    except (OSError, WebSocketError) as e:
                        WARN.fail(
                            "websocket", f"unavailable ({type(e).__name__}), polling every {self.s.poll_s} s"
                        )
                        # A session that was up and died (timeout, dead path): reconnect soon; a refused
                        # connection: poll and retry in 5 minutes.
                        next_ws = time.monotonic() + (5 if self.ws_established else 300)
                try:
                    self.poll_once()
                    WARN.ok("commands")
                except IngestError as e:
                    WARN.fail("commands", f"poll failed: {e}")
            except Exception as e:  # the control channel must outlive any single bad round
                WARN.fail("control", f"{type(e).__name__}: {e}")
                next_ws = time.monotonic() + 300
            self.stop.wait(self.s.poll_s)

    def run(self) -> None:
        threads = [
            threading.Thread(target=self.metrics_loop, name="metrics", daemon=True),
            threading.Thread(target=self.logs_loop, name="logs", daemon=True),
            threading.Thread(target=self.control_loop, name="control", daemon=True),
            threading.Thread(target=self.inventory_loop, name="inventory", daemon=True),
            threading.Thread(target=self.panel_loop, name="panel", daemon=True),
            threading.Thread(target=self.selftest_loop, name="selftest", daemon=True),
        ]
        if self.telemetry:
            self.telemetry.stop = self.stop
            threads.append(threading.Thread(target=self.telemetry.run, name="telemetry", daemon=True))
        if self.s.client:
            codes = ClientCodes(self.s, lambda path, body: post_json(self.s, path, body))
            threads.append(threading.Thread(target=codes.run, args=(self.stop,), name="client", daemon=True))
        for t in threads:
            t.start()
        self.stop.wait()


# P10: job reports (the Worker keeps them in the Control object: no D1 rows) and the inventory pushes that
# follow panel actions (a D1 upsert each, at most one per INVENTORY_MIN_GAP_S); a busy admin day (50 actions,
# a few job reports and one inventory push each) stays well below these.
PANEL_REQUESTS = 400
PANEL_ROWS = 100
# P13 (EXPERIENCE=1): one minute row per minute + one segment per 5 minutes (requests); the Worker writes the
# raw row and the 1-minute summary per minute, plus capped event rows (verdict changes, lag spikes: 500/day
# each, with their daily counter), sessions, messages and the hourly rollups (~600). Backfill replays only
# rows that never arrived.
EXPERIENCE_REQUESTS = 1440 + 288
EXPERIENCE_ROWS = 2 * 1440 + 2 * 2 * 500 + 600
# v1.1 (CLIENT=1): at most CLIENT_PER_HOUR code requests per hour; the Worker writes the code row and its use.
CLIENT_REQUESTS = 24 * CLIENT_PER_HOUR
CLIENT_ROWS = 2 * CLIENT_REQUESTS


def daily_budget(
    interval_s: int = 60,
    max_rows: int = 30000,
    log_batch_s: int = 300,
    poll_s: int = 15,
    experience: bool = True,
    client: bool = False,
) -> dict[str, int]:
    """Worker requests and D1 rows written per day by this agent in the worst case (WebSocket down all day:
    polling). Rows: one per sample, the day's log rows, one counter upsert per log batch, one inventory
    upsert per INVENTORY_S (P9), and the Worker's rollup (one 5-min row per 5 minutes written, one expired
    rollup deleted); sample and log day tables are dropped whole, which writes no rows."""
    samples = 86400 // interval_s
    batches = 86400 // log_batch_s
    polls = 86400 // poll_s
    inventory = 86400 // INVENTORY_S
    return {
        "requests": samples
        + 2 * batches
        + polls
        + inventory
        + PANEL_REQUESTS
        + (EXPERIENCE_REQUESTS if experience else 0)
        + (CLIENT_REQUESTS if client else 0),
        "d1_rows": samples
        + max_rows
        + batches
        + inventory
        + 2 * 288
        + PANEL_ROWS
        + (EXPERIENCE_ROWS if experience else 0)
        + (CLIENT_ROWS if client else 0),
    }


# --- v1.3: the same actions from mcctl, over Tailscale SSH --------------------------------------------------

# `sudo mc-run -/etc/minecraft/notify.env mc_agent.py cli LOGIN ACTION [ARGS_JSON]` (mcctl/actions.py): runs
# as the ops user in mc-run's sandbox, so systemd actions go through the same polkit rules as the agent's. No
# signature: the caller is the deploy user over Tailscale SSH (who has sudo anyway); the Executor's own
# checks, the job runner and the audit entry are the panel's. Destructive world and plugin actions also need
# the typed confirmation the panel asks for (cli_confirm), checked here again.
CLI_LOGIN = re.compile(r"mcctl:[A-Za-z0-9._@-]{1,64}")
CLI_ACTIONS = ("restart", "plugin", "moderate", "world_create", "world_switch", "world_delete")
CLI_LISTS = ("worlds", "plugins")
CLI_USAGE = (
    f"usage: mc_agent.py cli mcctl:USER {{{'|'.join(CLI_ACTIONS)}}} [ARGS_JSON] | "
    f"mc_agent.py cli mcctl:USER {{{'|'.join(CLI_LISTS)}}}"
)


def cli_confirm(action: str, args: dict[str, Any]) -> str | None:
    """The typed confirmation an action needs (the Worker's phrases for the world library; plugins: the
    CLI's own, the panel confirms them on its confirmation host)."""
    name = args.get("name")
    if action == "world_create":
        return f"create {name}"
    if action == "world_switch":
        return f"switch to {name}"
    if action == "world_delete":
        return f"delete {name}"
    if action == "plugin":
        return f"{'enable' if args.get('enabled') is True else 'disable'} {name}"
    return None


def cli_panel(state_dir: Path, report: Callable[[dict[str, Any]], None]) -> Panel:
    """The agent's Panel (default paths), with a job runner that runs in the foreground."""
    return Panel(
        jobs=mc_panel.Jobs(report, lock_path=mc_panel.JOB_LOCK_FILE, background=False),
        pending=mc_panel.Pending(state_dir / "pending.json"),
        rejected=mc_panel.RejectedLogins(state_dir / "rejected.json"),
    )


def cli_main(
    argv: list[str],
    panel: Panel | None = None,
    state_dir: Path | None = None,
    out: Callable[[str], None] | None = None,
) -> int:
    """One action for mcctl; prints the job's progress (`countdown: ...`, `running: ...`) and ends with
    `RESULT ok|failed|refused: <text>`. Exit 0 done, 1 failed/cancelled/refused, 2 usage."""
    say = out or (lambda line: print(line, flush=True))
    if len(argv) not in (2, 3) or not CLI_LOGIN.fullmatch(argv[0]):
        say(CLI_USAGE)
        return 2
    login, action = argv[0], argv[1]
    try:
        raw = json.loads(argv[2]) if len(argv) == 3 else {}
    except ValueError:
        raw = None
    if not isinstance(raw, dict) or (action not in CLI_ACTIONS and action not in CLI_LISTS):
        say(CLI_USAGE)
        return 2
    state_dir = state_dir or Path("/var/lib/mc-agent")
    last: dict[str, str] = {}

    def report(job: dict[str, Any]) -> None:
        line = f"{job.get('state')}: {job.get('step') or job.get('label')}"
        if line != last.get("line"):
            last["line"] = line
            say(line)

    if panel is None:
        panel = cli_panel(state_dir, report)
    else:
        panel.jobs.report = report
    executor = Executor(lambda: {}, None, panel)
    if action == "plugins":
        disabled = set(mc_panel.read_disabled(panel.plugins_disabled))
        lock = mc_panel.read_lock(panel.plugins_lock)
        say(json.dumps([p | {"enabled": p["name"] not in disabled} for p in lock], sort_keys=True))
        return 0
    if action == "worlds":
        say(json.dumps(worlds_inventory(panel, executor), sort_keys=True))
        return 0

    args = {k: v for k, v in raw.items() if k != "confirm"}
    shown = json.dumps(args, sort_keys=True)[:500]

    def finish(ok: bool, kind: str, text: str) -> int:
        audit(
            f"mcctl action={action} ok={ok} login={login}",
            action=action,
            login=login,
            args=shown,
            via="mcctl",
            result="ok" if ok else text[:300],
        )
        say(f"RESULT {kind}: {text}")
        return 0 if ok else 1

    want = cli_confirm(action, args)
    if want is not None and raw.get("confirm") != want:
        return finish(False, "refused", f'{action}: type "{want}" to confirm')
    if action.startswith("world_") and not state_dir.is_dir():
        why = "the world library records its changes through mc-agent (panel.mode: cloudflare): not here"
        return finish(False, "refused", f"{action}: {why}")

    def cancel(*_: object) -> None:  # mcctl's session ended (Ctrl-C, ssh gone): cancel a countdown
        job = panel.jobs.current
        if job is not None and job.state == "countdown":
            job.cancel.set()

    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        with contextlib.suppress(ValueError, OSError):  # not the main thread (tests)
            signal.signal(sig, cancel)
    try:
        text = executor.run(
            {"id": f"mcctl-{int(time.time())}", "action": action, "args": args, "login": login}
        )
    except Rejected as e:
        return finish(False, "refused", str(e))
    except (OSError, subprocess.SubprocessError, mc_rcon.RconError) as e:
        return finish(False, "failed", f"{action} failed: {type(e).__name__}")
    job = panel.jobs.current
    if action == "moderate" or job is None or job.id != executor.cmd_id:
        return finish(True, "ok", text)  # no job: done at once (moderation, a restart without countdown)
    if job.state == "done":
        return finish(True, "ok", job.step)
    return finish(False, "failed" if job.state == "failed" else job.state, job.step or job.state)


def main() -> int:
    try:
        settings = Settings.from_env()
    except ValueError as e:
        print(f"mc-agent: {e}", file=sys.stderr)
        return 2
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    agent = Agent(settings)
    signal.signal(signal.SIGTERM, lambda *_: agent.stop.set())
    signal.signal(signal.SIGINT, lambda *_: agent.stop.set())
    print(f"mc-agent {VERSION}: pushing to {urllib.parse.urlsplit(settings.ingest_url).hostname}")
    agent.run()
    return 0


# --- v1.4: `mc_agent.py telemetry`, the telemetry alone ---------------------------------------------------
# The telemetry in a process of its own, with the same inputs and outputs as inside the agent
# (make_telemetry), plus what a separate process needs: online.json (who is online by the latest snapshot,
# {uuid: {u, n, tips, join}}, rewritten after every poll, atomically, 0640), its own reader of systemd's
# per-run resource lines (Overhead.observe_journal, which the agent's logs loop feeds in-process;
# SYSLOG_IDENTIFIER=systemd, cursor in journal.json, every LOG_BATCH_S), and itself in the overhead card
# (SIDECAR_UNITS). No unit runs it: both agents run the telemetry in-process (the Rust one: smallworld-
# agent's telemetry module). It is the entry point the telemetry parity harness drives
# (tests/telemetry_parity), as `smallworld-agent telemetry` is on the Rust side.

SIDECAR_DIR = Path("/var/lib/mc-telemetry")  # StateDirectory=mc-telemetry (the Python agent's segments/ too)
ONLINE_FILE = SIDECAR_DIR / "online.json"  # = rust telemetry::sidecar::ONLINE_FILE
SIDECAR_JOURNAL_STATE = SIDECAR_DIR / "journal.json"
SIDECAR_UNITS = (*telemetry.OVERHEAD_UNITS, "mc-telemetry.service")
SIDECAR_JOURNAL_MATCH = "SYSLOG_IDENTIFIER=systemd"
ONLINE_KEYS = ("u", "n", "tips", "join")


def online_map(online: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Telemetry.online() narrowed to what player_message needs (no position, world or client details)."""
    return {u: {k: p.get(k) for k in ONLINE_KEYS} for u, p in online.items()}


def write_online(path: Path, online: dict[str, dict[str, Any]]) -> None:
    """Atomic (tmp + rename), 0640 like the verdicts."""
    telemetry._atomic_text(path, json.dumps(online_map(online), sort_keys=True))


class SidecarTelemetry(telemetry.Telemetry):
    """Telemetry that also publishes its online map after every poll (even a failed one: a fresh file means a
    live process)."""

    online_file: Path = ONLINE_FILE

    def poll(self) -> dict[str, Any] | None:
        try:
            return super().poll()
        finally:
            try:
                write_online(self.online_file, self.online())
            except OSError as e:
                WARN.fail("online.json", f"{type(e).__name__}: {e}")
            else:
                WARN.ok("online.json")


def overhead_round(
    state: Path, observe: Callable[[list[dict[str, Any]]], None], read_fn: Callable[..., Any] = read_journal
) -> bool:
    """One batch of systemd's own journal lines into Overhead.observe_journal; True if more are waiting. The
    cursor is saved after the batch was observed (a crash re-reads it: a run counted twice at worst)."""
    st = LogState.load(state)
    entries, cursor, more = read_fn(st.cursor, argv=[*journal_argv(st.cursor), SIDECAR_JOURNAL_MATCH])
    observe(entries)
    if cursor != st.cursor:
        st.cursor = cursor
        st.save(state)
    return bool(more)


def overhead_loop(s: Settings, t: telemetry.Telemetry, stop: threading.Event, state: Path) -> None:
    while not stop.wait(s.log_batch_s):
        try:
            for _ in range(LOG_CATCHUP_BATCHES):
                if not overhead_round(state, t.overhead.observe_journal) or stop.is_set():
                    break
            WARN.ok("overhead journal")
        except Exception as e:  # one bad round must not end the loop
            WARN.fail("overhead journal", f"{type(e).__name__}: {e}")


def telemetry_main() -> int:
    """`mc_agent.py telemetry`: the telemetry alone (see above; the parity harness's entry point)."""
    try:
        settings = Settings.from_env()
        if not settings.experience:
            raise ValueError("EXPERIENCE is off: the telemetry sidecar has nothing to do")
        t = make_telemetry(settings, stats=lambda: telemetry.unit_stats(SIDECAR_UNITS), cls=SidecarTelemetry)
    except ValueError as e:
        print(f"mc-telemetry: {e}", file=sys.stderr)
        return 2
    settings.state_dir.mkdir(parents=True, exist_ok=True)
    stop = threading.Event()
    t.stop = stop
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    print(f"mc-telemetry {VERSION}: pushing to {urllib.parse.urlsplit(settings.ingest_url).hostname}")
    loop_args = (settings, t, stop, SIDECAR_JOURNAL_STATE)
    threads = [
        threading.Thread(target=t.run, name="telemetry", daemon=True),
        threading.Thread(target=overhead_loop, args=loop_args, name="overhead", daemon=True),
    ]
    for th in threads:
        th.start()
    stop.wait()
    return 0


if __name__ == "__main__":
    os.umask(0o077)  # spool, cursor and nonces stay private (StateDirectoryMode=0700 too)
    if sys.argv[1:2] == ["cli"]:
        sys.exit(cli_main(sys.argv[2:]))
    sys.exit(telemetry_main() if sys.argv[1:2] == ["telemetry"] else main())
