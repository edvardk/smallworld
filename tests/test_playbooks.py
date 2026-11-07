"""Playbooks parse, run on the rendered config repo inventory (P3), and site.yml's secret check works with
mock MC_SECRET_* env (P2). No host is touched: only the `always` pre_tasks run, against localhost."""

import os
import shutil
import subprocess

import pytest
from conftest import REPO, make_config_repo

from mcctl import configrepo, render
from mcctl.secrets import SECRETS

pytestmark = pytest.mark.skipif(shutil.which("ansible-playbook") is None, reason="needs ansible-playbook")
MOCK = {s.env: f"mock-{s.name.lower()}-value" for s in SECRETS}


def run(args: list[str], env: dict[str, str], adhoc: bool = False) -> subprocess.CompletedProcess[str]:
    base = {k: v for k, v in os.environ.items() if not k.startswith("MC_SECRET_")}
    return subprocess.run(
        ["ansible" if adhoc else "ansible-playbook", *args],
        cwd=REPO / "ansible",
        env=base | {"ANSIBLE_NOCOLOR": "1", "ANSIBLE_GATHERING": "explicit"} | env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


@pytest.fixture(scope="module")
def inventory(tmp_path_factory: pytest.TempPathFactory) -> str:
    """The rendered inventory of a config repo made from the template."""
    repo = configrepo.load(make_config_repo(tmp_path_factory.mktemp("config"), renderable=True))
    out = tmp_path_factory.mktemp("rendered")
    render.render(repo, out)
    return str(out / render.INVENTORY_FILE)


@pytest.mark.parametrize("playbook", ["site.yml", "bootstrap.yml"])
def test_syntax_check_with_mock_env(playbook: str, inventory: str) -> None:
    proc = run(["--syntax-check", "-i", inventory, playbook], MOCK)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def always_pre_tasks(inventory: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    # Only the `always` pre_tasks run (rendered config + secret check), locally, without become.
    return run(
        ["site.yml", "-i", inventory, "-c", "local", "-e", "ansible_become=false", "--tags", "none"], env
    )


@pytest.fixture
def secret_check(inventory: str):
    return lambda env: always_pre_tasks(inventory, env)


def test_rendered_inventory_and_group_vars_are_loaded(inventory: str) -> None:
    proc = run(["-i", inventory, "all", "-m", "debug", "-a", "var=mc_hostname"], {}, adhoc=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "mc_hostname: example" in proc.stdout  # from <rendered>/ansible/group_vars/all.yml


def test_site_refuses_to_run_without_rendered_config() -> None:
    proc = run(
        ["site.yml", "-i", "localhost,", "-c", "local", "-e", "ansible_become=false", "--tags", "none"], MOCK
    )
    assert proc.returncode != 0
    assert "No deployment values" in proc.stdout


def test_secret_check_passes_with_mock_env_and_hides_values(secret_check) -> None:
    proc = secret_check(MOCK)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "Check secrets are present" in proc.stdout
    for value in MOCK.values():
        assert value not in proc.stdout + proc.stderr


def test_secret_check_fails_without_required_secret(secret_check) -> None:
    env = {k: v for k, v in MOCK.items() if k != "MC_SECRET_RESTIC_PASSWORD"}
    proc = secret_check(env)
    assert proc.returncode != 0
    assert "Required MC_SECRET_* environment variables are empty" in proc.stdout
    optional_unset = {s.env: MOCK[s.env] for s in SECRETS if s.required}
    assert secret_check(optional_unset).returncode == 0


def test_site_refuses_a_fenced_box_before_any_role(inventory: str, tmp_path) -> None:
    """P12 review (M1): a deploy to a box `mcctl migrate` fenced fails before it changes anything."""
    args = ["site.yml", "-i", inventory, "-c", "local", "-e", "ansible_become=false", "--tags", "none"]
    args += ["-e", f"mc_state_dir={tmp_path}"]
    assert run(args, MOCK).returncode == 0
    (tmp_path / "fenced").write_text("{}")
    proc = run(args, MOCK)
    assert proc.returncode != 0 and "FENCED:" in proc.stdout, proc.stdout


def test_players_reconcile_change_detection() -> None:
    # "UNCHANGED" contains "CHANGED": the role must test the prefix (the second e2e run caught it)
    import yaml
    from conftest import REPO

    tasks = yaml.safe_load((REPO / "ansible/roles/players/tasks/main.yml").read_text())
    rec = next(t for t in tasks if t["name"] == "Reconcile whitelist.json and ops.json")
    assert rec["changed_when"] == "players_reconcile.stdout.startswith('CHANGED')"


@pytest.mark.parametrize(
    ("stdout", "alerted"),
    [
        ("UNCHANGED\nKEPT: Dora_K, eli (on the live whitelist ...)\nALERT-KEPT: Dora_K eli", "Dora_K eli"),
        ("UNCHANGED\nKEPT: Dora_K, eli (on the live whitelist ...)", None),  # announced at an earlier deploy
        ("UNCHANGED", None),
        (None, None),  # --check (mcctl deploy --check): the command was skipped, no stdout at all
    ],
)
def test_players_role_shows_kept_players_and_announces_them_as_the_ops_user(
    tmp_path, stdout, alerted
) -> None:
    """v1.3 whitelist guard: the role's own tasks after the reconcile, on localhost with a fake mc-run: KEPT is
    shown, ALERT-KEPT becomes one `mc-run notify.env mc_panel.py alert-kept NAME...`, and nothing is a change."""
    import json
    import sys

    import yaml

    tasks = yaml.safe_load((REPO / "ansible/roles/players/tasks/main.yml").read_text())
    start = next(i for i, t in enumerate(tasks) if t["name"] == "Show what the reconcile changed or kept")
    picked = tasks[start:]
    assert [t["name"] for t in picked] == [
        "Show what the reconcile changed or kept",
        "Remember whom to announce",
        "Tell Discord which players were kept although players.yml doesn't list them",
    ]
    log = tmp_path / "mc-run.log"
    fake = tmp_path / "mc-run"
    fake.write_text(
        f"#!{sys.executable}\nimport json, sys\nopen({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
    )
    fake.chmod(0o755)
    cmd = picked[2]["ansible.builtin.command"]
    cmd["argv"] = cmd["argv"].replace("/usr/local/bin/mc-run", str(fake))
    reg = (
        {"changed": False, "rc": 0, "stdout": stdout, "stdout_lines": stdout.splitlines()}
        if stdout is not None
        else {"changed": False, "skipped": True}
    )
    play = [{"hosts": "localhost", "connection": "local", "gather_facts": False,
             "vars": {"players_reconcile": reg, "mc_etc_dir": "/etc/minecraft", "mc_bin_dir": "/opt/minecraft/bin"},
             "tasks": picked}]  # fmt: skip
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    proc = run([str(tmp_path / "play.yml"), "-i", "localhost,"], {})
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "changed=0" in proc.stdout[proc.stdout.rfind("PLAY RECAP") :]
    assert ("KEPT: Dora_K, eli" in proc.stdout) == (
        "KEPT" in (stdout or "")
    ) and "ALERT-KEPT" not in proc.stdout
    calls = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
    if alerted:
        assert calls == [["-/etc/minecraft/notify.env", "/opt/minecraft/bin/mc_panel.py", "alert-kept",
                          *alerted.split()]]  # fmt: skip
    else:
        assert calls == []


def test_no_doubled_backslashes_in_playbook_yaml() -> None:
    """Plain and folded YAML scalars keep `\\\\` as two backslashes, so a Jinja regex like '^100\\\\.' matches a
    literal backslash, never an IP (the live bootstrap failure). Regexes use classes like `[.]` instead."""
    bad = [
        f"{p.relative_to(REPO)}:{n}"
        for p in sorted((REPO / "ansible").rglob("*.yml")) + sorted((REPO / "server-config").rglob("*.j2"))
        for n, line in enumerate(p.read_text().splitlines(), 1)
        if "\\\\" in line
    ]
    assert not bad, bad


def test_bootstrap_tailnet_ipv4_expressions(tmp_path) -> None:
    """bootstrap.yml's own set_fact + assert tasks, run on localhost with a real `tailscale status` shape."""
    import json

    import yaml

    tasks = yaml.safe_load((REPO / "ansible/bootstrap.yml").read_text())[0]["tasks"]
    picked = [t for t in tasks if t["name"].startswith(("The node's tailnet IPv4", "The node has a tailnet"))]
    assert len(picked) == 2
    status = {"Self": {"TailscaleIPs": ["fd7a:115c:a1e0::1", "100.64.0.7"]}}
    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {"tailscale_final": {"results": [{}, {"stdout": json.dumps(status)}]}},
            "tasks": [
                *picked,
                {"ansible.builtin.assert": {"that": "mc_tailnet_ipv4 == '100.64.0.7'", "quiet": True}},
            ],
        }
    ]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    proc = run([str(tmp_path / "play.yml"), "-i", "localhost,"], {})
    assert proc.returncode == 0, proc.stdout + proc.stderr


FAKE_TAILSCALE = """#!{python}
import json, os, sys
state_file = os.environ["FAKE_TS_STATE"]
st = json.load(open(state_file))
args = sys.argv[1:]
st.setdefault("calls", []).append(args)
if args[:2] == ["status", "--json"]:
    st["status_reads"] = st.get("status_reads", 0) + 1
    backend = "Running" if st["status_reads"] > st["starting_reads"] else "Starting"
    ips = st["ips"] if backend == "Running" else []
    print(json.dumps({{"BackendState": backend, "Self": {{"TailscaleIPs": ips, "Tags": st["tags"],
                                                          "DNSName": "mc.example.ts.net."}}}}))
elif args[:2] == ["debug", "prefs"]:
    print(json.dumps({{"RunSSH": True, "Hostname": "mc", "CorpDNS": False}}))
json.dump(st, open(state_file, "w"))
"""


def tailscale_play(
    tmp_path, *, starting_reads: int, tags: list[str]
) -> tuple[subprocess.CompletedProcess[str], dict]:
    """The tailscale role from its state read on (install steps left out), on localhost, with a fake CLI."""
    import json
    import sys

    import yaml

    tasks = yaml.safe_load((REPO / "ansible/roles/tailscale/tasks/main.yml").read_text())
    start = next(i for i, t in enumerate(tasks) if t["name"].startswith("Read Tailscale state"))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "tailscale"
    fake.write_text(FAKE_TAILSCALE.format(python=sys.executable))
    fake.chmod(0o755)
    state = tmp_path / "ts.json"
    ips = ["fd7a:115c:a1e0::1", "100.64.0.7"]
    state.write_text(json.dumps({"starting_reads": starting_reads, "ips": ips, "tags": tags}))
    gv = yaml.safe_load((REPO / "ansible/group_vars/all/main.yml").read_text())
    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars": {
                "mc_hostname": "mc",
                "tailscale_auth_key": "",
                "mc_tailscale_server_tag": gv["mc_tailscale_server_tag"],
            },
            "tasks": tasks[start:],
        }
    ]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    proc = run([str(tmp_path / "play.yml"), "-i", "localhost,"], {"PATH": path, "FAKE_TS_STATE": str(state)})
    return proc, json.loads(state.read_text())


