"""P8 acceptance inside the e2e-mock container (run as root after site.yml and hardening_check.py; stdlib only).

mc-agent was installed by site.yml with INGEST_URL=http://127.0.0.1:8787 (tests/e2e/vars.yml) and has been
failing to reach it since: this script now starts a mock of the Worker there and checks that the agent
  - runs as mcops in its sandbox, and a process running as minecraft can't read its environment;
  - pushes metric samples with live values (TPS, players, heap), including the ones it buffered on disk while
    the Worker was unreachable (older than the mock);
  - ships the journal: a gzipped NDJSON archive and D1 rows of minecraft.service / mc-* units;
  - falls back to polling when the WebSocket can't be held, verifies signed commands, runs `status` and an allowed
    console command, refuses a denied console command and a replayed command, posts the results, and writes
    every command to the local journal (MC_AUDIT=1);
  - (P8 review) stops and starts the server through signed `stop`/`start` commands: the admin-stopped marker is
    written (the unit may write the markers directory) and the server stops, then the marker is removed and the
    server comes back.
  - P10, every panel action end to end through signed commands (a mock of the GitHub API on 127.0.0.1:8788 stands
    in for the config repo):
      whitelist add -> live in whitelist.json -> pending.json -> a deploy (`site.yml --tags players`) keeps it
      although Git doesn't have it yet -> a pull request (branch panel/*, canonical players.yml) -> merged -> the
      entry leaves pending.json; then the same for a remove;
      restart with a 10 s countdown (job reports: countdown -> running -> done, the JVM really restarted);
      plugin disable -> the jar moves to plugins/disabled/ (mc-plugins-apply.service, as minecraft) -> the server
      restarts without it -> enable brings it back;
      backup now -> restic's snapshot list for the panel;
      restore round trip: a snapshot, a change to the world, restore of that snapshot through
      mc-restore@<id>.service -> the change is gone, .restored-from names it, and a pre-restore safety snapshot
      holding the change exists;
      (P10 review) a deploy during a restore (restore.lock held, marker by=restore) waits, then fails without
      starting the server; with only the marker left it deploys and leaves the server down.
  - v1.2 world library: world_create (the active world archived first, a seeded void world generated, the seed
    checked over RCON), a deploy honouring the pending switch, the server.yml pull request, world_switch back (the
    old world's files return), world_delete, and the refusals (delete the active world, switch to an unknown one).
  - v1.1, /modpack's code hand-off: a request written as minecraft (what mc-brand writes) into the tmpfs hand-off
    directory -> the agent asks the Worker (POST /agent/client-code, bearer token) -> the code file in the verdict
    directory, which minecraft can read but not write; the code never reaches the journal.

    MC_SECRET_AGENT_TOKEN=... MC_SECRET_COMMAND_HMAC_KEY=... [all MC_SECRET_* for the players role run] \
      python3 tests/e2e/agent_check.py
"""

from __future__ import annotations

import gzip
import hashlib
import hmac
import http.server
import json
import os
import pwd
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, ClassVar

from e2e_common import timers_paused

TOKEN = os.environ["MC_SECRET_AGENT_TOKEN"]
KEY = os.environ["MC_SECRET_COMMAND_HMAC_KEY"].encode()
STARTED_MS = int(time.time() * 1000)
seen: dict[str, list[Any]] = {"metrics": [], "logs": [], "archive": [], "results": [], "ws": [], "unauth": [],
                              "inventory": [], "job": [], "github_token": [], "client_code": []}  # fmt: skip
INSTALLATION_TOKEN = "e2e-installation-token-not-a-secret"  # what the mock Worker hands out
CLIENT_CODE = "424242"  # v1.1: the mock Worker's one-time code
served_commands = threading.Event()
queued: list[dict[str, str]] = []  # later phases: commands the main thread hands to the next poll
queue_lock = threading.Lock()
failures: list[str] = []


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        failures.append(what)


def command(action: str, args: dict[str, Any], cid: str) -> dict[str, str]:
    cmd = {"id": cid, "action": action, "args": args, "login": "e2e@tests", "iat": int(time.time() * 1000),
           "nonce": secrets.token_hex(16)}  # fmt: skip
    payload = json.dumps(cmd)
    return {"payload": payload, "sig": hmac.new(KEY, payload.encode(), hashlib.sha256).hexdigest()}


