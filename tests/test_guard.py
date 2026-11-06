"""scripts/guard.py: the anonymity safeguards on every commit after publishing (docs/publishing.md "After
publishing"). Real git in temporary repositories, isolated from the machine's git config (a maintainer's
`smallworld.denylist` must never leak in); gitleaks is absent unless a test says otherwise. Identifying-looking
samples are built at run time so this file passes tests/test_publishable.py and guard.py itself."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

import guard
import publish_fresh as pf
import pytest

OWNER = pf.OWNER
NOREPLY = f"4242+{OWNER}@users.noreply.github.com"
SECRET_WORD = "Zorblax"  # the made-up identifying value of these tests' denylists
SESSION = "https://" + "claude" + ".ai/code/" + "session_" + "01AbC"
PERSONAL_EMAIL = "someone" + "@" + "personal-mail.net"
PUBLIC_IP = ".".join(("8", "8", "4", "5"))


def isolated_env(tmp_path: Path) -> dict[str, str]:
    """os.environ without identity overrides, with an empty global and no system git config."""
    empty = tmp_path / "empty.gitconfig"
    empty.touch()
    env = {k: v for k, v in os.environ.items() if not k.startswith(("GIT_", "SMALLWORLD_"))}
    return env | {"GIT_CONFIG_GLOBAL": empty.as_posix(), "GIT_CONFIG_NOSYSTEM": "1"}


class Runner:
    """guard's git runner in the isolated environment; records each argv."""

    def __init__(self, env: dict[str, str]) -> None:
        self.env, self.calls = env, []

    def __call__(self, argv: Sequence[str], stdin: bytes | None = None) -> subprocess.CompletedProcess[bytes]:
        self.calls.append(list(argv))
        return subprocess.run(list(argv), input=stdin, capture_output=True, check=False, env=self.env)


def git(repo: Path, env: dict[str, str], *args: str, email: str = NOREPLY, name: str = OWNER) -> str:
    ident = ["-c", f"user.name={name}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false"]
    return subprocess.run(
        ["git", "-C", repo.as_posix(), *ident, *args], check=True, capture_output=True, text=True, env=env
    ).stdout


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return isolated_env(tmp_path)


def make_repo(tmp_path: Path, env: dict[str, str], files: dict[str, str] | None = None) -> Path:
    repo = tmp_path / "src"
    base = {
        "publish-exclude": "# history\nplans/\nDESIGN.md\n",
        "pyproject.toml": '[project]\nname = "x"\nversion = "1.2.0"\n',
        "README.md": "hello\n",
    }
    for path, text in {**base, **(files or {})}.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
    subprocess.run(
        ["git", "-c", "init.defaultBranch=main", "init", "-q", repo.as_posix()], check=True, env=env
    )
    git(repo, env, "add", "-A")
    git(repo, env, "commit", "-q", "-m", "base")
    return repo


def deny(tmp_path: Path, *patterns: str) -> Path:
    path = tmp_path / "deny.txt"
    path.write_text("# test denylist\n## names\nnever-matches-anything-q9\n" + "\n".join(patterns) + "\n")
    return path


def run(
    repo: Path, env: dict[str, str], *args: str, extra_env: dict[str, str] | None = None, which=None
) -> tuple[int, str]:
    lines: list[str] = []
    code = guard.main(
        ["-C", repo.as_posix(), *args],
        runner=Runner(env),
        out=lines.append,
        env=extra_env or {},
        which=which or (lambda _name: None),
    )
    return code, "\n".join(lines)


def stage(repo: Path, env: dict[str, str], files: dict[str, str]) -> None:
    for path, text in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())
    git(repo, env, "add", "-A")


def clean(out: str) -> None:
    """The output may be public (CI): no pattern, no matched text, no personal email."""
    low = out.lower()
    assert SECRET_WORD.lower() not in low and "zorbl" not in low
    assert PERSONAL_EMAIL not in low and "personal-mail" not in low


# --- staged ------------------------------------------------------------------------------------------------


