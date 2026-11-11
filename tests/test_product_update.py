"""v1.5: the config repo's manual product-update run (mcctl/productupdate.py, `mcctl product-update`): the
release it resolves, the Worker-change check, the pin pull request (new, or the weekly updater's / `mcctl
upgrade`'s reused), the summaries, the reusable workflow's jobs, and how a config repo's workflow gets the new
manual-run input (mcctl/wfsecrets.py). No network: a fake GitHub API (mcctl.http transport)."""

from __future__ import annotations

import base64
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import EXAMPLE, REPO, TEST_SHA, V141_CLIENT_LOCK, fake_release, make_config_repo
from test_updates import FakeGitHub

from mcctl import cli, configrepo, doctor, fingerprint, http, productupdate, updates, upgrade, wfsecrets

REUSABLE = REPO / ".github/workflows/server.yml"
CALLER = REPO / "template/.github/workflows/server.yml"
TEMPLATE = CALLER.read_text()
NEW_SHA = "b" * 40
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@t.test"]


def load(path: Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text())
    data["on"] = data.pop(True)
    return data


class GitHub(FakeGitHub):
    """+ branch lookups and forced ref moves (a leftover update/* branch), html_url and auto_merge on pulls."""

    def __call__(self, req: http.Request) -> http.Response:
        r = f"/repos/{self.repo}"
        path = req.url.split("https://api.github.com", 1)[1].split("?")[0]
        if req.method == "GET" and path.startswith(f"{r}/git/ref/heads/") and not path.endswith("/main"):
            branch = path.split("/git/ref/heads/", 1)[1]
            self.calls.append((req.method, path))
            if branch not in self.branches:
                return http.Response(404, {}, b'{"message": "Not Found"}')
            return http.Response(200, {}, b'{"object": {"sha": "' + b"e" * 40 + b'"}}')
        if req.method == "PATCH" and path.startswith(f"{r}/git/refs/heads/"):
            self.calls.append((req.method, path))
            self.branches[path.split("/git/refs/heads/", 1)[1]] = dict(self.branches["main"])
            return http.Response(200, {}, b"{}")
        resp = super().__call__(req)
        if req.method == "POST" and path == f"{r}/pulls":
            pr = self.pulls[-1]
            pr["html_url"] = f"https://github.com/{self.repo}/pull/{pr['number']}"
        return resp


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    return make_config_repo(tmp_path / "config", renderable=True)


def target(new: str = "v0.0.1", sha: str = NEW_SHA, old: str = "v0.0.0") -> productupdate.Target:
    return productupdate.Target(configrepo.PRODUCT_REPOSITORY, old, TEST_SHA, new, sha)


def prepared(cfg: Path, t: productupdate.Target) -> dict[str, str]:
    text = (cfg / configrepo.WORKFLOW).read_text()
    return {configrepo.WORKFLOW: updates.bump_pin(text, t.repository, t.sha, t.new)}


# --- the Worker's inputs ---------------------------------------------------------------------------------


def product_tree(root: Path) -> Path:
    for rel, text in {
        "watchdog/worker.js": "export default {}\n",
        "watchdog/panel/panel.js": "// ui\n",
        "watchdog/test/worker.test.mjs": "// test\n",
        "terraform/watchdog.tf": "# tf\n",
        "mcctl/render.py": "# render\n",
        "mcctl/other.py": "# not an input\n",
    }.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
    return root


def test_worker_diff_compares_what_terraform_reads(tmp_path: Path) -> None:
    """v1.5.2: the Worker fingerprint's files (mcctl/fingerprint.py), the ones the panel's Versions row
    compares: worker.js, the panel's files, terraform/; not render.py (v1.5.1 changed only a comment in it and
    the summary asked for a local apply) nor tests, caches, or a render-built pack."""
    old, new = product_tree(tmp_path / "old"), product_tree(tmp_path / "new")
    assert productupdate.worker_diff(old, new) == []
    (new / "watchdog/test/worker.test.mjs").write_text("// changed test\n")
    (new / "mcctl/other.py").write_text("# changed, not an input\n")
    (new / "terraform/.terraform").mkdir()
    (new / "terraform/.terraform/cache").write_text("x")
    (new / "terraform/plan.tfplan").write_text("x")
    (new / "watchdog/panel/pack").mkdir()
    (new / "watchdog/panel/pack/a.zip").write_text("x")
    (old / "mcctl/render.py").write_text("# old render, only a comment changed\n")
    assert productupdate.worker_diff(old, new) == []
    assert fingerprint.worker_fingerprint(old) == fingerprint.worker_fingerprint(new)
    (new / "watchdog/panel/panel.js").write_text("// new ui\n")
    (new / "terraform/panel.tf").write_text("# new\n")
    (new / "terraform/.terraform.lock.hcl").write_text("# providers\n")
    assert productupdate.worker_diff(old, new) == [
        "terraform/.terraform.lock.hcl", "terraform/panel.tf", "watchdog/panel/panel.js",
    ]  # fmt: skip
    assert fingerprint.worker_fingerprint(old) != fingerprint.worker_fingerprint(new)
    assert productupdate.worker_diff(old, tmp_path / "missing") is None
    assert [productupdate.worker_state(x) for x in (None, [], ["a"])] == ["unknown", "false", "true"]
    # this checkout is a product checkout: compared with itself, nothing changed
    assert productupdate.worker_diff(REPO, REPO) == []


