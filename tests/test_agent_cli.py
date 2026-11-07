"""v1.3 phase 2: `mc_agent.py cli`, the server-side entry point of mcctl's actions over SSH (restart, plugin,
moderate, world_*; the worlds/plugins lists), and the job lock shared with mc-agent (mc_panel.Jobs).

The agent's Executor does the work exactly as for the panel; these tests prove the CLI entry adds the typed
confirmations, runs the job in the foreground, prints progress and RESULT, and writes the panel's audit entry
with the `mcctl:<user>` login. No systemd, no RCON server: both are replaced.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import mc_agent
import mc_panel
import notify
import pytest
import worlds


def snap(name: str, sid: str, dv: int = 4671) -> dict[str, Any]:
    return {"id": sid * 8, "short_id": sid, "time": "2026-11-02T10:00:00Z",
            "tags": ["world-archive", f"world:{name}", f"dv:{dv}"], "summary": {"total_bytes_processed": 10}}  # fmt: skip


class Box:
    """The server around the CLI entry: systemctl calls, RCON lines, audit entries, printed lines."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, players: int = 0) -> None:
        self.tmp, self.players = tmp, players
        self.calls: list[list[str]] = []
        self.rcon: list[str] = []
        self.audits: list[tuple[str, dict[str, str]]] = []
        self.lines: list[str] = []
        self.handlers: dict[int, Any] = {}
        self.state, self.library, self.request = tmp / "worlds.json", tmp / "lib.json", tmp / "req.json"
        monkeypatch.setattr(notify, "ADMIN_STOPPED_FILE", tmp / "admin-stopped")
        monkeypatch.setattr(notify, "MAINTENANCE_FILE", tmp / "maintenance.json")
        monkeypatch.delenv("WATCHDOG_URL", raising=False)
        monkeypatch.setattr(mc_agent, "systemctl", self.systemctl)
        monkeypatch.setattr(mc_agent, "audit", lambda msg, **f: self.audits.append((msg, f)))
        monkeypatch.setattr(mc_agent.signal, "signal", lambda sig, fn: self.handlers.__setitem__(sig, fn))
        monkeypatch.setattr(mc_panel, "wait_for_rcon", lambda *a, **k: True)
        monkeypatch.setattr(mc_panel, "unit_steps", lambda unit, since: ["STEP 7/7: verified"])
        monkeypatch.setattr(mc_agent.mc_rcon, "connect", self.connect)
        worlds.save_state({"active": "main", "since": "", "data_version": 4671, "deleted": [],
                           "archives": worlds.archives_from([snap("creative", "cccccccc")])}, self.state)  # fmt: skip
        self.library.write_text(json.dumps({"active": "main", "library": []}))
        (tmp / "lock.json").write_text(
            json.dumps([{"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1.4.40"}])
        )
        self.waits: list[float] = []

    def systemctl(self, *a: str, **k: object) -> None:
        self.calls.append(list(a))
        if a[-1] == worlds.PANEL_UNIT:
            r = json.loads(self.request.read_text())
            st = worlds.load_state(self.state)
            st["active"] = r["name"]
            worlds.save_state(st, self.state)

    def connect(self, *a: object, **k: object) -> Box:
        return self

    def __enter__(self) -> Box:
        return self

    def __exit__(self, *a: object) -> None:
        return None

    def command(self, line: str) -> str:
        self.rcon.append(line)
        return f"ran {line}"

    def job_rcon(self, line: str) -> str:
        return f"There are {self.players} of a max of 20 players online:"

    def wait(self, ev: threading.Event, secs: float) -> bool:
        self.waits.append(secs)
        return ev.is_set()

    def panel(self) -> mc_agent.Panel:
        return mc_agent.Panel(
            jobs=mc_panel.Jobs(lambda j: None, rcon=self.job_rcon, wait=self.wait, background=False,
                               lock_path=self.tmp / "job.lock"),
            pending=mc_panel.Pending(self.tmp / "pending.json"),
            rejected=mc_panel.RejectedLogins(self.tmp / "rejected.json"),
            plugins_lock=self.tmp / "lock.json",
            plugins_disabled=self.tmp / "disabled.json",
            snapshots=self.tmp / "snapshots.json",
            worlds_state=self.state,
            worlds_library=self.library,
            world_request=self.request,
            clock=lambda: 5000.0,
        )  # fmt: skip

    def cli(self, *argv: str, state_dir: Path | None = None) -> int:
        return mc_agent.cli_main(list(argv), self.panel(), state_dir or self.tmp, self.lines.append)