def test_staged_hit_in_content_reports_rule_number_only(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    stage(repo, env, {"docs/a.md": f"fine\nThe {SECRET_WORD} server\n"})
    code, out = run(repo, env, "staged", "--denylist", deny(tmp_path, "zorb.ax").as_posix())
    assert code == 1, out
    assert "docs/a.md:2: denylist rule 2" in out and "REFUSED (staged)" in out
    clean(out)
    assert "zorb.ax" not in out


def test_staged_hit_on_a_path_hides_the_path(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    stage(repo, env, {f"art/{SECRET_WORD.lower()}.txt": "nothing\n", "b.txt": "ok\n"})
    code, out = run(repo, env, "staged", "--denylist", deny(tmp_path, "zorblax").as_posix())
    assert code == 1
    assert "<file 1 of 2 scanned: its path matches the denylist> (path): denylist rule 2" in out
    clean(out)


def test_staged_excluded_path_is_refused(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    stage(repo, env, {"plans/next.md": "a plan\n", "DESIGN.md": "design\n"})
    code, out = run(repo, env, "staged", "--denylist", deny(tmp_path).as_posix())
    assert code == 1
    assert "plans/next.md: listed in publish-exclude" in out and "DESIGN.md: listed in publish-exclude" in out


def test_staged_allowed_forms_pass(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    text = (
        f"uses: {pf.PUBLIC_REPOSITORY}/.github/workflows/server.yml@abc\n"
        f"https://github.com/{OWNER}\n"
        f"package io.github.{OWNER}.mcbrand;\n"
        f"Author: <{NOREPLY}>\n"
        "Co-Authored-By: Claude <noreply@anthropic.com>\n"
        "admin@example.com 192.0.2.10\n"
    )
    stage(repo, env, {"docs/ok.md": text})
    code, out = run(repo, env, "staged", "--denylist", deny(tmp_path, OWNER).as_posix())
    assert code == 0, out
    assert "gitleaks isn't on PATH" in out  # one warning line, not a failure


def test_staged_generic_checks_without_a_denylist(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    stage(repo, env, {"a.md": f"see {SESSION}\nmail {PERSONAL_EMAIL}\nbare {OWNER} name\nhost {PUBLIC_IP}\n"})
    code, out = run(repo, env, "staged")
    assert code == 1
    assert "no denylist found, generic checks only" in out
    for problem in ("a.md:1: session-link", "a.md:2: email", "a.md:3: owner", "a.md:4: public-ip"):
        assert problem in out
    clean(out)


def test_staged_runs_gitleaks_when_present(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    stage(repo, env, {"a.md": "fine\n"})
    calls: list[list[str]] = []

    class Leaky(Runner):
        def __call__(self, argv, stdin=None):
            if argv[0] == "/opt/gitleaks":
                calls.append(list(argv))
                return subprocess.CompletedProcess(list(argv), 1, b"Finding: REDACTED\n", b"leaks found: 1\n")
            return super().__call__(argv, stdin)

    lines: list[str] = []
    code = guard.main(
        ["-C", repo.as_posix(), "staged"],
        runner=Leaky(env),
        out=lines.append,
        env={},
        which=lambda _: "/opt/gitleaks",
    )
    assert code == 1 and "gitleaks found a secret" in "\n".join(lines)
    assert calls[0][1:5] == ["git", "--staged", "--redact", "--no-banner"]


def test_staged_changelog_history_is_cut_like_publish_fresh(tmp_path, env) -> None:
    """The archived repository's changelog still holds the old history; only what would be public counts."""
    log = (
        "# Changelog\n\n## [Unreleased]\n\n## [1.2.0] - 2026-11-06\n\n- current\n\n"
        f"## [1.1.0] - 2026-11-01\n\n- old {SECRET_WORD} history\n"
    )
    repo = make_repo(tmp_path, env)
    stage(repo, env, {"CHANGELOG.md": log})
    assert run(repo, env, "staged", "--denylist", deny(tmp_path, "zorblax").as_posix())[0] == 0
    public = pf.public_changelog(log.replace(f"- old {SECRET_WORD} history\n", ""), "1.2.0")
    stage(repo, env, {"CHANGELOG.md": public + f"\n- {SECRET_WORD}\n"})  # public form: scanned whole
    assert run(repo, env, "staged", "--denylist", deny(tmp_path, "zorblax").as_posix())[0] == 1


# --- commit-msg --------------------------------------------------------------------------------------------


def test_commit_msg_refuses_a_session_link_and_a_hit(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    git(repo, env, "config", "user.name", OWNER)  # no global config: CI runners have no name to fall back on
    git(repo, env, "config", "user.email", NOREPLY)
    msg = tmp_path / "MSG"
    msg.write_text(f"Fix it for {SECRET_WORD}\n\n{SESSION}\n# {PERSONAL_EMAIL} in a comment is stripped\n")
    code, out = run(
        repo, env, "commit-msg", msg.as_posix(), "--denylist", deny(tmp_path, "zorblax").as_posix()
    )
    assert code == 1
    assert "message:1: denylist rule 2" in out and "message:3: session-link" in out
    assert "email" not in out.replace("noreply", "")
    clean(out)
    msg.write_text(f"Fix it\n\nCo-Authored-By: Claude <noreply@anthropic.com>\n# {SECRET_WORD}\n")
    assert (
        run(repo, env, "commit-msg", msg.as_posix(), "--denylist", deny(tmp_path, "zorblax").as_posix())[0]
        == 0
    )


def test_commit_msg_refuses_a_personal_author_email(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    git(repo, env, "config", "user.name", OWNER)  # no global config: CI runners have no name to fall back on
    git(repo, env, "config", "user.email", PERSONAL_EMAIL)
    msg = tmp_path / "MSG"
    msg.write_text("A fine message\n")
    code, out = run(repo, env, "commit-msg", msg.as_posix(), "--denylist", deny(tmp_path).as_posix())
    assert code == 1
    assert "author email isn't a GitHub noreply address" in out and "committer email" in out
    assert "users.noreply.github.com" in out  # the fix
    clean(out)


def test_identity_rules() -> None:
    assert guard.email_ok(NOREPLY) and guard.email_ok("49699333+dependabot[bot]@users.noreply.github.com")
    assert guard.email_ok("noreply@github.com") and guard.email_ok("t@example.invalid")
    assert (
        not guard.email_ok(PERSONAL_EMAIL)
        and not guard.email_ok("")
        and not guard.email_ok("x" + "@" + "gmail.com")
    )
    report = guard.Report(rules=pf.parse_denylist(f"{OWNER}\n"))
    guard.check_identity("author", OWNER, NOREPLY, report)  # the public login is the identity's name
    assert report.lines == []
    guard.check_identity("author", f"{OWNER} Smith", NOREPLY, report)
    assert report.lines == ["author name: denylist rule 1"]


# --- range and tree ----------------------------------------------------------------------------------------


def test_range_catches_a_hit_in_the_first_commits_message(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    base = git(repo, env, "rev-parse", "HEAD").strip()
    stage(repo, env, {"a.txt": "one\n"})
    git(repo, env, "commit", "-q", "-m", f"first: for {SECRET_WORD}")
    stage(repo, env, {"b.txt": "two\n"})
    git(repo, env, "commit", "-q", "-m", "second")
    first = git(repo, env, "rev-parse", "HEAD~1").strip()
    code, out = run(repo, env, "range", base, "HEAD", "--denylist", deny(tmp_path, "zorblax").as_posix())
    assert code == 1
    assert "2 commit(s) checked" in out and f"commit {first[:12]} message:1: denylist rule 2" in out
    clean(out)
    assert run(repo, env, "range", first, "HEAD", "--denylist", deny(tmp_path, "zorblax").as_posix())[0] == 0


def test_range_checks_identities_and_changed_files(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    base = git(repo, env, "rev-parse", "HEAD").strip()
    stage(repo, env, {"plans/x.md": "x\n", "c.txt": f"{SECRET_WORD}\n"})
    git(repo, env, "commit", "-q", "-m", "files", email=PERSONAL_EMAIL, name=f"{SECRET_WORD} Q")
    code, out = run(repo, env, "range", base, "HEAD", "--denylist", deny(tmp_path, "zorblax").as_posix())
    assert code == 1
    for problem in (
        "author name: denylist rule 2",
        "author email isn't a GitHub noreply address",
        "committer email isn't",
        "plans/x.md: listed in publish-exclude",
        "c.txt:1: denylist rule 2",
    ):
        assert problem in out
    clean(out)
    # "-": every ancestor of HEAD (a branch that never left main's history)
    code, out = run(repo, env, "range", "-", "HEAD", "--denylist", deny(tmp_path, "zorblax").as_posix())
    assert code == 1 and "2 commit(s) checked" in out


def test_range_passes_a_dependabot_commit(tmp_path, env) -> None:
    """Dependabot's identity and its Signed-off-by line (support@github.com) must not fail `publication`."""
    repo = make_repo(tmp_path, env)
    base = git(repo, env, "rev-parse", "HEAD").strip()
    stage(repo, env, {"requirements.in": "ansible-core==2.21.6\n"})
    msg = (
        "Bump ansible-core in /requirements in the python group\n\n"
        "- [Release notes](https://github.com/ansible/ansible/releases)\n\n"
        "Signed-off-by: dependabot[bot] <support@github.com>\n"
    )
    bot = "49699333+dependabot[bot]@users.noreply.github.com"
    git(repo, env, "commit", "-q", "-m", msg, email=bot, name="dependabot[bot]")
    code, out = run(repo, env, "range", base, "HEAD", "--denylist", deny(tmp_path, "zorblax").as_posix())
    assert code == 0, out


def test_tree_scans_one_revision_like_publish_fresh(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env, {"plans/p.md": f"{SECRET_WORD}\n", "docs/a.md": "see plans/\n"})
    code, out = run(repo, env, "tree", "HEAD", "--denylist", deny(tmp_path, "zorblax").as_posix())
    assert code == 1  # the excluded file isn't scanned, but a reference to it is a dangling one
    assert "docs/a.md:1: excluded-reference" in out and "denylist rule" not in out
    assert "files checked, 1 excluded" in out


# --- denylist resolution, CI text, requireDenylist ------------------------------------------------------------


def test_denylist_from_ci_text_env(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    stage(repo, env, {"a.md": f"{SECRET_WORD}\n"})
    text = deny(tmp_path, "zorblax").read_text()
    code, out = run(repo, env, "staged", extra_env={"SMALLWORLD_DENYLIST_TEXT": text})
    assert code == 1 and "a.md:1: denylist rule 2" in out
    clean(out)
    # an empty value (a fork's CI run: no secret) counts as unset
    code, out = run(repo, env, "staged", extra_env={"SMALLWORLD_DENYLIST_TEXT": ""})
    assert code == 0 and "no denylist found" in out


def test_missing_denylist_warns_unless_required(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    code, out = run(repo, env, "tree")
    assert code == 0 and "warning: no denylist found" in out
    git(repo, env, "config", "smallworld.requireDenylist", "true")
    code, out = run(repo, env, "tree")
    assert code == 2 and "smallworld.requireDenylist is set" in out
    git(repo, env, "config", "smallworld.requireDenylist", "false")
    assert run(repo, env, "tree", "--require-denylist")[0] == 2
    # a configured path that doesn't exist is a configuration error, never "no denylist"
    git(repo, env, "config", "smallworld.denylist", (tmp_path / "nope.txt").as_posix())
    code, out = run(repo, env, "tree")
    assert code == 2 and "git config smallworld.denylist" in out


def test_denylist_precedence(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    files = {}
    for name in ("explicit", "envpath", "config", "default"):
        files[name] = tmp_path / f"{name}.txt"
        files[name].write_text(f"{name}\n")
    (tmp_path / "mc-config").mkdir()
    (tmp_path / "mc-config" / "publish-denylist.txt").write_text("default\n")
    git(repo, env, "config", "smallworld.denylist", files["config"].as_posix())
    run_text = guard.Git(repo, Runner(env)).text_runner

    def origin(explicit: Path | None, e: dict[str, str]) -> tuple[str, str | None]:
        found = pf.find_denylist(repo, explicit, e, run_text)
        return found.origin, found.text

    both = {"SMALLWORLD_DENYLIST_TEXT": "text\n", "SMALLWORLD_DENYLIST": files["envpath"].as_posix()}
    assert origin(files["explicit"], both) == ("--denylist", "explicit\n")
    assert origin(None, both) == ("$SMALLWORLD_DENYLIST_TEXT", "text\n")
    assert origin(None, {"SMALLWORLD_DENYLIST": files["envpath"].as_posix()}) == (
        "$SMALLWORLD_DENYLIST",
        "envpath\n",
    )
    assert origin(None, {}) == ("git config smallworld.denylist", "config\n")
    git(repo, env, "config", "--unset", "smallworld.denylist")
    assert origin(None, {}) == ("default", "default\n")


def test_usage_errors_exit_2(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    assert run(repo, env, "nonsense")[0] == 2
    assert run(repo, env, "range", "HEAD")[0] == 2
    bad = tmp_path / "bad.txt"
    bad.write_text("(unclosed\n")
    code, out = run(repo, env, "tree", "--denylist", bad.as_posix())
    assert code == 2 and "not a valid regex" in out and "(unclosed" not in out.split("regex")[0]


# --- install and the hooks -----------------------------------------------------------------------------------


def test_install_sets_hooks_path_and_reports(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    git(repo, env, "config", "user.name", OWNER)
    git(repo, env, "config", "user.email", NOREPLY)
    code, out = run(repo, env, "install")
    assert code == 0, out
    assert git(repo, env, "config", "core.hooksPath").strip() == ".githooks"
    assert "author identity: ok" in out and "denylist: none found" in out and "gitleaks: not on PATH" in out
    git(repo, env, "config", "user.email", PERSONAL_EMAIL)
    code, out = run(repo, env, "install")
    assert code == 1 and "isn't a GitHub noreply address" in out
    clean(out)


HOOKS = Path(__file__).resolve().parent.parent / ".githooks"


def test_hook_scripts_are_committed_executable() -> None:
    out = subprocess.run(
        ["git", "-C", HOOKS.parent.as_posix(), "ls-files", "-s", "--", ".githooks"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    modes = {line.split("\t")[1]: line.split()[0] for line in out.splitlines()}
    assert modes == {f".githooks/{n}": "100755" for n in ("commit-msg", "pre-commit", "pre-push")}


@pytest.mark.posix_only
@pytest.mark.skipif(sys.platform == "win32", reason="runs the sh hooks")
def test_hooks_block_commits_and_pushes(tmp_path, env) -> None:
    repo = make_repo(tmp_path, env)
    shutil.copytree(HOOKS, repo / ".githooks")
    (repo / ".gitignore").write_text("__pycache__/\n")
    (repo / "scripts").mkdir()
    for name in ("guard.py", "publish_fresh.py"):
        shutil.copy2(HOOKS.parent / "scripts" / name, repo / "scripts" / name)
    git(repo, env, "add", "-A")
    git(repo, env, "commit", "-q", "-m", "tools")
    git(repo, env, "config", "core.hooksPath", ".githooks")
    git(repo, env, "config", "smallworld.denylist", deny(tmp_path, "zorblax").as_posix())
    # gitleaks absent from PATH for the hook (one warning line)
    env = env | {
        "PATH": os.pathsep.join(
            p for p in env["PATH"].split(os.pathsep) if not (Path(p) / "gitleaks").exists()
        )
    }

    def commit(message: str, email: str = NOREPLY) -> subprocess.CompletedProcess[str]:
        ident = ["-c", f"user.name={OWNER}", "-c", f"user.email={email}", "-c", "commit.gpgsign=false"]
        return subprocess.run(
            ["git", "-C", repo.as_posix(), *ident, "commit", "-q", "-m", message],
            capture_output=True,
            text=True,
            env=env,
        )

    stage(repo, env, {"a.md": f"{SECRET_WORD}\n"})
    res = commit("content hit")
    assert res.returncode != 0 and "denylist rule 2" in res.stdout + res.stderr
    clean(res.stdout + res.stderr)
    stage(repo, env, {"a.md": "fine\n"})
    assert commit(f"see {SESSION}").returncode != 0  # commit-msg
    assert commit("fine", email=PERSONAL_EMAIL).returncode != 0
    assert commit("fine").returncode == 0, "the clean commit goes through"

    # pre-push: a bare remote; a new branch is checked from where it left main
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", remote.as_posix()], check=True, env=env)
    git(repo, env, "remote", "add", "origin", remote.as_posix())
    push = ["git", "-C", repo.as_posix(), "push", "-q", "origin"]
    assert subprocess.run([*push, "main"], capture_output=True, env=env).returncode == 0
    git(repo, env, "fetch", "-q", "origin")
    git(repo, env, "checkout", "-q", "-b", "topic")
    stage(repo, env, {"b.md": "ok\n"})
    git(repo, env, "-c", "core.hooksPath=/dev/null", "commit", "-q", "-m", f"bypassed {SECRET_WORD}")
    res = subprocess.run([*push, "topic"], capture_output=True, text=True, env=env)
    assert res.returncode != 0 and "message:1: denylist rule 2" in res.stdout + res.stderr
    git(repo, env, "commit", "-q", "--amend", "-m", "fixed", "--no-verify")
    assert subprocess.run([*push, "topic"], capture_output=True, env=env).returncode == 0
    assert subprocess.run([*push, "--delete", "topic"], capture_output=True, env=env).returncode == 0
