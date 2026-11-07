"""P12: `migrate`, `retire-old`, `upgrade` (product + Minecraft), `lock update`, `secrets rotate`, `players` and
`restore`, against mcctl/mock.py's fake providers and tools and recorded HTTP fixtures (nothing real is
contacted, applied or restored)."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from conftest import REPO, TEST_SHA, Replay

from mcctl import cli, configrepo, init, migrate, migrations, mock, players, restorecmd, rotate, serverops
from mcctl import upgrade as upgrade_mod
from mcctl.providers import ProviderError, tailscale
from mcctl.ui import UI

ANSWERS = yaml.safe_load((REPO / "tests/e2e/answers.yml").read_text())
P12_ANSWERS = ANSWERS | {
    "migrate_start": True,
    "rollback_start": True,
    "confirm_retire": "e2e-old",
    "confirm_restore": "restore a1b2c3d4",
    "confirm_minecraft_version": "27.1",
}
NEW_V4, NEW_V6 = "8.8.4.4", "2001:4860:4860::8844"


@pytest.fixture(scope="module")
def deployed(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A config repo as `mcctl init --mock` leaves it: deployed on 9.9.9.9, world backed up."""
    root = tmp_path_factory.mktemp("p12") / "cfg"
    talk = UI(answers=ANSWERS, mock=True, env={}, out=io.StringIO())
    assert init.run(root, talk, mock.deps(root), init.Options(mock=True, product_sha=TEST_SHA)) == 0
    return root


def fresh(deployed: Path, tmp_path: Path, answers: dict | None = None) -> tuple[init.Ctx, io.StringIO]:
    root = tmp_path / "cfg"
    shutil.copytree(deployed, root, symlinks=True)
    out = io.StringIO()
    talk = UI(answers=P12_ANSWERS if answers is None else answers, mock=True, env={}, out=out)
    deps = mock.deps(root)
    deps.tools.extra_ipv6.append(NEW_V6)  # type: ignore[attr-defined]
    deps.cloud.ts_devices.append(  # type: ignore[attr-defined]
        {"id": "n111", "name": "e2e.mock.ts.net", "hostname": "e2e", "addresses": [mock.TAILNET_IP]}
    )
    deps.tools.origin_main = git(root, "rev-parse", "HEAD").strip()  # type: ignore[attr-defined]
    return init.Ctx(root=root, ui=talk, deps=deps, opts=init.Options(mock=True)), out


def with_ui(ctx: init.Ctx, answers: dict | None = None) -> io.StringIO:
    """A fresh UI (other answers) on the same config repo and fakes: the next command's run."""
    out = io.StringIO()
    ctx.ui = UI(answers=P12_ANSWERS if answers is None else answers, mock=True, env={}, out=out)
    return out


def ssh_scripts(ctx: init.Ctx) -> list[str]:
    return [c[-1] for c in ctx.deps.tools.calls if c[0] == "ssh"]  # type: ignore[attr-defined]


def fences(ctx: init.Ctx, reason: str) -> int:
    return sum(1 for s in ssh_scripts(ctx) if "systemctl start mc-backup.service" in s and reason in s)


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def secrets_in(ctx: init.Ctx) -> list[str]:
    return ["mock-restic-password-not-a-secret", "mock-gh-oauth-token", "kMOCK1CNTRL-mockTailscaleApiKey"]


# --- migrate -------------------------------------------------------------------------------------------


def test_migrate_dry_run_prints_the_exact_sequence_and_changes_nothing(
    deployed: Path, tmp_path: Path
) -> None:
    ctx, out = fresh(deployed, tmp_path)
    head = git(ctx.root, "rev-parse", "HEAD")
    assert migrate.run(ctx, NEW_V4, NEW_V6, dry_run=True) == 0
    text = out.getvalue()
    ids = [s.id for s in migrate.STEPS]
    assert ids == ["preflight", "fence", "addresses", "rename", "bootstrap", "deploy", "verify", "smoke"]
    positions = [text.index(f". {i}") for i in ids]
    assert positions == sorted(positions), text
    assert "9.9.9.9 -> 8.8.4.4 / 2001:4860:4860::8844" in text and "e2e -> e2e-old" in text
    assert "retire-old" in text and "--rollback" in text and "dry run" in text
    assert git(ctx.root, "rev-parse", "HEAD") == head and ctx.state.get("migrate") is None
    assert ctx.deps.tools.calls == [] or all(c[0] == "git" for c in ctx.deps.tools.calls)  # type: ignore[attr-defined]