class Mock(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a: object) -> None:
        pass

    def reply(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def authorized(self) -> bool:
        if self.headers.get("Authorization") == f"Bearer {TOKEN}":
            return True
        seen["unauth"].append(self.path)
        self.reply(401, {"error": "unauthorized"})
        return False

    def do_GET(self) -> None:
        if not self.authorized():
            return
        if self.path == "/agent/ws":
            seen["ws"].append(self.headers.get("Upgrade"))
            self.reply(404, {"error": "no WebSocket in the mock: use the poll fallback"})
            return
        if self.path == "/agent/commands":
            cmds: list[dict[str, str]] = []
            if not served_commands.is_set():
                served_commands.set()
                status = command("status", {}, "e2e-status")
                cmds = [status, command("console", {"command": "list"}, "e2e-list"),
                        command("console", {"command": "/minecraft:stop"}, "e2e-denied"), status]  # fmt: skip
            with queue_lock:
                cmds += queued
                queued.clear()
            self.reply(200, {"commands": cmds})
            return
        self.reply(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self.authorized():
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        if self.path == "/ingest/metrics":
            seen["metrics"] += json.loads(body)["samples"]
        elif self.path == "/ingest/logs":
            seen["logs"] += json.loads(body)["rows"]
        elif self.path.startswith("/ingest/archive?batch="):
            seen["archive"].append([json.loads(line) for line in gzip.decompress(body).decode().splitlines()])
        elif self.path == "/ingest/inventory":
            seen["inventory"].append(json.loads(body)["inventory"])
        elif self.path == "/agent/results":
            seen["results"] += json.loads(body)["results"]
        elif self.path == "/ingest/job":
            seen["job"].append(json.loads(body)["job"])
        elif self.path == "/agent/client-code":  # v1.1: /modpack's one-time code
            seen["client_code"].append(json.loads(body))
            self.reply(200, {"code": CLIENT_CODE, "expires_ms": int(time.time() * 1000) + 900000})
            return
        elif self.path == "/agent/github-token":  # P10 review: the Worker mints the panel App's tokens
            seen["github_token"].append(1)
            self.reply(200, {"token": INSTALLATION_TOKEN, "expires_at": "2099-01-01T00:00:00Z"})
            return
        else:
            self.reply(404, {"error": "not found"})
            return
        self.reply(200, {"ok": True})


def as_minecraft(action: Any) -> int:
    pw = pwd.getpwnam("minecraft")
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.setgroups(os.getgrouplist("minecraft", pw.pw_gid))
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            os.umask(0o027)  # minecraft.service's UMask
            action()
            code = 0
        except PermissionError:
            code = 13
        except BaseException:
            code = 1
        os._exit(code)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])


def client_code_round() -> None:
    """v1.1: the plugin's request -> the agent -> the (mock) Worker -> the code file the plugin reads."""
    req_dir, codes = Path("/run/mc-telemetry/client"), Path("/run/mc-verdicts/client-codes.tsv")
    uuid, nonce = "0f8fad5b-d9cb-469f-a165-70867728950e", secrets.token_hex(16)

    def write_request() -> None:
        req_dir.mkdir(exist_ok=True)
        (req_dir / f"{uuid}.req").write_text(f"mc-client-request 1\n{uuid}\tSteve\t{nonce}\t1\n")

    expect(as_minecraft(write_request) == 0, "minecraft writes /modpack's request into its tmpfs hand-off")
    line = f"C\t{uuid}\t{nonce}\t{CLIENT_CODE}\t"
    got = wait_for("the code file", lambda: codes.is_file() and line in codes.read_text(), timeout=60, step=1)
    expect(bool(got), f"mc-agent answered the request in {codes}")
    expect(
        seen["client_code"] == [{"uuid": uuid, "name": "Steve"}], f"one code request ({seen['client_code']})"
    )
    expect(as_minecraft(codes.read_text) == 0, "minecraft can read the code file")
    expect(as_minecraft(lambda: codes.write_text("x")) == 13, "minecraft can't write the code file")
    st = codes.stat()
    expect(pwd.getpwuid(st.st_uid).pw_name == "mcops" and oct(st.st_mode & 0o777) == "0o640",
           f"the code file is mcops's, 0640 ({oct(st.st_mode)})")  # fmt: skip
    journal = subprocess.run(["journalctl", "_SYSTEMD_UNIT=mc-agent.service", "--no-pager", "-o", "cat"],
                             capture_output=True, text=True).stdout  # fmt: skip
    expect(CLIENT_CODE not in journal, "the code never reaches the journal")
    expect(as_minecraft(lambda: (req_dir / f"{uuid}.req").unlink()) == 0, "the plugin removes its request")


def as_minecraft_read(path: str) -> int:
    pw = pwd.getpwnam("minecraft")
    pid = os.fork()
    if pid == 0:
        code = 1
        try:
            os.setgroups(os.getgrouplist("minecraft", pw.pw_gid))
            os.setgid(pw.pw_gid)
            os.setuid(pw.pw_uid)
            Path(path).read_bytes()
            code = 0
        except PermissionError:
            code = 13
        except BaseException:  # reported as "other failure"
            code = 1
        os._exit(code)
    return os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])


def show(unit: str, *props: str) -> dict[str, str]:
    out = subprocess.run(
        ["systemctl", "show", unit, "-p", ",".join(props)], capture_output=True, text=True
    ).stdout
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def result_of(cid: str, timeout: float = 120) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for r in seen["results"]:
            if r.get("id") == cid:
                return r
        time.sleep(1)
    return {}


def server_answers(timeout: float = 300) -> bool:
    import socket

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", 25565), timeout=5):
                return True
        except OSError:
            time.sleep(3)
    return False


def rcon_client_sockets(port: int = 25575) -> set[str]:
    """Inodes of the established TCP connections to the RCON port (the clients' side), from /proc/net/tcp."""
    out = set()
    for line in Path("/proc/net/tcp").read_text().splitlines()[1:]:
        f = line.split()
        if f[3] == "01" and int(f[2].rsplit(":", 1)[1], 16) == port:
            out.add(f[9])
    return out


def rcon_client_lines() -> int:
    """'Thread RCON Client' lines of this run of the server (its invocation's journal: nothing older)."""
    inv = show("minecraft.service", "InvocationID")["InvocationID"]
    journal = subprocess.run(["journalctl", f"_SYSTEMD_INVOCATION_ID={inv}", "-o", "cat", "--no-pager"],
                             capture_output=True, text=True).stdout  # fmt: skip
    return sum("Thread RCON Client" in ln for ln in journal.splitlines())


