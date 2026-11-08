"""v1.4: smallworld-rcon (rust/smallworld-rcon) behaves exactly like `scripts/mc_rcon.py`'s command line.

Differential: every case runs both the Python script and the committed Rust binary for this machine
(rust/dist/smallworld-rcon-<target>; SMALLWORLD_RCON_BIN points at another build, e.g. `cargo build`'s) against a
fresh fake RCON server each, and asserts the same exit status, stdout, stderr (for an uncaught Python exception:
the traceback's last line, which is all the binary prints) and the same packets received by the server. The binary
being the source's is scripts/build_rust.py check's job (ci.yml job rust). Plus the workspace rules: no
dependencies, the pins, the glibc error texts the binary embeds.
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import tomllib
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import yaml
from conftest import REPO

SCRIPTS = REPO / "scripts"
RUST = REPO / "rust"
TARGETS = {"x86_64": "x86_64-unknown-linux-musl", "aarch64": "aarch64-unknown-linux-musl"}
GROUP_VARS = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())


def _binary() -> Path | None:
    if os.environ.get("SMALLWORLD_RCON_BIN"):
        return Path(os.environ["SMALLWORLD_RCON_BIN"])
    if sys.platform != "linux" or platform.machine() not in TARGETS:
        return None
    return RUST / "dist" / f"smallworld-rcon-{TARGETS[platform.machine()]}"


BINARY = _binary()
needs_binary = pytest.mark.skipif(
    BINARY is None or not BINARY.is_file(),
    reason="no smallworld-rcon binary for this platform (Linux x86_64/aarch64 only; or set SMALLWORLD_RCON_BIN)",
)

Reply = str | bytes  # bytes: raw packets to send instead of a normal reply


class FakeServer:
    """An RCON server that records every packet it receives (id, type, body). `handler(command) -> reply` answers
    commands; a str is split into 4096-character packets like vanilla, bytes are sent as they are. `chunk` writes
    the replies in pieces of that many bytes (fragmented reads); `login` is "ok", "refuse", "silent" or "empty-first"
    (an empty RESPONSE before the auth result, as some servers send); `silent` never answers commands."""

    def __init__(
        self,
        handler: Callable[[str], Reply],
        password: str = "pw",
        *,
        chunk: int = 0,
        login: str = "ok",
        silent: bool = False,
    ) -> None:
        self.handler, self.password, self.chunk, self.login, self.silent = (
            handler,
            password,
            chunk,
            login,
            silent,
        )
        self.packets: list[list[tuple[int, int, bytes]]] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        self.conns: list[socket.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    @staticmethod
    def packet(rid: int, rtype: int, body: bytes) -> bytes:
        data = struct.pack("<ii", rid, rtype) + body + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    def _send(self, conn: socket.socket, data: bytes) -> None:
        if not self.chunk:
            conn.sendall(data)
            return
        for i in range(0, len(data), self.chunk):
            conn.sendall(data[i : i + self.chunk])
            time.sleep(0.0005)

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.conns.append(conn)
            log: list[tuple[int, int, bytes]] = []
            self.packets.append(log)
            threading.Thread(target=self._client, args=(conn, log), daemon=True).start()

    def _recv(self, conn: socket.socket, n: int) -> bytes:
        data = b""
        while len(data) < n:
            chunk = conn.recv(n - len(data))
            if not chunk:
                raise ConnectionError
            data += chunk
        return data

    def _client(self, conn: socket.socket, log: list[tuple[int, int, bytes]]) -> None:
        with conn:
            try:
                while True:
                    (n,) = struct.unpack("<i", self._recv(conn, 4))
                    data = self._recv(conn, n)
                    rid, rtype = struct.unpack("<ii", data[:8])
                    body = data[8:-2]
                    log.append((rid, rtype, body))
                    if rtype == 3:
                        if self.login == "silent":
                            continue
                        if self.login == "empty-first":
                            self._send(conn, self.packet(rid, 0, b""))
                        ok = body.decode(errors="replace") == self.password and self.login != "refuse"
                        self._send(conn, self.packet(rid if ok else -1, 2, b""))
                    elif rtype == 2:
                        if self.silent:
                            continue
                        reply = self.handler(body.decode(errors="replace"))
                        if isinstance(reply, bytes):
                            if reply == b"CLOSE":
                                return
                            self._send(conn, reply.replace(b"<ID>", struct.pack("<i", rid)))
                            continue
                        out = b"".join(
                            self.packet(rid, 0, reply[i : i + 4096].encode())
                            for i in range(0, max(len(reply), 1), 4096)
                        )
                        self._send(conn, out)
                    else:
                        self._send(conn, self.packet(rid, 0, f"Unknown request {rtype:x}".encode()))
            except (OSError, ConnectionError, struct.error):
                pass

    def stop(self) -> None:
        self.sock.close()
        for c in self.conns:
            try:
                c.close()
            except OSError:
                pass


PROPS = (
    "#Minecraft server properties\nenable-rcon=true\nrcon.ip=127.0.0.1\nrcon.port={port}\nrcon.password=pw\n"
)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Result:
    def __init__(self, rc: int, stdout: str, stderr: str, packets: list) -> None:
        self.rc, self.stdout, self.stderr, self.packets = rc, stdout, stderr, packets

    def __repr__(self) -> str:
        return (
            f"Result(rc={self.rc}, stdout={self.stdout!r}, stderr={self.stderr!r}, packets={self.packets!r})"
        )


def bin_dir(tmp: Path) -> Path:
    """The binary next to the Python helpers it runs (countdown's Discord note: notify.py), as on the box."""
    d = tmp / "bin"
    if not d.is_dir():
        d.mkdir(parents=True, exist_ok=True)
        assert BINARY is not None
        shutil.copy2(BINARY, d / "smallworld-rcon")
        for name in ("notify.py", "alerts.py", "mc_rcon.py"):
            shutil.copy2(SCRIPTS / name, d / name)
            (d / name).chmod(0o755)
    return d


def run_one(
    impl: str,
    tmp: Path,
    args: list[str | bytes],
    *,
    props: str | bytes | None = PROPS,
    server: dict | None = None,
    handler: Callable[[], Callable[[str], Reply]] | None = None,
    env: dict[str, str] | None = None,
    before: Callable[[Path], None] | None = None,
    timeout: float = 60,
) -> Result:
    """One implementation ("py" or "rs") against its own fresh fake server and server directory."""
    work = tmp / impl
    work.mkdir(parents=True)
    sdir = work / "server"
    sdir.mkdir()
    srv = FakeServer(handler() if handler else (lambda c: ""), **(server or {}))
    port = srv.port if server is not None or handler is not None else free_port()
    if props is not None:
        data = (
            props.replace(b"{port}", str(port).encode())
            if isinstance(props, bytes)
            else props.format(port=port)
        )
        (sdir / "server.properties").write_bytes(data if isinstance(data, bytes) else data.encode())
    if before:
        before(sdir)
    full_env = {k: v for k, v in os.environ.items() if k not in ("MAINPID", "DISCORD_WEBHOOK_URL")}
    full_env |= {"MC_SERVER_DIR": str(sdir), "PYTHONDONTWRITEBYTECODE": "1"} | (env or {})
    if impl == "py":
        argv: list[str | bytes] = [sys.executable, str(SCRIPTS / "mc_rcon.py"), *args]
    else:
        argv = [str(bin_dir(tmp) / "smallworld-rcon"), *args]
    try:
        proc = subprocess.run(argv, capture_output=True, env=full_env, timeout=timeout, check=False)
    finally:
        time.sleep(0.05)  # the server thread logs the last packet
        srv.stop()
    out, err = (
        stream.decode(errors="backslashreplace")
        .replace("mc_rcon.py", "smallworld-rcon")
        .replace(str(work), "<work>")
        for stream in (proc.stdout, proc.stderr)
    )
    return Result(proc.returncode, out, err, [p for p in srv.packets])


def compare(tmp: Path, args: list[str | bytes], **kw) -> tuple[Result, Result]:
    """Runs both implementations (in parallel) and asserts they behaved the same."""
    with ThreadPoolExecutor(2) as pool:
        fpy = pool.submit(run_one, "py", tmp, args, **kw)
        frs = pool.submit(run_one, "rs", tmp, args, **kw)
        py, rs = fpy.result(), frs.result()
    assert rs.rc == py.rc, (py, rs)
    assert rs.stdout == py.stdout, (py, rs)
    if py.stderr.startswith("Traceback"):
        # The binary prints the exception line(s) the traceback ends with (a message may hold a newline).
        assert re.match(r"[\w.]+(: |\n)", rs.stderr) and py.stderr.endswith("\n" + rs.stderr), (py, rs)
    elif py.rc == 2 and "error:" in py.stderr:
        # argparse's usage line wraps by terminal width; the error line is what callers read.
        assert rs.stderr.splitlines()[-1] == py.stderr.splitlines()[-1], (py, rs)
    else:
        assert rs.stderr == py.stderr, (py, rs)
    assert rs.packets == py.packets, (py, rs)
    return py, rs


def reply(text: str) -> Callable[[], Callable[[str], Reply]]:
    return lambda: lambda c: text


# --- the workspace rules ---------------------------------------------------------------------------------------


def test_smallworld_rcon_stays_dependency_free() -> None:
    """smallworld-rcon is std only: its one dependency is smallworld-core without features (whose only
    crates.io dependency, serde_json, is optional behind `json`), so the resolved graph of the binary holds no
    crates.io crate. The agent's dependencies are the workspace's (rust/allowed-crates.txt, deny.toml)."""
    ws = tomllib.loads((RUST / "Cargo.toml").read_text())
    assert ws["workspace"]["dependencies"]["smallworld-core"] == {
        "path": "smallworld-core",
        "default-features": False,
    }
    rcon = tomllib.loads((RUST / "smallworld-rcon/Cargo.toml").read_text())
    assert rcon["dependencies"] == {"smallworld-core": {"workspace": True}}
    for table in ("dev-dependencies", "build-dependencies", "target", "features"):
        assert not rcon.get(table), f"smallworld-rcon: [{table}] must stay empty"
    core = tomllib.loads((RUST / "smallworld-core/Cargo.toml").read_text())
    assert core["features"]["default"] == []
    assert all(d.get("optional") for d in core["dependencies"].values()), core["dependencies"]
    for member in ("smallworld-rcon", "smallworld-core"):
        assert (
            tomllib.loads((RUST / member / "Cargo.toml").read_text())["lints"]["rust"]["unsafe_code"]
            == "forbid"
        )
    assert "#![forbid(unsafe_code)]" in (RUST / "smallworld-rcon/src/main.rs").read_text()
    assert "#![forbid(unsafe_code)]" in (RUST / "smallworld-core/src/lib.rs").read_text()
    assert not list(RUST.rglob("build.rs")), "no build scripts of our own"
    if shutil.which("cargo") is None:
        return
    tree = subprocess.run(
        ["cargo", "tree", "--locked", "--offline", "-p", "smallworld-rcon", "-e", "normal,build",
         "--target", "all", "--prefix", "none", "--format", "{p}"],
        cwd=RUST, capture_output=True, text=True, check=False,
    )  # fmt: skip
    if tree.returncode != 0:  # the crates aren't fetched here (lint's pytest); the CI rust job fetches them
        return
    crates = {line.split()[0] for line in tree.stdout.splitlines() if line.strip()}
    assert crates == {"smallworld-rcon", "smallworld-core"}, crates


def test_toolchain_and_release_profile() -> None:
    """The exact toolchain with both musl targets; the reproducible release profile. (The committed binaries vs
    their group_vars pins: tests/test_consistency.py.)"""
    tc = tomllib.loads((RUST / "rust-toolchain.toml").read_text())["toolchain"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", tc["channel"]), "an exact version, never stable/nightly"
    assert set(tc["targets"]) == set(TARGETS.values())
    profile = tomllib.loads((RUST / "Cargo.toml").read_text())["profile"]["release"]
    assert profile["codegen-units"] == 1 and profile["lto"] and profile["panic"] == "abort"
    assert profile["strip"] in ("symbols", True)
    config = tomllib.loads((RUST / ".cargo/config.toml").read_text())
    assert all(config["target"][t]["linker"] == "rust-lld" for t in TARGETS.values())


def test_call_sites_use_the_binary() -> None:
    """Every product call of mc_rcon.py's command line goes through smallworld-rcon now; the base role installs
    the pinned binary for the box's architecture, checked before it is copied."""
    roles = REPO / "ansible/roles"
    unit = (roles / "minecraft/templates/minecraft.service.j2").read_text()
    assert "{{ mc_bin_dir }}/smallworld-rcon stop" in unit
    assert "smallworld-rcon countdown" in (roles / "minecraft/tasks/restart.yml").read_text()
    tasks = (roles / "minecraft/tasks/main.yml").read_text()
    assert "smallworld-rcon worldborder" in tasks and "smallworld-rcon gamerule" in tasks
    base = (roles / "base/tasks/main.yml").read_text()
    assert 'exec sudo -u minecraft {{ mc_bin_dir }}/smallworld-rcon cmd "$@"' in base
    install = base.split("Read the sha256 of the smallworld-rcon binary")[1].split("Install `mc-run` helper")[
        0
    ]
    assert "mc_smallworld_rcon_sha256[base_rcon_arch]" in install and "delegate_to: localhost" in install
    assert "owner: root" in install and 'mode: "0755"' in install
    for path in roles.rglob("*"):
        if path.is_file() and path.suffix in (".yml", ".j2"):
            for line in path.read_text().splitlines():
                code = line.split("#")[0]
                assert not re.search(r"mc_rcon\.py (cmd|stop|countdown|worldborder|gamerule)", code) or (
                    "exec {{ mc_bin_dir }}/mc_rcon.py stop" in code
                ), f"{path}: {line.strip()}"


def execstop_script(bin_dir: Path) -> str:
    unit = (REPO / "ansible/roles/minecraft/templates/minecraft.service.j2").read_text()
    (line,) = [x for x in unit.splitlines() if x.startswith("ExecStop=")]
    m = re.fullmatch(r"ExecStop=/bin/sh -c '(.*)'", line)
    assert m, line
    # systemd turns $$ into $ (and the unit has no other $ or % to expand)
    assert "%" not in m.group(1) and not re.search(r"(?<!\$)\$(?!\$)", m.group(1).replace("$$", ""))
    return m.group(1).replace("$$", "$").replace("{{ mc_bin_dir }}", str(bin_dir))


@pytest.mark.skipif(shutil.which("sh") is None, reason="needs /bin/sh")
@pytest.mark.parametrize(("rust", "status", "fallback"), [
    ("exit 0", 0, False),    # stopped
    ("exit 1", 1, False),    # RCON failed / still running: final, systemd SIGTERMs (no second save-all/stop)
    ("exit 2", 0, True),     # a usage error can only be a broken binary
    ("kill -ABRT $$", 0, True),  # a panic (panic=abort)
    (None, 0, True),         # missing
    ("NOTEXEC", 0, True),    # not executable (e.g. the wrong architecture: exec format error)
])  # fmt: skip
def test_execstop_falls_back_only_when_the_binary_cannot_answer(
    tmp_path: Path, rust: str | None, status: int, fallback: bool
) -> None:
    (tmp_path / "mc_rcon.py").write_text('#!/bin/sh\necho python-stop "$@"\nexit 0\n')
    (tmp_path / "mc_rcon.py").chmod(0o755)
    if rust == "NOTEXEC":
        (tmp_path / "smallworld-rcon").write_bytes(b"\x7fELF garbage")
        (tmp_path / "smallworld-rcon").chmod(0o755)
    elif rust is not None:
        (tmp_path / "smallworld-rcon").write_text(f"#!/bin/sh\n{rust}\n")
        (tmp_path / "smallworld-rcon").chmod(0o755)
    proc = subprocess.run(
        ["/bin/sh", "-c", execstop_script(tmp_path)], capture_output=True, text=True, check=False
    )
    assert proc.returncode == status, proc
    assert ("python-stop stop" in proc.stdout) is fallback, proc
    assert ("falling back to mc_rcon.py stop" in proc.stderr) is fallback, proc


def test_build_script_scrubs_the_environment_and_checks_the_toolchain(tmp_path: Path) -> None:
    sys.path.insert(0, str(SCRIPTS))
    import build_rust

    env = build_rust.build_env(tmp_path)
    assert "--remap-path-prefix=" in env["CARGO_ENCODED_RUSTFLAGS"] and env["SOURCE_DATE_EPOCH"]
    os.environ["RUSTFLAGS"] = "-C opt-level=0"
    try:
        assert "RUSTFLAGS" not in build_rust.build_env(tmp_path)
    finally:
        del os.environ["RUSTFLAGS"]
    assert (
        build_rust.pinned_toolchain()
        == tomllib.loads((RUST / "rust-toolchain.toml").read_text())["toolchain"]["channel"]
    )
    assert set(build_rust.TARGETS) == set(GROUP_VARS["mc_smallworld_rcon_sha256"])


@pytest.mark.skipif(sys.platform != "linux", reason="glibc's strerror texts")
def test_embedded_errno_texts_are_glibcs() -> None:
    """The binary prints Python's `[Errno N] text` with glibc's texts (it links musl, whose texts differ)."""
    src = (RUST / "smallworld-core/src/errno.rs").read_text()
    table = src.split("const GLIBC_STRERROR: [&str; 133] = [")[1].split("];")[0]
    texts = re.findall(r'"([^"]*)"', table)
    assert len(texts) == 133
    if platform.libc_ver()[0] == "glibc":
        assert texts == [os.strerror(i) for i in range(1, 134)]


# --- differential: the same behaviour, case by case --------------------------------------------------------------


@needs_binary
def test_cmd_success_and_formatting(tmp_path: Path) -> None:
    py, _ = compare(
        tmp_path, ["cmd", "list"], handler=reply("§6There are §a3§6 of a max of 20 players online")
    )
    assert py.stdout == "There are 3 of a max of 20 players online\n"
    assert py.packets == [[(1, 3, b"pw"), (2, 2, b"list"), (3, 0, b"")]]


@needs_binary
@pytest.mark.parametrize(
    "words",
    [
        ["say", "hello", "world"],
        ["tp", "@p", "0", "-60", "0"],
        ["say", "é ü ✓ §x"],
        ["a", "--", "--", "b"],
        ["--", "-x"],
    ],
)
def test_cmd_words(tmp_path: Path, words: list[str]) -> None:
    compare(tmp_path, ["cmd", *words], handler=lambda: lambda c: f"echo {c} §")


@needs_binary
def test_multi_packet_and_fragmented_replies(tmp_path: Path) -> None:
    long = "".join(f"line {i} ✓\n" for i in range(1500))  # > 4096 characters: several packets
    py, _ = compare(tmp_path / "a", ["cmd", "big"], handler=reply(long))
    assert py.stdout == long + "\n"
    py, _ = compare(tmp_path / "b", ["cmd", "big"], handler=reply(long), server={"chunk": 7})
    assert py.stdout == long + "\n"
    compare(tmp_path / "c", ["cmd", "x"], handler=reply("tiny"), server={"chunk": 1})


@needs_binary
def test_reply_bytes_edge_cases(tmp_path: Path) -> None:
    # invalid UTF-8 (replaced), a short packet (struct.error), a wrong id, an empty body, a closed connection
    bad_utf8 = FakeServer.packet(0, 0, b"a\xffb\xe2\x82c").replace(struct.pack("<i", 0), b"<ID>", 1)
    compare(tmp_path / "a", ["cmd", "x"], handler=lambda: lambda c: bad_utf8 if c == "x" else "end")
    short = struct.pack("<i", 4) + b"\x01\x00\x00\x00"
    compare(tmp_path / "b", ["cmd", "x"], handler=lambda: lambda c: short)
    compare(tmp_path / "c", ["cmd", "x"], handler=lambda: lambda c: FakeServer.packet(99, 0, b"no"))
    tiny = struct.pack("<i", 9) + b"<ID>\x00\x00\x00\x00\x00"
    compare(tmp_path / "d", ["cmd", "x"], handler=lambda: lambda c: tiny)
    compare(tmp_path / "e", ["cmd", "x"], handler=lambda: lambda c: b"CLOSE")


@needs_binary
@pytest.mark.parametrize("sub", [["cmd", "list"], ["stop"], ["countdown", "5"],
                                 ["worldborder", "--dimension", "d", "--radius", "5"],
                                 ["gamerule", "--name", "x", "--value", "1", "--dimension", "d"]])  # fmt: skip
def test_auth_failure_refused_and_disabled(tmp_path: Path, sub: list[str]) -> None:
    py, _ = compare(tmp_path / "auth", sub, handler=reply(""), server={"login": "refuse"})
    assert py.rc in (0, 1)
    compare(tmp_path / "refused", sub)  # nobody listens on the port
    compare(tmp_path / "disabled", sub, props=PROPS.replace("enable-rcon=true", "enable-rcon=false"))
    compare(tmp_path / "noprops", sub, props=None)


@needs_binary
def test_login_variants(tmp_path: Path) -> None:
    compare(tmp_path / "a", ["cmd", "list"], handler=reply("ok"), server={"login": "empty-first"})
    compare(tmp_path / "b", ["cmd", "list"], handler=reply("ok"), server={"password": "other"})


@needs_binary
def test_timeouts(tmp_path: Path) -> None:
    """The 10 s socket timeout: a login nobody answers, a command nobody answers (both in parallel: ~10 s)."""
    cases = [
        (tmp_path / "login-cmd", ["cmd", "list"], {"login": "silent"}),
        (tmp_path / "login-countdown", ["countdown", "5"], {"login": "silent"}),
        (tmp_path / "command", ["cmd", "list"], {"silent": True}),
        (
            tmp_path / "gamerule",
            ["gamerule", "--name", "x", "--value", "1", "--dimension", "d"],
            {"silent": True},
        ),
    ]
    start = time.monotonic()
    with ThreadPoolExecutor(len(cases)) as pool:
        futures = [pool.submit(compare, d, a, handler=reply(""), server=s) for d, a, s in cases]
        results = [f.result() for f in futures]
    assert 9 < time.monotonic() - start < 30
    assert results[0][0].stderr.endswith("TimeoutError: timed out\n")
    assert results[1][0].stdout == "countdown skipped: timed out\n"


@needs_binary
def test_countdown(tmp_path: Path) -> None:
    online = reply("There are 2 of a max of 20 players online: a, b")
    py, _ = compare(tmp_path / "one", ["countdown", "1"], handler=online)
    assert [p[2] for p in py.packets[0] if p[1] == 2] == [
        b"list",
        b"say Server restarting in 1 seconds for an update.",
        b"say Restarting now.",
    ]
    compare(tmp_path / "zero", ["countdown", "0"], handler=online)
    compare(tmp_path / "negative", ["countdown", "-1"], handler=online)  # ValueError from time.sleep
    compare(tmp_path / "huge", ["countdown", "99999999999"], handler=online)  # OverflowError from time.sleep
    py, _ = compare(
        tmp_path / "empty", ["countdown", "60"], handler=reply("There are 0 of a max of 20 players")
    )
    assert py.stdout == "no players online; skipping countdown\n"
    compare(tmp_path / "unparsed", ["countdown", "60"], handler=reply("§cUnknown command"))


@needs_binary
def test_countdown_discord_note_goes_through_notify(tmp_path: Path) -> None:
    """With a webhook configured both post the same note through notify.py (here an http:// URL, which notify
    refuses without sending anything: both print its `discord notify failed: ValueError`)."""
    online = reply("There are 1 of a max of 20 players online: a")
    py, _ = compare(
        tmp_path, ["countdown", "1"], handler=online, env={"DISCORD_WEBHOOK_URL": "http://127.0.0.1:9/x"}
    )
    assert py.stderr == "discord notify failed: ValueError\n"


def border_handler(*, broken: bool = False) -> Callable[[], Callable[[str], Reply]]:
    def make() -> Callable[[str], Reply]:
        state = {"size": 59999968.0}

        def handler(cmd: str) -> str:
            if cmd.endswith("worldborder get"):
                return f"The world border is currently {state['size']:.0f} block(s) wide"
            if " worldborder set " in cmd and not broken:
                state["size"] = float(cmd.rsplit(" ", 1)[1])
            return ""

        return handler

    return make


@needs_binary
def test_worldborder_idempotency(tmp_path: Path) -> None:
    args = ["worldborder", "--dimension", "minecraft:overworld", "--radius", "5000"]
    shared = border_handler()  # one state for both runs of each implementation
    for impl in ("py", "rs"):
        handler = shared()
        first = run_one(impl, tmp_path / f"{impl}1", args, handler=lambda h=handler: h)
        second = run_one(impl, tmp_path / f"{impl}2", args, handler=lambda h=handler: h)
        assert (first.stdout, second.stdout, first.rc, second.rc) == ("CHANGED\n", "OK\n", 0, 0), (
            first,
            second,
        )
        if impl == "py":
            want = (first.packets, second.packets)
        else:
            assert (first.packets, second.packets) == want
    compare(tmp_path / "broken", args, handler=border_handler(broken=True))
    compare(tmp_path / "half", ["worldborder", "--dimension", "d", "--radius", "5000"],
            handler=reply("The world border is currently 10000.5 block(s) wide"))  # fmt: skip
    compare(
        tmp_path / "negative", ["worldborder", "--dimension", "d", "--radius", "-3"], handler=border_handler()
    )


def gamerule_handler(start: str = "false") -> Callable[[], Callable[[str], Reply]]:
    def make() -> Callable[[str], Reply]:
        state: dict[str, str] = {}

        def handler(cmd: str) -> str:
            m = re.fullmatch(r"execute in (\S+) run gamerule (\w+)(?: (\S+))?", cmd)
            if not m:
                return "?"
            dim, rule, value = m.groups()
            if dim == "minecraft:the_end":
                return f"Unknown dimension '{dim}'"
            if value is None:
                return f"Gamerule {rule} is currently set to: {state.get(dim, start)}"
            state[dim] = value
            return f"Gamerule {rule} is now set to: {value}"

        return handler

    return make


@needs_binary
def test_gamerule_idempotency(tmp_path: Path) -> None:
    dims = ["--dimension", "minecraft:overworld", "--dimension", "minecraft:the_nether", "--dimension",
            "minecraft:the_end"]  # fmt: skip
    args = ["gamerule", "--name", "locator_bar", "--value", "true", *dims]
    for impl in ("py", "rs"):
        handler = gamerule_handler()()
        first = run_one(impl, tmp_path / f"{impl}1", args, handler=lambda h=handler: h)
        second = run_one(impl, tmp_path / f"{impl}2", args, handler=lambda h=handler: h)
        assert (first.stdout, second.stdout) == ("CHANGED\n", "OK\n"), (first, second)
        if impl == "py":
            want = (first.packets, second.packets)
        else:
            assert (first.packets, second.packets) == want
    compare(tmp_path / "same", args, handler=gamerule_handler("true"))
    compare(tmp_path / "unexpected", args, handler=reply("§cIncorrect argument for command " + "x" * 300))
    compare(tmp_path / "stuck", args, handler=reply("Gamerule locator_bar is currently set to: false"))
    for bad in (["Bad", "true"], ["ok", "yes"], ["ok", "1234567890"], ["ok\n", "true"], ["ok", "-5"]):
        compare(tmp_path / f"bad-{bad[0].strip()}-{bad[1]}", ["gamerule", "--name", bad[0], "--value", bad[1],
                *dims], handler=gamerule_handler())  # fmt: skip


@needs_binary
def test_stop(tmp_path: Path) -> None:
    """save-all flush, stop (one packet), then wait for $MAINPID: here a process the fake server ends on `stop`."""
    procs: dict[str, subprocess.Popen] = {}

    def run_stop(impl: str) -> Result:
        proc = subprocess.Popen(["sleep", "60"])
        procs[impl] = proc
        threading.Thread(target=proc.wait, daemon=True).start()  # reaps it: a zombie would still "exist"

        def make() -> Callable[[str], Reply]:
            def handler(cmd: str) -> str:
                if cmd == "stop":
                    threading.Timer(0.7, proc.terminate).start()
                    return "Stopping the server"
                return "Saved the game"

            return handler

        return run_one(impl, tmp_path / "wait", ["stop"], handler=make, env={"MAINPID": str(proc.pid)})

    (tmp_path / "wait").mkdir()
    py, rs = run_stop("py"), run_stop("rs")
    assert (py.rc, py.stdout, py.stderr) == (0, "", "")
    assert (rs.rc, rs.stdout, rs.stderr, rs.packets) == (py.rc, py.stdout, py.stderr, py.packets)
    assert [p[2] for p in py.packets[0]] == [b"pw", b"save-all flush", b"", b"stop"]
    saved = reply("Saved the game")
    compare(tmp_path / "nopid", ["stop"], handler=saved)
    compare(tmp_path / "closed", ["stop"], handler=lambda: lambda c: b"CLOSE" if c == "stop" else "Saved")
    compare(tmp_path / "badpid", ["stop"], handler=saved, env={"MAINPID": "abc"})
    compare(tmp_path / "hugepid", ["stop"], handler=saved, env={"MAINPID": "99999999999"})
    compare(tmp_path / "emptypid", ["stop"], handler=saved, env={"MAINPID": ""})


# server.properties variants, all run through `cmd list` against a server whose password is "pass=w:rd é".
PROPS_CASES = {
    "escapes": "enable-rcon=true\nrcon.port={port}\nrcon.password=pa\\u0073s\\=w\\:rd \\u00e9\n",
    "spaces-colons": "  enable-rcon : true  \r\nrcon.port {port}\n\trcon.password  =  pass=w:rd é\n",
    "comments": "# x\n! y\n  #enable-rcon=false\nenable-rcon=true\nrcon.port={port}\nrcon.password=pass=w:rd é\n",
    "crlf-ff": "enable-rcon=true\r\nrcon.port={port}\x0crcon.password=pass=w:rd é\r\n",
    "later-wins": "enable-rcon=false\nenable-rcon=true\nrcon.port=1\nrcon.port={port}\nrcon.password=pass=w:rd é\n",
    "unicode-space": "enable-rcon=true\nrcon.port={port}\u2003\nrcon.password\u00a0pass=w:rd é\u3000\n",
    "empty-ip": "enable-rcon=true\nrcon.ip=\nrcon.port={port}\nrcon.password=pass=w:rd é\n",
    "port-underscore": "enable-rcon=true\nrcon.port=+{port}\nrcon.password=pass=w:rd é\n",
    "no-password": "enable-rcon=true\nrcon.port={port}\n",
    "bad-port": "enable-rcon=true\nrcon.port=25_575x\nrcon.password=pass=w:rd é\n",
    "negative-port": "enable-rcon=true\nrcon.port=-5\nrcon.password=pass=w:rd é\n",
    "bad-escape": "enable-rcon=true\nrcon.port={port}\nrcon.password=\\uZZZZ\n",
    "surrogate-password": "enable-rcon=true\nrcon.port={port}\nrcon.password=x\\uD83D\\uDE00\n",
    "surrogate-elsewhere": "motd=\\uD83D\\uDE00\nenable-rcon=true\nrcon.port={port}\nrcon.password=pass=w:rd é\n",
    "enable-True": "enable-rcon=True\nrcon.port={port}\nrcon.password=pass=w:rd é\n",
    "default-port": "enable-rcon=true\nrcon.password=pass=w:rd é\n",
}


@needs_binary
@pytest.mark.parametrize("case", sorted(PROPS_CASES))
def test_properties_parsing(tmp_path: Path, case: str) -> None:
    compare(tmp_path, ["cmd", "list"], props=PROPS_CASES[case], handler=reply("ok"),
            server={"password": "pass=w:rd é"})  # fmt: skip


@needs_binary
def test_properties_file_edge_cases(tmp_path: Path) -> None:
    ok = {"handler": reply("ok")}
    compare(
        tmp_path / "utf8", ["cmd", "list"], props=b"enable-rcon=true\nrcon.port={port}\nmotd=\xff\n", **ok
    )
    compare(tmp_path / "trunc", ["cmd", "list"], props=b"enable-rcon=true\nmotd=\xe2\x82", **ok)

    def symlink(sdir: Path) -> None:
        (sdir / "real.properties").write_text(PROPS.format(port=1))
        (sdir / "server.properties").symlink_to(sdir / "real.properties")

    compare(tmp_path / "symlink", ["cmd", "list"], props=None, before=symlink, **ok)
    compare(
        tmp_path / "dir",
        ["cmd", "list"],
        props=None,
        before=lambda d: (d / "server.properties").mkdir(),
        **ok,
    )
    compare(
        tmp_path / "fifo", ["stop"], props=None, before=lambda d: os.mkfifo(d / "server.properties"), **ok
    )


@needs_binary
def test_server_dir_variants(tmp_path: Path) -> None:
    for i, value in enumerate(["/nonexistent//x/", "", "rel/./dir"]):
        compare(tmp_path / str(i), ["cmd", "list"], props=None, env={"MC_SERVER_DIR": value})


@needs_binary
def test_non_utf8_arguments(tmp_path: Path) -> None:
    """Python decodes them to lone surrogates and fails only when sending (after the login)."""
    compare(tmp_path / "cmd", ["cmd", "say", b"caf\xe9"], handler=reply("ok"))
    compare(
        tmp_path / "rule",
        ["gamerule", "--name", b"\xff", "--value", "1", "--dimension", "d"],
        handler=reply(""),
    )
    compare(
        tmp_path / "dim",
        ["worldborder", "--dimension", b"\xff\xfe", "--radius", "1"],
        handler=border_handler(),
    )


ARGPARSE_CASES = [
    "", "x", "cmd", "countdown", "countdown abc", "countdown 5 6", "worldborder", "worldborder --dimension",
    "worldborder --dimension a --radius x", "worldborder --dim a --rad 5 extra", "gamerule --name a",
    "gamerule --name a --value b", "cmd -x", "cmd list -x", "-x cmd list", "stop extra", "countdown -x",
    "cmd -hello", "cmd --help=x", "cmd -h=x", "cmd say -hello", "worldborder --dimension -- x --radius 5",
    "worldborder --dimension -x --radius 5", "worldborder --dimension -5 --radius 5", "countdown -- 5",
    "countdown 5 --", "cmd a -- -- b", "--cmd list", "cmd --list", "cmd -", "cmd -1x", "cmd -1.5", "cmd -.5",
    "cmd -5.", "worldborder --dimension=a=b --radius=1", "worldborder --radius 1 --dimension", "countdown 5 --x",
    "cmd -hx", "worldborder -h", "worldborder --dimension x --radius 1 -x", "worldborder --dimension x --radius -1",
    "stop -h", "stop --", "stop -- --", "countdown -- -- 5", "worldborder -- --dimension x",
    "worldborder --dimension x --radius 1 --", "worldborder --dimension x -- --radius 1", "cmd --he",
    "gamerule --name a --value b --dimension=", "cmd list -- -h", "-h cmd", "cmd list -h", "countdown -h 5",
    "countdown x -h", "countdown 5 -x", "cmd a -x b", "-x cmd a -y", "worldborder --dimension x --radius=",
    "cmd -x -h", "cmd -hh", "stop -hfoo", "cmd --=x", "worldborder --=x", "cmd ---x", "countdown 0x5",
    "countdown +5", "countdown 5_0", "countdown 1_", "worldborder --dimension d --radius 007", "cmd -=x",
    "worldborder -=x", "countdown -5\n", "gamerule --nam a --val 1 --dim d --dim e", "cmd a\nb",
]  # fmt: skip
# argparse changed in 3.13: a `--` before the subcommand is dropped (the box runs Python 3.13).
ARGPARSE_313_CASES = ["-- cmd list", "-- -- cmd list"]


@needs_binary
def test_argparse_behaviour(tmp_path: Path) -> None:
    """Every command line ends the same way: the parsed command reaches the (disabled) server identically, or the
    same `error:` line and status 2, or help (status 0)."""
    cases = ARGPARSE_CASES + (ARGPARSE_313_CASES if sys.version_info >= (3, 13) else [])
    props = PROPS.replace("enable-rcon=true", "enable-rcon=false")

    def one(i: int, line: str) -> None:
        args = line.split(" ") if line else []
        py = run_one("py", tmp_path / f"{i}", args, props=props)
        rs = run_one("rs", tmp_path / f"{i}", args, props=props)
        assert rs.rc == py.rc, (line, py, rs)
        if py.rc == 0 and py.stdout.startswith("usage:"):
            assert rs.stdout.startswith("usage:"), (line, rs)  # help: argparse-like, not byte for byte
        elif py.rc == 2:
            assert rs.stderr.splitlines()[-1] == py.stderr.splitlines()[-1], (line, py, rs)
        else:
            assert (rs.stdout, rs.stderr.splitlines()[-1:]) == (py.stdout, py.stderr.splitlines()[-1:]), (
                line,
                py,
                rs,
            )

    with ThreadPoolExecutor(8) as pool:
        for f in [pool.submit(one, i, line) for i, line in enumerate(cases)]:
            f.result()


@needs_binary
def test_argparse_parsed_values_reach_the_server(tmp_path: Path) -> None:
    for i, args in enumerate(
        [
            ["cmd", "list", "--", "-h"],
            ["cmd", "--", "--", ""],
            ["gamerule", "--dimension", "a", "--name", "b", "--value", "1", "--dimension", "c"],
        ]
    ):
        compare(tmp_path / str(i), args, handler=gamerule_handler())  # fmt: skip
