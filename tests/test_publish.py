"""v1.3 Part B: `mcctl publish` (preview, consequences, routes, restart awareness, revert) and the players PR
route. Real git in temporary repositories (a bare `origin`, explicit identity); gh, the status probe, the
pinned product's `apply`/`deploy`, the clock and sleep are fakes: nothing real is contacted or deployed."""

from __future__ import annotations

import io
import json
import subprocess
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from conftest import REPO, make_config_repo

from mcctl import cli, configrepo, init, players, publish, remote, schema, yamledit
from mcctl.mock import GIT_IDENTITY
from mcctl.ui import UI

PR_URL = "https://github.com/mcctl-tests/tests-mc-config/pull/7"
IDX = publish.index(REPO)


def git(root: Path, *args: str, check: bool = True) -> str:
    proc = subprocess.run(["git", *GIT_IDENTITY, "-C", str(root), *args], capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise AssertionError(f"git {args}: {proc.stderr}")
    return proc.stdout.strip()


def proc(argv: Sequence[str], rc: int = 0, out: str = "", err: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(list(argv), rc, out, err)


class Fake:
    """Real git (identity added); a scripted gh whose `pr merge` really merges the branch into origin/main."""

    def __init__(self, root: Path, origin: Path, tmp: Path) -> None:
        self.root, self.origin, self.tmp = root, origin, tmp
        self.calls: list[list[str]] = []
        self.streamed: list[list[str]] = []
        self.checks: list[list[dict[str, Any]]] = [
            [
                {"name": "server / validate", "bucket": "pending"},
                {"name": "server / gate", "bucket": "pending"},
            ],
            [
                {"name": "server / validate", "bucket": "pass"},
                {"name": "server / gate", "bucket": "fail"},  # "needs the admin"
            ],
        ]
        self.head = ""
        self.merged = ""
        self.pr_create_rc = 0
        self.deploy_runs: list[dict[str, Any]] = []  # `gh run list --status success` (revert)
        self.deploy_jobs: dict[str, str] = {}  # run id -> deploy job conclusion

    def gh_calls(self, *prefix: str) -> list[list[str]]:
        return [c for c in self.calls if c[0] == "gh" and c[1 : 1 + len(prefix)] == list(prefix)]

    def __call__(self, argv: Sequence[str]) -> subprocess.CompletedProcess[str]:
        argv = list(argv)
        self.calls.append(argv)
        if argv[0] == "git":
            return subprocess.run(["git", *GIT_IDENTITY, *argv[1:]], capture_output=True, text=True)
        assert argv[0] == "gh", argv
        args = argv[1:]
        if args[:2] == ["pr", "view"] and "url,state" in args:
            return proc(argv, 1, "", "no pull requests found")
        if args[:2] == ["pr", "create"]:
            self.head = args[args.index("--head") + 1]
            return proc(argv, self.pr_create_rc, PR_URL + "\n", "" if not self.pr_create_rc else "boom")
        if args[:2] == ["pr", "checks"]:
            items = self.checks.pop(0) if len(self.checks) > 1 else self.checks[0]
            return proc(argv, 0, json.dumps(items))
        if args[:2] == ["pr", "merge"]:
            assert "--admin" in args and "--merge" in args
            work = self.tmp / "merger"
            if not work.exists():
                subprocess.run(["git", "clone", "-q", str(self.origin), str(work)], check=True)
            git(work, "fetch", "-q", "origin")
            git(work, "checkout", "-q", "main")
            git(work, "reset", "-q", "--hard", "origin/main")
            git(work, "merge", "-q", "--no-ff", "-m", f"Merge {self.head}", f"origin/{self.head}")
            git(work, "push", "-q", "origin", "main")
            self.merged = git(work, "rev-parse", "HEAD")
            return proc(argv)
        if args[:2] == ["pr", "view"] and "mergeCommit,state" in args:
            return proc(argv, 0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": self.merged}}))
        if args[:2] == ["run", "list"] and "--commit" in args:
            sha = args[args.index("--commit") + 1]
            run = {"databaseId": 99, "status": "completed", "conclusion": "success", "event": "push",
                   "url": "https://github.com/x/actions/runs/99", "headSha": sha}  # fmt: skip
            return proc(argv, 0, json.dumps([run]))
        if args[:2] == ["run", "list"] and "--status" in args:
            return proc(argv, 0, json.dumps(self.deploy_runs))
        if args[:2] == ["run", "view"]:
            conclusion = self.deploy_jobs.get(args[2], "success")
            jobs = [{"name": "server / validate", "conclusion": "success"},
                    {"name": "server / deploy", "conclusion": conclusion}]  # fmt: skip
            return proc(argv, 0, json.dumps({"conclusion": "success", "jobs": jobs}))
        return proc(argv, 1, "", f"fake gh: unexpected {args[:2]}")

    def stream(self, argv: Sequence[str]) -> int:
        self.streamed.append(list(argv))
        return 0


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 11, 7, 12, 0, 0, tzinfo=UTC)
        self.slept = 0.0

    def now(self) -> datetime:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept += s
        self.t += timedelta(seconds=s)


class Online:
    """The status probe: a list of player counts (None: unreachable), the last one repeated."""

    def __init__(self, counts: list[int | None]) -> None:
        self.counts = counts
        self.asked = 0

    def __call__(self, cfg: schema.ServerConfig) -> remote.Status:
        n = self.counts[min(self.asked, len(self.counts) - 1)]
        self.asked += 1
        if n is None:
            return remote.Status(reachable=False, error="ssh: timeout")
        names = ["Alex", "Sam", "Kim"][:n]
        return remote.Status(reachable=True, server="active", players_online=n, players_max=20, players=names)


class Product:
    def __init__(self, rc: int = 0) -> None:
        self.rc = rc
        self.calls: list[list[str]] = []

    def __call__(self, pin: configrepo.Pin, args: Sequence[str]) -> int:
        self.calls.append(list(args))
        return self.rc


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
    env: dict[str, Any],
    answers: dict[str, Any] | None = None,
    online: list[int | None] | None = None,
    product_rc: int = 0,
    gh: bool = True,
) -> tuple[init.Ctx, Fake, publish.Tools, io.StringIO]:
    fake = Fake(env["root"], env["origin"], env["tmp"])
    clock = Clock()
    out = io.StringIO()
    deps = init.Deps(
        run=fake,
        stream=fake.stream,
        which=lambda n: f"/usr/bin/{n}" if (gh or n != "gh") else None,
        now=clock.now,
        sleep=clock.sleep,
    )
    ctx = init.Ctx(root=env["root"], ui=UI(answers=answers or {}, out=out), deps=deps, opts=init.Options())
    tools = publish.Tools(online=Online(online or [0]), product=Product(product_rc), max_wait_minutes=10)
    return ctx, fake, tools, out


def edit(root: Path, values: dict[str, Any]) -> None:
    path = root / configrepo.SERVER_YML
    text = path.read_text()
    for k, v in values.items():
        assert not schema.field_errors(k, v), k
        text = yamledit.set_value(text, k, v)
    path.write_text(text)


def go(ctx: init.Ctx, tools: publish.Tools, **kw: Any) -> int:
    return publish.run(ctx, tools=tools, idx=IDX, **kw)


# --- preview / consequences -------------------------------------------------------------------------------


def test_dry_run_previews_a_restart_and_changes_nothing(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, fake, tools, out = make(env, online=[2])
    edit(root, {"performance.view_distance": 8})
    head = git(root, "rev-parse", "HEAD")
    assert go(ctx, tools, dry_run=True) == 0
    text = out.getvalue()
    assert "uncommitted: server.yml" in text and "server.yml: performance.view_distance" in text
    assert "Minecraft restart: yes" in text and "mc_view_distance (" in text
    assert "server.properties.yml" in text
    assert "Terraform: no change" in text and "players online now: 2 (Alex, Sam)" in text
    assert "valid (product" in text and "dry run" in text
    assert git(root, "rev-parse", "HEAD") == head and git(root, "status", "--porcelain") == "M server.yml"
    assert not fake.gh_calls() and not any("push" in c for c in fake.calls)
    assert git(root, "worktree", "list").count("\n") == 0  # the base worktree is gone


def test_consequences_players_only_no_restart_no_terraform(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, _, _, _ = make(env)
    data = configrepo.load(root).players
    new = schema.Players(
        whitelist=[*data.whitelist, schema.Player("NewPal", "00000000-0000-4000-8000-0000000000f1")],
        ops=[*data.ops, schema.Op("NewPal", "00000000-0000-4000-8000-0000000000f1", 2)],
    )
    (root / configrepo.PLAYERS_YML).write_text(schema.dump_players(new))
    c = publish.consequences(ctx, git(root, "rev-parse", "origin/main"), IDX)
    assert c.players == ["+ NewPal", "op NewPal (level 2)"]
    assert c.restart is False and c.terraform == [] and not c.backend
    assert set(c.roles) == {"players"} and c.overlays == []


def test_consequences_backup_timer_role_only_and_dns_ttl_terraform(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, _, _, _ = make(env)
    edit(root, {"backups.prune_schedule": "Sun 04:30", "dns.ttl": 600})
    c = publish.consequences(ctx, git(root, "rev-parse", "origin/main"), IDX)
    assert c.settings == ["backups.prune_schedule", "dns.ttl"]
    assert c.variables == ["mc_prune_schedule"] and "backups" in c.roles and c.restart is False
    assert c.terraform == ["dns_ttl"]


def test_consequences_lock_change_restarts(env: dict[str, Any]) -> None:
    old = publish.Rendered({}, schema.Players([], []), {"server_lock": {"a": 1}}, {}, "b", "i", {})
    new = publish.Rendered({}, schema.Players([], []), {"server_lock": {"a": 2}}, {}, "b", "i", {})
    c = publish.judge(old, new, IDX)
    assert c.restart is True and "server_lock" in c.restart_why[0]
    new.backend = "other"
    assert publish.judge(old, new, IDX).backend


def test_motd_also_changes_the_panel_title_in_terraform(env: dict[str, Any]) -> None:
    ctx, _, _, _ = make(env)
    edit(env["root"], {"minecraft.motd": "Fresh paint"})
    c = publish.consequences(ctx, git(env["root"], "rev-parse", "origin/main"), IDX)
    assert c.restart is True and any(w.startswith("mc_motd (server.properties.yml") for w in c.restart_why)
    assert "panel_title" in c.terraform  # no branding: the panel's heading is the MOTD


def test_heap_change_restarts_through_the_unit(env: dict[str, Any]) -> None:
    ctx, _, _, _ = make(env)
    edit(env["root"], {"jvm.heap_mb": 3072})
    c = publish.consequences(ctx, git(env["root"], "rev-parse", "origin/main"), IDX)
    assert c.restart is True and any(w.startswith("mc_heap_mb") for w in c.restart_why), c


def test_nothing_to_publish(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env)
    assert go(ctx, tools) == 0 and "nothing to publish" in out.getvalue()
    assert not fake.gh_calls("pr")


# --- refusals ---------------------------------------------------------------------------------------------


def test_refuses_an_invalid_config_before_anything_lands(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, fake, tools, out = make(env)
    (root / configrepo.SERVER_YML).write_text(
        (root / configrepo.SERVER_YML).read_text() + "\nnot_a_field: 1\n"
    )
    with pytest.raises(init.StepError, match="doesn't validate"):
        go(ctx, tools)
    assert "unknown: the working tree doesn't render" in out.getvalue()
    assert not fake.gh_calls("pr", "create") and git(root, "branch", "--list", "publish/*") == ""


def test_refuses_without_gh_and_on_a_detached_head(env: dict[str, Any]) -> None:
    root = env["root"]
    edit(root, {"minecraft.motd": "x"})
    ctx, _, tools, _ = make(env, gh=False)
    with pytest.raises(init.StepError, match="`gh`"):
        go(ctx, tools)
    assert go(ctx, tools, dry_run=True) == 0  # the preview needs no gh
    git(root, "checkout", "-q", "--detach")
    ctx, _, tools, _ = make(env)
    with pytest.raises(init.StepError, match="detached HEAD"):
        go(ctx, tools)


def test_refuses_another_branch_and_a_backend_change(env: dict[str, Any]) -> None:
    root = env["root"]
    git(root, "checkout", "-q", "-b", "feature")
    edit(root, {"minecraft.motd": "x"})
    ctx, _, tools, _ = make(env)
    with pytest.raises(init.StepError, match="not feature"):
        go(ctx, tools)
    git(root, "checkout", "-q", "main")
    edit(root, {"state.key": "other/terraform.tfstate"})
    with pytest.raises(init.StepError, match="state backend"):
        go(ctx, tools)


def test_route_and_revert_combinations_are_refused(env: dict[str, Any]) -> None:
    ctx, _, tools, _ = make(env)
    with pytest.raises(init.StepError, match="--route"):
        go(ctx, tools, route="sideways")
    with pytest.raises(init.StepError, match="pull request"):
        go(ctx, tools, route="main", revert=True)


# --- route pr ---------------------------------------------------------------------------------------------


def test_pr_route_commits_opens_the_pr_merges_when_green_and_follows(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, fake, tools, out = make(env, online=[0])
    edit(root, {"minecraft.motd": "Fresh paint"})
    assert go(ctx, tools) == 0, out.getvalue()
    text = out.getvalue()
    (create,) = fake.gh_calls("pr", "create")
    assert fake.head.startswith("publish/20261107-120000")
    title = create[create.index("--title") + 1]
    assert title == "mcctl publish: server.yml (minecraft.motd)"
    body = create[create.index("--body") + 1]
    assert "Minecraft restart: yes" in body and "admin's merge" in body
    assert "server / validate: passed" in text and "needs the admin (expected" in text
    assert fake.gh_calls("pr", "merge") and "merged" in text and "deployed" in text
    assert fake.streamed == [
        ["gh", "run", "watch", "99", "-R", "mcctl-tests/tests-mc-config", "--exit-status"]
    ]
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert git(root, "rev-parse", "HEAD") == fake.merged == git(env["origin"], "rev-parse", "main")
    assert "Fresh paint" in (root / configrepo.SERVER_YML).read_text()
    assert "status: active, players 0" in text


def test_pr_route_failed_check_does_not_merge(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env)
    fake.checks = [[{"name": "server / validate", "bucket": "fail", "link": "https://x/1"}]]
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools) == 1
    assert not fake.gh_calls("pr", "merge") and "a check failed" in out.getvalue()
    assert git(env["root"], "rev-parse", "--abbrev-ref", "HEAD").startswith("publish/")  # stays: fix, rerun


def test_pr_route_checks_time_out(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env)
    fake.checks = [[{"name": "server / validate", "bucket": "pending"}]]
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools) == 1
    assert "didn't finish within 45 min" in out.getvalue() and not fake.gh_calls("pr", "merge")
    assert ctx.deps.now() - datetime(2026, 11, 7, 12, tzinfo=UTC) >= timedelta(minutes=45)


def test_pr_route_no_follow_stops_after_the_pr(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env)
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools, follow=False) == 0
    assert (
        fake.gh_calls("pr", "create")
        and not fake.gh_calls("pr", "checks")
        and "--no-follow" in out.getvalue()
    )


def test_pr_route_merge_declined(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env, answers={"publish_merge": False})
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools) == 0 and not fake.gh_calls("pr", "merge") and "waits for you" in out.getvalue()


def test_rerun_on_a_publish_branch_reuses_it(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, _, tools, _ = make(env)
    edit(root, {"minecraft.motd": "one"})
    assert go(ctx, tools, follow=False) == 0
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    edit(root, {"minecraft.motd": "two"})
    ctx, _fake, tools, _ = make(env)
    assert go(ctx, tools, follow=False) == 0
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == branch
    assert git(env["origin"], "log", "-1", "--format=%s", branch).startswith("mcctl publish: server.yml")


# --- restart awareness ------------------------------------------------------------------------------------


def test_players_online_unattended_waits_until_empty_then_merges(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env, online=[2, 2, 1, 0])  # preview, question, then the wait
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools) == 0
    text = out.getvalue()
    assert "there are 2 player(s) online" in text and "the server is empty" in text
    assert fake.gh_calls("pr", "merge")


def test_players_online_unattended_wait_times_out_and_cancels(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env, online=[3])
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools) == 0
    assert "still not empty after 10 min" in out.getvalue() and not fake.gh_calls("pr", "merge")
    assert "not merged (cancelled)" in out.getvalue()


@pytest.mark.parametrize(("answer", "merged"), [("now", True), ("cancel", False)])
def test_players_online_answered(env: dict[str, Any], answer: str, merged: bool) -> None:
    ctx, fake, tools, _ = make(env, answers={"publish_restart": answer}, online=[2])
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools) == 0
    assert bool(fake.gh_calls("pr", "merge")) is merged