def rcon_quiet_round() -> None:
    """v1.0.1: mc-agent keeps one RCON connection (the same socket over several metric rounds, 30 s each here)
    and the server's log has no per-connection "Thread RCON Client ... started/shutting down" lines (log4j2
    filters); the RCON listener's own start line is still there."""
    before = rcon_client_lines()
    first = rcon_client_sockets()
    time.sleep(95)  # three metric rounds
    later = rcon_client_sockets()
    expect(len(first) == 1 and first == later,
           f"mc-agent keeps one RCON connection across metric rounds ({first} -> {later})")  # fmt: skip
    rcon("list")  # one connection of our own: filtered as well
    time.sleep(1)
    after = rcon_client_lines()
    expect(before == after == 0, f"no per-connection RCON lines in the server log ({before} -> {after})")
    latest = Path("/opt/minecraft/server/logs/latest.log").read_text(errors="replace")
    expect("Thread RCON Client" not in latest and "RCON running on" in latest,
           "latest.log: RCON started, no per-connection lines (the rest of the logging is unchanged)")  # fmt: skip


def stop_start_round() -> None:
    """Signed stop then start through the poll fallback (what the panel's buttons send in P10)."""
    marker = Path("/var/lib/minecraft/admin-stopped")
    with queue_lock:
        queued.append(command("stop", {"reason": "e2e stop via panel"}, "e2e-stop"))
    r = result_of("e2e-stop")
    expect(r.get("ok") is True, f"signed stop ran ({r})")
    expect(
        marker.exists() and "e2e stop via panel" in marker.read_text(),
        "the agent wrote the admin-stopped marker",
    )
    expect(show("minecraft.service", "ActiveState")["ActiveState"] == "inactive", "the server stopped")
    seed_usercache()  # P10: while it's stopped (it rewrites the file on shutdown)
    with queue_lock:
        queued.append(command("start", {}, "e2e-start"))
    r = result_of("e2e-start")
    expect(r.get("ok") is True, f"signed start ran ({r})")
    expect(not marker.exists(), "the agent removed the admin-stopped marker")
    state = show("minecraft.service", "ActiveState")["ActiveState"]
    expect(state in ("active", "activating"), f"the server is starting again ({state})")
    expect(server_answers(), "the restarted server accepts connections")


# --- P10 ---------------------------------------------------------------------------------------------------------

SERVER = Path("/opt/minecraft/server")
PANEL_NAME, PANEL_UUID = "E2EPanelPal", "4e2e0000-0000-4000-8000-00000000a11e"


def seed_usercache() -> None:
    """A made-up player in the server's profile cache, so `whitelist add` resolves without Mojang (the cache is
    read at start; online lookups stay the fallback)."""
    cache = SERVER / "usercache.json"
    try:
        entries = json.loads(cache.read_text())
    except (OSError, ValueError):
        entries = []
    entries = [e for e in entries if e.get("name") != PANEL_NAME]
    entries.append({"name": PANEL_NAME, "uuid": PANEL_UUID, "expiresOn": "2099-01-01 00:00:00 +0000"})
    cache.write_text(json.dumps(entries))
    pw = pwd.getpwnam("minecraft")
    os.chown(cache, pw.pw_uid, pw.pw_gid)


def canonical(whitelist: list[dict[str, str]], ops: list[dict[str, Any]]) -> str:
    """players.yml as mcctl writes it (the agent must keep this form)."""
    lines = []
    for section, items, keys in (
        ("whitelist", whitelist, ("name", "uuid")),
        ("ops", ops, ("name", "uuid", "level")),
    ):
        if not items:
            lines.append(f"{section}: []")
            continue
        lines.append(f"{section}:")
        for it in sorted(items, key=lambda x: (x["name"].lower(), x["uuid"])):
            lines.append("  - {" + ", ".join(f"{k}: {json.dumps(it[k])}" for k in keys) + "}")
    return "\n".join(lines) + "\n"


