"""P4 acceptance: `mcctl doctor` against mocks (recorded HTTP, fake tools, fake DNS); expiry.yml and state."""

import datetime
import json
import os
import subprocess

import pytest
import yaml
from conftest import REPO, Replay, make_config_repo

from mcctl import cli, configrepo, doctor, hostnet, http, state, tf

NOW = datetime.datetime(2026, 10, 31, 12, tzinfo=datetime.UTC)
SECRETS = {
    "CLOUDFLARE_API_TOKEN": "cf-secret-1",
    "AWS_ACCESS_KEY_ID": "aws-id-2",
    "AWS_SECRET_ACCESS_KEY": "aws-secret-3",
    "AWS_ENDPOINT_URL_S3": "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
    "GITHUB_TOKEN": "gh-secret-4",
    "TF_VAR_product_read_token": "gh-product-5",
    "TAILSCALE_OAUTH_CLIENT_ID": "ts-id-6",
    "TAILSCALE_OAUTH_CLIENT_SECRET": "ts-secret-7",
}
ALL_OK = (
    "cloudflare_ok.json",
    "cloudflare_registrar_forbidden.json",  # the admin token has no registrar permission: RDAP decides
    "cloudflare_zero_trust_ok.json",
    "github_admin_ok.json",
    "github_product_ok.json",
    "tailscale_ok.json",
    "rdap_ok.json",
)


class Tools:
    """Fake `run`: gh logged in, terraform at the tested version, tailscale running, ping per `ping_rc`."""

    def __init__(self, ping_rc: int = 0, tf_version: str = tf.TERRAFORM_VERSION) -> None:
        self.calls: list[list[str]] = []
        self.ping_rc, self.tf_version = ping_rc, tf_version
        self.ip_json = "[]"

    def __call__(self, argv):
        argv = list(argv)
        self.calls.append(argv)
        out, rc = "", 0
        if argv[:2] == ["terraform", "version"]:
            out = json.dumps({"terraform_version": self.tf_version})
        elif argv[:2] == ["tailscale", "status"]:
            out = json.dumps({"BackendState": "Running"})
        elif argv[:2] == ["tailscale", "ping"]:
            rc = self.ping_rc
        elif argv[0] == "ssh" and argv[-1] == hostnet.COMMAND:
            out = self.ip_json
        return subprocess.CompletedProcess(argv, rc, out, "")


def deps(
    *fixtures: str, tools: Tools | None = None, dns: dict | None = None, plan: tf.Plan | None = None
) -> doctor.Deps:
    return doctor.Deps(
        transport=Replay(*(fixtures or ALL_OK)),
        run=tools or Tools(),
        which=lambda name: f"/usr/bin/{name}",
        resolve=lambda host, family: set((dns or {}).get((host, family), set())),
        now=lambda: NOW,
        plan=lambda repo, root: plan or tf.Plan(),  # never a real terraform plan
    )


def write_env(root, values=SECRETS, mode=0o600):
    path = root / ".env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    path.chmod(mode)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    for k in [*SECRETS, "TAILSCALE_API_KEY", "TF_VAR_discord_webhook_url", "GH_TOKEN"]:
        monkeypatch.delenv(k, raising=False)
    root = make_config_repo(tmp_path / "config", renderable=True)
    write_env(root)
    return root


def levels(report) -> dict[str, str]:
    return {r.name: r.level for r in report.results}


def test_doctor_github_rulesets_and_apps(repo) -> None:
    """P10 review: a plan without rulesets fails (the panel App could push to main); missing Apps warn."""
    got = levels(doctor.run(repo, deps=deps()))
    assert (
        got["github rulesets"] == "ok"
        and got["panel github app"] == "warn"
        and got["updater github app"] == "warn"
    )
    free = doctor.run(repo, deps=deps("github_protection_free.json", *ALL_OK))
    assert levels(free)["github rulesets"] == "fail" and "GitHub Pro" in free.format()
    env = dict(SECRETS, TF_VAR_panel_github_app_id="123456", TF_VAR_panel_github_app_private_key="UEVN")
    write_env(repo, env)
    assert levels(doctor.run(repo, deps=deps()))["panel github app"] == "ok"
    text = (repo / "server.yml").read_text().replace("github:\n", "github:\n  protect_main: false\n", 1)
    (repo / "server.yml").write_text(text)
    off = doctor.run(repo, deps=deps())
    assert levels(off)["github rulesets"] == "warn" and "push to main" in off.format()