def test_migrate_end_to_end_then_resume_then_retire(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    assert migrate.run(ctx, NEW_V4, NEW_V6) == 0, out.getvalue()
    rec = ctx.state.get("migrate")
    assert rec["status"] == "done"
    outs = rec["outputs"]
    assert outs["done"] == [s.id for s in migrate.STEPS] and outs["finished"]
    assert outs["snapshot_id"].startswith("a1b2c3d4") and outs["old_device_id"] == "n111"
    cfg = configrepo.load(ctx.root).server
    assert (cfg.host.ipv4, cfg.host.ipv6) == (NEW_V4, NEW_V6)
    assert "mcctl migrate: host addresses 8.8.4.4" in git(ctx.root, "log", "--format=%s")
    calls = ctx.deps.tools.calls  # type: ignore[attr-defined]
    scripts = [c[-1] for c in calls if c[0] == "ssh"]
    fence = next(s for s in scripts if "admin.py stop" in s)
    assert "disable --now" in fence and "mc-backup.timer" in fence and "disable minecraft.service" in fence
    assert fence.index("admin.py stop") < fence.index("systemctl start mc-backup.service")
    assert any("-replace=tailscale_tailnet_key.server" in line for line in ctx.deps.tf_log)  # type: ignore[attr-defined]
    assert ctx.deps.cloud.ts_devices[0]["hostname"] == "e2e-old"  # type: ignore[attr-defined]
    text = out.getvalue()
    for secret in secrets_in(ctx):
        assert secret not in text and secret not in json.dumps(rec)
        assert all(secret not in " ".join(c) for c in calls)
    # a re-run is a no-op; another target is refused until the old box is retired
    assert migrate.run(ctx, NEW_V4, NEW_V6) == 0 and "already migrated" in out.getvalue()
    with pytest.raises(init.StepError, match="isn't retired"):
        migrate.run(ctx, "8.8.8.8", None)
    # retire-old: typed name, deletes the old node only
    assert migrate.retire_old(ctx) == 0
    assert ctx.deps.cloud.ts_devices == []  # type: ignore[attr-defined]
    assert ctx.state.get("migrate")["outputs"]["retired"] and "cancel the old machine" in out.getvalue()


def test_migrate_refuses_when_the_restore_is_not_the_fenced_snapshot(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    ctx.deps.tools.restored_from = "b" * 64  # type: ignore[attr-defined]
    assert migrate.run(ctx, NEW_V4, NEW_V6) == 1
    rec = ctx.state.get("migrate")
    assert rec["status"] == "failed" and "verify" not in rec["outputs"]["done"]
    assert "not the fenced snapshot" in out.getvalue()
    ctx.deps.tools.restored_from = None  # type: ignore[attr-defined]
    before = len(ctx.deps.tf_log)  # type: ignore[attr-defined]
    assert migrate.run(ctx, NEW_V4, NEW_V6) == 0  # resumes at verify: no second apply or fence
    assert len(ctx.deps.tf_log) == before  # type: ignore[attr-defined]


def test_migrate_rollback_before_the_cutover(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    ctx.deps.bootstrap = lambda repo, root, ask: 1  # the new host's bootstrap fails
    assert migrate.run(ctx, NEW_V4, None) == 1
    assert ctx.state.get("migrate")["outputs"]["done"] == ["preflight", "fence", "addresses", "rename"]
    fence = next(s for s in ssh_scripts(ctx) if "migrating to" in s)
    assert fence.index("systemctl start mc-backup.service") < fence.index(serverops.FENCED)
    assert fence.index("mc-restore@") < fence.index("admin.py stop")  # a running restore is waited for
    assert migrate.rollback(ctx, dry_run=True) == 0 and "unfence the old server" in out.getvalue()
    assert "fence the new server" not in out.getvalue()  # never bootstrapped: nothing ran there
    calls = len(ctx.deps.tools.calls)  # type: ignore[attr-defined]
    assert migrate.rollback(ctx) == 0, out.getvalue()
    assert configrepo.load(ctx.root).server.host.ipv4 == "9.9.9.9"
    assert ctx.deps.cloud.ts_devices[0]["hostname"] == "e2e"  # type: ignore[attr-defined]
    later = [c[-1] for c in ctx.deps.tools.calls[calls:] if c[0] == "ssh"]  # type: ignore[attr-defined]
    assert not any("migration rolled back" in s for s in later)
    unfence = next(i for i, s in enumerate(later) if f"rm -f {serverops.FENCED}" in s)
    resume = next(i for i, s in enumerate(later) if "enable --now" in s and "admin.py start" in s)
    assert unfence < resume and not any(
        "systemctl start mc-restore@" in s for s in later
    )  # its world is current
    assert "nothing to push" in out.getvalue()  # main never had the address commit
    assert migrate.rollback(ctx) == 0 and "No migration to roll back" in out.getvalue()


def _cutover_then_failed_deploy(deployed: Path, tmp_path: Path) -> tuple[init.Ctx, io.StringIO]:
    """Migrated up to the deploy, which pushed the address commit but timed out waiting: Minecraft may run on the
    new box, though `deploy` is not marked done. The new box is tailnet node n222 `e2e`."""
    ctx, out = fresh(deployed, tmp_path)
    ctx.deps.tools.backup_appends = True  # type: ignore[attr-defined]
    ctx.deps.stream = lambda argv: 1  # `gh run watch` fails
    assert migrate.run(ctx, NEW_V4, NEW_V6) == 1
    rec = ctx.state.get("migrate")["outputs"]
    assert "bootstrap" in rec["done"] and "deploy" not in rec["done"]
    assert ctx.deps.tools.origin_main == rec["addresses_commit"]  # type: ignore[attr-defined]
    ctx.deps.cloud.ts_devices.append(  # type: ignore[attr-defined]
        {"id": "n222", "name": "e2e.mock.ts.net", "hostname": "e2e", "addresses": ["100.64.0.9"]}
    )
    return ctx, out


def test_migrate_rollback_after_bootstrap_fences_the_new_box_even_without_deploy(
    deployed: Path, tmp_path: Path
) -> None:
    ctx, out = _cutover_then_failed_deploy(deployed, tmp_path)
    old_final = ctx.state.get("migrate")["outputs"]["snapshot_id"]
    calls = len(ctx.deps.tools.calls)  # type: ignore[attr-defined]
    assert migrate.rollback(ctx) == 0, out.getvalue()
    later = [c[-1] for c in ctx.deps.tools.calls[calls:] if c[0] == "ssh"]  # type: ignore[attr-defined]
    fence_new = next(i for i, s in enumerate(later) if "migration rolled back" in s)
    new_final = ctx.deps.tools.snapshots[-1]["id"]  # type: ignore[attr-defined]
    assert new_final != old_final
    unfence = next(i for i, s in enumerate(later) if f"rm -f {serverops.FENCED}" in s)
    restore = next(i for i, s in enumerate(later) if f"mc-restore@{new_final[:12]}" in s)
    resume = next(i for i, s in enumerate(later) if "enable --now" in s)
    assert fence_new < unfence < restore < resume  # restore first, then start
    assert [d["id"] for d in ctx.deps.cloud.ts_devices] == ["n111"]  # type: ignore[attr-defined]
    assert ctx.deps.cloud.ts_devices[0]["hostname"] == "e2e"  # type: ignore[attr-defined]
    # origin/main had the address commit: the revert was pushed
    assert ctx.deps.tools.origin_main == git(ctx.root, "rev-parse", "HEAD").strip()  # type: ignore[attr-defined]
    assert configrepo.load(ctx.root).server.host.ipv4 == "9.9.9.9"
    assert ctx.state.get("migrate")["outputs"]["rolled_back"]


def test_migrate_rollback_after_deploy_restores_the_new_boxs_play(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    ctx.deps.tools.backup_appends = True  # type: ignore[attr-defined]
    assert migrate.run(ctx, NEW_V4, NEW_V6) == 0, out.getvalue()
    ctx.deps.cloud.ts_devices.append(  # type: ignore[attr-defined]
        {"id": "n222", "name": "e2e.mock.ts.net", "hostname": "e2e", "addresses": ["100.64.0.9"]}
    )
    out = with_ui(ctx)
    assert migrate.rollback(ctx) == 0, out.getvalue()
    new_final = ctx.deps.tools.snapshots[-1]["id"]  # type: ignore[attr-defined]
    assert any(f"mc-restore@{new_final[:12]}" in s for s in ssh_scripts(ctx))
    assert fences(ctx, "migration rolled back") == 1


def test_migrate_rollback_rerun_after_a_failed_dns_apply_never_fences_the_old_box(
    deployed: Path, tmp_path: Path
) -> None:
    ctx, _ = _cutover_then_failed_deploy(deployed, tmp_path)
    out = with_ui(ctx, P12_ANSWERS | {"confirm_apply": "wrong"})
    assert migrate.rollback(ctx) == 1  # new box fenced, node deleted, renamed back, then the apply refused
    rb = ctx.state.get("migrate")["outputs"]["rollback"]
    assert rb["done"] == ["fence-new", "delete-new", "rename-back", "revert-addresses"], out.getvalue()
    snaps = len(ctx.deps.tools.snapshots)  # type: ignore[attr-defined]
    with pytest.raises(init.StepError, match="rollback of this migration is in progress"):
        migrate.run(ctx, NEW_V4, NEW_V6)
    assert migrate.retire_old(ctx) in (0, 1)
    assert [d["id"] for d in ctx.deps.cloud.ts_devices] == ["n111"]  # type: ignore[attr-defined]
    out = with_ui(ctx)
    assert migrate.rollback(ctx) == 0, out.getvalue()
    # `e2e` is the OLD box again: a second fence would have taken a stale "latest" snapshot of it
    assert fences(ctx, "migration rolled back") == 1
    assert len(ctx.deps.tools.snapshots) == snaps  # type: ignore[attr-defined]
    new_final = ctx.deps.tools.snapshots[-1]["id"]  # type: ignore[attr-defined]
    assert sum(f"mc-restore@{new_final[:12]}" in s for s in ssh_scripts(ctx)) == 1


def test_migrate_rollback_rerun_after_a_failed_restore_and_resume(deployed: Path, tmp_path: Path) -> None:
    ctx, _ = _cutover_then_failed_deploy(deployed, tmp_path)
    ctx.deps.tools.fail_scripts.append("systemctl start mc-restore@")  # type: ignore[attr-defined]
    out = with_ui(ctx)
    assert migrate.rollback(ctx) == 1
    rb = ctx.state.get("migrate")["outputs"]["rollback"]
    assert rb["done"][-1] == "unfence-old" and rb["restore"], out.getvalue()
    assert not any("enable --now" in s for s in ssh_scripts(ctx))  # nothing started on the stale world
    ctx.deps.tools.fail_scripts[:] = ["admin.py start"]  # type: ignore[attr-defined]
    out = with_ui(ctx)
    assert migrate.rollback(ctx) == 1
    assert ctx.state.get("migrate")["outputs"]["rollback"]["done"][-1] == "restore-old", out.getvalue()
    ctx.deps.tools.fail_scripts.clear()  # type: ignore[attr-defined]
    out = with_ui(ctx)
    assert migrate.rollback(ctx) == 0, out.getvalue()
    assert (
        sum("systemctl start mc-restore@" in s for s in ssh_scripts(ctx)) == 2
    )  # the failed one + the retry, no third
    assert fences(ctx, "migration rolled back") == 1


def test_migrate_rollback_unreachable_new_box_needs_a_typed_override(deployed: Path, tmp_path: Path) -> None:
    ctx, _ = _cutover_then_failed_deploy(deployed, tmp_path)
    ctx.deps.tools.fail_scripts.append("migration rolled back")  # type: ignore[attr-defined]
    out = with_ui(ctx, P12_ANSWERS | {"confirm_new_unreachable": "yes"})
    assert migrate.rollback(ctx) == 1 and "split brain" in out.getvalue()
    assert len(ctx.deps.cloud.ts_devices) == 2  # type: ignore[attr-defined]  (nothing deleted)
    out = with_ui(ctx, P12_ANSWERS | {"confirm_new_unreachable": f"{NEW_V4} is off"})
    assert migrate.rollback(ctx) == 0, out.getvalue()
    assert ctx.state.get("migrate")["outputs"]["rollback"]["new_fenced"] is False


def test_migrate_refuses_bad_targets(deployed: Path, tmp_path: Path) -> None:
    ctx, _ = fresh(deployed, tmp_path)
    with pytest.raises(init.StepError):
        migrate.run(ctx, "10.0.0.1", None)  # not public
    with pytest.raises(init.StepError, match=r"already host\.ipv4"):
        migrate.run(ctx, "9.9.9.9", None)


# --- Tailscale device API (recorded fixtures) -------------------------------------------------------------


def test_tailscale_device_calls_against_fixtures() -> None:
    replay = Replay("tailscale_devices.json")
    key = "tskey-api-kFIXTURE1-notARealKey"
    dev = tailscale.find_device(key, "mc-server", replay)
    assert dev is not None and dev["id"] == "12345" and tailscale.machine_name(dev) == "mc-server"
    tailscale.rename_device(key, "12345", "mc-server-old", replay)
    assert tailscale.delete_device(key, "12345", replay)
    assert tailscale.delete_device(key, "99999", replay)  # 404: already gone
    assert json.loads(replay.requests[1].body) == {"name": "mc-server-old"}
    assert all(r.headers["Authorization"] == f"Bearer {key}" for r in replay.requests)
    with pytest.raises(ProviderError):
        tailscale.rename_device(key, "403", "x", replay)


# --- secrets rotate -------------------------------------------------------------------------------------


def test_rotate_rcon_applies_the_replace_then_deploys(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    deployed_with = []
    assert rotate.run(ctx, "rcon", lambda repo, root: deployed_with.append(repo.root) or 0) == 0, (
        out.getvalue()
    )
    assert ctx.deps.tf_log[-2:] == ["plan -replace=random_password.rcon", "apply"]  # type: ignore[attr-defined]
    assert deployed_with == [ctx.root]
    assert rotate.run(ctx, "rcon", lambda r, p: 0, dry_run=True) == 0 and "dry run" in out.getvalue()
    assert rotate.run(ctx, "nope", lambda r, p: 0) == 2


def _rotating_deployer(ctx: init.Ctx, calls: list[str], rc: int = 0) -> rotate.Deployer:
    """A deploy that (like the backups role's restic_rekey.py) leaves the server on a new key."""

    def deployer(repo: configrepo.ConfigRepo, root: Path) -> int:
        calls.append("deploy")
        if rc == 0:
            tools = ctx.deps.tools  # type: ignore[attr-defined]
            if not any(k["id"].startswith("9e70") for k in tools.restic_keys):
                tools.restic_keys = [{"id": k["id"], "current": False} for k in tools.restic_keys]
                tools.restic_keys.append({"id": "9e70" + "0" * 60, "current": True})
        return rc

    return deployer


def test_rotate_restic_key_lets_the_deploy_add_the_key_then_removes_the_old(
    deployed: Path, tmp_path: Path
) -> None:
    ctx, out = fresh(deployed, tmp_path)
    calls: list[str] = []
    assert rotate.run(ctx, "restic-key", _rotating_deployer(ctx, calls)) == 0, out.getvalue()
    tools = ctx.deps.tools  # type: ignore[attr-defined]
    assert tools.stdin_seen == []  # mcctl never handles the password: the deploy adds the key on the server
    scripts = ssh_scripts(ctx)
    assert any("key remove 01d0" in s for s in scripts) and calls == ["deploy"]
    assert ctx.deps.tf_log[-2:] == ["plan -replace=random_password.restic", "apply"]  # type: ignore[attr-defined]
    assert "mock-restic-password" not in out.getvalue()
    assert ctx.state.get("rotate-restic")["status"] == "done"


def test_rotate_restic_key_resumes_after_the_apply_whoever_deploys(deployed: Path, tmp_path: Path) -> None:
    ctx, _ = fresh(deployed, tmp_path)
    calls: list[str] = []
    assert rotate.run(ctx, "restic-key", _rotating_deployer(ctx, calls, rc=1)) == 1  # the deploy fails
    assert ctx.state.get("rotate-restic")["outputs"]["phase"] == "applied"
    applies = ctx.deps.tf_log.count("apply")  # type: ignore[attr-defined]
    _rotating_deployer(ctx, [])(
        configrepo.load(ctx.root), ctx.root
    )  # meanwhile CI deployed (and added the key)
    with_ui(ctx)
    assert rotate.run(ctx, "restic-key", _rotating_deployer(ctx, calls)) == 0
    assert ctx.deps.tf_log.count("apply") == applies  # type: ignore[attr-defined]  (no second -replace)
    assert any("key remove 01d0" in s for s in ssh_scripts(ctx))


def test_rotate_restic_key_refuses_to_remove_the_key_still_in_use(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    assert rotate.run(ctx, "restic-key", lambda repo, root: 0) == 1  # the server still uses the old key
    assert "still opens the repository with the old key" in out.getvalue()
    assert not any("key remove" in s for s in ssh_scripts(ctx))


def test_restic_rekey_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """scripts/restic_rekey.py against a fake restic: which environment opens the repository decides."""
    import importlib.util

    spec = importlib.util.spec_from_file_location("restic_rekey", REPO / "scripts" / "restic_rekey.py")
    assert spec and spec.loader
    rekey = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(rekey)
    keys = {"old"}
    seen: list[tuple[tuple[str, ...], str | None]] = []

    def fake(env: dict[str, str], *args: str, stdin: str | None = None) -> int:
        seen.append((args, stdin))
        if env.get("AWS_ACCESS_KEY_ID") == "dead":
            return 1
        if args[:2] == ("key", "add"):
            assert env["RESTIC_PASSWORD"] in keys and stdin
            keys.add(stdin.strip())
            return 0
        return 0 if env["RESTIC_PASSWORD"] in keys else 12

    monkeypatch.setattr(rekey, "restic", fake)
    text = "# managed\nRESTIC_REPOSITORY=/r\nRESTIC_PASSWORD=new\nAWS_ACCESS_KEY_ID=k2\n"
    new = rekey.parse_env(text)
    assert new == {"RESTIC_REPOSITORY": "/r", "RESTIC_PASSWORD": "new", "AWS_ACCESS_KEY_ID": "k2"}
    current = {"RESTIC_REPOSITORY": "/r", "RESTIC_PASSWORD": "old", "AWS_ACCESS_KEY_ID": "k1"}
    assert rekey.rekey(current, new, {"PATH": "/usr/bin"}) == "KEY_ADDED" and keys == {"old", "new"}
    assert all("new" not in a for a, _ in seen)  # the password only ever on stdin, never an argument
    assert rekey.rekey(current, new, {}) == "NEW_OK"  # a second deploy: nothing to do
    with pytest.raises(SystemExit, match="neither"):
        rekey.rekey(current | {"AWS_ACCESS_KEY_ID": "dead"}, new | {"RESTIC_PASSWORD": "other"}, {})
    with pytest.raises(SystemExit, match="no current one"):
        rekey.rekey({}, new | {"RESTIC_PASSWORD": "other"}, {})
    # a rotated R2 token: the new environment is used whole
    env = rekey.restic_env({"PATH": "/bin", "RESTIC_PASSWORD": "old", "AWS_ACCESS_KEY_ID": "k1"}, new)
    assert env == {
        "PATH": "/bin",
        "RESTIC_REPOSITORY": "/r",
        "RESTIC_PASSWORD": "new",
        "AWS_ACCESS_KEY_ID": "k2",
    }


# --- players --------------------------------------------------------------------------------------------


def test_players_add_op_deop_remove(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    path = ctx.root / "players.yml"
    assert players.change(ctx, "add", "NewPal", route="commit") == 0
    repo = configrepo.load(ctx.root)  # canonical (validate requires the exact dump)
    assert "NewPal" in [p.name for p in repo.players.whitelist]
    assert git(ctx.root, "log", "-1", "--format=%s").strip() == "mcctl players add NewPal"
    assert players.change(ctx, "add", "newpal", route="commit") == 0 and "nothing to do" in out.getvalue()
    assert players.change(ctx, "op", "NewPal", level=2, route="commit") == 0
    assert ("NewPal", 2) in [(o.name, o.level) for o in configrepo.load(ctx.root).players.ops]
    assert players.change(ctx, "deop", "NewPal", route="commit") == 0
    assert "NewPal" not in [o.name for o in configrepo.load(ctx.root).players.ops]
    assert (
        players.change(ctx, "op", "Fresh", uuid="00000000-0000-4000-8000-0000000000f1", route="push") == 0
    )  # pushes
    assert players.change(ctx, "remove", "Fresh", route="commit") == 0
    names = [p.name for p in configrepo.load(ctx.root).players.whitelist]
    assert "Fresh" not in names and "Fresh" not in path.read_text()
    with pytest.raises(init.StepError, match="UUID"):
        players.change(ctx, "add", "Bad", uuid="nope")
    assert players.change(ctx, "list", "") == 0 and "E2EAdmin" in out.getvalue()


# --- restore --------------------------------------------------------------------------------------------


def test_restore_list_live_and_to(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    assert restorecmd.list_snapshots(ctx) == 0 and "a1b2c3d4" in out.getvalue()
    assert restorecmd.live(ctx, "a1b2c3d4", dry_run=True) == 0 and "safety snapshot" in out.getvalue()
    assert restorecmd.live(ctx, "a1b2c3d4") == 0, out.getvalue()
    scripts = [c[-1] for c in ctx.deps.tools.calls if c[0] == "ssh"]  # type: ignore[attr-defined]
    assert any("sudo systemctl start mc-restore@a1b2c3d40000.service" in s for s in scripts)
    assert restorecmd.live(ctx, "ffffffff") == 1  # unknown snapshot
    assert restorecmd.live(ctx, "NOT-HEX") == 2
    wrong, _ = fresh(deployed, tmp_path / "w", P12_ANSWERS | {"confirm_restore": "restore 00000000"})
    calls = len(wrong.deps.tools.calls)  # type: ignore[attr-defined]
    assert restorecmd.live(wrong, "a1b2c3d4") == 1
    assert restorecmd.live(wrong, "a1b2c3d4", confirm="restore 0000") == 1  # --confirm: compared the same way
    assert not any("mc-restore@" in c[-1] for c in wrong.deps.tools.calls[calls:])  # type: ignore[attr-defined]
    assert restorecmd.to_dir(ctx, "/opt/minecraft/server", None) == 2  # never the live dir
    assert restorecmd.to_dir(ctx, "/var/tmp/check", None) == 0  # noqa: S108 - a path on the (fake) server


def test_restore_list_marks_the_deploys_pre_upgrade_snapshot(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    ctx.deps.tools.pre_upgrade = {  # type: ignore[attr-defined]
        "snapshot_id": "a1b2c3d4" + "0" * 56,
        "from": "26.3",
        "to": "27.1",
        "time": "2026-11-01T10:00:00Z",
    }
    assert restorecmd.list_snapshots(ctx) == 0
    assert "a1b2c3d4  2026-10-31T11:00:00Z  auto  <- pre-upgrade" in out.getvalue()
    assert "Minecraft 26.3 -> 27.1" in out.getvalue()


def test_serverops_scripts() -> None:
    assert "--verify-into /var/tmp/x" in serverops.restore_to_script(None, "/var/tmp/x")  # noqa: S108
    with pytest.raises(ValueError):
        serverops.restore_to_script(None, "/var/tmp/../opt")  # noqa: S108
    with pytest.raises(ValueError):
        serverops.restore_live_script("abc; rm -rf /")
    text = "@@mcctl:keys\n" + json.dumps(
        [{"id": "a" * 64, "current": False}, {"id": "b" * 64, "current": True}]
    )
    assert serverops.current_key(text) == "b" * 64 and serverops.key_ids(text) == ["a" * 64, "b" * 64]
    snaps = [{"id": "c" * 64, "time": "2026-01-02"}, {"id": "d" * 64, "time": "2026-01-01"}]
    assert serverops.latest("@@mcctl:snapshots\n" + json.dumps(snaps))["id"] == "c" * 64
    with pytest.raises(ValueError):
        serverops.restore_to_script(None, "/home/ed/x")  # mc-run has ProtectHome=yes
    with pytest.raises(ValueError):
        serverops.restore_to_script(None, "/srv/x")  # the ops user can't create it
    pre = "@@mcctl:preupgrade\n" + json.dumps({"snapshot_id": "c" * 64, "from": "26.3", "to": "27.1"})
    assert serverops.parse_pre_upgrade(pre)["to"] == "27.1"
    assert serverops.parse_pre_upgrade("@@mcctl:preupgrade\n") is None


# --- upgrade (product) ----------------------------------------------------------------------------------


def test_product_upgrade_opens_a_pr_with_the_target_releases_migrations(
    deployed: Path, tmp_path: Path
) -> None:
    ctx, out = fresh(deployed, tmp_path)
    ctx.deps.transport = Replay("github_release_latest.json")
    ran: list[tuple[str, list[str]]] = []

    def runner(pin: configrepo.Pin, args: list[str]) -> int:
        ran.append((pin.sha, list(args)))
        return 0

    start = git(ctx.root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    assert upgrade_mod.product_upgrade(ctx, None, None, runner=runner) == 0, out.getvalue()
    sha = "c" * 40
    assert [a[0] for _, a in ran] == ["schema-migrate", "validate"] and all(s == sha for s, _ in ran)
    assert git(ctx.root, "rev-parse", "--abbrev-ref", "HEAD").strip() == start
    wf = git(ctx.root, "show", "upgrade/product-v9.0.0:.github/workflows/server.yml")
    assert f"server.yml@{sha} # v9.0.0" in wf
    gh = [c for c in ctx.deps.tools.calls if c[:3] == ["gh", "pr", "create"]]  # type: ignore[attr-defined]
    assert gh and "upgrade/product-v9.0.0" in gh[0]
    assert "mock-gh-oauth-token" not in out.getvalue()
    assert all(
        r.headers.get("Authorization") == "Bearer mock-gh-oauth-token" for r in ctx.deps.transport.requests
    )


def test_product_upgrade_routes_only_a_pure_patch_bump_through_the_gate(
    deployed: Path, tmp_path: Path
) -> None:
    """P12 review (M2): `server / gate` auto-merges a product bump only from update/* and only if the workflow's
    `uses:` line is all that changed: a pure patch bump goes there (auto-merge on); anything else, or one whose
    schema migration touched server.yml, stays on upgrade/* for the admin."""
    ctx, out = fresh(deployed, tmp_path)
    pin = configrepo.load(ctx.root).pin
    major, minor, patch = (int(x) for x in str(pin.version).lstrip("v").split("."))
    to = f"v{major}.{minor}.{patch + 1}"
    assert upgrade_mod.product_upgrade(ctx, to, "d" * 40, runner=lambda p, a: 0) == 0, out.getvalue()
    gh = [c for c in ctx.deps.tools.calls if c[:2] == ["gh", "pr"]]  # type: ignore[attr-defined]
    assert f"update/product-{to}" in gh[0] and gh[1][:3] == ["gh", "pr", "merge"] and "--auto" in gh[1]
    changed = git(ctx.root, "diff", "--name-only", f"main...update/product-{to}").split()
    assert changed == [".github/workflows/server.yml"]  # what the gate's product policy accepts

    ctx2, out2 = fresh(deployed, tmp_path / "m")

    def migrating(p: configrepo.Pin, args: list[str]) -> int:
        if args[0] == "schema-migrate":
            path = ctx2.root / "server.yml"
            path.write_text(path.read_text() + "# migrated\n")
        return 0

    assert upgrade_mod.product_upgrade(ctx2, to, "d" * 40, runner=migrating) == 0, out2.getvalue()
    gh2 = [c for c in ctx2.deps.tools.calls if c[:2] == ["gh", "pr"]]  # type: ignore[attr-defined]
    assert len(gh2) == 1 and f"upgrade/product-{to}" in gh2[0]  # no auto-merge: the admin merges
    assert "Needs the admin" in " ".join(gh2[0])


def test_product_upgrade_refuses_downgrades_and_rolls_back_a_failed_migration(
    deployed: Path, tmp_path: Path
) -> None:
    ctx, out = fresh(deployed, tmp_path)
    with pytest.raises(init.StepError, match="no downgrades"):
        upgrade_mod.product_upgrade(ctx, "v0.0.1", "a" * 40, runner=lambda p, a: 0)
    head = git(ctx.root, "rev-parse", "HEAD")
    with pytest.raises(init.StepError, match="schema migration failed"):
        upgrade_mod.product_upgrade(ctx, "v9.9.9", "b" * 40, runner=lambda p, a: 1)
    assert git(ctx.root, "rev-parse", "HEAD") == head and "upgrade/" not in git(ctx.root, "branch")
    assert git(ctx.root, "status", "--porcelain") == ""
    assert upgrade_mod.product_upgrade(ctx, "v9.9.9", "b" * 40, dry_run=True) == 0
    assert "schema-migrate --from" in out.getvalue()


def test_schema_migrations_runner() -> None:
    reg = (
        migrations.Migration("1.2.0", "rename a", lambda t: t.replace("old_key:", "new_key:")),
        migrations.Migration("1.1.0", "noop", lambda t: t),
        migrations.Migration("2.0.0", "future", lambda t: t + "x"),
    )
    assert [m.version for m in migrations.pending("1.0.0", "1.5.0", reg)] == ["1.1.0", "1.2.0"]
    text, applied = migrations.migrate("old_key: 1\n", "1.0.0", "1.5.0", reg)
    assert text == "new_key: 1\n" and applied == ["1.2.0: rename a"]
    assert migrations.migrate(text, "1.0.0", "1.5.0", reg)[1] == []  # idempotent
    assert [m.version for m in migrations.MIGRATIONS] == ["1.2.0"]


GLYPH_BEFORE = """name: demo   # {wine} outside branding stays
branding:
  # comment: {wine}
  rules:
    - Be kind.
    - "Have fun \\U0001F377"
    - 'Cheers \U0001f377'
  join:
    first_join: "Welcome, {player}! {wine} Stay a while"
  drink: {enabled: true, name: "{wine}"}
client: {enabled: false}   # {wine}
"""
GLYPH_AFTER = """name: demo   # {wine} outside branding stays
branding:
  # comment: {glyph}
  rules:
    - Be kind.
    - "Have fun {glyph}"
    - 'Cheers {glyph}'
  join:
    first_join: "Welcome, {player}! {glyph} Stay a while"
  drink: {enabled: true, name: "{glyph}"}
client: {enabled: false}   # {wine}
"""


def test_migration_1_2_0_glyph_placeholder() -> None:
    assert "\U0001f377" in GLYPH_BEFORE and "\\U0001F377" in GLYPH_BEFORE  # the emoji and its YAML escape
    text, applied = migrations.migrate(GLYPH_BEFORE, "1.1.0", "1.2.0")
    assert text == GLYPH_AFTER
    assert applied == ["1.2.0: branding texts: {wine} and the wine emoji -> {glyph}"]
    assert migrations.migrate(text, "1.1.0", "1.2.0") == (text, [])  # idempotent
    assert migrations.migrate(GLYPH_BEFORE, "1.2.0", "1.2.0")[1] == []  # already on 1.2.0: nothing runs
    flow = 'name: demo\nbranding: {rules: ["fun {wine}"]}\n'
    assert migrations.glyph_placeholder(flow) == 'name: demo\nbranding: {rules: ["fun {glyph}"]}\n'
    data = yaml.safe_load(text)
    assert data["branding"]["rules"][1:] == ["Have fun {glyph}", "Cheers {glyph}"]


def test_schema_migrate_cli_is_a_noop_today(
    deployed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ctx, _ = fresh(deployed, tmp_path)
    before = (ctx.root / "server.yml").read_text()
    assert cli.main(["schema-migrate", "-C", str(ctx.root), "--from", "0.1.0"]) == 0
    assert (ctx.root / "server.yml").read_text() == before


# --- upgrade (Minecraft) / lock update ------------------------------------------------------------------


def test_minecraft_upgrade_refusals(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    with pytest.raises(init.StepError, match="downgraded"):
        upgrade_mod.minecraft_upgrade(ctx, "1.21.4")
    with pytest.raises(init.StepError, match="no config keys"):
        upgrade_mod.minecraft_upgrade(ctx, "27.1")
    product = tmp_path / "product"
    (product / "server-config" / "keys" / "27.1").mkdir(parents=True)
    ctx.product_root = product
    assert upgrade_mod.minecraft_upgrade(ctx, "27.1", dry_run=True) == 0
    text = out.getvalue()
    assert "typed confirmation" in text and "pre-upgrade snapshot" in text and "rollback" in text
    assert "right before the new jar starts" in text  # the deploy's backup, not one now
    wrong, _ = fresh(deployed, tmp_path / "w", P12_ANSWERS | {"confirm_minecraft_version": "26.3"})
    wrong.product_root = product
    head = git(wrong.root, "rev-parse", "HEAD")
    assert upgrade_mod.minecraft_upgrade(wrong, "27.1", locker=_no_locker) == 1  # typed answer didn't match
    assert git(wrong.root, "rev-parse", "HEAD") == head


def _no_locker(*_a: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError("the lock tool must not run")


def test_minecraft_upgrade_blocked_by_a_plugin_leaves_nothing_behind(deployed: Path, tmp_path: Path) -> None:
    ctx, _ = fresh(deployed, tmp_path)
    product = tmp_path / "product"
    (product / "server-config" / "keys" / "27.1").mkdir(parents=True)
    ctx.product_root = product
    head = git(ctx.root, "rev-parse", "HEAD")

    def locker(
        prod: Path, cfg: Path, version: str, paper: bool, client: bool
    ) -> subprocess.CompletedProcess[str]:
        assert (prod, version, paper, client) == (product, "27.1", False, False)
        return subprocess.CompletedProcess([], 1, "", "LuckPerms has no release for 27.1")

    with pytest.raises(init.StepError, match=r"no release for 27\.1"):
        upgrade_mod.minecraft_upgrade(ctx, "27.1", locker=locker)
    assert not any("mc-backup.service" in s for s in ssh_scripts(ctx))  # the deploy backs up, not mcctl
    assert git(ctx.root, "rev-parse", "HEAD") == head and "upgrade/" not in git(ctx.root, "branch")
    assert configrepo.load(ctx.root).server.minecraft.version == "26.3"


def test_lock_update_same_version_with_nothing_new_is_a_noop(deployed: Path, tmp_path: Path) -> None:
    ctx, out = fresh(deployed, tmp_path)
    ok = subprocess.CompletedProcess([], 0, "server: purpur 26.3 build 1 (java 25)\n", "")
    assert upgrade_mod.minecraft_upgrade(ctx, None, locker=lambda *a: ok) == 0
    assert "nothing to do" in out.getvalue() and "update/lock-" not in git(ctx.root, "branch")


def test_lock_update_client_creates_and_commits_a_first_client_lock(deployed: Path, tmp_path: Path) -> None:
    # v1.2 rollout bug: the client lock was never created while client.enabled was false, and a refreshed one
    # was left out of the lock PR's commit.
    ctx, out = fresh(deployed, tmp_path)
    git(
        ctx.root, "rm", "-q", configrepo.CLIENT_LOCK
    )  # a config repo from before v1.1 has none (the first deployment's)
    git(ctx.root, "-c", "user.name=t", "-c", "user.email=t@t.test", "commit", "-qm", "pre-v1.1 repo")
    seen: list[bool] = []

    def locker(
        prod: Path, cfg: Path, version: str, paper: bool, client: bool
    ) -> subprocess.CompletedProcess[str]:
        seen.append(client)
        if client:
            shutil.copy(REPO / "template" / configrepo.CLIENT_LOCK, cfg / configrepo.CLIENT_LOCK)
        return subprocess.CompletedProcess([], 0, "client: sodium 1.0\n", "")

    assert upgrade_mod.minecraft_upgrade(ctx, None, dry_run=True, client=True, locker=locker) == 0
    assert "first client.lock.yml" in out.getvalue() and seen == []
    assert upgrade_mod.minecraft_upgrade(ctx, None, client=True, locker=locker, today=None) == 0, (
        out.getvalue()
    )
    assert seen == [True]
    branch = next(b.strip(" *") for b in git(ctx.root, "branch").splitlines() if "update/lock-" in b)
    files = git(ctx.root, "show", "--name-only", "--format=", branch).split()
    assert configrepo.CLIENT_LOCK in files
    gh = [c for c in ctx.deps.tools.calls if c[:3] == ["gh", "pr", "create"]]  # type: ignore[attr-defined]
    assert gh and any("first client.lock.yml" in a for a in gh[-1])


def test_cli_wiring(deployed: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    ctx, _ = fresh(deployed, tmp_path)
    assert cli.main(["migrate", "-C", str(ctx.root)]) == 2  # neither --to nor --rollback
    assert cli.main(["upgrade", "-C", str(ctx.root), "--minecraft-version", "27.1", "--to", "v1.0.0"]) == 2
    assert cli.main(["players", "-C", str(ctx.root), "add"]) == 2
    with pytest.raises(SystemExit):
        cli.main(["secrets", "rotate", "-C", str(ctx.root), "github-admin"])  # not a generated secret