class GitHubMock(http.server.BaseHTTPRequestHandler):
    """The few GitHub REST endpoints the agent's whitelist flow uses, for one config repo."""

    files: ClassVar[dict[str, str]] = {}
    server: ClassVar[dict[str, str]] = {"main": "name: e2e\n"}  # v1.2: server.yml per branch (world PRs)
    prs: ClassVar[dict[int, dict[str, Any]]] = {}
    tokens: ClassVar[set[str]] = set()

    def log_message(self, *a: object) -> None:
        pass

    def reply(self, code: int, body: object) -> None:
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def handle_any(self) -> None:
        import base64

        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n)) if n else None
        GitHubMock.tokens.add(self.headers.get("Authorization", ""))
        path, m = self.path.split("?")[0], self.command
        if not path.startswith(f"/repos/{CONFIG_REPO}/"):
            return self.reply(404, {"message": "Not Found"})
        rest = path[len(f"/repos/{CONFIG_REPO}/") :]
        if m == "GET" and rest == "git/ref/heads/main":
            return self.reply(200, {"object": {"sha": "e2e0" * 10}})
        if m == "GET" and rest == "contents/players.yml":
            text = GitHubMock.files["main"]
            return self.reply(200, {"sha": "blob", "content": base64.b64encode(text.encode()).decode()})
        if m == "GET" and rest == "contents/server.yml":
            text = GitHubMock.server["main"]
            return self.reply(200, {"sha": "blob", "content": base64.b64encode(text.encode()).decode()})
        if m == "POST" and rest == "git/refs":
            GitHubMock.files[body["ref"].removeprefix("refs/heads/")] = GitHubMock.files["main"]
            GitHubMock.server[body["ref"].removeprefix("refs/heads/")] = GitHubMock.server["main"]
            return self.reply(201, {})
        if m == "PUT" and rest == "contents/server.yml":
            GitHubMock.server[body["branch"]] = base64.b64decode(body["content"]).decode()
            return self.reply(200, {})
        if m == "PUT" and rest == "contents/players.yml":
            GitHubMock.files[body["branch"]] = base64.b64decode(body["content"]).decode()
            return self.reply(200, {})
        if m == "POST" and rest == "pulls":
            number = len(GitHubMock.prs) + 1
            GitHubMock.prs[number] = {"state": "open", "merged": False, **body}
            return self.reply(
                201, {"number": number, "html_url": f"https://github.com/{CONFIG_REPO}/pull/{number}"}
            )
        if m == "GET" and rest.startswith("pulls/"):
            return self.reply(200, GitHubMock.prs[int(rest.split("/")[1])])
        return self.reply(404, {"message": "Not Found"})

    do_GET = do_POST = do_PUT = handle_any


CONFIG_REPO = "mcctl-e2e/e2e-mc-config"  # tests/e2e/answers.yml repo


def merge_pr(number: int) -> None:
    pr = GitHubMock.prs[number]
    GitHubMock.files["main"] = GitHubMock.files[pr["head"]]
    GitHubMock.server["main"] = GitHubMock.server[pr["head"]]
    pr.update(state="closed", merged=True)


def wait_for(what: str, cond: Any, timeout: float = 180, step: float = 2) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = cond()
        if value:
            return value
        time.sleep(step)
    expect(False, f"timed out waiting for {what}")
    return None


def pending() -> list[dict[str, Any]]:
    try:
        return json.loads(Path("/var/lib/mc-agent/pending.json").read_text()).get("entries", [])
    except (OSError, ValueError):
        return []


def whitelisted() -> dict[str, str]:
    return {e["name"]: e["uuid"] for e in json.loads((SERVER / "whitelist.json").read_text())}


def send(action: str, args: dict[str, Any], cid: str, timeout: float = 120) -> dict[str, Any]:
    with queue_lock:
        queued.append(command(action, args, cid))
    return result_of(cid, timeout)


def job_done(cid: str, timeout: float = 600) -> dict[str, Any]:
    def finished() -> dict[str, Any] | None:
        for j in reversed(seen["job"]):
            if j.get("id") == cid and j.get("state") in ("done", "failed", "cancelled"):
                return j
        return None

    return wait_for(f"job {cid} to finish", finished, timeout, 2) or {}


def rcon(line: str) -> str:
    sys.path.insert(0, "/opt/minecraft/bin")
    import mc_rcon

    with mc_rcon.connect() as r:
        return mc_rcon.strip_formatting(r.command(line))


def players_role() -> subprocess.CompletedProcess[str]:
    """A deploy's players role, as the real playbook runs it (Git from the rendered inventory + pending)."""
    return subprocess.run(
        ["/opt/ansible/bin/ansible-playbook", "-i", "/src/rendered/ansible/inventory.yml", "site.yml",
         "--tags", "players", "-e", "@/src/product/tests/e2e/vars.yml"],
        cwd="/src/product/ansible", capture_output=True, text=True, check=False,
    )  # fmt: skip


def whitelist_round() -> None:
    wl, ops = whitelisted(), json.loads((SERVER / "ops.json").read_text())
    GitHubMock.files = {"main": canonical([{"name": n, "uuid": u} for n, u in wl.items()],
                                          [{"name": o["name"], "uuid": o["uuid"], "level": o["level"]} for o in ops])}  # fmt: skip
    gh = http.server.ThreadingHTTPServer(("127.0.0.1", 8788), GitHubMock)
    threading.Thread(target=gh.serve_forever, daemon=True).start()
    try:
        name, uuid = PANEL_NAME, PANEL_UUID
        r = send("whitelist", {"op": "add", "name": name}, "e2e-wl-add")
        if not r.get("ok"):  # the profile cache didn't resolve it: a real account through Mojang
            name, uuid = "Notch", "069a79f4-44e9-4726-a5be-fca90e38aaf5"
            print(f"     (cached name refused: {r.get('error')}; trying {name} via Mojang)")
            r = send("whitelist", {"op": "add", "name": name}, "e2e-wl-add-2")
        expect(r.get("ok") is True, f"signed whitelist add ran ({r})")
        expect(whitelisted().get(name) == uuid, f"{name} is live in whitelist.json with its UUID")
        entry = next((e for e in pending() if e["uuid"] == uuid), {})
        expect(
            entry.get("op") == "add" and entry.get("login") == "e2e@tests",
            f"recorded in pending.json ({entry})",
        )
        # a deploy before the PR is merged must not undo it (Git doesn't have the player yet)
        run = players_role()
        expect(run.returncode == 0, f"site.yml --tags players ran ({run.stdout[-400:]}{run.stderr[-400:]})")
        expect(whitelisted().get(name) == uuid, "the deploy kept the pending add (Git + pending)")
        pr = wait_for("the whitelist pull request", lambda: GitHubMock.prs.get(1), 120)
        if pr:
            text = GitHubMock.files.get(pr["head"], "")
            expect(pr["head"].startswith("panel/") and pr["base"] == "main", f"PR from {pr['head']} to main")
            expect(
                f'{{name: "{name}", uuid: "{uuid}"}}' in text and text.endswith("\n"),
                "canonical players.yml in the PR",
            )
            expect(
                GitHubMock.tokens == {f"Bearer {INSTALLATION_TOKEN}"} and len(seen["github_token"]) >= 1,
                "the installation token minted by the Worker, nothing else (no GitHub credential on the box)",
            )
            merge_pr(1)
            wait_for(
                "pending.json to clear after the merge",
                lambda: not any(e["uuid"] == uuid for e in pending()),
                90,
            )
        r = send("whitelist", {"op": "remove", "name": name}, "e2e-wl-remove")
        expect(r.get("ok") is True and name not in whitelisted(), f"signed whitelist remove ran ({r})")
        pr2 = wait_for("the second pull request", lambda: GitHubMock.prs.get(2), 120)
        if pr2:
            expect(name not in GitHubMock.files.get(pr2["head"], name), "the remove is in the second PR")
            merge_pr(2)
            wait_for("pending.json to clear again", lambda: not pending(), 90)
    finally:
        gh.shutdown()
        gh.server_close()  # world_round binds 8788 again