# --- the release -----------------------------------------------------------------------------------------


def test_resolve_latest_a_tag_current_and_refusals(cfg: Path) -> None:
    pin = configrepo.load(cfg).pin
    gh = GitHub(cfg, release=("v0.0.1", NEW_SHA))
    t = productupdate.resolve(pin, "latest", "prt", gh)
    assert (t.old, t.new, t.sha, t.current, t.patch, t.migrate_from) == (
        "v0.0.0", "v0.0.1", NEW_SHA, False, True, "0.0.0",
    )  # fmt: skip
    assert "Bearer prt" in gh.auth
    assert productupdate.resolve(pin, "  ", "", gh).new == "v0.0.1"  # empty input: latest
    assert productupdate.resolve(pin, "v0.0.1", "", gh).sha == NEW_SHA
    assert productupdate.resolve(pin, "latest", "", GitHub(cfg, release=("v0.0.0", TEST_SHA))).current
    with pytest.raises(productupdate.UpdateError, match=r"no tag v0\.2\.0"):
        productupdate.resolve(pin, "v0.2.0", "", gh)
    with pytest.raises(productupdate.UpdateError, match="latest` or a release tag"):
        productupdate.resolve(pin, "main", "", gh)
    with pytest.raises(productupdate.UpdateError, match="no release"):
        productupdate.resolve(pin, "latest", "", GitHub(cfg, release=None))
    newer = configrepo.Pin(pin.repository, TEST_SHA, "v0.1.0")
    with pytest.raises(productupdate.UpdateError, match="no downgrades"):
        productupdate.resolve(newer, "latest", "", gh)
    assert target(old="v1.4.2").migrate_from == "1.4.2" and not target("v1.5.0", old="v1.4.2").patch


# --- the pull request -------------------------------------------------------------------------------------


def test_a_patch_opens_an_auto_merged_pr_on_the_updaters_branch(cfg: Path) -> None:
    gh = GitHub(cfg)
    repo = updates.Repo(gh.repo, "t", gh)
    t = target()
    out = productupdate.open_or_reuse(repo, t, prepared(cfg, t), auto_merge="builds+patch", protected=True,
                                      worker=[])  # fmt: skip
    assert out.auto and not out.reused and out.branch == "update/product-v0.0.1" and out.number == 1
    assert gh.auto_merge == ["PR_1"]
    assert f"@{NEW_SHA} # v0.0.1" in gh.branches["update/product-v0.0.1"][configrepo.WORKFLOW]
    assert "will merge itself" in out.state() and out.url.endswith("/pull/1")
    assert (
        "Nothing: it merges by itself" in gh.pulls[0]["body"] and "Local step:** none" in gh.pulls[0]["body"]
    )
    # run again: the open PR is reused, nothing new, auto-merge not requested twice when it is on
    gh.pulls[0]["auto_merge"] = {"merge_method": "squash"}
    again = productupdate.open_or_reuse(updates.Repo(gh.repo, "t", gh), t, prepared(cfg, t),
                                        auto_merge="builds+patch", protected=True, worker=[])  # fmt: skip
    assert again.reused and len(gh.pulls) == 1 and gh.auto_merge == ["PR_1"]
    assert any("already proposes this: reused" in line for line in again.log)


