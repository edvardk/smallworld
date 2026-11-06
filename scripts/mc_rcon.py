#!/usr/bin/env python3
"""Minimal Source-RCON client for the local Minecraft server, plus the operations built on it.

Connection details (port, password) are read from server.properties, so no extra secret files exist.
Stdlib only: this runs on the server.

    mc_rcon.py cmd list                      # run a command, print the reply
    mc_rcon.py stop                          # save-all flush, stop, wait for $MAINPID (systemd ExecStop)
    mc_rcon.py countdown 60                  # announce a restart at 60/30/10s (skipped if nobody online)
    mc_rcon.py worldborder --dimension minecraft:overworld --radius 5000   # idempotent
"""

from __future__ import annotations

import argparse
import errno
import os
import re
import socket
import stat
import struct
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path

SERVER_DIR = Path(os.environ.get("MC_SERVER_DIR", "/opt/minecraft/server"))

TYPE_RESPONSE, TYPE_COMMAND, TYPE_LOGIN = 0, 2, 3
FORMATTING = re.compile(r"§.")


class RconError(Exception):
    pass


class RconAuthError(RconError):
    """The server answered the login, but refused the password: it is alive."""


class RconSettingsError(RconError):
    """server.properties couldn't be read, or RCON is disabled there: nothing was asked of the server."""


class RconReplyError(RconError):
    """The login succeeded, then a command's reply failed: the server is alive."""


UNTRUSTED_MAX_BYTES = 16 << 20