def restart_round() -> None:
    before = show("minecraft.service", "MainPID")["MainPID"]
    r = send("restart", {"countdown": 10, "skip_if_empty": False}, "e2e-restart")
    expect(
        r.get("ok") is True and "scheduled" in r.get("output", ""),
        f"restart with a countdown scheduled ({r})",
    )
    job = job_done("e2e-restart")
    states = [j["state"] for j in seen["job"] if j.get("id") == "e2e-restart"]
    expect(
        job.get("state") == "done" and states[0] == "countdown" and "running" in states,
        f"job states {states}",
    )
    first = next(j for j in seen["job"] if j.get("id") == "e2e-restart")
    expect(
        9000 <= first["deadline"] - first["started"] <= 11000,
        f"a 10 s countdown ({first['deadline'] - first['started']} ms)",
    )
    expect(show("minecraft.service", "MainPID")["MainPID"] not in (before, "0"), "the JVM restarted")
    expect(server_answers(), "the server is back after the restart")


def plugin_round() -> None:
    lock = json.loads(Path("/opt/minecraft/plugins-lock.json").read_text())
    if not lock:
        expect(False, "the plugins role wrote the lock for the panel")
        return
    name, jar = lock[0]["name"], lock[0]["filename"]
    r = send("plugin", {"name": name, "enabled": False, "countdown": 0}, "e2e-plugin-off")
    expect(r.get("ok") is True, f"plugin disable scheduled ({r})")
    job = job_done("e2e-plugin-off")
    expect(job.get("state") == "done", f"plugin disable job done ({job.get('step')})")
    expect((SERVER / "plugins/disabled" / jar).is_file() and not (SERVER / "plugins" / jar).exists(),
           f"{jar} moved to plugins/disabled/")  # fmt: skip
    expect(server_answers(), "the server restarted without it")
    wait_for("RCON after the plugin restart", lambda: "There are" in rcon_safe("list"), 120)
    expect(
        name.lower() not in rcon_safe("plugins").lower(),
        f"{name} isn't loaded ({rcon_safe('plugins')[:120]})",
    )
    r = send("plugin", {"name": name, "enabled": True, "countdown": 0}, "e2e-plugin-on")
    job = job_done("e2e-plugin-on")
    expect(
        job.get("state") == "done" and (SERVER / "plugins" / jar).is_file(),
        f"plugin enabled again ({job.get('step')})",
    )
    wait_for("RCON after the second plugin restart", lambda: "There are" in rcon_safe("list"), 120)
    expect(name.lower() in rcon_safe("plugins").lower(), f"{name} is loaded again")


def rcon_safe(line: str) -> str:
    try:
        return rcon(line)
    except Exception as e:  # the server may still be starting
        return f"{type(e).__name__}"


def restic_snapshots(*args: str) -> list[dict[str, Any]]:
    """restic's snapshot list. A run that fails (the repository locked by a backup's forget, which the world
    round's first backup of the new world races with) is retried, never read as an empty list."""
    deadline = time.monotonic() + 120
    while True:
        proc = subprocess.run(["/usr/local/bin/mc-restic", "restic", "snapshots", "--json", *args],
                              capture_output=True, text=True, check=False)  # fmt: skip
        if proc.returncode == 0:
            return json.loads(proc.stdout or "[]")
        if time.monotonic() > deadline:
            raise RuntimeError(f"restic snapshots failed: {proc.stderr.strip()[-300:]}")
        time.sleep(3)


