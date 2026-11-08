"""Fakes for the dashboard (mcctl/dashboard.py): its tests and scripts/dashboard_screenshot.py.

No SSH, no network: a fixed server status, worlds/plugins/snapshot lists, a doctor report, and a `spawn` that
records the mcctl command lines and answers with canned output.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcctl import dashboard, doctor, remote, schema

PLAYERS_ONLINE = ["Steve_Builds", "alex_mines"]


PLAYERS = schema.Players(
    whitelist=[
        schema.Player("Steve_Builds", "00000000-0000-4000-8000-000000000001"),
        schema.Player("alex_mines", "00000000-0000-4000-8000-000000000002"),
        schema.Player("Notch_Jr", "00000000-0000-4000-8000-000000000003"),
        schema.Player("RedstoneRita", "00000000-0000-4000-8000-000000000004"),
    ],
    ops=[schema.Op("Steve_Builds", "00000000-0000-4000-8000-000000000001", 4)],
)


def write_players(root: Path) -> None:
    """players.yml with PLAYERS (the canonical text `mcctl validate` requires)."""
    (root / "players.yml").write_text(schema.dump_players(PLAYERS))


def sample_status(cfg: object = None) -> remote.Status:
    return remote.Status(
        reachable=True,
        server="active",
        players_online=2,
        players_max=20,
        players=list(PLAYERS_ONLINE),
        tps=19.94,
        mspt=12.3,
        last_backup="Sat 2026-11-07 09:00:04 UTC",
        last_backup_result="success",
        last_main_run={
            "conclusion": "success",
            "createdAt": "2026-11-07T08:12:40Z",
            "event": "push",
            "headSha": "4f3c2a1b9e8d7c6b5a4f3e2d1c0b9a8f7e6d5c4b",
        },
        pending_panel_prs=[
            {"number": 7, "title": "panel: whitelist add Notch_Jr", "headRefName": "panel/wl-7"}
        ],
        product={"version": "0.0.0", "commit": ""},
    )


WORLDS = {
    "active": "survival",
    "since": "2026-10-01T18:00:00Z",
    "data_version": 4671,
    "archives": [
        {"name": "creative", "time": "2026-09-12T10:00:00Z", "size": 734003200, "data_version": 4671},
        {"name": "skyblock", "time": "2026-08-02T21:30:00Z", "size": 104857600, "data_version": 4440},
    ],
    "library": [
        {"name": "amplified", "type": "amplified", "preset": "survival_smp", "seed": "-4172144997902289642"}
    ],
    "deleted": [],
}
PLUGINS = [
    {"name": "Chunky", "version": "1.4.40", "enabled": True},
    {"name": "spark", "version": "1.10.142", "enabled": True},
    {"name": "BlueMap", "version": "5.15", "enabled": False},
]
SNAPSHOTS = [
    {"id": "a1b2c3d4" + "0" * 56, "short_id": "a1b2c3d4", "time": "2026-11-06T03:00:05Z", "tags": ["daily"]},
    {"id": "b2c3d4e5" + "0" * 56, "short_id": "b2c3d4e5", "time": "2026-11-07T03:00:04Z", "tags": ["daily"]},
    {"id": "c3d4e5f6" + "0" * 56, "short_id": "c3d4e5f6", "time": "2026-11-07T09:00:04Z", "tags": ["manual"]},
]


def sample_report(root: Path | None = None) -> doctor.Report:
    r = doctor.Report()
    r.add("python", "ok", "3.12.8 (>= 3.12)")
    r.add("tools", "ok", "terraform 1.14.6, ansible-playbook 2.21.6, gh 2.83.1, tailscale 1.90.2")
    r.add(".env", "ok", "mode 0600, 9 credentials")
    r.add("cloudflare token", "ok", "valid, expires 2027-05-01")
    r.add("github token", "warn", "expires in 12 days (2026-11-19): renew it, then `mcctl doctor`")
    r.add("dns", "ok", "play.example.org -> 203.0.113.10")
    r.add("tailnet", "ok", "lighthouse reachable over Tailscale SSH")
    r.add("watchdog", "ok", "heartbeat 41 s ago")
    r.add("versions", "ok", "server v0.0.0, Worker v0.0.0, pin v0.0.0 (" + "0" * 12 + ")")
    r.add(
        "terraform plan", "fail", "2 resources would change: `mcctl apply` (or a pending server.yml change)"
    )
    return r


OUTPUTS: dict[str, list[str]] = {
    "restart": [
        "restart with a 60 s countdown",
        "countdown 60 s: 2 player(s) warned",
        "  [ok] restarted; RCON up",
    ],
    "worlds": [
        "switch to creative: survival is archived first (the safety snapshot)",
        "  [ok] now on creative",
    ],
    "publish": [
        "Changes against origin/main (4f3c2a1b9e8d), on main",
        "  uncommitted: server.yml",
        "  files: server.yml",
        "  server.yml: performance.view_distance",
        "Consequences",
        "  Ansible roles: minecraft (mc_view_distance)",
        "  server config overlays: server.properties.j2",
        "  [!!] Minecraft restart: yes (server.properties.j2)",
        "  Terraform: no change",
        "  players online now: 2 (Steve_Builds, alex_mines)",
        "Validate",
        "  [ok] valid (product edvardk/smallworld@000000000000)",
        "(dry run: nothing was committed, pushed or deployed; route pr would land: mcctl publish: server.yml "
        "(performance.view_distance))",
    ],
    "logs": [
        "Nov 07 10:02:11 lighthouse java[812]: [10:02:11 INFO]: Steve_Builds joined the game",
        "Nov 07 10:02:40 lighthouse java[812]: [10:02:40 INFO]: <Steve_Builds> anyone up for the nether?",
        "Nov 07 10:03:02 lighthouse java[812]: [10:03:02 INFO]: alex_mines joined the game",
        "Nov 07 10:04:15 lighthouse java[812]: [10:04:15 WARN]: Can't keep up! Is the server overloaded?",
        "Nov 07 10:05:00 lighthouse java[812]: [10:05:00 INFO]: Saved the game",
    ],
    "rcon": ["There are 2 of a max of 20 players online: Steve_Builds, alex_mines"],
    "backup": ["mc-backup.service finished: Result=success"],
}


@dataclass
class FakeSpawn:
    """Records each mcctl command line; prints OUTPUTS[first word] and exits with `codes` (default 0). A
    command in `hold` blocks until `release` is set (a long job, a live tail)."""

    codes: dict[str, int] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    hold: set[str] = field(default_factory=set)
    release: threading.Event = field(default_factory=threading.Event)
    stopped: list[str] = field(default_factory=list)

    def __call__(
        self,
        argv: Sequence[str],
        on_line: Callable[[str], None],
        on_start: Callable[[Callable[[], None]], None],
    ) -> int:
        argv = list(argv)
        self.calls.append(argv)
        word = argv[0]
        on_start(lambda: (self.stopped.append(word), self.release.set()))
        shown = " ".join(argv[: argv.index("-C")] if "-C" in argv else argv)
        for line in OUTPUTS.get(word, [f"mcctl {shown}: ok"]):
            on_line(line)
        if word in self.hold:
            self.release.wait(10)
        code = self.codes.get(word, 0)
        if code:
            on_line(f"  [xx] mcctl {word}: something failed")
        return code


def services(spawn: FakeSpawn | None = None, **kw: Any) -> dashboard.Services:
    return dashboard.Services(
        status=kw.pop("status", sample_status),
        query=kw.pop("query", lambda cfg, what: WORLDS if what == "worlds" else PLUGINS),
        snapshots=kw.pop("snapshots", lambda cfg: list(SNAPSHOTS)),
        doctor=kw.pop("doctor", sample_report),
        spawn=spawn or FakeSpawn(),
        update=kw.pop("update", None),
        terminal=kw.pop("terminal", lambda argv: 0),
        **kw,
    )
