#!/usr/bin/env python3
"""Publish a release as a fresh public repository with one commit (docs/publishing.md).

    publish_fresh.py                          # dry run of HEAD: export, scan, report (changes nothing)
    publish_fresh.py --commit v1.2.0 --create ../smallworld-publish

1. **Export** the tree of `--commit` (`git archive`: tracked files only, modes kept), minus the entries of
   the in-repo `publish-exclude` file (the maintainer's planning and design history, which stays in the
   archived original repository). `CHANGELOG.md` is transformed: the public changelog starts at the release
   being published (plus `## [Unreleased]`), with one line saying where the earlier history is.
2. **Scan** every exported path and every file against
   - the denylist (`find_denylist`: `--denylist`, else `$SMALLWORLD_DENYLIST_TEXT` (the content) or
     `$SMALLWORLD_DENYLIST`, else `git config smallworld.denylist`, else `../mc-config/publish-denylist.txt`
     relative to the product checkout, i.e. in the private config repo; scripts/guard.py resolves it the
     same way): one case-insensitive regex per line, `#` comments,
     `## <category>` lines name the category of the patterns below them. It holds identifying data, so it is
     never part of this repository (refused if it would be exported);
   - the generic checks (`generic_problems`, also run on the working tree by `tests/test_publishable.py`):
     session links, emails outside reserved domains, public IP literals outside documentation ranges, the
     owner's name outside its allowed forms, personal timezone defaults, references to excluded paths.
   Allowed spans (`ALLOWED`: the public repository, the owner's GitHub account URL, the Java package, GitHub
   noreply addresses) are blanked before the denylist runs. Binary files are scanned as Latin-1 text, zip
   archives (jars, packs) member by member. Any hit refuses, with `path:line` and the denylist line that
   matched, never the matched text.
3. Only with `--create DEST`, and only on a clean scan: a new repository in DEST with ONE commit, author and
   committer `<owner> <ID+owner@users.noreply.github.com>` (ID from the GitHub API through `gh`, or
   `--noreply-id` offline), message `Smallworld vX.Y.Z`, no trailers, tagged `vX.Y.Z`; checked after
   writing. Then it prints, and never runs, the commands that publish it: it never pushes and never creates
   or changes a GitHub repository.

Every git/gh call goes through an injectable runner (`Runner`), like mcctl/pinmove.py.
"""

from __future__ import annotations

import argparse
import fnmatch
import io
import ipaddress
import os
import posixpath
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
import zlib
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Kind = Literal["text", "binary", "name"]
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]

ROOT = Path(__file__).resolve().parent.parent
PUBLIC_REPOSITORY = (
    "edvardk/smallworld"  # = mcctl.configrepo.PRODUCT_REPOSITORY once renamed (checked on --create)
)
OWNER = PUBLIC_REPOSITORY.split("/")[0]
PRODUCT_NAME = "Smallworld"
AUTHOR = "Edvard Kristiansen"  # copyright holder (LICENSE) and README byline: allowed in exactly this form
EXCLUDE_FILE = "publish-exclude"
CHANGELOG = "CHANGELOG.md"
DEFAULT_DENYLIST = Path("..") / "mc-config" / "publish-denylist.txt"  # relative to the product checkout
DENYLIST_ENV = "SMALLWORLD_DENYLIST"  # a path
DENYLIST_TEXT_ENV = "SMALLWORLD_DENYLIST_TEXT"  # the content itself (CI: an Actions secret)
DENYLIST_GIT_CONFIG = "smallworld.denylist"
ZIP_DEPTH = 3

_o = re.escape(OWNER)
# Spans the owner's name may appear in (blanked before any owner/denylist check).
ALLOWED = tuple(
    re.compile(p, re.I)
    for p in (
        rf"(?<![\w.-]){re.escape(PUBLIC_REPOSITORY)}(?![\w-])",  # the public repository (URLs, uses: lines)
        rf"github\.com/{_o}(?![\w/-])",  # the GitHub account
        rf"io[./$]github[./$]{_o}(?![\w-])",  # the Java package (source dir, javac $SwitchMap$)
        r"(?<![\w.+-])(?:\d+\+)?[\w.-]+@users\.noreply\.github\.com",  # GitHub noreply addresses
        rf"(?-i:{re.escape(AUTHOR)})",  # the author's name, exactly (LICENSE, README byline)
    )
)


