"""P4 `mcctl plan` / `apply` / `bootstrap` with a fake terraform and ansible-playbook on PATH (nothing real is
ever applied): typed confirmation, plan file handling, Tailscale credential juggling, secrets via env only."""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from conftest import make_config_repo

from mcctl import bootstrap, cli, configrepo, remote, render, state, tf

KEY = "tskey-auth-fresh-secret"


def fake_tools(
    tmp_path: Path, monkeypatch, *, changes: list[dict] | None = None, apply_rc: int = 0, play_rc: int = 0
) -> Path:
    """terraform: init/plan/show/apply/output, each recording argv + selected env; ansible-playbook records."""
    log = tmp_path / "calls.jsonl"
    show = json.dumps({"resource_changes": changes if changes is not None else []})
    outputs = json.dumps({"server_tailscale_auth_key": {"sensitive": True, "value": KEY}})
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    keep = [
        "TF_DATA_DIR",
        "TAILSCALE_API_KEY",
        "TAILSCALE_OAUTH_CLIENT_ID",
        "TS_AUTHKEY",
        "CLOUDFLARE_API_TOKEN",
    ]
    common = (
        "import json, os, sys\n"
        f"open({str(log)!r}, 'a').write(json.dumps({{'tool': os.path.basename(sys.argv[0]), 'argv': sys.argv[1:], "
        f"'cwd': os.getcwd(), 'env': {{k: os.environ.get(k) for k in {keep!r}}}}}) + '\\n')\n"
    )
    scripts = {
        "terraform": common
        + (
            "cmd = sys.argv[2]\n"
            "if cmd == 'plan':\n"
            "    out = [a for a in sys.argv if a.startswith('-out=')][0][5:]\n"
            "    open(out, 'w').write('plan')\n"
            "    print('Plan: fake')\n"
            f"elif cmd == 'show': print({show!r})\n"
            f"elif cmd == 'output': print({outputs!r})\n"
            f"elif cmd == 'apply': sys.exit({apply_rc})\n"
        ),
        "ansible-playbook": common + f"sys.exit({play_rc})\n",
    }
    for name, body in scripts.items():
        exe = bin_dir / name
        exe.write_text(f"#!{sys.executable}\n{body}")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def calls(log: Path) -> list[dict]:
    return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []


def tf_calls(log: Path, cmd: str) -> list[dict]:
    return [c for c in calls(log) if c["tool"] == "terraform" and c["argv"][1] == cmd]


CREATE = [{"address": "cloudflare_dns_record.server_a", "change": {"actions": ["create"]}}]
REPLACE_KEY = [{"address": bootstrap.KEY_RESOURCE, "change": {"actions": ["delete", "create"]}}]


def test_summarize_actions() -> None:
    plan = tf.summarize(
        {
            "resource_changes": [
                {"address": "a", "change": {"actions": ["create"]}},
                {"address": "b", "change": {"actions": ["update"]}},
                {"address": "c", "change": {"actions": ["create", "delete"]}},
                {"address": "d", "change": {"actions": ["delete"]}},
                {"address": "e", "change": {"actions": ["no-op"]}},
                {"address": "f", "change": {"actions": ["read"]}},
            ]
        }
    )
    assert (plan.create, plan.update, plan.replace, plan.delete) == (["a"], ["b"], ["c"], ["d"])
    assert "1 to add, 1 to change, 1 to replace, 1 to destroy" in plan.summary()
    assert "DESTROY: d" in plan.summary()
    assert tf.Plan().empty


