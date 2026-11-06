"""The pinned Ansible collections (mcctl/galaxy.py): every playbook mcctl runs gets exactly the versions in
ansible/requirements.yml, from mcctl's own directory, even when the running checkout is the pin (the live
bootstrap failure: "couldn't resolve module/action 'ansible.posix.authorized_key'")."""

import json
import re
import subprocess
from pathlib import Path

import pytest
from conftest import REPO, fake_galaxy

from mcctl import deploy, galaxy, product

PINS = galaxy.pins(REPO)


def checkout(tmp_path: Path, text: str | None = None) -> Path:
    root = tmp_path / "product"
    (root / "ansible").mkdir(parents=True)
    (root / galaxy.REQUIREMENTS).write_text(text or (REPO / galaxy.REQUIREMENTS).read_text())
    return root


def recording(calls: list) -> galaxy.Runner:
    def run(argv, env):
        calls.append((list(argv), dict(env)))
        return fake_galaxy(argv, env)

    return run


def test_pins_match_requirements() -> None:
    assert set(PINS) == {"ansible.posix", "community.general"}
    assert all(v[0].isdigit() for v in PINS.values())


def test_pins_need_a_version(tmp_path) -> None:
    root = checkout(tmp_path, "collections:\n  - name: ansible.posix\n")
    with pytest.raises(galaxy.GalaxyError, match="pinned version"):
        galaxy.pins(root)


def test_ensure_installs_once_then_reuses(tmp_path) -> None:
    root = checkout(tmp_path)
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache"), "PATH": "/usr/bin", "CLOUDFLARE_API_TOKEN": "admin"}
    calls: list = []
    dest = galaxy.ensure(root, env, run=recording(calls), galaxy="/venv/bin/ansible-galaxy")
    assert (
        dest.parent == tmp_path / "cache" / "mcctl" / "collections"
    )  # keyed by requirements.yml, not in git
    assert galaxy.installed(dest) == PINS
    ((argv, genv),) = calls
    assert argv[:4] == ["/venv/bin/ansible-galaxy", "collection", "install", "-r"] and "-p" in argv
    assert "CLOUDFLARE_API_TOKEN" not in genv and genv["PATH"] == "/usr/bin"  # no admin credentials
    assert not list(dest.parent.glob("*.tmp-*"))
    assert galaxy.ensure(root, env, run=recording(calls), galaxy="x") == dest and len(calls) == 1


def test_ensure_replaces_wrong_versions(tmp_path) -> None:
    root = checkout(tmp_path)
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}
    dest = galaxy.location(root, env)
    old = dest / "ansible_collections/ansible/posix"
    old.mkdir(parents=True)
    info = {"namespace": "ansible", "name": "posix", "version": "1.0.0"}
    (old / "MANIFEST.json").write_text(json.dumps({"collection_info": info}))
    _, _, wrong = galaxy.status(root, env)
    assert any("ansible.posix is 1.0.0" in w for w in wrong)
    assert galaxy.installed(galaxy.ensure(root, env, galaxy="g")) == PINS


def test_ensure_fails_clearly(tmp_path) -> None:
    root = checkout(tmp_path)
    env = {"XDG_CACHE_HOME": str(tmp_path / "cache")}

    def broken(argv, env):
        return subprocess.CompletedProcess(list(argv), 1, "", "ERROR! galaxy.ansible.com unreachable")

    with pytest.raises(galaxy.GalaxyError, match="unreachable"):
        galaxy.ensure(root, env, run=broken, galaxy="g")

    def wrong_version(argv, env):
        dest = Path(argv[argv.index("-p") + 1]) / "ansible_collections/ansible/posix"
        dest.mkdir(parents=True)
        info = {"namespace": "ansible", "name": "posix", "version": "9.9.9"}
        (dest / "MANIFEST.json").write_text(json.dumps({"collection_info": info}))
        return subprocess.CompletedProcess(list(argv), 0, "", "")

    with pytest.raises(galaxy.GalaxyError, match=re.escape("ansible.posix is 9.9.9")):
        galaxy.ensure(root, env, run=wrong_version, galaxy="g")
    assert not galaxy.location(root, env).exists()  # nothing half-installed left


def test_ensure_without_ansible_galaxy(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(galaxy, "_find", lambda env: None)
    with pytest.raises(galaxy.GalaxyError, match="ansible-galaxy` not found"):
        galaxy.ensure(checkout(tmp_path), {"XDG_CACHE_HOME": str(tmp_path / "cache")})


def test_cached_product_uses_its_own_collections(tmp_path) -> None:
    entry = tmp_path / "cache" / "mcctl" / ("a" * 40)
    entry.mkdir(parents=True)
    checkout(tmp_path / "x").rename(entry / "src")
    (entry / product.READY).write_text("a" * 40)
    assert galaxy.location(entry / "src", {}) == (entry / "collections").resolve()


def test_deploy_points_ansible_at_the_pinned_collections_only(tmp_path, monkeypatch) -> None:
    """The running checkout is the pin (MCCTL_PRODUCT_DIR in tests): collections are still ensured."""
    dest = deploy.collections(REPO)
    assert galaxy.installed(dest) == PINS
    env = deploy.playbook_env(
        {"ANSIBLE_COLLECTIONS_PATH": "/home/u/.ansible/collections", "PATH": "/b"}, {}, dest
    )
    assert (
        env["ANSIBLE_COLLECTIONS_PATH"] == str(dest) and env["ANSIBLE_COLLECTIONS_SCAN_SYS_PATH"] == "False"
    )


def test_deploy_collections_error_names_the_manual_fix(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(galaxy, "_find", lambda env: None)
    with pytest.raises(deploy.DeployError, match=re.escape(galaxy.MANUAL)):
        deploy.collections(REPO)