@dataclass(frozen=True)
class Entry:
    mode: int  # git mode: 0o100644, 0o100755 or 0o120000 (symlink; data = target)
    data: bytes


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    category: str
    source: str  # where it was defined ("denylist:12", "generic")


@dataclass(frozen=True)
class Hit:
    path: str
    line: int | None  # None: the path itself matched
    category: str
    source: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else f"{self.path} (path)"
        return f"{where}: {self.category} ({self.source})"


class PublishError(Exception):
    pass


def default_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
    # The identity of the commit comes from -c user.* only: GIT_AUTHOR_*/GIT_COMMITTER_* would override it.
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_AUTHOR_", "GIT_COMMITTER_"))}
    return subprocess.run(list(argv), capture_output=True, text=True, check=False, env=env)


def _ok(run: Runner, argv: Sequence[str]) -> str:
    proc = run(argv)
    if proc.returncode != 0:
        raise PublishError(f"{shlex.join(argv)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout


# --- configuration files ---------------------------------------------------------------------------------


def parse_excludes(text: str) -> list[str]:
    """`publish-exclude`: one entry per line (`dir/`, a path, or an fnmatch glob), `#` comments."""
    return [s for line in text.splitlines() if (s := line.strip()) and not s.startswith("#")]


def is_excluded(path: str, excludes: Iterable[str]) -> bool:
    for entry in excludes:
        if entry.endswith("/"):
            if path.startswith(entry):
                return True
        elif path == entry or fnmatch.fnmatchcase(path, entry):
            return True
    return False


def parse_denylist(text: str, name: str = "denylist") -> list[Rule]:
    """One case-insensitive regex per line; `#` comments; `## <category>` names the following patterns."""
    rules, category = [], "uncategorised"
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("##"):
            category = s.lstrip("#").strip() or "uncategorised"
            continue
        if not s or s.startswith("#"):
            continue
        try:
            pattern = re.compile(s, re.I)
        except re.error as e:
            raise PublishError(f"{name}:{n}: not a valid regex ({e})") from e
        if pattern.search(""):
            raise PublishError(f"{name}:{n}: the pattern matches the empty string")
        rules.append(Rule(pattern, category, f"{name}:{n}"))
    if not rules:
        raise PublishError(f"{name}: no patterns (an empty denylist proves nothing)")
    return rules


@dataclass(frozen=True)
class DenylistSource:
    origin: str  # which setting named it (for messages; never the content)
    path: Path | None  # None: the content came from DENYLIST_TEXT_ENV
    text: str | None  # None: no file at `path`
    default: bool = False  # the built-in default path (a missing default isn't a configuration error)

    @property
    def name(self) -> str:
        return self.path.name if self.path is not None else DENYLIST_TEXT_ENV


def find_denylist(
    source: Path,
    explicit: Path | None = None,
    env: Mapping[str, str] | None = None,
    run: Runner = default_runner,
) -> DenylistSource:
    """The denylist to use (shared by publish_fresh.py and guard.py): `--denylist PATH`, else the content in
    $SMALLWORLD_DENYLIST_TEXT, else the path in $SMALLWORLD_DENYLIST, else `git config smallworld.denylist`
    (`~` expanded, relative to the checkout), else DEFAULT_DENYLIST next to the checkout. Empty values count
    as unset (a fork pull request's CI run gets an empty secret)."""
    env = os.environ if env is None else env

    def at(origin: str, path: Path, default: bool = False) -> DenylistSource:
        path = (source / path.expanduser()).resolve()
        text = path.read_text(encoding="utf-8") if path.is_file() else None
        return DenylistSource(origin, path, text, default)

    if explicit is not None:
        return at("--denylist", explicit)
    if (text := env.get(DENYLIST_TEXT_ENV) or "").strip():
        return DenylistSource(f"${DENYLIST_TEXT_ENV}", None, text)
    if value := (env.get(DENYLIST_ENV) or "").strip():
        return at(f"${DENYLIST_ENV}", Path(value))
    proc = run(["git", "-C", str(source), "config", "--path", "--get", DENYLIST_GIT_CONFIG])
    if proc.returncode == 0 and (value := proc.stdout.strip()):
        return at(f"git config {DENYLIST_GIT_CONFIG}", Path(value))
    return at("default", DEFAULT_DENYLIST, default=True)


# --- export ------------------------------------------------------------------------------------------------


def export(run: Runner, source: Path, commit: str) -> dict[str, Entry]:
    """Every tracked file of `commit` (path -> Entry), via `git archive` (binary-safe, modes kept)."""
    with tempfile.TemporaryDirectory(prefix="publish-") as tmp:
        tar_path = Path(tmp) / "tree.tar"
        _ok(run, ["git", "-C", str(source), "archive", "--format=tar", "-o", str(tar_path), commit])
        out: dict[str, Entry] = {}
        with tarfile.open(tar_path) as tar:
            for m in tar.getmembers():
                if m.issym():
                    out[m.name] = Entry(0o120000, m.linkname.encode())
                elif m.isfile():
                    f = tar.extractfile(m)
                    assert f is not None
                    out[m.name] = Entry(0o100755 if m.mode & 0o111 else 0o100644, f.read())
        return out


def changelog_line(version: str) -> str:
    return (
        f"Releases before {version} were developed in a private repository, kept as an archive; "
        "their history stays there."
    )


def public_changelog(text: str, version: str) -> str:
    """The changelog as published: the preamble, `## [Unreleased]`, the `## [version]` section, and one line
    pointing at the archived history."""
    parts = re.split(r"(?m)^(?=## \[)", text)
    preamble, sections = parts[0], parts[1:]
    keep = {"Unreleased", version}
    kept = [s for s in sections if (m := re.match(r"## \[([^\]]+)\]", s)) and m.group(1) in keep]
    if not any(s.startswith(f"## [{version}] - ") for s in kept):
        raise PublishError(f"{CHANGELOG} has no `## [{version}] - YYYY-MM-DD` section")
    if not any(s.startswith("## [Unreleased]") for s in kept):
        kept.insert(0, "## [Unreleased]\n\n")
    body = "".join(kept).rstrip("\n")
    return f"{preamble}{body}\n\n{changelog_line(version)}\n"


def version_of(files: dict[str, Entry]) -> str:
    entry = files.get("pyproject.toml")
    if entry is None:
        raise PublishError("the exported tree has no pyproject.toml")
    return str(tomllib.loads(entry.data.decode())["project"]["version"])


def select(files: dict[str, Entry], version: str) -> tuple[dict[str, Entry], list[str]]:
    """The files to publish (exclusions applied, CHANGELOG transformed) and the exclude entries."""
    if EXCLUDE_FILE not in files:
        raise PublishError(f"the exported tree has no {EXCLUDE_FILE}")
    excludes = parse_excludes(files[EXCLUDE_FILE].data.decode())
    out = {p: e for p, e in files.items() if not is_excluded(p, excludes)}
    if CHANGELOG in out:
        out[CHANGELOG] = Entry(
            out[CHANGELOG].mode, public_changelog(out[CHANGELOG].data.decode(), version).encode()
        )
    return out, excludes


# --- scanning ----------------------------------------------------------------------------------------------


def mask_allowed(text: str) -> str:
    """`text` with every allowed span blanked (same length, so columns and lines stay put)."""
    for rx in ALLOWED:
        text = rx.sub(lambda m: " " * len(m.group(0)), text)
    return text


def _zip_members(path: str, data: bytes) -> list[tuple[str, str, bytes | None]] | None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return [
                (f"{path}!/{i.filename}", i.filename, None if i.is_dir() else z.read(i)) for i in z.infolist()
            ]
    except (zipfile.BadZipFile, NotImplementedError, RuntimeError, ValueError):
        return None


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
PNG_PIXELS = {b"IDAT", b"fdAT"}  # compressed pixel data: random bytes, checked by eye (docs/publishing.md)


def _png_metadata(data: bytes) -> bytes | None:
    """A well-formed PNG's every chunk but its pixel data (text chunks, zTXt/iTXt inflated), or None."""
    if not data.startswith(PNG_SIGNATURE):
        return None
    out, pos = [], len(PNG_SIGNATURE)
    while pos < len(data):
        if pos + 12 > len(data):
            return None
        size = int.from_bytes(data[pos : pos + 4], "big")
        kind, body = data[pos + 4 : pos + 8], data[pos + 8 : pos + 8 + size]
        if len(body) != size:
            return None
        pos += 12 + size
        if kind in PNG_PIXELS:
            continue
        out.append(kind + b" " + body)
        key, _, rest = body.partition(b"\0")
        packed = None  # zTXt: method byte, then zlib; iTXt: flag, method, language\0, translated\0, text
        if kind == b"zTXt":
            packed = rest[1:]
        elif kind == b"iTXt" and rest[:1] == b"\1":
            packed = rest[2:].split(b"\0", 2)[-1]
        if packed is not None:
            try:
                out.append(key + b" " + zlib.decompress(packed))
            except zlib.error:
                return None
        if kind == b"IEND":
            break
    return b"\n".join(out)


def texts(path: str, data: bytes, depth: int = 0) -> Iterator[tuple[str, str, Kind]]:
    """(label, text, kind) triples to scan for one file: UTF-8 text as is; zip archives (jars, packs)
    member by member, their names too ("name"); a well-formed PNG without its compressed pixel data (whose
    random bytes match short patterns by chance; the pixels are reviewed by eye); anything else decoded as
    Latin-1 ("binary"), so ASCII strings inside binaries are still found."""
    png = _png_metadata(data)
    if png is not None:
        yield path, png.decode("latin-1"), "binary"
        return
    members = _zip_members(path, data) if depth < ZIP_DEPTH and data[:4] == b"PK\x03\x04" else None
    if members is not None:
        for label, name, content in members:
            yield label, name, "name"
            if content is not None:
                yield from texts(label, content, depth + 1)
        return
    if b"\0" not in data:
        try:
            yield path, data.decode("utf-8"), "text"
            return
        except UnicodeDecodeError:
            pass
    yield path, data.decode("latin-1"), "binary"


def scan_text(label: str, text: str, rules: Sequence[Rule]) -> list[Hit]:
    hits = []
    for n, line in enumerate(mask_allowed(text).splitlines(), 1):
        hits += [Hit(label, n, r.category, r.source) for r in rules if r.pattern.search(line)]
    return hits


def scan(files: dict[str, Entry], rules: Sequence[Rule]) -> list[Hit]:
    """Denylist hits in the paths and contents of `files`."""
    hits: list[Hit] = []
    for path in sorted(files):
        masked = mask_allowed(path)
        hits += [Hit(path, None, r.category, r.source) for r in rules if r.pattern.search(masked)]
        for label, text, kind in texts(path, files[path].data):
            if kind == "name":
                masked = mask_allowed(text)
                hits += [Hit(label, None, r.category, r.source) for r in rules if r.pattern.search(masked)]
            else:
                hits += scan_text(label, text, rules)
    return hits


# --- generic checks (no identifying data: also tests/test_publishable.py) ----------------------------------

# Built from parts so this file doesn't match itself.
SESSION_LINK = re.compile("claude" + r"\.ai/code/" + "session_", re.I)
EMAIL = re.compile(r"(?<![\w.+-])([\w.+-]+)@([\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,})(?![\w-])")
EMAIL_DOMAINS = re.compile(
    r"(?:^|\.)(?:example(?:\.com|\.net|\.org)?|test|invalid|internal|localhost|users\.noreply\.github\.com)$",
    re.I,
)
# GitHub's own addresses (support@: the Signed-off-by line of every Dependabot commit).
EMAIL_ADDRESSES = {"noreply@anthropic.com", "noreply@github.com", "git@github.com", "support@github.com"}
NOT_EMAIL = re.compile(r"\.(?:service|timer|socket|target|mount|path|slice|scope)$")  # systemd unit@instance
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?![\w.-])")
IPV6 = re.compile(r"(?<![\w:.])[0-9A-Fa-f]{0,4}(?::[0-9A-Fa-f]{0,4}){2,7}(?![\w:.])")
# Documentation/test ranges and well-known public resolvers: not anyone's server.
IP_ALLOWED = tuple(
    ipaddress.ip_network(n)
    for n in (
        "192.0.2.0/24",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "1.1.1.1/32",
        "1.0.0.1/32",
        "8.8.8.8/32",
        "8.8.4.4/32",
        "9.9.9.9/32",
        "149.112.112.112/32",
        "2001:db8::/32",
        "3fff::/20",  # the second IPv6 documentation range (RFC 9637)
        "3ffe::/16",  # the retired 6bone range
        "2620:fe::/48",  # Quad9
        "2001:4860:4860::/48",  # Google Public DNS
        "2606:4700:4700::/48",  # Cloudflare DNS
        "2000::/128",  # 2000::/3 written as an address (the global unicast prefix)
    )
)
TIMEZONE_FILES = ("mcctl/schema.py", "mcctl/init.py")
TIMEZONE = re.compile(r"\b(?:Europe|America|Asia|Africa|Australia|Pacific|Atlantic|Indian)/[A-Z]\w+")


