"""P4 building blocks: state file, .env parsing, expiry.yml, the pinned product cache, tailnet wrappers."""

import datetime
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import REPO, TEST_SHA

from mcctl import cli, configrepo, envfile, expiry, product, remote, schema, state
from mcctl.configrepo import Pin
from mcctl.providers import CredentialCheck

# --- state --------------------------------------------------------------------------------------------


def test_state_round_trip(tmp_path) -> None:
    st = state.load(tmp_path)
    assert st.get("x") is None and not st.is_done("x")
    now = datetime.datetime(2026, 10, 31, 9, 30, 15, 123, tzinfo=datetime.UTC)
    st.record("bootstrap", "done", inputs={"ipv4": "9.9.9.9"}, outputs={"ok": True}, now=now)
    again = state.load(tmp_path)
    assert again.is_done("bootstrap") and again.is_done("bootstrap", {"ipv4": "9.9.9.9"})
    assert not again.is_done("bootstrap", {"ipv4": "1.1.1.1"})  # changed input -> re-run
    assert again.get("bootstrap")["updated_at"] == "2026-10-31T09:30:15Z"
    data = json.loads((tmp_path / state.STATE_FILE).read_text())
    assert data["version"] == 1 and set(data["steps"]["bootstrap"]) == {
        "status",
        "input_hash",
        "outputs",
        "updated_at",
    }
    assert not list((tmp_path / ".mcctl").glob(".state.*"))  # atomic write leaves no temp file


def test_state_rejects_garbage(tmp_path) -> None:
    (tmp_path / ".mcctl").mkdir()
    (tmp_path / state.STATE_FILE).write_text("{nope")
    with pytest.raises(state.StateError, match="not JSON"):
        state.load(tmp_path)
    (tmp_path / state.STATE_FILE).write_text('{"version": 9, "steps": {}}')
    with pytest.raises(state.StateError, match="unknown format"):
        state.load(tmp_path)


# --- .env ---------------------------------------------------------------------------------------------


def test_envfile_parse() -> None:
    text = "# c\nA=1\nexport B=\"two words\"\nC='x#y'\nD=val # comment\nE=\n"
    assert envfile.parse(text) == {"A": "1", "B": "two words", "C": "x#y", "D": "val", "E": ""}
    with pytest.raises(envfile.EnvFileError) as exc:
        envfile.parse("A=1\nsecret-without-key\n")
    assert "line 2" in str(exc.value) and "secret-without-key" not in str(exc.value)


@pytest.mark.parametrize(
    ("line", "value"),
    [
        ('K="v" # note', "v"),
        ("K='v' # note", "v"),
        ('K="a # b"', "a # b"),
        ("K='a # b'  ", "a # b"),
        ("K=abc#def", "abc#def"),
        ("K=abc #def", "abc"),
        ("K=abc\t# tab comment", "abc"),
        ("K=a b c   ", "a b c"),
        ('K="esc \\"q\\" \\\\ end"', 'esc "q" \\ end'),
        ("K=$HOME", "$HOME"),
        ("  export   K=x", "x"),
        ('K=""', ""),
        ("K=#", "#"),
    ],
)
def test_envfile_subset(line, value) -> None:
    assert envfile.parse(line + "\n") == {"K": value}


@pytest.mark.parametrize("line", ['K="open', "K='open", 'K="v" trailing', "K='v'x", "K = v", "1K=v"])
def test_envfile_rejects_outside_the_subset(line) -> None:
    with pytest.raises(envfile.EnvFileError) as exc:
        envfile.parse("A=1\n" + line + "\n")
    assert "line 2" in str(exc.value) and "open" not in str(exc.value) and "trailing" not in str(exc.value)


def test_envfile_write_round_trips_and_stays_private(tmp_path) -> None:
    (tmp_path / ".env").write_text("# keep me\nA=1\nB=old # note\n")
    (tmp_path / ".env").chmod(0o600)
    values = {"B": "new", "C": 'we"ird # \\ value', "D": "tskey-api-k1-abc"}
    envfile.write(tmp_path, values)
    text = (tmp_path / ".env").read_text()
    assert text.startswith("# keep me\nA=1\nB=new\n") and "D=tskey-api-k1-abc" in text
    assert envfile.parse(text) == {"A": "1"} | values
    assert stat.S_IMODE((tmp_path / ".env").stat().st_mode) == 0o600
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    envfile.write(fresh, {"K": "v"})
    assert stat.S_IMODE((fresh / ".env").stat().st_mode) == 0o600
    with pytest.raises(envfile.EnvFileError):
        envfile.write(fresh, {"K": "two\nlines"})