@pytest.mark.parametrize(
    ("new", "level", "protected", "migrate", "why"),
    [
        ("v0.1.0", "builds+patch", True, False, "minor/major"),
        ("v0.0.1", "builds", True, False, "automation.auto_merge is builds"),
        ("v0.0.1", "builds+patch", False, False, "protect_main is false"),
        ("v0.0.1", "builds+patch", True, True, "changes more than the `uses:` line"),
    ],
)
def test_everything_but_a_pure_patch_waits_for_the_admin(cfg, new, level, protected, migrate, why) -> None:
    gh = GitHub(cfg, release=(new, NEW_SHA))
    t = target(new)
    files = prepared(cfg, t)
    if migrate:
        files[configrepo.SERVER_YML] = (cfg / configrepo.SERVER_YML).read_text() + "# migrated\n"
    out = productupdate.open_or_reuse(updates.Repo(gh.repo, "t", gh), t, files, auto_merge=level,
                                      protected=protected, worker=["watchdog/worker.js"])  # fmt: skip
    assert not out.auto and why in out.why and gh.auto_merge == []
    assert out.branch == f"update/product-{new}"  # the only branch the updater App may write
    assert "waiting for your merge" in out.state()
    assert f"mcctl rollout --to {new}" in gh.pulls[0]["body"]
    if migrate:
        assert gh.branches[out.branch][configrepo.SERVER_YML].endswith("# migrated\n")


def test_the_weekly_updaters_pr_is_reused_and_brought_up_to_date(cfg: Path) -> None:
    gh = GitHub(cfg, release=("v0.1.0", NEW_SHA))
    updates.run(cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds+patch", product_token="",
                today=__import__("datetime").date(2026, 11, 3))  # fmt: skip
    assert [p["head"]["ref"] for p in gh.pulls] == ["update/product-v0.1.0"]
    t = target("v0.1.0")
    files = prepared(cfg, t) | {configrepo.SERVER_YML: (cfg / "server.yml").read_text() + "# migrated\n"}
    out = productupdate.open_or_reuse(updates.Repo(gh.repo, "t", gh), t, files, auto_merge="builds+patch",
                                      protected=True, worker=None)  # fmt: skip
    assert out.reused and out.number == 1 and len(gh.pulls) == 1 and not out.auto
    assert gh.branches["update/product-v0.1.0"][configrepo.SERVER_YML].endswith("# migrated\n")
    assert any("brought server.yml up to date" in line for line in out.log)


def test_an_open_mcctl_upgrade_pr_is_reused_untouched(cfg: Path) -> None:
    gh = GitHub(cfg, release=("v0.1.0", NEW_SHA))
    gh.pulls.append({"number": 1, "node_id": "PR_1", "state": "open", "title": "mcctl upgrade: product v0.1.0",
                     "head": {"ref": "upgrade/product-v0.1.0"}, "body": "",
                     "html_url": f"https://github.com/{gh.repo}/pull/1"})  # fmt: skip
    t = target("v0.1.0")
    out = productupdate.open_or_reuse(updates.Repo(gh.repo, "t", gh), t, prepared(cfg, t),
                                      auto_merge="builds+patch", protected=True, worker=[])  # fmt: skip
    assert out.reused and out.branch == "upgrade/product-v0.1.0" and not out.auto and len(gh.pulls) == 1
    assert "mcctl upgrade" in out.why
    assert not [c for c in gh.calls if c[0] in ("PUT", "POST")]  # nothing written
    # the weekly updater doesn't duplicate it either
    log = updates.run(cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds+patch", product_token="",
                      today=__import__("datetime").date(2026, 11, 3))  # fmt: skip
    assert len(gh.pulls) == 1 and any("not duplicated" in line for line in log)


def test_a_leftover_branch_is_reset_and_older_prs_are_superseded(cfg: Path) -> None:
    gh = GitHub(cfg, release=("v0.0.2", NEW_SHA))
    gh.branches["update/product-v0.0.2"] = {"stale": "x"}  # a closed PR's branch was never deleted
    gh.pulls.append({"number": 1, "node_id": "PR_1", "state": "open", "title": "Product update to v0.0.1",
                     "head": {"ref": "update/product-v0.0.1"}, "body": ""})  # fmt: skip
    t = target("v0.0.2")
    out = productupdate.open_or_reuse(updates.Repo(gh.repo, "t", gh), t, prepared(cfg, t),
                                      auto_merge="builds+patch", protected=True, worker=[])  # fmt: skip
    assert not out.reused and out.number == 2 and out.auto
    assert ("PATCH", f"/repos/{gh.repo}/git/refs/heads/update/product-v0.0.2") in gh.calls
    assert "stale" not in gh.branches["update/product-v0.0.2"]
    assert gh.pulls[0]["state"] == "closed"  # superseded by v0.0.2
    assert any("reset to main" in line for line in out.log)


def test_the_prepared_workflow_must_pin_the_target(cfg: Path) -> None:
    gh = GitHub(cfg)
    with pytest.raises(productupdate.UpdateError, match=r"doesn't pin v0\.0\.1"):
        productupdate.open_or_reuse(updates.Repo(gh.repo, "t", gh), target(), prepared(cfg, target("v0.0.3")),
                                    auto_merge="builds+patch", protected=True, worker=[])  # fmt: skip
    assert gh.pulls == []


