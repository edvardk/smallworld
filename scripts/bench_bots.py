#!/usr/bin/env python3
"""Headless offline-mode players for the benches (v1.1, scripts/bench_view.py). Bench machines only.

A minimal Minecraft Java client, stdlib only: handshake, login (offline mode: no account, no encryption),
compression, the configuration phase (known packs echoed back, client information with the view distance the
bench wants, keep-alives, pings), then play: keep-alives, pings, teleport confirmations, chunk-batch
acknowledgements (so the server keeps sending chunks as a real client asks for them), `player_loaded`, an
optional chat command (`/view 16`), and walking in a straight line at walking speed: one position per tick,
each followed by `client_tick_end` as the 26.3 client does.

Packet ids are not hard-coded: they come from the pinned server jar's own data report (`packets.json`, written
by `java -DbundlerMainClass=net.minecraft.data.Main -jar server.jar --reports`), so a Minecraft update can't
silently shift them. The payload layouts used here were checked in the pinned 26.3 jar (javap):
ClientInformation (locale, view distance, chat visibility, colours, skin parts, main hand, text filtering,
listing, particle status), ServerboundMovePlayerPacket.Pos (x, y, z, flags), ClientboundPlayerPositionPacket
(teleport id, position, delta movement, rotation, relative flags), ServerboundAcceptTeleportationPacket (26.3:
teleport id, position, rotation).
"""

from __future__ import annotations

import json
import socket
import struct
import subprocess
import threading
import time
import uuid
import zlib
from pathlib import Path

WALK_PER_TICK = 0.2  # blocks per 50 ms: a little under walking speed (4.3 blocks/s)


def packet_ids(jdk: Path, server_jar: Path, out: Path) -> dict[str, dict[str, dict[str, int]]]:
    """{state: {direction: {name: id}}} from the jar's own report (generated once, cached in `out`)."""
    report = out / "reports" / "packets.json"
    if not report.is_file():
        subprocess.run(
            [str(jdk / "bin" / "java"), "-DbundlerMainClass=net.minecraft.data.Main", "-jar", str(server_jar),
             "--reports", "--output", str(out)],
            cwd=out.parent, check=True, capture_output=True, timeout=600,
        )  # fmt: skip
    raw = json.loads(report.read_text())
    out: dict[str, dict[str, dict[str, int]]] = {}
    for state, dirs in raw.items():
        out[state] = {
            d: {name.removeprefix("minecraft:"): v["protocol_id"] for name, v in p.items()}
            for d, p in dirs.items()
        }
    return out


def varint(n: int) -> bytes:
    n &= 0xFFFFFFFF
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def read_varint(buf: bytes, pos: int = 0) -> tuple[int, int]:
    value = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return (value - (1 << 32) if value >= 1 << 31 else value), pos
        shift += 7


def string(s: str) -> bytes:
    raw = s.encode()
    return varint(len(raw)) + raw


