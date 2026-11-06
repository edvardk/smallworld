"""P10 panel actions on the server (scripts/mc_panel.py + mc_agent.py's executor): the players.yml codec, the
whitelist -> Git flow against a local fake of the GitHub API, rejected logins, the job runner (countdown, cancel,
skip when empty, one at a time), plugin switches, restores, moderation, and the players role's reconcile with
pending changes. No network beyond 127.0.0.1, no systemd: systemctl and RCON are faked."""

from __future__ import annotations

import base64
import contextlib
import http.server
import json
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import mc_agent
import mc_panel
import notify
import pytest

from mcctl import schema

UUID_A = "3f2a0000-0000-4000-8000-000000000c41"
UUID_B = "5c090000-0000-4000-8000-00000000e212"
UUID_C = "0d61aa00-bb00-1122-4455-667788997f90"
CANON = (
    "whitelist:\n"
    f'  - {{name: "Em_K", uuid: "{UUID_A}"}}\n'
    f'  - {{name: "jonas_r", uuid: "{UUID_B}"}}\n'
    "ops:\n"
    f'  - {{name: "Em_K", uuid: "{UUID_A}", level: 4}}\n'
)


# --- players.yml codec ----------------------------------------------------------------------------------------


def test_players_codec_round_trips_and_matches_mcctl() -> None:
    parsed = mc_panel.parse_players(CANON)
    assert parsed["ops"] == [{"name": "Em_K", "uuid": UUID_A, "level": 4}]
    assert mc_panel.dump_players(parsed) == CANON
    loaded = schema.load_players(parsed)
    assert schema.dump_players(loaded) == CANON  # the panel writes exactly what `mcctl validate` wants
    assert mc_panel.dump_players({"whitelist": [], "ops": []}) == "whitelist: []\nops: []\n"
    # names that YAML would turn into booleans/numbers stay JSON-quoted strings
    odd = {"whitelist": [{"name": "yes", "uuid": UUID_C}, {"name": "1234", "uuid": UUID_B}], "ops": []}
    text = mc_panel.dump_players(odd)
    assert text == schema.dump_players(schema.load_players(odd))
    assert mc_panel.parse_players(text)["whitelist"][0]["name"] == "1234"


@pytest.mark.parametrize(
    "text",
    ["whitelist:\n- {name: Em_K, uuid: x}\n", "whitelist:\n  - name: Em_K\n", "whitelist:\n  - {name: \"Sam\"}\n",
     "players: []\n"],
)  # fmt: skip
def test_players_codec_refuses_non_canonical_files(text: str) -> None:
    with pytest.raises(ValueError):
        mc_panel.parse_players(text)


def test_apply_entries_adds_removes_and_never_touches_ops() -> None:
    players = mc_panel.parse_players(CANON)
    entries = [
        {"op": "add", "name": "Felix_2011", "uuid": UUID_C},
        {"op": "remove", "name": "jonas_r", "uuid": UUID_B},
        {
            "op": "remove",
            "name": "Em_K",
            "uuid": UUID_A,
        },  # an op: stays (the auto-merge refuses ops changes)
        {"op": "add", "name": "Em_K", "uuid": UUID_A},  # already there: no change
    ]
    new, changed = mc_panel.apply_entries(players, entries)
    assert changed == [UUID_C, UUID_B]
    assert {p["name"] for p in new["whitelist"]} == {"Em_K", "Felix_2011"}
    assert new["ops"] == players["ops"]


def test_pending_latest_change_per_player_wins(tmp_path: Path) -> None:
    pend = mc_panel.Pending(tmp_path / "pending.json")
    pend.record("add", "Felix_2011", UUID_C, "admin@x", 100.0)
    pend.record("remove", "Felix_2011", UUID_C, "admin@x", 200.0)
    data = pend.load()
    assert [(e["op"], e["ts"]) for e in data["entries"]] == [("remove", 200.0)]
    assert data["changed"] == 200.0
    assert (tmp_path / "pending.json").stat().st_mode & 0o777 == 0o600


# --- GitHub (a local fake of the REST endpoints the agent uses) -------------------------------------------------


class FakeGitHub:
    def __init__(self, players: str = CANON) -> None:
        self.files = {"main": players}
        self.prs: dict[int, dict[str, Any]] = {}
        self.calls: list[tuple[str, str, dict[str, Any] | None]] = []
        self.auth: set[str] = set()
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
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
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                fake.calls.append((self.command, self.path, body))
                fake.auth.add(self.headers.get("Authorization", ""))
                code, out = fake.route(self.command, self.path, body)
                self.reply(code, out)

            do_GET = do_POST = do_PUT = handle_any

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def route(self, method: str, path: str, body: Any) -> tuple[int, Any]:
        repo = "/repos/octocat/mc-config"
        if method == "GET" and path == f"{repo}/git/ref/heads/main":
            return 200, {"object": {"sha": "c0ffee" * 6 + "abcd"}}
        if method == "GET" and path.startswith(f"{repo}/contents/players.yml"):
            text = self.files["main"]
            return 200, {"sha": "blob1", "content": base64.b64encode(text.encode()).decode()}
        if method == "POST" and path == f"{repo}/git/refs":
            self.files[body["ref"].removeprefix("refs/heads/")] = self.files["main"]
            return 201, {"ref": body["ref"]}
        if method == "PUT" and path == f"{repo}/contents/players.yml":
            self.files[body["branch"]] = base64.b64decode(body["content"]).decode()
            return 200, {"commit": {"sha": "x"}}
        if method == "POST" and path == f"{repo}/pulls":
            number = len(self.prs) + 1
            self.prs[number] = {"state": "open", "merged": False, **body}
            return 201, {
                "number": number,
                "html_url": f"https://github.com/octocat/mc-config/pull/{number}",
            }
        if method == "GET" and path.startswith(f"{repo}/pulls/"):
            return 200, self.prs[int(path.rsplit("/", 1)[1])]
        return 404, {"message": "Not Found"}

    def merge(self, number: int) -> None:
        pr = self.prs[number]
        self.files["main"] = self.files[pr["head"]]
        pr.update(state="closed", merged=True)

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def github() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    yield fake
    fake.close()