def test_doctor_all_good_writes_expiry(repo) -> None:
    report = doctor.run(repo, deps=deps())
    got = levels(report)
    assert not report.failed, report.format()
    for name in ("cloudflare token", "github admin token", "product read token", "tailscale oauth client"):
        assert got[name] == "ok", report.format()
    assert got["tool terraform"] == "ok" and got[".env"] == "ok" and got["config repo"] == "ok"
    assert got["dns"] == "skip" and got["tailnet"] == "skip" and got["watchdog"] == "skip"
    assert got["domain expiry"] == "ok"
    # The GitHub admin PAT expires in 20 days: warned.
    assert got["expiry github-admin"] == "warn"
    data = yaml.safe_load((repo / "expiry.yml").read_text())
    assert data == {
        "credentials": [
            {"name": "cloudflare-admin", "expires_on": datetime.date(2027, 3, 1), "checked_at": NOW.date()},
            {"name": "github-admin", "expires_on": datetime.date(2026, 11, 20), "checked_at": NOW.date()},
            {
                "name": "github-product-read",
                "expires_on": datetime.date(2027, 10, 1),
                "checked_at": NOW.date(),
            },
            {"name": "tailscale-oauth", "expires_on": None, "checked_at": NOW.date()},
        ]
    }
    # Names and dates only; the repo still validates; nothing secret anywhere in the output.
    text = (repo / "expiry.yml").read_text() + report.format()
    assert not any(v in text for v in SECRETS.values() if not v.startswith("https://"))
    configrepo.load(repo)
    rec = state.load(repo).get("doctor")
    assert rec["status"] == "done" and rec["updated_at"] == "2026-10-31T12:00:00Z"
    # A second run with the same answers leaves expiry.yml alone.
    report = doctor.run(repo, deps=deps())
    assert "unchanged" in next(r.detail for r in report.results if r.name == "expiry.yml")


def test_doctor_env_mode_and_fix(repo) -> None:
    (repo / ".env").chmod(0o644)
    report = doctor.run(repo, deps=deps())
    assert levels(report)[".env"] == "fail" and report.failed
    report = doctor.run(repo, fix=True, deps=deps())
    assert levels(report)[".env"] == "ok"
    assert (repo / ".env").stat().st_mode & 0o777 == 0o600


def test_doctor_missing_credentials_keep_old_expiry(repo) -> None:
    (repo / "expiry.yml").write_text(
        "credentials:\n  - {name: cloudflare-admin, expires_on: 2027-01-01, checked_at: 2026-09-01}\n"
    )
    write_env(repo, {"GITHUB_TOKEN": "gh-secret-4"})
    report = doctor.run(
        repo, deps=deps("github_admin_ok.json", "github_product_private_anonymous.json", "rdap_ok.json")
    )
    got = levels(report)
    assert got["cloudflare token"] == "fail" and got["state backend"] == "fail"
    assert got["tailscale oauth client"] == "fail" and got["product read token"] == "warn"
    names = {c["name"]: c for c in yaml.safe_load((repo / "expiry.yml").read_text())["credentials"]}
    assert str(names["cloudflare-admin"]["checked_at"]) == "2026-09-01"  # not re-checked: kept
    assert "github-admin" in names
    assert state.load(repo).get("doctor")["status"] == "failed"


def test_doctor_bad_tokens(repo) -> None:
    report = doctor.run(
        repo,
        deps=deps(
            "cloudflare_invalid.json",
            "github_admin_401.json",
            "github_product_ok.json",
            "tailscale_bad_secret.json",
            "rdap_ok.json",
        ),
    )
    got = levels(report)
    assert got["cloudflare token"] == got["github admin token"] == got["tailscale oauth client"] == "fail"
    names = [c["name"] for c in yaml.safe_load((repo / "expiry.yml").read_text())["credentials"]]
    assert names == ["github-product-read"]  # only what was learned