class Bot:
    """One player. `run()` in a thread: connects, joins, walks (dx, dz per tick) until `stop` is set."""

    def __init__(
        self,
        ids: dict[str, dict[str, dict[str, int]]],
        port: int,
        name: str,
        direction: tuple[float, float],
        view: int,
        command: str = "",
        protocol: int = 0,
    ) -> None:
        self.ids, self.port, self.name, self.dir, self.view = ids, port, name, direction, view
        self.command, self.protocol = command, protocol
        self.stop = threading.Event()
        self.joined = threading.Event()
        self.sock: socket.socket | None = None
        self.threshold = -1
        self.state = "login"
        self.pos: list[float] | None = None
        self.lock = threading.Lock()
        self.chunks = 0
        self.error = ""

    def send(self, state: str, name: str, body: bytes = b"") -> None:
        data = varint(self.ids[state]["serverbound"][name]) + body
        if self.threshold >= 0:
            data = varint(len(data)) + zlib.compress(data) if len(data) >= self.threshold else b"\x00" + data
        assert self.sock is not None
        with self.lock:
            self.sock.sendall(varint(len(data)) + data)

    def _recv_exact(self, n: int) -> bytes:
        assert self.sock is not None
        out = bytearray()
        while len(out) < n:
            chunk = self.sock.recv(n - len(out))
            if not chunk:
                raise ConnectionError("closed")
            out += chunk
        return bytes(out)

    def _read(self) -> tuple[int, bytes]:
        length = shift = 0
        while True:
            b = self._recv_exact(1)[0]
            length |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        data = self._recv_exact(length)
        if self.threshold >= 0:
            size, p = read_varint(data)
            data = zlib.decompress(data[p:]) if size else data[p:]
        pid, p = read_varint(data)
        return pid, data[p:]

    def client_information(self) -> bytes:
        # locale, view distance, chat visible, colours, all skin parts, right hand, no text filtering, listed,
        # all particles
        head = string("en_us") + bytes([self.view]) + varint(0) + b"\x01" + b"\x7f"
        return head + varint(1) + b"\x00" + b"\x01" + varint(0)

    def run(self) -> None:
        try:
            self._run()
        except (OSError, ConnectionError, struct.error, IndexError, zlib.error) as e:
            if not self.stop.is_set():
                self.error = f"{type(e).__name__}: {e}"
        finally:
            if self.sock:
                self.sock.close()

    def _run(self) -> None:
        self.sock = socket.create_connection(("127.0.0.1", self.port), timeout=30)
        self.sock.settimeout(30)
        hs = varint(self.protocol) + string("127.0.0.1") + struct.pack(">H", self.port) + varint(2)
        self.send("handshake", "intention", hs)
        offline = uuid.uuid3(uuid.NAMESPACE_OID, "OfflinePlayer:" + self.name)  # the server derives its own
        self.send("login", "hello", string(self.name) + offline.bytes)
        cb = {s: {v: k for k, v in d.get("clientbound", {}).items()} for s, d in self.ids.items()}
        walker: threading.Thread | None = None
        while not self.stop.is_set():
            pid, body = self._read()
            name = cb[self.state].get(pid, "")
            if self.state == "login":
                if name == "login_compression":
                    self.threshold, _ = read_varint(body)
                elif name == "login_finished":
                    self.send("login", "login_acknowledged")
                    self.state = "configuration"
                    self.send("configuration", "client_information", self.client_information())
                elif name == "login_disconnect":
                    raise ConnectionError(f"login refused: {body[:200]!r}")
            elif self.state == "configuration":
                if name == "select_known_packs":
                    self.send("configuration", "select_known_packs", body)  # we "know" what it offers
                elif name == "keep_alive":
                    self.send("configuration", "keep_alive", body)
                elif name == "ping":
                    self.send("configuration", "pong", body)
                elif name == "finish_configuration":
                    self.send("configuration", "finish_configuration")
                    self.state = "play"
                elif name == "disconnect":
                    raise ConnectionError("disconnected during configuration")
            else:
                if name == "keep_alive":
                    self.send("play", "keep_alive", body)
                elif name == "ping":
                    self.send("play", "pong", body)
                elif name == "player_position":
                    tid, p = read_varint(body)
                    x, y, z = struct.unpack(">ddd", body[p : p + 24])
                    yaw, pitch = struct.unpack(">ff", body[p + 48 : p + 56])  # after the delta movement
                    # 26.3: the confirmation echoes the position and rotation it accepted
                    self.send(
                        "play",
                        "accept_teleportation",
                        varint(tid) + struct.pack(">dddff", x, y, z, yaw, pitch),
                    )
                    first = self.pos is None
                    self.pos = [x, y, z]
                    if first:
                        self.send("play", "player_loaded")
                        if self.command:
                            self.send("play", "chat_command", string(self.command))
                        self.joined.set()
                        walker = threading.Thread(target=self.walk, daemon=True)
                        walker.start()
                elif name == "level_chunk_with_light":
                    self.chunks += 1  # (Paper's chunk system sends them without vanilla's batches)
                elif name == "chunk_batch_finished":
                    self.send("play", "chunk_batch_received", struct.pack(">f", 64.0))
                elif name == "disconnect":
                    raise ConnectionError(f"disconnected: {body[:300]!r}")
                elif name == "start_configuration":
                    self.send("play", "configuration_acknowledged")
                    self.state = "configuration"

    def walk(self) -> None:
        while not self.stop.wait(0.05):
            if self.pos is None:
                continue
            self.pos[0] += self.dir[0] * WALK_PER_TICK
            self.pos[2] += self.dir[1] * WALK_PER_TICK
            body = struct.pack(">ddd", *self.pos) + b"\x01"  # on ground
            try:
                # 26.3: one position per client tick, then `client_tick_end` (a second position in one tick
                # is "invalid_player_movement"; ServerGamePacketListenerImpl.receivedPositionThisTick)
                self.send("play", "move_player_pos", body)
                self.send("play", "client_tick_end")
            except OSError:
                return


def protocol_version(server_jar: Path) -> int:
    import zipfile

    with zipfile.ZipFile(server_jar) as z:
        return int(json.loads(z.read("version.json"))["protocol_version"])


def join_all(bots: list[Bot], timeout: float = 120) -> list[threading.Thread]:
    threads = [threading.Thread(target=b.run, daemon=True, name=b.name) for b in bots]
    for t in threads:
        t.start()
        time.sleep(0.5)
    end = time.time() + timeout
    for b in bots:
        if not b.joined.wait(max(1.0, end - time.time())):
            raise SystemExit(f"bot {b.name} didn't join: {b.error or 'timeout'}")
    return threads
