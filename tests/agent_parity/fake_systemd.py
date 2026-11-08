"""Fake `systemctl` and `journalctl` for the parity harness (installed first on the agent's PATH).

The harness writes this file twice into its fakebin directory (as `systemctl` and `journalctl`) with a
shebang for the test interpreter and CTL set to its control directory. Every call is appended to
CTL/calls.jsonl; what it answers comes from CTL/systemd.json, read on every call so a scenario can change
it between steps:

    {"show": {unit: {prop: value}} (several units: one block each, as systemctl prints them),
     "is_active": {unit: state},
     "actions": {"<verb> <unit>": {"exit": n, "write": {path: text}, "steps": [lines], "block": path}},
     "steps": {unit: [journal lines for `journalctl -u UNIT --since ...`]},
     "journal": host path of the `journalctl -o json` fixture (one JSON object per line, with __CURSOR),
     "follow": [JSON lines `journalctl -f` prints before it waits]}

An action runs inside the agent's mount namespace: its `write` paths are the production paths the agent
sees. `block` waits until that host path exists (a long unit run, for the job-lock scenarios).
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

CTL = Path(os.environ.get("PARITY_FAKE_CTL", "."))  # replaced by the harness when it installs the file


def load() -> dict:
    try:
        return json.loads((CTL / "systemd.json").read_text())
    except (OSError, ValueError):
        return {}


def record(tool: str, argv: list[str]) -> None:
    line = json.dumps({"tool": tool, "argv": argv}) + "\n"
    fd = os.open(CTL / "calls.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, line.encode())
    finally:
        os.close(fd)


def systemctl(argv: list[str], cfg: dict) -> int:
    args = [a for a in argv if a != "--no-ask-password"]
    if args[:1] == ["show"]:
        props = [args[i + 1] for i, a in enumerate(args) if a == "-p" and i + 1 < len(args)]
        props = [p for item in props for p in item.split(",")]
        units = [a for a in args[1:] if a.endswith(".service")]
        if len(units) > 1:  # one block per unit, blank-line separated, Id = the unit (telemetry's overhead)
            blocks = []
            for u in units:
                have = {"Id": u} | cfg.get("show", {}).get(u, {})
                blocks.append("\n".join(f"{p}={have.get(p, '')}" for p in props))
            print("\n\n".join(blocks))
            return 0
        unit = units[0] if units else ""
        have = cfg.get("show", {}).get(unit, {})
        if "--value" in args:
            print("\n".join(str(have.get(p, "")) for p in props))
        else:
            print("\n".join(f"{p}={have.get(p, '')}" for p in props))
        return 0
    if args[:1] == ["is-active"]:
        states = [cfg.get("is_active", {}).get(u, "inactive") for u in args[1:]]
        print("\n".join(states))
        return 0 if all(s == "active" for s in states) else 3
    verb = args[0] if args else ""
    unit = next((a for a in args[1:] if not a.startswith("--")), "")
    act = cfg.get("actions", {}).get(f"{verb} {unit}", {})
    for path, text in (act.get("write") or {}).items():
        p = Path(path)
        tmp = p.with_name(p.name + ".fake-tmp")
        tmp.write_text(text)
        os.replace(tmp, p)
    if act.get("steps"):
        steps = CTL / "steps.json"
        have = json.loads(steps.read_text()) if steps.exists() else {}
        have.setdefault(unit, []).extend(act["steps"])
        steps.write_text(json.dumps(have))
    if act.get("block"):
        deadline = time.monotonic() + 120
        while not Path(act["block"]).exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    if act.get("exit"):
        print(f"Job for {unit} failed.", file=sys.stderr)
    return int(act.get("exit") or 0)


def journalctl(argv: list[str], cfg: dict) -> int:
    if "-f" in argv:
        for line in cfg.get("follow", []):
            print(line, flush=True)
        time.sleep(300)
        return 0
    if "-u" in argv:
        unit = argv[argv.index("-u") + 1]
        steps = CTL / "steps.json"
        have = json.loads(steps.read_text()) if steps.exists() else {}
        for line in have.get(unit, []) + cfg.get("steps", {}).get(unit, []):
            print(line)
        return 0
    cursor = next((a.split("=", 1)[1] for a in argv if a.startswith("--after-cursor=")), None)
    fixture = cfg.get("journal")
    if not fixture:
        return 0
    out = sys.stdout
    seen = cursor is None
    try:
        with open(fixture, encoding="utf-8") as f:
            for line in f:
                if not seen:
                    try:
                        raw = json.loads(line)
                    except ValueError:
                        raw = None
                    seen = isinstance(raw, dict) and raw.get("__CURSOR") == cursor
                    continue
                out.write(line)
        out.flush()
    except BrokenPipeError:  # the agent stopped reading at its batch cap
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def main() -> int:
    tool = Path(sys.argv[0]).name
    argv = sys.argv[1:]
    record(tool, argv)
    cfg = load()
    return systemctl(argv, cfg) if tool == "systemctl" else journalctl(argv, cfg)


if __name__ == "__main__":
    sys.exit(main())