def test_doctor_dns_and_tailnet(tmp_path, monkeypatch) -> None:
    for k in SECRETS:
        monkeypatch.delenv(k, raising=False)
    data = yaml.safe_load((make_config_repo(tmp_path / "x", renderable=True) / "server.yml").read_text())
    data["host"] |= {"ipv4": "9.9.9.9", "ipv6": "2620:fe::fe"}
    data["deploy"] = {"enabled": True}
    root = make_config_repo(tmp_path / "c", data)
    write_env(root)
    import socket

    fqdn = "mc.tests.mcctl.internal"
    dns = {(fqdn, socket.AF_INET): {"9.9.9.9"}, (fqdn, socket.AF_INET6): {"2001:4860:4860::8888"}}
    report = doctor.run(root, deps=deps(tools=Tools(ping_rc=1), dns=dns))
    got = levels(report)
    assert got["dns A"] == "ok" and got["dns AAAA"] == "fail"
    assert got["tailnet"] == "fail"  # deploy.enabled: the box should answer
    tools = Tools()
    report = doctor.run(root, deps=deps(tools=tools, dns=dns))
    assert levels(report)["tailnet"] == "ok"
    assert ["tailscale", "ping", "--c", "1", "--timeout", "5s", "example"] in tools.calls


def test_doctor_discord_and_api_key(repo, monkeypatch) -> None:
    data = yaml.safe_load((repo / "server.yml").read_text())
    data["alerts"] = {"discord": True}
    (repo / "server.yml").write_text(yaml.safe_dump(data))
    monkeypatch.setenv("TAILSCALE_API_KEY", "tskey-api-oneoff")
    report = doctor.run(repo, deps=deps())
    got = levels(report)
    assert got["discord webhook"] == "fail" and got["tailscale api key"] == "warn"
    monkeypatch.setenv(
        "TF_VAR_discord_webhook_url", "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghi-JKL"
    )
    report = doctor.run(repo, deps=deps(*ALL_OK, "discord_ok.json"))
    assert levels(report)["discord webhook"] == "ok"
    assert "abcDEF_ghi-JKL" not in report.format()


def test_doctor_panel_github_login_needs_its_secret(repo, monkeypatch) -> None:
    data = yaml.safe_load((repo / "server.yml").read_text())
    data.setdefault("panel", {})["access"] = {"idp": "github", "github_client_id": "Iv1.0123456789abcdef"}
    (repo / "server.yml").write_text(yaml.safe_dump(data))
    monkeypatch.delenv("TF_VAR_panel_github_client_secret", raising=False)
    assert levels(doctor.run(repo, deps=deps()))["panel github login"] == "fail"
    monkeypatch.setenv("TF_VAR_panel_github_client_secret", "s" * 40)
    report = doctor.run(repo, deps=deps())
    assert levels(report)["panel github login"] == "ok" and "s" * 40 not in report.format()


def test_doctor_terraform_versions() -> None:
    assert doctor.terraform_version_level(tf.TERRAFORM_VERSION)[0] == "ok"
    major, minor, _ = tf.TERRAFORM_VERSION.split(".")
    assert doctor.terraform_version_level(f"{major}.{minor}.99")[0] == "warn"
    assert doctor.terraform_version_level("1.5.7")[0] == "fail"
    assert doctor.terraform_version_level(None)[0] == "fail"


def test_doctor_invalid_config_repo(tmp_path) -> None:
    report = doctor.run(tmp_path, deps=deps())
    assert levels(report)["config repo"] == "fail" and report.failed


def test_doctor_placeholders_fail(tmp_path, monkeypatch) -> None:
    root = make_config_repo(tmp_path / "c")  # example.com etc.
    write_env(root)
    report = doctor.run(root, deps=deps())
    assert levels(report)["config repo"] == "fail" and levels(report)["credentials"] == "skip"