def _public_ip(token: str) -> bool:
    try:
        addr = ipaddress.ip_address(token)
    except ValueError:
        return False
    if (
        not addr.is_global
        or addr.is_multicast
        or any(addr in net for net in IP_ALLOWED if net.version == addr.version)
    ):
        return False
    return not (addr.version == 6 and token.lower().startswith("ffff:"))  # a netmask, not an address


def _email_ok(local: str, domain: str) -> bool:
    address = f"{local}@{domain}".lower()
    return bool(EMAIL_DOMAINS.search(domain)) or address in EMAIL_ADDRESSES or bool(NOT_EMAIL.search(domain))


def _mentions(excludes: Sequence[str], present: Iterable[str]) -> list[tuple[str, re.Pattern[str]]]:
    """Patterns for the exclude entries that actually left something out (a mention of a path the public
    repository won't have is a broken reference)."""
    present = list(present)
    out = []
    for entry in excludes:
        if any(ch in entry for ch in "*?[") or not any(is_excluded(p, [entry]) for p in present):
            continue
        out.append((entry, re.compile(rf"(?<![\w-]){re.escape(entry)}(?![\w-])")))
    return out


MD_LINK = re.compile(r"\]\(([^)\s#]+)(?:#[^)\s]*)?\)")


def generic_problems(
    files: dict[str, Entry], excludes: Sequence[str] = (), all_paths: Iterable[str] = ()
) -> list[Hit]:
    """Generic anonymity problems in `files` (the export). `all_paths`: every tracked path before the
    exclusions (to find references to excluded ones)."""
    mentions = _mentions(excludes, all_paths)
    hits: list[Hit] = []
    owner = re.compile(rf"(?<![\w]){_o}(?![\w])", re.I)

    def add(path: str, n: int | None, category: str) -> None:
        hits.append(Hit(path, n, category, "generic"))

    for path in sorted(files):
        if owner.search(mask_allowed(path)):
            add(path, None, "owner")
        if path == EXCLUDE_FILE:
            continue
        for label, text, kind in texts(path, files[path].data):
            if kind == "name":
                if owner.search(mask_allowed(text)):
                    add(label, None, "owner")
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if SESSION_LINK.search(line):
                    add(label, n, "session-link")
                if kind == "text":  # emails and IPs: text only (random bytes look like both)
                    if any(not _email_ok(m.group(1), m.group(2)) for m in EMAIL.finditer(line)):
                        add(label, n, "email")
                    if any(_public_ip(m.group(0)) for m in (*IPV4.finditer(line), *IPV6.finditer(line))):
                        add(label, n, "public-ip")
                if owner.search(mask_allowed(line)):
                    add(label, n, "owner")
                if path in TIMEZONE_FILES and TIMEZONE.search(line):
                    add(label, n, "timezone-default")
                if label == path and any(rx.search(line) for _, rx in mentions):
                    add(label, n, "excluded-reference")
                elif label == path and path.endswith(".md"):
                    for m in MD_LINK.finditer(line):
                        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), m.group(1)))
                        if not re.match(r"^[a-z]+:", m.group(1)) and is_excluded(target, excludes):
                            add(label, n, "excluded-reference")
                            break
    return hits