def test_wait_empty_flag_and_unknown_players(env: dict[str, Any]) -> None:
    ctx, _fake, tools, out = make(env, answers={"publish_restart": "now"}, online=[None, None, 0])
    edit(env["root"], {"minecraft.motd": "x"})
    assert go(ctx, tools, wait_empty=True) == 0  # --wait-empty wins over the answer
    assert "players online unknown" in out.getvalue() and "the server is empty" in out.getvalue()


def test_no_restart_needs_no_question(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env, online=[5])
    edit(env["root"], {"backups.prune_schedule": "Sun 04:30"})
    assert go(ctx, tools) == 0 and fake.gh_calls("pr", "merge")
    assert "player(s) online" not in out.getvalue()


# --- terraform -------------------------------------------------------------------------------------------


def test_terraform_change_runs_apply_before_the_merge(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env)
    edit(env["root"], {"dns.ttl": 600})
    assert go(ctx, tools) == 0
    assert tools.product.calls == [["apply", "-C", str(env["root"])]]  # type: ignore[attr-defined]
    assert "Terraform: yes (dns_ttl)" in out.getvalue() and fake.gh_calls("pr", "merge")


def test_terraform_not_applied_stops_unattended(env: dict[str, Any]) -> None:
    ctx, fake, tools, out = make(env, product_rc=1)
    edit(env["root"], {"dns.ttl": 600})
    assert go(ctx, tools) == 0
    assert not fake.gh_calls("pr", "merge") and "not merged (cancelled)" in out.getvalue()