TOKEN = "ghs_" + "T0kenFromTheWorker" * 2  # an installation token shape (built at run time for gitleaks)


def gh_for(fake: FakeGitHub, minted: list[int] | None = None) -> mc_panel.GitHub:
    env = {"CONFIG_REPO": "octocat/mc-config", "GITHUB_API_URL": fake.url}

    def fetch() -> dict[str, str]:  # the agent's POST /agent/github-token
        if minted is not None:
            minted.append(1)
        return {"token": TOKEN, "expires_at": "2099-01-01T00:00:00Z"}

    gh = mc_panel.github_from_env(env, fetch)
    assert gh is not None
    return gh


def test_whitelist_changes_become_one_pull_request_after_the_batch_window(tmp_path: Path, github) -> None:
    pend = mc_panel.Pending(tmp_path / "pending.json")
    gh = gh_for(github)
    log: list[str] = []
    pend.record("add", "Felix_2011", UUID_C, "sam@example.com", 1000.0)
    pend.record("remove", "jonas_r", UUID_B, "sam@example.com", 1010.0)
    assert (
        mc_panel.sync_pending(pend, gh, 1030.0, log=lambda m, **f: log.append(m)) is False
    )  # still batching
    assert not github.prs
    assert mc_panel.sync_pending(pend, gh, 1071.0, log=lambda m, **f: log.append(m)) is True
    assert list(github.prs) == [1]
    pr = github.prs[1]
    assert pr["base"] == "main" and pr["head"].startswith("panel/") and "+Felix_2011 -jonas_r" in pr["title"]
    branch_text = github.files[pr["head"]]
    assert branch_text == schema.dump_players(schema.load_players(mc_panel.parse_players(branch_text)))
    assert '"Felix_2011"' in branch_text and "jonas_r" not in branch_text and "level: 4" in branch_text
    assert github.auth == {f"Bearer {TOKEN}"}
    data = pend.load()
    assert data["pr"]["number"] == 1 and all(e["pr"] == 1 for e in data["entries"])
    assert data["error"] == "" and log == ["panel PR #1 opened"]
    # merged: the entries leave pending.json (the next check after PR_CHECK_S)
    github.merge(1)
    assert mc_panel.sync_pending(pend, gh, 1071.0 + 60, log=lambda m, **f: log.append(m)) is False
    assert (
        mc_panel.sync_pending(pend, gh, 1071.0 + mc_panel.PR_CHECK_S, log=lambda m, **f: log.append(m))
        is True
    )
    data = pend.load()
    assert data["entries"] == [] and data["pr"] is None


def test_a_pull_request_closed_unmerged_is_reverted_live_dropped_and_announced(tmp_path, github) -> None:
    """P10 review: closing the panel's PR without merging means "no": the server follows Git again at once."""
    pend = mc_panel.Pending(tmp_path / "pending.json")
    gh = gh_for(github)
    pend.record("add", "Felix_2011", UUID_C, "admin@x", 0.0)
    pend.record("remove", "jonas_r", UUID_B, "admin@x", 1.0)
    mc_panel.sync_pending(pend, gh, 100.0)
    github.prs[1].update(state="closed")
    rcon: list[str] = []
    alerts: list[str] = []

    def fake_rcon(line: str) -> str:
        rcon.append(line)
        return "ok"

    assert mc_panel.sync_pending(pend, gh, 100.0 + mc_panel.PR_CHECK_S, alert=alerts.append, rcon=fake_rcon)
    assert rcon == ["whitelist remove Felix_2011", "whitelist add jonas_r"]
    data = pend.load()
    assert data["entries"] == [] and data["pr"] is None and "closed without merging" in data["error"]
    assert len(alerts) == 1 and "#1" in alerts[0] and "remove Felix_2011" in alerts[0]
    assert [e["why"] for e in data["expired"]] == ["PR #1 closed", "PR #1 closed"]
    mc_panel.sync_pending(pend, gh, 1000.0, rcon=fake_rcon)
    assert len(github.prs) == 1 and len(rcon) == 2  # not retried, nothing more reverted


def test_a_revert_that_fails_is_reported(tmp_path, github) -> None:
    pend = mc_panel.Pending(tmp_path / "pending.json")
    gh = gh_for(github)
    pend.record("add", "Felix_2011", UUID_C, "admin@x", 0.0)
    mc_panel.sync_pending(pend, gh, 100.0)
    github.prs[1].update(state="closed")
    alerts: list[str] = []

    def down(line: str) -> str:
        raise ConnectionRefusedError("rcon down")

    mc_panel.sync_pending(pend, gh, 100.0 + mc_panel.PR_CHECK_S, alert=alerts.append, rcon=down)
    data = pend.load()
    assert data["entries"] == [] and "couldn't revert remove Felix_2011" in data["error"]
    assert "failed: remove Felix_2011" in alerts[0]


def test_changes_already_in_git_are_dropped_without_a_pull_request(tmp_path, github) -> None:
    gh = gh_for(github)
    pend2 = mc_panel.Pending(tmp_path / "p2.json")
    pend2.record("add", "jonas_r", UUID_B, "admin@x", 0.0)
    assert mc_panel.sync_pending(pend2, gh, 100.0) is True
    assert pend2.load()["entries"] == [] and not github.prs


