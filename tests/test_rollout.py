"""v1.5: `mcctl rollout` (sync, pin PR, checks and merge, deploy, render/plan/apply). Real git in temporary
repositories (a bare `origin`); gh, the GitHub API (recorded responses), the status probe, the pinned product's
commands, the clock and sleep are fakes: nothing real is contacted, merged, deployed or applied."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import Replay, make_config_repo

from mcctl import cli, configrepo, init, publish, remote, rollout, schema, updates, upgrade
from mcctl.mock import GIT_IDENTITY
from mcctl.ui import UI

NEW_SHA = "c" * 40  # tests/fixtures/http/github_release_latest.json: v9.0.0


def git(root: Path, *args: str) -> str:
    proc = subprocess.run(["git", *GIT_IDENTITY, "-C", str(root), *args], capture_output=True, text=True)
    assert proc.returncode == 0, f"git {args}: {proc.stderr}"
    return proc.stdout.strip()


def proc(argv: Sequence[str], rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, out, err)


class Fake:
    """Real git; a scripted gh. `pr merge --admin` really merges into origin/main; with `gate_merges` the first
    state poll after the checks merges the PR (the gate's auto-merge)."""

    def __init__(self, origin: Path, tmp: Path) -> None:
        self.origin, self.tmp = origin, tmp
        self.calls: list[list[str]] = []
        self.streamed: list[list[str]] = []
        self.open: dict[str, str] = {}  # branch -> url of an open PR
        self.urls: dict[str, str] = {}  # url -> branch
        self.state = "OPEN"
        self.gate_merges = False
        self.checked = False
        self.merged = ""

    def gh(self, *prefix: str) -> list[list[str]]:
        return [c[1:] for c in self.calls if c[0] == "gh" and c[1 : 1 + len(prefix)] == list(prefix)]

    def _merge(self, branch: str) -> None:
        work = self.tmp / "merger"
        if not work.exists():
            subprocess.run(["git", "clone", "-q", str(self.origin), str(work)], check=True)
        git(work, "fetch", "-q", "origin")
        git(work, "checkout", "-q", "main")
        git(work, "reset", "-q", "--hard", "origin/main")
        git(work, "merge", "-q", "--no-ff", "-m", f"Merge {branch}", f"origin/{branch}")
        git(work, "push", "-q", "origin", "main")
        self.merged, self.state = git(work, "rev-parse", "HEAD"), "MERGED"

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "git":
            return subprocess.run(["git", *GIT_IDENTITY, *argv[1:]], capture_output=True, text=True)
        args = argv[1:]
        if args == ["auth", "token"]:
            return proc(argv, 0, "gho_fake\n")
        if args[:2] == ["pr", "view"] and "url,state" in args:
            url = self.open.get(args[2])
            return (
                proc(argv, 0, json.dumps({"url": url, "state": "OPEN"})) if url else proc(argv, 1, "", "none")
            )
        if args[:2] == ["pr", "create"]:
            branch = args[args.index("--head") + 1]
            url = f"https://github.com/mcctl-tests/cfg/pull/{len(self.urls) + 1}"
            self.urls[url] = branch
            return proc(argv, 0, url + "\n")
        if args[:2] == ["pr", "checks"]:
            self.checked = True
            items = [{"name": "server / validate", "bucket": "pass"},
                     {"name": "server / gate", "bucket": "pass" if self.gate_merges else "fail"}]  # fmt: skip
            return proc(argv, 0, json.dumps(items))
        if args[:2] == ["pr", "view"] and args[-1] == "state":
            if self.gate_merges and self.checked and self.state == "OPEN":
                self._merge(self.urls[args[2]])
            return proc(argv, 0, json.dumps({"state": self.state}))
        if args[:2] == ["pr", "merge"]:
            if "--admin" in args:
                self._merge(self.urls[args[2]])
            return proc(argv)
        if args[:2] == ["pr", "view"] and "mergeCommit,state" in args:
            return proc(argv, 0, json.dumps({"state": self.state, "mergeCommit": {"oid": self.merged}}))
        if args[:2] == ["run", "list"]:
            run = {
                "databaseId": 7,
                "status": "completed",
                "conclusion": "success",
                "event": "push",
                "url": "u",
            }
            return proc(argv, 0, json.dumps([run]))
        if args[:2] == ["run", "view"]:
            jobs = [{"name": "server / deploy", "conclusion": "success"}]
            return proc(argv, 0, json.dumps({"conclusion": "success", "jobs": jobs}))
        return proc(argv, 1, "", f"fake gh: unexpected {args[:2]}")

    def stream(self, argv: Sequence[str]) -> int:
        self.streamed.append(list(argv))
        return 0


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += timedelta(seconds=s)


class Product:
    """The pinned product's commands: records (pin sha, argv)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    def __call__(self, pin: configrepo.Pin, args: Sequence[str]) -> int:
        self.calls.append((pin.sha, list(args)))
        return 0

    def commands(self) -> list[str]:
        return [a[0] for _, a in self.calls]


@pytest.fixture
def env(tmp_path: Path) -> dict[str, Any]:
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    root = make_config_repo(tmp_path / "cfg", renderable=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    git(root, "remote", "add", "origin", str(origin))
    git(root, "push", "-q", "-u", "origin", "main")
    return {"root": root, "origin": origin, "tmp": tmp_path}


def make(
    env: dict[str, Any], answers: dict[str, Any]
) -> tuple[init.Ctx, Fake, publish.Tools, Product, io.StringIO]:
    fake = Fake(env["origin"], env["tmp"])
    clock = Clock()
    out = io.StringIO()
    deps = init.Deps(
        run=fake,
        stream=fake.stream,
        which=lambda n: f"/usr/bin/{n}",
        now=clock.now,
        sleep=clock.sleep,
        transport=Replay("github_release_latest.json"),
    )
    ctx = init.Ctx(root=env["root"], ui=UI(answers=answers, out=out), deps=deps, opts=init.Options())
    product = Product()
    online = lambda cfg: remote.Status(reachable=True, server="active", players_online=0, players_max=20)  # noqa: E731
    return ctx, fake, publish.Tools(online=online, product=product), product, out


def pinned(root: Path, ref: str = "HEAD") -> str:
    return git(root, "show", f"{ref}:{configrepo.WORKFLOW}")


def test_dry_run_prints_the_steps_and_changes_nothing(env: dict[str, Any]) -> None:
    ctx, fake, tools, product, out = make(env, {})
    head = git(env["root"], "rev-parse", "HEAD")
    assert rollout.run(ctx, dry_run=True, tools=tools) == 0
    text = out.getvalue()
    assert "v0.0.0 -> v9.0.0" in text and "mcctl upgrade --to v9.0.0" in text and "dry run" in text
    assert "`mcctl apply` after a yes" in text
    assert git(env["root"], "rev-parse", "HEAD") == head and product.calls == []
    assert [c for c in fake.calls if c[0] == "git"] == [] and fake.gh() == [["auth", "token"]]
    # the token went to the API in a header, never on a command line or in the output
    assert "gho_fake" not in text
    assert all(r.headers.get("Authorization") == "Bearer gho_fake" for r in ctx.deps.transport.requests)


def test_a_minor_release_end_to_end(env: dict[str, Any]) -> None:
    """Latest release v9.0.0: upgrade PR, checks, the admin's typed merge, the deploy, then the new pin's render,
    plan and apply (apply's own typed confirmation is inside the pinned product's `apply`)."""
    root = env["root"]
    ctx, fake, tools, product, out = make(env, {"confirm_rollout_merge": "v9.0.0", "rollout_apply": True})
    assert rollout.run(ctx, tools=tools) == 0, out.getvalue()
    text = out.getvalue()
    (create,) = fake.gh("pr", "create")
    assert create[create.index("--head") + 1] == "upgrade/product-v9.0.0"
    assert fake.gh("pr", "merge") == [
        ["pr", "merge", "https://github.com/mcctl-tests/cfg/pull/1", "--merge", "--admin"]
    ]
    assert fake.streamed and fake.streamed[0][:3] == ["gh", "run", "watch"]
    assert "deployed" in text
    # main was pulled: the merge is local, the pin moved, and Terraform ran from the new pin
    assert git(root, "rev-parse", "HEAD") == fake.merged
    assert f"server.yml@{NEW_SHA} # v9.0.0" in pinned(root)
    assert product.commands() == ["schema-migrate", "validate", "render", "plan", "apply"]
    assert all(sha == NEW_SHA for sha, _ in product.calls)
    assert product.calls[-1][1] == ["apply", "-C", str(root)]  # no --yes, ever
    assert "gho_fake" not in text


def test_no_typed_merge_no_merge_no_terraform(env: dict[str, Any]) -> None:
    ctx, fake, tools, product, out = make(env, {"confirm_rollout_merge": "yes"})
    assert rollout.run(ctx, tools=tools) == 1
    assert "not merged (not confirmed)" in out.getvalue()
    assert fake.gh("pr", "merge") == [] and fake.merged == ""
    assert product.commands() == ["schema-migrate", "validate"]


def test_a_patch_pr_the_gate_merges_is_reported_and_apply_can_wait(env: dict[str, Any]) -> None:
    """The weekly updater's open `update/product-v0.0.1`: used as it is, merged by the gate, no admin merge."""
    root, origin = env["root"], env["origin"]
    git(root, "switch", "-q", "-c", "update/product-v0.0.1")
    wf = root / configrepo.WORKFLOW
    wf.write_text(updates.bump_pin(wf.read_text(), schema_repo(root), "d" * 40, "v0.0.1"))
    git(root, "commit", "-qam", "updater: product v0.0.1")
    git(root, "push", "-q", "origin", "update/product-v0.0.1")
    git(root, "switch", "-q", "main")
    ctx, fake, tools, product, out = make(env, {"rollout_apply": False})
    url = "https://github.com/mcctl-tests/cfg/pull/9"
    fake.open["update/product-v0.0.1"], fake.urls[url] = url, "update/product-v0.0.1"
    fake.gate_merges = True
    assert rollout.run(ctx, "v0.0.1", tools=tools) == 0, out.getvalue()
    text = out.getvalue()
    assert "rolls out by itself" in text and f"using the open pull request {url}" in text
    assert "merged by `server / gate` (auto-merge)" in text
    assert fake.gh("pr", "create") == [] and fake.gh("pr", "merge") == []
    assert product.commands() == ["render", "plan"] and "not applied" in text
    assert "# v0.0.1" in pinned(root) and git(root, "rev-parse", "HEAD") == git(origin, "rev-parse", "main")


def schema_repo(root: Path) -> str:
    return configrepo.load(root).pin.repository


def test_already_pinned_fast_forwards_then_terraform(env: dict[str, Any]) -> None:
    """The empty-plan failure: the pin moved on GitHub, local main is behind. rollout pulls before render."""
    root, origin, tmp = env["root"], env["origin"], env["tmp"]
    other = tmp / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    wf = other / configrepo.WORKFLOW
    wf.write_text(updates.bump_pin(wf.read_text(), schema_repo(root), NEW_SHA, "v9.0.0"))
    git(other, "commit", "-qam", "pin v9.0.0 (merged on GitHub)")
    git(other, "push", "-q", "origin", "main")
    ctx, fake, tools, product, out = make(env, {"rollout_apply": True})
    assert rollout.run(ctx, "v9.0.0", tools=tools) == 0, out.getvalue()
    assert "already pinned to v9.0.0" in out.getvalue() and "fast-forwarded" in out.getvalue()
    assert product.commands() == ["render", "plan", "apply"] and {s for s, _ in product.calls} == {NEW_SHA}
    assert fake.gh("pr") == []


def test_sync_refuses_dirty_branch_ahead_and_diverged(env: dict[str, Any]) -> None:
    root, origin, tmp = env["root"], env["origin"], env["tmp"]
    ctx, _, tools, product, _ = make(env, {})
    (root / "notes.txt").write_text("x\n")
    with pytest.raises(init.StepError, match="uncommitted changes"):
        rollout.run(ctx, "v9.0.0", tools=tools)
    (root / "notes.txt").unlink()
    git(root, "switch", "-q", "-c", "feature")
    with pytest.raises(init.StepError, match="on feature, not main") as e:
        rollout.sync(ctx)
    assert e.value.fix == "`git switch main`"
    git(root, "switch", "-q", "main")
    git(root, "commit", "-q", "--allow-empty", "-m", "local only")
    with pytest.raises(init.StepError, match="commits origin/main doesn't"):
        rollout.sync(ctx)
    other = tmp / "other"
    subprocess.run(["git", "clone", "-q", str(origin), str(other)], check=True)
    git(other, "commit", "-q", "--allow-empty", "-m", "merged on GitHub")
    git(other, "push", "-q", "origin", "main")
    with pytest.raises(init.StepError, match="diverged") as e:
        rollout.sync(ctx)
    assert "git reset --hard origin/main" in e.value.fix
    assert product.calls == []


def test_refuses_downgrades_and_bad_tags(env: dict[str, Any]) -> None:
    ctx, _, tools, _, _ = make(env, {})
    with pytest.raises(init.StepError, match=r"vX\.Y\.Z"):
        rollout.run(ctx, "9.0.0", tools=tools)
    wf = env["root"] / configrepo.WORKFLOW
    wf.write_text(updates.bump_pin(wf.read_text(), schema_repo(env["root"]), NEW_SHA, "v2.0.0"))
    git(env["root"], "commit", "-qam", "pin v2.0.0")
    git(env["root"], "push", "-q", "origin", "main")
    with pytest.raises(init.StepError, match="no downgrades"):
        rollout.run(ctx, "v1.0.0", tools=tools)


def test_cli_and_dashboard() -> None:
    from mcctl import dashboard

    args = cli.build_parser().parse_args(["rollout", "--to", "v1.5.0", "--dry-run"])
    assert args.func is cli.cmd_rollout and args.to == "v1.5.0" and args.dry_run
    keys = {a.key: a for a in dashboard.ACTIONS}
    assert (
        "rollout --dry-run" in keys["rollout-preview"].help and "mcctl rollout" in keys["term-rollout"].help
    )
    assert schema.PRODUCT_ROOT.is_dir()


def test_upgrade_uses_the_updaters_pr_when_its_branch_exists(env: dict[str, Any]) -> None:
    """v1.5 (seen live): `mcctl upgrade` for a patch pushes update/product-vX.Y.Z, which the updater (weekly,
    or the Actions product-update run) had pushed first: the push is rejected, and its open PR is used."""
    root = env["root"]
    git(root, "switch", "-q", "-c", "update/product-v0.0.1")
    wf = root / configrepo.WORKFLOW
    wf.write_text(updates.bump_pin(wf.read_text(), schema_repo(root), "d" * 40, "v0.0.1") + "# updater\n")
    git(root, "commit", "-qam", "updater: product v0.0.1")
    git(root, "push", "-q", "origin", "update/product-v0.0.1")
    git(root, "switch", "-q", "main")
    git(root, "branch", "-q", "-D", "update/product-v0.0.1")
    ctx, fake, _tools, product, out = make(env, {})
    url = "https://github.com/mcctl-tests/cfg/pull/4"
    fake.open["update/product-v0.0.1"] = url
    made = upgrade.product_upgrade_pr(ctx, "v0.0.1", "d" * 40, runner=product)
    assert made is not None and made.url == url and made.unattended, out.getvalue()
    assert "exists on GitHub with an open pull request: using it" in out.getvalue()
    assert fake.gh("pr", "create") == [] and fake.gh("pr", "merge") == [
        ["pr", "merge", url, "--auto", "--squash"]
    ]
    assert (
        git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main" and git(root, "status", "--porcelain") == ""
    )
    assert "update/product-v0.0.1" not in git(root, "branch")
    # no open PR on that branch: a clear refusal naming the fix
    del fake.open["update/product-v0.0.1"]
    with pytest.raises(init.StepError) as e:
        upgrade.product_upgrade_pr(ctx, "v0.0.1", "d" * 40, runner=product)
    assert "git push origin --delete update/product-v0.0.1" in e.value.fix
