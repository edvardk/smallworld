"""Setup automation (p11-setup): the hand-made Cloudflare admin token (and why it can't be minted), Zero Trust
detection, the Tailscale OAuth client through the API, password managers, host conveniences, the seed file, and
`mcctl product-setup`. Everything against mcctl.mock's fake APIs: nothing real is contacted."""

import datetime
import io
import json
import re
import urllib.parse

import pytest
import yaml
from conftest import REPO, TEST_SHA, Replay

from mcctl import cli, configrepo, doctor, envfile, hostnet, init, mock, productsetup, render, state, ui
from mcctl.providers import cloudflare, tailscale

ANSWERS = yaml.safe_load((REPO / "tests/e2e/answers.yml").read_text())
NOW = datetime.datetime(2026, 11, 1, 12, tzinfo=datetime.UTC)


def run_wizard(root, answers=None, *, deps=None, opts=None):
    out = io.StringIO()
    talk = ui.UI(answers=ANSWERS if answers is None else answers, mock=True, env={}, out=out)
    deps = deps or mock.deps(root)
    rc = init.run(root, talk, deps, opts or init.Options(mock=True, product_sha=TEST_SHA))
    return rc, out.getvalue(), deps


# --- Cloudflare -----------------------------------------------------------------------------------------------


FX_ACC = "0123456789abcdef0123456789abcdef"  # the recorded fixtures' account


def test_token_creation_error_1001_is_explained() -> None:
    """Cloudflare refuses an API-made token that may manage tokens (error 1001, a live run): the message
    says so and how to fix it, wherever mcctl creates a token."""
    t = Replay("cloudflare_token_create_1001.json")
    with pytest.raises(cloudflare.CloudflareError) as e:
        cloudflare.create_r2_token("admin", "account", FX_ACC, "x-tfstate", t)
    msg = str(e.value) + " " + (e.value.fix or "")
    assert "1001: sub-token is not allowed to have permissions to manage other tokens" in msg
    assert "refuses to let a token created through the API hold permissions to manage tokens" in msg
    assert "must be created by hand in the dashboard: Manage Account > Account API Tokens" in msg
    assert t.requests[-1].headers["Authorization"] == "Bearer admin"  # only to Cloudflare, by the admin token


def test_mock_cloudflare_refuses_api_made_token_managers_like_the_real_one() -> None:
    cloud = mock.Cloud()
    ids = {g["name"]: g["id"] for g in cloud.permission_groups()}
    for group in ("Account API Tokens Write", "API Tokens Write"):
        body = {"name": "x", "policies": [{"effect": "allow", "resources": {},
                                           "permission_groups": [{"id": ids[group]}]}]}  # fmt: skip
        resp = cloud(mock.http.Request("POST", f"{cloudflare.API}/accounts/{mock.ACCOUNT}/tokens",
                                       {"Authorization": f"Bearer {mock.ADMIN_TOKEN}"},
                                       json.dumps(body).encode()))  # fmt: skip
        assert resp.status == 400 and resp.json()["errors"][0]["code"] == cloudflare.SUB_TOKEN_ERROR


def test_tokens_mcctl_and_terraform_create_never_manage_tokens() -> None:
    """The admin token is hand-made (a token-manager may not be API-made); what it creates (init's state token,
    Terraform's backup token, `secrets rotate r2`'s replacement) holds only the R2 object permission."""
    t = Replay("cloudflare_token_create_1001.json")
    with pytest.raises(cloudflare.CloudflareError):
        cloudflare.create_r2_token("admin", "account", FX_ACC, "x-tfstate", t)
    body = json.loads(t.requests[-1].body)
    assert body["policies"][0]["permission_groups"] == [{"id": "2efd5506f9c8494dacb1fa10a3e7d5b6"}]
    assert cloudflare.R2_WRITE_GROUP == "Workers R2 Storage Bucket Item Write"
    tf = (REPO / "terraform/cloudflare.tf").read_text()
    assert re.findall(r"permission_groups\s*=\s*(.+)", tf) == ["[{ id = local.r2_write_permission_id }]"]
    assert re.findall(r"^\s*policies\s*=\s*(\S+)", tf, re.M) == ["local.r2_backups_policies"] * 2
    assert 'r2_write_permission_name = "Workers R2 Storage Bucket Item Write"' in tf
    others = [f.name for f in (REPO / "terraform").glob("*.tf") if f.name != "cloudflare.tf"]
    assert not [
        n
        for n in others
        if re.search(r"cloudflare_(api|account)_token\"", (REPO / "terraform" / n).read_text())
    ]