def test_envfile_duplicate_keys(tmp_path) -> None:
    with pytest.raises(envfile.EnvFileError, match="line 3: K is already set on line 1") as e:
        envfile.parse("K=one\nX=1\nK=secret-two\n")
    assert "secret-two" not in str(e.value)
    (tmp_path / ".env").write_text("K=one\n# c\nK=two\nX=1\n")
    envfile.write(tmp_path, {"K": "new"})  # repairs: first definition replaced, the later one dropped
    assert (tmp_path / ".env").read_text() == "K=new\n# c\nX=1\n"


def test_envfile_merge_and_mode(tmp_path) -> None:
    assert envfile.load(tmp_path) == {}
    (tmp_path / ".env").write_text("A=file\nB=file\n")
    (tmp_path / ".env").chmod(0o600)
    assert envfile.merged(tmp_path, {"A": "shell", "B": ""}) == {"A": "shell", "B": "file"}
    # The state credentials (AWS_*) come from .env only; the shell's are dropped and named by shadowed().
    (tmp_path / ".env").write_text("A=file\nAWS_ACCESS_KEY_ID=file-key\n")
    shell = {"AWS_ACCESS_KEY_ID": "shell-key", "AWS_PROFILE": "other", "PATH": "/bin"}
    assert envfile.merged(tmp_path, shell) == {"A": "file", "AWS_ACCESS_KEY_ID": "file-key", "PATH": "/bin"}
    assert envfile.shadowed(tmp_path, shell) == ["AWS_ACCESS_KEY_ID", "AWS_PROFILE"]
    assert envfile.shadowed(tmp_path, {"AWS_ACCESS_KEY_ID": "file-key"}) == []
    (tmp_path / ".env").chmod(0o640)
    assert "chmod 600" in envfile.mode_problem(tmp_path / ".env")
    with pytest.raises(envfile.EnvFileError):
        envfile.load(tmp_path)


# --- expiry.yml ---------------------------------------------------------------------------------------


def _check(name: str, expires: datetime.date | None, known: bool = True) -> CredentialCheck:
    c = CredentialCheck(name)
    if known:
        c.expires(expires)
    return c


def test_expiry_write_merge_and_validate(tmp_path) -> None:
    today = datetime.date(2026, 10, 31)
    prev = [
        {"name": "old-one", "expires_on": datetime.date(2027, 1, 1), "checked_at": datetime.date(2026, 1, 1)}
    ]
    checks = [
        _check("zeta", None),
        _check("alpha", datetime.date(2027, 2, 3)),
        _check("unknown", None, known=False),
    ]
    path, changed = expiry.write(tmp_path, prev, checks, today)
    assert changed
    text = path.read_text()
    assert text.splitlines()[-3:] == [
        "  - {name: alpha, expires_on: 2027-02-03, checked_at: 2026-10-31}",
        "  - {name: old-one, expires_on: 2027-01-01, checked_at: 2026-01-01}",
        "  - {name: zeta, expires_on: null, checked_at: 2026-10-31}",
    ]
    _, errors = configrepo.check_expiry(yaml.safe_load(text))
    assert errors == []
    assert expiry.write(tmp_path, configrepo.check_expiry(yaml.safe_load(text))[0], checks, today)[1] is False


def test_expiry_empty_matches_template(tmp_path) -> None:
    expiry.write(tmp_path, [], [], datetime.date(2026, 10, 31))
    assert (tmp_path / "expiry.yml").read_text() == (REPO / "template/expiry.yml").read_text()


# --- the pinned product -------------------------------------------------------------------------------

PIN = Pin("edvardk/smallworld", TEST_SHA, "v0.4.0")