# --- routes main / deploy ------------------------------------------------------------------------------------


def test_main_route_pushes_main_and_follows(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, fake, tools, out = make(env)
    edit(root, {"minecraft.motd": "direct"})
    assert go(ctx, tools, route="main") == 0
    assert git(env["origin"], "rev-parse", "main") == git(root, "rev-parse", "HEAD")
    assert not fake.gh_calls("pr") and fake.streamed and "deployed" in out.getvalue()


def test_main_route_cancelled_pushes_nothing(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, _, tools, _ = make(env, answers={"publish_restart": "cancel"}, online=[1])
    edit(root, {"minecraft.motd": "direct"})
    before = git(env["origin"], "rev-parse", "main")
    assert go(ctx, tools, route="main") == 0
    assert git(env["origin"], "rev-parse", "main") == before


def test_deploy_route_is_the_labelled_emergency(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, fake, tools, out = make(env)
    edit(root, {"performance.view_distance": 6})
    before = git(env["origin"], "rev-parse", "main")
    assert go(ctx, tools, route="deploy") == 0
    assert tools.product.calls == [["deploy", "-C", str(root)]]  # type: ignore[attr-defined]
    text = out.getvalue()
    assert "EMERGENCY" in text and "skipping CI" in text and "main doesn't have this change" in text
    assert git(env["origin"], "rev-parse", "main") == before and not fake.gh_calls()


# --- revert ---------------------------------------------------------------------------------------------


def test_revert_opens_a_pr_undoing_the_last_deploy(env: dict[str, Any]) -> None:
    root = env["root"]
    first = git(root, "rev-parse", "HEAD")
    edit(root, {"minecraft.motd": "deployed one"})
    git(root, "commit", "-qam", "motd")
    edit(root, {"backups.prune_schedule": "Sun 04:30"})
    git(root, "commit", "-qam", "prune")
    git(root, "push", "-q", "origin", "main")
    last = git(root, "rev-parse", "HEAD")
    ctx, fake, tools, out = make(env)
    fake.deploy_runs = [
        {"databaseId": 3, "headSha": last, "event": "push"},
        {"databaseId": 2, "headSha": last, "event": "schedule"},
        {"databaseId": 1, "headSha": first, "event": "push"},
    ]
    assert go(ctx, tools, revert=True, dry_run=True) == 0
    assert "would revert 2 commit(s)" in out.getvalue() and not fake.gh_calls("pr", "create")
    ctx, fake, tools, out = make(env)
    fake.deploy_runs = [{"databaseId": 3, "headSha": last}, {"databaseId": 1, "headSha": first}]
    assert go(ctx, tools, revert=True) == 0, out.getvalue()
    (create,) = fake.gh_calls("pr", "create")
    assert create[create.index("--title") + 1] == f"mcctl publish --revert: undo the deploy of {last[:12]}"
    assert git(root, "diff", first, "HEAD", "--", "server.yml") == ""  # main is back at the earlier deploy
    assert fake.head.startswith("publish/revert-")


def test_revert_refuses_dirty_and_no_deploy(env: dict[str, Any]) -> None:
    ctx, _fake, tools, _ = make(env)
    with pytest.raises(init.StepError, match="no successful deploy"):
        go(ctx, tools, revert=True)
    edit(env["root"], {"minecraft.motd": "x"})
    with pytest.raises(init.StepError, match="uncommitted"):
        go(ctx, tools, revert=True)


def test_revert_skips_runs_whose_deploy_job_did_not_run(env: dict[str, Any]) -> None:
    root = env["root"]
    first = git(root, "rev-parse", "HEAD")
    edit(root, {"minecraft.motd": "deployed one"})
    git(root, "commit", "-qam", "motd")
    git(root, "push", "-q", "origin", "main")
    ctx, fake, _tools, _ = make(env)
    fake.deploy_runs = [{"databaseId": 5, "headSha": git(root, "rev-parse", "HEAD")},
                        {"databaseId": 4, "headSha": first}]  # fmt: skip
    fake.deploy_jobs = {"5": "skipped"}
    assert publish.last_deploys(ctx, configrepo.load(root).server) == [first]


# --- players (PR route) -----------------------------------------------------------------------------------


def test_players_add_goes_through_a_pull_request(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, fake, tools, out = make(env, online=[3])
    uuid = "00000000-0000-4000-8000-0000000000f1"
    assert players.change(ctx, "add", "NewPal", uuid=uuid, tools=tools) == 0, out.getvalue()
    assert fake.head.startswith("players/add-newpal-20261107")
    (create,) = fake.gh_calls("pr", "create")
    assert create[create.index("--title") + 1] == "mcctl players add NewPal"
    assert fake.gh_calls("pr", "merge") and "player(s) online" not in out.getvalue()  # no restart
    assert git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
    assert "NewPal" in [p.name for p in configrepo.load(root).players.whitelist]
    assert git(env["origin"], "show", "main:players.yml").count("NewPal") == 1


def test_players_pr_failure_restores_players_yml(env: dict[str, Any]) -> None:
    root = env["root"]
    before = (root / configrepo.PLAYERS_YML).read_text()
    ctx, fake, tools, _ = make(env)
    fake.pr_create_rc = 1
    with pytest.raises(init.StepError, match="gh pr create"):
        players.change(ctx, "add", "NewPal", uuid="00000000-0000-4000-8000-0000000000f1", tools=tools)
    assert (root / configrepo.PLAYERS_YML).read_text() == before
    assert (
        git(root, "rev-parse", "--abbrev-ref", "HEAD") == "main"
        and git(root, "branch", "--list", "players/*") == ""
    )


def test_players_push_and_commit_routes_keep_the_old_behaviour(env: dict[str, Any]) -> None:
    root = env["root"]
    ctx, fake, _tools, _ = make(env)
    assert (
        players.change(ctx, "add", "NewPal", uuid="00000000-0000-4000-8000-0000000000f1", route="push") == 0
    )
    assert git(env["origin"], "rev-parse", "main") == git(root, "rev-parse", "HEAD") and not fake.gh_calls()
    assert players.change(ctx, "deop", "E2EAdmin", route="commit") in (0,)
    with pytest.raises(init.StepError, match="route"):
        players.change(ctx, "add", "X", route="sideways")


def test_players_pr_refuses_without_gh(env: dict[str, Any]) -> None:
    root = env["root"]
    before = (root / configrepo.PLAYERS_YML).read_text()
    ctx, _, tools, _ = make(env, gh=False)
    with pytest.raises(init.StepError, match="--push"):
        players.change(ctx, "add", "NewPal", uuid="00000000-0000-4000-8000-0000000000f1", tools=tools)
    assert (root / configrepo.PLAYERS_YML).read_text() == before


# --- the command line ----------------------------------------------------------------------------------------


def test_cli_publish_and_players_flags(monkeypatch: pytest.MonkeyPatch, env: dict[str, Any]) -> None:
    seen: dict[str, Any] = {}
    monkeypatch.setattr(publish, "run", lambda ctx, **kw: seen.update(kw) or 0)
    root = str(env["root"])
    assert cli.run(["publish", "-C", root, "--route", "main", "--wait-empty", "--no-follow", "-m", "hi"]) == 0
    assert seen["route"] == "main" and seen["wait_empty"] and not seen["follow"] and seen["message"] == "hi"
    assert seen["tools"].max_wait_minutes == 120
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(players, "change", lambda ctx, a, n, **kw: calls.append(kw) or 0)
    assert cli.run(["players", "add", "X", "-C", root]) == 0
    assert cli.run(["players", "add", "X", "--push", "-C", root]) == 0
    assert cli.run(["players", "add", "X", "--no-push", "-C", root]) == 0
    assert [c["route"] for c in calls] == ["pr", "push", "commit"]
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["players", "add", "X", "--push", "--no-push"])