def test_run_pr_needs_the_app_and_never_prints_the_key(cfg: Path) -> None:
    code, text, _ = productupdate.run_pr(cfg, {}, target(), [])
    assert code == 1 and "no updater GitHub App" in text and "mcctl rollout --to v0.0.1" in text
    gh = GitHub(cfg)
    pem = "-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----\n"

    def transport(req: http.Request) -> http.Response:
        if req.url.endswith("/installation"):
            return http.Response(200, {}, b'{"id": 7}')
        if req.url.endswith("/app/installations/7/access_tokens"):
            return http.Response(201, {}, b'{"token": "ghs-fake", "expires_at": "2026-11-08T05:00:00Z"}')
        return gh(req)

    env = {
        "UPDATER_APP_ID": "42", "UPDATER_APP_KEY": pem, "GITHUB_REPOSITORY": gh.repo,
        "PREPARED_WORKFLOW": base64.b64encode(prepared(cfg, target())[configrepo.WORKFLOW].encode()).decode(),
    }  # fmt: skip
    code, text, log = productupdate.run_pr(cfg, env, target(), ["watchdog/worker.js"], transport,
                                           sign=lambda key, data: b"sig")  # fmt: skip
    assert code == 0 and "Bearer ghs-fake" in gh.auth
    assert "PRIVATE" not in text + "".join(log) and "ghs-fake" not in text + "".join(log)
    assert "| Product | `v0.0.0` -> **`v0.0.1`**" in text and "will merge itself" in text
    assert "mcctl rollout --to v0.0.1" in text and "`watchdog/worker.js`" in text


# --- v1.5.1: a release that ships a new in-house client mod ------------------------------------------------

V150_LOCK = (REPO / "template/client.lock.yml").read_text()


def test_the_pin_pr_carries_the_targets_in_house_entries_and_still_auto_merges(cfg: Path) -> None:
    """The live v1.4.1 -> v1.5.0 case on the update button: main pins mod 1.0.0; the prepared change (the
    `inhouse` step, from the target checkout) has 1.1.0. Pin + in-house entries is still a pure patch bump."""
    (cfg / configrepo.CLIENT_LOCK).write_text(V141_CLIENT_LOCK.read_text())
    gh = GitHub(cfg)
    t = target()
    files = prepared(cfg, t) | {configrepo.CLIENT_LOCK: V150_LOCK}
    out = productupdate.open_or_reuse(updates.Repo(gh.repo, "t", gh), t, files, auto_merge="builds+patch",
                                      protected=True, worker=[])  # fmt: skip
    assert out.auto and gh.auto_merge == ["PR_1"]
    assert gh.branches["update/product-v0.0.1"][configrepo.CLIENT_LOCK] == V150_LOCK
    assert (
        "in-house client mod entries" in gh.pulls[0]["body"] and "only the pin moved" in gh.pulls[0]["body"]
    )
    # anything else in the lock: the admin
    gh2 = GitHub(cfg)
    other = V150_LOCK.replace("mc26.3-0.9.2-fabric", "mc26.3-0.9.3-fabric", 1)
    assert other != V150_LOCK
    out2 = productupdate.open_or_reuse(updates.Repo(gh2.repo, "t", gh2), t,
                                       prepared(cfg, t) | {configrepo.CLIENT_LOCK: other},
                                       auto_merge="builds+patch", protected=True, worker=[])  # fmt: skip
    assert not out2.auto and "more than the `uses:` line" in out2.why and gh2.auto_merge == []
    # the weekly updater's pin-only PR for the release is brought up to date with the lock
    gh3 = GitHub(cfg)
    gh3.pulls.append({"number": 1, "node_id": "PR_1", "state": "open", "title": "Product update to v0.0.1",
                      "head": {"ref": "update/product-v0.0.1"}, "body": ""})  # fmt: skip
    gh3.branches["update/product-v0.0.1"] = dict(gh3.branches["main"]) | prepared(cfg, t)
    out3 = productupdate.open_or_reuse(updates.Repo(gh3.repo, "t", gh3), t, files, auto_merge="builds+patch",
                                       protected=True, worker=[])  # fmt: skip
    assert out3.reused and out3.auto
    assert gh3.branches["update/product-v0.0.1"][configrepo.CLIENT_LOCK] == V150_LOCK
    assert any("brought client.lock.yml up to date" in line for line in out3.log)


