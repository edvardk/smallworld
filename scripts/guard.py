#!/usr/bin/env python3
"""Anonymity safeguards on every commit, after publishing (docs/publishing.md "After publishing").

    guard.py staged                 # pre-commit: the staged files (paths and contents)
    guard.py commit-msg FILE        # commit-msg: the message, the author and committer identity
    guard.py range BASE HEAD        # pre-push and CI: every commit in BASE..HEAD (BASE "-": all of HEAD)
    guard.py tree [REV]             # nightly/releases: one revision, exactly as publish_fresh.py scans it
    guard.py install                # core.hooksPath = .githooks; checks the identity, the denylist, gitleaks

The checks are scripts/publish_fresh.py's (imported, never copied): the private denylist (one
case-insensitive regex per line, `find_denylist`: `--denylist PATH`, `$SMALLWORLD_DENYLIST_TEXT`,
`$SMALLWORLD_DENYLIST`, `git config smallworld.denylist`, `../mc-config/publish-denylist.txt`) and the
generic checks that make sense on a part of the tree (session links, emails outside reserved domains, public
IPs, the owner's name outside its allowed forms, timezone defaults). Paths listed in `publish-exclude` may
never be added. Commit identities must be GitHub noreply addresses (or reserved domains). Without a denylist
the generic checks still run, with a warning; `git config smallworld.requireDenylist true` (or
`--require-denylist`) makes that a failure. `staged` also runs gitleaks when it is on PATH.

CI logs are public: the output never contains a denylist pattern, the text it matched or an email address.
A denylist hit is `path:line: denylist rule N` (N: the rule's 1-based index among the file's patterns); a
path that itself matches the denylist is shown as its position in the list of scanned files.

Exit codes: 0 clean, 1 hits, 2 usage or configuration error. Stdlib only (any python3 >= 3.11): the hooks
run it without the venv.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import publish_fresh as pf

# git (and gitleaks) calls: argv, optional stdin bytes -> bytes output. Tests inject their own.
GitRunner = Callable[..., subprocess.CompletedProcess[bytes]]
Out = Callable[[str], None]

REQUIRE_CONFIG = "smallworld.requireDenylist"
HOOKS_PATH = ".githooks"
ZERO_SHA = "0" * 40
NOREPLY_FIX = "git config user.email <ID>+<login>@users.noreply.github.com"
SCISSORS = re.compile(r"^# -+ >8 -+$", re.M)  # `git commit -v`: the diff below it isn't the message
# A changelog already in its public form (publish_fresh.public_changelog's closing line): scanned whole.
# GitHub's merge-commit headline names the pull request's branch as <owner>/<branch>.
GITHUB_MERGE = re.compile(rf"^Merge pull request #\d+ from {re.escape(pf.OWNER)}/\S+$", re.I)
PUBLIC_CHANGELOG = re.compile(r"^Releases before \S+ were developed in a private repository", re.M)


class GuardError(Exception):
    pass


def default_git(argv: Sequence[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(list(argv), input=stdin, capture_output=True, check=False)


@dataclass
class Git:
    """`git -C <repo> ...` through an injectable runner (bytes in, bytes out)."""

    repo: Path
    runner: GitRunner = default_git

    def run(self, *args: str, stdin: bytes | None = None, check: bool = True) -> bytes:
        argv = ["git", "-C", str(self.repo), *args]
        proc = self.runner(argv, stdin) if stdin is not None else self.runner(argv)
        if check and proc.returncode != 0:
            err = (proc.stderr or b"").decode(errors="replace").strip()
            raise GuardError(f"git {' '.join(args[:3])} failed: {err}")
        return proc.stdout

    def text(self, *args: str, check: bool = True) -> str:
        return self.run(*args, check=check).decode("utf-8", errors="replace")

    def ok(self, *args: str) -> bool:
        argv = ["git", "-C", str(self.repo), *args]
        return self.runner(argv).returncode == 0

    def config(self, key: str, *flags: str) -> str | None:
        argv = ["git", "-C", str(self.repo), "config", *flags, "--get", key]
        proc = self.runner(argv)
        value = (proc.stdout or b"").decode(errors="replace").strip()
        return value if proc.returncode == 0 and value else None

    def text_runner(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        """publish_fresh's Runner (text) on top of this one."""
        proc = self.runner(list(argv))
        return subprocess.CompletedProcess(
            proc.args,
            proc.returncode,
            (proc.stdout or b"").decode(errors="replace"),
            (proc.stderr or b"").decode(errors="replace"),
        )

    def blobs(self, specs: Sequence[str]) -> list[bytes | None]:
        """Each `<rev>:<path>`'s content (`:<path>`: the index); None for a missing or non-blob object."""
        if not specs:
            return []
        if any("\n" in s for s in specs):
            raise GuardError("a path contains a newline: rename it")
        data = self.run("cat-file", "--batch", stdin=("\n".join(specs) + "\n").encode())
        out: list[bytes | None] = []
        pos = 0
        for _ in specs:
            end = data.index(b"\n", pos)
            header = data[pos:end].split()
            pos = end + 1
            if len(header) == 3 and header[2].isdigit():
                size = int(header[2])
                body = data[pos : pos + size]
                pos += size + 1
                out.append(body if header[1] == b"blob" else None)
            else:  # "<spec> missing" / "ambiguous"
                out.append(None)
        return out