def test_tailscale_role_waits_out_a_join_in_progress_without_a_new_key(tmp_path) -> None:
    """Re-run after the node joined (still Starting at first): no `tailscale up`, no key needed."""
    proc, st = tailscale_play(tmp_path, starting_reads=2, tags=["tag:mc-server"])
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert not any(c[0] == "up" for c in st["calls"]) and st["status_reads"] >= 3


def test_tailscale_role_refuses_a_node_without_the_server_tag(tmp_path) -> None:
    proc, st = tailscale_play(tmp_path, starting_reads=0, tags=[])
    assert proc.returncode != 0 and "not Running as tag:mc-server" in proc.stdout
    assert not any(c[0] == "up" for c in st["calls"])


def test_deploys_restart_mc_agent_when_its_code_changes() -> None:
    """v1.2.3: v1.2.2's agent fix was installed but never ran: the scripts were copied without restarting the one
    long-running process that imports them. Every server-side script mc-agent imports (transitively) must be
    installed by a task that notifies `Restart mc-agent`, and that handler must only run where mc-agent exists."""
    import ast

    import yaml

    def local_imports(name: str) -> set[str]:
        tree = ast.parse((REPO / "scripts" / name).read_text())
        mods = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        mods |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        return {f"{m}.py" for m in mods if (REPO / "scripts" / f"{m}.py").exists()}

    needed, todo = set(), ["mc_agent.py"]
    while todo:
        name = todo.pop()
        if name not in needed:
            needed.add(name)
            todo.extend(local_imports(name))
    assert {"mc_agent.py", "mc_panel.py", "mc_rcon.py", "notify.py"} <= needed

    tasks = yaml.safe_load((REPO / "ansible/roles/base/tasks/main.yml").read_text())
    install = next(t for t in tasks if t.get("name") == "Install server-side scripts")
    assert needed <= set(install["loop"]), sorted(needed - set(install["loop"]))
    assert install.get("notify") == "mc-agent code changed"

    # A flush can run the handlers before the agent role installed the unit (fresh box), and boxes without the
    # panel never have it: the listeners stat the unit first and restart only an installed agent.
    handlers = yaml.safe_load((REPO / "ansible/roles/agent/handlers/main.yml").read_text())
    listening = [h for h in handlers if h.get("listen") == "mc-agent code changed"]
    assert [next(iter(k for k in h if k.startswith("ansible.builtin."))) for h in listening] == [
        "ansible.builtin.stat",
        "ansible.builtin.systemd_service",
    ]
    stat, restart = listening
    assert stat["ansible.builtin.stat"]["path"] == "/etc/systemd/system/mc-agent.service"
    assert restart["ansible.builtin.systemd_service"]["state"] == "restarted"
    assert restart["when"] == f"{stat['register']}.stat.exists"
