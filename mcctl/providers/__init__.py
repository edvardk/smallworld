"""Credential validators (design §3, §4 init steps 3/5/7, `doctor`): one module per provider.

Each check takes the credential, the non-secret context it needs (zone, repository, ...) and an
`mcctl.http.Transport`, and returns a `CredentialCheck`. Values are never put into messages.
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass, field
from typing import Any, Literal

Level = Literal["ok", "warn", "fail"]


class ProviderError(RuntimeError):
    """A provider API call failed in a way the user can fix: `fix` says how (never contains a credential)."""

    def __init__(self, message: str, fix: str = "") -> None:
        super().__init__(message)
        self.fix = fix


@dataclass
class CredentialCheck:
    """Outcome of validating one credential.

    `credential` is the expiry.yml name. `expires_on` is only meaningful when `expiry_known` is true: a date,
    or None for a credential that never expires.
    """

    credential: str
    level: Level = "ok"
    messages: list[str] = field(default_factory=list)
    expires_on: datetime.date | None = None
    expiry_known: bool = False
    facts: dict[str, Any] = field(default_factory=dict)  # non-secret findings (account id, ...)

    def fail(self, message: str) -> CredentialCheck:
        self.level = "fail"
        self.messages.append(message)
        return self

    def warn(self, message: str) -> None:
        if self.level == "ok":
            self.level = "warn"
        self.messages.append(message)

    def info(self, message: str) -> None:
        self.messages.append(message)

    def expires(self, when: datetime.date | None) -> None:
        self.expires_on, self.expiry_known = when, True


def parse_date(value: object) -> datetime.date | None:
    """The calendar date (UTC) at the start of an ISO 8601 / RFC 3339 / `YYYY-MM-DD hh:mm:ss UTC` string."""
    if not isinstance(value, str):
        return None
    m = re.match(r"\s*(\d{4}-\d{2}-\d{2})", value)
    if m is None:
        return None
    try:
        return datetime.date.fromisoformat(m.group(1))
    except ValueError:
        return None


def parse_datetime(value: object) -> datetime.datetime | None:
    """An aware datetime from an RFC 3339 string (`Z` or an offset); None if it isn't one."""
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=datetime.UTC)