def test_pending_changes_expire_after_a_day_with_an_alert(tmp_path) -> None:
    pend = mc_panel.Pending(tmp_path / "pending.json")
    pend.record("add", "Felix_2011", UUID_C, "admin@x", 0.0)
    alerts: list[str] = []
    assert mc_panel.sync_pending(pend, None, 3600.0, alert=alerts.append) is True
    data = pend.load()
    assert data["entries"] and "no config repo" in data["error"] and not alerts
    mc_panel.sync_pending(pend, None, mc_panel.PENDING_TTL_S + 1, alert=alerts.append)
    data = pend.load()
    assert data["entries"] == [] and data["expired"][0]["name"] == "Felix_2011"
    assert len(alerts) == 1 and "add Felix_2011" in alerts[0] and "dropped" in alerts[0]


def test_github_errors_never_carry_the_token(tmp_path, github) -> None:
    gh = gh_for(github)
    gh.repo = "octocat/missing"
    with pytest.raises(mc_panel.GitHubError) as e:
        gh.players_on_main()
    assert "HTTP 404" in str(e.value) and TOKEN not in str(e.value)
    pend = mc_panel.Pending(tmp_path / "pending.json")
    pend.record("add", "Felix_2011", UUID_C, "admin@x", 0.0)
    mc_panel.sync_pending(pend, gh, 100.0)
    assert "HTTP 404" in pend.load()["error"] and TOKEN not in (tmp_path / "pending.json").read_text()


def test_github_settings_need_https_except_loopback() -> None:
    base = {"CONFIG_REPO": "o/r"}
    fetch = dict
    assert mc_panel.github_from_env({}, fetch) is None
    assert mc_panel.github_from_env(base, None) is None
    assert mc_panel.github_from_env({**base, "CONFIG_REPO": "not a repo"}, fetch) is None
    gh = mc_panel.github_from_env(base, fetch)
    assert gh is not None and gh.api == "https://api.github.com" and gh.branch == "main"
    with pytest.raises(ValueError):
        mc_panel.github_from_env({**base, "GITHUB_API_URL": "http://evil.example"}, fetch)


def test_installation_tokens_are_cached_until_near_expiry_and_errors_hide_details() -> None:
    now = [1_000_000.0]
    calls: list[int] = []

    def fetch() -> dict[str, str]:
        calls.append(1)
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now[0] + 3600))
        return {"token": TOKEN, "expires_at": exp}

    cache = mc_panel.TokenCache(fetch, clock=lambda: now[0])
    assert cache() == TOKEN and cache() == TOKEN and len(calls) == 1
    now[0] += 3600 - 299  # within 5 minutes of expiry: a fresh one
    assert cache() == TOKEN and len(calls) == 2
    cache.invalidate()
    cache()
    assert len(calls) == 3

    def broken() -> dict[str, str]:
        raise OSError("connection refused to the Worker")

    with pytest.raises(mc_panel.GitHubError, match="no GitHub token from the Worker"):
        mc_panel.TokenCache(broken)()
    with pytest.raises(mc_panel.GitHubError, match="no usable GitHub token"):
        mc_panel.TokenCache(lambda: {"error": "no GitHub App configured"})()


def test_a_401_gets_one_fresh_token(tmp_path, github) -> None:
    minted: list[int] = []
    gh = gh_for(github, minted)
    real = github.route
    seen = {"n": 0}

    def once_401(method: str, path: str, body: Any) -> tuple[int, Any]:
        seen["n"] += 1
        return (401, {"message": "Bad credentials"}) if seen["n"] == 1 else real(method, path, body)

    github.route = once_401  # type: ignore[method-assign]
    gh.players_on_main()
    assert len(minted) == 2


# --- rejected logins -------------------------------------------------------------------------------------------

LINES = [
    (1000, "[14:06:01 INFO]: UUID of player xXcreeperXx is 5c090000-0000-4000-8000-00000000e212"),
    (1001, "[14:06:02 INFO]: Disconnecting xXcreeperXx (/203.0.113.9:51210): You are not whitelisted on this server!"),
    (1002, "[14:06:02 INFO]: xXcreeperXx (/203.0.113.9:51210) lost connection: You are not whitelisted on this server!"),
    (5000, "[14:07:00 INFO]: Disconnecting Felix_2011 (/[2001:db8:1:2::5]:5555): You are not white-listed on this server!"),
    (6000, "[14:08:00 INFO]: Disconnecting Bob_1 (/198.51.100.7:1): You are banned from this server."),
    (7000, "[14:09:00 INFO]: Em_K lost connection: Disconnected"),
    (90000, "[14:10:00 INFO]: Disconnecting xXcreeperXx (/203.0.113.10:4): You are not whitelisted on this server!"),
]  # fmt: skip


def test_rejected_logins_are_parsed_deduplicated_and_masked(tmp_path: Path) -> None:
    rej = mc_panel.RejectedLogins(tmp_path / "rejected.json")
    assert rej.feed(LINES) is True
    entries = {e["name"]: e for e in rej.entries()}
    assert set(entries) == {"xXcreeperXx", "Felix_2011"}
    creeper = entries["xXcreeperXx"]
    assert (creeper["count"], creeper["first"], creeper["last"]) == (
        2,
        1001,
        90000,
    )  # not 3: lost-connection line
    assert creeper["uuid"] == UUID_B and creeper["ip"] == "203.0.113.x"
    assert entries["Felix_2011"]["ip"] == "2001:db8:1::/48" and entries["Felix_2011"]["uuid"] is None
    assert rej.feed([(1, "[x INFO]: Done (38.4s)!")]) is False
    assert rej.drop("felix_2011") is True and {e["name"] for e in rej.entries()} == {"xXcreeperXx"}
    for i in range(80):
        rej.feed(
            [
                (
                    100000 + i * 20000,
                    f"[14:00:00 INFO]: Disconnecting P{i:03d}x (/198.51.100.4:5): You are not whitelisted on this server!",
                )
            ]
        )
    assert len(rej.entries()) == mc_panel.REJECTED_KEEP


