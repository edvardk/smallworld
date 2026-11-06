"""server.yml schema migrations between product releases (design §4 `upgrade`, §8 P12).

`mcctl upgrade` runs the **target** product's `mcctl schema-migrate --from <old version>` on the config repo,
so a release can carry the migrations from every older release to itself. Each migration is a pure function
`text -> text` over the hand-edited server.yml, written with `mcctl.yamledit` (comments and layout kept,
never re-dumped with PyYAML), registered with the first release whose schema needs it. A migration must be
idempotent: running it on an already migrated file changes nothing.

tests/test_mcctl_p12.py exercises the runner with a registered example; each migration has its own tests.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from mcctl import __version__

VERSION = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")
UNTAGGED = re.compile(r"-\d+-g[0-9a-f]{7,40}$")  # `git describe` suffix of an untagged pin


@dataclass(frozen=True)
class Migration:
    version: str  # the first release that needs it, "X.Y.Z"
    summary: str
    apply: Callable[[str], str]


WINE_EMOJI = "\U0001f377"
# The emoji as a YAML escape in a double-quoted string ("\U0001F377"), in either case.
WINE_ESCAPE = re.compile(r"\\U0001[fF]377")
TOP_LEVEL_KEY = re.compile(r"^[^\s#]")


def glyph_placeholder(text: str) -> str:
    """1.2.0: the branding texts' `{wine}` (and the literal U+1F377 that read as it) become `{glyph}`, the
    placeholder for the resource pack's glyph (branding.resource_pack.glyph). Only inside the top-level
    `branding:` block; everything else (comments and layout included) stays byte for byte."""
    lines = text.splitlines(keepends=True)
    inside = False
    for i, line in enumerate(lines):
        if TOP_LEVEL_KEY.match(line):
            inside = line.startswith("branding:")
        if inside:
            line = line.replace("{wine}", "{glyph}").replace(WINE_EMOJI, "{glyph}")
            lines[i] = WINE_ESCAPE.sub("{glyph}", line)
    return "".join(lines)


MIGRATIONS: tuple[Migration, ...] = (
    Migration("1.2.0", "branding texts: {wine} and the wine emoji -> {glyph}", glyph_placeholder),
)


def parse(version: str) -> tuple[int, int, int]:
    m = VERSION.fullmatch(version.strip())
    if not m:
        raise ValueError(f"not a release version: {version!r}")
    major, minor, patch = (int(g) for g in m.groups())
    return major, minor, patch


def pending(
    old: str | None, new: str = __version__, registry: Sequence[Migration] = MIGRATIONS
) -> list[Migration]:
    """Migrations with old < version <= new, oldest first (old None: from the very first release).

    `old` may be an untagged pin (vX.Y.Z-N-g<sha>, mcctl/pinmove.py): it counts as X.Y.Z, so every migration
    after that release runs (some may be in its N commits already; migrations are idempotent)."""
    low = parse(UNTAGGED.sub("", old.strip())) if old else (0, 0, 0)
    high = parse(new)
    return sorted((m for m in registry if low < parse(m.version) <= high), key=lambda m: parse(m.version))


def migrate(
    text: str, old: str | None, new: str = __version__, registry: Sequence[Migration] = MIGRATIONS
) -> tuple[str, list[str]]:
    applied = []
    for m in pending(old, new, registry):
        out = m.apply(text)
        if out != text:
            applied.append(f"{m.version}: {m.summary}")
        text = out
    return text, applied
