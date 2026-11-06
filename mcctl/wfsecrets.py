"""v1.2: the secrets a config repo's workflow passes to the reusable workflow, against the product's template.

Config repos pass secrets explicitly (`secrets:` under `jobs.server`, never `secrets: inherit`), so a secret a
newer product needs is missing until the config repo's workflow names it: v1.1's ARTIFACT_PUBLISH_TOKEN was
(mc-config PR #2; the deploy skipped client-publish). `missing` compares the block with the product's
`template/.github/workflows/server.yml`; `add` inserts the missing lines after the block's last entry and
keeps everything else byte for byte (comments, order, layout). Used by `mcctl doctor` (`--fix` commits it),
the pin move (`mcctl/pinmove.py`, the same commit) and `mcctl schema-migrate` (so the target release of
`mcctl upgrade` adds what it needs).
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

from mcctl import configrepo

TEMPLATE = Path("template") / configrepo.WORKFLOW  # relative to a product root
NAME = re.compile(r"[A-Z][A-Z0-9_]*")


class WorkflowError(ValueError):
    pass


def template_text(product_root: Path) -> str:
    return (product_root / TEMPLATE).read_text()


def passed(text: str) -> list[str] | None:
    """The secret names `jobs.server.secrets` passes, in order (None: `secrets: inherit`, i.e. all)."""
    try:
        wf = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkflowError(f"{configrepo.WORKFLOW} isn't valid YAML: {e}") from None
    job = ((wf or {}).get("jobs") or {}).get("server") if isinstance(wf, dict) else None
    if not isinstance(job, dict):
        raise WorkflowError(f"{configrepo.WORKFLOW} has no `jobs.server` (the call of the reusable workflow)")
    secrets = job.get("secrets")
    if secrets == "inherit":
        return None
    if secrets is None:
        return []
    if not isinstance(secrets, dict):
        raise WorkflowError(f"{configrepo.WORKFLOW}: `jobs.server.secrets` is neither a map nor `inherit`")
    return [str(k) for k in secrets]


def missing(config_text: str, template: str) -> list[str]:
    """Secrets the template passes that the config repo's workflow doesn't, in the template's order."""
    have = passed(config_text)
    if have is None:
        return []
    return [n for n in passed(template) or [] if n not in have]


def line(name: str) -> str:
    return f"{name}: ${{{{ secrets.{name} }}}}"


def add(text: str, names: list[str]) -> str:
    """text with `NAME: ${{ secrets.NAME }}` appended to jobs.server's secrets block (created if absent)."""
    if not names:
        return text
    if bad := [n for n in names if not NAME.fullmatch(n)]:
        raise WorkflowError(f"not secret names: {', '.join(bad)}")
    lines = text.splitlines(keepends=True)
    job = _job(lines, "server")
    if job is None:
        raise WorkflowError(f"{configrepo.WORKFLOW} has no `jobs.server` block")
    start, end, indent = job
    block = _key(lines, start + 1, end, "secrets")
    if block is None:  # no secrets: append a block at the job's end
        child = _child_indent(lines, start + 1, end) or indent + 2
        new = [" " * child + "secrets:\n", *(" " * (child + 2) + line(n) + "\n" for n in names)]
        at = _last_content(lines, start, end) + 1
    else:
        at_key, child = block
        if lines[at_key].split(":", 1)[1].strip() not in ("", "{}"):
            raise WorkflowError(f"{configrepo.WORKFLOW}: `secrets:` isn't a block map (inherit? flow style?)")
        b_end = _block_end(lines, at_key + 1, end, child)
        entry = _child_indent(lines, at_key + 1, b_end) or child + 2
        new = [" " * entry + line(n) + "\n" for n in names]
        at = _last_content(lines, at_key, b_end) + 1
        if lines[at_key].rstrip().endswith("{}"):
            lines[at_key] = lines[at_key].replace("{}", "", 1).rstrip() + "\n"
    if at > 0 and not lines[at - 1].endswith("\n"):
        lines[at - 1] += "\n"
    return "".join(lines[:at] + new + lines[at:])


def _indent(s: str) -> int:
    return len(s) - len(s.lstrip(" "))


def _blank(s: str) -> bool:
    return not s.strip() or s.lstrip().startswith("#")


def _block_end(lines: list[str], i: int, end: int, indent: int) -> int:
    while i < end and (_blank(lines[i]) or _indent(lines[i]) > indent):
        i += 1
    return i


def _last_content(lines: list[str], start: int, end: int) -> int:
    i = end - 1
    while i > start and _blank(lines[i]):
        i -= 1
    return i


def _child_indent(lines: list[str], start: int, end: int) -> int | None:
    return next((_indent(s) for s in lines[start:end] if not _blank(s)), None)


def _key(lines: list[str], start: int, end: int, key: str) -> tuple[int, int] | None:
    """(line, indent) of `key:` directly inside lines[start:end] (the shallowest indent there)."""
    child = _child_indent(lines, start, end)
    for i in range(start, end):
        s = lines[i]
        if not _blank(s) and _indent(s) == child and re.match(rf"{re.escape(key)}\s*:", s.lstrip()):
            return i, child
    return None


def _job(lines: list[str], name: str) -> tuple[int, int, int] | None:
    """(line, end, indent) of the job `name:` under the top-level `jobs:` key."""
    top = next((i for i, s in enumerate(lines) if re.match(r"jobs\s*:", s)), None)
    if top is None:
        return None
    jobs_end = _block_end(lines, top + 1, len(lines), 0)
    found = _key(lines, top + 1, jobs_end, name)
    if found is None:
        return None
    i, indent = found
    return i, _block_end(lines, i + 1, jobs_end, indent), indent