SPOOFS = [
    # chat (vanilla chat line, unsigned chat, /me, /say, /msg echo), a plugin's message mid-line, no prefix
    "[14:06:02 INFO]: <Mallory> Disconnecting Victim_1 (/203.0.113.9:1): You are not whitelisted on this server!",
    "[14:06:02 INFO]: [Not Secure] <Mallory> Disconnecting Victim_1 (/198.51.100.4:5): not whitelisted",
    "[14:06:02 INFO]: * Mallory Disconnecting Victim_1 (/198.51.100.4:5): not whitelisted",
    "[14:06:02 INFO]: [Mallory] Victim_1 (/198.51.100.4:5) lost connection: You are not whitelisted on this server!",
    "[14:06:02 INFO]: Mallory whispers to you: Disconnecting Victim_1 (/198.51.100.4:5): not whitelisted",
    "Disconnecting Victim_1 (/198.51.100.4:5): You are not whitelisted on this server!",
    "[14:06:02 WARN]: [SomePlugin] Disconnecting Victim_1 (/198.51.100.4:5): not whitelisted",
    "[14:06:02 INFO]: Disconnecting Victim_1 (/198.51.100.4:5) (x): You are not whitelisted on this server!",
]


@pytest.mark.parametrize("line", SPOOFS)
def test_rejected_logins_ignore_lines_players_or_plugins_can_write(tmp_path: Path, line: str) -> None:
    """P10 review: only the server's own login listener line, anchored at the console prefix, counts."""
    rej = mc_panel.RejectedLogins(tmp_path / "rejected.json")
    assert rej.feed([(1, line)]) is False and rej.entries() == []


def test_spoofed_uuid_line_from_chat_doesnt_poison_the_uuid_map(tmp_path: Path) -> None:
    rej = mc_panel.RejectedLogins(tmp_path / "rejected.json")
    rej.feed(
        [(1, "[14:06:01 INFO]: <Mallory> UUID of player Victim_1 is 00000000-0000-4000-8000-000000000bad")]
    )
    assert rej.uuids == {}
    real = "\x1b[0m[14:06:03 INFO]: Disconnecting Victim_1 (/198.51.100.4:5): You are not whitelisted on this server!\r"
    assert rej.feed([(2, real)]) is True and rej.entries()[0]["uuid"] is None


@pytest.mark.parametrize(
    ("addr", "masked"),
    [("/203.0.113.7:51234", "203.0.113.x"), ("203.0.113.7", "203.0.113.x"), ("/[2001:db8::1]:25565", "2001:db8::/48"),
     ("<ip address withheld>", ""), ("/local:E:1234", "")],
)  # fmt: skip
def test_addresses_are_masked(addr: str, masked: str) -> None:
    assert mc_panel.mask_address(addr) == masked


def test_ship_logs_feeds_rejected_logins_only_after_delivery(settings_factory, monkeypatch, tmp_path) -> None:
    s = settings_factory()
    monkeypatch.setattr(
        mc_agent, "request", lambda *a, **k: (_ for _ in ()).throw(mc_agent.IngestError("down"))
    )
    seen: list[list[dict[str, Any]]] = []
    entries = [
        {"ts": 1001, "unit": "minecraft.service", "ident": "java", "prio": 6, "msg": LINES[1][1], "audit": 0}
    ]
    with pytest.raises(mc_agent.IngestError):
        mc_agent.ship_logs(
            s, tmp_path / "logs.json", lambda c: (entries, "c1", False), lambda t: t, seen.append
        )
    assert seen == []
    monkeypatch.setattr(mc_agent, "request", lambda *a, **k: {})
    monkeypatch.setattr(mc_agent, "post_json", lambda *a, **k: {})
    mc_agent.ship_logs(s, tmp_path / "logs.json", lambda c: (entries, "c1", False), lambda t: t, seen.append)
    assert [e["msg"] for e in seen[0]] == [LINES[1][1]]


@pytest.fixture
def settings_factory(tmp_path: Path):
    def make() -> mc_agent.Settings:
        return mc_agent.Settings("https://hb.example.invalid", "t" * 48, b"k" * 64, state_dir=tmp_path)

    return make


# --- jobs -------------------------------------------------------------------------------------------------------


class Clock:
    def __init__(self) -> None:
        self.t = 1000.0
        self.waits: list[float] = []
        self.cancel_at: float | None = None

    def now(self) -> float:
        return self.t

    def wait(self, ev: threading.Event, secs: float) -> bool:
        self.waits.append(secs)
        if self.cancel_at is not None and self.t + secs >= self.cancel_at:
            self.t = self.cancel_at
            return True
        self.t += secs
        return ev.is_set()


def jobs_with(players: int, clock: Clock) -> tuple[mc_panel.Jobs, list[str], list[dict[str, Any]]]:
    said: list[str] = []
    reports: list[dict[str, Any]] = []

    def rcon(line: str) -> str:
        said.append(line)
        return f"There are {players} of a max of 20 players online: a"

    return mc_panel.Jobs(reports.append, rcon, clock.now, clock.wait), said, reports