# --- the fresh repository ----------------------------------------------------------------------------------


def noreply_id(run: Runner, owner: str = OWNER) -> int:
    """The owner's numeric GitHub id (for the noreply address), from `gh api users/<owner>`."""
    out = _ok(run, ["gh", "api", f"users/{owner}", "--jq", ".id"]).strip()
    if not out.isdigit():
        raise PublishError(f"gh api users/{owner} returned no numeric id")
    return int(out)


def identity(user_id: int, owner: str = OWNER) -> tuple[str, str]:
    return owner, f"{user_id}+{owner}@users.noreply.github.com"


def commit_message(version: str) -> str:
    return f"{PRODUCT_NAME} v{version}"


def write_tree(files: dict[str, Entry], dest: Path) -> None:
    for path, entry in sorted(files.items()):
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        if entry.mode == 0o120000:
            os.symlink(entry.data.decode(), target)
        else:
            target.write_bytes(entry.data)
            target.chmod(0o755 if entry.mode == 0o100755 else 0o644)


def create(run: Runner, files: dict[str, Entry], dest: Path, version: str, user_id: int) -> str:
    """A new repository in `dest` holding `files` as ONE commit, tagged vX.Y.Z; returns the commit SHA."""
    if dest.exists() and any(dest.iterdir()):
        raise PublishError(f"{dest} exists and isn't empty")
    dest.mkdir(parents=True, exist_ok=True)
    name, email = identity(user_id)
    git = ["git", "-C", str(dest)]
    cfg = ["-c", "core.autocrlf=false", "-c", "core.safecrlf=false"]
    _ok(run, ["git", "-c", "init.defaultBranch=main", "init", "-q", str(dest)])
    write_tree(files, dest)
    # -f: the export is exactly what git archive gave, including files the .gitignore patterns would match.
    _ok(run, [*git, *cfg, "add", "-A", "-f", "."])
    tree = _ok(run, [*git, "write-tree"]).strip()
    # commit-tree: no hooks, no commit template, no trailers; the identity only from -c user.*.
    ident = ["-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false"]
    sha = _ok(run, [*git, *ident, "commit-tree", tree, "-m", commit_message(version)]).strip()
    _ok(run, [*git, "update-ref", "refs/heads/main", sha])
    _ok(run, [*git, "tag", f"v{version}", sha])
    verify(run, dest, sha, files, version, user_id)
    return sha


