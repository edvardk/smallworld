"""v1.5.4: `mcctl deploy --check` (ansible-playbook --check --diff) must get through a deploy that changes nothing.

Ansible skips `command`/`shell` tasks in check mode ("Command would have run if not in check mode"), so a read
whose output a later assert or condition uses must say `check_mode: false`, or the dry run fails on a box
that is fine (live, v1.5.3: "sshd's effective config still allows root or password logins", from the skipped
`sshd -T`). A `changed_when: false` command task is either such a read (it gets `check_mode: false`) or has a
side effect that a dry run must not cause (listed below, with the reason). Checking the result of a change
(e.g. the Tailscale upgrade) isn't a read either: the change didn't happen in the dry run.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from conftest import REPO

ROLES = REPO / "ansible" / "roles"

NOT_READS = {
    ("minecraft/tasks/defer.yml", "Tell Discord a restart is pending"): "posts to Discord",
    (
        "minecraft/tasks/restart.yml",
        "Open a maintenance window (self-heal and watchdog stand down)",
    ): "writes",
    ("minecraft/tasks/restart.yml", "Announce restart in game (skipped if nobody is online)"): "RCON say",
    ("minecraft/tasks/restart.yml", "Close the maintenance window"): "writes",
    (
        "players/tasks/main.yml",
        "Tell Discord which players were kept although players.yml doesn't list them",
    ): "posts to Discord",
    ("tailscale/tasks/main.yml", "Check the pinned version is installed"): "checks the upgrade's result",
}


def _tasks(items: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for t in items or []:
        if isinstance(t, dict):
            out.append(t)
            for key in ("block", "rescue", "always"):
                out += _tasks(t.get(key))
    return out


def _read_only_commands() -> dict[tuple[str, str], dict[str, Any]]:
    found = {}
    for path in sorted(ROLES.glob("*/tasks/*.yml")):
        for t in _tasks(yaml.safe_load(path.read_text())):
            command = any(k in t for k in ("ansible.builtin.command", "ansible.builtin.shell"))
            if command and t.get("changed_when") is False:
                found[(path.relative_to(ROLES).as_posix(), str(t.get("name")))] = t
    return found


def test_reads_run_in_check_mode() -> None:
    tasks = _read_only_commands()
    assert set(NOT_READS) <= set(tasks), "a NOT_READS entry no longer exists: update the list"
    for key, task in tasks.items():
        if key in NOT_READS:
            assert "check_mode" not in task, f"{key} has a side effect ({NOT_READS[key]}): never in a dry run"
        else:
            assert task.get("check_mode") is False, (
                f"{key}: a read with `changed_when: false` needs `check_mode: false` (mcctl deploy --check), "
                "or, if it has a side effect, an entry in NOT_READS"
            )


def test_the_live_case_is_covered() -> None:
    tasks = _read_only_commands()
    assert tasks[("access/tasks/main.yml", "Read the effective sshd config")]["check_mode"] is False
    assert (
        tasks[("backups/tasks/main.yml", "Check whether the restic repository exists")]["check_mode"] is False
    )


def test_names_are_unique_per_file() -> None:
    """The keys above are (file, task name): a duplicate name would hide a task from the check."""
    for path in sorted(ROLES.glob("*/tasks/*.yml")):
        names = [t.get("name") for t in _tasks(yaml.safe_load(path.read_text())) if t.get("name")]
        dupes = {n for n in names if names.count(n) > 1}
        assert not dupes, f"{Path(path).relative_to(ROLES)}: duplicate task names {sorted(dupes)}"