def test_a_countdown_warns_players_at_the_announced_times_then_works() -> None:
    clock = Clock()
    jobs, said, reports = jobs_with(3, clock)
    ran: list[float] = []
    job = jobs.start(
        "j1",
        "restart",
        "Restart",
        "admin@x",
        60,
        lambda j: ran.append(clock.t) or "restarted",
        background=False,
    )
    assert job.state == "done" and job.step == "restarted" and ran == [1060.0]
    chat = [line for line in said if line.startswith("tellraw")]
    assert [json.loads(line.split(" ", 2)[2])[2]["text"] for line in chat] == [
        "Restart in 1 minute. Find a safe spot!", "Restart in 30 seconds. Find a safe spot!",
        "Restart in 10 seconds. Find a safe spot!", "Restart in 5 seconds. Find a safe spot!",
        "Restart in 4 seconds. Find a safe spot!", "Restart in 3 seconds. Find a safe spot!",
        "Restart in 2 seconds. Find a safe spot!", "Restart in 1 second. Find a safe spot!",
    ]  # fmt: skip
    assert sum(1 for line in said if line.startswith("title @a actionbar")) == len(chat)
    assert [r["state"] for r in reports] == ["countdown", "running", "done"]
    assert reports[0]["deadline"] == 1060000 and reports[0]["players"] == 3


def test_nobody_online_skips_the_countdown_and_a_cancel_stops_it() -> None:
    clock = Clock()
    jobs, said, _ = jobs_with(0, clock)
    job = jobs.start("j1", "restart", "Restart", "admin@x", 300, lambda j: "ok", background=False)
    assert job.state == "done" and clock.waits == [] and not any(line.startswith("tellraw") for line in said)
    clock2 = Clock()
    clock2.cancel_at = 1000.0 + 200
    jobs2, said2, reports2 = jobs_with(2, clock2)
    work_ran: list[int] = []
    job2 = jobs2.start(
        "j2", "stop", "Server stopping", "admin@x", 300, lambda j: work_ran.append(1) or "x", background=False
    )
    assert job2.state == "cancelled" and not work_ran
    assert "Server stopping cancelled." in said2[-2]
    assert reports2[-1]["state"] == "cancelled"


def test_one_job_at_a_time_and_failures_are_reported() -> None:
    clock = Clock()
    jobs, _, reports = jobs_with(0, clock)
    gate = threading.Event()
    jobs.start("j1", "restore", "Restore", "admin@x", 0, lambda j: gate.wait(5) and "ok")
    with pytest.raises(mc_panel.ActionError, match="busy: restore"):
        jobs.start("j2", "restart", "Restart", "admin@x", 0, lambda j: "x", background=False)
    with pytest.raises(mc_panel.ActionError, match="already running"):
        jobs.cancel("admin@x")
    gate.set()
    for _ in range(50):
        if not jobs.busy():
            break
        threading.Event().wait(0.05)
    with pytest.raises(mc_panel.ActionError, match="countdown must be"):
        jobs.start("j3", "restart", "Restart", "admin@x", 7, lambda j: "x", background=False)

    def boom(job: mc_panel.Job) -> str:
        raise mc_panel.ActionError("restarted, but RCON didn't answer")

    job = jobs.start("j4", "restart", "Restart", "admin@x", 0, boom, background=False)
    assert job.state == "failed" and "RCON" in job.step and reports[-1]["state"] == "failed"


# --- the executor's P10 actions -----------------------------------------------------------------------------------


