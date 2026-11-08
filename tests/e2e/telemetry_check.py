"""P13 acceptance on the e2e-mock runner: player-experience telemetry on the real Purpur in the container, shipped
by the real mc-agent to the real Worker code (watchdog/test/e2e-worker.mjs on 127.0.0.1:8787, node:sqlite as D1).

Runs on the runner (needs node >= 22.13), drives the container with `docker exec`:
  - the mc-telemetry plugin loaded (journal "Enabling mc-telemetry"), writes snapshots to /run/mc-telemetry
    (tmpfs, minecraft-owned, 0640 files the agent can read); the GC log is there too;
  - /run/mc-verdicts is setgid minecraft and owned by the ops user: nothing running as minecraft may write it;
  - mc-agent ships minute rows (live) and segments (R2 under telemetry/), the Worker returns verdicts, the agent
    writes them to verdicts.tsv, and `/lag` (alias /whylag) over RCON shows the Worker's verdict;
  - a signed player_message (a broadcast) reaches messages.tsv; one with a bad shape is refused by the agent;
  - product overhead (cgroup accounting) is in the rows: minecraft.service and mc-agent.service, nothing else
    (v1.4, rust mode: the Rust agent runs the telemetry in-process, as the Python one does: mc-agent's main process
    is smallworld-agent, there is no mc-telemetry.service and no online.json hand-off);
  - contention (P13 review M1): a CPU hog with the JVM's weights saturates every core (Chunky pre-generation on a
    first deploy); a backup still finishes within CONTENTION_BACKUP_MAX_S (its save-off window too) and the
    agent's metrics (the Worker's "host alive" signal) never go stale;
  - outage: the Worker refuses for ~3 minutes, rows pile up in segments; it comes back, nobody is online (quiet
    time), the backfill delivers them: every minute between first and last row present once (no gaps, no dups);
  - P14 Discord: the interactions endpoint answers a signed PING, refuses a bad signature (401) and a user who
    isn't allowlisted, and a signed `/backup now` reaches the real agent as a signed command (result: backup
    started; the reply's follow-up edit carries it).

    MC_SECRET_AGENT_TOKEN=... MC_SECRET_COMMAND_HMAC_KEY=... python tests/e2e/telemetry_check.py BOX
"""

from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[2]
URL = "http://127.0.0.1:8787"
CONTENTION_BACKUP_MAX_S = 240  # a backup of the e2e world under a saturated box (unloaded: a few seconds)
CONTENTION_METRICS_GAP_S = 90  # metrics every 30 s here; the Worker calls the host down after 180 s
failures: list[str] = []
IPV4 = re.compile(r"(?<![\d.])\d{1,3}(?:\.\d{1,3}){3}(?![\d.])")
IPV6 = re.compile(r"(?i)(?<![0-9a-f:])[0-9a-f]{1,4}(?::[0-9a-f]{0,4}){2,7}(?![0-9a-f:])")