def test_product_override_and_reexec(tmp_path, monkeypatch) -> None:
    p = product.resolve(PIN, {product.ENV_OVERRIDE: str(REPO)})
    assert p.root == REPO and product.reexec_argv(p, ["deploy"]) is None  # this checkout: run in-process
    with pytest.raises(product.ProductError, match="isn't a product checkout"):
        product.resolve(PIN, {product.ENV_OVERRIDE: str(tmp_path)})


def test_product_uses_a_ready_cache(tmp_path, monkeypatch) -> None:
    cache = (tmp_path / "mcctl" / TEST_SHA).resolve()
    # this mcctl isn't the pinned one; the cached src is a clean checkout at the pin
    monkeypatch.setattr(product, "checkout_sha", lambda root: TEST_SHA if root == cache / "src" else "f" * 40)
    (cache / "src").mkdir(parents=True)
    (cache / "venv/bin").mkdir(parents=True)
    (cache / "venv/bin/python").write_text("")
    (cache / product.READY).write_text(TEST_SHA)
    p = product.resolve(PIN, {"XDG_CACHE_HOME": str(tmp_path), "PATH": "/usr/bin"})
    assert p.root == cache / "src"
    argv = product.reexec_argv(p, ["deploy", "-C", "."])
    assert argv == [str(cache / "venv/bin/python"), "-P", "-c", product.BOOTSTRAP, "deploy", "-C", "."]
    env = p.env({"PATH": "/usr/bin", "PYTHONPATH": "/elsewhere"})
    assert env["PATH"].startswith(str(cache / "venv/bin")) and env[product.ENV_OVERRIDE] == str(cache / "src")
    assert env["ANSIBLE_COLLECTIONS_PATH"] == str(cache / "collections") and "PYTHONPATH" not in env


def test_product_rebuilds_an_unverified_cache(tmp_path, monkeypatch) -> None:
    cache = tmp_path / "mcctl" / TEST_SHA
    (cache / "src").mkdir(parents=True)
    (cache / product.READY).write_text(TEST_SHA)  # .ready, but no venv and src isn't at the pin
    monkeypatch.setattr(product, "checkout_sha", lambda _root: None)
    built = []
    monkeypatch.setattr(product, "build", lambda pin, dest: built.append(dest) or product.cached(dest))
    product.resolve(PIN, {"XDG_CACHE_HOME": str(tmp_path)})
    assert built == [cache] and (tmp_path / "mcctl" / f"{TEST_SHA}.lock").exists()


def test_product_build_survives_a_concurrent_finish(tmp_path, monkeypatch) -> None:
    """The rename fails because another process put a finished cache in place: use theirs."""

    def fake_run(argv, what):
        if "checkout" in argv:
            src = Path(argv[2])
            (src / "requirements").mkdir()
            (src / "requirements/deploy.txt").write_text("")

    monkeypatch.setattr(product, "_run", fake_run)
    monkeypatch.setattr(product, "checkout_sha", lambda _root: TEST_SHA)
    dest = tmp_path / TEST_SHA
    real_rename = Path.rename

    def racing_rename(self, target):
        (dest / "venv/bin").mkdir(parents=True)
        (dest / "venv/bin/python").write_text("")
        (dest / product.READY).write_text(TEST_SHA)
        raise OSError(39, "Directory not empty")

    monkeypatch.setattr(Path, "rename", racing_rename)
    p = product.build(PIN, dest)
    monkeypatch.setattr(Path, "rename", real_rename)
    assert p.root == dest.resolve() / "src"
    assert [x.name for x in tmp_path.iterdir()] == [TEST_SHA]  # our half (the .tmp dir) is gone