def _moved_worker(**after_changes: object) -> dict:
    """The watchdog Worker's update after mcctl ran from another computer, shaped like the live plan that
    showed it (provider 5.28.1): the assets path differs, the manifest hash doesn't, and the computed
    attributes the provider resets on every update are unknown."""
    assets = {"asset_manifest_sha256": "ab" * 32, "config": {"run_worker_first": True}, "jwt": None}
    before = {
        "id": "lh-watchdog",
        "compatibility_date": "2026-10-01",
        "assets": assets | {"directory": "/home/a/code/cfg/.mcctl/rendered/watchdog"},
        "bindings": [{"name": "DB", "type": "d1", "id": "d1"}, {"name": "CONTROL", "namespace_id": None}],
        "etag": "old",
        "handlers": ["fetch", "scheduled"],
        "modified_on": "2026-11-09T17:54:27Z",
        "migration_tag": "v1",
    }
    after = {
        "id": "lh-watchdog",
        "compatibility_date": "2026-10-01",
        "assets": assets | {"directory": "/Users/b/code/cfg/.mcctl/rendered/watchdog"},
        "bindings": [{"name": "DB", "type": "d1", "id": "d1"}, {"name": "CONTROL"}],
    } | after_changes
    unknown = {
        "assets": {},
        "bindings": [{}, {"namespace_id": True}],
        "etag": True,
        "handlers": True,
        "modified_on": True,
        "migration_tag": True,
    }
    return {
        "address": "cloudflare_workers_script.watchdog",
        "type": "cloudflare_workers_script",
        "change": {"actions": ["update"], "before": before, "after": after, "after_unknown": unknown},
    }


def test_summarize_marks_a_moved_assets_directory(capsys) -> None:
    rc = _moved_worker()
    assert tf.relocated_only(rc)
    plan = tf.summarize({"resource_changes": [rc]})
    assert plan.update == plan.relocated == ["cloudflare_workers_script.watchdog"]
    assert plan.only_relocated and not plan.empty  # still applied: the state records the new path
    assert plan.rows() == [["info: path only", "cloudflare_workers_script.watchdog"]]
    tf.show_plan(plan, "would")
    assert "the files are identical" in capsys.readouterr().out


def test_summarize_keeps_real_worker_changes() -> None:
    other = {
        "assets": {"directory": "/Users/b/x", "asset_manifest_sha256": "cd" * 32, "config": {}, "jwt": None}
    }
    for rc in (
        _moved_worker(compatibility_date="2026-12-01"),  # a configured change besides the path
        _moved_worker(**other),  # different files
        _moved_worker(bindings=[{"name": "DB", "type": "d1", "id": "d2"}, {"name": "CONTROL"}]),
    ):
        assert not tf.relocated_only(rc)
    unknown_hash = _moved_worker()
    unknown_hash["change"]["after_unknown"]["assets"] = {"asset_manifest_sha256": True}
    same_path = _moved_worker()
    same_path["change"]["after"]["assets"] = same_path["change"]["before"]["assets"]
    other_type = _moved_worker() | {"type": "cloudflare_worker", "address": "cloudflare_worker.x"}
    for rc in (unknown_hash, same_path, other_type):
        assert not tf.relocated_only(rc)
    plan = tf.summarize({"resource_changes": [_moved_worker(), *CREATE]})
    assert plan.relocated and not plan.only_relocated
    assert (
        tf.summarize({"resource_changes": [{"address": "b", "change": {"actions": ["update"]}}]}).relocated
        == []
    )


def test_confirm_needs_the_name(capsys) -> None:
    plan = tf.Plan(create=["x"])
    assert tf.confirm(plan, "lh", lambda _p: "lh")
    assert not tf.confirm(plan, "lh", lambda _p: "yes")

    def eof(_p: str) -> str:
        raise EOFError

    assert not tf.confirm(plan, "lh", eof)
    assert "no terminal" in capsys.readouterr().out


def test_environment_drops_oauth_with_api_key(tmp_path) -> None:
    shell = {
        "PATH": "/bin",
        "TAILSCALE_API_KEY": "k",
        "TAILSCALE_OAUTH_CLIENT_ID": "i",
        "TAILSCALE_OAUTH_CLIENT_SECRET": "s",
    }
    env = tf.environment(tmp_path, shell)
    assert "TAILSCALE_OAUTH_CLIENT_ID" not in env and env["TAILSCALE_API_KEY"] == "k"
    assert env["TF_DATA_DIR"] == str(tmp_path / ".mcctl" / "terraform")


def test_environment_reads_dotenv_shell_wins(tmp_path) -> None:
    (tmp_path / ".env").write_text(
        "CLOUDFLARE_API_TOKEN=from-file\nexport GITHUB_TOKEN='quoted'\nEMPTY=\n# c\n"
    )
    (tmp_path / ".env").chmod(0o600)
    env = tf.environment(tmp_path, {"CLOUDFLARE_API_TOKEN": "from-shell"})
    assert env["CLOUDFLARE_API_TOKEN"] == "from-shell" and env["GITHUB_TOKEN"] == "quoted"
    assert "EMPTY" not in env
    (tmp_path / ".env").chmod(0o644)
    with pytest.raises(tf.TfError, match="chmod 600"):
        tf.environment(tmp_path, {})