def strings(value: object) -> list[str]:
    """Every string (keys included) in a JSON value."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in [str(k), *strings(v)]]
    if isinstance(value, list):
        return [s for v in value for s in strings(v)]
    return []


def expect(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what, flush=True)
    if not ok:
        failures.append(what)


def sh(
    box: str, script: str, user: str | None = None, timeout: float = 120
) -> subprocess.CompletedProcess[str]:
    argv = ["docker", "exec", box] + (["runuser", "-u", user, "--"] if user else []) + ["sh", "-c", script]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)


def get(path: str, body: object = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, method="POST" if data else "GET")  # noqa: S310 - loopback
    with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310
        return json.loads(r.read())


def wait(what: str, cond: Any, timeout: float, step: float = 5.0) -> Any:
    end = time.time() + timeout
    while time.time() < end:
        try:
            got = cond()
        except Exception as e:
            got = None
            last = e
        if got:
            return got
        time.sleep(step)
    print(f"  (timed out waiting for {what}{': ' + repr(last) if 'last' in locals() else ''})", flush=True)
    return None


def rust_agent(box: str) -> bool:
    """v1.4: agent.implementation rust (the default; the e2e's python shards set the fallback): mc-agent's main
    process is smallworld-agent."""
    pid = sh(box, "systemctl show -P MainPID mc-agent.service").stdout.strip()
    return pid not in ("", "0") and sh(box, f"readlink /proc/{pid}/exe").stdout.strip().endswith(
        "/smallworld-agent"
    )


def rcon(box: str, command: str) -> str:
    return sh(box, f"/opt/minecraft/bin/mc_rcon.py cmd {command}", user="minecraft").stdout


def main() -> int:
    box = sys.argv[1]
    node = subprocess.Popen(
        ["node", "--import", "./watchdog/test/register.mjs", "watchdog/test/e2e-worker.mjs", "8787"],
        cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )  # fmt: skip
    try:
        wait("the e2e worker", lambda: get("/__e2e/state"), 30, 1)
        # the agent may have backed off its WebSocket for 5 minutes after the earlier mock; restart it so it polls now
        # (its telemetry thread restarts with it: the first rows reach this worker without a backoff)
        sh(box, "systemctl restart mc-agent.service")
        run(box)
    finally:
        node.terminate()
        out = node.communicate(timeout=10)[0]
        if failures:
            print("--- e2e worker output ---\n" + out[-4000:])
    print(f"{len(failures)} failure(s)")
    return 1 if failures else 0


def run(box: str) -> None:
    journal = sh(box, "journalctl -u minecraft --no-pager | grep -c 'Enabling mc-telemetry'").stdout.strip()
    expect(journal not in ("", "0"), "the mc-telemetry plugin is enabled by the real Purpur")
    snaps = wait(
        "snapshots",
        lambda: sh(box, "ls /run/mc-telemetry | grep -c '^snap-'").stdout.strip() not in ("", "0"),
        120,
    )
    expect(bool(snaps), "snapshots appear in /run/mc-telemetry")
    st = sh(
        box,
        "stat -c '%U:%G %a' /run/mc-telemetry; stat -c '%U:%G %a' $(ls -1 /run/mc-telemetry/snap-*.json | tail -1)",
    ).stdout.split("\n")
    expect(st[0] == "minecraft:minecraft 750", f"/run/mc-telemetry is minecraft's, 0750 ({st[0]})")
    expect(st[1] == "minecraft:minecraft 640", f"snapshots are 0640, group-readable by the agent ({st[1]})")
    snap = json.loads(sh(box, "cat $(ls -1v /run/mc-telemetry/snap-*.json | tail -1)").stdout)
    expect(
        snap["srv"]["ticks"] > 0 and snap["srv"]["p99"] is not None,
        f"a snapshot carries the tick distribution (p99 {snap['srv']['p99']} ms)",
    )
    expect(
        isinstance(snap["srv"].get("self_p99_us"), (int, float)),
        f"the plugin records its own per-tick cost ({snap['srv'].get('self_p99_us')} us)",
    )
    # an IP address in any string (a number like 1203.5 is none: the old "203." substring check flaked)
    ips = [v for v in strings(snap) if IPV4.search(v) or IPV6.search(v)]
    expect(not ips, f"no IP address in a snapshot ({ips[:3]})")
    expect(
        sh(box, "test -s /run/mc-telemetry/gc.log").returncode == 0,
        "the GC log is on tmpfs (/run/mc-telemetry/gc.log)",
    )
    vd = sh(box, "stat -c '%U:%G %a' /run/mc-verdicts").stdout.strip()
    expect(vd == "mcops:minecraft 2750", f"/run/mc-verdicts is the ops user's, setgid minecraft ({vd})")
    expect(
        sh(box, "touch /run/mc-verdicts/x", user="minecraft").returncode != 0,
        "minecraft can't write the verdict directory",
    )
    expect(
        sh(box, "cat /etc/minecraft/agent.env", user="minecraft").returncode != 0,
        "minecraft can't read the agent's credentials",
    )

    rows = wait(
        "live minute rows", lambda: [r for r in get("/__e2e/state")["rows"] if not r["backfill"]], 200
    )
    expect(bool(rows), f"mc-agent ships live minute rows to the Worker ({len(rows or [])})")
    if rows:
        r = rows[-1]
        expect(
            r["ticks"] > 0 and r["samples"] >= 1,
            f"a minute row aggregates the snapshots ({r['samples']} samples, {r['ticks']} ticks)",
        )
        expect(
            "minecraft.service" in r["ovh"] and "mc-agent.service" in r["ovh"],
            f"overhead per unit is in the row ({sorted(r['ovh'])})",
        )
        expect(
            "mc-telemetry.service" not in r["ovh"],
            f"... and no telemetry unit of its own ({sorted(r['ovh'])})",
        )
        expect(r["verdict"] in ("good", "fair", "poor"), f"the Worker judged the minute ({r['verdict']})")
    impl = "rust" if rust_agent(box) else "python"
    want = os.environ.get("E2E_AGENT_IMPL") or impl
    expect(impl == want, f"the telemetry runs in mc-agent's {want} process ({impl})")
    expect(
        sh(box, "systemctl show -P LoadState mc-telemetry.service").stdout.strip() != "loaded",
        f"the telemetry runs in-process ({impl}): no mc-telemetry.service",
    )
    expect(
        sh(box, "test -e /var/lib/mc-telemetry/online.json").returncode != 0,
        "... and no online.json hand-off (player_message reads the in-process telemetry)",
    )
    ident = sh(box, "stat -c '%U %a' /var/lib/mc-agent/telemetry-id.json").stdout.strip()
    expect(ident == "mcops 600", f"telemetry-id.json is the ops user's, 0600, in mc-agent's state ({ident})")
    tsv = wait("verdicts.tsv", lambda: sh(box, "cat /run/mc-verdicts/verdicts.tsv").stdout, 120)
    expect(
        bool(tsv) and tsv.startswith("mc-verdicts 1 ") and "\nS\t" in tsv,
        "the agent wrote the Worker's verdicts for the plugin",
    )
    perm = sh(box, "stat -c '%G %a' /run/mc-verdicts/verdicts.tsv").stdout.strip()
    expect(perm == "minecraft 640", f"verdicts.tsv is readable by the plugin only ({perm})")
    lag = wait("/lag", lambda: (o := rcon(box, "lag")) and "Server:" in o and o, 60, 3)
    expect(
        bool(lag) and "Now:" in (lag or ""),
        f"/lag over RCON shows the Worker's verdict: {(lag or '').splitlines()[:1]}",
    )
    expect("Server:" in rcon(box, "whylag"), "/whylag is the same command")

    # signed in-game message: a broadcast is queued for the plugin; a bad one is refused by the agent
    def signed(args: dict[str, Any]) -> str:
        return get("/__e2e/enqueue", {"action": "player_message", "args": args, "login": "e2e@tests"})["id"]

    good = signed(
        {
            "to": "*",
            "kind": "server",
            "text": "e2e: the server is lagging for everyone",
            "hover": "x",
            "id": f"lag-{secrets.token_hex(8)}",
        }
    )
    bad = signed({"to": "*", "kind": "shout", "text": "nope", "id": "lag-0123456789abcdef"})
    res = wait(
        "message results",
        lambda: (
            (rs := {x["id"]: x for x in get("/__e2e/state")["results"]}) and good in rs and bad in rs and rs
        ),
        90,
        3,
    )
    expect(bool(res) and res[good]["ok"], f"a signed player_message is accepted ({res and res[good]})")
    expect(
        bool(res) and not res[bad]["ok"] and "kind must be" in res[bad]["error"],
        "a malformed player_message is refused by the agent",
    )
    msgs = sh(box, "cat /run/mc-verdicts/messages.tsv").stdout
    expect(
        msgs.startswith("mc-messages 1") and "e2e: the server is lagging" in msgs,
        "the message is queued for the plugin",
    )

    seg = wait("a segment in R2", lambda: get("/__e2e/state")["segments"], 150)
    expect(
        bool(seg) and seg[0].startswith("telemetry/") and seg[0].endswith(".ndjson.gz"),
        f"segments reach R2 under telemetry/ ({(seg or [''])[0]})",
    )
    local = sh(box, "ls /var/lib/mc-telemetry/segments | grep -c ndjson.gz").stdout.strip()
    expect(local not in ("", "0"), f"the box keeps its own copy of the segments ({local})")

    discord_round(box)
    contention_round(box)

    # outage -> backlog -> quiet time -> backfill (nobody is online in the container: always quiet). Only the
    # minutes since this check's first live row count: earlier ones (agent_check restarted the server) may have
    # real gaps, and are backfilled too.
    t0 = min(r["ts"] for r in get("/__e2e/state")["rows"] if not r["backfill"])
    before = {r["ts"] for r in get("/__e2e/state")["rows"] if r["ts"] >= t0}
    get("/__e2e/down?on=1")
    time.sleep(200)
    backlog = sh(box, "cat /var/lib/mc-telemetry/segments/state.json").stdout
    pending = sum(len(v.get("todo", [])) for v in json.loads(backlog or "{}").values())
    expect(pending >= 1, f"during the outage the rows wait in segments ({pending} rows)")
    get("/__e2e/down?on=0")

    def complete() -> list[int] | None:
        rows = get("/__e2e/state")["rows"]
        ts = sorted(r["ts"] for r in rows if r["ts"] >= t0)
        if not ts or len(ts) != len(set(ts)):
            return None
        missing = [t for t in range(ts[0], ts[-1], 60000) if t not in set(ts)]
        st = json.loads(sh(box, "cat /var/lib/mc-telemetry/segments/state.json").stdout or "{}")
        left = sum(len(v.get("todo", [])) for v in st.values())
        return ts if not missing and left == 0 and len(ts) > len(before) + 2 else None

    done = wait("the backfill", complete, 420, 10)
    rows = get("/__e2e/state")["rows"]
    expect(bool(done), f"after the outage the backfill fills every minute, no gaps ({len(rows)} rows)")
    expect(any(r["backfill"] for r in rows), "some rows arrived by backfill")
    ids = [r["id"] for r in rows]
    expect(len(ids) == len(set(ids)), "no duplicate rows")
    m = get("/__e2e/maintain")
    expect(isinstance(m, dict) and "hours" in m, f"the Worker's rollup/retention runs over the data ({m})")


def discord(payload: dict[str, Any], *, sign: bool = True, tamper: bool = False) -> tuple[int, Any]:
    """POST an interaction to the Worker's Discord endpoint, signed by the e2e worker's stand-in key."""
    body = json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if sign:
        req = urllib.request.Request(URL + "/__e2e/discord-sign", data=body, method="POST")  # noqa: S310
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - loopback
            sig = json.loads(r.read())
        signature = sig["signature"]
        if tamper:
            signature = ("0" if signature[0] != "0" else "1") + signature[1:]
        headers |= {"X-Signature-Ed25519": signature, "X-Signature-Timestamp": sig["timestamp"]}
    req = urllib.request.Request(URL + "/discord/interactions", data=body, method="POST", headers=headers)  # noqa: S310
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 - loopback
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


def discord_round(box: str) -> None:
    """P14: Discord slash commands through the real Worker code to the real agent."""
    ids = {
        "app": "1300000000000000001",
        "guild": "1300000000000000002",
        "channel": "1300000000000000003",
        "user": "1300000000000000004",
    }
    seq = [0]

    def interaction(name: str, sub: str | None = None, user: str = ids["user"], roles: list[str] | None = None,
                    options: list[dict[str, Any]] | None = None) -> dict[str, Any]:  # fmt: skip
        seq[0] += 1
        opts = [{"type": 1, "name": sub, "options": options or []}] if sub else []
        return {
            "id": str(1310000000000000000 + int(time.time()) * 10 + seq[0]),
            "application_id": ids["app"], "type": 2, "token": f"e2e-interaction-token-{seq[0]:08d}",
            "guild_id": ids["guild"], "channel_id": ids["channel"],
            "member": {"user": {"id": user, "username": "e2e"}, "roles": roles or []},
            "data": {"name": name, "type": 1, "options": opts},
        }  # fmt: skip

    status, body = discord({"type": 1, "id": "1", "application_id": ids["app"], "token": "x"})
    expect(
        status == 200 and body == {"type": 1}, f"Discord: a signed PING is answered PONG ({status} {body})"
    )
    status, _ = discord({"type": 1}, tamper=True)
    expect(status == 401, f"Discord: a bad signature is refused with 401 ({status})")
    status, _ = discord({"type": 1}, sign=False)
    expect(status == 401, f"Discord: an unsigned request is refused with 401 ({status})")
    status, body = discord(interaction("backup", "now", user="1399999999999999999"))
    expect(
        status == 200 and "allowlist" in body["data"]["content"],
        f"Discord: a user who isn't allowlisted is refused ({body and body.get('data')})",
    )
    status, body = discord(interaction("status"))
    expect(status == 200 and "e2e" in body["data"]["content"], f"Discord: /status answers from D1 ({status})")
    status, body = discord(interaction("backup", "now"))
    content = body.get("data", {}).get("content", "") if isinstance(body, dict) else ""
    m = re.search(r"command ([0-9a-f]{8})", content)
    expect(status == 200 and bool(m), f"Discord: /backup now is queued as a signed command ({content!r})")
    if not m:
        return
    res = wait(
        "the agent's backup result",
        lambda: next((r for r in get("/__e2e/state")["results"] if r["id"].startswith(m.group(1))), None),
        120,
        3,
    )
    expect(
        bool(res) and res["ok"] and "backup started" in res["output"],
        f"Discord: the real agent ran /backup now ({res})",
    )
    edit = wait(
        "the reply's follow-up edit",
        lambda: [t for t in get("/__e2e/state")["discord_follow_ups"] if "Backup: done" in t],
        60,
        3,
    )
    expect(bool(edit), f"Discord: the reply is edited with the result ({(edit or [''])[0][:80]!r})")
    # (the contention round below times its own backup: this one must be over first)
    wait(
        "mc-backup to finish",
        lambda: sh(box, "systemctl is-active mc-backup").stdout.strip() != "active",
        180,
        3,
    )
    journal = sh(
        box,
        "journalctl -u mc-agent --no-pager | grep -c 'action=backup ok=True login=discord:1300000000000000004'",
    )
    expect(journal.stdout.strip() not in ("", "0"), "the agent's own audit names the Discord user")
    helper_round(box, discord, interaction)


def helper_round(box: str, discord: Any, interaction: Any) -> None:
    """v1.0.1: a helper (by the role in the signed payload: e2e-worker.mjs's helper_role_id) may /whitelist add,
    as a signed command the real agent runs and audits with the helper's id; /backup now stays admin-only."""
    helper, role = "1300000000000000011", "1300000000000000020"
    status, body = discord(interaction("backup", "now", user=helper, roles=[role]))
    expect(status == 200 and "admins only" in body["data"]["content"],
           f"Discord: a helper's /backup now is refused ({body and body.get('data')})")  # fmt: skip
    status, body = discord(interaction("status", user=helper, roles=[]))
    expect(status == 200 and "allowlist" in body["data"]["content"],
           "Discord: the same user without the role is refused")  # fmt: skip
    name = [{"type": 3, "name": "name", "value": "E2eHelperPick"}]
    status, body = discord(interaction("whitelist", "add", user=helper, roles=[role], options=name))
    content = body.get("data", {}).get("content", "") if isinstance(body, dict) else ""
    m = re.search(r"command ([0-9a-f]{8})", content)
    expect(
        status == 200 and bool(m),
        f"Discord: a helper's /whitelist add is queued as a signed command ({content!r})",
    )
    if not m:
        return
    res = wait(
        "the agent's whitelist result",
        lambda: next((r for r in get("/__e2e/state")["results"] if r["id"].startswith(m.group(1))), None),
        120,
        3,
    )
    expect(bool(res), f"Discord: the real agent answered the helper's whitelist add ({res})")
    journal = sh(
        box,
        f"journalctl -u mc-agent --no-pager | grep -c 'action=whitelist ok=.* login=discord:{helper} (helper)'",
    )
    expect(journal.stdout.strip() not in ("", "0"), "the agent's audit names the helper and their tier")


def contention_round(box: str) -> None:
    """P13 review M1: helpers with a deadline are not starved by a saturated box. The hog is a sibling unit
    with minecraft.service's weights (CPUWeight=IOWeight=1000), two busy loops per core: to the scheduler the
    same competition as the JVM pre-generating chunks."""
    hog = "mc-e2e-hog"
    started = sh(
        box,
        f"systemd-run --unit={hog} -p CPUWeight=1000 -p IOWeight=1000 sh -c "
        "'for i in $(seq $(( $(nproc) * 2 ))); do (while :; do :; done) & done; wait'",
    )
    expect(started.returncode == 0, f"a CPU hog with the JVM's weights runs ({started.stderr.strip()[:200]})")
    if started.returncode:
        return
    try:
        t0 = time.time()
        time.sleep(15)
        b0 = time.monotonic()
        backup = sh(box, "systemctl start mc-backup.service", timeout=CONTENTION_BACKUP_MAX_S + 60)
        took = time.monotonic() - b0
        result = sh(box, "systemctl show -P Result mc-backup.service").stdout.strip()
        expect(
            backup.returncode == 0 and result == "success" and took <= CONTENTION_BACKUP_MAX_S,
            f"under full CPU contention a backup finishes in {took:.0f} s (<= {CONTENTION_BACKUP_MAX_S} s, {result})",
        )
        window = sh(
            box, "journalctl -u mc-backup.service --no-pager -o cat -n 200 | grep 'save-off window' | tail -1"
        ).stdout.strip()
        secs = float(window.split(":")[1].split()[0]) if window else float("inf")
        expect(secs <= CONTENTION_BACKUP_MAX_S, f"the save-off window stays bounded ({window or 'missing'})")
        rest = 100 - (time.time() - t0)  # keep the hog on for >= 3 metrics intervals
        if rest > 0:
            time.sleep(rest)
        t1 = time.time()
        busy_ns = int(sh(box, f"systemctl show -P CPUUsageNSec {hog}").stdout.strip() or 0)
        cpus = int(sh(box, "nproc").stdout.strip() or 1)
        expect(
            busy_ns / 1e9 >= 0.5 * cpus * (t1 - t0),
            f"the hog really saturated the box ({busy_ns / 1e9:.0f} CPU-s over {t1 - t0:.0f} s x {cpus} CPUs)",
        )
    finally:
        sh(box, f"systemctl stop {hog}")
    arrivals = [t0 * 1000] + [
        a for a in get("/__e2e/state")["seen"]["metrics_at"] if t0 * 1000 <= a <= t1 * 1000
    ]
    gap = max(b - a for a, b in zip(arrivals, [*arrivals[1:], t1 * 1000], strict=True)) / 1000
    expect(
        len(arrivals) >= 3 and gap <= CONTENTION_METRICS_GAP_S,
        f"the agent's metrics keep arriving under contention ({len(arrivals) - 1} samples, max gap {gap:.0f} s)",
    )


if __name__ == "__main__":
    if not os.environ.get("MC_SECRET_AGENT_TOKEN") or not os.environ.get("MC_SECRET_COMMAND_HMAC_KEY"):
        sys.exit("MC_SECRET_AGENT_TOKEN and MC_SECRET_COMMAND_HMAC_KEY must be set")
    sys.exit(main())