def test_run_pr_reads_the_prepared_client_lock(cfg: Path) -> None:
    (cfg / configrepo.CLIENT_LOCK).write_text(V141_CLIENT_LOCK.read_text())
    gh = GitHub(cfg)

    def transport(req: http.Request) -> http.Response:
        if req.url.endswith("/installation"):
            return http.Response(200, {}, b'{"id": 7}')
        if req.url.endswith("/app/installations/7/access_tokens"):
            return http.Response(201, {}, b'{"token": "ghs-fake", "expires_at": "2026-11-08T05:00:00Z"}')
        return gh(req)

    def b64(text: str) -> str:
        return base64.b64encode(text.encode()).decode()

    env = {
        "UPDATER_APP_ID": "42", "UPDATER_APP_KEY": "-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA "
        "PRIVATE KEY-----\n", "GITHUB_REPOSITORY": gh.repo,
        "PREPARED_WORKFLOW": b64(prepared(cfg, target())[configrepo.WORKFLOW]),
        "PREPARED_CLIENT_LOCK": b64(V150_LOCK),
    }  # fmt: skip
    code, text, _ = productupdate.run_pr(cfg, env, target(), [], transport, sign=lambda key, data: b"sig")
    assert code == 0 and "will merge itself" in text
    assert gh.branches["update/product-v0.0.1"][configrepo.CLIENT_LOCK] == V150_LOCK


def test_cli_inhouse_then_prepared_hands_the_lock_on(cfg: Path, tmp_path: Path) -> None:
    (cfg / configrepo.CLIENT_LOCK).write_text(V141_CLIENT_LOCK.read_text())
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "seed"]):
        _git(cfg, *argv)
    wf = cfg / configrepo.WORKFLOW
    wf.write_text(updates.bump_pin(wf.read_text(), configrepo.PRODUCT_REPOSITORY, NEW_SHA, "v0.0.1"))
    release = fake_release(tmp_path / "target")
    assert cli.main(["product-update", "inhouse", "-C", str(cfg), "--new-product", str(release)]) == 0
    assert (cfg / configrepo.CLIENT_LOCK).read_text() == V150_LOCK
    assert cli.main(["product-update", "inhouse", "-C", str(cfg), "--new-product", str(release)]) == 0
    prep = tmp_path / "prep"
    old, new = product_tree(tmp_path / "p-old"), product_tree(tmp_path / "p-new")
    assert cli.main(["product-update", "prepared", "-C", str(cfg), "--old-product", str(old),
                     "--new-product", str(new), "--github-output", str(prep)]) == 0  # fmt: skip
    got = dict(line.split("=", 1) for line in prep.read_text().splitlines())
    assert got["changed"] == f"{configrepo.WORKFLOW},{configrepo.CLIENT_LOCK}"
    assert base64.b64decode(got["client_lock"]).decode() == V150_LOCK
    # a release whose template doesn't match its jar stops the run before the target's validate
    _git(cfg, "checkout", "-q", "--", configrepo.CLIENT_LOCK)
    stale = fake_release(tmp_path / "stale", jar=b"other")
    assert cli.main(["product-update", "inhouse", "-C", str(cfg), "--new-product", str(stale)]) == 1
    assert (cfg / configrepo.CLIENT_LOCK).read_text() == V141_CLIENT_LOCK.read_text()


def test_the_inhouse_step_runs_before_the_targets_validate() -> None:
    jobs = load(REUSABLE)["jobs"]
    names = [s.get("name", "") for s in jobs["product-update"]["steps"]]
    inhouse = names.index("The target release's in-house client mod entries (client.lock.yml)")
    assert names.index("Check out the target release") < inhouse
    assert inhouse < names.index("The target release's schema-migrate and validate")
    step = jobs["product-update"]["steps"][inhouse]
    assert step["working-directory"] == "product" and "secrets" not in yaml.safe_dump(step)
    assert jobs["product-update"]["outputs"]["client_lock"] == "${{ steps.prepared.outputs.client_lock }}"
    last = jobs["product-update-pr"]["steps"][-1]
    assert last["env"]["PREPARED_CLIENT_LOCK"] == "${{ needs.product-update.outputs.client_lock }}"


# --- the summaries -----------------------------------------------------------------------------------------


def test_run_locally_says_exactly_what_to_type() -> None:
    changed = productupdate.run_locally("o/mc-config", "v1.5.1", ["watchdog/worker.js"], merged=True)
    text = "\n".join(changed)
    assert "```sh\ncd <your clone of o/mc-config>\ngit pull --ff-only\nmcctl rollout --to v1.5.1\n```" in text
    assert "after the deploy" in text and "straight to `mcctl render`" in text
    waiting = "\n".join(productupdate.run_locally("o/c", "v2.0.0", ["terraform/x.tf"], merged=False))
    assert "merges it after you type v2.0.0" in waiting
    unknown = "\n".join(productupdate.run_locally("o/c", "v1.5.1", None, merged=True))
    assert "Run this if the panel or Worker changed" in unknown and "mcctl rollout --to v1.5.1" in unknown
    none = "\n".join(productupdate.run_locally("o/c", "v1.5.1", [], merged=True))
    assert "No local step needed" in none and "mcctl rollout" not in none
    many = "\n".join(productupdate.run_locally("o/c", "v1", [f"f{i}" for i in range(9)], merged=True))
    assert "and 3 more" in many
    assert "already pinned to **`v0.0.1`**" in productupdate.nothing_to_do(target())