def test_product_build_steps(tmp_path, monkeypatch) -> None:
    ran: list[list[str]] = []

    def fake_run(argv, what):
        ran.append(list(argv))
        if argv[1:4] == ["-C", str(tmp_path / f"{TEST_SHA}.tmp-{os.getpid()}" / "src"), "checkout"]:
            src = Path(argv[2])
            (src / "requirements").mkdir()
            (src / "requirements/deploy.txt").write_text("")

    monkeypatch.setattr(product, "_run", fake_run)
    monkeypatch.setattr(product, "checkout_sha", lambda _root: TEST_SHA)
    p = product.build(PIN, tmp_path / TEST_SHA)
    assert (
        p.root == (tmp_path / TEST_SHA / "src").resolve() and (tmp_path / TEST_SHA / product.READY).is_file()
    )
    fetch = next(a for a in ran if "fetch" in a)
    assert fetch[-2:] == ["https://github.com/edvardk/smallworld.git", TEST_SHA]
    assert f"credential.helper={product.GH_CREDENTIAL_HELPER}" in fetch
    pip = next(a for a in ran if a[0].endswith("venv/bin/pip"))
    assert "--require-hashes" in pip and "--no-deps" in pip
    assert any(a[0].endswith("venv/bin/ansible-galaxy") for a in ran)


