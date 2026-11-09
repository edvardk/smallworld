"""The outside world of mc-agent, faked on loopback for the parity harness (tests/agent_parity).

Every fake records what the agent sent, in arrival order, and answers like the real thing:
- FakeWorker: the Cloudflare Worker's agent routes (watchdog/worker.js `handleAgent` and the Control Durable
  Object): bearer-token check, /ingest/*, /agent/commands + /agent/results (the poll fallback),
  /agent/github-token, /agent/client-code and the WebSocket at /agent/ws (RFC 6455 server side: the "ping" ->
  "pong" auto-response, `hello` -> the queued commands, `result` and `console` messages).
- FakeGitHub: the GitHub REST endpoints the whitelist/world -> Git flow and the self-test use, plus the two
  Mojang lookup endpoints (MOJANG_LOOKUP_URLS), like tests/e2e/agent_check.py's GitHubMock.
- FakeHttps: Discord's webhook and the watchdog's /maintenance over TLS (notify._post is https only), with a
  throw-away certificate the agent trusts through SSL_CERT_FILE.
- FakeRcon: Source RCON with scripted replies; it edits whitelist.json like the server does.
- Journal: the journald native-protocol socket (the audit entries).

Nothing here talks to the network beyond 127.0.0.1.
"""

from __future__ import annotations