def test_the_deploy_runs_summary_links_the_pr_and_the_result() -> None:
    t = target("v1.5.1", old="v1.5.0")
    ok = productupdate.deploy_summary(repository="o/c", target=t, result="success", run_url="https://run",
                                      head_message="Product update to v1.5.1 (#12)\n\nbody",
                                      worker=["watchdog/worker.js"])  # fmt: skip
    assert ok.startswith("## Product update deployed: v1.5.0 -> v1.5.1")
    assert "[#12](https://github.com/o/c/pull/12): merged" in ok and "[succeeded](https://run)" in ok
    assert "mcctl rollout --to v1.5.1" in ok and "after the deploy" in ok
    bad = productupdate.deploy_summary(repository="o/c", target=t, result="failure", run_url="u",
                                       head_message="manual push", worker=[])  # fmt: skip
    assert (
        "the deploy of v1.5.1 failure" in bad
        and "Fix the deploy first" in bad
        and "### Run locally" not in bad
    )


# --- the CLI steps (what the workflow runs) -------------------------------------------------------------


def _git(root: Path, *args: str) -> str:
    return subprocess.run([*GIT, "-C", str(root), *args], capture_output=True, text=True, check=True).stdout


def test_cli_resolve_prepared_and_previous_pin(cfg: Path, tmp_path: Path, monkeypatch) -> None:
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "seed"]):
        _git(cfg, *argv)
    gh = GitHub(cfg, release=("v0.0.0", TEST_SHA))
    monkeypatch.setattr(http, "urllib_transport", gh)
    out, summary = tmp_path / "out", tmp_path / "summary"
    argv = [
        "product-update",
        "resolve",
        "-C",
        str(cfg),
        "--github-output",
        str(out),
        "--summary",
        str(summary),
    ]
    assert cli.main(argv) == 0
    assert "state=current" in out.read_text() and "nothing to do" in summary.read_text()
    assert _git(cfg, "status", "--porcelain") == ""
    gh.release = ("v0.0.1", NEW_SHA)
    out.write_text("")
    assert cli.main(argv) == 0
    outputs = dict(line.split("=", 1) for line in out.read_text().splitlines())
    assert outputs | {} == {"state": "upgrade", "old": "v0.0.0", "old_sha": TEST_SHA, "new": "v0.0.1",
                            "sha": NEW_SHA, "from": "0.0.0"}  # fmt: skip
    assert f"@{NEW_SHA} # v0.0.1" in (cfg / configrepo.WORKFLOW).read_text()
    assert cli.main([*argv[:4], "--version", "v9.9.9", "--summary", str(summary)]) == 1
    assert "no tag v9.9.9" in summary.read_text()

    old, new = product_tree(tmp_path / "p-old"), product_tree(tmp_path / "p-new")
    (new / "watchdog/worker.js").write_text("export default { v: 2 }\n")
    prep = tmp_path / "prep"
    assert cli.main(["product-update", "prepared", "-C", str(cfg), "--old-product", str(old),
                     "--new-product", str(new), "--github-output", str(prep)]) == 0  # fmt: skip
    got = dict(line.split("=", 1) for line in prep.read_text().splitlines())
    assert got["changed"] == configrepo.WORKFLOW and got["server_yml"] == ""
    assert got["worker"] == "true" and got["worker_paths"] == "watchdog/worker.js"
    assert base64.b64decode(got["workflow"]).decode() == (cfg / configrepo.WORKFLOW).read_text()
    wf = cfg / configrepo.WORKFLOW  # a CRLF checkout (Windows) still prepares the text with \n endings
    wf.write_bytes(wf.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    assert cli.main(["product-update", "prepared", "-C", str(cfg), "--old-product", str(old),
                     "--new-product", str(new), "--github-output", str(prep)]) == 0  # fmt: skip
    got = dict(line.split("=", 1) for line in prep.read_text().splitlines()[-5:])
    assert b"\r" not in base64.b64decode(got["workflow"])
    assert base64.b64decode(got["workflow"]).decode() == wf.read_text()
    (cfg / "players.yml").write_text("# changed\n")  # only the workflow and server.yml may change
    assert cli.main(["product-update", "prepared", "-C", str(cfg), "--old-product", str(old),
                     "--new-product", str(new)]) == 1  # fmt: skip

    _git(cfg, "checkout", "-q", "--", "players.yml")
    before = _git(cfg, "rev-parse", "HEAD").strip()
    _git(cfg, "commit", "-qam", "Product update to v0.0.1 (#3)")
    pin_out = tmp_path / "pin"
    common = ["product-update", "previous-pin", "-C", str(cfg), "--github-output", str(pin_out)]
    assert cli.main([*common, "--before", before, "--product-sha", NEW_SHA]) == 0
    assert pin_out.read_text() == f"previous_product_sha={TEST_SHA}\nprevious_product_version=v0.0.0\n"
    pin_out.write_text("")
    assert cli.main([*common, "--before", before, "--product-sha", TEST_SHA]) == 0  # the pin didn't move
    assert cli.main([*common, "--before", "0" * 40, "--product-sha", NEW_SHA]) == 0  # a new branch
    assert pin_out.read_text() == ""

    deploy = tmp_path / "deploy"
    monkeypatch.setenv("GITHUB_REPOSITORY", "o/c")
    monkeypatch.setenv("HEAD_MESSAGE", "Product update to v0.0.1 (#3)")
    assert cli.main(["product-update", "deploy-summary", "-C", str(cfg), "--old", "v0.0.0", "--old-sha",
                     TEST_SHA, "--previous-product", str(old), "--product", str(new), "--result", "success",
                     "--run-url", "https://run", "--summary", str(deploy)]) == 0  # fmt: skip
    text = deploy.read_text()
    assert "v0.0.0 -> v0.0.1" in text and "[#3](https://github.com/o/c/pull/3)" in text
    assert "mcctl rollout --to v0.0.1" in text


# --- the workflows ------------------------------------------------------------------------------------------


def test_the_manual_run_and_its_jobs() -> None:
    caller = load(CALLER)
    inputs = caller["on"]["workflow_dispatch"]["inputs"]
    assert "product-update" in inputs["task"]["options"]
    assert inputs["product_version"] == {
        "description": "product-update: the product release to pin and deploy (latest, or vX.Y.Z)",
        "type": "string",
        "default": "latest",
    }
    assert (EXAMPLE / configrepo.WORKFLOW).read_text().split("    uses:")[0] == TEMPLATE.split("    uses:")[0]
    wf = load(REUSABLE)
    jobs = wf["jobs"]
    prep, pr = jobs["product-update"], jobs["product-update-pr"]
    picker = next(s for s in jobs["validate"]["steps"] if s.get("id") == "task")["run"]
    assert (
        "product-update" in picker
        and """if [ "$tasks" = '["product-update"]' ] && [ "$REF" != refs/heads/main ]""" in picker
    )
    # minimum permissions, no OIDC token, no Terraform or Cloudflare anywhere in them
    assert prep["permissions"] == {"contents": "read"} and pr["permissions"] == {"contents": "read"}
    text = yaml.safe_dump(prep) + yaml.safe_dump(pr)
    assert not re.search(r"terraform|cloudflare|mcctl (plan|apply)|TF_VAR", text, re.I)
    # prep holds no secret but the read-only product token; it runs no Terraform; no environment
    assert set(re.findall(r"secrets\.(\w+)", yaml.safe_dump(prep))) == {"PRODUCT_READ_TOKEN"}
    assert "environment" not in prep
    # the App's key: only in the last step of product-update-pr, a fresh runner in the update environment
    assert pr["environment"] == "${{ needs.validate.outputs.update_environment }}"
    assert pr["needs"] == ["validate", "product-update"]
    assert "outputs.state == 'upgrade'" in pr["if"] and pr["concurrency"]["cancel-in-progress"] is False
    for step in pr["steps"][:-1]:
        assert "secrets.UPDATER" not in yaml.safe_dump(step)
    last = pr["steps"][-1]
    assert (
        last["env"]["UPDATER_APP_KEY"] == "${{ secrets.UPDATER_APP_KEY }}"
        and "product-update pr" in last["run"]
    )
    names = [s.get("id") for s in prep["steps"]]
    assert names.index("target") < names.index("prepared")
    migrate = next(
        s for s in prep["steps"] if s.get("name") == "The target release's schema-migrate and validate"
    )
    assert migrate["working-directory"] == "target" and "schema-migrate" in migrate["run"]
    assert "--require-hashes --no-deps" in migrate["run"]
    # the deploy run after the merge: the same summary, never failing the deploy
    deploy = jobs["deploy"]["steps"]
    summary = next(s for s in deploy if s.get("name") == "Product update summary")
    assert summary["continue-on-error"] is True and summary["if"].startswith("always()")
    assert summary["env"]["RESULT"] == "${{ job.status }}"
    assert "previous_product_sha" in jobs["validate"]["outputs"]


# --- the config repo gets the new manual-run input ---------------------------------------------------------


def old_template() -> str:
    """The template as v1.4 shipped it (no product-update)."""
    text = TEMPLATE.replace("options: [deploy, update, product-update, backup-check, expiry-check]",
                            "options: [deploy, update, backup-check, expiry-check]")  # fmt: skip
    return re.sub(r"      product_version:\n(        .*\n)+", "", text)


def test_dispatch_inputs_are_added_keeping_everything_else() -> None:
    old = old_template()
    assert wfsecrets.missing_dispatch(old, TEMPLATE) == [
        "input product_version",
        "task option product-update",
    ]
    new = wfsecrets.add_dispatch(old, TEMPLATE)
    assert new == TEMPLATE and wfsecrets.missing_dispatch(new, TEMPLATE) == []
    assert wfsecrets.add_dispatch(TEMPLATE, TEMPLATE) == TEMPLATE  # idempotent
    # a config repo's own option order, comments and extra inputs stay
    custom = old.replace("options: [deploy, update, backup-check, expiry-check]",
                         "options: [update, deploy, expiry-check]  # mine")  # fmt: skip
    custom = custom.replace(
        "        default: false\n", "        default: false\n      mine:\n        type: string\n"
    )
    out = wfsecrets.add_dispatch(custom, TEMPLATE)
    assert "options: [update, product-update, backup-check, deploy, expiry-check]  # mine" in out
    assert "      mine:\n        type: string\n      product_version:\n" in out
    with pytest.raises(wfsecrets.WorkflowError, match="one-line"):
        wfsecrets.add_dispatch(old.replace("options: [deploy, update, backup-check, expiry-check]",
                                           "options:\n          - deploy"), TEMPLATE)  # fmt: skip
    no_form = old.replace("  workflow_dispatch:\n", "  workflow_dispatch_off:\n")
    assert wfsecrets.missing_dispatch(no_form, TEMPLATE) == []  # nothing to extend: left alone
    assert wfsecrets.sync(old, TEMPLATE) == (TEMPLATE, ["manual run gets input product_version, task option "
                                                        "product-update"])  # fmt: skip


def _repo_with(tmp_path: Path, text: str) -> configrepo.ConfigRepo:
    root = make_config_repo(tmp_path / "c", renderable=True)
    wf = root / configrepo.WORKFLOW
    wf.write_text(text.replace("__PRODUCT_REPOSITORY__", configrepo.PRODUCT_REPOSITORY)
                  .replace("__PRODUCT_SHA__", TEST_SHA).replace("__PRODUCT_VERSION__", "v0.0.0"))  # fmt: skip
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], ["commit", "-qm", "seed"]):
        _git(root, *argv)
    return configrepo.load(root)


