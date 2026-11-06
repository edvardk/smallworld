"""Minecraft Java status ping, stdlib only (init step 12: smoke test from the laptop, IPv4 and IPv6).

Server List Ping (protocol docs, wiki.vg "Server List Ping"): handshake (packet 0x00: protocol version
VarInt, address string, port u16, next state 1), status request (0x00), status response (0x00 + JSON string).
`scripts/smoke_test.py` does the same with mcstatus for CI; mcctl stays stdlib + PyYAML.
"""

from __future__ import annotations

import json
import socket
import struct
from typing import Any

PROTOCOL = -1  # "any": servers answer status for any protocol version


def _varint(n: int) -> bytes:
    n &= 0xFFFFFFFF
    out = b""
    while True:
        byte = n & 0x7F
        n >>= 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def _read_varint(sock: socket.socket) -> int:
    n = 0
    for i in range(5):
        b = sock.recv(1)
        if not b:
            raise OSError("connection closed")
        n |= (b[0] & 0x7F) << (7 * i)
        if not b[0] & 0x80:
            return n
    raise OSError("VarInt too long")


def _packet(data: bytes) -> bytes:
    return _varint(len(data)) + data


def status(
    address: str, port: int = 25565, *, host: str | None = None, timeout: float = 10.0
) -> dict[str, Any]:
    """The server's status JSON (`version`, `players`, `description`), or OSError/ValueError."""
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    name = (host or address).encode()
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect((address, port))
        handshake = (
            b"\x00" + _varint(PROTOCOL) + _varint(len(name)) + name + struct.pack(">H", port) + b"\x01"
        )
        sock.sendall(_packet(handshake) + _packet(b"\x00"))
        length = _read_varint(sock)
        data = b""
        while len(data) < length:
            chunk = sock.recv(length - len(data))
            if not chunk:
                raise OSError("connection closed")
            data += chunk
    if not data or data[0] != 0:
        raise ValueError("not a status response")
    # data = packet id 0x00, then a VarInt-prefixed JSON string
    i, n, shift = 1, 0, 0
    while True:
        b = data[i]
        n |= (b & 0x7F) << shift
        i += 1
        shift += 7
        if not b & 0x80:
            break
    return json.loads(data[i : i + n].decode())