def backup_and_restore_round() -> None:
    before = {s["id"] for s in restic_snapshots()}
    r = send("backup", {}, "e2e-backup")
    expect(r.get("ok") is True, f"backup now ran ({r})")
    wait_for("the backup to finish", lambda: show("mc-backup.service", "ActiveState")["ActiveState"] == "inactive" and
             {s["id"] for s in restic_snapshots()} - before, 300, 3)  # fmt: skip
    listed = json.loads(Path("/var/lib/mc-snapshots/snapshots.json").read_text())["snapshots"]
    new = [s for s in listed if s["id"] not in before]
    expect(bool(new), f"restic's snapshot list for the panel has the new snapshot ({len(listed)} listed)")
    wait_for(
        "an inventory push with snapshots", lambda: any(i.get("snapshots") for i in seen["inventory"]), 60
    )
    if not new:
        return
    snap = new[0]
    marker = SERVER / "world" / "e2e-after-snapshot.txt"
    marker.write_text("built after the snapshot")
    pw = pwd.getpwnam("minecraft")
    os.chown(marker, pw.pw_uid, pw.pw_gid)
    r = send("restore", {"snapshot": snap["short_id"], "countdown": 0}, "e2e-restore")
    expect(r.get("ok") is True, f"restore scheduled ({r})")
    job = job_done("e2e-restore", 900)
    expect(
        job.get("state") == "done" and job.get("step", "").startswith("RESTORED"),
        f"restore job done ({job.get('step')})",
    )
    steps = [j.get("step", "") for j in seen["job"] if j.get("id") == "e2e-restore"]
    expect(any("safety snapshot" in st for st in steps), f"the steps were reported ({steps[-4:]})")
    expect(not marker.exists(), "the world is back at the snapshot (the later change is gone)")
    restored = json.loads((SERVER / ".restored-from").read_text())
    expect(restored["snapshot_id"] == snap["id"], ".restored-from names the snapshot")
    safety = restic_snapshots("--tag", "pre-restore")
    expect(bool(safety), f"a pre-restore safety snapshot exists ({len(safety)})")
    if safety:
        latest = max(safety, key=lambda s: s["time"])
        ls = subprocess.run(["/usr/local/bin/mc-restic", "restic", "ls", latest["id"]], capture_output=True, text=True,
                            check=False).stdout  # fmt: skip
        expect("e2e-after-snapshot.txt" in ls, "the safety snapshot holds the change the restore removed")
    unit = show(f"mc-restore@{snap['short_id']}.service", "Result", "User")
    expect(unit == {"Result": "success", "User": "mcops"}, f"mc-restore@ ran as mcops ({unit})")
    expect(server_answers(), "the restored server accepts connections")


def deploy_minecraft(*extra: str) -> subprocess.CompletedProcess[str]:
    """A deploy's minecraft role, as the real playbook runs it."""
    return subprocess.run(
        ["/opt/ansible/bin/ansible-playbook", "-i", "/src/rendered/ansible/inventory.yml", "site.yml",
         "--tags", "minecraft", "-e", "@/src/product/tests/e2e/vars.yml", *extra],
        cwd="/src/product/ansible", capture_output=True, text=True, check=False,
    )  # fmt: skip


def restore_guard_round() -> None:
    """P10 review: a deploy during a restore must not start the server. Mid-restore state (as restore.py --panel
    leaves it between its stop and its start): restore.lock held by mcops, the server stopped, the admin-stopped
    marker by=restore. The deploy waits for the lock and then fails without starting anything; with the lock
    released but the marker still there (a failed apply) it deploys and leaves the server down."""
    marker = Path("/var/lib/minecraft/admin-stopped")
    subprocess.run(["systemctl", "stop", "minecraft.service"], check=True)
    holder = subprocess.Popen(["runuser", "-u", "mcops", "--", "flock", "/var/lib/minecraft/restore.lock",
                               "sh", "-c", "echo '{\"by\": \"restore\", \"reason\": \"e2e restore in progress\"}' "
                               "> /var/lib/minecraft/admin-stopped && sleep 300"])  # fmt: skip
    try:
        wait_for("the restore lock to be held", marker.exists, 30, 1)
        run = deploy_minecraft("-e", "mc_restore_wait_s=20")
        out = run.stdout + run.stderr
        expect(run.returncode != 0 and "A restore is still running" in out,
               f"a deploy during a restore waits, then refuses ({out[-300:]})")  # fmt: skip
        state = show("minecraft.service", "ActiveState")["ActiveState"]
        expect(
            state not in ("active", "activating"), f"the deploy didn't start the server mid-restore ({state})"
        )
    finally:
        holder.kill()
        holder.wait()
    run = deploy_minecraft()
    out = run.stdout + run.stderr
    expect(run.returncode == 0 and "admin-stopped exists" in out,
           f"lock released, marker kept: the deploy runs and warns ({out[-300:]})")  # fmt: skip
    state = show("minecraft.service", "ActiveState")["ActiveState"]
    expect(
        state not in ("active", "activating"), f"a marker left by a restore keeps the server down ({state})"
    )
    marker.unlink()
    subprocess.run(["systemctl", "start", "minecraft.service"], check=True)
    expect(server_answers(), "the server is back after the restore-guard round")


def world_state() -> dict[str, Any]:
    try:
        return json.loads(Path("/var/lib/mc-snapshots/worlds.json").read_text())
    except (OSError, ValueError):
        return {}


def pending_world() -> dict[str, Any] | None:
    try:
        return json.loads(Path("/var/lib/mc-agent/pending.json").read_text()).get("world")
    except (OSError, ValueError):
        return None


def world_round() -> None:
    """The world round needs the GitHub mock too (whitelist_round shuts its own down when it ends)."""
    gh = http.server.ThreadingHTTPServer(("127.0.0.1", 8788), GitHubMock)
    threading.Thread(target=gh.serve_forever, daemon=True).start()
    try:
        _world_round()
    finally:
        gh.shutdown()
        gh.server_close()