def test_upgrade_migration_and_doctor_bring_the_input_in(tmp_path: Path) -> None:
    """`mcctl upgrade` (and the product-update run) apply the target's schema-migrate, which adds the input to
    the same pull request; a pin moved without it (the updater's PR) is flagged and fixed by `doctor --fix`."""
    repo = _repo_with(tmp_path / "a", old_template())
    assert upgrade.schema_migrate(repo.root, "1.4.1", REPO) == [
        f"{configrepo.WORKFLOW}: the manual run gets input product_version, task option product-update "
        "(the product's template has them)"
    ]
    assert wfsecrets.missing_dispatch((repo.root / configrepo.WORKFLOW).read_text(), TEMPLATE) == []
    assert upgrade.schema_migrate(repo.root, "1.4.1", REPO) == []  # idempotent

    repo = _repo_with(tmp_path / "b", old_template())

    def run(argv):
        argv = list(argv)
        return subprocess.run([*GIT, *argv[1:]] if argv[0] == "git" else argv, capture_output=True, text=True)

    env = {"MCCTL_PRODUCT_DIR": str(REPO)}
    report = doctor.Report()
    doctor.check_workflow_inputs(report, repo, env, doctor.Deps(run=run), False)
    (row,) = report.results
    assert row.level == "warn" and "product_version" in row.detail and "doctor --fix" in row.detail
    report = doctor.Report()
    doctor.check_workflow_inputs(report, repo, env, doctor.Deps(run=run), True)
    assert report.results[0].level == "ok" and "push to main" in report.results[0].detail
    assert "mcctl doctor: the manual run gets" in _git(repo.root, "log", "-1", "--format=%s")
    report = doctor.Report()
    doctor.check_workflow_inputs(report, repo, env, doctor.Deps(run=run), False)
    assert report.results[0].level == "ok"