def test_plan_prints_summary_and_keeps_no_plan_file(tmp_path, monkeypatch, capsys, config_repo) -> None:
    log = fake_tools(tmp_path, monkeypatch, changes=CREATE)
    assert cli.main(["plan", "-C", str(config_repo), "--target", "cloudflare_dns_record.server_a"]) == 0
    plan = tf_calls(log, "plan")[0]
    rendered = config_repo.resolve() / ".mcctl/rendered"
    assert f"-var-file={rendered / render.TFVARS_FILE}" in plan["argv"]
    assert "-target=cloudflare_dns_record.server_a" in plan["argv"] and "-input=false" in plan["argv"]
    assert plan["env"]["TF_DATA_DIR"] == str(config_repo.resolve() / ".mcctl/terraform")
    assert "1 to add" in capsys.readouterr().out
    assert not tf_calls(log, "apply")
    assert not list((config_repo / ".mcctl/terraform").glob("tfplan*"))


def test_apply_confirmed(tmp_path, monkeypatch, config_repo) -> None:
    log = fake_tools(tmp_path, monkeypatch, changes=CREATE)
    monkeypatch.setattr("builtins.input", lambda _p: "example")
    assert cli.main(["apply", "-C", str(config_repo)]) == 0
    apply = tf_calls(log, "apply")
    assert len(apply) == 1 and apply[0]["argv"][-1].endswith(".mcctl/terraform/tfplan")
    assert not (config_repo / ".mcctl/terraform/tfplan").exists()  # deleted after apply
    assert state.load(config_repo.resolve()).is_done("apply")


def test_apply_aborted_or_no_terminal(tmp_path, monkeypatch, config_repo) -> None:
    log = fake_tools(tmp_path, monkeypatch, changes=CREATE)
    monkeypatch.setattr("builtins.input", lambda _p: "y")
    assert cli.main(["apply", "-C", str(config_repo)]) == 1
    assert not tf_calls(log, "apply") and not (config_repo / ".mcctl/terraform/tfplan").exists()


def test_apply_without_changes(tmp_path, monkeypatch, capsys, config_repo) -> None:
    log = fake_tools(tmp_path, monkeypatch, changes=[])
    monkeypatch.setattr("builtins.input", lambda _p: pytest.fail("asked without changes"))
    assert cli.main(["apply", "-C", str(config_repo)]) == 1
    assert "No changes" in capsys.readouterr().out and not tf_calls(log, "apply")


def test_apply_failure(tmp_path, monkeypatch, capsys, config_repo) -> None:
    fake_tools(tmp_path, monkeypatch, changes=CREATE, apply_rc=1)
    monkeypatch.setattr("builtins.input", lambda _p: "example")
    assert cli.main(["apply", "-C", str(config_repo)]) == 1
    assert "`terraform apply` failed" in capsys.readouterr().err
    assert not state.load(config_repo.resolve()).is_done("apply")


def test_apply_refuses_placeholders(tmp_path, monkeypatch, capsys) -> None:
    log = fake_tools(tmp_path, monkeypatch, changes=CREATE)
    root = make_config_repo(tmp_path / "c")
    assert cli.main(["apply", "-C", str(root)]) == 1
    assert "refusing to apply placeholder values" in capsys.readouterr().err and not calls(log)


# --- bootstrap ----------------------------------------------------------------------------------------


def host_repo(tmp_path: Path) -> Path:
    data = yaml.safe_load((make_config_repo(tmp_path / "x", renderable=True) / "server.yml").read_text())
    data["host"] |= {"ipv4": "9.9.9.9", "ipv6": "2620:fe::fe"}
    return make_config_repo(tmp_path / "c", data)


TS_STATUS = json.dumps(
    {
        "BackendState": "Running",
        "Peer": {
            "nodekey:a": {"DNSName": "example-old.tail1234.ts.net.", "TailscaleIPs": ["100.64.0.9"]},
            "nodekey:b": {
                "DNSName": "example.tail1234.ts.net.",
                "TailscaleIPs": ["fd7a:115c:a1e0::1", "100.101.102.103"],
            },
        },
    }
)