def test_admin_token_lines_spell_out_the_account_token_form() -> None:
    lines = cloudflare.admin_token_lines("example.stream", access=True)
    text = "\n".join(lines)
    assert "Manage Account > Account API Tokens" in text and "Custom token" in text
    rows = [line for line in lines if line.startswith("    ")]
    assert len(rows) == 8 and "Permissions (8 rows" in text
    assert rows[0].strip().startswith("Zone > DNS > Edit") and "Account > Account API Tokens > Edit" in text
    assert "bucket-only R2 credentials" in text  # why the token manages tokens
    assert "Zone Resources: Include > Specific zone > example.stream" in text and "a year" in text
    assert len([x for x in cloudflare.admin_token_lines("z", access=False) if x.startswith("    ")]) == 6


def test_duration_minutes() -> None:
    assert cloudflare.duration_minutes("24h") == 1440 and cloudflare.duration_minutes("2h45m") == 165
    assert cloudflare.duration_minutes("10m") == 10 and cloudflare.duration_minutes("30s") == 0.5
    assert cloudflare.duration_minutes("") is None and cloudflare.duration_minutes("10 minutes") is None


def test_zero_trust_missing_pauses_then_resumes(tmp_path) -> None:
    root = tmp_path / "c"
    cloud = mock.Cloud(zero_trust=False)
    rc, out, _ = run_wizard(root, ANSWERS | {"zero_trust_enrolled": False}, deps=mock.deps(root, cloud=cloud))
    assert rc == 0 and "Paused at zero-trust (waiting_for_zero_trust)" in out
    assert cloudflare.ZERO_TRUST_URL in out and "[~]  5 Cloudflare Zero Trust" in out
    assert "sign up for Cloudflare Zero Trust" in out  # still on the human checklist
    cloud.zero_trust = True  # the admin signs up in the dashboard
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=cloud))
    assert rc == 0, out
    assert "Zero Trust team mock-team.cloudflareaccess.com" in out and "global session 24h -> 15m" in out


def test_zero_trust_not_needed_without_the_cloudflare_panel(tmp_path) -> None:
    root = tmp_path / "c"
    rc, out, _ = run_wizard(root, ANSWERS | {"set": {"jvm.heap_mb": 1024, "panel.mode": "off"}})
    assert rc == 0, out
    assert "not needed (panel.mode: off)" in out


# --- Tailscale -------------------------------------------------------------------------------------------------


def test_tailscale_oauth_client_body() -> None:
    cloud = mock.Cloud()
    cid, secret = tailscale.create_oauth_client(
        ui.MOCK_SECRETS["tailscale_api_key"], "mcctl lh/terraform!", cloud
    )
    body = json.loads(cloud.requests[-1].body)
    assert body["keyType"] == "client" and body["tags"] == ["tag:mc-terraform"]
    assert set(body["scopes"]) == tailscale.EXPECTED_SCOPES and re.fullmatch(
        r"[A-Za-z0-9 -]+", body["description"]
    )
    assert cloud.ts_clients == {cid: secret}