def verify(run: Runner, dest: Path, sha: str, files: dict[str, Entry], version: str, user_id: int) -> None:
    """Re-read what was written: one parentless commit, the right identity and message, every file."""
    git = ["git", "-C", str(dest)]
    out = _ok(run, [*git, "log", "-1", "--format=%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B", sha])
    parents, an, ae, cn, ce, body = out.split("\0")
    name, email = identity(user_id)
    problems = []
    if parents.strip():
        problems.append("the commit has a parent")
    if (an, ae, cn, ce) != (name, email, name, email):
        problems.append(f"author/committer are {an} <{ae}> / {cn} <{ce}>, not {name} <{email}>")
    if body.strip() != commit_message(version):
        problems.append("the commit message isn't exactly " + repr(commit_message(version)))
    listed = _ok(run, [*git, "ls-tree", "-r", "-z", "--name-only", sha]).split("\0")
    if sorted(p for p in listed if p) != sorted(files):
        problems.append("the commit's files differ from the export")
    if _ok(run, [*git, "rev-parse", f"v{version}^{{commit}}"]).strip() != sha:
        problems.append(f"tag v{version} doesn't point at the commit")
    if problems:
        raise PublishError("the fresh repository is wrong: " + "; ".join(problems))


def product_repository(files: dict[str, Entry]) -> str | None:
    """mcctl/configrepo.py's PRODUCT_REPOSITORY in the export (the name config repos will pin)."""
    entry = files.get("mcctl/configrepo.py")
    m = re.search(rb'^PRODUCT_REPOSITORY = "([^"]+)"', entry.data, re.M) if entry else None
    return m.group(1).decode() if m else None


