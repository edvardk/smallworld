"""The config repo's `.env` (design §3): admin credentials, mode 0600, never committed, never in CI.

mcctl reads it itself, so `set -a; source .env` is no longer needed. Variables already set in the shell win,
so a one-off `TAILSCALE_API_KEY=... mcctl apply` works, except the Terraform state credentials (`AWS_*`),
which come from .env only. Values are never printed, not even in parse errors.

The supported subset (a strict subset of what `sh` and the usual dotenv loaders agree on), one entry per line:

    # a comment                 blank lines and lines starting with `#` are ignored
    KEY=value                   unquoted: trailing whitespace stripped; ` #` or a tab + `#` starts a comment,
    KEY=value # note              a `#` with no whitespace before it is part of the value (abc#def)
    export KEY=value            an optional `export ` prefix
    KEY='v a l # x'             single quotes: literal, nothing inside is special
    KEY="v \"q\" \\ # x" # note  double quotes: `\"` and `\\` are the only escapes
    KEY=                        empty (treated as unset)
    KEY=dl://title/password     P14: a Dashlane secret reference, resolved by `merged()` through dcli

After a closing quote only whitespace and an optional ` # comment` may follow. Not supported (an error, never
a guess): multi-line values, unterminated quotes, `$VAR` expansion is simply not done (`$` is literal),
spaces around `=`, and keys that aren't `[A-Za-z_][A-Za-z0-9_]*`. `write()` emits only this subset.
"""

from __future__ import annotations

import os
import re
import stat
from collections.abc import Callable, Mapping
from pathlib import Path

ENV_FILE = ".env"
_LINE = re.compile(r"(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)")
_TRAILER = re.compile(r"[ \t]*(?:[ \t]#.*)?")  # after a closing quote: whitespace, optional comment


class EnvFileError(RuntimeError):
    pass


def _value(raw: str, n: int) -> str:
    """The value part of line n (everything after `=`), per the subset above."""
    if raw[:1] == "'":
        end = raw.find("'", 1)
        if end < 0:
            raise EnvFileError(f"{ENV_FILE} line {n}: unterminated single quote")
        value, rest = raw[1:end], raw[end + 1 :]
    elif raw[:1] == '"':
        chars: list[str] = []
        i = 1
        while i < len(raw) and raw[i] != '"':
            if raw[i] == "\\" and i + 1 < len(raw) and raw[i + 1] in '"\\':
                i += 1
            chars.append(raw[i])
            i += 1
        if i >= len(raw):
            raise EnvFileError(f"{ENV_FILE} line {n}: unterminated double quote")
        value, rest = "".join(chars), raw[i + 1 :]
    else:
        m = re.search(r"[ \t]#", raw)
        return (raw[: m.start()] if m else raw).rstrip(" \t")
    if not _TRAILER.fullmatch(rest):
        raise EnvFileError(f"{ENV_FILE} line {n}: only a ` # comment` may follow a quoted value")
    return value


def parse(text: str) -> dict[str, str]:
    """KEY -> value. A key defined twice is an error: shells and dotenv loaders disagree on which one wins."""
    out: dict[str, str] = {}
    seen: dict[str, int] = {}
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.lstrip(" \t")
        if not line.strip() or line.startswith("#"):
            continue
        m = _LINE.fullmatch(line)
        if m is None:
            raise EnvFileError(f"{ENV_FILE} line {n}: expected KEY=VALUE")  # the line itself may be a secret
        key = m.group(1)
        if key in seen:
            raise EnvFileError(f"{ENV_FILE} line {n}: {key} is already set on line {seen[key]}; keep one")
        seen[key] = n
        out[key] = _value(m.group(2), n)
    return out


def _quote(value: str) -> str:
    """A value in the subset: bare when that round-trips, else double-quoted."""
    if "\n" in value or "\r" in value:
        raise EnvFileError("a .env value can't contain a newline")
    if value and re.fullmatch(r"[A-Za-z0-9_./:@%+,=~^-]+", value):
        return value
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write(config_root: Path, updates: Mapping[str, str]) -> Path:
    """Set KEY=VALUE entries in <config>/.env (created mode 0600, kept 0600), preserving every other line.

    An existing `KEY=` line is replaced in place; new keys are appended. The file is written atomically and
    is never group/world readable, not even for an instant."""
    path = config_root / ENV_FILE
    old = path.read_text().splitlines() if path.is_file() else []
    pending = dict(updates)
    lines: list[str] = []
    for raw in old:
        m = _LINE.fullmatch(raw.lstrip(" \t"))
        if m and m.group(1) in updates:
            if m.group(1) in pending:  # the first definition is replaced in place, any later one dropped
                lines.append(f"{m.group(1)}={_quote(pending.pop(m.group(1)))}")
            continue
        lines.append(raw)
    lines += [f"{k}={_quote(v)}" for k, v in pending.items()]
    text = "\n".join(lines) + "\n"
    parse(text)  # never write something we can't read back
    tmp = path.with_name(f".{ENV_FILE}.tmp-{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return path


def mode_problem(path: Path) -> str | None:
    """A message if group or others can read/write the file (it holds admin tokens)."""
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return f"{path} is mode {mode:04o}; it holds admin tokens: chmod 600 (or `mcctl doctor --fix`)"
    return None


def load(config_root: Path, *, require_private: bool = True) -> dict[str, str]:
    """Non-empty values from <config>/.env (missing file = {}). Refuses a group/world-accessible file."""
    path = config_root / ENV_FILE
    if not path.is_file():
        return {}
    if require_private and (problem := mode_problem(path)):
        raise EnvFileError(problem)
    return {k: v for k, v in parse(path.read_text()).items() if v != ""}


def _is_state_credential(key: str) -> bool:
    """AWS_* configure the Terraform state backend (S3 API of R2): they must be this config repo's."""
    return key.startswith("AWS_")


def merged(
    config_root: Path,
    shell: Mapping[str, str] | None = None,
    resolve: Callable[[Mapping[str, str]], dict[str, str]] | None = None,
) -> dict[str, str]:
    """The shell environment with .env filling in what the shell doesn't set (non-empty shell values win).

    Exception: the Terraform state credentials (every AWS_* variable) come from .env only. A shell's AWS_*
    (another project's keys, an AWS_PROFILE) would point Terraform at another state, so they are dropped;
    `shadowed()` names them for a warning.

    P14: values that are Dashlane references (`dl://...`) are replaced by the secret (`resolve`, default
    `dashlane.DEFAULT.resolve`: `dcli read`), in memory only; a reference that doesn't resolve raises
    `dashlane.DashlaneError` (naming the key, never a value)."""
    from mcctl import dashlane

    base = {k: v for k, v in (os.environ if shell is None else shell).items() if not _is_state_credential(k)}
    for key, value in load(config_root).items():
        if _is_state_credential(key) or not base.get(key):
            base[key] = value
    return (resolve or dashlane.DEFAULT.resolve)(base)


def shadowed(config_root: Path, shell: Mapping[str, str] | None = None) -> list[str]:
    """Names of the shell's AWS_* variables that mcctl ignores because they differ from .env (or .env lacks
    them). Names only: values are never shown."""
    env = load(config_root, require_private=False)
    shell = os.environ if shell is None else shell
    return sorted(k for k, v in shell.items() if _is_state_credential(k) and v and env.get(k) != v)