def _world_round() -> None:
    """v1.2 world library, end to end through signed commands: create a new (void) world with a seed -> the active
    world is archived first (restic, tags world-archive + world:<name>, as mcops) -> mc-world-apply.service swaps the
    folders as minecraft -> the server generates the new world (the seed matches over RCON, server.properties has
    the type) -> pending.json -> a deploy honours it (worlds-library.json) -> a server.yml pull request -> merged;
    switch back -> the old world (a marker file in it) is back; delete the archived world; refusals."""
    marker = SERVER / "world" / "e2e-world-marker.txt"
    marker.write_text("the original world")
    pw = pwd.getpwnam("minecraft")
    os.chown(marker, pw.pw_uid, pw.pw_gid)
    for action, args, why in (("world_delete", {"name": "world"}, "active world"),
                              ("world_switch", {"name": "nowhere"}, "isn't an archived")):  # fmt: skip
        r = send(action, args, f"e2e-{action}-refused")
        expect(r.get("ok") is False and why in r.get("error", ""), f"{action} refused ({r.get('error')})")

    r = send("world_create", {"name": "e2e-void", "seed": "8675309", "type": "flat_void", "preset": "creative_build",
                              "countdown": 0}, "e2e-world-create")  # fmt: skip
    expect(r.get("ok") is True, f"world_create scheduled ({r})")
    job = job_done("e2e-world-create", 1200)
    expect(job.get("state") == "done" and job.get("step", "").startswith("STEP 7/7"),
           f"world_create done ({job.get('step')})")  # fmt: skip
    expect(world_state().get("active") == "e2e-void", f"worlds.json: e2e-void is active ({world_state()})")
    archives = restic_snapshots("--tag", "world-archive,world:world")
    expect(bool(archives), f"the old world was archived first (world-archive + world:world: {len(archives)})")
    expect(not marker.exists(), "the new world doesn't have the old world's files")
    expect(server_answers(), "the new world's server accepts connections")
    expect("8675309" in rcon_safe("seed"), f"the new world has the seed ({rcon_safe('seed')})")
    props = (SERVER / "server.properties").read_text()
    expect("level-type=minecraft\\:flat" in props or "level-type=minecraft:flat" in props,
           "server.properties: the void type")  # fmt: skip
    expect("gamemode=creative" in props, "server.properties: the creative_build preset")
    unit = show("mc-world.service", "Result", "User")
    expect(unit == {"Result": "success", "User": "mcops"}, f"mc-world.service ran as mcops ({unit})")
    expect(
        show("mc-world-apply.service", "User")["User"] == "minecraft",
        "mc-world-apply.service runs as minecraft",
    )
    expect((SERVER / "world" / "level.dat").stat().st_uid == pw.pw_uid, "the new world belongs to minecraft")
    expect(
        (pending_world() or {}).get("active") == "e2e-void",
        f"pending.json records the switch ({pending_world()})",
    )
    # a deploy before the pull request is merged keeps the live world
    run = deploy_minecraft()
    lib = json.loads(Path("/opt/minecraft/worlds-library.json").read_text())
    expect(run.returncode == 0 and lib.get("active") == "e2e-void",
           f"a deploy honours the pending world ({lib.get('active')}, rc {run.returncode})")  # fmt: skip
    expect("8675309" in rcon_safe("seed"), "the deploy didn't switch worlds")
    pr = wait_for("a world pull request", lambda: next(
        (n for n, p in GitHubMock.prs.items() if p.get("head", "").startswith("panel/world-")), None), 180, 3)  # fmt: skip
    if pr:
        text = GitHubMock.server[GitHubMock.prs[pr]["head"]]
        expect('active: "e2e-void"' in text and 'name: "e2e-void"' in text and 'name: "world"' in text,
               f"the PR sets world.active/world.library in server.yml ({text[-300:]})")  # fmt: skip
        merge_pr(pr)
        wait_for("the merged world change to leave pending.json", lambda: pending_world() is None, 300, 3)

    r = send("world_switch", {"name": "world", "countdown": 0}, "e2e-world-switch")
    expect(r.get("ok") is True, f"world_switch scheduled ({r})")
    job = job_done("e2e-world-switch", 1200)
    expect(job.get("state") == "done", f"world_switch done ({job.get('step')})")
    expect(
        marker.exists() and marker.read_text() == "the original world",
        "the archived world is back, files and all",
    )
    expect(world_state().get("active") == "world", "worlds.json: world is active again")
    expect(server_answers(), "the server accepts connections after switching back")
    expect(
        any(a.get("name") == "e2e-void" for a in world_state().get("archives", [])),
        "e2e-void is archived now",
    )

    r = send("world_delete", {"name": "e2e-void"}, "e2e-world-delete")
    expect(r.get("ok") is True, f"world_delete scheduled ({r})")
    job = job_done("e2e-world-delete", 600)
    st = world_state()
    expect(job.get("state") == "done" and not any(a.get("name") == "e2e-void" for a in st.get("archives", [])),
           f"e2e-void deleted ({job.get('step')})")  # fmt: skip
    expect(any(d.get("name") == "e2e-void" for d in st.get("deleted", [])),
           "its snapshots inside the bucket lock wait to be forgotten")  # fmt: skip
    expect(
        show("minecraft.service", "ActiveState")["ActiveState"] == "active",
        "a delete doesn't touch the server",
    )
    wait_for("an inventory push with the world library",
             lambda: any((i.get("worlds") or {}).get("active") == "world" for i in seen["inventory"]), 60)  # fmt: skip
    marker.unlink(missing_ok=True)