def ok_runner(argv):
    out = TS_STATUS if list(argv[:2]) == ["tailscale", "status"] else ""
    return subprocess.CompletedProcess(list(argv), 0, out, "")


def prober(states: dict):
    """Port-22 probe answers per address, each a list consumed in order (before bootstrap, after)."""
    states = {k: list(v) for k, v in states.items()}
    return lambda address, port: states[address].pop(0)


def test_bootstrap_end_to_end(tmp_path, monkeypatch, capsys) -> None:
    root = host_repo(tmp_path)
    log = fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", "admin-token")
    repo = configrepo.load(root)
    seen: list[list[str]] = []

    def runner(argv):
        seen.append(list(argv))
        return ok_runner(argv)

    rc = bootstrap.run(
        repo,
        cli.schema.PRODUCT_ROOT,
        ask=lambda _p: "example",
        runner=runner,
        prober=prober({"9.9.9.9": ["open", "no-answer"], "2620:fe::fe": ["open", "refused"]}),
    )
    assert rc == 0
    assert f"-replace={bootstrap.KEY_RESOURCE}" in tf_calls(log, "plan")[0]["argv"]
    play = next(c for c in calls(log) if c["tool"] == "ansible-playbook")
    rendered = root.resolve() / ".mcctl/rendered"
    assert play["argv"] == [
        "-i", str(rendered / render.INVENTORY_FILE), "bootstrap.yml", "-e", "ansible_host=9.9.9.9", "-e", "ansible_user=root",
    ]  # fmt: skip
    assert Path(play["cwd"]) == cli.schema.PRODUCT_ROOT / "ansible"
    # The fresh key reaches the playbook only through its environment; admin tokens don't.
    assert play["env"]["TS_AUTHKEY"] == KEY and play["env"]["CLOUDFLARE_API_TOKEN"] is None
    # Verified over the node's 100.x address from `tailscale status`, never the (DNS-resolved) short name.
    ssh = next(a for a in seen if a[0] == "ssh")
    assert ssh[-3:] == ["deploy@100.101.102.103", "--", "true"]
    out = capsys.readouterr()
    assert KEY not in out.out + out.err
    assert "public TCP 22 on 9.9.9.9: closed" in out.out
    rec = state.load(root.resolve()).get("bootstrap")
    assert rec["status"] == "done" and rec["outputs"] == {"public_ssh_closed": True}

    # Re-running after success detects it: no plan, no apply, no new key.
    log.unlink()
    rc = bootstrap.run(repo, cli.schema.PRODUCT_ROOT, ask=lambda _p: pytest.fail("asked"), runner=ok_runner)
    assert rc == 0 and not calls(log)
    assert "Already bootstrapped" in capsys.readouterr().out


def joined_runner(argv):
    """This laptop sees the server online with its tag (an earlier bootstrap joined it, then failed)."""
    if list(argv[:2]) != ["tailscale", "status"]:
        return ok_runner(argv)
    status = json.loads(TS_STATUS)
    status["Peer"]["nodekey:b"] |= {"Online": True, "Tags": [bootstrap.SERVER_TAG]}
    return subprocess.CompletedProcess(list(argv), 0, json.dumps(status), "")


def test_bootstrap_rerun_on_a_joined_node_mints_no_key(tmp_path, monkeypatch, capsys) -> None:
    root = host_repo(tmp_path)
    log = fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY)
    repo = configrepo.load(root)
    state.load(root.resolve()).record(
        "bootstrap", "failed", inputs={"ipv4": "9.9.9.9", "ipv6": "2620:fe::fe", "tailscale_name": "example"}
    )
    rc = bootstrap.run(
        repo,
        cli.schema.PRODUCT_ROOT,
        ask=lambda _p: pytest.fail("asked to confirm an apply"),
        runner=joined_runner,
        prober=prober({"9.9.9.9": ["open", "no-answer"], "2620:fe::fe": ["open", "refused"]}),
    )
    assert rc == 0 and not tf_calls(log, "plan") and not tf_calls(log, "apply")
    play = next(c for c in calls(log) if c["tool"] == "ansible-playbook")
    assert not play["env"]["TS_AUTHKEY"]  # the role skips `tailscale up` on a Running node
    assert "no new key minted" in capsys.readouterr().out