@pytest.fixture
def box(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Box:
    return Box(tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "argv",
    [[], ["sam", "restart"], ["mcctl:", "restart"], ["mcctl:a b", "restart"], ["mcctl:sam", "console", "{}"],
     ["mcctl:sam", "stop", "{}"], ["mcctl:sam", "restart", "[1]"], ["mcctl:sam", "restart", "{nope"],
     ["mcctl:sam", "restart", "{}", "extra"]],
)  # fmt: skip
def test_usage_errors(box: Box, argv: list[str]) -> None:
    assert box.cli(*argv) == 2
    assert box.lines[-1].startswith("usage: mc_agent.py cli") and box.calls == [] and box.audits == []


def test_lists_are_read_only_json(box: Box) -> None:
    (box.tmp / "disabled.json").write_text(json.dumps({"disabled": ["chunky"]}))
    assert box.cli("mcctl:sam", "plugins") == 0
    assert json.loads(box.lines[-1]) == [
        {"name": "chunky", "filename": "Chunky-1.4.40.jar", "version": "1.4.40", "enabled": False}
    ]
    assert box.cli("mcctl:sam", "worlds") == 0
    inv = json.loads(box.lines[-1])
    assert inv["active"] == "main" and [a["name"] for a in inv["archives"]] == ["creative"]
    assert box.calls == [] and box.audits == []


def test_restart_runs_the_job_in_the_foreground_and_is_audited(box: Box) -> None:
    assert box.cli("mcctl:sam", "restart", json.dumps({"countdown": 60, "backup": False})) == 0
    assert box.calls == [["restart", "minecraft.service"]]
    assert box.lines[-1] == "RESULT ok: restarted; the server answers again"
    assert any(line.startswith("running:") for line in box.lines)
    ((msg, fields),) = box.audits
    assert msg == "mcctl action=restart ok=True login=mcctl:sam"
    assert fields | {"args": json.loads(fields["args"])} == {
        "action": "restart", "login": "mcctl:sam", "args": {"backup": False, "countdown": 60}, "via": "mcctl",
        "result": "ok",
    }  # fmt: skip


def test_a_countdown_with_players_online_then_cancel_on_hangup(box: Box) -> None:
    box.players = 2

    def wait(ev: threading.Event, secs: float) -> bool:
        box.waits.append(secs)
        box.handlers[mc_agent.signal.SIGHUP]()  # ssh went away during the countdown
        return ev.is_set()

    box.wait = wait  # type: ignore[method-assign]
    assert box.cli("mcctl:sam", "restart", json.dumps({"countdown": 60})) == 1
    assert box.calls == [] and box.waits == [30]
    assert box.lines[0] == "countdown: countdown 60 s, 2 player(s) online"
    assert box.lines[-1] == "RESULT cancelled: cancelled during the countdown"
    assert box.audits[0][1]["result"] == "cancelled during the countdown"


def test_refusals_are_audited_with_the_reason(box: Box) -> None:
    (box.tmp / "admin-stopped").write_text("{}")
    assert box.cli("mcctl:sam", "restart", "{}") == 1
    assert box.lines[-1].startswith("RESULT refused: restart: the server is admin-stopped")
    assert box.audits[0][1]["result"].startswith("restart: the server is admin-stopped") and box.calls == []


@pytest.mark.parametrize("confirm", [None, "switch creative", "switch to main"])
def test_world_switch_needs_the_typed_confirmation(box: Box, confirm: str | None) -> None:
    args = {"name": "creative", "countdown": 0} | ({"confirm": confirm} if confirm else {})
    assert box.cli("mcctl:sam", "world_switch", json.dumps(args)) == 1
    assert box.lines[-1] == 'RESULT refused: world_switch: type "switch to creative" to confirm'
    assert box.calls == [] and not box.request.exists()
    assert "confirm" not in box.audits[0][1]["args"]


def test_world_switch_runs_the_panels_unit_and_records_it_for_git(box: Box) -> None:
    args = {"name": "creative", "countdown": 0, "confirm": "switch to creative"}
    assert box.cli("mcctl:sam", "world_switch", json.dumps(args)) == 0
    assert box.calls == [["start", "mc-world.service"]] and box.lines[-1] == "RESULT ok: STEP 7/7: verified"
    assert json.loads(box.request.read_text())["login"] == "mcctl:sam"
    pend = mc_panel.Pending(box.tmp / "pending.json").load()["world"]
    assert pend["active"] == "creative" and pend["login"] == "mcctl:sam"


def test_world_checks_are_the_agents(box: Box) -> None:
    args = {"name": "nope", "countdown": 0, "confirm": "switch to nope"}
    assert box.cli("mcctl:sam", "world_switch", json.dumps(args)) == 1
    assert "isn't an archived world" in box.lines[-1] and box.calls == []


def test_world_actions_need_the_agents_state_dir(box: Box) -> None:
    args = {"name": "creative", "confirm": "delete creative"}
    assert box.cli("mcctl:sam", "world_delete", json.dumps(args), state_dir=box.tmp / "missing") == 1
    assert "panel.mode: cloudflare" in box.lines[-1] and box.calls == []


def test_plugin_toggle_with_confirmation(box: Box) -> None:
    assert (
        box.cli("mcctl:sam", "plugin", json.dumps({"name": "chunky", "enabled": False, "countdown": 0})) == 1
    )
    assert box.lines[-1] == 'RESULT refused: plugin: type "disable chunky" to confirm'
    args = {"name": "chunky", "enabled": False, "countdown": 0, "confirm": "disable chunky"}
    assert box.cli("mcctl:sam", "plugin", json.dumps(args)) == 0
    assert box.calls == [["start", "mc-plugins-apply.service"], ["restart", "minecraft.service"]]
    record = json.loads((box.tmp / "disabled.json").read_text())
    assert record["disabled"] == ["chunky"] and record["by"] == "mcctl:sam"
    args = {"name": "worldedit", "enabled": False, "countdown": 0, "confirm": "disable worldedit"}
    assert box.cli("mcctl:sam", "plugin", json.dumps(args)) == 1
    assert "not a plugin in plugins.lock.yml" in box.lines[-1]


def test_moderation_is_immediate(box: Box) -> None:
    assert (
        box.cli("mcctl:sam", "moderate", json.dumps({"verb": "kick", "target": "jonas_r", "reason": "brb"}))
        == 0
    )
    assert box.rcon == ["kick jonas_r brb"] and box.lines[-1] == "RESULT ok: ran kick jonas_r brb"
    assert box.cli("mcctl:sam", "moderate", json.dumps({"verb": "op", "target": "jonas_r"})) == 1
    assert len(box.audits) == 2 and box.audits[1][1]["result"].startswith("moderate:")


def test_cli_confirm_matches_mcctl() -> None:
    from mcctl import actions

    for action, args in (("world_create", {"name": "x"}), ("world_switch", {"name": "x"}),
                         ("world_delete", {"name": "x"}), ("plugin", {"name": "x", "enabled": True}),
                         ("plugin", {"name": "x", "enabled": False})):  # fmt: skip
        assert mc_agent.cli_confirm(action, args) == actions.confirm_phrase(
            action, "x", bool(args.get("enabled"))
        )
    assert set(mc_agent.CLI_ACTIONS) <= set(mc_agent.ACTIONS)
    assert mc_agent.cli_confirm("restart", {}) is None


# --- one job at a time across processes (mc_panel.Jobs lock_path) --------------------------------------------


def test_the_job_lock_is_shared_across_job_runners(tmp_path: Path) -> None:
    lock = tmp_path / "job.lock"
    agent = mc_panel.Jobs(lambda j: None, rcon=lambda line: "There are 0 of a max", lock_path=lock)
    cli = mc_panel.Jobs(
        lambda j: None, rcon=lambda line: "There are 0 of a max", lock_path=lock, background=False
    )
    gate, started = threading.Event(), threading.Event()
    agent.start("a1", "restore", "Restore", "admin@x", 0, lambda j: started.set() or (gate.wait(5) and "ok"))
    assert started.wait(5)
    with pytest.raises(mc_panel.ActionError, match="started from the panel or mcctl"):
        cli.start("c1", "restart", "Restart", "mcctl:sam", 0, lambda j: "x")
    gate.set()
    for _ in range(100):
        if not agent.busy():
            break
        threading.Event().wait(0.02)
    job = cli.start("c2", "restart", "Restart", "mcctl:sam", 0, lambda j: "done")  # released: runs now
    assert job.state == "done"
    assert lock.stat().st_mode & 0o777 == 0o600  # CodeQL: not world-readable; only mcops opens it

    def boom(job: mc_panel.Job) -> str:
        raise ValueError("broken")

    failing = mc_panel.Jobs(lambda j: None, lock_path=lock, background=False)
    assert failing.start("c3", "restart", "Restart", "x", 0, boom).state == "failed"
    assert (
        cli.start("c4", "restart", "Restart", "x", 0, lambda j: "ok").state == "done"
    )  # released after a crash


def test_an_unopenable_lock_file_only_loses_the_cross_process_check(tmp_path: Path) -> None:
    jobs = mc_panel.Jobs(lambda j: None, lock_path=tmp_path / "missing" / "job.lock", background=False)
    assert jobs.start("j", "restart", "Restart", "x", 0, lambda j: "ok").state == "done"