def read_untrusted(path: Path | str, limit: int = UNTRUSTED_MAX_BYTES, tail: bool = False) -> bytes:
    """Reads a file in a directory the minecraft user can write (P13 review L4: code running in the JVM may
    have planted something else there). Never blocks and never follows a symlink: opened O_NONBLOCK|O_NOFOLLOW
    (a FIFO opens at once, a symlink fails with ELOOP), then refused unless fstat says it's a regular file;
    reads at most `limit` bytes (the first, or with `tail` the last). Raises OSError."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC | getattr(os, "O_NOCTTY", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, "not a regular file", str(path))
        if tail and st.st_size > limit:
            os.lseek(fd, st.st_size - limit, os.SEEK_SET)
        chunks, left = [], limit
        while left > 0:
            chunk = os.read(fd, min(left, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def read_properties(path: Path) -> dict[str, str]:
    """Parse a Java .properties file (the subset Minecraft writes)."""
    props: dict[str, str] = {}
    for raw in read_untrusted(path, 1 << 20).decode("utf-8").splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        m = re.match(r"((?:\\.|[^=:\s\\])+)\s*[=:\s]\s*(.*)$", line)
        if m:
            props[unescape(m.group(1))] = unescape(m.group(2))
        else:
            props[unescape(line)] = ""
    return props


def unescape(s: str) -> str:
    out, i = [], 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            n = s[i + 1]
            if n == "u" and i + 5 < len(s):
                out.append(chr(int(s[i + 2 : i + 6], 16)))
                i += 6
                continue
            out.append({"t": "\t", "n": "\n", "r": "\r", "f": "\f"}.get(n, n))
            i += 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def strip_formatting(s: str) -> str:
    return FORMATTING.sub("", s)


class Rcon:
    def __init__(self, host: str, port: int, password: str, timeout: float = 10.0) -> None:
        self.host, self.port, self.password, self.timeout = host, port, password, timeout
        self.sock: socket.socket | None = None
        self._id = 0

    def __enter__(self) -> Rcon:
        self.sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        rid, _ = self._request(TYPE_LOGIN, self.password)
        if rid == -1:
            raise RconAuthError("RCON authentication failed")
        return self

    def __exit__(self, *exc: object) -> None:
        if self.sock:
            self.sock.close()
            self.sock = None

    def _send(self, rtype: int, payload: str) -> int:
        assert self.sock is not None
        self._id += 1
        body = struct.pack("<ii", self._id, rtype) + payload.encode("utf-8") + b"\x00\x00"
        self.sock.sendall(struct.pack("<i", len(body)) + body)
        return self._id

    def _recv(self) -> tuple[int, int, str]:
        (length,) = struct.unpack("<i", self._read_exact(4))
        data = self._read_exact(length)
        rid, rtype = struct.unpack("<ii", data[:8])
        return rid, rtype, data[8:-2].decode("utf-8", errors="replace")

    def _read_exact(self, n: int) -> bytes:
        assert self.sock is not None
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise RconError("connection closed by server")
            buf += chunk
        return buf

    def _request(self, rtype: int, payload: str) -> tuple[int, str]:
        rid = self._send(rtype, payload)
        while True:
            got, ptype, body = self._recv()
            # Login replies with an empty RESPONSE before the auth result on some servers.
            if rtype == TYPE_LOGIN and ptype == TYPE_RESPONSE:
                continue
            if got not in (rid, -1):
                raise RconError(f"unexpected RCON request id {got} (expected {rid})")
            return got, body

    def command(self, cmd: str, timeout: float | None = None, single: bool = False) -> str:
        """Run a command and return its full reply.

        timeout overrides the socket timeout (slow commands); single=True reads one packet only
        (for `stop`, after which the server may close the connection).
        """
        assert self.sock is not None
        if timeout is not None:
            self.sock.settimeout(timeout)
        try:
            cid = self._send(TYPE_COMMAND, cmd)
            rid, _, body = self._recv()
            if rid != cid:
                raise RconError(f"unexpected RCON request id {rid} (expected {cid})")
            parts = [body]
            if single:
                return strip_formatting(body)
            # Replies over 4096 bytes arrive as several packets with no end marker. The server handles
            # one request at a time, in order, so a follow-up packet of an invalid type is answered
            # ("Unknown request 0") only after the last fragment. It is sent after the first fragment
            # arrives, because vanilla drops the connection if two requests arrive in one read.
            marker = self._send(TYPE_RESPONSE, "")
            while True:
                rid, _, body = self._recv()
                if rid == marker:
                    break
                if rid != cid:
                    raise RconError(f"unexpected RCON request id {rid} (expected {cid})")
                parts.append(body)
        finally:
            if timeout is not None:
                self.sock.settimeout(self.timeout)
        return strip_formatting("".join(parts))


class PersistentRcon:
    """One long-lived RCON connection, for a long-running process (mc-agent's metrics).

    Vanilla starts a thread per RCON connection and logs "Thread RCON Client /127.0.0.1 started" and
    "... shutting down" for each one, so a poller that connects every minute fills the server log. This keeps
    the connection open between calls (the server sets no idle timeout on it). Thread-safe: one call at a
    time.
    Any error closes the connection; the next call reconnects, at once if a kept connection turned out to be
    dead (a server restart) and with an exponential backoff (1 s doubling to `max_backoff`) after a failed
    connect. `factory` builds an unconnected Rcon (e.g. `lambda: connect(server_dir, 5.0)`): it reads
    server.properties again on every reconnect, so a changed password or port is picked up. Use it for
    read-only commands: one may be sent again after a dropped connection.
    """

    def __init__(
        self,
        factory: Callable[[], Rcon],
        max_backoff: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory, self.max_backoff, self.clock = factory, max_backoff, clock
        self.lock = threading.Lock()
        self.conn: Rcon | None = None
        self.failures = 0
        self.retry_at = 0.0
        self.connects = 0  # connections opened (tests, and the e2e count of RCON client threads)

    def _open(self) -> Rcon:
        now = self.clock()
        if now < self.retry_at:
            raise RconError(f"RCON reconnect backing off for {self.retry_at - now:.0f}s")
        try:
            client = self.factory()
        except (OSError, KeyError, ValueError, RconError) as e:
            raise RconSettingsError(f"RCON settings unreadable ({type(e).__name__}: {e})") from e
        self.connects += 1
        try:
            client.__enter__()
        except BaseException:
            client.__exit__(None, None, None)
            self.failures += 1
            self.retry_at = now + min(self.max_backoff, 2.0 ** (self.failures - 1))
            raise
        self.failures, self.retry_at = 0, 0.0
        return client

    def commands(self, cmds: Sequence[str], timeout: float | None = None) -> list[str]:
        """Each command's reply, in order. Raises RconSettingsError, RconAuthError (alive), RconReplyError
        (logged in, then the reply failed: alive) or OSError/RconError (couldn't connect)."""
        with self.lock:
            kept = self.conn is not None
            while True:
                if self.conn is None:
                    self.conn = self._open()
                try:
                    return [self.conn.command(c, timeout) for c in cmds]
                except (OSError, RconError) as e:
                    self.close_locked()
                    if not kept:
                        raise RconReplyError(f"{type(e).__name__}: {e}") from e
                    kept = False  # the kept connection was dead (server restarted?): one fresh try

    def command(self, cmd: str, timeout: float | None = None) -> str:
        return self.commands([cmd], timeout)[0]

    def close_locked(self) -> None:
        if self.conn is not None:
            self.conn.__exit__(None, None, None)
            self.conn = None

    def close(self) -> None:
        with self.lock:
            self.close_locked()


def connect(server_dir: Path | None = None, timeout: float = 10.0) -> Rcon:
    props = read_properties((server_dir or SERVER_DIR) / "server.properties")
    if props.get("enable-rcon") != "true":
        raise RconError("RCON is disabled in server.properties")
    host = props.get("rcon.ip") or "127.0.0.1"
    return Rcon(host, int(props.get("rcon.port", "25575")), props["rcon.password"], timeout)


def online_players(rcon: Rcon) -> int:
    m = re.search(r"There are (\d+)", rcon.command("list"))
    return int(m.group(1)) if m else 0


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def stop(wait_pid: int | None, timeout: float = 110.0) -> int:
    """Graceful stop for systemd ExecStop. Returns 0 if the server exited (or was already down).

    The whole stop (flush, stop, wait) stays within `timeout`, below the unit's TimeoutStopSec=120.
    """
    deadline = time.monotonic() + timeout
    try:
        with connect() as r:
            # save-all flush writes every chunk synchronously: allow it most of the budget.
            r.command("save-all flush", timeout=max(1.0, deadline - time.monotonic() - 10))
            r.command("stop", timeout=max(1.0, deadline - time.monotonic()), single=True)
    except (OSError, RconError) as e:
        print(f"RCON stop failed ({e}); systemd will fall back to SIGTERM", file=sys.stderr)
        return 1
    if wait_pid:
        while pid_alive(wait_pid) and time.monotonic() < deadline:
            time.sleep(0.5)
        if pid_alive(wait_pid):
            print("server still running after stop; systemd will SIGTERM it", file=sys.stderr)
            return 1
    return 0


def countdown(seconds: int, notify: bool = True) -> None:
    """Announce a restart in-game (60/30/10s). Silently skipped if the server isn't running."""
    try:
        with connect() as r:
            online = online_players(r)
            if online == 0:
                print("no players online; skipping countdown")
                return
            if notify:
                discord(restart_alert(seconds, online))
            marks = [m for m in (60, 30, 10) if m <= seconds] or [seconds]
            remaining = seconds
            for mark in marks:
                time.sleep(max(0, remaining - mark))
                remaining = mark
                r.command(f"say Server restarting in {mark} seconds for an update.")
            time.sleep(remaining)
            r.command("say Restarting now.")
    except (OSError, RconError) as e:
        print(f"countdown skipped: {e}")


def restart_alert(seconds: int, online: int) -> str:
    """The Discord note for a deploy restart (docs/operations.md "Alerts"); the template is alerts.py's."""
    from alerts import Alert, render  # local import keeps mc_rcon usable on its own

    return render(
        Alert(
            "info",
            f"The server restarts in {seconds} s for a config deploy",
            impact=f"{online} player(s) online are warned in-game and disconnected for about a minute; "
            "they can rejoin as soon as it is back.",
            cause="A deploy changed a setting that needs a restart.",
            details=f"in-game countdown {seconds} s",
        )
    )


def discord(message: str) -> None:
    from notify import discord as _discord  # local import keeps mc_rcon usable on its own

    _discord(message)


BORDER = re.compile(r"(\d+(?:\.\d+)?) block")


def worldborder(dimension: str, radius: int) -> bool:
    """Set a dimension's border to diameter 2*radius centred on 0,0. Returns True if it changed."""
    want = 2 * radius
    prefix = f"execute in {dimension} run "
    with connect() as r:
        m = BORDER.search(r.command(prefix + "worldborder get"))
        if m and round(float(m.group(1))) == want:
            return False
        r.command(prefix + "worldborder center 0 0")
        r.command(prefix + f"worldborder set {want}")
        m = BORDER.search(r.command(prefix + "worldborder get"))
        if not m or round(float(m.group(1))) != want:
            raise RconError(f"failed to set {dimension} border to {want}")
    return True


GAMERULE_VALUE = re.compile(r"(?:currently|now|already) set to:? (\S+)\s*$")
GAMERULE_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
UNKNOWN_DIMENSION = "Unknown dimension"


def gamerule(name: str, value: str, dimensions: list[str]) -> bool:
    """v1.2: set a game rule in every dimension (Paper keeps game rules per world; the console's own is the
    overworld's, so each dimension goes through `execute in`). Idempotent: reads first, sets only a different
    value, reads again. A dimension the server doesn't have is skipped. Returns True if anything changed."""
    if not GAMERULE_NAME.match(name) or not re.fullmatch(r"true|false|-?\d{1,9}", value):
        raise RconError(f"bad game rule {name!r} = {value!r}")
    changed = False
    with connect() as r:
        for dim in dimensions:
            prefix = f"execute in {dim} run "
            reply = r.command(prefix + f"gamerule {name}")
            if UNKNOWN_DIMENSION in reply:
                continue
            m = GAMERULE_VALUE.search(reply)
            if m is None:
                raise RconError(f"gamerule {name} in {dim}: unexpected reply {reply[:200]!r}")
            if m.group(1) == value:
                continue
            r.command(prefix + f"gamerule {name} {value}")
            m = GAMERULE_VALUE.search(r.command(prefix + f"gamerule {name}"))
            if m is None or m.group(1) != value:
                raise RconError(f"failed to set {name} to {value} in {dim}")
            changed = True
    return changed


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("cmd")
    c.add_argument("command", nargs="+")
    sub.add_parser("stop")
    cd = sub.add_parser("countdown")
    cd.add_argument("seconds", type=int)
    wb = sub.add_parser("worldborder")
    wb.add_argument("--dimension", required=True)
    wb.add_argument("--radius", type=int, required=True)
    gr = sub.add_parser("gamerule")  # v1.2: idempotent, every dimension
    gr.add_argument("--name", required=True)
    gr.add_argument("--value", required=True)
    gr.add_argument("--dimension", action="append", required=True)
    args = p.parse_args(argv)

    if args.cmd == "cmd":
        with connect() as r:
            print(r.command(" ".join(args.command)))
        return 0
    if args.cmd == "stop":
        pid = os.environ.get("MAINPID")
        return stop(int(pid) if pid else None)
    if args.cmd == "countdown":
        countdown(args.seconds)
        return 0
    if args.cmd == "gamerule":
        print("CHANGED" if gamerule(args.name, args.value, args.dimension) else "OK")
        return 0
    changed = worldborder(args.dimension, args.radius)
    print("CHANGED" if changed else "OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
