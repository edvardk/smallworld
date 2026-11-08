"""The parity scenarios (design: the port design's §3.2 list). Each takes a fresh Scenario, drives the agent
and records notes; the harness adds what every fake saw. Keep them deterministic: wait for an event before the
next step, and mark a trace section timing-dependent (`s.skip`) rather than compare a race."""

from __future__ import annotations

import json
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from harness import (
    AGENT_TOKEN,
    HMAC_KEY,
    PYTHON,
    RCON_PASSWORD,
    SCRIPTS,
    SYNC,
    UUIDS,
    WORLDS_STATE,
    Scenario,
    norm,
    norm_sample,
    world_archive,
)

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))
import mc_panel

SCENARIOS: dict[str, Callable[[Scenario], None]] = {}


def scenario(fn: Callable[[Scenario], None]) -> Callable[[Scenario], None]:
    SCENARIOS[fn.__name__] = fn
    return fn


def signed_raw(s: Scenario, payload: str, sig: str | None = None) -> dict[str, str]:
    import hashlib
    import hmac

    return {
        "payload": payload,
        "sig": sig or hmac.new(HMAC_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest(),
    }


def payload(s: Scenario, **fields: Any) -> str:
    s.n += 1
    base = {"id": f"cmd{s.n:03d}", "action": "status", "args": {}, "login": "admin@parity.test",
            "iat": int(time.time() * 1000), "nonce": f"nonce-raw-{s.n:04d}"}  # fmt: skip
    base.update(fields)
    return json.dumps({k: v for k, v in base.items() if v is not ...}, separators=(",", ":"))


# --- 1, 2: the command envelope ---------------------------------------------------------------------------


@scenario
def verify(s: Scenario) -> None:
    """Signature, age, replay and field checks (handle_command/verify), each refusal audited."""
    s.begin()
    s.daemon()
    good = s.signed("console", {"command": "seed"})
    s.note("good", s.send(good))
    s.note("replayed", s.send(good))
    upper = s.signed("status")
    s.note("uppercase_sig", s.send(dict(upper, sig=upper["sig"].upper()))["ok"])
    bad = s.signed("status")
    s.note("bad_sig", s.send(dict(bad, sig=("0" if bad["sig"][0] != "0" else "1") + bad["sig"][1:])))
    now = int(time.time() * 1000)
    s.note("too_old", s.cmd("status", iat_ms=now - 61_000))
    s.note("from_future", s.cmd("status", iat_ms=now + 120_000))
    s.note("short_nonce", s.cmd("status", nonce="abc"))
    s.note("bad_id", s.cmd("status", cmd_id="has space"))
    s.note("not_json", s.send(signed_raw(s, "not json")))
    s.note("not_object", s.send(signed_raw(s, "[1, 2]")))
    s.note("args_list", s.send(signed_raw(s, payload(s, args=[]))))
    s.note("no_login", s.send(signed_raw(s, payload(s, login=...))))
    s.note("iat_bool", s.send(signed_raw(s, payload(s, iat=True))))
    s.note(
        "iat_float",
        s.send(
            signed_raw(s, payload(s, action="console", args={"command": "tps"}, iat=time.time() * 1000.0))
        ),
    )
    s.note("too_long", s.send(signed_raw(s, payload(s, args={"pad": "x" * 8200}))))
    s.note("sig_not_str", s.send({"payload": payload(s), "sig": 5}))  # type: ignore[dict-item]
    s.note("unknown_action", s.cmd("frobnicate", {"x": 1}))
    s.note("long_login", s.cmd("status", login="L" * 250)["ok"])
    s.note("unicode_args", s.cmd("console", {"command": "say café ☃", "z": [1, {"b": None}]}))
    s.note(
        "player_message_off",
        s.cmd("player_message", {"to": "*", "kind": "lag", "text": "hi", "id": "lag-0123abcd"}),
    )
    s.note("selftest_status_shape", s.cmd("status", {"ignored": True})["ok"])
    s.stop_daemon()


@scenario
def nonce_store(s: Scenario) -> None:
    """An unreadable nonce store refuses everything for 10 min (audited at start); an unwritable one refuses
    the command."""
    s.box.write("/var/lib/mc-agent/nonces.json", "{broken", 0o600)
    s.begin()
    s.daemon()
    s.note("unreadable", s.cmd("status"))
    s.stop_daemon("unreadable")
    s.box.host("/var/lib/mc-agent/nonces.json").unlink()
    s.box.host("/var/lib/mc-agent/nonces.json.tmp").mkdir()
    s.daemon()
    s.note("unwritable", s.cmd("status"))
    s.stop_daemon("unwritable")
    s.box.host("/var/lib/mc-agent/nonces.json.tmp").rmdir()
    s.box.write("/var/lib/mc-agent/nonces.json", json.dumps({"old-nonce-1": time.time() - 3600}), 0o600)
    s.daemon()
    s.note("pruned_store", s.cmd("status")["ok"])
    s.stop_daemon("pruned")


# --- 3: the console allowlist -----------------------------------------------------------------------------

from parity_vectors import CONSOLE_CORPUS  # noqa: E402 - one corpus for the vectors and the live agent


@scenario
def console(s: Scenario) -> None:
    """The console allowlist corpus over signed commands: what is refused, and the exact line RCON gets."""
    s.begin()
    s.daemon()
    results = []
    for line in CONSOLE_CORPUS:
        r = s.cmd("console", {"command": line})
        results.append([line, r.get("output") if r["ok"] else "REFUSED " + r["error"]])
    for bad in ({"command": 5}, {}, {"command": None}):
        r = s.cmd("console", bad)
        results.append([repr(bad), r.get("error")])
    s.note("results", results)
    moderate = [
        {"verb": "kick", "target": "Bob", "reason": "spam\nand \x07more"},
        {"verb": "ban", "target": "Griefer2", "reason": "x" * 150},
        {"verb": "ban-ip", "target": "203.0.113.9", "reason": ""},
        {"verb": "pardon", "target": "Griefer", "reason": "ignored for pardon"},
        {"verb": "pardon-ip", "target": "203.0.113.9"},
        {"verb": "kick", "target": "@a"},
        {"verb": "ban-ip", "target": "\u0662.\u0662.\u0662.\u0662"},
        {"verb": "op", "target": "Bob"},
        {"verb": "kick", "target": 5},
    ]
    s.note("moderate", [s.cmd("moderate", args) for args in moderate])
    s.stop_daemon()


# --- 4, 5: status, lookup ---------------------------------------------------------------------------------


@scenario
def status(s: Scenario) -> None:
    """`status` = one sample over the kept RCON connection (one connection shared with the metrics loop)."""
    s.rcon.players = ["Bob", "Carol"]
    s.begin()
    s.daemon()
    r = s.cmd("status")
    s.note("status", dict(r, output=norm_sample(json.loads(r["output"]))))
    s.box.write("/var/lib/minecraft/admin-stopped", {"since": "x", "reason": "test", "by": "test"})
    r = s.cmd("status")
    s.note("status_admin_stopped", json.loads(r["output"])["admin_stopped"])
    s.stop_daemon()


@scenario
def lookup(s: Scenario) -> None:
    """Java name -> UUID from the box: the first endpoint refuses (403), the second answers; cache; not found;
    unverified; the per-minute limit."""
    s.begin()
    s.daemon()
    for label, name in (("found", "Steve"), ("cached", "steve"), ("missing", "Nobody_Here"), ("bad_name", "x y"),
                        ("short", "ab"), ("none", None)):  # fmt: skip
        s.note(label, s.cmd("lookup", {"name": name} if name is not None else {}))
    s.github.mojang_status = {"a": 200, "b": 200}
    s.note("first_endpoint", s.cmd("lookup", {"name": "Carol"}))
    s.github.mojang_status = {"a": 500, "b": 503}
    s.note("unverified", s.cmd("lookup", {"name": "Dave"}))
    s.github.mojang_status = {"a": 404, "b": 200}
    rate = [s.cmd("lookup", {"name": f"Rate_{i:02d}"}) for i in range(18)]
    s.note("rate_limit", [r.get("output") or r.get("error") for r in rate])
    s.stop_daemon()


# --- 6: the Panel -> Git self-test ------------------------------------------------------------------------


@scenario
def selftest_ok(s: Scenario) -> None:
    """A passing self-test (read-only GitHub calls), cached for a minute on demand, in the inventory."""
    s.begin()
    s.daemon()
    s.note("first", s.cmd("selftest"))
    s.note("cached", s.cmd("selftest"))
    inv = s.wait_inventory(lambda i: i.get("selftest") is not None)
    s.note("inventory_selftest", norm(inv["selftest"]))
    s.stop_daemon()


@scenario
def selftest_failures(s: Scenario) -> None:
    """Each failing step, the Discord alert after two failures in a row and the one when it passes again."""
    good = {"token": "ghs_" + "T" * 36, "expires_at": "2099-01-01T00:00:00Z",
            "permissions": {"contents": "write", "pull_requests": "write"}}  # fmt: skip
    cases: list[tuple[str, list[tuple[int, Any]], bool | None, dict[str, Any] | None]] = [
        ("worker_502", [(502, {"error": "GitHub App: POST /app/installations/{id}/access_tokens: HTTP 403"})], True, None),
        ("no_token", [(200, {"error": "nothing", "expires_at": "x"})], True, None),
        ("bad_chars", [(200, dict(good, token="ghs_" + "t!" * 12 + "é"))], True, None),
        ("short_token", [(200, dict(good, token="ghs_short"))], True, None),
        ("not_object", [(200, ["token"])], True, None),
        ("long_token", [(200, dict(good, token="ghs_" + "L" * 386))], True, None),
        ("missing_perms", [(200, dict(good, permissions={"contents": "read"}))], True, None),
        ("no_perms_key", [(200, {k: v for k, v in good.items() if k != "permissions"})], True, None),
        ("push_false", [(200, good)], False, None),
        ("push_unknown", [(200, good)], None, None),
        ("alert", [(502, {"error": "down"})], True,
         {"last": {"ok": False, "trigger": "daily", "ts": 1}, "failures": 1, "alerted": False}),
        ("recovered", [(200, good)], True,
         {"last": {"ok": False, "trigger": "daily", "ts": 1}, "failures": 3, "alerted": True}),
    ]  # fmt: skip
    s.begin()
    for label, tokens, push, seed in cases:
        state = s.box.host("/var/lib/mc-agent/selftest.json")
        if seed is not None:
            s.box.write("/var/lib/mc-agent/selftest.json", seed, 0o600)
        elif state.exists():
            state.unlink()
        s.worker.github_tokens = list(tokens)
        s.github.push = push
        s.daemon()
        s.note(label, s.cmd("selftest"))
        s.stop_daemon(label)
        s.note(f"{label}_state", norm(s.box.json("/var/lib/mc-agent/selftest.json")))


# --- 7, 8: stop / start / restart / backup ----------------------------------------------------------------


@scenario
def stop_start(s: Scenario) -> None:
    """Stop (marker first), start, restart (maintenance window), the refusals, and jobs with nobody online."""
    s.begin()
    s.daemon()
    s.note("stop", s.cmd("stop", {"reason": "maintenance\nnight\x07 shift "}))
    s.note("marker", norm(s.box.json("/var/lib/minecraft/admin-stopped")))
    s.note("restart_admin_stopped", s.cmd("restart"))
    s.note("start", s.cmd("start"))
    s.box.write("/var/lib/minecraft/fenced", "fenced by mcctl migrate\n")
    s.note("start_fenced", s.cmd("start"))
    s.note("restart_fenced", s.cmd("restart"))
    s.box.host("/var/lib/minecraft/fenced").unlink()
    s.note("restart", s.cmd("restart"))
    s.note("bad_countdown", s.cmd("restart", {"countdown": 7}))
    s.note("bool_countdown", s.cmd("restart", {"countdown": True}))
    s.note("bad_backup", s.cmd("stop", {"backup": "yes"}))
    s.note("stop_job", s.cmd("stop", {"countdown": 10, "backup": True}))
    s.note("stop_job_end", norm(s.wait_job(s.last_id)))
    s.note("start_again", s.cmd("start"))
    s.note("restart_job", s.cmd("restart", {"countdown": 60, "skip_if_empty": True}))
    s.note("restart_job_end", norm(s.wait_job(s.last_id)))
    s.systemd["actions"]["start mc-backup.service"] = {"exit": 1}
    s.save_systemd()
    s.note("restart_backup_fails", s.cmd("restart", {"countdown": 0, "backup": True}))
    s.note("restart_backup_fails_end", norm(s.wait_job(s.last_id)))
    s.systemd["actions"]["restart minecraft.service"] = {"exit": 1}
    s.save_systemd()
    s.note("restart_fails", s.cmd("restart"))
    s.stop_daemon()


@scenario
def countdown_cancel(s: Scenario) -> None:
    """A countdown with players online (announcements), busy refusals, cancel, and a running job that can't be
    cancelled."""
    s.rcon.players = ["Bob", "Carol"]
    release = s.tmp / "release-backup"
    s.systemd["actions"]["start mc-backup.service"] = {"block": str(release)}
    s.save_systemd()
    s.begin()
    s.daemon()
    s.note("cancel_nothing", s.cmd("cancel"))
    s.note("restart", s.cmd("restart", {"countdown": 10}))
    job = s.last_id
    s.wait(lambda: any(j["state"] == "countdown" for j in s.jobs(job)), 10, "the countdown")
    s.note("start_busy", s.cmd("start"))
    s.note("stop_busy", s.cmd("stop", {"countdown": 0, "backup": True}))
    s.note("cancel", s.cmd("cancel"))
    s.note("cancelled", norm(s.wait_job(job)))
    s.note("restart_backup", s.cmd("restart", {"countdown": 0, "backup": True}))
    job = s.last_id
    s.wait(
        lambda: any(j.get("step", "").startswith("backing up") for j in s.jobs(job)), 10, "the backup step"
    )
    s.note("cancel_running", s.cmd("cancel"))
    release.touch()
    s.note("restart_backup_end", norm(s.wait_job(job)))
    s.note("job_reports", [norm(j) for j in s.jobs(job)])
    s.stop_daemon()


@scenario
def backup(s: Scenario) -> None:
    """`backup` starts mc-backup without blocking, waits for it in a thread, then pushes the inventory."""
    s.begin()
    s.daemon()
    first = len(s.inventories())
    s.note("backup", s.cmd("backup"))
    s.wait(
        lambda: sum(c["argv"][-1:] == ["mc-backup.service"] for c in s.calls()) >= 2, 10, "the backup wait"
    )
    inv = s.wait_inventory(lambda i: True, after=first)
    s.note("inventory_after_backup", sorted(inv))
    s.stop_daemon()


# --- 9: plugins -------------------------------------------------------------------------------------------


@scenario
def plugin(s: Scenario) -> None:
    """Plugin toggles: the checks, the job (disabled list, mc-plugins-apply, restart) and the stopped case."""
    s.begin()
    s.daemon()
    s.note("unknown", s.cmd("plugin", {"name": "nope", "enabled": False}))
    s.note("not_bool", s.cmd("plugin", {"name": "chunky", "enabled": "no"}))
    s.note("already", s.cmd("plugin", {"name": "chunky", "enabled": True}))
    s.note("bad_countdown", s.cmd("plugin", {"name": "chunky", "enabled": False, "countdown": 7}))
    first = len(s.inventories())
    s.note("disable", s.cmd("plugin", {"name": "chunky", "enabled": False}))
    s.note("disable_end", norm(s.wait_job(s.last_id)))
    inv = s.wait_inventory(
        lambda i: i.get("plugins_disabled") == ["chunky"] and (i.get("job") or {}).get("state") == "done",
        first,
    )
    s.note("inventory_after_disable", norm({k: inv[k] for k in ("plugins_disabled", "job")}))
    s.box.write("/var/lib/minecraft/admin-stopped", {"since": "x", "reason": "test", "by": "test"})
    s.note("enable_stopped", s.cmd("plugin", {"name": "chunky", "enabled": True}))
    s.note("enable_stopped_end", norm(s.wait_job(s.last_id)))
    s.stop_daemon()


# --- 10: restore ------------------------------------------------------------------------------------------


@scenario
def restore(s: Scenario) -> None:
    """Restore checks, a restore that works and one that fails (the unit's STEP lines become the result)."""
    s.systemd["actions"]["start mc-restore@aaaaaaaa.service"] = {
        "steps": [
            "STEP 1/7: safety snapshot",
            "STEP 7/7: verified",
            "RESTORED aaaaaaaa (2026-11-01T10:00:00Z)",
        ]
    }
    full = "a" * 64
    s.systemd["actions"][f"start mc-restore@{full}.service"] = {
        "exit": 1, "steps": ["STEP 1/7: safety snapshot", "FAILED: restic couldn't read the snapshot"]
    }  # fmt: skip
    s.save_systemd()
    s.begin()
    s.daemon()
    for label, snap in (("bad_id", "xyz"), ("upper", "AAAAAAAA"), ("unknown", "bbbbbbbb"), ("archive", "cccccccc"),
                        ("none", None)):  # fmt: skip
        s.note(label, s.cmd("restore", {"snapshot": snap} if snap else {}))
    s.note("ok", s.cmd("restore", {"snapshot": "aaaaaaaa"}))
    s.note("ok_end", norm(s.wait_job(s.last_id)))
    s.note("failed", s.cmd("restore", {"snapshot": full}))
    s.note("failed_end", norm(s.wait_job(s.last_id)))
    s.stop_daemon()


# --- 11: the world library --------------------------------------------------------------------------------


def worlds_after(active: str, archives: list[dict[str, Any]], deleted: list[str]) -> str:
    state = dict(WORLDS_STATE, active=active, archives=archives,
                 deleted=[{"name": d, "at": "2026-11-08T00:00:00+00:00"} for d in deleted])  # fmt: skip
    return json.dumps(state, sort_keys=True, indent=1) + "\n"


@scenario
def worlds(s: Scenario) -> None:
    """world_create/switch/delete: the checks, the request file, mc-world.service, the record for Git."""
    s.begin()
    s.daemon(CONFIG_REPO="")
    refusals = [
        ("create_active", "world_create", {"name": "main"}),
        ("create_archived", "world_create", {"name": "creative"}),
        ("create_deleted", "world_create", {"name": "gone"}),
        ("create_bad_name", "world_create", {"name": "Bad Name"}),
        ("create_bad_type", "world_create", {"name": "new1", "type": "bogus"}),
        ("create_bad_seed", "world_create", {"name": "new1", "seed": "12a"}),
        ("create_bad_border", "world_create", {"name": "new1", "border_radius": {"minecraft:overworld": -1}}),
        ("switch_active", "world_switch", {"name": "main"}),
        ("switch_unknown", "world_switch", {"name": "nope"}),
        ("switch_newer", "world_switch", {"name": "future"}),
        ("delete_active", "world_delete", {"name": "main"}),
        ("delete_unknown", "world_delete", {"name": "nope"}),
    ]
    for label, action, args in refusals:
        s.note(label, s.cmd(action, args))
    main = world_archive("main", "dddddddd", 4671)
    creative = world_archive("creative", "cccccccc", 4671)
    future = world_archive("future", "ffffffff", 9999)
    s.systemd["actions"]["start mc-world.service"] = {
        "write": {
            "/var/lib/mc-snapshots/worlds.json": worlds_after("new1", [main, creative, future], ["gone"])
        },
        "steps": ["STEP 1/5: archiving main", "STEP 5/5: new1 is running"],
    }
    s.save_systemd()
    args = {"name": "new1", "seed": "123", "type": "amplified", "preset": "survival_smp",
            "border_radius": {"minecraft:overworld": 3000}, "pregen": True}  # fmt: skip
    s.note("create", s.cmd("world_create", args))
    s.note("create_end", norm(s.wait_job(s.last_id)))
    s.note("create_request", norm(s.box.json("/var/lib/minecraft/world-request.json")))
    new1 = world_archive("new1", "eeeeeeee", 4671)
    s.systemd["actions"]["start mc-world.service"] = {
        "write": {
            "/var/lib/mc-snapshots/worlds.json": worlds_after("creative", [main, new1, future], ["gone"])
        },
        "steps": ["STEP 5/5: creative is running"],
    }
    s.save_systemd()
    s.note("switch", s.cmd("world_switch", {"name": "creative", "countdown": 0}))
    s.note("switch_end", norm(s.wait_job(s.last_id)))
    s.systemd["actions"]["start mc-world.service"] = {
        "write": {
            "/var/lib/mc-snapshots/worlds.json": worlds_after("creative", [new1, future], ["gone", "main"])
        },
        "steps": ["STEP 2/2: main forgotten"],
    }
    s.save_systemd()
    s.note("delete", s.cmd("world_delete", {"name": "main"}))
    s.note("delete_end", norm(s.wait_job(s.last_id)))
    s.systemd["actions"]["start mc-world.service"] = {
        "exit": 1,
        "steps": ["FAILED: the new world didn't start"],
    }
    s.save_systemd()
    s.note("create_fails", s.cmd("world_create", {"name": "fail1"}))
    s.note("create_fails_end", norm(s.wait_job(s.last_id)))
    first = len(s.inventories())
    inv = s.wait_inventory(lambda i: (i.get("job") or {}).get("state") == "failed", max(0, first - 1))
    s.note("inventory_worlds", norm(inv["worlds"]))
    s.stop_daemon()


# --- 12, 13: whitelist and world -> Git -------------------------------------------------------------------


def pending(s: Scenario) -> dict[str, Any]:
    return s.box.json("/var/lib/mc-agent/pending.json") or {}


@scenario
def whitelist_pr(s: Scenario) -> None:
    """Whitelist add -> pending.json -> PR (one 401: a fresh token) -> merged; remove -> PR closed -> reverted
    live + Discord; the refusals."""
    s.worker.github_tokens = [(200, {"token": "ghs_" + "F" * 36, "expires_at": "2099-01-01T00:00:00Z"})]
    s.github.unauthorized_once = True
    s.begin()
    s.daemon(**SYNC)
    s.note("bad_op", s.cmd("whitelist", {"op": "toggle", "name": "Carol"}))
    s.note("bad_name", s.cmd("whitelist", {"op": "add", "name": "x"}))
    s.note("remove_absent", s.cmd("whitelist", {"op": "remove", "name": "Nobody"}))
    s.note("remove_op", s.cmd("whitelist", {"op": "remove", "name": "alice_op"}))
    s.note("add", s.cmd("whitelist", {"op": "add", "name": "carol"}))
    s.wait_audit("panel PR #1 opened")
    s.wait(lambda: pending(s).get("pr"), 10, "the PR in pending.json")  # audited before it is merged back
    s.note("pending_with_pr", norm(pending(s)))
    s.github.merge(1)
    s.wait_audit("panel PR #1 merged")
    s.wait(lambda: not pending(s).get("entries"), 10, "the merged entries to leave pending.json")
    s.note("players_after_merge", s.github.files["main"]["players.yml"])
    s.note("remove", s.cmd("whitelist", {"op": "remove", "name": "Bob"}))
    s.wait_audit("panel PR #2 opened")
    s.github.close(2)
    s.wait_audit("panel PR #2 closed unmerged")
    s.wait(lambda: not pending(s).get("entries"), 10, "the rejected entries to leave pending.json")
    s.note("pending_after_close", norm(pending(s)))
    s.note(
        "add_unknown",
        s.cmd(
            "whitelist",
            {"op": "add", "name": "Zed"},
        ),
    )
    s.stop_daemon()


@scenario
def pending_expiry(s: Scenario) -> None:
    """A pending whitelist change older than 24 h is dropped (Discord, audit, `expired`); without a config
    repo the error says so."""
    old = time.time() - 90_000
    s.box.write("/var/lib/mc-agent/pending.json", {"entries": [
        {"uuid": UUIDS["Dave"], "name": "Dave", "op": "add", "ts": old, "login": "admin@parity.test", "pr": None},
    ], "pr": None, "error": "", "changed": old, "expired": []}, 0o600)  # fmt: skip
    s.begin()
    s.daemon(CONFIG_REPO="", **SYNC)
    s.wait_audit("panel whitelist entries expired")
    s.wait(lambda: pending(s).get("expired"), 10, "the expired entry")
    s.note("pending", norm(pending(s)))
    s.stop_daemon()


@scenario
def world_pr(s: Scenario) -> None:
    """A recorded world change -> server.yml PR (world section rewritten, comments kept) -> merged; the next
    one closed unmerged (Discord, kept, no new PR)."""
    lib = [{"name": "creative", "seed": "", "type": "flat_classic", "preset": None, "border_radius": None, "pregen": None},
           {"name": "main", "seed": "42", "type": "default", "preset": "survival_smp",
            "border_radius": {"minecraft:overworld": 5000, "minecraft:the_nether": None, "minecraft:the_end": None},
            "pregen": False}]  # fmt: skip
    old = time.time() - 600
    s.box.write("/var/lib/mc-agent/pending.json", {"entries": [], "pr": None, "error": "", "changed": old,
                "expired": [], "world": {"active": "creative", "library": lib, "what": "switch to creative",
                                         "ts": old, "login": "admin@parity.test", "pr": None, "open_pr": None,
                                         "error": ""}}, 0o600)  # fmt: skip
    s.begin()
    s.daemon(**SYNC)
    s.wait_audit("panel world PR #1 opened")
    s.note("server_yml_pr1", s.github.files[s.github.prs[1]["head"]]["server.yml"])
    s.github.merge(1)
    s.wait_audit("panel world PR #1 merged")
    s.wait(lambda: pending(s).get("world") is None, 10, "the merged world change to leave pending.json")
    mc_panel.Pending(s.box.host("/var/lib/mc-agent/pending.json")).record_world(
        "main", lib[1:], "switch to main", "admin@parity.test", time.time() - 120
    )
    s.wait_audit("panel world PR #2 opened")
    s.github.close(2)
    s.wait_audit("panel world PR #2 closed unmerged")
    s.wait(lambda: (pending(s).get("world") or {}).get("open_pr") is None, 10, "the closed world PR")
    s.note("pending_after_close", norm(pending(s)))
    s.stop_daemon()


# --- 14, 18: logs (scrubber, caps, cursor, day rollover) and the rejected-logins feed ---------------------

DAY1 = 1_794_009_600_000_000  # 2026-11-07T00:00:00Z in microseconds (journald's __REALTIME_TIMESTAMP)


def journal_line(i: int, unit: str, msg: Any, us: int, **extra: str) -> str:
    raw = {"__CURSOR": f"s=parity;i={i:05d}", "__REALTIME_TIMESTAMP": str(us), "_SYSTEMD_UNIT": unit,
           "SYSLOG_IDENTIFIER": extra.pop("ident", unit.split(".")[0]), "PRIORITY": extra.pop("prio", "6"),
           "MESSAGE": msg, **extra}  # fmt: skip
    if not unit:
        del raw["_SYSTEMD_UNIT"]
    return json.dumps(raw)


def journal_fixture() -> list[str]:
    mc = "minecraft.service"
    t = DAY1 - 120_000_000  # two minutes before midnight UTC: the D1 cap restarts at midnight
    secrets = [
        f"agent token {AGENT_TOKEN} in a line",
        f"rcon.password={RCON_PASSWORD}",
        f"hmac {HMAC_KEY}",
        "Authorization: Bearer abc.def-ghi",
        "auth basic dXNlcjpwYXNzd29yZA==",
        "RESTIC_PASSWORD=hunter2hunter2 and HEARTBEAT_TOKEN='quoted value' ok",
        'json {"api_key": "s3cr3t-value", "x_secret":"v2"} end',
        "my.db.pass: plain",
        "aws AKIA0189ABCD0189EFGH and ASIA0189QRST0189UVWXX stays",
        "gh ghp_" + "a" * 36 + " and github_pat_" + "b" * 30,
        "ts tskey-auth-kAbC123-xyz",
        "hook https://discord.com/api/webhooks/1/abcdef rest",
        "url https://user:pw@example.com/x and ftp://u:p@h/",
        "token_bucket=5 tokenizer=no",
        "x_key= spaced",
        "été Bearer ééé ok",
    ]
    lines = []
    i = 0

    def add(unit: str, msg: Any, dt: int = 1_000_000, **extra: str) -> None:
        nonlocal i, t
        i += 1
        t += dt
        lines.append(journal_line(i, unit, msg, t, **extra))

    add(mc, "[23:58:01 INFO]: UUID of player Mallory is 66666666-6666-4666-8666-666666666666")
    add(
        mc,
        "[23:58:01 INFO]: Disconnecting Mallory (/203.0.113.7:51234): You are not whitelisted on this server!",
    )
    add(
        mc,
        "[23:58:02 INFO]: Mallory (/203.0.113.7:51234) lost connection: You are not whitelisted on this server!",
    )
    add(mc, "[23:58:03 INFO]: <Eve> [23:58:03 INFO]: Disconnecting Bob (/192.0.2.4:5): not whitelisted")
    add(
        mc,
        "\x1b[0m[23:58:04 INFO]: Disconnecting Trudy (/[2001:db8:1:2::5]:4444): You are not white-listed on this server!",
    )
    for text in secrets:
        add(mc, text)
    add("sshd.service", f"Accepted password for root: {AGENT_TOKEN}", ident="sshd")
    add("", "mc-setfull: IN=eth0 SRC=198.51.100.9 DST=10.0.0.1 PROTO=TCP DPT=25565", ident="kernel", prio="4")
    add("", "other kernel line", ident="kernel")
    add(
        "mc-agent.service",
        "agent action=status ok=True login=admin",
        ident="mc-agent",
        prio="5",
        MC_AUDIT="1",
    )
    add("mc-backup.service", [104, 105, 32, 255, 254, 33], ident="mc-backup")
    add("mc-backup.service", "x" * 9000, ident="mc-backup")
    add("init.scope", "mc-backup.service: Consumed 2.1s CPU time.", ident="systemd", UNIT="mc-backup.service")
    add("mc-world@x.service", "templated unit", ident="mc-world")
    add(mc, "bad ts")
    lines[-1] = lines[-1].replace(f'"__REALTIME_TIMESTAMP": "{t}"', '"__REALTIME_TIMESTAMP": "soon"')
    lines.append("not json at all")
    lines.append(json.dumps([1, 2, 3]))
    while len(lines) < 4990:  # a backlog: more than one batch (LOG_BATCH_MAX_ENTRIES = 5000)
        add(mc, f"[23:59:00 INFO]: tick {i}", dt=20_000)
    for k in range(25):  # across midnight UTC: the next day's D1 cap
        add(mc, f"[00:00:{k:02d} INFO]: after midnight {k}", dt=5_000_000)
    return lines


@scenario
def logs_rejected(s: Scenario) -> None:
    """One log round over a backlog: archive (scrubbed, product units + firewall lines only), D1 rows (capped
    per UTC day), the cursor; the rejected-logins feed from the same entries, then rejected_dismiss."""
    fixture = s.tmp / "journal.jsonl"
    lines = journal_fixture()
    fixture.write_text("\n".join(lines) + "\n")
    s.systemd["journal"] = str(fixture)
    s.save_systemd()
    s.begin()
    s.daemon(LOG_BATCH_S="10", LOG_MAX_ROWS_PER_DAY="4")
    last_cursor = json.loads(lines[-1])["__CURSOR"]
    s.wait(lambda: (s.box.json("/var/lib/mc-agent/logs.json") or {}).get("cursor") == last_cursor, 40,
           "the log round to reach the end of the journal")  # fmt: skip
    first = len(s.inventories())
    inv = s.wait_inventory(lambda i: bool(i.get("rejected")), max(0, first - 1))
    s.note("inventory_rejected", inv["rejected"])
    s.note("dismiss", s.cmd("rejected_dismiss", {"name": "mallory"}))
    s.note("dismiss_again", s.cmd("rejected_dismiss", {"name": "Mallory"}))
    s.note("dismiss_bad", s.cmd("rejected_dismiss", {"name": "a b"}))
    s.stop_daemon()


# --- 20: the WebSocket ------------------------------------------------------------------------------------


@scenario
def ws_reconnect(s: Scenario) -> None:
    """hello, a command over the socket, the Worker closing it: one poll, then a new socket ~5 s later."""
    s.begin()
    s.daemon()
    s.note("over_ws", s.cmd("console", {"command": "seed"}))
    polls = len(s.worker.requests_to("GET /agent/commands"))
    session = s.worker.session()
    assert session is not None
    session.close(1000)
    s.wait(lambda: len([e for e in s.worker.events if e["event"] == "ws-hello"]) >= 2, 20, "the reconnect")
    s.note("polls_between", len(s.worker.requests_to("GET /agent/commands")) - polls)
    s.note("after_reconnect", s.cmd("console", {"command": "tps"}))
    s.stop_daemon()


@scenario
def poll_fallback(s: Scenario) -> None:
    """The Worker refuses the upgrade: poll at once (results in one POST), again POLL_S later; no second
    WebSocket attempt for 5 minutes; `tail` needs the socket."""
    s.worker.ws_mode = "refuse"
    s.worker.queue = [s.signed("console", {"command": "seed"}), s.signed("tail", {"seconds": 30}),
                      "junk", s.signed("console", {"command": "mspt"})]  # type: ignore[list-item]  # fmt: skip
    s.begin()
    s.daemon(ready="poll")
    s.wait(lambda: s.worker.requests_to("POST /agent/results"), 10, "the first results")
    s.note("second", s.cmd("console", {"command": "tps"}))
    s.note("ws_attempts", len([e for e in s.worker.events if e["event"] == "ws-refused"]))
    s.stop_daemon()


@scenario
def tail(s: Scenario) -> None:
    """`tail` streams minecraft.service's journal (journalctl -f) over the socket, scrubbed, for 10-180 s."""
    us = DAY1
    s.systemd["follow"] = [
        journal_line(1, "minecraft.service", "[10:00:00 INFO]: Done (3.2s)!", us),
        journal_line(2, "minecraft.service", f"[10:00:01 INFO]: leaked {AGENT_TOKEN}", us + 1000, prio="4"),
        journal_line(3, "minecraft.service", "Bearer abc.def", us + 2000),
        "not json",
        journal_line(4, "minecraft.service", "x" * 2100, us + 3000),
    ]
    s.save_systemd()
    s.begin()
    s.daemon()
    s.note("bad_seconds", s.cmd("tail", {"seconds": "10"}))
    s.note("tail", s.cmd("tail", {"seconds": 5}))
    s.wait(lambda: sum(len(m.get("lines") or []) for m in s.worker.console) >= 4, 10, "the console lines")
    s.note("lines", [line for m in s.worker.console for line in m["lines"]])
    s.note("again", s.cmd("tail", {"seconds": 999}))
    s.stop_daemon()
    s.worker.console = []  # message boundaries depend on pipe reads: the flattened lines are the note


# --- 17: /modpack client codes ----------------------------------------------------------------------------


@scenario
def client_codes(s: Scenario) -> None:
    """Request files -> POST /agent/client-code -> the code file (0640): a code, the Worker's 404/429/odd
    answers, the hourly limit; malformed requests are ignored."""
    s.worker.client_codes = [
        (200, {"code": "123456", "expires_ms": 1_800_000_000_000}),
        (404, {"error": "the client modpack is off"}),
        (429, {"error": "slow down"}),
        (200, {"code": "12x456", "expires_ms": 1}),
        (401, {"error": "unauthorized"}),
    ] + [(200, {"code": f"{k:06d}", "expires_ms": 1_800_000_000_000}) for k in range(15)]
    reqdir = s.box.host("/run/mc-telemetry/client")
    for k in range(22):
        uuid = f"{k:08x}-0000-4000-8000-000000000000"
        (reqdir / f"{uuid}.req").write_text(f"mc-client-request 1\n{uuid}\tPlayer{k}\t{k:032x}\t1700000000\n")
    (reqdir / "junk.req").write_text("mc-client-request 1\nnope\n")
    other = "ffffffff-0000-4000-8000-000000000000"
    (reqdir / f"{other}.req").write_text(
        f"mc-client-request 1\n00000000-0000-4000-8000-000000000001\tX\t{0:032x}\t1\n"
    )
    (reqdir / ".hidden.req").write_text("x")
    s.begin()
    s.daemon(CLIENT="1")
    code_file = s.box.host("/run/mc-verdicts/client-codes.tsv")
    s.wait(lambda: code_file.exists() and len(code_file.read_text().splitlines()) == 23, 20, "the code file")
    s.stop_daemon()


# --- 21: auth and settings --------------------------------------------------------------------------------


@scenario
def auth_settings(s: Scenario) -> None:
    """A wrong agent token: every request gets 401 and the agent keeps running; bad settings exit 2."""
    s.begin()
    s.daemon(ready="poll", AGENT_TOKEN="WrongTokenParity-0123456789abcdefABCDEF")
    s.wait(lambda: s.worker.requests_to("POST /ingest/inventory") and s.worker.requests_to("POST /ingest/metrics"),
           10, "the refused pushes")  # fmt: skip
    s.note("routes", sorted({r["route"] for r in s.worker.requests}))
    s.note("ws", [e["event"] for e in s.worker.events])
    s.stop_daemon("wrong_token")
    s.worker.requests.clear()
    s.worker.events.clear()
    for label, env in (("short_token", {"AGENT_TOKEN": "short"}), ("short_key", {"COMMAND_HMAC_KEY": "k" * 31}),
                       ("plain_http", {"INGEST_URL": "http://example.com"}), ("no_url", {"INGEST_URL": ""}),
                       ("ftp", {"INGEST_URL": "ftp://127.0.0.1"}), ("bad_int", {"POLL_S": "x"})):  # fmt: skip
        proc = s.daemon(ready="none", **env)
        s.exits.append({label: proc.wait(30)})
    s.note("requests_after_bad_settings", len(s.worker.requests))


# --- 22: the cli as mcctl runs it -------------------------------------------------------------------------


def mcctl_argv(login: str, action: str, args: dict[str, Any] | None = None) -> list[str]:
    """What follows the agent's entry point in mcctl's SSH command (mcctl/actions.py agent_argv, which
    agent_command runs)."""
    from mcctl.actions import AGENT_ENTRY, agent_argv

    argv = agent_argv(login, action, args)
    rest = argv[argv.index(AGENT_ENTRY) + 1 :]
    assert rest[0] == "cli", rest
    return rest[1:]


@scenario
def cli(s: Scenario) -> None:
    """Every cli action and list as mcctl/actions.py builds them, the usage errors, the typed confirmations,
    a foreground job, SIGTERM during a countdown, a box without the agent's state dir."""
    login = "mcctl:alex"
    s.systemd["actions"]["start mc-world.service"] = {
        "write": {"/var/lib/mc-snapshots/worlds.json": worlds_after(
            "creative", [world_archive("main", "dddddddd", 4671), world_archive("future", "ffffffff", 9999)], ["gone"])},
        "steps": ["STEP 5/5: creative is running"],
    }  # fmt: skip
    s.save_systemd()
    s.begin()
    for label, argv in (("usage_none", []), ("usage_login", ["alex", "restart"]),
                        ("usage_action", [login, "frobnicate"]), ("usage_json", [login, "restart", "{bad"]),
                        ("usage_array", [login, "restart", "[1]"]), ("usage_extra", [login, "restart", "{}", "x"])):  # fmt: skip
        s.cli_run(label, argv)
    s.cli_run("plugins", mcctl_argv(login, "plugins"))
    s.cli_run("worlds", mcctl_argv(login, "worlds"))
    s.cli_run("kick", mcctl_argv(login, "moderate", {"verb": "kick", "target": "Bob", "reason": "spam\nnow"}))
    s.cli_run("ban_ip_digits", mcctl_argv(login, "moderate", {"verb": "ban-ip", "target": "٢.٢.٢.٢"}))
    s.cli_run("pardon", mcctl_argv(login, "moderate", {"verb": "pardon", "target": "Griefer"}))
    s.cli_run("restart", mcctl_argv(login, "restart"))
    s.cli_run("plugin_unconfirmed", mcctl_argv(login, "plugin", {"name": "chunky", "enabled": False}))
    s.cli_run(
        "plugin",
        mcctl_argv(login, "plugin", {"name": "chunky", "enabled": False, "confirm": "disable chunky"}),
    )
    s.cli_run("world_delete_unconfirmed", mcctl_argv(login, "world_delete", {"name": "creative"}))
    s.cli_run(
        "world_switch",
        mcctl_argv(login, "world_switch", {"name": "creative", "confirm": "switch to creative"}),
    )
    s.cli_run("world_no_agent", mcctl_argv(login, "world_create", {"name": "x1", "confirm": "create x1"}),
              layout=s.layout_without("/var/lib/mc-agent"))  # fmt: skip
    s.rcon.players = ["Bob"]
    proc = s.cli(mcctl_argv(login, "restart", {"countdown": 10}))
    s.wait(lambda: any(line.startswith("countdown:") for line in proc.stdout), 20, "the cli's countdown")
    proc.signal(signal.SIGTERM)
    s.note("restart_sigterm", {"exit": proc.wait(30), "stdout": proc.stdout})


# --- 23: locks across processes (cli <-> daemon) and pending.json writers ---------------------------------


@scenario
def locks(s: Scenario) -> None:
    """job.lock between the daemon and the cli (both ways), then pending.json with two writer processes,
    the cli's world record and the daemon's sync loop at once: nothing recorded is lost."""
    s.rcon.players = ["Bob"]
    s.begin()
    s.daemon(**SYNC)
    s.note("daemon_restart", s.cmd("restart", {"countdown": 10}))
    job = s.last_id
    s.wait(lambda: any(j["state"] == "countdown" for j in s.jobs(job)), 10, "the daemon's countdown")
    s.cli_run("cli_busy", mcctl_argv("mcctl:alex", "restart", {"countdown": 10}))
    s.note("daemon_cancel", s.cmd("cancel"))
    s.wait_job(job)
    proc = s.cli(mcctl_argv("mcctl:alex", "restart", {"countdown": 10}))
    s.wait(lambda: any(line.startswith("countdown:") for line in proc.stdout), 20, "the cli's countdown")
    s.note("daemon_busy", s.cmd("restart", {"countdown": 10}))
    s.note("daemon_cancel_other", s.cmd("cancel"))
    proc.signal(signal.SIGTERM)
    s.note("cli_cancelled", {"exit": proc.wait(30), "stdout": proc.stdout})
    s.note("daemon_after", s.cmd("restart", {"countdown": 0, "backup": True}))
    s.wait_job(s.last_id)

    # pending.json: the e2e cli round's writers (mc_panel.Pending in other processes) while the sync works
    s.rcon.players = []
    path = s.box.host("/var/lib/mc-agent/pending.json")
    s.systemd["actions"]["start mc-world.service"] = {
        "write": {"/var/lib/mc-snapshots/worlds.json": worlds_after(
            "creative", [world_archive("main", "dddddddd", 4671)], ["gone"])},
    }  # fmt: skip
    s.save_systemd()
    writer = (
        "import sys, time\nfrom pathlib import Path\nsys.path.insert(0, sys.argv[4])\nimport mc_panel\n"
        "w, n, gap = int(sys.argv[1]), int(sys.argv[2]), float(sys.argv[3])\n"
        "pend = mc_panel.Pending(Path(sys.argv[5]))\n"
        "for j in range(n):\n"
        "    pend.record('add', f'Cli{w}_{j:02d}', f'c1100000-0000-4000-8000-{w:04x}{j:08x}', 'mcctl:alex', time.time())\n"
        "    time.sleep(gap)\n"
    )
    import subprocess

    procs = [subprocess.Popen([sys.executable, "-I", "-c", writer, str(w), "15", "0.05", str(SCRIPTS), str(path)])
             for w in (1, 2)]  # fmt: skip
    world = s.cli(
        mcctl_argv("mcctl:alex", "world_switch", {"name": "creative", "confirm": "switch to creative"})
    )
    stop = threading.Event()

    def merger() -> None:
        while not stop.is_set():
            for n, pr in list(s.github.prs.items()):
                if pr["state"] == "open":
                    s.github.merge(n)
            time.sleep(0.2)

    t = threading.Thread(target=merger, daemon=True)
    t.start()
    codes = [p.wait(60) for p in procs]
    s.note("writers", codes)
    s.note("cli_world", {"exit": world.wait(60), "result": world.stdout[-1:]})
    try:
        s.wait(lambda: path.exists() and not pending(s).get("entries") and pending(s).get("world") is None
               and not pending(s).get("pr"), 40, "pending.json to drain into Git")  # fmt: skip
    finally:
        stop.set()
        t.join(5)
    s.note("players_on_main", s.github.files["main"]["players.yml"])
    s.note("world_on_main", s.github.files["main"]["server.yml"])
    s.note("pending_final", norm(pending(s)))
    s.stop_daemon()
    # which entries landed in which pull request depends on timing: the end state is the comparison
    s.skip |= {"github", "worker", "audit", "files", "rcon", "systemd"}


# --- 24: state round trip Python -> impl -> Python --------------------------------------------------------


@scenario
def state_roundtrip(s: Scenario) -> None:
    """One state dir through Python, the implementation under test and Python again: nonces (replays stay
    refused), pending entries, rejected logins, the self-test state, the log cursor."""
    s.box.write("/var/lib/mc-agent/rejected.json", [{"name": "Mallory", "uuid": None, "count": 2, "first": 1,
                                                     "last": 2, "ip": "203.0.113.x"}], 0o600)  # fmt: skip
    s.box.write("/var/lib/mc-agent/selftest.json", {"last": {"ok": False, "trigger": "daily", "ts": 1,
                                                             "configured": True, "error": "token: x", "steps": []},
                                                    "failures": 1, "alerted": False}, 0o600)  # fmt: skip
    s.box.write(
        "/var/lib/mc-agent/logs.json", {"cursor": "s=parity;i=00009", "day": "20261108", "rows": 3}, 0o600
    )
    s.begin()
    phases = [("python_1", PYTHON), ("impl", s.impl), ("python_2", PYTHON)]
    sent: list[dict[str, str]] = []
    for label, impl in phases:
        before = len(s.inventories())
        s.daemon(impl=impl, CONFIG_REPO="")
        s.note(f"{label}_inventory", norm({k: v for k, v in s.wait_inventory(lambda i: True, before).items()
                                           if k in ("pending", "rejected", "selftest", "whitelist_sync")}))  # fmt: skip
        s.note(f"{label}_replays", [s.send(m)["error"] for m in sent])
        msg = s.signed("whitelist", {"op": "add", "name": ["Carol", "Dave", "Carol"][len(sent)]})
        s.note(f"{label}_whitelist", s.send(msg))
        sent.append(msg)
        s.stop_daemon(label)
    s.note("final_pending", norm(pending(s)))


# --- P13: the experience telemetry in-process -------------------------------------------------------------


@scenario
def experience(s: Scenario) -> None:
    """EXPERIENCE=1: the agent runs the telemetry in its own process (Python: a thread of mc_agent.py; Rust: the
    telemetry module in smallworld-agent's daemon, v1.4, no sidecar): the plugin's snapshots become minute rows
    for the Worker, and player_message reads who is online from that telemetry (tips, unknown players, rate
    limits) and queues messages.tsv for the plugin. tests/telemetry_parity proves the module in depth."""
    t0 = 1_768_471_200_000  # 2026-01-15T10:00:00Z, a minute boundary in the past: rows close at once
    alice, bob = UUIDS["Alice_Op"], UUIDS["Bob"]

    def pl(u: str, n: str, **kw: Any) -> dict[str, Any]:
        return {"u": u, "n": n, "ping": 40, "brand": "vanilla", "view": 10, "send": 10, "loc": "en_us",
                "w": "world", "cx": 0, "cz": 0, "ent": 12, "glide": False, "tips": True, "join": t0 - 600_000,
                **kw}  # fmt: skip

    def snap(seq: int, ts: int, players: list[dict[str, Any]], **srv: Any) -> dict[str, Any]:
        base = {"tps": 20.0, "ticks": 200, "mean": 12.5, "p50": 11.0, "p95": 20.0, "p99": 30.0, "p999": 40.0,
                "max": 45.0, "spikes": [], "heap_used_mb": 2048, "heap_max_mb": 6656,
                "worlds": [{"n": "world", "chunks": 400, "entities": 300, "tiles": 50, "ticking": 40,
                            "players": len(players)}]}  # fmt: skip
        return {"v": 1, "seq": seq, "ts": ts, "win_ms": 10_000, "srv": base | srv, "pl": players}

    s.box.write("/var/lib/mc-agent/telemetry-id.json", json.dumps({"src": "0badc0de", "seq": 3}), 0o600)
    players = [pl(alice, "Alice_Op", ping=35), pl(bob, "Bob", tips=False, ent=90)]
    for i, (ts, who, extra) in enumerate([
        (t0 + 5_000, players, {}),
        (t0 + 15_000, players, {"spikes": [[t0 + 16_000, 75.55]], "max": 75.55}),
        (t0 + 65_000, players, {}),
    ], 1):  # fmt: skip
        s.box.write(f"/run/mc-telemetry/snap-{i}.json", json.dumps(snap(i, ts, who, **extra)), 0o640)
    s.begin()
    s.daemon(
        EXPERIENCE="1", SEGMENT_S="3600", TELEMETRY_DIR="/run/mc-telemetry", VERDICT_DIR="/run/mc-verdicts"
    )
    s.wait(lambda: len(s.worker.requests_to("POST /ingest/experience")) >= 2, 30, "two minute rows")
    msg = {"kind": "lag", "text": "Lag spike\tfrom the farm", "hover": "/lag for details"}
    s.note("to_alice", s.cmd("player_message", msg | {"to": alice, "id": "lag-0123abcd"}))
    s.note("alice_again", s.cmd("player_message", msg | {"to": alice, "id": "lag-0123abce"}))
    s.note("to_bob_tips_off", s.cmd("player_message", msg | {"to": bob, "id": "lag-0123abcf"}))
    s.note("to_carol_offline", s.cmd("player_message", msg | {"to": UUIDS["Carol"], "id": "lag-0123abd0"}))
    s.note("broadcast", s.cmd("player_message", msg | {"to": "*", "kind": "server", "id": "srv-0123abcd"}))
    s.note(
        "broadcast_again", s.cmd("player_message", msg | {"to": "*", "kind": "server", "id": "srv-0123abce"})
    )
    tsv = s.box.host("/run/mc-verdicts/messages.tsv")
    s.note("messages_tsv", {"mode": oct(tsv.stat().st_mode & 0o777),
                            "text": re.sub(r"\t\d{13}\t", "\t<expires>\t", tsv.read_text())})  # fmt: skip
    tsv.unlink()  # its expiry times are wall-clock: compared normalised above
    s.stop_daemon()
