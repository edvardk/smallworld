"""P11: the weekly update task (mcctl update-prs: lock and product update PRs through the updater GitHub App), the
expiry check (mcctl expiry-check), the update slot vs automation.update_day, and the workflows' contracts
(update/expiry-check jobs, Dependabot auto-merge). No network: a fake GitHub API (mcctl.http transport)."""

from __future__ import annotations

import base64
import datetime
import json
import re
import urllib.parse
from pathlib import Path
from typing import Any

import pytest
import yaml
from conftest import MOD_JAR, REPO, TEST_SHA, V141_CLIENT_LOCK, make_config_repo

from mcctl import configrepo, expirycheck, http, updates

REUSABLE = REPO / ".github/workflows/server.yml"
DEPENDABOT = REPO / ".github/workflows/dependabot-automerge.yml"
NEW_SHA = "b" * 40
TODAY = datetime.date(2026, 11, 3)


class FakeGitHub:
    """The config repo (main + branches), its pulls, the product's latest release, and GraphQL auto-merge."""

    def __init__(self, root: Path, release: tuple[str, str] | None = ("v0.0.1", NEW_SHA)) -> None:
        self.repo = "mcctl-tests/tests-mc-config"
        self.branches: dict[str, dict[str, str]] = {"main": self._files(root)}
        self.pulls: list[dict[str, Any]] = []
        self.release = release
        self.auto_merge: list[str] = []
        self.calls: list[tuple[str, str]] = []
        self.auth: set[str] = set()
        self.product_files: dict[str, bytes] = {}  # v1.5.1: the release's files (contents API at its sha)

    @staticmethod
    def _files(root: Path) -> dict[str, str]:
        return {
            p.relative_to(root).as_posix(): p.read_text()
            for p in root.rglob("*")
            if p.is_file() and ".git" not in p.parts
        }

    def __call__(self, req: http.Request) -> http.Response:
        u = urllib.parse.urlsplit(req.url)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        self.calls.append((req.method, path))
        self.auth.add(req.headers.get("Authorization", ""))
        body = json.loads(req.body) if req.body else None
        r = f"/repos/{self.repo}"

        def ok(data: Any, status: int = 200) -> http.Response:
            return http.Response(status, {}, json.dumps(data).encode())

        if path == "/repos/edvardk/smallworld/releases/latest":
            return ok({"tag_name": self.release[0]}) if self.release else ok({"message": "Not Found"}, 404)
        if self.release and path == f"/repos/edvardk/smallworld/git/ref/tags/{self.release[0]}":
            return ok({"object": {"type": "commit", "sha": self.release[1]}})
        if self.release and path.startswith("/repos/edvardk/smallworld/contents/"):
            name = urllib.parse.unquote(path.removeprefix("/repos/edvardk/smallworld/contents/"))
            if q.get("ref") == [self.release[1]] and name in self.product_files:
                data = base64.b64encode(self.product_files[name]).decode()
                return ok({"encoding": "base64", "content": data})
            return ok({"message": "Not Found"}, 404)
        if path == f"{r}/git/ref/heads/main":
            return ok({"object": {"sha": "c" * 40}})
        if path.startswith(f"{r}/contents/"):
            name = urllib.parse.unquote(path[len(f"{r}/contents/") :])
            if req.method == "GET":
                files = self.branches.get(q.get("ref", ["main"])[0], {})
                if name not in files:
                    return ok({"message": "Not Found"}, 404)
                return ok({"sha": "blob-" + name, "content": base64.b64encode(files[name].encode()).decode()})
            self.branches[body["branch"]][name] = base64.b64decode(body["content"]).decode()
            return ok({"commit": {}})
        if path == f"{r}/git/refs" and req.method == "POST":
            self.branches[body["ref"].removeprefix("refs/heads/")] = dict(self.branches["main"])
            return ok({}, 201)
        if path.startswith(f"{r}/git/refs/heads/") and req.method == "DELETE":
            self.branches.pop(path.rsplit("/heads/", 1)[1], None)
            return http.Response(204, {}, b"")
        if path == f"{r}/pulls" and req.method == "POST":
            pr = {"number": len(self.pulls) + 1, "node_id": f"PR_{len(self.pulls) + 1}", "state": "open",
                  "head": {"ref": body["head"]}, "title": body["title"], "body": body["body"]}  # fmt: skip
            self.pulls.append(pr)
            return ok(pr, 201)
        if path == f"{r}/pulls" and req.method == "GET":
            return ok([p for p in self.pulls if p["state"] == "open"])
        if m := re.fullmatch(rf"{r}/pulls/(\d+)", path):
            self.pulls[int(m.group(1)) - 1]["state"] = body["state"]
            return ok({})
        if re.fullmatch(rf"{r}/issues/\d+/comments", path):
            return ok({}, 201)
        if path == "/graphql":
            self.auto_merge.append(body["variables"]["id"])
            return ok({"data": {"enablePullRequestAutoMerge": {"clientMutationId": None}}})
        return ok({"message": f"unexpected {req.method} {path}"}, 404)


