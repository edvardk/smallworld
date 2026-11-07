import contextlib
import json
import socket
import struct
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "ansible" / "library"))

from mcctl import galaxy  # noqa: E402

# The controller's (mcctl's) tests that make sense on every OS: ci.yml job `controller` runs them on macOS and
# Windows with -m "controller and not posix_only" (Linux runs the whole suite). Pure logic: schema, render, the
# gate, update PRs, the pin move, the workflow secrets. Not the flows that drive ssh/terraform/ansible/fake
# binaries (init, deploy, doctor, tf, p12, galaxy, ui), and not the box's scripts (fcntl, pwd, grp). A test in
# these files that needs POSIX carries @pytest.mark.posix_only (docs/ci.md "The controller on every OS").
CONTROLLER_TESTS = frozenset(
    {
        "test_configrepo.py",
        "test_environments.py",
        "test_gate.py",
        "test_guard.py",
        "test_home.py",
        "test_mcctl_metrics.py",
        "test_mcctl_providers.py",
        "test_mcctl_actions.py",
        "test_mcctl_render.py",
        "test_mcctl_schema.py",
        "test_panelpr.py",
        "test_pinmove.py",
        "test_release.py",
        "test_updates.py",
        "test_wfsecrets.py",
        "test_yamledit.py",
    }
)


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """On Windows only the controller's test files are collected (the others import the box's POSIX-only
    scripts, and a collection error would stop the run)."""
    if (
        sys.platform == "win32"
        and collection_path.suffix == ".py"
        and collection_path.name.startswith("test_")
    ):
        return collection_path.name not in CONTROLLER_TESTS
    return None


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason="needs POSIX (posix_only)")
    for item in items:
        if item.path.name in CONTROLLER_TESTS:
            item.add_marker(pytest.mark.controller)
        if sys.platform == "win32" and item.get_closest_marker("posix_only"):
            item.add_marker(skip)


class FakeRcon:
    """Source RCON server for tests: one thread per client; `connections` counts the logins attempted."""

    def __init__(self, password: str, handler: Callable[[str], str]) -> None:
        self.password, self.handler = password, handler
        self.commands: list[str] = []
        self.connections = 0
        self.clients: list[socket.socket] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    @staticmethod
    def _packet(rid: int, rtype: int, body: str) -> bytes:
        data = struct.pack("<ii", rid, rtype) + body.encode() + b"\x00\x00"
        return struct.pack("<i", len(data)) + data

    def stop(self) -> None:
        """Stop listening (a blocked accept() keeps a merely closed socket listening) and drop every client."""
        with contextlib.suppress(OSError):
            self.sock.shutdown(socket.SHUT_RDWR)
        self.sock.close()
        self.drop_clients()

    def drop_clients(self) -> None:
        """Close every open client connection (what a server restart does to them)."""
        for c in list(self.clients):
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
            c.close()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            self.connections += 1
            self.clients.append(conn)
            threading.Thread(target=self._client, args=(conn,), daemon=True).start()

    def _client(self, conn: socket.socket) -> None:
        with conn:
            try:
                while True:
                    head = conn.recv(4)
                    if len(head) < 4:
                        break
                    (n,) = struct.unpack("<i", head)
                    data = b""
                    while len(data) < n:
                        chunk = conn.recv(n - len(data))
                        if not chunk:
                            return
                        data += chunk
                    rid, rtype = struct.unpack("<ii", data[:8])
                    body = data[8:-2].decode()
                    if rtype == 3:
                        conn.sendall(self._packet(rid if body == self.password else -1, 2, ""))
                    elif rtype == 2:
                        # Like vanilla: long replies are split into 4096-character packets.
                        self.commands.append(body)
                        reply = self.handler(body)
                        for i in range(0, max(len(reply), 1), 4096):
                            conn.sendall(self._packet(rid, 0, reply[i : i + 4096]))
                    else:
                        conn.sendall(self._packet(rid, 0, f"Unknown request {rtype:x}"))
            except OSError:
                pass


@pytest.fixture
def fake_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., FakeRcon]]:
    import mc_rcon

    servers: list[FakeRcon] = []

    def make(handler: Callable[[str], str], password: str = "pw") -> FakeRcon:
        srv = FakeRcon(password, handler)
        (tmp_path / "server.properties").write_text(
            f"#Minecraft server properties\nenable-rcon=true\nrcon.ip=127.0.0.1\nrcon.port={srv.port}\n"
            f"rcon.password={password}\nlevel-name=world\n"
        )
        monkeypatch.setattr(mc_rcon, "SERVER_DIR", tmp_path)
        servers.append(srv)
        return srv

    yield make
    for s in servers:
        s.stop()


# --- config repos ------------------------------------------------------------------------------------

import yaml  # noqa: E402

