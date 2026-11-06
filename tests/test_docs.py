"""P15 docs checks: every relative Markdown link resolves (file and #anchor), and the setup guide names the
Cloudflare token permissions exactly as mcctl checks them. No network: external links are not fetched."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import REPO

from mcctl.providers import cloudflare

DOCS = sorted(
    [
        *(REPO / name for name in ("README.md", "SECURITY.md", "CHANGELOG.md", "CONTRIBUTING.md")),
        *(REPO / "docs").rglob("*.md"),
        REPO / "template" / "README.md",
    ]
)
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)|!\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^(```|~~~)")


def prose(text: str) -> str:
    """The text outside fenced code blocks."""
    out, fenced = [], False
    for line in text.splitlines():
        if FENCE.match(line.strip()):
            fenced = not fenced
            continue
        if not fenced:
            out.append(line)
    return "\n".join(out)


def slug(heading: str) -> str:
    """GitHub's heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    s = re.sub(r"[`*_]|\[([^\]]*)\]\([^)]*\)", lambda m: m.group(1) or "", heading.strip().lower())
    s = re.sub(r"[^\w\- ]", "", s)
    return s.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    seen: dict[str, int] = {}
    out = set()
    for line in prose(path.read_text()).splitlines():
        m = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if not m:
            continue
        base = slug(m.group(1))
        n = seen.get(base, 0)
        seen[base] = n + 1
        out.add(base if n == 0 else f"{base}-{n}")
    for m in re.finditer(r'<a (?:name|id)="([^"]+)"', path.read_text()):
        out.add(m.group(1))
    return out


@pytest.mark.parametrize("doc", DOCS, ids=lambda p: str(p.relative_to(REPO)))
def test_relative_links_resolve(doc: Path) -> None:
    assert doc.is_file(), doc
    broken = []
    for m in LINK.finditer(prose(doc.read_text())):
        target = m.group(1) or m.group(2)
        if re.match(r"^[a-z]+:", target):  # https:, mailto:
            continue
        path_part, _, anchor = target.partition("#")
        dest = (doc.parent / path_part).resolve() if path_part else doc
        if not dest.exists():
            broken.append(f"{target}: no such file")
            continue
        if anchor and dest.suffix == ".md" and anchor not in anchors(dest):
            broken.append(f"{target}: no heading #{anchor} in {dest.relative_to(REPO)}")
    assert broken == [], f"{doc.relative_to(REPO)}: " + "; ".join(broken)


def test_setup_guide_names_the_exact_cloudflare_permissions() -> None:
    """docs/setup.md lists the admin token's rows exactly as mcctl/providers/cloudflare.py checks them, and
    every look-alike mcctl knows about."""
    text = (REPO / "docs/setup.md").read_text()
    rows = cloudflare.ACCOUNT_TOKEN_PERMISSIONS + cloudflare.ACCOUNT_TOKEN_ACCESS
    assert len(rows) == 8
    for kind, name, level, _ in rows:
        assert f"{kind} > {name} > {level}" in text, name
    for lookalike in cloudflare.LOOKALIKES:
        assert lookalike in text.lower(), lookalike


def test_docs_index_is_linked_from_the_readme() -> None:
    readme = (REPO / "README.md").read_text()
    for page in ("docs/setup.md", "docs/operations.md", "docs/reference.md", "SECURITY.md", "CHANGELOG.md"):
        assert f"]({page}" in readme, page


MAP = REPO / "docs" / "map.md"
PATH_SUFFIXES = set(".py .md .yml .yaml .tf .tftpl .js .mjs .json .j2 .html .css".split())


def map_paths() -> list[str]:
    """The backticked words in docs/map.md that name a repo path (a slash, or a file suffix). Config-repo files
    are written there without backticks; placeholders (`<config>/...`), routes (`/modpack`) and globs are skipped."""
    out = []
    for word in re.findall(r"`([^`\s]+)`", prose(MAP.read_text())):
        if word.startswith(("<", "/", "-")) or "*" in word:
            continue
        if "/" in word or Path(word).suffix in PATH_SUFFIXES:
            out.append(word)
    return out


def test_map_paths_exist() -> None:
    paths = map_paths()
    assert len(paths) > 100
    missing = sorted({p for p in paths if not (REPO / p).exists()})
    assert missing == [], f"docs/map.md names paths that don't exist: {missing}"


def test_map_lists_every_mcctl_command() -> None:
    from mcctl import cli

    sub = next(a for a in cli.build_parser()._actions if a.dest == "command")
    text = MAP.read_text()
    missing = [name for name in sub.choices if not re.search(rf"`mcctl {re.escape(name)}(?![\w-])", text)]
    assert missing == [], f"docs/map.md doesn't list: {missing}"