import base64
import contextlib
import gzip
import hashlib
import http.server
import json
import select
import socket
import ssl
import struct
import subprocess
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def wait_until(pred: Callable[[], Any], timeout: float = 10.0, what: str = "condition") -> Any:
    """Poll `pred` until it returns something truthy; raise TimeoutError naming `what`."""
    deadline = time.monotonic() + timeout
    while True:
        got = pred()
        if got:
            return got
        if time.monotonic() > deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(0.02)


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    owner: Any = None

    def log_message(self, *a: object) -> None:
        pass

    def body(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def reply(self, code: int, body: object = None, raw: bytes | None = None) -> None:
        data = raw if raw is not None else json.dumps({} if body is None else body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self) -> None:
        self.owner.handle(self)

    do_GET = do_POST = do_PUT = do_DELETE = handle_any


class _TlsServer(_Server):
    """TLS with the handshake in the request's own thread (not the accept loop, which would serialise
    every client behind one slow handshake)."""

    ctx: ssl.SSLContext

    def finish_request(self, request: Any, client_address: Any) -> None:
        try:
            tls = self.ctx.wrap_socket(request, server_side=True)
        except (OSError, ssl.SSLError):
            return
        try:
            super().finish_request(tls, client_address)
        finally:
            with contextlib.suppress(OSError):
                tls.close()


def _serve(owner: Any, ctx: ssl.SSLContext | None = None) -> _Server:
    handler = type("Handler", (_Handler,), {"owner": owner})
    srv: _Server
    if ctx is not None:
        srv = _TlsServer(("127.0.0.1", 0), handler)
        srv.ctx = ctx
    else:
        srv = _Server(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return srv


def decode_body(raw: bytes, content_type: str) -> Any:
    if content_type == "application/gzip":
        try:
            text = gzip.decompress(raw).decode("utf-8")
        except (OSError, UnicodeDecodeError):
            return {"undecodable": len(raw)}
        return [json.loads(line) for line in text.splitlines() if line.strip()]
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return {"text": raw.decode("utf-8", "replace")}


# --- the Worker -----------------------------------------------------------------------------------------------


class WsSession:
    """One agent WebSocket on the fake Worker (server side)."""

    def __init__(self, handler: _Handler, worker: FakeWorker, index: int) -> None:
        self.h, self.worker, self.index = handler, worker, index
        self.sock = handler.connection
        self.send_lock = threading.Lock()
        self.closed = threading.Event()
        self.unmasked = 0  # client frames without a mask (RFC 6455 5.1 says every client frame is masked)
        self.worker_closed = False  # the fake sent the close first (close())

    def _read(self, n: int) -> bytes:
        data = self.h.rfile.read(n)
        if len(data) < n:
            raise ConnectionError("closed")
        return data

    def frame(self) -> tuple[bool, int, bytes]:
        b0, b1 = self._read(2)
        n = b1 & 0x7F
        if n == 126:
            (n,) = struct.unpack(">H", self._read(2))
        elif n == 127:
            (n,) = struct.unpack(">Q", self._read(8))
        mask = self._read(4) if b1 & 0x80 else None
        if mask is None:
            self.unmasked += 1
        payload = self._read(n)
        if mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return bool(b0 & 0x80), b0 & 0x0F, payload

    def send(self, opcode: int, payload: bytes) -> None:
        n = len(payload)
        head = bytes([0x80 | opcode])
        if n < 126:
            head += bytes([n])
        elif n < 1 << 16:
            head += bytes([126]) + struct.pack(">H", n)
        else:
            head += bytes([127]) + struct.pack(">Q", n)
        with self.send_lock:
            self.sock.sendall(head + payload)

    def send_text(self, text: str) -> None:
        self.send(0x1, text.encode())

    def close(self, code: int = 1000) -> None:
        self.worker_closed = True
        with contextlib.suppress(OSError):
            self.send(0x8, struct.pack(">H", code))

    def run(self) -> None:
        parts: list[bytes] = []
        try:
            while True:
                fin, op, data = self.frame()
                if op == 0x9:
                    self.worker.event("ws-ping", session=self.index)
                    self.send(0xA, data)
                    continue
                if op == 0xA:
                    continue
                if op == 0x8:
                    # The agent's answer to the Worker's close (ws_reconnect: by "worker"), or the close the agent
                    # starts itself (by "agent"): v1.5, deterministic at shutdown in both implementations (SIGTERM
                    # closes the socket with 1000, then the process exits). Answered unless it was the answer.
                    by = "worker" if self.worker_closed else "agent"
                    self.worker.event("ws-close", session=self.index, code=data[:2].hex(), by=by)
                    if not self.worker_closed:
                        with contextlib.suppress(OSError):
                            self.send(0x8, data[:2])
                    return
                parts.append(data)
                if not fin:
                    continue
                text = b"".join(parts).decode("utf-8", "replace")
                parts = []
                self.worker.ws_message(self, text)
        except (ConnectionError, OSError):
            return
        finally:
            self.closed.set()


class FakeWorker:
    """The Worker's agent routes. `ws_mode`: accept | refuse (503 to the upgrade) | close (close right after
    the hello). `fail`: route -> HTTP status to answer instead (e.g. {"POST /ingest/inventory": 500})."""

    def __init__(self, token: str) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []  # every HTTP request, in arrival order
        self.events: list[dict[str, Any]] = []  # WebSocket events
        self.queue: list[dict[str, str]] = []  # signed commands waiting for a poll or a hello
        self.results: dict[str, dict[str, Any]] = {}
        self.result_order: list[tuple[str, str]] = []  # (channel, id)
        self.console: list[dict[str, Any]] = []
        self.sessions: list[WsSession] = []
        self.ws_mode = "accept"
        self.fail: dict[str, int] = {}
        self.github_tokens: list[tuple[int, Any]] = []  # scripted /agent/github-token replies (then default)
        self.client_codes: list[tuple[int, Any]] = []  # scripted /agent/client-code replies
        # route -> fn(request record) -> (status, JSON body) or None for the default answer (tests/telemetry_parity:
        # /ingest/experience verdicts, 5xx and 429 paths)
        self.replies: dict[str, Callable[[dict[str, Any]], tuple[int, Any] | None]] = {}
        self.srv = _serve(self)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self) -> None:
        for s in self.sessions:
            with contextlib.suppress(OSError):
                s.sock.shutdown(socket.SHUT_RDWR)
        self.srv.shutdown()
        self.srv.server_close()

    def event(self, kind: str, **fields: Any) -> None:
        with self.lock:
            self.events.append({"event": kind, **fields})

    # --- what the harness calls ---

    def session(self) -> WsSession | None:
        with self.lock:
            live = [s for s in self.sessions if not s.closed.is_set()]
        return live[-1] if live else None

    def send_command(self, command: dict[str, str]) -> None:
        """Over the live WebSocket if there is one, else queued for the next poll (or hello)."""
        s = self.session()
        if s is None:
            with self.lock:
                self.queue.append(command)
            return
        s.send_text(json.dumps({"type": "command", **command}, separators=(",", ":")))

    def requests_to(self, route: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["route"] == route]

    # --- server side ---

    def handle(self, h: _Handler) -> None:
        path = h.path
        route = f"{h.command} {path.split('?')[0]}"
        auth_ok = h.headers.get("Authorization", "") == f"Bearer {self.token}"
        if route == "GET /agent/ws" and h.headers.get("Upgrade", "").lower() == "websocket":
            return self._upgrade(h, auth_ok)
        raw = h.body()
        ctype = h.headers.get("Content-Type", "")
        rec = {
            "route": route,
            "query": path.partition("?")[2],
            "auth": auth_ok,
            "user_agent": h.headers.get("User-Agent", ""),
            "content_type": ctype,
            "body": decode_body(raw, ctype),
            "t": time.monotonic(),
        }
        with self.lock:
            self.requests.append(rec)
        if not auth_ok:
            return h.reply(401, {"error": "unauthorized"})
        if route in self.fail:
            return h.reply(self.fail[route], {"error": "scripted failure"})
        if route in self.replies:
            scripted = self.replies[route](rec)
            if scripted is not None:
                return h.reply(scripted[0], scripted[1])
        if route == "GET /agent/commands":
            with self.lock:
                commands, self.queue = self.queue[:], []
            return h.reply(200, {"commands": commands})
        if route == "POST /agent/results":
            body = rec["body"] if isinstance(rec["body"], dict) else {}
            for r in body.get("results") or []:
                self._result("poll", r)
            return h.reply(200, {"ok": True, "stored": len(body.get("results") or [])})
        if route == "POST /agent/github-token":
            with self.lock:
                scripted = self.github_tokens.pop(0) if self.github_tokens else None
            if scripted is None:
                scripted = (200, {"token": "ghs_" + "T" * 36, "expires_at": "2099-01-01T00:00:00Z",
                                  "permissions": {"contents": "write", "pull_requests": "write"}})  # fmt: skip
            return h.reply(scripted[0], scripted[1])
        if route == "POST /agent/client-code":
            with self.lock:
                scripted = self.client_codes.pop(0) if self.client_codes else (404, {"error": "off"})
            return h.reply(scripted[0], scripted[1])
        if route.startswith(("POST /ingest/", "PUT /ingest/")):
            return h.reply(200, {"ok": True})
        return h.reply(404, {"error": "not found"})

    def _result(self, channel: str, r: object) -> None:
        if isinstance(r, dict):
            with self.lock:
                self.results[str(r.get("id"))] = r
                self.result_order.append((channel, str(r.get("id"))))

    def _upgrade(self, h: _Handler, auth_ok: bool) -> None:
        info = {
            "auth": auth_ok,
            "user_agent": h.headers.get("User-Agent", ""),
            "version": h.headers.get("Sec-WebSocket-Version", ""),
            "connection_upgrade": "upgrade" in h.headers.get("Connection", "").lower(),
        }
        if not auth_ok or self.ws_mode == "refuse":
            self.event("ws-refused", **info)
            h.close_connection = True
            return h.reply(401 if not auth_ok else 503, {"error": "refused"})
        key = h.headers.get("Sec-WebSocket-Key", "")
        accept = base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()  # noqa: S324
        h.send_response(101)
        h.send_header("Upgrade", "websocket")
        h.send_header("Connection", "Upgrade")
        h.send_header("Sec-WebSocket-Accept", accept)
        h.end_headers()
        h.wfile.flush()
        with self.lock:
            session = WsSession(h, self, len(self.sessions))
            self.sessions.append(session)
        self.event("ws-open", session=session.index, **info)
        session.run()
        self.event("ws-ended", session=session.index, unmasked=session.unmasked)
        h.close_connection = True

    def ws_message(self, s: WsSession, text: str) -> None:
        if text == "ping":  # the Durable Object's auto-response
            self.event("ws-text-ping", session=s.index)
            s.send_text("pong")
            return
        try:
            msg = json.loads(text)
        except ValueError:
            self.event("ws-junk", session=s.index, text=text[:200])
            return
        kind = msg.get("type") if isinstance(msg, dict) else None
        if kind == "hello":
            self.event("ws-hello", session=s.index, message=msg)
            if self.ws_mode == "close":
                s.close(1000)
                return
            with self.lock:
                queued, self.queue = self.queue[:], []
            for c in queued:
                s.send_text(json.dumps({"type": "command", **c}, separators=(",", ":")))
        elif kind == "result":
            self._result("ws", {k: v for k, v in msg.items() if k != "type"})
        elif kind == "console":
            with self.lock:
                self.console.append(msg)
        else:
            self.event("ws-other", session=s.index, message=msg)


# --- GitHub + Mojang ------------------------------------------------------------------------------------------


class FakeGitHub:
    """The config repo's REST endpoints (git refs, contents, pulls) and the Mojang lookups (/mojang/a|b/)."""

    def __init__(self, repo: str, players: str, server_yml: str = "name: parity\n") -> None:
        self.repo = repo
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        self.files: dict[str, dict[str, str]] = {"main": {"players.yml": players, "server.yml": server_yml}}
        self.changed: dict[
            str, set[str]
        ] = {}  # branch -> the files its commits changed (a merge applies those)
        self.prs: dict[int, dict[str, Any]] = {}
        self.unauthorized_once = False  # the next repo call answers 401 (an installation token revoked early)
        self.push: bool | None = True  # the repository's permissions.push
        self.mojang: dict[str, dict[str, str]] = {}  # lower name -> {"id", "name"}
        # what each lookup endpoint answers instead of a lookup (api.mojang.com: 403, as for Cloudflare's egress)
        self.mojang_status: dict[str, int] = {"a": 403, "b": 200}
        self.srv = _serve(self)
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def merge(self, number: int) -> None:
        with self.lock:
            pr = self.prs[number]
            for name in self.changed.get(pr["head"], set()):
                self.files["main"][name] = self.files[pr["head"]][name]
            pr.update(state="closed", merged=True)

    def close(self, number: int) -> None:
        with self.lock:
            self.prs[number].update(state="closed", merged=False)

    def handle(self, h: _Handler) -> None:
        raw = h.body()
        body = json.loads(raw) if raw else None
        path, _, query = h.path.partition("?")
        auth = h.headers.get("Authorization", "")
        rec = {"method": h.command, "path": path, "query": query, "auth": auth, "body": body,
               "user_agent": h.headers.get("User-Agent", ""), "accept": h.headers.get("Accept", ""),
               "api_version": h.headers.get("X-GitHub-Api-Version", "")}  # fmt: skip
        with self.lock:
            self.requests.append(rec)
        if path.startswith("/mojang/"):
            _, _, endpoint, name = path.split("/", 3)
            if self.mojang_status.get(endpoint, 200) != 200:
                return h.reply(self.mojang_status[endpoint], {"error": "refused"})
            hit = self.mojang.get(name.lower())
            return h.reply(200, hit) if hit else h.reply(404, {})
        with self.lock:
            if self.unauthorized_once:
                self.unauthorized_once = False
                return h.reply(401, {"message": "Bad credentials"})
        prefix = f"/repos/{self.repo}"
        if h.command == "GET" and path == prefix:
            perms = {"pull": True} | ({} if self.push is None else {"push": self.push})
            return h.reply(200, {"full_name": self.repo, "permissions": perms})
        if not path.startswith(prefix + "/"):
            return h.reply(404, {"message": "Not Found"})
        rest = path[len(prefix) + 1 :]
        m = h.command
        with self.lock:
            if m == "GET" and rest.startswith("git/ref/heads/"):
                return h.reply(200, {"object": {"sha": "a1b2c3d4" * 5}})
            if m == "GET" and rest.startswith("contents/"):
                name = rest.removeprefix("contents/")
                text = self.files["main"].get(name)
                if text is None:
                    return h.reply(404, {"message": "Not Found"})
                sha = hashlib.sha1(text.encode()).hexdigest()  # noqa: S324 - a blob id
                return h.reply(200, {"sha": sha, "content": base64.b64encode(text.encode()).decode()})
            if m == "POST" and rest == "git/refs":
                self.files[body["ref"].removeprefix("refs/heads/")] = dict(self.files["main"])
                return h.reply(201, {})
            if m == "PUT" and rest.startswith("contents/"):
                name = rest.removeprefix("contents/")
                self.files.setdefault(body["branch"], {})[name] = base64.b64decode(body["content"]).decode()
                self.changed.setdefault(body["branch"], set()).add(name)
                return h.reply(200, {})
            if m == "POST" and rest == "pulls":
                number = len(self.prs) + 1
                self.prs[number] = {"state": "open", "merged": False, **body}
                return h.reply(
                    201, {"number": number, "html_url": f"https://github.com/{self.repo}/pull/{number}"}
                )
            if m == "GET" and rest == "pulls":
                return h.reply(200, [{"number": n} for n, p in self.prs.items() if p["state"] == "open"][:1])
            if m == "GET" and rest.startswith("pulls/"):
                return h.reply(200, self.prs[int(rest.split("/")[1])])
        return h.reply(404, {"message": "Not Found"})


# --- Discord + the watchdog (TLS) -----------------------------------------------------------------------------


def make_cert(directory: Path) -> tuple[Path, Path]:
    """A throw-away CA and a leaf certificate for 127.0.0.1 it signed (openssl), valid for two days. cert.pem is
    the chain (leaf, then CA): the fake server sends it, and the agent trusts it as its CA bundle
    (SSL_CERT_FILE). A leaf, not a self-signed CA certificate, because rustls (the Rust agent) refuses a CA
    certificate used as the server's (webpki `CaUsedAsEndEntity`), as it should; OpenSSL accepts either."""
    cert, key = directory / "cert.pem", directory / "key.pem"
    if not cert.exists():
        ca, ca_key = directory / "ca.pem", directory / "ca-key.pem"
        csr, leaf, ext = directory / "leaf.csr", directory / "leaf.pem", directory / "leaf.ext"
        ec = ["-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes"]

        def ssl(*args: str) -> None:
            subprocess.run(["openssl", *args], check=True, capture_output=True, timeout=60)

        ssl("req", "-x509", *ec, "-keyout", str(ca_key), "-out", str(ca), "-days", "2", "-subj", "/CN=parity CA",
            "-addext", "basicConstraints=critical,CA:TRUE", "-addext", "keyUsage=critical,keyCertSign")  # fmt: skip
        ssl("req", "-new", *ec, "-keyout", str(key), "-out", str(csr), "-subj", "/CN=127.0.0.1")
        ext.write_text(
            "subjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature\nextendedKeyUsage=serverAuth\n"
        )
        ssl("x509", "-req", "-in", str(csr), "-CA", str(ca), "-CAkey", str(ca_key), "-CAcreateserial",
            "-days", "2", "-out", str(leaf), "-extfile", str(ext))  # fmt: skip
        cert.write_bytes(leaf.read_bytes() + ca.read_bytes())
    return cert, key


class FakeHttps:
    """Discord's webhook (POST /api/webhooks/...) and the watchdog (POST /maintenance) on one TLS port."""

    def __init__(self, cert: Path, key: Path, token: str) -> None:
        self.token = token
        self.lock = threading.Lock()
        self.requests: list[dict[str, Any]] = []
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        self.srv = _serve(self, ctx)
        self.url = f"https://127.0.0.1:{self.srv.server_address[1]}"

    def stop(self) -> None:
        self.srv.shutdown()
        self.srv.server_close()

    def handle(self, h: _Handler) -> None:
        raw = h.body()
        rec = {"method": h.command, "path": h.path, "auth": h.headers.get("Authorization", ""),
               "content_type": h.headers.get("Content-Type", ""), "body": decode_body(raw, "")}  # fmt: skip
        with self.lock:
            self.requests.append(rec)
        h.reply(
            204 if h.path.startswith("/api/webhooks/") else 200,
            raw=b"" if h.path.startswith("/api/") else b"{}",
        )

    def to(self, prefix: str) -> list[dict[str, Any]]:
        with self.lock:
            return [r for r in self.requests if r["path"].startswith(prefix)]


# --- RCON -----------------------------------------------------------------------------------------------------


class FakeRcon:
    """Source RCON with scripted replies. Records (connection, command); `whitelist add|remove` edits
    whitelist.json in `server_dir` the way the server does (names resolved through `known`)."""

    def __init__(self, password: str, server_dir: Path) -> None:
        self.password, self.server_dir = password, server_dir
        self.lock = threading.Lock()
        self.log: list[tuple[int, str]] = []
        self.connections = 0
        self.players: list[str] = []
        self.known: dict[str, tuple[str, str]] = {}  # lower name -> (uuid, Mojang's spelling of the name)
        self.replies: dict[str, str] = {}  # exact command -> reply
        self.down = False  # refuse connections (a stopped server)
        self.clients: list[socket.socket] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        for c in list(self.clients):
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
            c.close()

    @staticmethod
    def _packet(rid: int, rtype: int, body: str) -> bytes:
        data = struct.pack("<ii", rid, rtype) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if self.down:
                conn.close()
                continue
            with self.lock:
                self.connections += 1
                cid = self.connections
                self.clients.append(conn)
            threading.Thread(target=self._client, args=(conn, cid), daemon=True).start()

    def _client(self, conn: socket.socket, cid: int) -> None:
        with conn:
            try:
                while True:
                    head = conn.recv(4)
                    if len(head) < 4:
                        return
                    (n,) = struct.unpack("<i", head)
                    data = b""
                    while len(data) < n:
                        chunk = conn.recv(n - len(data))
                        if not chunk:
                            return
                        data += chunk
                    rid, rtype = struct.unpack("<ii", data[:8])
                    body = data[8:-2].decode("utf-8", "replace")
                    if rtype == 3:
                        conn.sendall(self._packet(rid if body == self.password else -1, 2, ""))
                    elif rtype == 2:
                        with self.lock:
                            self.log.append((cid, body))
                        reply = self.reply(body)
                        for i in range(0, max(len(reply), 1), 4096):
                            conn.sendall(self._packet(rid, 0, reply[i : i + 4096]))
                    else:
                        conn.sendall(self._packet(rid, 0, f"Unknown request {rtype:x}"))
            except OSError:
                return

    def reply(self, cmd: str) -> str:
        if cmd in self.replies:
            return self.replies[cmd]
        if cmd == "list":
            n = len(self.players)
            return f"There are {n} of a max of 20 players online: {', '.join(self.players)}"
        if cmd == "tps":
            return "§6TPS from last 1m, 5m, 15m: §a19.98, §a20.0, §a*20.0"
        if cmd == "mspt":
            return (
                "§6Server tick times §e(§7avg§e/§7min§e/§7max§e)§6 from last 5s§7,§6 10s§7,§6 1m§e:\n"
                "§6◴ §a12.1§7/§a9.8§7/§a20.3§7, §a12.4§7/§a9.1§7/§a25.0§7, §a11.9§7/§a8.0§7/§a31.2"
            )
        words = cmd.split()
        if words[:1] == ["whitelist"] and len(words) == 3 and words[1] in ("add", "remove"):
            return self._whitelist(words[1], words[2])
        return f"ran: {cmd}"

    def _whitelist(self, op: str, name: str) -> str:
        path = self.server_dir / "whitelist.json"
        entries = json.loads(path.read_text()) if path.exists() else []
        have = next((e for e in entries if e["name"].lower() == name.lower()), None)
        if op == "add":
            known = self.known.get(name.lower())
            if known is None:
                return "That player does not exist"
            if have:
                return "Player is already whitelisted"
            entries.append({"uuid": known[0], "name": known[1]})  # the server writes Mojang's spelling
            msg = f"Added {known[1]} to the whitelist"
        else:
            if not have:
                return "Player is not whitelisted"
            entries = [e for e in entries if e is not have]
            msg = f"Removed {have['name']} from the whitelist"
        path.write_text(json.dumps(entries, indent=2))
        return msg


# --- journald -------------------------------------------------------------------------------------------------


def parse_native(data: bytes) -> list[tuple[str, str]]:
    """journald's native protocol: KEY=value lines, or KEY\\n<u64 le length><bytes>\\n for binary values."""
    fields: list[tuple[str, str]] = []
    pos = 0
    while pos < len(data):
        nl = data.index(b"\n", pos)
        line = data[pos:nl]
        if b"=" in line:
            k, _, v = line.partition(b"=")
            fields.append((k.decode(), v.decode("utf-8", "replace")))
            pos = nl + 1
        else:
            (n,) = struct.unpack("<Q", data[nl + 1 : nl + 9])
            fields.append((line.decode(), data[nl + 9 : nl + 9 + n].decode("utf-8", "replace")))
            pos = nl + 9 + n + 1
    return fields


class Journal:
    """A datagram socket in journald's place; `entries` are the parsed audit datagrams, in arrival order."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.entries: list[list[tuple[str, str]]] = []
        self.raw: list[bytes] = []
        self.lock = threading.Lock()
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(str(path))
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                ready, _, _ = select.select([self.sock], [], [], 0.05)
                if not ready:
                    continue
                with self.lock:  # received and parsed under the lock: drain() sees nothing half-done
                    data = self.sock.recv(1 << 20)
                    self.raw.append(data)
                    try:
                        self.entries.append(parse_native(data))
                    except (ValueError, struct.error):
                        self.entries.append([("UNPARSEABLE", data.hex())])
            except (OSError, ValueError):
                return

    def drain(self, timeout: float = 5.0) -> None:
        """Wait until every datagram already sent is parsed."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self.lock:
                ready, _, _ = select.select([self.sock], [], [], 0)
                if not ready:
                    return
            time.sleep(0.01)

    def stop(self) -> None:
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()

    def snapshot(self) -> list[list[tuple[str, str]]]:
        with self.lock:
            return list(self.entries)
