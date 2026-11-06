"""The alert template for mcctl's own Discord messages (the weekly expiry check): the product's one template.

It is the box's `scripts/alerts.py` (stdlib only), loaded from the product checkout mcctl runs from, so a CI
alert reads exactly like one from health.py or the Worker (docs/operations.md "Alerts").
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

from mcctl import schema

PATH = schema.PRODUCT_ROOT / "scripts" / "alerts.py"
_NAME = "mcctl_box_alerts"


def _load() -> ModuleType:
    if _NAME in sys.modules:
        return sys.modules[_NAME]
    spec = importlib.util.spec_from_file_location(_NAME, PATH)
    assert spec and spec.loader, PATH
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_NAME] = mod  # dataclasses resolve their module through sys.modules
    spec.loader.exec_module(mod)
    return mod


_alerts = _load()
Alert = _alerts.Alert
render = _alerts.render
NO_ACTION: str = _alerts.NO_ACTION