def test_product_build_refuses_old_layout(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(product, "_run", lambda argv, what: None)
    monkeypatch.setattr(product, "checkout_sha", lambda _root: TEST_SHA)
    with pytest.raises(product.ProductError, match="predates the cached-checkout layout"):
        product.build(PIN, tmp_path / TEST_SHA)
    assert not list(tmp_path.iterdir())  # nothing half-built left


def test_cli_reexecs_into_the_pinned_product(config_repo, monkeypatch) -> None:
    monkeypatch.delenv(product.ENV_OVERRIDE)  # a user's shell: no override
    other = product.Product(Path("/cache/src"), Path("/cache/venv/bin"), Path("/cache/collections"))
    monkeypatch.setattr(product, "resolve", lambda pin, env: other)
    seen = {}

    def fake_reexec(argv, env):
        seen.update(argv=argv, env=env)
        return 7

    monkeypatch.setattr(cli, "_reexec", fake_reexec)
    assert cli.main(["deploy", "-C", str(config_repo), "--check"]) == 7
    assert seen["argv"] == [
        "/cache/venv/bin/python",
        "-P",
        "-c",
        product.BOOTSTRAP,
        "deploy",
        "-C",
        str(config_repo),
        "--check",
    ]
    assert seen["env"][product.ENV_OVERRIDE] == "/cache/src"


def test_cli_never_reexecs_with_the_override_set(config_repo, monkeypatch, capsys) -> None:
    other = product.Product(Path("/cache/src"), None, None)
    monkeypatch.setattr(product, "resolve", lambda pin, env: other)
    monkeypatch.setattr(cli, "_reexec", lambda argv, env: pytest.fail("re-executed"))
    assert cli.main(["deploy", "-C", str(config_repo)]) == 1
    assert "refusing to re-execute again" in capsys.readouterr().err


def test_python_version_gate(monkeypatch, capsys) -> None:
    import mcctl

    assert mcctl.python_problem((3, 12, 0)) is None and mcctl.python_problem((3, 13, 1)) is None
    msg = mcctl.python_problem((3, 11, 9))
    assert msg and ">= 3.12" in msg and "3.11.9" in msg and "server" in msg
    monkeypatch.setattr(cli, "python_problem", lambda: "too old")
    assert cli.main(["validate"]) == 1 and "too old" in capsys.readouterr().err


def test_checkout_sha_of_this_repo() -> None:
    sha = product.checkout_sha(schema.PRODUCT_ROOT)
    assert sha is None or len(sha) == 40  # None while the working tree has uncommitted changes
    assert product.checkout_sha(Path("/")) is None


# --- tailnet wrappers ---------------------------------------------------------------------------------

STATUS_OUT = """@@mcctl:active
active
@@mcctl:backup
Result=success
ExecMainExitTimestamp=Sat 2026-10-31 11:00:42 GMT
@@mcctl:list
There are 2 of a max of 20 players online: Alex, Sam
@@mcctl:tps
§6TPS from last 5s, 1m, 5m, 15m: §a20.0, §a19.8, §a*20.0, §a20.0
@@mcctl:mspt
§6Server tick times §e(§7avg§e/§7min§e/§7max§e)§6 from last 5s§7,§6 10s§7,§6 1m§e:
§6◴ §a5.1§7/§a2.0§7/§a9.0§e, §a5.0§7/§a2.0§7/§a9.0§e, §a4.8§7/§a1.9§7/§a30.2
@@mcctl:disabled
["Chunky"]
@@mcctl:stopped
@@mcctl:reboot
yes
"""


def test_parse_status() -> None:
    st = remote.parse_status(STATUS_OUT)
    assert st.server == "active" and st.players_online == 2 and st.players == ["Alex", "Sam"]
    assert st.tps == 19.8 and st.mspt == 4.8
    assert st.last_backup == "Sat 2026-10-31 11:00:42 GMT" and st.last_backup_result == "success"
    assert st.disabled_plugins == ["Chunky"] and st.reboot_required and not st.admin_stopped


def test_status_with_github(config_repo) -> None:
    cfg = configrepo.load(config_repo).server
    runs = [
        {"event": "schedule", "status": "completed", "conclusion": "success"},
        {
            "event": "push",
            "status": "completed",
            "conclusion": "failure",
            "headSha": "a" * 40,
            "createdAt": "2026-10-31T10:00:00Z",
        },
    ]
    prs = [
        {"number": 4, "headRefName": "panel/20261031", "title": "players"},
        {"number": 5, "headRefName": "feature"},
    ]
    seen: list[list[str]] = []

    def run(argv):
        argv = list(argv)
        seen.append(argv)
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(argv, 0, STATUS_OUT, "")
        out = runs if argv[1] == "run" else prs
        return subprocess.CompletedProcess(argv, 0, json.dumps(out), "")

    st = remote.status(cfg, run)
    assert st.last_main_run["conclusion"] == "failure" and [p["number"] for p in st.pending_panel_prs] == [4]
    assert seen[0][:1] == ["ssh"] and "deploy@example" in seen[0]
    assert ["-R", "mcctl-tests/tests-mc-config"] == seen[1][3:5]
    text = remote.format_status(st, "example")
    assert "2/20 (Alex, Sam)" in text and "19.8 / 4.8" in text and "pending panel PRs: #4" in text
    assert "disabled plugins: Chunky" in text and "failure" in text


def test_status_unreachable(config_repo) -> None:
    cfg = configrepo.load(config_repo).server

    def run(argv):
        if argv[0] == "ssh":
            return subprocess.CompletedProcess(
                list(argv), 255, "", "ssh: Could not resolve hostname example\n"
            )
        return subprocess.CompletedProcess(list(argv), 1, "", "gh: not logged in")

    st = remote.status(cfg, run)
    assert not st.reachable and "Could not resolve hostname" in st.error
    assert "UNREACHABLE" in remote.format_status(st, "example") and len(st.notes) == 2


def test_remote_commands_are_quoted() -> None:
    assert remote.rcon_command(["whitelist", "add", "Alex"]) == "mc whitelist add Alex"
    assert remote.rcon_command(["say", "hi; rm -rf /"]) == "mc say 'hi; rm -rf /'"
    with pytest.raises(ValueError):
        remote.rcon_command([])
    assert remote.logs_command("mc-backup", 50, True) == "sudo journalctl -u mc-backup -n 50 --no-pager -f"
    with pytest.raises(ValueError):
        remote.logs_command("x; reboot", 1, False)
    assert remote.backup_list_command(3) == "sudo mc-restic restic snapshots --latest 3"


def fake_ssh(tmp_path: Path, monkeypatch, rc: int = 0, out: str = "", err: str = "") -> Path:
    record = tmp_path / "ssh.json"
    bin_dir = tmp_path / "sshbin"
    bin_dir.mkdir()
    exe = bin_dir / "ssh"
    exe.write_text(
        f"#!{sys.executable}\nimport json, sys\njson.dump(sys.argv[1:], open({str(record)!r}, 'w'))\n"
        f"sys.stdout.write({out!r}); sys.stderr.write({err!r}); sys.exit({rc})\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return record


@pytest.mark.parametrize(
    ("args", "command"),
    [
        (["rcon", "list"], "mc list"),
        (["logs", "mc-health", "-n", "5"], "sudo journalctl -u mc-health -n 5 --no-pager"),
        (["backup", "list", "--latest", "2"], "sudo mc-restic restic snapshots --latest 2"),
    ],
)
def test_cli_tailnet_wrappers(tmp_path, monkeypatch, config_repo, args, command) -> None:
    record = fake_ssh(tmp_path, monkeypatch)
    assert cli.main([*args[:1], "-C", str(config_repo), *args[1:]]) == 0
    argv = json.loads(record.read_text())
    assert argv[-3:] == ["deploy@example", "--", command] and "BatchMode=yes" in argv


SUDO_WARNING = "sudo: unable to resolve host mc-server: Name or service not known\n"


def backup_run(result: str = "success", status: str = "0", start: str = "0", condition: str = "yes") -> str:
    """What backup_now_command prints on the server (systemctl show's properties, then the journal tail)."""
    return (
        f"@@mcctl:backup\nStartExit={start}\nResult={result}\nExecMainStatus={status}\nConditionResult={condition}\n"
        "@@mcctl:journal\nsnapshot 9ef24d38 saved\nFinished mc-backup.service\n"
    )


def test_backup_outcome_is_the_units_result_never_stderr() -> None:
    def proc(out: str, rc: int = 0, err: str = SUDO_WARNING) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["ssh"], rc, out, err)

    # first real setup: the backup succeeded, sudo warned on stderr (and `systemctl status` exited 3)
    assert remote.backup_outcome(proc(backup_run())) == (True, "mc-backup.service finished: Result=success")
    ok, detail = remote.backup_outcome(proc(backup_run("exit-code", "1", "1"), rc=1))
    assert not ok and "Result=exit-code ExecMainStatus=1" in detail and "9ef24d38" in detail
    ok, detail = remote.backup_outcome(proc(backup_run(condition="no")))
    assert not ok and "fenced" in detail
    ok, detail = remote.backup_outcome(
        proc("", rc=255, err="ssh: connect to host mc-server port 22: timed out")
    )
    assert not ok and "ssh exit 255" in detail and "timed out" in detail
    command = remote.backup_now_command()
    assert "systemctl status" not in command and "Result,ExecMainStatus,ConditionResult" in command


@pytest.mark.parametrize(
    ("out", "rc", "want"), [(backup_run(), 0, 0), (backup_run("exit-code", "1", "1"), 1, 1)]
)
def test_cli_backup_now(tmp_path, monkeypatch, config_repo, capsys, out, rc, want) -> None:
    record = fake_ssh(tmp_path, monkeypatch, rc=rc, out=out, err=SUDO_WARNING)
    assert cli.main(["backup", "-C", str(config_repo), "now"]) == want
    assert json.loads(record.read_text())[-3:] == ["deploy@example", "--", remote.backup_now_command()]
    captured = capsys.readouterr()
    assert "Finished mc-backup.service" in captured.out + captured.err


def test_cli_ssh_is_interactive(tmp_path, monkeypatch, config_repo) -> None:
    record = fake_ssh(tmp_path, monkeypatch, rc=3)
    assert cli.main(["ssh", "-C", str(config_repo)]) == 3
    argv = json.loads(record.read_text())
    assert argv[-1] == "deploy@example" and "BatchMode=yes" not in argv


def test_cli_status_json(tmp_path, monkeypatch, config_repo, capsys) -> None:
    monkeypatch.setattr(
        remote,
        "capture",
        lambda argv: subprocess.CompletedProcess(list(argv), 0, STATUS_OUT if argv[0] == "ssh" else "[]", ""),
    )
    monkeypatch.setattr(remote.status, "__defaults__", (remote.capture,))
    assert cli.main(["status", "-C", str(config_repo), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["players_online"] == 2 and data["reachable"] is True


def test_admin_stop_start_commands() -> None:
    assert (
        remote.admin_command("stop", "restore\nnow") == "sudo /opt/minecraft/bin/admin.py stop 'restore now'"
    )
    assert remote.admin_command("stop") == "sudo /opt/minecraft/bin/admin.py stop 'mcctl stop'"
    assert remote.admin_command("start") == "sudo /opt/minecraft/bin/admin.py start"
    with pytest.raises(ValueError):
        remote.admin_command("reboot")


def test_admin_script_path_matches_ansible() -> None:
    import yaml

    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    assert remote.ADMIN_SCRIPT == gv["mc_root"] + "/bin/admin.py"
    assert "admin.py" in (REPO / "ansible/roles/base/tasks/main.yml").read_text()