def test_bootstrap_force_mints_a_key_even_when_joined(tmp_path, monkeypatch) -> None:
    root = host_repo(tmp_path)
    log = fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY)
    rc = bootstrap.run(
        configrepo.load(root),
        cli.schema.PRODUCT_ROOT,
        ask=lambda _p: "example",
        runner=joined_runner,
        prober=prober({"9.9.9.9": ["open", "no-answer"], "2620:fe::fe": ["open", "refused"]}),
        force=True,
    )
    assert rc == 0 and tf_calls(log, "apply")
    assert bootstrap.already_joined("example", joined_runner) and not bootstrap.already_joined(
        "example", ok_runner
    )


def test_bootstrap_rerun_when_tailnet_ssh_broke(tmp_path, monkeypatch, capsys) -> None:
    root = host_repo(tmp_path)
    log = fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY)
    repo = configrepo.load(root)
    state.load(root.resolve()).record(
        "bootstrap", "done", inputs={"ipv4": "9.9.9.9", "ipv6": "2620:fe::fe", "tailscale_name": "example"}
    )

    def no_peer(argv):
        return subprocess.CompletedProcess(list(argv), 0, json.dumps({"Peer": {}}), "")

    assert bootstrap.run(repo, cli.schema.PRODUCT_ROOT, runner=no_peer) == 1
    assert "--force" in capsys.readouterr().out and not calls(log)


def test_bootstrap_port_22_unverifiable_from_here(tmp_path, monkeypatch, capsys) -> None:
    """No IPv6 on this laptop: v6 port 22 can't be proven closed, so it's reported as unverifiable."""
    root = host_repo(tmp_path)
    fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY)
    repo = configrepo.load(root)
    rc = bootstrap.run(
        repo,
        cli.schema.PRODUCT_ROOT,
        ask=lambda _p: "example",
        runner=ok_runner,
        prober=prober({"9.9.9.9": ["open", "no-answer"], "2620:fe::fe": ["no-route", "no-route"]}),
    )
    out = capsys.readouterr().out
    assert rc == 0 and "2620:fe::fe: unverifiable from here" in out and "9.9.9.9: closed" in out
    assert state.load(root.resolve()).get("bootstrap")["outputs"] == {"public_ssh_closed": False}


def test_bootstrap_verifies_afterwards(tmp_path, monkeypatch, capsys) -> None:
    root = host_repo(tmp_path)
    fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY)
    repo = configrepo.load(root)
    rc = bootstrap.run(
        repo,
        cli.schema.PRODUCT_ROOT,
        ask=lambda _p: "example",
        runner=ok_runner,
        prober=prober({"9.9.9.9": ["open", "no-answer"], "2620:fe::fe": ["open", "open"]}),
    )
    assert rc == 1 and "public TCP 22 still answers on 2620:fe::fe" in capsys.readouterr().out
    assert state.load(root.resolve()).get("bootstrap")["status"] == "failed"
    seen: list[list[str]] = []

    def failing_ssh(argv):
        seen.append(list(argv))
        if argv[0] == "tailscale":
            return ok_runner(argv)
        return subprocess.CompletedProcess(list(argv), 255, "", "")

    problems, _notes = bootstrap.verify(repo, failing_ssh, lambda a, p: "no-answer")
    assert problems == ["`ssh deploy@100.101.102.103 true` (Tailscale SSH to example) failed"]
    assert seen[1][-3:] == ["deploy@100.101.102.103", "--", "true"] and "BatchMode=yes" in seen[1]


def test_tailnet_address_ignores_other_nodes_and_bad_output() -> None:
    assert bootstrap.tailnet_address("example", ok_runner) == "100.101.102.103"
    assert bootstrap.tailnet_address("exam", ok_runner) is None  # prefix of another name doesn't match
    bad = lambda argv: subprocess.CompletedProcess(list(argv), 1, "", "not running")  # noqa: E731
    assert bootstrap.tailnet_address("example", bad) is None


def test_probe_reports_refused_and_no_route() -> None:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # bound, not listening: connect is refused
        assert bootstrap.probe("127.0.0.1", port, timeout=1) == "refused"
        s.listen()
        assert bootstrap.probe("127.0.0.1", port, timeout=1) == "open"