def test_cli_doctor(repo, monkeypatch, capsys) -> None:
    fake = deps()
    monkeypatch.setattr(doctor, "Deps", lambda: fake)
    assert cli.main(["doctor", "-C", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "[  ok] cloudflare token" in out and "cf-secret-1" not in out


def _applied(root) -> None:
    state.load(root).record("apply", "done", now=NOW)


@pytest.mark.parametrize(
    ("fixture", "level", "fragment"),
    [
        ("watchdog_ok.json", "ok", "cron ran 120 s ago; checks and probes fine"),
        ("watchdog_stale.json", "fail", "last cron run 60 min ago (> 15): the cron isn't running"),
        ("watchdog_problems.json", "warn", "backup late, v6 probe failing (TypeError: Specified address"),
    ],
)
def test_doctor_watchdog_freshness(repo, fixture, level, fragment) -> None:
    _applied(repo)
    report = doctor.run(repo, deps=deps(*ALL_OK, fixture))
    result = next(r for r in report.results if r.name == "watchdog")
    assert result.level == level and fragment in result.detail, report.format()
    assert result.detail.startswith("https://hb.mc.tests.mcctl.internal: ")


@pytest.mark.parametrize(
    ("fixture", "level", "fragment"),
    [
        ("watchdog_client_on.json", "ok", "the Worker has the client set up"),
        ("watchdog_client_off.json", "fail", "the Worker has no client set up"),
        ("watchdog_ok.json", "warn", "predates this check"),  # a v1.1 Worker: no `client` flag
    ],
)
def test_doctor_client_page_needs_the_apply(repo, fixture, level, fragment) -> None:
    """v1.1 rollout: client.enabled deployed without `mcctl apply` left the Worker without CLIENT/CLIENT_KEY."""
    _applied(repo)
    assert "client page" not in levels(doctor.run(repo, deps=deps(*ALL_OK, fixture)))  # client off: no check
    data = yaml.safe_load((repo / "server.yml").read_text())
    data["client"] = {"enabled": True}
    (repo / "server.yml").write_text(yaml.safe_dump(data))
    report = doctor.run(repo, deps=deps(*ALL_OK, fixture))
    result = next(r for r in report.results if r.name == "client page")
    assert result.level == level and fragment in result.detail, report.format()
    assert (f"`mcctl apply -C {repo}`" in result.detail) == (level != "ok")


def test_doctor_watchdog_unreachable_after_apply(repo) -> None:
    _applied(repo)

    def transport(req):
        if req.url.startswith("https://hb."):
            raise http.HttpError("hb.mc.tests.mcctl.internal/status: Name or service not known")
        return replay(req)

    replay = Replay(*ALL_OK)
    d = deps()
    d.transport = transport
    report = doctor.run(repo, deps=d)
    result = next(r for r in report.results if r.name == "watchdog")
    assert result.level == "fail" and "unreachable" in result.detail


def test_doctor_zero_trust_and_product_repo(repo) -> None:
    """Setup automation: the Zero Trust organization (missing = fail) and the product repo's settings (skipped
    without an admin GitHub token; recorded fixtures only)."""
    got = levels(doctor.run(repo, deps=deps()))
    assert got["zero trust"] == "ok" and got["product repo"] == "skip"
    fixtures = (
        *(f for f in ALL_OK if f != "cloudflare_zero_trust_ok.json"),
        "cloudflare_zero_trust_missing.json",
    )
    report = doctor.run(repo, deps=deps(*fixtures))
    assert levels(report)["zero trust"] == "fail" and "one.dash.cloudflare.com" in report.format()
    fixtures = (
        *(f for f in ALL_OK if f != "cloudflare_zero_trust_ok.json"),
        "cloudflare_zero_trust_long_session.json",
    )
    report = doctor.run(repo, deps=deps(*fixtures))
    text = report.format()
    assert levels(report)["zero trust"] == "warn" and "global session 24h > 30m" in text
    assert "Access controls > Access settings > Set your global session duration" in text


def test_domain_check_prefers_cloudflare_registrar_when_the_token_may_read_it() -> None:
    """Registrar auto-renew from Cloudflare when readable (recorded fixture); a 403 falls back to RDAP."""
    account, zone = "0123456789abcdef0123456789abcdef", "tests.mcctl.internal"
    today = datetime.date(2026, 11, 1)
    report = doctor.Report()
    deps = doctor.Deps(transport=Replay("cloudflare_registrar_on.json"))
    doctor.check_domain(report, zone, deps, today, ("tok", account))
    assert [(r.name, r.level) for r in report.results] == [("domain expiry", "ok")]
    assert "auto-renew on" in report.results[0].detail
    report = doctor.Report()
    deps = doctor.Deps(transport=Replay("cloudflare_registrar_forbidden.json", "rdap_ok.json"))
    doctor.check_domain(report, zone, deps, today, ("tok", account))
    assert report.results[0].name == "domain expiry" and "registered until" in report.results[0].detail


def test_doctor_checks_the_pinned_ansible_collections(tmp_path) -> None:
    """The running checkout is the pin (tests: MCCTL_PRODUCT_DIR): its collections must be installed too."""
    from mcctl import galaxy

    repo = configrepo.load(make_config_repo(tmp_path / "config", renderable=True))
    report = doctor.Report()
    doctor.check_collections(report, repo, os.environ, deps(), fix=False)
    (row,) = report.results
    assert row.level == "warn" and "not installed" in row.detail and "--fix" in row.detail
    report = doctor.Report()
    doctor.check_collections(report, repo, os.environ, deps(), fix=True)  # conftest's fake ansible-galaxy
    (row,) = report.results
    assert row.level == "ok" and galaxy.describe(galaxy.pins(REPO)) in row.detail


def test_doctor_ci_oidc_subject(repo, monkeypatch) -> None:
    """GitHub's immutable OIDC sub claims (2026-07-15): the tailnet's CI identity must trust the sub GitHub
    issues; a wrong server.yml github.oidc_subject or a stale applied subject fails with the fix."""
    ok = doctor.run(repo, deps=deps())
    row = next(r for r in ok.results if r.name == "ci oidc subject")
    assert row.level == "ok" and "repo:mcctl-tests@2/tests-mc-config@1:ref:refs/heads/main" in row.detail
    # An old repository (name-only sub) while server.yml says immutable: fail, naming the fix.
    old = doctor.run(repo, deps=deps("github_oidc_old_repo.json", *ALL_OK))
    row = next(r for r in old.results if r.name == "ci oidc subject")
    assert row.level == "fail" and "github.oidc_subject: legacy" in row.detail and "403" in row.detail
    # The identity applied earlier for the name-only subject (before the fix): fail, re-apply.
    monkeypatch.setattr(doctor, "check_watchdog", lambda *a: None)  # the apply is "done" below
    st = state.load(repo)
    st.record(
        "apply", "done", outputs={"ci_oidc_subject": "repo:mcctl-tests/tests-mc-config:ref:refs/heads/main"}
    )
    stale = doctor.run(repo, deps=deps())
    row = next(r for r in stale.results if r.name == "ci oidc subject")
    assert row.level == "fail" and "re-run `mcctl setup`" in row.detail
    custom = doctor.run(repo, deps=deps("github_oidc_custom_template.json", *ALL_OK))
    assert levels(custom)["ci oidc subject"] == "fail"


# --- terraform plan clean (no perpetual drift) ---------------------------------------------------------------


APPLIED = (*ALL_OK, "watchdog_ok.json")


def _plan_result(report) -> doctor.Result:
    return next(r for r in report.results if r.name == "terraform plan")


def test_doctor_terraform_plan_clean(repo) -> None:
    assert _plan_result(doctor.run(repo, deps=deps())).level == "skip"  # nothing applied yet
    _applied(repo)
    got = _plan_result(doctor.run(repo, deps=deps(*APPLIED)))
    assert got.level == "ok" and "No changes" in got.detail
    drift = tf.Plan(update=["cloudflare_d1_database.watchdog", "cloudflare_workers_script.watchdog"])
    got = _plan_result(doctor.run(repo, deps=deps(*APPLIED, plan=drift)))
    assert got.level == "warn" and got.detail.startswith(
        "2 pending: cloudflare_d1_database.watchdog, cloudflare_workers_script.watchdog."
    )


def test_doctor_terraform_plan_needs_the_state_credentials(repo) -> None:
    _applied(repo)
    write_env(repo, {k: v for k, v in SECRETS.items() if not k.startswith("AWS_")})
    called: list[str] = []
    d = deps(*APPLIED)
    d.plan = lambda repo, root: called.append("plan") or tf.Plan()
    got = _plan_result(doctor.run(repo, deps=d))
    assert got.level == "skip" and "AWS_ACCESS_KEY_ID" in got.detail and called == []
    d.plan = None  # init's last step
    write_env(repo)
    assert _plan_result(doctor.run(repo, deps=d)).level == "skip"


def test_doctor_terraform_plan_failure_warns(repo) -> None:
    _applied(repo)

    def failing(repo, root):
        raise tf.TfError("Error: Invalid provider configuration\nmore")

    d = deps(*APPLIED)
    d.plan = failing
    got = _plan_result(doctor.run(repo, deps=d))
    assert got.level == "warn" and "couldn't plan: Error: Invalid provider configuration more" in got.detail


def test_terraform_drift_is_read_only(tmp_path, monkeypatch) -> None:
    """`plan -detailed-exitcode -lock=false`; exit 2 = changes (summarised from `show -json`), 0 = none."""
    calls: list[list[str]] = []
    show = {
        "resource_changes": [
            {"address": "cloudflare_r2_bucket_lock.backups", "change": {"actions": ["update"]}}
        ]
    }

    def run(argv, **kw):
        calls.append(list(argv))
        if "plan" in argv:
            return subprocess.CompletedProcess(argv, run.rc, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps(show), "")

    monkeypatch.setattr(tf, "exe", lambda name: name)
    monkeypatch.setattr(subprocess, "run", run)
    t = tf.Terraform(tmp_path, {})
    run.rc = 2
    assert t.drift(tmp_path / "vars.json", tmp_path / "p").update == ["cloudflare_r2_bucket_lock.backups"]
    assert "-lock=false" in calls[0] and "-detailed-exitcode" in calls[0] and not (tmp_path / "p").exists()
    run.rc = 0
    assert t.drift(tmp_path / "vars.json", tmp_path / "p").empty
    run.rc = 1
    with pytest.raises(tf.TfError):
        t.drift(tmp_path / "vars.json", tmp_path / "p")


# --- host.ipv6 vs the host's addresses --------------------------------------------------------------------------


def _bootstrapped(root) -> None:
    state.load(root).record("bootstrap", "done", now=NOW)


def test_doctor_warns_about_an_unset_ipv6_the_host_has(repo) -> None:
    data = yaml.safe_load((repo / "server.yml").read_text())
    data["host"] |= {"ipv4": "9.9.9.9", "ipv6": None}
    (repo / "server.yml").write_text(yaml.safe_dump(data, sort_keys=False))
    _bootstrapped(repo)
    tools = Tools()
    tools.ip_json = (REPO / "tests/fixtures/ip/eui64_slaac.json").read_text()
    got = next(r for r in doctor.run(repo, deps=deps(*APPLIED, tools=tools)).results if r.name == "host ipv6")
    assert got.level == "warn" and "players on IPv6-only networks can't connect" in got.detail
    assert "`mcctl config set host.ipv6 3ffe:ffff:6:1234:5054:ff:fe12:3456` + `mcctl apply`" in got.detail
    assert any(c[0] == "ssh" and c[-1] == "ip -j -6 addr show scope global" for c in tools.calls)
    tools.ip_json = "[]"
    got = next(r for r in doctor.run(repo, deps=deps(*APPLIED, tools=tools)).results if r.name == "host ipv6")
    assert got.level == "ok"
