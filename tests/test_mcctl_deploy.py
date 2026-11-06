"""`mcctl deploy` (P2, P3): render the config repo, Terraform outputs -> MC_SECRET_* env -> ansible-playbook
(rendered inventory), never via argv."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest
from conftest import make_config_repo

from mcctl import cli, deploy, galaxy, render
from mcctl.secrets import SECRETS

VALUES = {s.output: f"value-of-{s.output}" for s in SECRETS}


def fake_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, outputs: dict, tf_rc: int = 0, init_rc: int = 0
) -> Path:
    """terraform: `init` records its argv + TF_DATA_DIR, `output -json` prints `outputs`; ansible-playbook
    records argv, cwd and env."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.json"
    tf_record = tmp_path / "tf-init.json"
    tf_json = json.dumps({k: {"sensitive": True, "type": "string", "value": v} for k, v in outputs.items()})
    scripts = {
        "terraform": (
            "import json, os, sys\n"
            "if not sys.argv[1].startswith('-chdir='): sys.exit(9)\n"
            "if sys.argv[2] == 'init':\n"
            f"    json.dump({{'argv': sys.argv[1:], 'data_dir': os.environ.get('TF_DATA_DIR')}}, open({str(tf_record)!r}, 'w'))\n"
            f"    sys.stderr.write('init failed\\n') if {init_rc} else None\n"
            f"    sys.exit({init_rc})\n"
            "if sys.argv[2:] != ['output', '-json']: sys.exit(9)\n"
            f"sys.stderr.write('tf failed\\n') if {tf_rc} else print({tf_json!r})\n"
            f"sys.exit({tf_rc})\n"
        ),
        "ansible-playbook": (
            "import json, os, sys\n"
            f"json.dump({{'argv': sys.argv[1:], 'cwd': os.getcwd(), 'env': dict(os.environ)}}, open({str(record)!r}, 'w'))\n"
        ),
        # the Tailscale SSH check session before Ansible (remote.login_check): argv only
        "ssh": f"import json, sys\njson.dump(sys.argv[1:], open({str(tmp_path / 'ssh.json')!r}, 'w'))\n",
    }
    for name, body in scripts.items():
        exe = bin_dir / name
        exe.write_text(f"#!{sys.executable}\n{body}")
        exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return record


def test_secret_env_maps_every_secret() -> None:
    env = deploy.secret_env(VALUES)
    assert env == {s.env: VALUES[s.output] for s in SECRETS}
    assert all(k.startswith("MC_SECRET_") for k in env)


def test_optional_secrets_may_be_unset() -> None:
    outputs = {s.output: (None if not s.required else "x") for s in SECRETS}
    env = deploy.secret_env(outputs)
    assert env["MC_SECRET_DISCORD_WEBHOOK_URL"] == "" and env["MC_SECRET_RCON_PASSWORD"] == "x"


def test_required_secret_missing() -> None:
    outputs = dict(VALUES) | {"rcon_password": ""}
    del outputs["restic_password"]
    with pytest.raises(deploy.DeployError, match="rcon_password, restic_password"):
        deploy.secret_env(outputs)
    with pytest.raises(deploy.DeployError, match="isn't a string"):
        deploy.secret_env(VALUES | {"rcon_password": 5})


def test_deploy_passes_secrets_only_through_env(tmp_path, monkeypatch, capsys, config_repo) -> None:
    record = fake_tools(tmp_path, monkeypatch, VALUES)
    monkeypatch.setenv("MC_SECRET_STALE", "old")  # left over in the shell: must not leak through
    for admin in (
        "CLOUDFLARE_API_TOKEN",
        "GITHUB_TOKEN",
        "AWS_SECRET_ACCESS_KEY",
        "TAILSCALE_OAUTH_CLIENT_SECRET",
    ):
        monkeypatch.setenv(admin, f"admin-{admin}")
    monkeypatch.setenv("ANSIBLE_FORCE_COLOR", "1")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/run/agent.sock")
    assert cli.main(["deploy", "-C", str(config_repo)]) == 0
    rec = json.loads(record.read_text())
    rendered = config_repo.resolve() / ".mcctl/rendered"
    assert rec["argv"] == ["-i", str(rendered / render.INVENTORY_FILE), "site.yml"]
    # Security review: one uncaptured session first, so Tailscale's `check` login URL is visible.
    ssh = json.loads((tmp_path / "ssh.json").read_text())
    assert ssh[-3:] == ["deploy@example", "--", "true"] and "BatchMode=yes" in ssh, ssh
    assert Path(rec["cwd"]) == deploy.Path(cli.schema.PRODUCT_ROOT) / "ansible"
    # Terraform: backend from the rendered state.tfbackend, per-config-repo data dir.
    tf = json.loads((tmp_path / "tf-init.json").read_text())
    assert f"-backend-config={rendered / render.BACKEND_FILE}" in tf["argv"]
    assert "-lockfile=readonly" in tf["argv"] and "-input=false" in tf["argv"]
    assert tf["data_dir"] == str(config_repo.resolve() / ".mcctl/terraform")
    assert all((rendered / f).is_file() for f in render.FILES)
    for s in SECRETS:
        assert rec["env"][s.env] == VALUES[s.output]
    assert "MC_SECRET_STALE" not in rec["env"]
    # Admin credentials from .env never reach ansible-playbook; the allowlisted basics do.
    assert not any(v.startswith("admin-") for v in rec["env"].values())
    assert rec["env"]["ANSIBLE_FORCE_COLOR"] == "1" and rec["env"]["SSH_AUTH_SOCK"] == "/run/agent.sock"
    assert "PATH" in rec["env"]
    # The pinned galaxy collections, even though this checkout is the pin (never the user's ~/.ansible).
    pinned = galaxy.installed(Path(rec["env"]["ANSIBLE_COLLECTIONS_PATH"]))
    assert (
        pinned == galaxy.pins(cli.schema.PRODUCT_ROOT)
        and rec["env"]["ANSIBLE_COLLECTIONS_SCAN_SYS_PATH"] == "False"
    )
    allowed = deploy.ENV_ALLOW | {s.env for s in SECRETS}
    extra = {k for k in rec["env"] if k not in allowed and not k.startswith(deploy.ENV_ALLOW_PREFIXES)}
    assert extra <= {"PWD", "SHLVL", "_", "LC_CTYPE"}, extra  # set by the interpreter/OS, not inherited
    out = capsys.readouterr()
    for v in VALUES.values():
        assert v not in out.out + out.err
        assert all(v not in a for a in rec["argv"])
    assert "values not shown" in out.out
    assert "Tailscale SSH check" in out.out