# --- the checks --------------------------------------------------------------------------------------------


@dataclass
class Report:
    rules: list[pf.Rule] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)

    def rule_number(self, source: str) -> int:
        return next(i for i, r in enumerate(self.rules, 1) if r.source == source)

    def denied(self, text: str) -> int | None:
        """The first denylist rule (1-based) matching `text` (allowed spans blanked), or None."""
        masked = pf.mask_allowed(text)
        return next((i for i, r in enumerate(self.rules, 1) if r.pattern.search(masked)), None)

    def add(self, line: str) -> None:
        self.lines.append(line)


def _shown(path: str, order: Sequence[str], report: Report) -> str:
    """`path` as it may appear in a public log: hidden behind its position when it matches the denylist."""
    top = path.split("!/")[0]
    if report.denied(path) is None:
        return path
    k = order.index(top) + 1 if top in order else 0
    return f"<file {k} of {len(order)} scanned: its path matches the denylist>"


def _loc(path: str, line: int | None, order: Sequence[str], report: Report) -> str:
    shown = _shown(path, order, report)
    return f"{shown}:{line}" if line is not None else f"{shown} (path)"


def scan_files(
    files: dict[str, pf.Entry],
    report: Report,
    where: str = "",
    excludes: Sequence[str] = (),
    everything: Sequence[str] = (),
) -> None:
    """Denylist and generic checks on `files`. Without `excludes`/`everything` (a part of the tree) the
    generic checks are the per-file ones: references to excluded paths need the whole tree."""
    order = sorted(files)
    for hit in pf.scan(files, report.rules):
        loc = _loc(hit.path, hit.line, order, report)
        report.add(f"{where}{loc}: denylist rule {report.rule_number(hit.source)}")
    for hit in pf.generic_problems(files, excludes, everything):
        report.add(f"{where}{_loc(hit.path, hit.line, order, report)}: {hit.category}")


def scan_message(text: str, report: Report, where: str) -> None:
    """A commit message: denylist, session links, emails outside reserved domains."""
    for n, line in enumerate(text.splitlines(), 1):
        if (rule := report.denied(GITHUB_MERGE.sub(" ", line))) is not None:
            report.add(f"{where}:{n}: denylist rule {rule}")
        if pf.SESSION_LINK.search(line):
            report.add(f"{where}:{n}: session-link (commit messages carry no session links)")
        if any(not pf._email_ok(m.group(1), m.group(2)) for m in pf.EMAIL.finditer(line)):
            report.add(f"{where}:{n}: email outside the reserved domains")


def email_ok(email: str) -> bool:
    """A commit identity's email: a GitHub noreply address (bots too) or a reserved domain."""
    local, at, domain = email.strip().rpartition("@")
    if not (local and at and domain):
        return False
    return bool(pf.EMAIL_DOMAINS.search(domain)) or email.strip().lower() in pf.EMAIL_ADDRESSES


def check_identity(role: str, name: str, email: str, report: Report, where: str = "") -> None:
    """Author or committer: the name against the denylist (the owner's login is the public identity), the
    email a noreply address."""
    if name.strip().lower() != pf.OWNER.lower() and (rule := report.denied(name)) is not None:
        report.add(f"{where}{role} name: denylist rule {rule}")
    if not email_ok(email):
        report.add(f"{where}{role} email isn't a GitHub noreply address (fix: {NOREPLY_FIX})")