EXAMPLE = REPO / "examples" / "config-repo"
TEST_SHA = "1" * 40
# Not placeholders (RFC 2606 / `example-*`), so `render` accepts them; used only by tests.
RENDERABLE_ADMIN = {"name": "Alex", "tailscale_login": "alex@github", "email": "alex@tests.mcctl.internal"}
RENDERABLE = {
    "admin": RENDERABLE_ADMIN,
    "github": {"owner": "mcctl-tests", "config_repo": "tests-mc-config"},
    "dns": {"zone": "tests.mcctl.internal", "hostname": "mc"},
}


def example_server() -> dict:
    """The example config repo's server.yml (placeholder admin)."""
    return yaml.safe_load((EXAMPLE / "server.yml").read_text())


def make_config_repo(dest: Path, server: dict | None = None, *, renderable: bool = False) -> Path:
    """A config repo from template/ pinned to TEST_SHA; `server` replaces server.yml, `renderable`
    swaps the placeholder admin, zone and GitHub names for RENDERABLE."""
    from mcctl import configrepo

    configrepo.materialize(dest, sha=TEST_SHA, version="v0.0.0")
    data = server if server is not None else example_server()
    if renderable:
        data = data | RENDERABLE
    (dest / "server.yml").write_text(yaml.safe_dump(data, sort_keys=False))
    return dest


@pytest.fixture
def config_repo(tmp_path: Path) -> Path:
    """A renderable config repo in a temporary directory."""
    return make_config_repo(tmp_path / "config", renderable=True)


@pytest.fixture(autouse=True)
def _product_is_this_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Commands that run the pinned product (plan/apply/deploy/bootstrap) use this working tree in tests,
    instead of fetching the pinned commit into ~/.cache/mcctl (mcctl/product.py). The cache (the pinned
    Ansible collections, mcctl/galaxy.py) is a temporary directory, never the real ~/.cache."""
    monkeypatch.setenv("MCCTL_PRODUCT_DIR", str(REPO))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path_factory.mktemp("xdg-cache")))
    # v1.3: the "newer release" notice (mcctl/notice.py) never asks GitHub from a test that happens to look like a
    # terminal; tests/test_notice.py switches it back on with a recorded transport.
    monkeypatch.setenv("MCCTL_NO_UPDATE_NOTICE", "1")
    # v1.3: the remembered config repos (mcctl/homecfg.py) live in a fresh directory per test, never ~/.config.
    config_home = tmp_path_factory.mktemp("xdg-config")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("APPDATA", str(config_home))
    # No test reaches Ansible Galaxy: ensure() "installs" the pins with a fake ansible-galaxy.
    monkeypatch.setattr(galaxy, "_find", lambda env: "ansible-galaxy")
    monkeypatch.setattr(galaxy, "_run", fake_galaxy)


def fake_galaxy(argv: Sequence[str], env: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
    """`ansible-galaxy collection install -r REQ -p DIR`: writes each pinned collection's MANIFEST.json."""
    from mcctl import yamlio

    args = list(argv)
    req, dest = Path(args[args.index("-r") + 1]), Path(args[args.index("-p") + 1])
    for item in yamlio.load(req.read_text())["collections"]:
        ns, name = item["name"].split(".")
        info = {"namespace": ns, "name": name, "version": item["version"]}
        coll = dest / "ansible_collections" / ns / name
        coll.mkdir(parents=True)
        (coll / "MANIFEST.json").write_text(json.dumps({"collection_info": info}))
    return subprocess.CompletedProcess(args, 0, "", "")


# --- recorded HTTP --------------------------------------------------------------------------------------

HTTP_FIXTURES = Path(__file__).resolve().parent / "fixtures" / "http"


class Replay:
    """An mcctl.http.Transport answering from fixtures/http/*.json (method + exact URL); anything else fails.
    An exchange with `"once": true` answers one request only (two POSTs to the same URL, for example).

    Records every request so tests can check headers (credentials go only where they belong)."""

    def __init__(self, *names: str) -> None:
        from mcctl import http

        self._http = http
        self.exchanges: list[dict] = []
        for name in names:
            self.exchanges += json.loads((HTTP_FIXTURES / name).read_text())["exchanges"]
        self.requests: list = []

    def __call__(self, req):
        self.requests.append(req)
        for ex in self.exchanges:
            if ex["request"] == {"method": req.method, "url": req.url}:
                if ex.get("once"):  # answered once, then the next exchange for the same request answers
                    self.exchanges.remove(ex)
                resp = ex["response"]
                body = b"" if resp["body"] is None else json.dumps(resp["body"]).encode()
                return self._http.Response(resp["status"], dict(resp["headers"]), body)
        raise AssertionError(f"unexpected request {req.method} {req.url}")

    def urls(self) -> list[str]:
        return [r.url for r in self.requests]
