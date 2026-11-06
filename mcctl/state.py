"""`.mcctl/state.json` (design §4): what mcctl has done in this config repo, per step.

Each step records `status` (done | failed), an `input_hash` of what it ran with (so a changed input re-runs
it), non-secret `outputs` and `updated_at`. `mcctl init` (P5) resumes from it; in P4 `apply`, `bootstrap`
and `doctor` record themselves. The directory is git-ignored; nothing secret is ever written here.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

STATE_FILE = Path(".mcctl") / "state.json"
VERSION = 1
Status = Literal["done", "failed", "waiting"]  # waiting: paused for the user (init step 9)


class StateError(RuntimeError):
    pass


def input_hash(inputs: Mapping[str, Any]) -> str:
    """Stable hash of non-secret inputs (canonical JSON)."""
    blob = json.dumps(inputs, sort_keys=True, separators=(",", ":"), default=str).encode()
    return "sha256:" + hashlib.sha256(blob).hexdigest()


@dataclass
class State:
    path: Path
    steps: dict[str, dict[str, Any]] = field(default_factory=dict)

    def get(self, step: str) -> dict[str, Any] | None:
        return self.steps.get(step)

    def is_done(self, step: str, inputs: Mapping[str, Any] | None = None) -> bool:
        rec = self.steps.get(step)
        if rec is None or rec.get("status") != "done":
            return False
        return inputs is None or rec.get("input_hash") == input_hash(inputs)

    def record(
        self,
        step: str,
        status: Status,
        *,
        inputs: Mapping[str, Any] | None = None,
        outputs: Mapping[str, Any] | None = None,
        now: datetime.datetime | None = None,
    ) -> None:
        when = (now or datetime.datetime.now(datetime.UTC)).replace(microsecond=0)
        self.steps[step] = {
            "status": status,
            "input_hash": input_hash(inputs or {}),
            "outputs": dict(outputs or {}),
            "updated_at": when.isoformat().replace("+00:00", "Z"),
        }
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps({"version": VERSION, "steps": self.steps}, indent=2, sort_keys=True) + "\n"
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.", suffix=".json")
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(text)
            os.replace(tmp, self.path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


def load(config_root: Path) -> State:
    path = config_root / STATE_FILE
    if not path.is_file():
        return State(path)
    try:
        data = json.loads(path.read_text())
    except ValueError as e:
        raise StateError(f"{path}: not JSON ({e}); move it aside to start over") from None
    if (
        not isinstance(data, dict)
        or data.get("version") != VERSION
        or not isinstance(data.get("steps"), dict)
    ):
        raise StateError(f"{path}: unknown format; move it aside to start over")
    return State(path, data["steps"])