@pytest.fixture
def cfg(tmp_path: Path) -> Path:
    root = make_config_repo(tmp_path / "config", renderable=True)
    wf = root / ".github/workflows/server.yml"
    wf.write_text(wf.read_text().replace("# v0.0.0", "# v0.0.0"))
    return root


def bump_lock(root: Path) -> None:
    p = root / "server.lock.yml"
    p.write_text(p.read_text().replace("build: 2655", "build: 2656"))


def test_a_lock_update_and_a_patch_release_become_auto_merged_prs(cfg) -> None:
    gh = FakeGitHub(cfg)
    bump_lock(cfg)
    repo = updates.Repo(gh.repo, "installation-token", gh)
    log = updates.run(cfg, repo, auto_merge="builds+patch", product_token="prt", today=TODAY)
    assert [p["head"]["ref"] for p in gh.pulls] == ["update/lock-20261103", "update/product-v0.0.1"]
    lock_branch = gh.branches["update/lock-20261103"]
    assert (
        "build: 2656" in lock_branch["server.lock.yml"]
        and lock_branch["plugins.lock.yml"] == gh.branches["main"]["plugins.lock.yml"]
    )
    wf = gh.branches["update/product-v0.0.1"][".github/workflows/server.yml"]
    assert f"server.yml@{NEW_SHA} # v0.0.1" in wf and TEST_SHA not in wf
    assert gh.auto_merge == ["PR_1", "PR_2"]  # builds+patch: both
    assert "Bearer installation-token" in gh.auth and any("opened #2" in line for line in log)
    # a second run with the same builds and release: nothing new
    again = updates.run(
        cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds+patch", product_token="", today=TODAY
    )
    assert len(gh.pulls) == 2 and any("already" in line for line in again)


V150_LOCK = (REPO / "template/client.lock.yml").read_text()


def v150_files() -> dict[str, bytes]:
    """v1.5.0's template lock and its mod jar (Smallworld 1.1.0), as the contents API serves them."""
    return {"template/client.lock.yml": V150_LOCK.encode(), MOD_JAR: (REPO / MOD_JAR).read_bytes()}


def test_the_product_pr_takes_the_releases_in_house_mod_entries(cfg) -> None:
    """v1.5.1, the live case: main's client.lock.yml pins v1.4.1's mod 1.0.0, the release ships 1.1.0. The
    product PR carries main's lock with the release's in-house entry (not the checkout's, which holds this
    run's lock update), read with the read-only product token, and a patch still auto-merges."""
    (cfg / configrepo.CLIENT_LOCK).write_text(V141_CLIENT_LOCK.read_text())
    gh = FakeGitHub(cfg)
    gh.product_files = v150_files()
    checkout = V141_CLIENT_LOCK.read_text().replace("mc26.3-0.9.2-fabric", "mc26.3-0.9.3-fabric", 1)
    (cfg / configrepo.CLIENT_LOCK).write_text(checkout)  # this week's lock update (its own PR)
    repo = updates.Repo(gh.repo, "installation-token", gh)
    log = updates.run(cfg, repo, auto_merge="builds+patch", product_token="prt", today=TODAY)
    assert [p["head"]["ref"] for p in gh.pulls] == ["update/lock-20261103", "update/product-v0.0.1"]
    product = gh.branches["update/product-v0.0.1"]
    assert product[configrepo.CLIENT_LOCK] == V150_LOCK
    assert gh.branches["update/lock-20261103"][configrepo.CLIENT_LOCK] == checkout
    assert gh.auto_merge == ["PR_1", "PR_2"] and "in-house client mod" in gh.pulls[1]["body"]
    assert any("client.lock.yml: v0.0.1's in-house entries (smallworld)" in line for line in log)
    reads = [r for r in gh.calls if r[1].startswith("/repos/edvardk/smallworld/contents/")]
    assert reads and "Bearer prt" in gh.auth