def old_repository(run: Runner, source: Path) -> str:
    """owner/name of the source checkout's origin (the repository to archive), or a placeholder."""
    proc = run(["git", "-C", str(source), "remote", "get-url", "origin"])
    url = proc.stdout.strip() if proc.returncode == 0 else ""
    m = re.search(r"github\.com[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)
    return m.group(1) if m else "<owner>/<old-repository>"


def next_steps(dest: Path, version: str, old: str, repo: str = PUBLIC_REPOSITORY) -> list[str]:
    d = shlex.quote(str(dest))
    return [
        f"gh repo create {repo} --public --description 'Minecraft server, as code'",
        f"git -C {d} remote add origin https://github.com/{repo}.git",
        f"git -C {d} push -u origin main",
        f"git -C {d} push origin v{version}",
        f"python3 -m mcctl product-setup --repo {repo}",
        f"gh repo archive {old} --yes",
    ]


# --- CLI ---------------------------------------------------------------------------------------------------


def report(hits: Sequence[Hit], out: Callable[[str], None], detail: bool = True) -> None:
    for h in hits if detail else ():
        out(f"  {h}")
    counts = Counter(h.category for h in hits)
    out("hits by category: " + (", ".join(f"{c} {n}" for c, n in sorted(counts.items())) or "none"))


def main(
    argv: Sequence[str] | None = None, run: Runner = default_runner, out: Callable[[str], None] = print
) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--source", type=Path, default=ROOT, help="the product checkout (default: %(default)s)")
    p.add_argument("--commit", default="HEAD", help="the commit to publish (default: HEAD)")
    p.add_argument("--version", help="X.Y.Z (default: pyproject.toml at the commit)")
    p.add_argument(
        "--denylist",
        type=Path,
        help=f"default: ${DENYLIST_TEXT_ENV}, ${DENYLIST_ENV}, git config {DENYLIST_GIT_CONFIG}, "
        f"else <source>/{DEFAULT_DENYLIST}",
    )
    p.add_argument("--create", type=Path, metavar="DEST", help="build the one-commit repository in DEST")
    p.add_argument("--noreply-id", type=int, help="the owner's numeric GitHub id (default: asked via gh)")
    p.add_argument("--quiet", action="store_true", help="only the summary, not every hit")
    args = p.parse_args(argv)
    source = args.source.resolve()
    try:
        denylist = find_denylist(source, args.denylist, run=run)
        everything = export(run, source, args.commit)
        version = (args.version or version_of(everything)).removeprefix("v")
        files, excludes = select(everything, version)
        if denylist.path is not None and denylist.path.is_relative_to(source):
            rel = denylist.path.relative_to(source).as_posix()
            if rel in files:
                raise PublishError(f"the denylist {rel} would be published: keep it outside this repository")
        if denylist.text is None:
            raise PublishError(f"no denylist at {denylist.path} ({denylist.origin}; --denylist PATH)")
        rules = parse_denylist(denylist.text, denylist.name)
        hits = scan(files, rules) + generic_problems(files, excludes, everything)
        left_out = len(everything) - len(files)
        out(f"{args.commit} as v{version}: {len(files)} files published, {left_out} excluded")
        if hits:
            out(f"REFUSED: {len(hits)} hit(s)")
            report(hits, out, detail=not args.quiet)
            return 1
        out("clean: no denylist or generic hits")
        named = product_repository(files)
        if named != PUBLIC_REPOSITORY:
            out(f"note: PRODUCT_REPOSITORY is {named}, not {PUBLIC_REPOSITORY} (--create refuses)")
        if args.create is None:
            return 0
        if named != PUBLIC_REPOSITORY:
            raise PublishError(f"PRODUCT_REPOSITORY at {args.commit} is {named}, not {PUBLIC_REPOSITORY}")
        dest = args.create.resolve()
        if dest.is_relative_to(source):
            raise PublishError(f"{dest} is inside the product checkout: pick a directory outside it")
        user_id = args.noreply_id if args.noreply_id is not None else noreply_id(run)
        sha = create(run, files, dest, version, user_id)
    except PublishError as e:
        out(f"publish_fresh: {e}")
        return 2
    out(f"created {dest}: one commit {sha} ({commit_message(version)}), tag v{version}")
    out("Nothing has been pushed. To publish (docs/publishing.md), run yourself:")
    for line in next_steps(dest, version, old_repository(run, source)):
        out(f"  {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