class FakeServer:
    """A server dir with whitelist.json/ops.json and an RCON that edits them like the real server would."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lines: list[str] = []
        (root / "whitelist.json").write_text(
            json.dumps([{"uuid": UUID_A, "name": "Em_K"}, {"uuid": UUID_B, "name": "jonas_r"}])
        )
        (root / "ops.json").write_text(json.dumps([{"uuid": UUID_A, "name": "Em_K", "level": 4}]))
        self.mojang = {"felix_2011": ("Felix_2011", UUID_C)}

    def command(self, line: str) -> str:
        self.lines.append(line)
        words = line.split()
        if words[:2] == ["whitelist", "add"]:
            hit = self.mojang.get(words[2].lower())
            if not hit:
                return "That player does not exist"
            wl = json.loads((self.root / "whitelist.json").read_text())
            wl.append({"uuid": hit[1], "name": hit[0]})
            (self.root / "whitelist.json").write_text(json.dumps(wl))
            return f"Added {hit[0]} to the whitelist"
        if words[:2] == ["whitelist", "remove"]:
            wl = [
                e
                for e in json.loads((self.root / "whitelist.json").read_text())
                if e["name"].lower() != words[2].lower()
            ]
            (self.root / "whitelist.json").write_text(json.dumps(wl))
            return f"Removed {words[2]} from the whitelist"
        return f"ran {line}"

    @contextlib.contextmanager
    def connect(self, *a: object, **k: object) -> Iterator[FakeServer]:
        yield self


@pytest.fixture
def panel(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    srv_dir = tmp_path / "server"
    srv_dir.mkdir()
    server = FakeServer(srv_dir)
    monkeypatch.setattr(mc_agent.mc_rcon, "connect", server.connect)
    monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp_path / "admin-stopped")
    monkeypatch.setattr(notify, "MAINTENANCE_FILE", tmp_path / "maintenance.json")
    monkeypatch.delenv("WATCHDOG_URL", raising=False)
    calls: list[list[str]] = []
    monkeypatch.setattr(mc_agent, "systemctl", lambda *a, **k: calls.append(list(a)))
    monkeypatch.setattr(mc_panel, "wait_for_rcon", lambda *a, **k: True)
    changed: list[int] = []
    p = mc_agent.Panel(
        jobs=mc_panel.Jobs(lambda j: None, rcon=lambda line: "There are 0 of a max of 20 players online:"),
        pending=mc_panel.Pending(tmp_path / "pending.json"),
        rejected=mc_panel.RejectedLogins(tmp_path / "rejected.json"),
        changed=lambda: changed.append(1),
        server_dir=srv_dir,
        plugins_lock=tmp_path / "plugins-lock.json",
        plugins_disabled=tmp_path / "plugins-disabled.json",
        snapshots=tmp_path / "snapshots.json",
        clock=lambda: 5000.0,
    )
    ex = mc_agent.Executor(lambda: {}, None, p)
    return ex, p, server, calls, changed, tmp_path


def run(ex: mc_agent.Executor, action: str, args: dict[str, Any]) -> str:
    return ex.run({"id": "c1", "action": action, "args": args, "login": "sam@example.com"})


def wait_idle(p: mc_agent.Panel) -> mc_panel.Job:
    for _ in range(100):
        if not p.jobs.busy():
            break
        threading.Event().wait(0.05)
    assert p.jobs.current is not None
    return p.jobs.current


def test_whitelist_add_and_remove_apply_live_and_are_recorded(panel) -> None:
    ex, p, server, _, changed, _ = panel
    p.rejected.feed(
        [
            (
                1,
                "[14:00:00 INFO]: Disconnecting Felix_2011 (/198.51.100.4:5): You are not whitelisted on this server!",
            )
        ]
    )
    out = run(ex, "whitelist", {"op": "add", "name": "felix_2011"})
    assert "Added Felix_2011" in out and server.lines == ["whitelist add felix_2011"]
    entries = p.pending.load()["entries"]
    assert [(e["op"], e["name"], e["uuid"], e["login"]) for e in entries] == [
        ("add", "Felix_2011", UUID_C, "sam@example.com")
    ]
    assert p.rejected.entries() == [] and changed  # approved: off the rejected list; inventory pushed
    run(ex, "whitelist", {"op": "remove", "name": "jonas_r"})
    assert ("remove", UUID_B) in {(e["op"], e["uuid"]) for e in p.pending.load()["entries"]}


@pytest.mark.parametrize(
    ("args", "why"),
    [({"op": "add", "name": "NoSuchPlayer"}, "didn't add"), ({"op": "remove", "name": "Em_K"}, "operator"),
     ({"op": "remove", "name": "nobody_here"}, "isn't on the whitelist"), ({"op": "add", "name": "a b"}, "Java name"),
     ({"op": "op", "name": "Em_K"}, "Java name")],
)  # fmt: skip
def test_whitelist_refusals(panel, args: dict[str, str], why: str) -> None:
    ex, p, *_ = panel
    with pytest.raises(mc_agent.Rejected, match=why):
        run(ex, "whitelist", args)
    assert p.pending.load()["entries"] == []


def test_moderation_goes_through_the_console_shapes(panel) -> None:
    ex, _, server, *_ = panel
    run(ex, "moderate", {"verb": "kick", "target": "jonas_r", "reason": "brb\nop Steve"})
    run(ex, "moderate", {"verb": "ban-ip", "target": "203.0.113.9", "reason": "spam"})
    run(ex, "moderate", {"verb": "pardon", "target": "griefer_99", "reason": "ignored"})
    assert server.lines == ["kick jonas_r brb op Steve", "ban-ip 203.0.113.9 spam", "pardon griefer_99"]
    for args in (
        {"verb": "kick", "target": "@a"},
        {"verb": "op", "target": "x"},
        {"verb": "pardon-ip", "target": "Steve"},
    ):
        with pytest.raises(mc_agent.Rejected):
            run(ex, "moderate", args)


def test_plugin_switch_writes_the_override_moves_the_jar_and_restarts(panel) -> None:
    ex, p, _, calls, _, tmp = panel
    (tmp / "plugins-lock.json").write_text(
        json.dumps([{"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1.4.40"}])
    )
    with pytest.raises(mc_agent.Rejected, match="not a plugin"):
        run(ex, "plugin", {"name": "worldedit", "enabled": False})
    with pytest.raises(mc_agent.Rejected, match="already enabled"):
        run(ex, "plugin", {"name": "chunky", "enabled": True})
    out = run(ex, "plugin", {"name": "chunky", "enabled": False, "countdown": 10})
    assert "disable scheduled" in out
    job = wait_idle(p)
    assert job.state == "done" and "restarted" in job.step, job.step
    assert mc_panel.read_disabled(tmp / "plugins-disabled.json") == ["chunky"]
    assert ["start", mc_panel.PLUGINS_APPLY_UNIT] in calls and ["restart", "minecraft.service"] in calls
    record = json.loads((tmp / "plugins-disabled.json").read_text())
    assert record["by"] == "sam@example.com"
    with pytest.raises(mc_agent.Rejected, match="already disabled"):
        run(ex, "plugin", {"name": "chunky", "enabled": False})


def test_restore_only_known_snapshots_through_the_restore_unit(panel, monkeypatch) -> None:
    ex, p, _, calls, _, tmp = panel
    snap = "f0a93b11" + "0" * 56
    (tmp / "snapshots.json").write_text(
        json.dumps(
            {
                "snapshots": [
                    {"id": snap, "short_id": "f0a93b11", "time": "2026-10-31T14:00:00Z", "tags": ["auto"]}
                ]
            }
        )
    )
    monkeypatch.setattr(
        mc_panel,
        "unit_steps",
        lambda unit, since: ["STEP 2/6: safety snapshot", "RESTORED f0a93b11 (safety snapshot 3b07aa90)"],
    )
    for bad in ("deadbeef", "F0A93B11", "f0a93b1", "../x"):
        with pytest.raises(mc_agent.Rejected):
            run(ex, "restore", {"snapshot": bad})
    run(ex, "restore", {"snapshot": "f0a93b11", "countdown": 0})
    job = wait_idle(p)
    assert job.state == "done" and job.step.startswith("RESTORED f0a93b11")
    assert calls[-1] == ["start", "mc-restore@f0a93b11.service"]

    def failing(*a: str, **k: object) -> None:
        import subprocess

        raise subprocess.CalledProcessError(1, "systemctl")

    monkeypatch.setattr(mc_agent, "systemctl", failing)
    monkeypatch.setattr(
        mc_panel,
        "unit_steps",
        lambda unit, since: ["STEP 4/6: fetching", "FAILED before anything changed (x)"],
    )
    run(ex, "restore", {"snapshot": snap})
    job = wait_idle(p)
    assert job.state == "failed" and "FAILED before anything changed" in job.step


def test_restart_and_stop_with_a_countdown_are_jobs(panel) -> None:
    ex, p, _, calls, _, tmp = panel
    assert "scheduled" in run(ex, "restart", {"countdown": 60, "backup": True})
    job = wait_idle(p)
    assert job.state == "done" and calls[:2] == [
        ["start", "mc-backup.service"],
        ["restart", "minecraft.service"],
    ]
    assert run(ex, "restart", {}) == "restarted"  # countdown 0, no backup: inline (P8 behaviour)
    with pytest.raises(mc_agent.Rejected, match="countdown"):
        run(ex, "restart", {"countdown": 5})
    with pytest.raises(mc_agent.Rejected, match="backup must be"):
        run(ex, "restart", {"countdown": 10, "backup": "yes"})
    run(ex, "stop", {"countdown": 10, "reason": "maintenance\ttonight"})
    job = wait_idle(p)
    marker = json.loads((tmp / "admin-stopped").read_text())
    assert (
        job.state == "done"
        and marker["reason"] == "maintenance tonight"
        and marker["by"] == "panel (sam@example.com)"
    )
    with pytest.raises(mc_agent.Rejected, match="admin-stopped"):
        run(ex, "restart", {"countdown": 10})
    assert run(ex, "start", {}) == "started" and not (tmp / "admin-stopped").exists()


def test_cancel_and_dismiss(panel) -> None:
    ex, p, *_ = panel
    with pytest.raises(mc_agent.Rejected, match="nothing to cancel"):
        run(ex, "cancel", {})
    p.rejected.feed(
        [
            (
                1,
                "[14:00:00 INFO]: Disconnecting Felix_2011 (/198.51.100.4:5): You are not whitelisted on this server!",
            )
        ]
    )
    assert run(ex, "rejected_dismiss", {"name": "Felix_2011"}) == "Felix_2011 dismissed"
    assert p.rejected.entries() == []


def test_p10_actions_need_the_panel_state() -> None:
    ex = mc_agent.Executor(lambda: {})
    for action in ("whitelist", "plugin", "restore", "cancel", "rejected_dismiss"):
        with pytest.raises(mc_agent.Rejected, match="not available"):
            ex.run({"id": "c", "action": action, "args": {"name": "Felix_2011", "op": "add"}, "login": "x"})


# --- plugins on disk (mc-plugins-apply.service, as minecraft) ------------------------------------------------------


def test_apply_plugins_moves_only_locked_jars_both_ways(tmp_path: Path) -> None:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "Chunky-1.4.40.jar").write_text("jar")
    (plugins / "Other.jar").write_text("not locked")
    (plugins / "Linked.jar").symlink_to(tmp_path / "elsewhere")
    lock = [{"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1"},
            {"name": "linked", "filename": "Linked.jar", "version": "1"}]  # fmt: skip
    assert mc_panel.apply_plugins(tmp_path, lock, ["chunky", "linked"]) == ["chunky: disabled"]
    assert (plugins / "disabled" / "Chunky-1.4.40.jar").exists() and (plugins / "Other.jar").exists()
    assert (plugins / "Linked.jar").is_symlink()  # never followed or moved
    assert mc_panel.apply_plugins(tmp_path, lock, ["chunky"]) == []  # idempotent
    assert mc_panel.apply_plugins(tmp_path, lock, []) == ["chunky: enabled"]
    assert (plugins / "Chunky-1.4.40.jar").exists()


def test_lock_and_disabled_files_are_validated(tmp_path: Path) -> None:
    (tmp_path / "lock.json").write_text(json.dumps([
        {"name": "chunky", "filename": "Chunky.jar"}, {"name": "../x", "filename": "a.jar"},
        {"name": "ok", "filename": "../../etc/passwd"}, "junk"]))  # fmt: skip
    assert [p["name"] for p in mc_panel.read_lock(tmp_path / "lock.json")] == ["chunky"]
    (tmp_path / "off.json").write_text(json.dumps({"disabled": ["chunky", "../x", 3, "chunky"]}))
    assert mc_panel.read_disabled(tmp_path / "off.json") == ["chunky"]
    assert mc_panel.read_disabled(tmp_path / "missing.json") == []


# --- players role: Git + pending adds - pending removes ---------------------------------------------------------


def desired(pending: list[dict[str, str]]) -> dict[str, Any]:
    return {
        "whitelist": [{"name": "Em_K", "uuid": UUID_A}, {"name": "jonas_r", "uuid": UUID_B}],
        "ops": [{"name": "Em_K", "uuid": UUID_A, "level": 4}],
        "pending": pending,
    }


def test_reconcile_honours_pending_changes_so_a_deploy_cant_undo_them(tmp_path: Path) -> None:
    rcon: list[str] = []
    pend = [{"op": "add", "name": "Felix_2011", "uuid": UUID_C, "ts": 1, "pr": 3},
            {"op": "remove", "name": "jonas_r", "uuid": UUID_B},
            {"op": "remove", "name": "Em_K", "uuid": UUID_A},  # an op stays whitelisted
            {"op": "add", "name": "bad name", "uuid": UUID_C}]  # fmt: skip
    changes = mc_panel.reconcile_players(
        desired(pend), tmp_path, live=True, rcon=lambda c: rcon.append(c) or ""
    )
    wl = {e["uuid"]: e["name"] for e in json.loads((tmp_path / "whitelist.json").read_text())}
    assert wl == {UUID_A: "Em_K", UUID_C: "Felix_2011"}
    ops = json.loads((tmp_path / "ops.json").read_text())
    assert ops == [{"uuid": UUID_A, "name": "Em_K", "level": 4, "bypassesPlayerLimit": False}]
    assert [c.split(" (")[0] for c in changes] == ["whitelist +2 -0", "ops +1 -0"] and rcon == [
        "whitelist reload"
    ]  # no `op <name>`
    # second run: nothing to do, even after the server rewrote the file in its own format
    (tmp_path / "whitelist.json").write_text(
        json.dumps([{"name": "felix_2011", "uuid": UUID_C}, {"uuid": UUID_A, "name": "Em_K"}], indent=4)
    )
    rcon.clear()
    assert (
        mc_panel.reconcile_players(desired(pend), tmp_path, live=True, rcon=rcon.append) == [] and rcon == []
    )
    # the PR merged: Git has the change, pending is empty: still nothing to do
    merged = desired([])
    merged["whitelist"] = [{"name": "Em_K", "uuid": UUID_A}, {"name": "Felix_2011", "uuid": UUID_C}]
    assert mc_panel.reconcile_players(merged, tmp_path, live=True, rcon=rcon.append) == []
    # without the pending entries a deploy would have reverted the panel's change
    assert [c.split(" (")[0] for c in mc_panel.reconcile_players(desired([]), tmp_path, live=False)] == [
        "whitelist +1 -1"
    ]


def test_reconcile_deops_and_level_changes_while_stopped(tmp_path: Path) -> None:
    (tmp_path / "ops.json").write_text(json.dumps([{"uuid": UUID_B, "name": "jonas_r", "level": 4}]))
    rcon: list[str] = []
    changes = mc_panel.reconcile_players(desired([]), tmp_path, live=True, rcon=rcon.append)
    assert (
        any(c.startswith("ops +1 -1") for c in changes)
        and rcon[-1:] == ["deop jonas_r"]
        and "op Em_K" not in rcon
    )
    d = desired([])
    d["ops"][0]["level"] = 2
    assert mc_panel.reconcile_players(d, tmp_path, live=False)[0].startswith("ops +0 -0")
    assert json.loads((tmp_path / "ops.json").read_text())[0]["level"] == 2


def test_reconcile_cli(tmp_path: Path, monkeypatch, capsys) -> None:
    monkeypatch.setattr(mc_panel, "SERVER_DIR", tmp_path)
    path = tmp_path / "desired.json"
    path.write_text(json.dumps(desired([])))
    assert mc_panel.main(["reconcile-players", str(path)]) == 0
    assert capsys.readouterr().out.startswith("CHANGED: whitelist +2 -0")
    assert mc_panel.main(["reconcile-players", str(path)]) == 0
    assert capsys.readouterr().out.strip() == "UNCHANGED"
    assert mc_panel.main(["bogus"]) == 2


# --- the agent's inventory and wiring ---------------------------------------------------------------------------------


def test_panel_state_reports_pending_rejected_plugins_snapshots_and_job(tmp_path, monkeypatch) -> None:
    agent = mc_agent.Agent(
        mc_agent.Settings("https://hb.example.invalid", "t" * 48, b"k" * 64, state_dir=tmp_path)
    )
    agent.panel.plugins_lock = tmp_path / "lock.json"
    agent.panel.plugins_disabled = tmp_path / "off.json"
    agent.panel.snapshots = tmp_path / "snaps.json"
    (tmp_path / "lock.json").write_text(
        json.dumps([{"name": "chunky", "filename": "Chunky.jar", "version": "1"}])
    )
    (tmp_path / "snaps.json").write_text(
        json.dumps(
            {"snapshots": [{"id": "ab" * 32, "short_id": "abababab", "time": "t", "tags": ["auto", "x"]}]}
        )
    )
    agent.panel.pending.record("add", "Felix_2011", UUID_C, "admin@x", 1.0)
    agent.panel.rejected.feed(
        [
            (
                1,
                "[14:00:00 INFO]: Disconnecting Bob_1 (/198.51.100.4:5): You are not whitelisted on this server!",
            )
        ]
    )
    state = agent.panel_state()
    assert state["pending"][0]["name"] == "Felix_2011" and state["whitelist_sync"]["configured"] is False
    assert state["rejected"][0]["name"] == "Bob_1" and state["plugins_lock"][0]["name"] == "chunky"
    assert (
        state["snapshots"][0]["tags"] == "auto,x" and state["job"] is None and state["plugins_disabled"] == []
    )


def test_polkit_allows_the_p10_units_only() -> None:
    from conftest import REPO

    rule = (REPO / "ansible/roles/base/templates/50-mc-ops.rules.j2").read_text()
    assert '"mc-plugins-apply.service": ["start"]' in rule
    assert r"var MC_OPS_RESTORE = /^mc-restore@[0-9a-f]{8,64}\.service$/;" in rule
    assert 'MC_OPS_RESTORE.test(unit) && verb == "start"' in rule
    unit = (REPO / "ansible/roles/backups/templates/mc-restore@.service.j2").read_text()
    assert (
        "User={{ mc_ops_user }}" in unit
        and "ExecStart={{ mc_bin_dir }}/restore.py --snapshot %i --panel" in unit
    )
    apply = (REPO / "ansible/roles/plugins/templates/mc-plugins-apply.service.j2").read_text()
    assert "User=minecraft" in apply and "ReadWritePaths={{ mc_server_dir }}/plugins" in apply
    assert "EnvironmentFile" not in apply