def test_playbook_env_allowlist() -> None:
    shell = {
        "PATH": "/bin",
        "HOME": "/h",
        "ANSIBLE_CONFIG": "x",
        "LC_ALL": "C",
        "CLOUDFLARE_API_TOKEN": "t",
        "AWS_ACCESS_KEY_ID": "a",
        "MC_SECRET_RCON_PASSWORD": "stale",
        "ANSIBLE_STDOUT_CALLBACK": "kept-by-prefix",
    }
    env = deploy.playbook_env(shell, {"MC_SECRET_RCON_PASSWORD": "fresh"})
    assert env == {
        "PATH": "/bin",
        "HOME": "/h",
        "ANSIBLE_CONFIG": "x",
        "LC_ALL": "C",
        "ANSIBLE_STDOUT_CALLBACK": "kept-by-prefix",
        "MC_SECRET_RCON_PASSWORD": "fresh",
    }


def test_deploy_check_mode(tmp_path, monkeypatch, config_repo) -> None:
    record = fake_tools(tmp_path, monkeypatch, VALUES)
    assert cli.main(["deploy", "-C", str(config_repo), "--check"]) == 0
    assert json.loads(record.read_text())["argv"][-3:] == ["site.yml", "--check", "--diff"]


def test_deploy_fails_cleanly(tmp_path, monkeypatch, capsys, config_repo) -> None:
    record = fake_tools(tmp_path, monkeypatch, VALUES, tf_rc=1)
    assert cli.main(["deploy", "-C", str(config_repo)]) == 1
    assert "terraform output -json` failed" in capsys.readouterr().err
    assert not record.exists()


def test_deploy_init_fails_cleanly(tmp_path, monkeypatch, capsys, config_repo) -> None:
    record = fake_tools(tmp_path, monkeypatch, VALUES, init_rc=1)
    assert cli.main(["deploy", "-C", str(config_repo)]) == 1
    err = capsys.readouterr().err
    assert "`terraform init` failed" in err and "init failed" in err
    assert not record.exists()


def test_deploy_missing_outputs(tmp_path, monkeypatch, capsys, config_repo) -> None:
    record = fake_tools(tmp_path, monkeypatch, {})
    assert cli.main(["deploy", "-C", str(config_repo)]) == 1
    assert "Has `terraform apply` run?" in capsys.readouterr().err
    assert not record.exists()


def test_deploy_refuses_placeholders(tmp_path, monkeypatch, capsys) -> None:
    record = fake_tools(tmp_path, monkeypatch, VALUES)
    root = make_config_repo(tmp_path / "c")  # example.com admin
    assert cli.main(["deploy", "-C", str(root)]) == 1
    assert "refusing to deploy placeholder values" in capsys.readouterr().err
    assert not record.exists() and not (tmp_path / "tf-init.json").exists()


def test_deploy_invalid_config_repo(tmp_path, capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        cli.main(["deploy", "-C", str(tmp_path)])
    assert exc.value.code == 1
    assert "server.yml: missing" in capsys.readouterr().err


def test_deploy_without_terraform(monkeypatch, capsys, config_repo) -> None:
    monkeypatch.setattr(deploy.shutil, "which", lambda _name: None)
    assert cli.main(["deploy", "-C", str(config_repo)]) == 1
    assert "`terraform` not found" in capsys.readouterr().err