def _ident(raw: str) -> tuple[str, str]:
    """`Name <email> 1700000000 +0000` (git var) -> (name, email)."""
    m = re.match(r"^(.*?)\s*<([^>]*)>", raw.strip())
    return (m.group(1), m.group(2)) if m else (raw.strip(), "")


def strip_message(text: str) -> str:
    """The message as git will record it: no `#` comment lines, nothing below the `-v` scissors."""
    text = SCISSORS.split(text, maxsplit=1)[0]
    return "\n".join(line for line in text.splitlines() if not line.startswith("#"))


def excludes_of(blob: bytes | None) -> list[str]:
    return pf.parse_excludes(blob.decode("utf-8", errors="replace")) if blob else []


def _version(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    try:
        return pf.version_of({"pyproject.toml": pf.Entry(0o100644, blob)})
    except (KeyError, ValueError, UnicodeDecodeError, pf.PublishError):
        return None


def public_form(files: dict[str, pf.Entry], version: str | None) -> dict[str, pf.Entry]:
    """CHANGELOG.md as it is (or will be) public: in the archived private repository the history before the
    published release is cut, as publish_fresh does; a changelog already in public form is scanned whole."""
    entry = files.get(pf.CHANGELOG)
    if entry is None or version is None:
        return files
    text = entry.data.decode("utf-8", errors="replace")
    if PUBLIC_CHANGELOG.search(text):
        return files
    try:
        public = pf.public_changelog(text, version)
    except pf.PublishError:
        return files
    return {**files, pf.CHANGELOG: pf.Entry(entry.mode, public.encode())}


def check_paths(paths: Sequence[str], excludes: Sequence[str], report: Report, where: str = "") -> None:
    order = sorted(paths)
    for path in order:
        if pf.is_excluded(path, excludes):
            shown = _shown(path, order, report)
            report.add(f"{where}{shown}: listed in {pf.EXCLUDE_FILE} (never part of the public repository)")


# --- denylist ----------------------------------------------------------------------------------------------


def load_rules(
    git: Git, explicit: Path | None, env: Mapping[str, str], require: bool, out: Out
) -> list[pf.Rule]:
    """The denylist's rules, or [] (warned) when there is none and none is required."""
    found = pf.find_denylist(git.repo, explicit, env, git.text_runner)
    if found.text is None:
        if not found.default:
            raise GuardError(f"no denylist file where {found.origin} points")
        required = require or (git.config(REQUIRE_CONFIG, "--bool") == "true")
        if required:
            raise GuardError(
                f"no denylist found and {REQUIRE_CONFIG} is set (git config {pf.DENYLIST_GIT_CONFIG} PATH)"
            )
        out(
            "guard: warning: no denylist found, generic checks only "
            f"(git config {pf.DENYLIST_GIT_CONFIG} PATH, or ${pf.DENYLIST_TEXT_ENV})"
        )
        return []
    try:
        return pf.parse_denylist(found.text, found.name)
    except pf.PublishError as e:
        # The message names a line number and the regex error, never the pattern.
        raise GuardError(f"denylist ({found.origin}): {e}") from e


# --- subcommands -------------------------------------------------------------------------------------------


def gitleaks_staged(git: Git, out: Out, which: Callable[[str], str | None] = shutil.which) -> bool:
    """gitleaks on the staged changes when it is installed; True when clean (or absent)."""
    binary = which("gitleaks")
    if binary is None:
        out("guard: warning: gitleaks isn't on PATH, secret scan skipped (docs/publishing.md: install it)")
        return True
    proc = git.runner(
        [binary, "git", "--staged", "--redact", "--no-banner", "--verbose", "--no-color", str(git.repo)]
    )
    if proc.returncode == 0:
        return True
    out((proc.stdout or b"").decode(errors="replace").rstrip())
    out((proc.stderr or b"").decode(errors="replace").rstrip())
    out("guard: gitleaks found a secret in the staged changes (values redacted above)")
    return False


def cmd_staged(git: Git, report: Report, out: Out, which: Callable[[str], str | None]) -> bool:
    raw = git.run("diff", "--cached", "--name-only", "-z", "--diff-filter=ACMR")
    paths = [p for p in raw.decode("utf-8", errors="surrogateescape").split("\0") if p]
    ls = git.run("ls-files", "-s", "-z", "--", *paths) if paths else b""
    modes = {}
    for rec in ls.decode("utf-8", errors="surrogateescape").split("\0"):
        if rec:
            meta, _, path = rec.partition("\t")
            modes[path] = int(meta.split()[0], 8)
    exclude_blob, pyproject = git.blobs([f":{pf.EXCLUDE_FILE}", ":pyproject.toml"])
    check_paths(paths, excludes_of(exclude_blob), report)
    wanted = [p for p in paths if modes.get(p, 0o100644) != 0o160000]  # submodules have no blob
    files = {
        p: pf.Entry(modes.get(p, 0o100644), b)
        for p, b in zip(wanted, git.blobs([f":{p}" for p in wanted]), strict=True)
        if b is not None
    }
    scan_files(public_form(files, _version(pyproject)), report)
    leaks_ok = gitleaks_staged(git, out, which)
    return leaks_ok


def cmd_commit_msg(git: Git, report: Report, message: Path) -> None:
    scan_message(strip_message(message.read_text(encoding="utf-8", errors="replace")), report, "message")
    for role, var in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        name, email = _ident(git.text("var", var))
        check_identity(role, name, email, report)


def commit_files(git: Git, sha: str, parents: Sequence[str]) -> list[str]:
    base = ["--root", sha] if not parents else [parents[0], sha]
    raw = git.run("diff-tree", "-r", "-z", "--name-only", "--no-commit-id", "--diff-filter=ACMR", *base)
    return [p for p in raw.decode("utf-8", errors="surrogateescape").split("\0") if p]


def check_commit(git: Git, sha: str, report: Report) -> None:
    fmt = "%P%x00%an%x00%ae%x00%cn%x00%ce%x00%B"
    parents_s, an, ae, cn, ce, body = git.text("log", "-1", f"--format={fmt}", sha).split("\0", 5)
    parents = parents_s.split()
    where = f"commit {sha[:12]} "
    scan_message(body, report, f"{where}message")
    check_identity("author", an, ae, report, where)
    check_identity("committer", cn, ce, report, where)
    paths = commit_files(git, sha, parents)
    exclude_blob, pyproject = git.blobs([f"{sha}:{pf.EXCLUDE_FILE}", f"{sha}:pyproject.toml"])
    check_paths(paths, excludes_of(exclude_blob), report, where)
    tree = git.text("ls-tree", "-r", "-z", sha, "--", *paths) if paths else ""
    modes = {}
    for rec in tree.split("\0"):
        if rec:
            meta, _, path = rec.partition("\t")
            modes[path] = int(meta.split()[0], 8)
    wanted = [p for p in paths if modes.get(p, 0o100644) != 0o160000]
    files = {
        p: pf.Entry(modes.get(p, 0o100644), b)
        for p, b in zip(wanted, git.blobs([f"{sha}:{p}" for p in wanted]), strict=True)
        if b is not None
    }
    scan_files(public_form(files, _version(pyproject)), report, where)


def cmd_range(git: Git, base: str, head: str, report: Report, out: Out) -> None:
    head_sha = git.text("rev-parse", "--verify", f"{head}^{{commit}}").strip()
    if base in ("-", "", ZERO_SHA):
        spec = [head_sha]
    else:
        spec = [f"{git.text('rev-parse', '--verify', f'{base}^{{commit}}').strip()}..{head_sha}"]
    shas = git.text("rev-list", "--reverse", *spec).split()
    for sha in shas:
        check_commit(git, sha, report)
    out(f"guard: {len(shas)} commit(s) checked")


def cmd_tree(git: Git, rev: str, report: Report, out: Out) -> None:
    """One revision as publish_fresh scans it: the exclusions applied, every path and file, the generic
    checks including references to excluded paths."""
    everything = pf.export(git.text_runner, git.repo, rev)
    excludes = excludes_of(e.data if (e := everything.get(pf.EXCLUDE_FILE)) else None)
    files = {p: e for p, e in everything.items() if not pf.is_excluded(p, excludes)}
    version = _version(e.data if (e := everything.get("pyproject.toml")) else None)
    files = public_form(files, version)
    scan_files(files, report, "", excludes, list(everything))
    out(f"guard: {rev}: {len(files)} files checked, {len(everything) - len(files)} excluded")


def cmd_install(git: Git, env: Mapping[str, str], out: Out, which: Callable[[str], str | None]) -> int:
    git.run("config", "core.hooksPath", HOOKS_PATH)
    out(f"guard: hooks installed (core.hooksPath = {HOOKS_PATH})")
    bad = 0
    for role, var in (("author", "GIT_AUTHOR_IDENT"), ("committer", "GIT_COMMITTER_IDENT")):
        proc = git.runner(["git", "-C", str(git.repo), "var", var])
        _, email = _ident((proc.stdout or b"").decode(errors="replace"))
        if proc.returncode != 0 or not email_ok(email):
            bad += 1
            out(f"guard: {role} identity: email isn't a GitHub noreply address (fix: {NOREPLY_FIX})")
        else:
            out(f"guard: {role} identity: ok (noreply)")
    found = pf.find_denylist(git.repo, None, env, git.text_runner)
    if found.text is not None:
        try:
            n = len(pf.parse_denylist(found.text, found.name))
            out(f"guard: denylist: {n} rule(s) ({found.origin})")
        except pf.PublishError as e:
            bad += 1
            out(f"guard: denylist ({found.origin}): {e}")
    else:
        out(f"guard: denylist: none found (git config {pf.DENYLIST_GIT_CONFIG} PATH)")
    required = git.config(REQUIRE_CONFIG, "--bool") == "true"
    out(f"guard: {REQUIRE_CONFIG}: {'true' if required else 'false'}")
    out(f"guard: gitleaks: {'found' if which('gitleaks') else 'not on PATH (secret scan skipped)'}")
    return 1 if bad else 0


# --- CLI ---------------------------------------------------------------------------------------------------


def repo_root(cwd: Path, runner: GitRunner) -> Path:
    proc = runner(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"])
    if proc.returncode != 0:
        raise GuardError("not inside a git repository")
    return Path(proc.stdout.decode().strip())


def main(
    argv: Sequence[str] | None = None,
    runner: GitRunner = default_git,
    out: Out = print,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
) -> int:
    # The common options go before or after the subcommand (SUPPRESS: a subcommand's unset copy doesn't
    # override a value given before it).
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-C", dest="repo", type=Path, default=argparse.SUPPRESS, help="the repository (cwd)")
    common.add_argument("--denylist", type=Path, default=argparse.SUPPRESS, help="the denylist file")
    common.add_argument(
        "--require-denylist",
        action="store_true",
        default=argparse.SUPPRESS,
        help=f"fail without a denylist (as git config {REQUIRE_CONFIG} true)",
    )
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common]
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("staged", parents=[common], help="the staged files (pre-commit)")
    m = sub.add_parser("commit-msg", parents=[common], help="a message file and the identity (commit-msg)")
    m.add_argument("file", type=Path)
    r = sub.add_parser("range", parents=[common], help="every commit in BASE..HEAD (BASE '-': all of HEAD)")
    r.add_argument("base")
    r.add_argument("head")
    t = sub.add_parser("tree", parents=[common], help="one revision, as publish_fresh.py scans it")
    t.add_argument("rev", nargs="?", default="HEAD")
    sub.add_parser("install", parents=[common], help="use .githooks and check the setup")
    try:
        args = p.parse_args(argv)
    except SystemExit as e:
        return 0 if e.code == 0 else 2
    env = os.environ if env is None else env
    repo = getattr(args, "repo", None) or Path.cwd()
    denylist, require = getattr(args, "denylist", None), getattr(args, "require_denylist", False)
    report = Report()
    try:
        git = Git(repo_root(repo, runner), runner)
        if args.cmd == "install":
            return cmd_install(git, env, out, which)
        report.rules = load_rules(git, denylist, env, require, out)
        extra_ok = True
        if args.cmd == "staged":
            extra_ok = cmd_staged(git, report, out, which)
        elif args.cmd == "commit-msg":
            cmd_commit_msg(git, report, args.file)
        elif args.cmd == "range":
            cmd_range(git, args.base, args.head, report, out)
        else:
            cmd_tree(git, args.rev, report, out)
    except (GuardError, pf.PublishError, OSError) as e:
        out(f"guard: {e}")
        return 2
    for line in report.lines:
        out(f"  {line}")
    if report.lines or not extra_ok:
        out(
            f"guard: REFUSED ({args.cmd}): {len(report.lines)} problem(s). Fix them; never bypass the hook "
            "(docs/publishing.md)."
        )
        return 1
    if args.cmd != "range" and args.cmd != "tree":
        return 0
    out(f"guard: clean ({'denylist and ' if report.rules else ''}generic checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