@pytest.mark.parametrize(
    ("after", "fragment"),
    [("open", "public SSH is still open"), ("no-answer", "after the firewall closed public 22")],
)
def test_bootstrap_playbook_failure_message_depends_on_the_step(
    tmp_path, monkeypatch, capsys, after, fragment
) -> None:
    root = host_repo(tmp_path)
    fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY, play_rc=2)
    repo = configrepo.load(root)
    rc = bootstrap.run(
        repo,
        cli.schema.PRODUCT_ROOT,
        ask=lambda _p: "example",
        runner=ok_runner,
        prober=prober({"9.9.9.9": ["open", after], "2620:fe::fe": ["open"]}),
    )
    assert rc == 2 and fragment in capsys.readouterr().out
    assert state.load(root.resolve()).get("bootstrap")["status"] == "failed"


def test_bootstrap_abort(tmp_path, monkeypatch) -> None:
    root = host_repo(tmp_path)
    log = fake_tools(tmp_path, monkeypatch, changes=REPLACE_KEY)
    repo = configrepo.load(root)
    closed = lambda a, p: "no-answer"  # noqa: E731
    assert (
        bootstrap.run(repo, cli.schema.PRODUCT_ROOT, ask=lambda _p: "no", runner=ok_runner, prober=closed)
        == 1
    )
    assert not tf_calls(log, "apply") and not any(c["tool"] == "ansible-playbook" for c in calls(log))


def test_bootstrap_needs_a_host(config_repo, capsys) -> None:
    assert cli.main(["bootstrap", "-C", str(config_repo)]) == 1
    assert "set host.ipv4" in capsys.readouterr().err


def test_bootstrap_playbook_checks_tailscale_ssh_itself() -> None:
    # Design §4 step 10: no manual pause; the play checks Tailscale SSH from the controller before the firewall.
    play = yaml.safe_load((cli.schema.PRODUCT_ROOT / "ansible/bootstrap.yml").read_text())[0]
    names = [t["name"] for t in play["tasks"]]
    assert not any("ansible.builtin.pause" in t for t in play["tasks"])
    check = next(t for t in play["tasks"] if "ansible.builtin.command" in t)
    assert check["delegate_to"] == "localhost" and check["become"] is False and check["changed_when"] is False
    # The node's own 100.x address (set_fact from its tailscale status), not a DNS name.
    assert check["ansible.builtin.command"]["argv"][-2:] == [
        "{{ deploy_user }}@{{ mc_tailnet_ipv4 }}",
        "true",
    ]
    fact = next(t for t in play["tasks"] if "ansible.builtin.set_fact" in t)
    assert "TailscaleIPs" in fact["ansible.builtin.set_fact"]["mc_tailnet_ipv4"]
    assert "BatchMode=yes" in check["ansible.builtin.command"]["argv"]
    assert names.index(check["name"]) < next(i for i, n in enumerate(names) if n.startswith("Firewall"))
    assert remote.render.DEPLOY_USER == "deploy"


def test_plan_file_is_private_before_terraform_writes(tmp_path, monkeypatch) -> None:
    """The plan file exists as 0600 (in a 0700 dir) before terraform runs, and terraform runs with umask 077."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    seen = tmp_path / "seen.json"
    exe = bin_dir / "terraform"
    exe.write_text(
        f"#!{sys.executable}\nimport json, os, stat, sys\n"
        "if sys.argv[2] == 'plan':\n"
        "    out = [a for a in sys.argv if a.startswith('-out=')][0][5:]\n"
        "    um = os.umask(0); os.umask(um)\n"
        "    info = {'mode': stat.S_IMODE(os.stat(out).st_mode), 'dir': stat.S_IMODE(os.stat(os.path.dirname(out)).st_mode), 'umask': um}\n"
        f"    open({str(seen)!r}, 'w').write(json.dumps(info))\n"
        "    open(out, 'w').write('plan')\n"
        "elif sys.argv[2] == 'show': print('{}')\n"
    )
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    out = tmp_path / "cfg" / ".mcctl" / "terraform" / "tfplan"
    tf.Terraform(tmp_path, dict(os.environ)).plan(tmp_path / "vars.json", out)
    assert json.loads(seen.read_text()) == {"mode": 0o600, "dir": 0o700, "umask": 0o077}