def test_tailscale_falls_back_to_the_console_when_the_api_refuses(tmp_path) -> None:
    class NoClients(mock.Cloud):
        def __call__(self, req):
            if req.method == "POST" and req.url.endswith("/tailnet/-/keys"):
                self.requests.append(req)
                return mock._json({"message": "forbidden"}, 403)
            return super().__call__(req)

    root = tmp_path / "c"
    rc, out, _ = run_wizard(root, deps=mock.deps(root, cloud=NoClients()))
    assert rc == 0, out
    assert "create it in the admin console instead" in out
    assert envfile.load(root)["TAILSCALE_OAUTH_CLIENT_ID"] == "mock-tailscale_oauth_client_id"


# --- restic password -> password manager ---------------------------------------------------------------------


def test_restic_password_goes_to_1password_on_stdin_only(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.which = lambda name: f"/mock/bin/{name}"  # op (and bw) installed
    fed: list[tuple[list[str], str]] = []
    deps.run_input = lambda argv, stdin: (fed.append((list(argv), stdin)), deps.tools(argv))[1]
    answers = {k: v for k, v in ANSWERS.items() if k != "restic_password_stored"}
    rc, out, _ = run_wizard(root, answers | {"restic_password_manager": True}, deps=deps)
    assert rc == 0, out
    ((argv, stdin),) = fed
    assert argv == ["op", "item", "create", "-"]
    item = json.loads(stdin)
    assert (
        item["title"] == "mcctl e2e restic"
        and item["fields"][0]["value"] == "mock-restic-password-not-a-secret"
    )
    assert "mock-restic-password-not-a-secret" not in out
    assert not any("mock-restic-password" in " ".join(c) for c in deps.tools.calls)  # never on argv
    assert state.load(root).get("apply")["outputs"]["restic_password"] == "password manager"


def test_restic_password_bitwarden(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.which = lambda name: None if name == "op" else f"/mock/bin/{name}"
    fed: list[list[str]] = []
    deps.run_input = lambda argv, stdin: (fed.append(list(argv)), deps.tools(argv))[1]
    rc, out, _ = run_wizard(root, ANSWERS | {"restic_password_manager": True}, deps=deps)
    assert rc == 0, out
    assert fed == [["bw", "create", "item"]] and "saved to Bitwarden (bw)" in out


# --- host conveniences ------------------------------------------------------------------------------------------

PROBE = 'ID=debian\nVERSION_ID="13"\n@@\nx86_64\n@@\nMemTotal: 8131452 kB\n@@\n'


IP_FIXTURES = REPO / "tests/fixtures/ip"  # recorded `ip -j -6 addr show scope global` output


def test_global_ipv6_detection() -> None:
    v6 = "2: eth0 inet6 3ffe:ffff:6:1::1/64 scope global \n2: eth0 inet6 fd00::1/64 scope global\n"
    assert init.global_ipv6(PROBE + v6) == ["3ffe:ffff:6:1::1"]  # the old `ip -o` text still parses
    assert init.global_ipv6(PROBE) == [] and init.global_ipv6("junk") == []
    assert "ip -j -6 addr show scope global" in init.PROBE_SCRIPT


def test_ipv6_detection_finds_eds_slaac_address() -> None:
    """A live host: an EUI-64 SLAAC address without dynamic/temporary flags, plus tailscale0's fd7a: ULA."""
    out = (IP_FIXTURES / "eui64_slaac.json").read_text()
    assert init.global_ipv6(PROBE + out) == ["3ffe:ffff:6:1234:5054:ff:fe12:3456"]
    assert init.check_host(PROBE + out, "3ffe:ffff:6:1234:5054:ff:fe12:3456") == []


def test_ipv6_detection_skips_temporary_and_deprecated_and_prefers_stable() -> None:
    got = hostnet.public_ipv6((IP_FIXTURES / "privacy.json").read_text())
    # static first, then the stable SLAAC (EUI-64, mngtmpaddr); never the privacy/deprecated ones or the ULA
    assert got == ["3ffe:ffff:c0c:1::7", "3ffe:ffff:c0c:1:5054:ff:fe12:3456"]
    link_local = '[{"ifname":"eth0","addr_info":[{"family":"inet6","local":"fe80::1","scope":"link"}]}]'
    assert hostnet.public_ipv6(link_local) == [] and hostnet.public_ipv6("[not json") == []
    assert hostnet.public_ipv6("3: tailscale0 inet6 fd7a:115c:a1e0::1/128 scope global") == []


def test_several_ipv6_addresses_are_asked_for(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    deps.tools.extra_ipv6 = ["3ffe:ffff:6:1::1", "3ffe:ffff:6:1::2"]  # type: ignore[attr-defined]
    rc, out, _ = run_wizard(root, ANSWERS | {"host_ipv6_pick": "3ffe:ffff:6:1::2"}, deps=deps)
    assert rc == 0, out
    assert configrepo.load(root).server.host.ipv6 == "3ffe:ffff:6:1::2"


def test_ipv6_is_detected_on_the_host_and_the_key_shown(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    real = deps.tools.__call__

    def tools(argv):
        proc = real(argv)
        if argv[0] == "ssh" and argv[-1] == init.PROBE_SCRIPT and "root@" in " ".join(argv):
            proc.stdout = proc.stdout.replace(
                "@@\n[]", "@@\n" + (IP_FIXTURES / "eui64_slaac.json").read_text()
            )
        return proc

    deps.run = tools
    rc, out, _ = run_wizard(root, ANSWERS | {"host_ipv6": ""}, deps=deps)
    assert rc == 0, out
    assert configrepo.load(root).server.host.ipv6 == "3ffe:ffff:6:1234:5054:ff:fe12:3456"
    assert (
        "IPv6 3ffe:ffff:6:1234:5054:ff:fe12:3456 detected on the host" in out and "ssh-ed25519 AAAAC3" in out
    )


def test_ssh_copy_id_when_the_key_isnt_installed(tmp_path) -> None:
    root = tmp_path / "c"
    deps = mock.deps(root)
    real, attempts, streamed = deps.tools.__call__, [], []

    def tools(argv):
        if argv[0] == "ssh" and "root@9.9.9.9" in argv:
            attempts.append(argv)
            if len(attempts) == 1:
                return mock._proc(argv, 255, "", "root@9.9.9.9: Permission denied (publickey,password).")
        return real(argv)

    deps.run = tools
    deps.stream = lambda argv: (streamed.append(list(argv)), 0)[1]
    rc, out, _ = run_wizard(root, ANSWERS | {"ssh_copy_id": True}, deps=deps)
    assert rc == 0, out
    assert ["ssh-copy-id", "-o", "StrictHostKeyChecking=yes", "root@9.9.9.9"] in streamed


# --- seed file + `mcctl setup` ---------------------------------------------------------------------------------


def test_seed_keeps_its_values_and_asks_for_the_placeholders(tmp_path) -> None:
    root = tmp_path / "lighthouse-mc-config"
    keep = (
        "repo",
        "name",
        "zone",
        "hostname",
        "timezone",
        "minecraft_version",
        "border_overworld",
        "border_nether",
    )
    answers = {k: v for k, v in ANSWERS.items() if k not in keep} | {
        "repo": "lighthouse-admin/lighthouse-mc-config"
    }
    answers |= {
        "tailscale_login": "sam@github",
        "email": "sam@tests.mcctl.internal",
        "confirm_apply": "lighthouse",
    }
    # the full example with its admin left at placeholders (as a seed written before the admin's accounts exist)
    text = (REPO / "examples/full/server.yml").read_text()
    text = text.replace("keeper@github", "sam@example.com").replace(
        "keeper@lighthouse.internal", "sam@example.com"
    )
    # a seed is server.yml alone (no art/)
    text = re.sub(r"\n *(icon|texture|glyph): art/\S+\.png[^\n]*", "", text)
    seed = tmp_path / "seed.yml"
    seed.write_text(text)
    deps = mock.deps(root)
    deps.cloud.zone = "lighthouse.internal"
    rc, out, _ = run_wizard(
        root, answers, deps=deps, opts=init.Options(mock=True, product_sha=TEST_SHA, seed=seed)
    )
    assert rc == 0, out
    cfg = configrepo.load(root).server
    assert (cfg.name, cfg.dns.zone, cfg.host.tailscale_name, cfg.backups.bucket) == (
        "lighthouse", "lighthouse.internal", "lighthouse-mc", "lighthouse-backups",
    )  # fmt: skip
    assert (cfg.admin.tailscale_login, cfg.admin.email) == ("sam@github", "sam@tests.mcctl.internal")
    assert "yaml-language-server" not in (root / "server.yml").read_text()
    assert "server.yml seeded from" in out


def test_setup_is_init_with_a_seed(tmp_path, capsys) -> None:
    rc = cli.main([
        "setup", "-C", str(tmp_path / "cfg"), "--answers", str(REPO / "tests/e2e/answers.yml"), "--non-interactive",
        "--mock", "--product-sha", TEST_SHA,
    ])  # fmt: skip
    assert rc == 0 and "Checklist:" in capsys.readouterr().out
    assert cli.main(["setup", "--seed", str(tmp_path / "nope.yml"), "-C", str(tmp_path / "x")]) == 2


# --- product-setup ---------------------------------------------------------------------------------------------

PRODUCT = configrepo.PRODUCT_REPOSITORY


def test_product_setup_plan_apply_idempotent() -> None:
    cloud = mock.Cloud()
    p = productsetup.plan("t", PRODUCT, cloud)
    assert {c.name for c in p.changes} == {
        "allow_auto_merge", "delete_branch_on_merge", "actions access", "actions may create pull requests",
        "dependabot alerts", "dependabot security updates", "ruleset on main", "ruleset on v* tags",
    }  # fmt: skip
    # a private personal repository: GitHub has no forking setting (422 on org-only), so it's a note
    assert any("only collaborators can fork" in n for n in p.notes)
    productsetup.apply(p)
    st = cloud.gh_settings[PRODUCT]
    assert st["access_level"] == "user" and st["workflow"] == {
        "default_workflow_permissions": "read",
        "can_approve_pull_request_reviews": True,
    }
    rs, tags = st["rulesets"].values()
    # security review: moving or deleting a v* tag only for the admin
    assert tags["target"] == "tag" and tags["conditions"]["ref_name"]["include"] == ["refs/tags/v*"]
    assert [r["type"] for r in tags["rules"]] == ["update", "deletion"]
    assert tags["bypass_actors"] == [{"actor_id": 5, "actor_type": "RepositoryRole", "bypass_mode": "always"}]
    # v1.5.1: creating one isn't restricted (autotag.yml tags with GITHUB_TOKEN; GitHub refuses the Actions app
    # as a bypass actor on a personal repository, 422), so no ruleset carries a creation rule
    assert not any(
        r["type"] == "creation" for ruleset in st["rulesets"].values() for r in ruleset.get("rules", [])
    )
    assert not hasattr(productsetup, "desired_tag_create_ruleset")
    assert rs["bypass_actors"] == []  # the admin doesn't bypass main's checks either
    assert rs["rules"][3]["type"] == "pull_request"
    assert rs["rules"][3]["parameters"]["required_approving_review_count"] == 0
    checks = rs["rules"][2]["parameters"]["required_status_checks"]
    assert [c["context"] for c in checks] == list(productsetup.REQUIRED_CHECKS)
    assert {c["integration_id"] for c in checks} == {15368}
    again = productsetup.plan("t", PRODUCT, cloud)
    assert again.changes == [] and len(again.in_sync) == 8
    # drift in the ruleset is noticed and fixed in place (PUT, same id)
    rs["rules"] = rs["rules"][:2]
    drift = productsetup.plan("t", PRODUCT, cloud)
    assert [c.name for c in drift.changes] == ["ruleset on main"]
    productsetup.apply(drift)
    assert list(st["rulesets"]) == [rs["id"], tags["id"]]
    assert len(st["rulesets"][rs["id"]]["rules"]) == 4


def test_product_setup_public_repo_secret_scanning() -> None:
    """A public product: secret scanning and push protection on (security_and_analysis, PATCH /repos); the
    private-only settings (Actions access, forking) are notes."""
    cloud = mock.Cloud()
    cloud.gh_settings[PRODUCT] = {
        "private": False,
        "security_and_analysis": {"secret_scanning": {"status": "enabled"}},
    }
    p = productsetup.plan("t", PRODUCT, cloud)
    names = {c.name for c in p.changes}
    assert "secret scanning push protection" in names and "secret scanning" not in names
    assert "secret scanning: enabled" in p.in_sync
    assert "actions access" not in names and "allow_forking" not in names
    productsetup.apply(p)
    analysis = cloud.gh_settings[PRODUCT]["security_and_analysis"]
    assert analysis == {
        "secret_scanning": {"status": "enabled"},
        "secret_scanning_push_protection": {"status": "enabled"},
    }
    patches = [json.loads(r.body) for r in cloud.requests if r.method == "PATCH" and r.url.endswith(PRODUCT)]
    assert {"security_and_analysis": {"secret_scanning_push_protection": {"status": "enabled"}}} in patches
    assert productsetup.plan("t", PRODUCT, cloud).changes == []
    # a private product: not offered (it needs a paid plan there), only noted
    private = productsetup.plan("t", PRODUCT, mock.Cloud())
    assert any("secret scanning" in n for n in private.notes)
    assert not any("secret scanning" in c.name for c in private.changes)


def test_product_setup_refuses_a_non_admin() -> None:
    class NotAdmin(mock.Cloud):
        def __call__(self, req):
            if req.url == f"https://api.github.com/repos/{PRODUCT}":
                return mock._json({"full_name": PRODUCT, "private": True, "permissions": {"admin": False}})
            return super().__call__(req)

    with pytest.raises(productsetup.SetupError, match="aren't an admin"):
        productsetup.plan("t", PRODUCT, NotAdmin())


def test_product_setup_token_sources() -> None:
    calls = []

    def run(argv):
        calls.append(list(argv))
        return mock._proc(argv, 0, "gho_from_gh\n")

    assert productsetup.token({"GH_TOKEN": "env-token"}, run, None) == "env-token" and calls == []
    assert productsetup.token({}, run, None) == "gho_from_gh" and calls == [["gh", "auth", "token"]]
    assert productsetup.token({}, lambda a: mock._proc(a, 1), lambda: "typed") == "typed"


def test_product_setup_cli_check_and_apply(monkeypatch, capsys) -> None:
    cloud = mock.Cloud()
    monkeypatch.setattr("mcctl.http.urllib_transport", cloud)
    monkeypatch.setenv("GH_TOKEN", "gh-secret-token")
    assert cli.main(["product-setup", "--check"]) == 1
    out = capsys.readouterr().out
    assert "[->] allow_auto_merge: False -> True" in out and "gh-secret-token" not in out
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))
    assert cli.main(["product-setup"]) == 0
    assert cli.main(["product-setup", "--check"]) == 0
    assert "in sync" in capsys.readouterr().out


def test_ruleset_check_names_match_the_workflows() -> None:
    ci = yaml.safe_load((REPO / ".github/workflows/ci.yml").read_text())
    e2e = yaml.safe_load((REPO / ".github/workflows/e2e-mock.yml").read_text())
    server = yaml.safe_load((REPO / ".github/workflows/server.yml").read_text())
    names = set(ci["jobs"]) - {"config-repo"} | set(e2e["jobs"])
    names |= {f"config-repo / {server['jobs']['validate'].get('name', 'validate')}"}
    assert set(productsetup.REQUIRED_CHECKS) <= names
    # update-lock opens PRs with GITHUB_TOKEN (no pull_request event): it must dispatch every required workflow
    text = (REPO / ".github/workflows/update-lock.yml").read_text()
    assert "gh workflow run ci.yml" in text and "gh workflow run e2e-mock.yml" in text
    tf = (REPO / "terraform/github.tf").read_text()
    assert f"github_actions_app_id = {productsetup.GITHUB_ACTIONS_APP_ID}" in tf


def test_doctor_product_repo_check(config_repo) -> None:
    from mcctl import doctor

    repo = configrepo.load(config_repo)
    cloud = mock.Cloud()
    d = doctor.Deps(transport=cloud, run=lambda argv: mock._proc(argv, 0, "gho_mock\n"))
    report = doctor.Report()
    doctor.check_product_repo(report, repo, {}, d)
    assert report.results[-1].level == "warn" and "mcctl product-setup" in report.results[-1].detail
    productsetup.apply(productsetup.plan("t", repo.pin.repository, cloud))
    doctor.check_product_repo(report, repo, {}, d)
    assert report.results[-1].level == "ok"


# --- account-owned admin token (the recommended path) -----------------------------------------------------------

ACCT_TOKEN = "mock-cloudflare_api_token"  # what ui.secret returns for `cloudflare_api_token` with --mock


def _account_policies(*extra: str, zone: str = mock.ZONE_ID) -> list[dict]:
    names = ["DNS Write", "Zone Read", "Workers R2 Storage Write", "Workers Scripts Write", "D1 Write",
             "Account API Tokens Write", "Access: Apps and Policies Write",
             "Access: Organizations, Identity Providers, and Groups Write", *extra]  # fmt: skip
    return [
        {"effect": "allow", "resources": {f"com.cloudflare.api.account.zone.{zone}": "*"},
         "permission_groups": [{"id": "z", "name": n} for n in names[:2]]},
        {"effect": "allow", "resources": {f"com.cloudflare.api.account.{mock.ACCOUNT}": "*"},
         "permission_groups": [{"id": "a", "name": n} for n in names[2:]]},
    ]  # fmt: skip


def _account_deps(root, policies: list[dict]):
    deps = mock.deps(root)
    deps.cloud.tokens[mock.ADMIN_ID] = ACCT_TOKEN
    deps.cloud.account_owned.add(ACCT_TOKEN)
    deps.cloud.token_policies[mock.ADMIN_ID] = policies
    return deps


def test_init_with_an_account_owned_token_records_the_owner_and_audits_it(tmp_path) -> None:
    root = tmp_path / "cfg"
    rc, out, deps = run_wizard(root, deps=_account_deps(root, _account_policies()))
    assert rc == 0, out
    assert "dash.cloudflare.com/?to=/:account/api-tokens" in out and "Specific zone" in out
    assert "exactly the permissions mcctl needs" in out
    assert configrepo.load(root).server.cloudflare.token_owner == "account"
    assert render.tfvars(configrepo.load(root).server, "o/r")["cloudflare_token_owner"] == "account"
    urls = [r.url for r in deps.cloud.requests]
    assert not any(u.endswith("/user/tokens") for u in urls)  # the state token is account-owned too
    assert any(u.endswith(f"/accounts/{mock.ACCOUNT}/tokens") for u in urls)
    assert not any("bootstrap" in u for u in urls) and "switch auto-renew on in the dashboard" in out
    assert ACCT_TOKEN not in out


def test_init_warns_about_extra_permissions_and_wide_scope(tmp_path) -> None:
    root = tmp_path / "cfg"
    policies = _account_policies("Workers KV Storage Write", zone="*")
    rc, out, _ = run_wizard(root, deps=_account_deps(root, policies))
    assert rc == 0, out
    assert "'workers kv storage write', which mcctl doesn't need" in out
    assert "all zones of the account" in out


def test_token_form_names_the_look_alikes(tmp_path) -> None:
    """Live: the form also offers "Access: Apps", "Zone DNS Settings" & co., which the admin picked by mistake."""
    lines = "\n".join(cloudflare.admin_token_lines("example.stream", access=True))
    assert "the combined Access: Apps and Policies, not Access: Apps or Access: Policies" in lines
    assert "Zone > DNS, not Zone DNS Settings" in lines and "FIRST dropdown set to Zone" in lines
    assert "not Access: Organizations or Access: Identity Providers" in lines
    assert "Access: Apps" not in "\n".join(cloudflare.admin_token_lines("example.stream", access=False))


def test_audit_suggests_the_row_a_look_alike_was_meant_to_be(tmp_path) -> None:
    root = tmp_path / "cfg"
    policies = _account_policies("Access: Apps Write", "Zone DNS Settings Write")
    policies[1]["permission_groups"] = [
        g for g in policies[1]["permission_groups"] if g["name"] != "Access: Apps and Policies Write"
    ]
    rc, out, deps = run_wizard(root, deps=_account_deps(root, policies))
    assert rc == 0, out
    assert "lacks 'access: apps and policies write'" in out
    assert (
        "'access: apps write', a look-alike of the row mcctl needs: replace it with "
        "Account > Access: Apps and Policies > Edit"
    ) in out
    assert "replace it with Zone > DNS > Edit" in out
    assert cloudflare.lookalike("Zone Settings Read") == "Zone > Zone > Read"
    assert cloudflare.lookalike("Zone Read") is None and cloudflare.lookalike("DNS Write") is None

    # `mcctl doctor --cloudflare-permissions` (replaces scripts/cf_permission_groups.py): same findings
    d = doctor.Deps()
    d.transport = deps.cloud
    report = doctor.cloudflare_permissions(root, d)
    text = report.format()
    assert "Account > Access: Apps and Policies > Edit  = 'Access: Apps and Policies Write' [account]" in text
    assert "Zone > DNS > Edit  = 'DNS Write' [zone]" in text
    assert "don't pick: Access: Apps Write, DNS View Read, Zone DNS Settings Write" in text
    assert (
        "has 'access: apps write': a look-alike, replace it with Account > Access: Apps and Policies" in text
    )
    assert report.failed and ACCT_TOKEN not in text
    deps.cloud.token_policies[mock.ADMIN_ID] = _account_policies()
    report = doctor.cloudflare_permissions(root, d)
    assert not report.failed and "exactly the rows above" in report.format()


def test_audit_policies_names_missing_groups() -> None:
    pols = _account_policies()
    pols[1]["permission_groups"] = [g for g in pols[1]["permission_groups"] if g["name"] != "D1 Write"]
    missing, extra, scope = cloudflare.audit_policies(pols, mock.ZONE_ID, mock.ACCOUNT, access=True)
    assert missing == ["d1 write"] and extra == [] and scope == []
    _, extra, _ = cloudflare.audit_policies(_account_policies(), mock.ZONE_ID, mock.ACCOUNT, access=False)
    assert len(extra) == 2  # the Access groups aren't needed without the cloudflare panel


def test_account_token_url_prefills_the_needed_permission_keys() -> None:
    url = cloudflare.token_url("mcctl lh (admin)", access=True)
    keys = json.loads(urllib.parse.unquote(url.split("permissionGroupKeys=")[1].split("&")[0]))
    assert [k["key"] for k in keys] == ["dns", "zone", "workers_r2", "workers_scripts", "d1",
                                        "account_api_tokens", "access", "access_acct"]  # fmt: skip
    assert "accountId" not in url  # account URLs choose the resources in the form
