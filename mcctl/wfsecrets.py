"""v1.2: the secrets a config repo's workflow passes to the reusable workflow, against the product's template.

Config repos pass secrets explicitly (`secrets:` under `jobs.server`, never `secrets: inherit`), so a secret a
newer product needs is missing until the config repo's workflow names it: v1.1's ARTIFACT_PUBLISH_TOKEN was
(mc-config PR #2; the deploy skipped client-publish). `missing` compares the block with the product's
`template/.github/workflows/server.yml`; `add` inserts the missing lines after the block's last entry and
keeps everything else byte for byte (comments, order, layout). Used by `mcctl doctor` (`--fix` commits it),
the pin move (`mcctl/pinmove.py`, the same commit) and `mcctl schema-migrate` (so the target release of
`mcctl upgrade` adds what it needs).

v1.5: the same for the config repo's manual-run form (`on.workflow_dispatch.inputs`): an input the template
has and the config repo's copy doesn't (v1.5's `product_version`), and a choice option it lacks (the `task`
input's `product-update`). `mcctl upgrade` only moves the `uses:` line, so without this a config repo would
never get the new button. `missing_dispatch` lists them, `add_dispatch` copies the template's input block
(re-indented) after the last input and inserts missing options into a one-line `options: [...]` list, keeping
everything else byte for byte; `sync` does secrets and inputs together.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
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


# --- v1.5: the workflow_dispatch inputs ----------------------------------------------------------------


def _dispatch_inputs(text: str) -> dict[str, object] | None:
    """`on.workflow_dispatch.inputs` (None: no such block, e.g. a config repo without a manual run)."""
    try:
        wf = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise WorkflowError(f"{configrepo.WORKFLOW} isn't valid YAML: {e}") from None
    if not isinstance(wf, dict):
        return None
    on = wf.get("on", wf.get(True))  # YAML 1.1 reads an unquoted `on` key as True
    dispatch = on.get("workflow_dispatch") if isinstance(on, dict) else None
    inputs = dispatch.get("inputs") if isinstance(dispatch, dict) else None
    return inputs if isinstance(inputs, dict) else None


def _options(spec: object) -> list[str]:
    if isinstance(spec, dict) and spec.get("type") == "choice" and isinstance(spec.get("options"), list):
        return [str(o) for o in spec["options"]]
    return []


def missing_dispatch(config_text: str, template: str) -> list[str]:
    """What the template's manual-run form has and the config repo's lacks: `input NAME` and `NAME option
    VALUE` (a choice input's option), in the template's order. Nothing when the config repo has no inputs
    block at all (nothing to extend: left to the admin)."""
    have, want = _dispatch_inputs(config_text), _dispatch_inputs(template)
    if have is None or not want:
        return []
    out = [f"input {name}" for name in want if name not in have]
    for name, spec in want.items():
        if name in have and _options(have[name]):
            out += [f"{name} option {o}" for o in _options(spec) if o not in _options(have[name])]
    return out


def _block(lines: list[str], path: Sequence[str]) -> tuple[int, int, int] | None:
    """(line, end, indent) of the nested key `path` (its block is lines[line + 1:end])."""
    start, end, found = 0, len(lines), None
    for key in path:
        found = _key(lines, start, end, key)
        if found is None:
            return None
        start, end = found[0] + 1, _block_end(lines, found[0] + 1, end, found[1])
    assert found is not None
    return found[0], end, found[1]


INPUTS_PATH = ("on", "workflow_dispatch", "inputs")
OPTIONS_LINE = re.compile(r"^(?P<head>\s*options\s*:\s*)\[(?P<items>[^\]\n]*)\](?P<tail>\s*(?:#.*)?)$")


def _merged_options(have: list[str], want: list[str]) -> list[str]:
    out = list(have)
    for n, opt in enumerate(want):
        if opt in out:
            continue
        before = next((w for w in reversed(want[:n]) if w in out), None)
        out.insert(out.index(before) + 1 if before is not None else 0, opt)
    return out


def add_dispatch(config_text: str, template: str) -> str:
    """config_text with the template's missing dispatch inputs and choice options added (module doc)."""
    lines = config_text.splitlines(keepends=True)
    tlines = template.splitlines(keepends=True)
    want = _dispatch_inputs(template) or {}
    for _ in range(len(want) * 2 + 2):  # one change per pass, re-located each time
        have = _dispatch_inputs("".join(lines))
        if have is None:
            return "".join(lines)
        inputs = _block(lines, INPUTS_PATH)
        if inputs is None:
            raise WorkflowError(f"{configrepo.WORKFLOW}: can't locate `on.workflow_dispatch.inputs`")
        name = next((n for n in want if n not in have), None)
        if name is not None:
            src = _block(tlines, (*INPUTS_PATH, name))
            if src is None:
                raise WorkflowError(f"the product's template has no editable `{name}` input")
            chunk = tlines[src[0] : _last_content(tlines, src[0], src[1]) + 1]
            child = _child_indent(lines, inputs[0] + 1, inputs[1]) or inputs[2] + 2
            shift = child - src[2]
            moved = [
                (" " * shift + s if shift >= 0 else s[min(-shift, _indent(s)) :]) if s.strip() else s
                for s in chunk
            ]
            if moved and not moved[-1].endswith("\n"):
                moved[-1] += "\n"
            at = _last_content(lines, inputs[0], inputs[1]) + 1
            if at > 0 and not lines[at - 1].endswith("\n"):
                lines[at - 1] += "\n"
            lines[at:at] = moved
            continue
        todo = next(
            ((n, _options(s)) for n, s in want.items()
             if _options(have.get(n)) and set(_options(s)) - set(_options(have.get(n)))),
            None,
        )  # fmt: skip
        if todo is None:
            return "".join(lines)
        name, options = todo
        spec = _block(lines, (*INPUTS_PATH, name))
        opt = _key(lines, spec[0] + 1, spec[1], "options") if spec else None
        m = OPTIONS_LINE.match(lines[opt[0]].rstrip("\n")) if opt else None
        if opt is None or m is None:
            raise WorkflowError(
                f"{configrepo.WORKFLOW}: the `{name}` input's options aren't a one-line `[...]` list: add "
                + ", ".join(o for o in options if o not in _options(have.get(name)))
                + " by hand"
            )
        merged = _merged_options(_options(have.get(name)), options)
        end = "\n" if lines[opt[0]].endswith("\n") else ""
        lines[opt[0]] = f"{m['head']}[{', '.join(merged)}]{m['tail']}{end}"
    raise WorkflowError(f"{configrepo.WORKFLOW}: the dispatch inputs didn't converge")  # pragma: no cover


def sync(config_text: str, template: str) -> tuple[str, list[str]]:
    """Secrets and dispatch inputs the template has and config_text lacks, added. Returns (text, what)."""
    what = []
    text = config_text
    if absent := missing(text, template):
        text = add(text, absent)
        what.append(f"passes {', '.join(absent)}")
    if lacking := missing_dispatch(text, template):
        text = add_dispatch(text, template)
        what.append(f"manual run gets {', '.join(lacking)}")
    return text, what


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