def test_a_release_whose_mod_entries_cant_be_read_gets_a_pin_only_pr(cfg) -> None:
    (cfg / configrepo.CLIENT_LOCK).write_text(V141_CLIENT_LOCK.read_text())
    gh = FakeGitHub(cfg)
    gh.product_files = v150_files() | {MOD_JAR: b"not the jar"}
    log = updates.run(cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds+patch", product_token="",
                      today=TODAY)  # fmt: skip
    assert configrepo.CLIENT_LOCK not in [
        n for n, t in gh.branches["update/product-v0.0.1"].items() if t != gh.branches["main"].get(n)
    ]
    assert any("couldn't take v0.0.1's in-house entries" in line for line in log)
    # a release without in-house mods (or the same entry): nothing to take
    gh2 = FakeGitHub(cfg)
    updates.run(cfg, updates.Repo(gh2.repo, "t", gh2), auto_merge="builds+patch", product_token="",
                today=TODAY)  # fmt: skip
    assert gh2.branches["update/product-v0.0.1"][configrepo.CLIENT_LOCK] == V141_CLIENT_LOCK.read_text()


def test_auto_merge_levels_and_minor_releases(cfg) -> None:
    gh = FakeGitHub(cfg, release=("v0.1.0", NEW_SHA))
    gh2 = FakeGitHub(cfg, release=None)
    bump_lock(cfg)
    updates.run(cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds", product_token="", today=TODAY)
    assert [p["head"]["ref"] for p in gh.pulls] == ["update/lock-20261103", "update/product-v0.1.0"]
    assert gh.auto_merge == ["PR_1"]  # a minor release waits for the admin
    assert "minor/major" in gh.pulls[1]["body"]
    updates.run(cfg, updates.Repo(gh2.repo, "t", gh2), auto_merge="none", product_token="", today=TODAY)
    assert len(gh2.pulls) == 1 and gh2.auto_merge == []


def test_no_auto_merge_without_protect_main(cfg) -> None:
    """p11-review: without the ruleset nothing requires `server / gate`, so auto-merge would merge at once."""
    gh = FakeGitHub(cfg)
    bump_lock(cfg)
    log = updates.run(
        cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds+patch", product_token="", today=TODAY,
        protected=False,
    )  # fmt: skip
    assert len(gh.pulls) == 2 and gh.auto_merge == []
    assert any("protect_main is false" in line for line in log)


def test_a_newer_lock_update_supersedes_the_open_one(cfg) -> None:
    gh = FakeGitHub(cfg, release=None)
    bump_lock(cfg)
    updates.run(cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds", product_token="", today=TODAY)
    p = cfg / "server.lock.yml"
    p.write_text(p.read_text().replace("build: 2656", "build: 2657"))
    updates.run(cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds", product_token="",
                today=TODAY + datetime.timedelta(days=7))  # fmt: skip
    assert [(x["head"]["ref"], x["state"]) for x in gh.pulls] == [
        ("update/lock-20261103", "closed"), ("update/lock-20261110", "open"),
    ]  # fmt: skip
    assert "update/lock-20261103" not in gh.branches


def test_nothing_to_do(cfg) -> None:
    gh = FakeGitHub(cfg, release=("v0.0.0", TEST_SHA))
    log = updates.run(
        cfg, updates.Repo(gh.repo, "t", gh), auto_merge="builds+patch", product_token="", today=TODAY
    )
    assert (
        gh.pulls == [] and any("newest builds" in x for x in log) and any("latest release" in x for x in log)
    )


def test_main_from_env_needs_the_app_and_never_prints_the_key(cfg, monkeypatch) -> None:
    assert "no updater GitHub App" in updates.main_from_env(cfg, {})[0]
    gh = FakeGitHub(cfg, release=None)
    pem = "-----BEGIN RSA PRIVATE KEY-----\nZmFrZQ==\n-----END RSA PRIVATE KEY-----\n"

    def transport(req: http.Request) -> http.Response:
        if req.url.endswith("/installation"):
            return http.Response(200, {}, b'{"id": 7}')
        if req.url.endswith("/app/installations/7/access_tokens"):
            assert json.loads(req.body or b"{}")["permissions"]["workflows"] == "write"
            return http.Response(201, {}, b'{"token": "ghs-fake", "expires_at": "2026-11-03T05:00:00Z"}')
        return gh(req)

    env = {"UPDATER_APP_ID": "42", "UPDATER_APP_KEY": pem, "GITHUB_REPOSITORY": gh.repo}
    log = updates.main_from_env(cfg, env, transport, sign=lambda key, data: b"sig", today=TODAY)
    assert "Bearer ghs-fake" in gh.auth and not any("PRIVATE" in line for line in log)


def test_bump_pin_moves_exactly_the_uses_line() -> None:
    text = (
        (REPO / "template/.github/workflows/server.yml").read_text().replace("__PRODUCT_REPOSITORY__", "o/p")
    )
    text = text.replace("__PRODUCT_SHA__", "a" * 40).replace("__PRODUCT_VERSION__", "v1.0.0")
    new = updates.bump_pin(text, "o/p", NEW_SHA, "v1.0.1")
    assert f"o/p/.github/workflows/server.yml@{NEW_SHA} # v1.0.1" in new
    assert [a for a, b in zip(text.splitlines(), new.splitlines(), strict=True) if a != b] and len(
        text.splitlines()
    ) == len(new.splitlines())
    assert updates.is_patch("v1.0.0", "v1.0.1") and not updates.is_patch("v1.0.0", "v1.1.0")
    assert (
        updates.newer("v1.0.1", "v1.0.0")
        and not updates.newer("v1.0.0", "v1.0.0")
        and updates.newer("v1.0.0", None)
    )


# --- the update slot vs automation.update_day -----------------------------------------------------------------


def test_the_update_schedule_must_be_the_update_day(cfg) -> None:
    wf = cfg / ".github/workflows/server.yml"
    configrepo.load(cfg)  # Tue / "17 4 * * 2"
    wf.write_text(wf.read_text().replace('"17 4 * * 2"', '"17 4 * * 3"'))
    with pytest.raises(configrepo.ConfigError) as e:
        configrepo.load(cfg)
    assert 'use "17 4 * * 2"' in str(e.value)
    assert configrepo.check_update_cron('    - cron: "17 4 * * 0"\n', "Sun") == []


# --- expiry check ----------------------------------------------------------------------------------------------


def expiry_repo(cfg: Path, entries: list[dict], deploy: bool = False) -> configrepo.ConfigRepo:
    (cfg / "expiry.yml").write_text(yaml.safe_dump({"credentials": entries}))
    if deploy:
        text = (cfg / "server.yml").read_text()
        (cfg / "server.yml").write_text(text + "deploy:\n  enabled: true\n")
    return configrepo.load(cfg)


def rdap_and_watchdog(expires: str = "2028-01-01", age: int = 120) -> http.Transport:
    def transport(req: http.Request) -> http.Response:
        if req.url == "https://data.iana.org/rdap/dns.json":
            return http.Response(
                200,
                {},
                json.dumps({"services": [[["internal"], ["https://rdap.example.invalid/"]]]}).encode(),
            )
        if req.url.startswith("https://rdap.example.invalid/domain/"):
            return http.Response(
                200,
                {},
                json.dumps(
                    {"events": [{"eventAction": "expiration", "eventDate": f"{expires}T00:00:00Z"}]}
                ).encode(),
            )
        if req.url.endswith("/status"):
            return http.Response(
                200, {}, json.dumps({"ok": True, "last_run_age_s": age, "checks": {}, "probes": {}}).encode()
            )
        raise AssertionError(req.url)

    return transport


def test_expiry_check_flags_expiring_stale_and_domain(cfg) -> None:
    repo = expiry_repo(cfg, [
        {"name": "cloudflare-admin", "expires_on": "2026-11-20", "checked_at": "2026-11-01"},
        {"name": "github-admin", "expires_on": "2027-06-01", "checked_at": "2026-08-01"},
        {"name": "tailscale-oauth", "expires_on": None, "checked_at": "2026-11-01"},
    ])  # fmt: skip
    f = expirycheck.check(repo, TODAY, rdap_and_watchdog(expires="2026-11-25"))
    text = " | ".join(f.problems)
    assert "cloudflare-admin expires in 17 days" in text and "github-admin was last checked" in text
    assert "tests.mcctl.internal expires in 22 days" in text and "watchdog: skipped" in " ".join(f.notes)
    assert len(f.problems) == 3
    posted: list[tuple[str, str]] = []
    expirycheck.report(repo, f, "https://discord.invalid/hook", lambda u, m: posted.append((u, m)))
    assert posted == [] if not repo.server.alerts.discord else posted[0][0] == "https://discord.invalid/hook"
    (cfg / "server.yml").write_text((cfg / "server.yml").read_text() + "alerts:\n  discord: true\n")
    repo2 = configrepo.load(cfg)
    expirycheck.report(repo2, f, "https://discord.invalid/hook", lambda u, m: posted.append((u, m)))
    assert posted[-1][0] == "https://discord.invalid/hook" and "17 days" in posted[-1][1]


def test_expiry_check_watches_the_watchdog_once_deployed(cfg, monkeypatch) -> None:
    repo = expiry_repo(
        cfg, [{"name": "github-admin", "expires_on": "2027-06-01", "checked_at": "2026-11-01"}], deploy=True
    )
    monkeypatch.setattr(expirycheck.rdap, "expiration", lambda zone, t: datetime.date(2028, 1, 1))
    ok = expirycheck.check(repo, TODAY, rdap_and_watchdog())
    assert ok.problems == [] and any("cron ran 120 s ago" in n for n in ok.notes)
    stale = expirycheck.check(repo, TODAY, rdap_and_watchdog(age=3600))
    assert any("cron isn't running" in p for p in stale.problems)
    empty = expirycheck.check(expiry_repo(cfg, []), TODAY, rdap_and_watchdog())
    assert any("lists no credentials" in p for p in empty.problems)


# --- workflow contracts ----------------------------------------------------------------------------------------


def test_update_and_expiry_jobs_hold_their_secrets_tightly() -> None:
    wf = yaml.safe_load(REUSABLE.read_text())
    # p11-review: the boot test (third-party code) runs in its own job with no secret but the read-only product
    # checkout; the App's key exists only in a later job on a fresh runner, after an API-only re-verification.
    test, upd = wf["jobs"]["update-test"], wf["jobs"]["update"]
    assert test["permissions"] == upd["permissions"] == {"contents": "read"} and "not-yet" not in wf["jobs"]
    assert (
        upd["needs"] == ["validate", "update-test"] and "needs.update-test.result == 'success'" in upd["if"]
    )
    assert "UPDATER" not in json.dumps(test)
    test_secrets = [s for s in test["steps"] if "secrets." in json.dumps(s)]
    assert [s.get("name") for s in test_secrets] == ["Check out the product at the running commit"]
    assert test["steps"][-1]["name"].startswith("Boot test") and "boot_test.py" in test["steps"][-1]["run"]
    locks = next(s for s in test["steps"] if s.get("id") == "locks")
    assert test["steps"].index(locks) < len(test["steps"]) - 1  # the outputs exist before the boot test runs
    assert set(test["outputs"]) == {"changed", "server_lock", "plugins_lock", "client_lock"}  # v1.1: + client
    steps = {s.get("name", s.get("uses", "")): s for s in test["steps"]}
    app_steps = [s for s in upd["steps"] if "UPDATER_APP_KEY" in json.dumps(s)]
    assert len(app_steps) == 1 and app_steps[0]["run"] == 'python -m mcctl update-prs -C "$CONFIG_DIR"'
    assert upd["steps"][-1] is app_steps[0] and "boot_test" not in json.dumps(upd)
    reverify = next(s for s in upd["steps"] if "verify_lock_update.py" in s.get("run", ""))
    assert upd["steps"].index(reverify) < upd["steps"].index(app_steps[0])
    boot = steps["Boot test (the new jar + plugins, throwaway world, until Done + status ping)"]
    # v1.1: a client-pack-only change needs no server boot test
    assert boot["if"] == "steps.locks.outputs.server_changed == 'true'" and "boot_test.py" in boot["run"]
    assert "client.lock.yml" in locks["run"] and "status --porcelain" in locks["run"]
    java = steps["Java for the boot test"]
    assert java["uses"].startswith("actions/setup-java@") and java["with"]["distribution"] == "temurin"
    exp = wf["jobs"]["expiry-check"]
    assert exp["permissions"] == {"contents": "read"}
    assert [s for s in exp["steps"] if "secrets." in json.dumps(s)][-1]["env"][
        "DISCORD_WEBHOOK_URL"
    ] == "${{ secrets.DISCORD_WEBHOOK_URL }}"
    assert "GITHUB_TOKEN" not in json.dumps(exp) and "ADMIN" not in json.dumps(exp)
    text = REUSABLE.read_text()
    assert '"17 4 * * "[0-6]) tasks=\'["update","deploy"]\'' in text


def test_dependabot_automerge_is_minor_and_patch_only() -> None:
    wf = yaml.safe_load(DEPENDABOT.read_text())
    job = wf["jobs"]["automerge"]
    assert wf["permissions"] == {} and job["permissions"] == {"contents": "write", "pull-requests": "write"}
    assert "dependabot[bot]" in job["if"]
    meta, merge = job["steps"]
    assert meta["uses"].startswith("dependabot/fetch-metadata@") and re.search(
        r"@[0-9a-f]{40} #", DEPENDABOT.read_text()
    )
    assert "semver-patch" in merge["if"] and "semver-minor" in merge["if"] and "major" not in merge["if"]
    # v1.0.1: fetch-metadata v3 (Node 24; same outputs): pinned by its full commit SHA with the version comment
    assert (
        "dependabot/fetch-metadata@25dd0e34f4fe68f24cc83900b1fe3fe149efef98 # v3.1.0"
        in DEPENDABOT.read_text()
    )
    assert merge["run"] == 'gh pr merge --auto --squash "$PR_URL"'


@pytest.mark.posix_only  # a `#!/bin/sh` fake java
def test_boot_test_keeps_scanning_the_log_after_done(tmp_path) -> None:
    """p11-review: an error after "Done" (during the ping or the stop) still fails the boot test."""
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import boot_test

    fake = tmp_path / "fake_java.py"
    fake.write_text(
        "import sys\n"
        "print('[Server thread/INFO]: Enabling Chunky v1.5.3', flush=True)\n"
        "print('[Server thread/INFO]: Done (3.2s)! For help, type \"help\"', flush=True)\n"
        "sys.stdin.readline()\n"
        "print('[Server thread/ERROR]: Encountered an unexpected exception', flush=True)\n"
    )
    java = tmp_path / "java"
    java.write_text(f"#!/bin/sh\nexec {sys.executable} {fake}\n")
    java.chmod(0o755)
    import mcctl.mcping

    orig = mcctl.mcping.status
    mcctl.mcping.status = lambda *a, **k: {"version": {"name": "fake"}}
    try:
        problems = boot_test.run(tmp_path, str(java), 25565, ["Chunky"], timeout=30)
    finally:
        mcctl.mcping.status = orig
    assert problems == ["server log: [Server thread/ERROR]: Encountered an unexpected exception"]