def done() -> bool:
    return bool(
        seen["results"] and seen["archive"] and seen["logs"] and seen["inventory"]
        and any(s["ts"] < STARTED_MS for s in seen["metrics"])
        and any(s["ts"] > STARTED_MS and s.get("up") for s in seen["metrics"])
    )  # fmt: skip


def main() -> int:
    # No timer-driven backup/health/reboot-gate run in the middle of the rounds (a self-heal start while the
    # restore-guard round keeps the server down on purpose, a backup snapshot taken for the panel's, ...).
    with timers_paused():
        return checks()


def checks() -> int:
    props = show("mc-agent.service", "ActiveState", "User", "MainPID")
    expect(
        props.get("ActiveState") == "active" and props.get("User") == "mcops",
        f"mc-agent runs as mcops ({props})",
    )
    pid = props.get("MainPID", "0")
    expect(as_minecraft_read(f"/proc/{pid}/environ") == 13, "minecraft can't read mc-agent's environment")
    expect(as_minecraft_read("/etc/minecraft/agent.env") == 13, "minecraft can't read agent.env")
    spool = Path("/var/lib/mc-agent/spool.jsonl")
    buffered = len(spool.read_text().splitlines()) if spool.exists() else 0
    expect(buffered > 0, f"while the Worker was unreachable the agent buffered {buffered} sample(s) on disk")

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 8787), Mock)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline and not done():
        time.sleep(3)
    time.sleep(2)
    rcon_quiet_round()
    client_code_round()
    stop_start_round()
    # P10
    whitelist_round()
    restart_round()
    plugin_round()
    backup_and_restore_round()
    restore_guard_round()
    world_round()  # v1.2
    srv.shutdown()

    old = [s for s in seen["metrics"] if s["ts"] < STARTED_MS]
    live = [s for s in seen["metrics"] if s["ts"] > STARTED_MS and s.get("up")]
    expect(len(old) >= 1, f"buffered samples were delivered once the Worker answered ({len(old)})")
    expect(bool(live), f"fresh samples arrive ({len(live)})")
    if live:
        s = live[-1]
        expect(
            isinstance(s.get("tps"), float) and isinstance(s.get("players"), int),
            f"TPS/players measured ({s})",
        )
        for key in ("mspt", "heap_used_mb", "heap_max_mb", "cpu_pct", "mem_pct", "disk_pct", "jvm_mem_mb",
                    "cpu_cores", "cpu0_pct", "mem_used_mb", "mem_total_mb", "disk_used_gb", "uptime_s", "tps5"):  # fmt: skip
            expect(key in s, f"sample has {key} ({s.get(key)})")
        expect(
            isinstance(s.get("players_online"), list),
            f"sample lists who is online ({s.get('players_online')})",
        )
        expect(
            isinstance(s.get("uptime_s"), int) and s["uptime_s"] > 0,
            f"server uptime measured ({s.get('uptime_s')})",
        )
    inv = seen["inventory"][-1] if seen["inventory"] else {}
    expect(bool(inv) and isinstance(inv.get("whitelist"), list) and isinstance(inv.get("plugins"), list),
           f"inventory pushed: whitelist + plugin jars ({str(inv)[:200]})")  # fmt: skip
    entries = [e for batch in seen["archive"] for e in batch]
    expect(bool(entries), f"journal archive uploaded ({len(entries)} entries, gzip NDJSON)")
    units = {r["unit"] for r in seen["logs"]}
    expect(bool(units) and units <= {u for u in units if u == "minecraft.service" or u.startswith("mc-")},
           f"D1 rows only from minecraft/mc-* ({sorted(units)[:6]})")  # fmt: skip
    # (The agent tried the WebSocket at start, before the mock was up, and retries it every 5 minutes: the
    # socket itself is covered by tests/test_agent.py; here the poll fallback answers the commands.)
    expect(not seen["unauth"], f"every request carried the agent token ({seen['unauth']})")
    results = {r["id"] or f"replay{i}": r for i, r in enumerate(seen["results"])}
    st, lst, den = results.get("e2e-status", {}), results.get("e2e-list", {}), results.get("e2e-denied", {})
    expect(st.get("ok") is True and '"tps"' in st.get("output", ""), f"status ran ({str(st)[:200]})")
    expect(
        lst.get("ok") is True and "players" in lst.get("output", ""), f"console list ran over RCON ({lst})"
    )
    expect(
        den.get("ok") is False and "not allowed" in den.get("error", ""), f"console stop was refused ({den})"
    )
    replays = [r for r in seen["results"] if "replayed" in r.get("error", "")]
    expect(len(replays) == 1, f"the replayed command was refused ({replays})")
    expect(show("minecraft.service", "ActiveState")["ActiveState"] == "active", "the server is still running")
    audit = subprocess.run(["journalctl", "SYSLOG_IDENTIFIER=mc-agent", "MC_AUDIT=1", "-o", "json", "--no-pager"],
                           capture_output=True, text=True).stdout.splitlines()  # fmt: skip
    expect(len(audit) >= 15, f"every command is in the local journal with MC_AUDIT=1 ({len(audit)})")
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
