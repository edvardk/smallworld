"""Snapshots of every alert the product sends (docs/operations.md "Alerts"), in its one plain-English template.

Each alert is built from fixed inputs (no network, no clock) and compared with
`tests/fixtures/alerts/<source>.<name>.txt`. A wording change is deliberate: regenerate with
`UPDATE_ALERT_SNAPSHOTS=1 pytest tests/test_alert_texts.py` and review the diff. Sources: the box's scripts
(health.py, the reboot gate, mc_rcon's deploy restart, mc_panel's whitelist -> Git flow), CI (ci_alert.py,
`mcctl expiry-check`, the update pull requests) and the watchdog Worker (`WORKER_ALERTS`, through node).
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import subprocess
from types import SimpleNamespace
from typing import Any

import alerts
import backup_check
import ci_alert
import health
import mc_panel
import mc_rcon
import pytest
import reboot_gate as rg
from alerts import Verdict
from conftest import REPO
from health import Ctx, GcStats, HostStats, ServiceState, State

from mcctl import configrepo, expirycheck, updates

SNAP = REPO / "tests" / "fixtures" / "alerts"
UPDATE = os.environ.get("UPDATE_ALERT_SNAPSHOTS") == "1"
CFG = {"mspt_alert": 45, "mspt_checks": 3, "heap_pct": 85, "heap_checks": 2, "gc_pause_ms": 50}
HOST_CFG = {"disk_pct": 85, "steal_pct": 10, "steal_checks": 3}
RUN = "https://github.com/example-owner/example-config/actions/runs/123"
SECRETS = re.compile(
    r"(?i)bearer |token=|password|discord(?:app)?\.com/api/webhooks|\b(?:\d{1,3}\.){3}\d{1,3}\b"
)


def _health() -> dict[str, str]:
    out: dict[str, str] = {}
    first, back = Verdict("first"), Verdict("recovered", bad_for_s=5400, worst=80.0)
    remind = Verdict("remind", suppressed=11, worse=True, bad_for_s=7 * 3600, worst=80.0)
    busy = Ctx(players=0, pregen="world")
    _, _, m = health.evaluate(State(status="ok"), False, None, None, GcStats(), CFG,
                              Ctx(now=1000, players=2, log_hint="java.lang.OutOfMemoryError: Java heap space"))  # fmt: skip
    out["down"] = m[0]
    _, _, m = health.evaluate(State(status="down", down_since=1000), True, 20.0, 20.0, GcStats(), CFG,
                              Ctx(now=1600, players=0))  # fmt: skip
    out["back"] = m[0]
    out["lag"] = health.lag_alert(first, 62.5, 16.0, CFG, Ctx(players=3))
    out["lag-reminder"] = health.lag_alert(remind, 80.0, 12.5, CFG, Ctx(players=3))
    out["lag-pregen"] = health.lag_alert(first, 62.5, 16.0, CFG, busy)
    out["lag-recovered"] = health.lag_alert(back, 30.0, 20.0, CFG, Ctx())
    g1 = GcStats(max_pause_ms=240.0, long_pauses=[240.0, 130.0], collector="g1", heap_after_pct=70.0)
    out["gc-g1"] = health.gc_alert(first, g1, CFG, Ctx(players=2))
    zgc = GcStats(collector="zgc", stalls=[40.0, 30.0, 50.0], heap_after_pct=90.0)
    out["gc-zgc-pregen"] = health.gc_alert(first, zgc, CFG, busy)
    out["gc-recovered"] = health.gc_alert(Verdict("recovered", bad_for_s=3600, worst=240.0), g1, CFG, Ctx())
    out["heap"] = health.heap_alert(first, GcStats(heap_after_pct=91.0), CFG, Ctx(players=1))
    out["heap-recovered"] = health.heap_alert(
        Verdict("recovered", bad_for_s=3600, worst=93.0), GcStats(), CFG, Ctx()
    )
    today = datetime.date(2026, 11, 5)
    st, _, m = health.evaluate_host(State(), HostStats(disk_pct=91.0), HOST_CFG, today, Ctx(now=1))
    out["disk"] = m[0]
    _, _, m = health.evaluate_host(st, HostStats(disk_pct=60.0), HOST_CFG, today, Ctx(now=7201))
    out["disk-recovered"] = m[0]
    st, cpu, msgs = State(), [0] * 8, []
    for i in range(4):
        cpu = [c + d for c, d in zip(cpu, [70, 0, 10, 0, 0, 0, 0, 20], strict=True)]
        st, _, m = health.evaluate_host(st, HostStats(cpu=cpu), HOST_CFG, today, Ctx(now=i * 300, players=4))
        msgs += m
    out["steal"] = msgs[0]
    cpu = [c + d for c, d in zip(cpu, [90, 0, 10, 0, 0, 0, 0, 0], strict=True)]
    _, _, m = health.evaluate_host(st, HostStats(cpu=cpu), HOST_CFG, today, Ctx(now=3900))
    out["steal-recovered"] = m[0]
    loop = ServiceState(active_state="activating", result="start-limit-hit")
    out["crash-loop"] = health.decide_heal(State(), False, loop, 1000).alerts[0]
    failed = ServiceState(active_state="failed")
    out["heal-failed"] = health.decide_heal(State(heal_pending=True), False, failed, 1000).alerts[0]
    gave_up = health.decide_heal(State(heals=[700.0, 800.0, 900.0]), False, failed, 1000)
    out["heal-gave-up"] = gave_up.alerts[0]
    out["heal-again"] = health.decide_heal(State(heals=[800.0, 900.0]), False, failed, 1000).alerts[0]
    out["restart-failed"] = health.restart_failed_alert("unit failed")
    n = health.WATCHDOG_UNREACHABLE_CHECKS
    _, m = health.evaluate_watchdog(State(watchdog_unreachable=n - 1), True, None)
    out["watchdog-unreachable"] = m[0]
    out["watchdog-reachable"] = health.evaluate_watchdog(
        State(watchdog_unreachable_alerted=True), True, {"last_run_age_s": 60}
    )[1][0]
    out["watchdog-stale"] = health.evaluate_watchdog(State(), True, {"last_run_age_s": 1800})[1][0]
    out["watchdog-checking"] = health.evaluate_watchdog(
        State(watchdog_stale_alerted=True), True, {"last_run_age_s": 60}
    )[1][0]
    return out


def _box_other() -> dict[str, str]:
    now = datetime.datetime(2026, 11, 5, 12, 0, tzinfo=datetime.UTC)
    facts = rg.Facts(now=now, required=True, boot_id="0123456789abcdef", server_active=True, players=2,
                     backup_running=False, restore_running=False, admin_stopped=False, maintenance=None)  # fmt: skip
    decision, _ = rg.decide(facts, {"since": now.timestamp() - 4 * 86400}, rg.parse_window("04:30-06:00"))
    assert decision.warn
    return {
        "reboot-gate.pending": decision.warn,
        "reboot-gate.rebooting": rg.rebooting_alert("in the reboot window (04:30-06:00), nobody online"),
        "reboot-gate.back": rg.back_alert("0123456789abcdef"),
        "mc_rcon.deploy-restart": mc_rcon.restart_alert(60, 3),
        "mc_panel.whitelist-expired": mc_panel.expired_alert("add Player_One, remove Player_Two"),
        "mc_panel.whitelist-rejected": mc_panel.rejected_alert(7, ["remove Player_One"], []),
        "mc_panel.whitelist-rejected-partly": mc_panel.rejected_alert(8, [], ["add Player_Two"]),
        "mc_panel.whitelist-kept": mc_panel.kept_alert(["Player_One", "Player_Two"]),
        "mc_panel.selftest-failing": mc_panel.selftest_alert(
            "token: the Worker returned no usable GitHub token (390 characters, unexpected '!')", 2
        ),
        "mc_panel.selftest-back": mc_panel.selftest_back_alert(3),
        "backup_check.failed": backup_check.alert(
            "restore", "latest snapshot 1a2b3c4d is 30.0 h old (> 2 h): backups have stopped"
        ),
    }


def _ci() -> dict[str, str]:
    repo: Any = SimpleNamespace(server=SimpleNamespace(name="mc"))
    findings = expirycheck.Findings(
        problems=[
            "credential cloudflare-admin expires in 17 days (2026-11-22): renew it, then `mcctl doctor`",
            "domain example.org expires in 22 days (2026-11-27): renew it",
            "watchdog: cron isn't running (last run 60 min ago)",
        ],
        notes=["credential github-admin: expires 2027-06-01"],
    )
    server = "server:\n  project: paper\n  version: 1.21.10\n  build: 130\n"
    plugins = "plugins:\n  - name: Chunky\n    version: 1.4.40\n"
    locks = {configrepo.SERVER_LOCK: server, configrepo.PLUGINS_LOCK: plugins}
    repo_name = "example-owner/example-product"
    return {
        "ci_alert.backup-check": ci_alert.alert("backup-check", RUN),
        "ci_alert.deploy": ci_alert.alert("deploy", RUN),
        "expiry-check.problems": expirycheck.alert(repo, findings, RUN),
        "update-pr.lock-auto-merge": updates.lock_body(locks, True),
        "update-pr.lock-manual": updates.lock_body(locks, False),
        "update-pr.product-patch": updates.product_body("v1.2.0", "v1.2.1", repo_name, "a" * 40, True, True),
        "update-pr.product-minor": updates.product_body(
            "v1.2.1", "v1.3.0", repo_name, "b" * 40, False, False
        ),
    }


WORKER_JS = """
import worker from "./watchdog/worker.js";
const A = worker._internals.WORKER_ALERTS;
const c = { w: "world", cx: 12, cz: -3, ent: 412, vil: 40, items: 220, tiles: 18 };
const out = {
  "probe-down-network": A.probeDown({ label: "IPv6", other: true, hostUp: true, agent: true, runs: 2, error: "timeout" }),
  "probe-down-host": A.probeDown({ label: "IPv4", other: false, hostUp: false, agent: false, runs: 2, error: "connection refused" }),
  "probe-back": A.probeBack({ label: "IPv4", version: "Paper 1.21.10", downForMs: 25 * 60000, online: 2 }),
  "backup-late": A.checkLate({ name: "backup", lateMs: 95 * 60000, hostUp: true, periodMinutes: 60 }),
  "health-late": A.checkLate({ name: "health", lateMs: 16 * 60000, hostUp: false, periodMinutes: 5 }),
  "backup-too-long": A.checkTooLong({ name: "backup", runningMs: 65 * 60000, maxMinutes: 60 }),
  "backup-failed": A.checkFailed({ name: "backup", detail: "backup failed: restic exit 1" }),
  "backup-back": A.checkBack({ name: "backup", lastOkAgoMs: 121 * 60000 }),
  "health-back": A.checkBack({ name: "health", lastOkAgoMs: 22 * 60000 }),
  "cron-failing": A.cronFailing({ error: "D1_ERROR: database unavailable" }),
  "cron-back": A.cronBack({ since: Date.UTC(2026, 10, 5, 11, 0) }),
  "housekeeping-failed": A.housekeepingFailed({ what: "experience", error: "D1_ERROR: too many statements" }),
  "crowded-chunk": A.crowdedChunk({ c, online: 3, limit: 300 }),
};
console.log(JSON.stringify(out));
"""


def _worker() -> dict[str, str]:
    proc = subprocess.run(
        ["node", "--import", "./watchdog/test/register.mjs", "--input-type=module", "-e", WORKER_JS],
        cwd=REPO, capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _all() -> dict[str, str]:
    out = {f"health.{k}": v for k, v in _health().items()}
    out |= _box_other() | _ci()
    if shutil.which("node"):
        out |= {f"worker.{k}": v for k, v in _worker().items()}
    return out


@pytest.fixture(scope="module")
def texts() -> dict[str, str]:
    return _all()


def test_every_alert_matches_its_snapshot(texts: dict[str, str]) -> None:
    if UPDATE:
        SNAP.mkdir(parents=True, exist_ok=True)
        for name, text in texts.items():
            (SNAP / f"{name}.txt").write_text(text + "\n")
    stored = {p.stem for p in SNAP.glob("*.txt")}
    if shutil.which("node"):
        assert stored == set(texts), "stale or missing snapshots: regenerate (see the module docstring)"
    for name, text in texts.items():
        assert (SNAP / f"{name}.txt").read_text() == text + "\n", name


def test_every_alert_is_in_the_template(texts: dict[str, str]) -> None:
    """What happened (headline), what it means for players (Impact), what to do (with a command or a link)."""
    for name, text in texts.items():
        assert not SECRETS.search(text), name
        if name.startswith("update-pr."):  # a pull request body: the same three questions, as sections
            for label in ("**What changed:**", "**What it means for players:**", "**What to do:**"):
                assert label in text, (name, label)
            continue
        first, *rest = text.split("\n")
        sev = next((k for k, v in alerts.SEVERITY.items() if first.startswith(v + " **")), None)
        assert sev and first.endswith("**"), (name, first)
        assert len(text) <= alerts.MAX_CHARS, name
        body = "\n".join(rest)
        assert "**Impact:**" in body, name
        if sev in ("warning", "action"):
            assert "**Likely cause:**" in body and "**What to do:**" in body, name
            todo = body.split("**What to do:**", 1)[1]
            assert "`" in todo or "https://" in todo or "panel" in todo, (name, todo)


def test_ci_alert_prints_without_a_webhook_and_checks_its_arguments(monkeypatch, capsys) -> None:
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    assert ci_alert.main(["backup-check", RUN]) == 0
    assert "The weekly backup test failed" in capsys.readouterr().out
    assert ci_alert.main(["deploy", "http://not-https"]) == 2
    assert ci_alert.main(["update", RUN]) == 2


def test_the_reusable_workflow_alerts_on_deploy_and_backup_check_failures() -> None:
    import yaml

    jobs = yaml.safe_load((REPO / ".github/workflows/server.yml").read_text())["jobs"]
    for job in ci_alert.KINDS:
        last = jobs[job]["steps"][-1]
        assert last["if"] == "failure()" and f"scripts/ci_alert.py {job} " in last["run"], job
        assert last["env"]["DISCORD_WEBHOOK_URL"] == "${{ secrets.DISCORD_WEBHOOK_URL }}"
