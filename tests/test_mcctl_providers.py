"""P4 token validators against recorded HTTP fixtures (tests/fixtures/http/, shaped from each provider's
documented responses). No real API is ever called: Replay fails on any unexpected request."""

import base64
import datetime
import json

import pytest
from conftest import Replay

from mcctl import ghapp, http
from mcctl.providers import (
    ProviderError,
    cloudflare,
    discord,
    github,
    parse_date,
    parse_datetime,
    rdap,
    tailscale,
)

NOW = datetime.datetime(2026, 10, 31, 12, tzinfo=datetime.UTC)
TOKEN = "cf-token-value-never-printed"
ZONE = "tests.mcctl.internal"
ACC = "0123456789abcdef0123456789abcdef"
ENDPOINT = f"https://{ACC}.r2.cloudflarestorage.com"


def no_secret(check, *secrets: str) -> None:
    text = " ".join(check.messages) + repr(check.facts)
    for s in secrets:
        assert s not in text


# --- Cloudflare ---------------------------------------------------------------------------------------


def test_cloudflare_ok() -> None:
    t = Replay("cloudflare_ok.json")
    c = cloudflare.check(TOKEN, ZONE, t, now=NOW, s3_endpoint=ENDPOINT)
    assert c.level == "ok", c.messages
    assert c.expiry_known and c.expires_on == datetime.date(2027, 3, 1)
    assert c.facts["account_id"] == ACC
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in t.requests)
    assert all(r.url.startswith(cloudflare.API) for r in t.requests)
    no_secret(c, TOKEN)


def test_cloudflare_endpoint_of_another_account() -> None:
    c = cloudflare.check(
        TOKEN,
        ZONE,
        Replay("cloudflare_ok.json"),
        now=NOW,
        s3_endpoint=f"https://{'f' * 32}.r2.cloudflarestorage.com",
    )
    assert c.level == "fail" and "another account" in " ".join(c.messages)
    c = cloudflare.check(
        TOKEN, ZONE, Replay("cloudflare_ok.json"), now=NOW, s3_endpoint="https://s3.example.net"
    )
    assert c.level == "warn"


@pytest.mark.parametrize(
    ("fixture", "level", "fragment"),
    [
        ("cloudflare_no_workers.json", "fail", "Workers Scripts > Edit (watchdog)"),
        ("cloudflare_no_d1.json", "fail", "Account > D1 > Edit"),
        ("cloudflare_no_token_admin.json", "fail", "User > API Tokens > Edit"),
        ("cloudflare_no_r2.json", "fail", "Workers R2 Storage > Edit"),
        ("cloudflare_no_zone.json", "fail", "no such zone visible"),
        ("cloudflare_invalid.json", "fail", "1000: Invalid API Token"),
        ("cloudflare_disabled.json", "fail", "status is 'disabled'"),
    ],
)
def test_cloudflare_problems(fixture: str, level: str, fragment: str) -> None:
    c = cloudflare.check(TOKEN, ZONE, Replay(fixture), now=NOW)
    assert c.level == level
    assert fragment in " ".join(c.messages)


def test_cloudflare_without_expiry_and_not_yet_valid() -> None:
    c = cloudflare.verify(TOKEN, Replay("cloudflare_no_expiry.json"), NOW)
    assert c.level == "ok" and c.expiry_known and c.expires_on is None
    early = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
    c = cloudflare.verify(TOKEN, Replay("cloudflare_ok.json"), early)
    assert c.level == "fail" and "isn't valid before" in c.messages[0]


# --- GitHub -------------------------------------------------------------------------------------------

REPO = "mcctl-tests/tests-mc-config"
PRODUCT = "edvardk/smallworld"
SHA = "1" * 40


def test_github_admin_ok() -> None:
    t = Replay("github_admin_ok.json")
    c = github.check_admin("ghp-secret", REPO, t)
    assert c.level == "ok", c.messages
    assert c.expires_on == datetime.date(2026, 11, 20)  # GitHub-Authentication-Token-Expiration
    assert t.requests[0].headers["X-GitHub-Api-Version"] == "2022-11-28"
    no_secret(c, "ghp-secret")


def test_github_admin_public_without_secrets_access() -> None:
    c = github.check_admin("t", REPO, Replay("github_admin_public_no_secrets.json"))
    msgs = " ".join(c.messages)
    assert c.level == "fail" and "must be private" in msgs and "Secrets read/write" in msgs
    assert c.expiry_known and c.expires_on is None  # no header = no expiry date


@pytest.mark.parametrize(
    ("fixture", "fragment"),
    [("github_admin_401.json", "HTTP 401"), ("github_admin_404.json", "isn't visible")],
)
def test_github_admin_rejected(fixture: str, fragment: str) -> None:
    c = github.check_admin("t", REPO, Replay(fixture))
    assert c.level == "fail" and fragment in c.messages[0] and not c.expiry_known


def test_github_product_read() -> None:
    c = github.check_product_read("t", PRODUCT, SHA, Replay("github_product_ok.json"))
    assert c.level == "ok" and c.expires_on == datetime.date(2027, 10, 1)
    assert github.repo_is_public(PRODUCT, Replay("github_product_private_anonymous.json")) is False
    t = Replay("github_product_private_anonymous.json")
    github.repo_is_public(PRODUCT, t)
    assert "Authorization" not in t.requests[0].headers


def test_github_apps_have_exactly_their_permissions() -> None:
    """P10 review: the panel App (and the updater) are checked for least privilege with the admin PAT."""
    t = Replay("github_app_ok.json")
    c = ghapp.check_app("admin-secret", "tests-panel", ghapp.PANEL, t)
    assert c.level == "ok", c.messages
    assert t.requests[0].headers["Authorization"] == "Bearer admin-secret"
    no_secret(c, "admin-secret")
    bad = ghapp.check_app("t", "tests-panel", ghapp.PANEL, Replay("github_app_extra.json"))
    text = " ".join(bad.messages)
    assert bad.level == "fail" and "workflows" in text and "administration" in text and "push" in text
    # the updater needs workflows too; the panel's permissions are missing one
    lacks = ghapp.check_app("t", "tests-panel", ghapp.UPDATER, Replay("github_app_ok.json"))
    assert lacks.level == "fail" and "lacks {'workflows': 'write'}" in " ".join(lacks.messages)


def test_github_manifest_conversion_and_installation_token() -> None:
    data = ghapp.convert("abc123code", Replay("github_manifest_conversion.json"))
    assert data["id"] == 123456 and data["slug"] == "tests-panel" and "BEGIN RSA" in data["pem"]
    with pytest.raises(ProviderError):
        ghapp.convert("bad code!", Replay("github_manifest_conversion.json"))
    t = Replay("github_app_installation_token.json")
    signed: list[bytes] = []

    def sign(pem: str, data: bytes) -> bytes:
        signed.append(data)
        return b"signature"

    tok = ghapp.installation_token(
        "123456", "PEM", REPO, ghapp.UPDATER_PERMISSIONS, t, now=1_800_000_000, sign=sign
    )
    assert tok == {"token": "fake-installation-token-for-tests", "expires_at": "2026-11-01T13:00:00Z"}
    header, claims = (json.loads(base64.urlsafe_b64decode(p + "==")) for p in signed[0].decode().split("."))
    assert header == {"alg": "RS256", "typ": "JWT"} and claims == {
        "iat": 1_800_000_000 - 60,
        "exp": 1_800_000_000 + 540,
        "iss": "123456",
    }
    body = json.loads(t.requests[1].body or b"{}")
    assert body == {"repositories": ["tests-mc-config"], "permissions": dict(ghapp.UPDATER_PERMISSIONS)}
    assert t.requests[0].headers["Authorization"].endswith(".c2lnbmF0dXJl")


def test_github_rulesets_plan_probe() -> None:
    assert github.rulesets_available("t", REPO, Replay("github_protection_pro.json")) is True
    assert github.rulesets_available("t", REPO, Replay("github_protection_free.json")) is False


# --- Tailscale ----------------------------------------------------------------------------------------


def test_tailscale_oauth_ok() -> None:
    t = Replay("tailscale_ok.json")
    c = tailscale.check_oauth("client-id", "tskey-client-secret", t)
    assert c.level == "ok" and c.expiry_known and c.expires_on is None
    assert (
        t.requests[0].method == "POST"
        and t.requests[0].body == b"client_id=client-id&client_secret=tskey-client-secret"
    )
    assert t.requests[1].headers["Authorization"] == "Bearer tskey-api-test-access-token"
    no_secret(c, "tskey-client-secret", "tskey-api-test-access-token")


@pytest.mark.parametrize(
    ("fixture", "fragment"),
    [
        ("tailscale_bad_secret.json", "token exchange failed"),
        ("tailscale_no_policy_scope.json", "policy_file"),
    ],
)
def test_tailscale_oauth_problems(fixture: str, fragment: str) -> None:
    c = tailscale.check_oauth("id", "secret", Replay(fixture))
    assert c.level == "fail" and fragment in c.messages[0]


# --- Discord, RDAP ------------------------------------------------------------------------------------

HOOK = "https://discord.com/api/webhooks/123456789012345678/abcDEF_ghi-JKL"


def test_discord_webhook() -> None:
    t = Replay("discord_ok.json")
    assert discord.check_webhook(HOOK, t).level == "ok"
    assert [r.method for r in t.requests] == ["GET"]  # nothing posted
    assert discord.check_webhook(HOOK, Replay("discord_deleted.json")).level == "fail"
    bad = discord.check_webhook("https://evil.example.net/api/webhooks/1/x", Replay())
    assert bad.level == "fail"
    no_secret(discord.check_webhook(HOOK, Replay("discord_ok.json")), "abcDEF_ghi-JKL")


def test_rdap_expiration_and_redirect() -> None:
    assert rdap.expiration(ZONE, Replay("rdap_ok.json")) == datetime.date(2027, 1, 1)
    t = Replay("rdap_redirect.json")
    assert rdap.expiration("example.com", t) == datetime.date(2026, 11, 15)
    assert t.urls()[-1] == "https://rdap.registrar.example.net/domain/example.com"


def test_rdap_unknown_tld() -> None:
    with pytest.raises(rdap.RdapError, match="no RDAP service"):
        rdap.expiration("mc.example.zz", Replay("rdap_ok.json"))


def test_rdap_longest_suffix() -> None:
    boot = {"services": [[["uk"], ["https://a/"]], [["co.uk"], ["https://b"]]]}
    assert rdap.base_url("x.co.uk", boot) == "https://b/"


# --- helpers ------------------------------------------------------------------------------------------


def test_date_parsing() -> None:
    assert parse_date("2026-12-31 00:00:00 UTC") == datetime.date(2026, 12, 31)
    assert parse_date("2027-03-01T00:00:00Z") == datetime.date(2027, 3, 1)
    assert parse_date("soon") is None and parse_date(None) is None
    assert parse_datetime("2027-03-01T00:00:00Z") == datetime.datetime(2027, 3, 1, tzinfo=datetime.UTC)


def test_http_refuses_plain_http() -> None:
    with pytest.raises(http.HttpError, match="non-HTTPS"):
        http.urllib_transport(http.Request("GET", "http://api.example.net/x?token=secret"))
    assert http.where("https://h.example.net/p?token=secret") == "h.example.net/p"


def test_cloudflare_account_owned_token() -> None:
    t = Replay("cloudflare_account_token.json")
    c = cloudflare.check(TOKEN, ZONE, t, now=NOW, s3_endpoint=ENDPOINT)
    assert c.level == "ok", c.messages
    assert c.facts["owner"] == "account" and c.facts["account_id"] == ACC
    assert f"{cloudflare.API}/accounts/{ACC}/tokens/verify" in t.urls()
    assert f"{cloudflare.API}/accounts/{ACC}/tokens/permission_groups" in t.urls()
    assert c.expires_on == datetime.date(2027, 3, 1) and "account-owned" in c.messages[0]


def test_cloudflare_account_token_that_user_verify_accepts_is_still_account_owned() -> None:
    """/user/tokens/verify accepted it, the user permission-group listing says 9109, the account verify accepts
    it: account-owned (not the confusing "needs User > API Tokens > Edit")."""
    t = Replay("cloudflare_account_token_9109.json")
    c = cloudflare.check(TOKEN, ZONE, t, now=NOW)
    assert c.level == "ok", c.messages
    assert c.facts["owner"] == "account" and c.messages[0].startswith("account-owned, active")
    assert t.urls()[-1] == f"{cloudflare.API}/accounts/{ACC}/tokens/permission_groups"
    assert not any("user-owned" in m or "User > API Tokens" in m for m in c.messages)


def test_cloudflare_user_token_says_user_owned() -> None:
    c = cloudflare.check(TOKEN, ZONE, Replay("cloudflare_ok.json"), now=NOW, s3_endpoint=ENDPOINT)
    assert c.facts["owner"] == "user" and c.messages[0].startswith("user-owned, active")


def test_cloudflare_rejected_by_both_endpoints() -> None:
    c = cloudflare.check(TOKEN, ZONE, Replay("cloudflare_invalid.json"), now=NOW)
    assert c.level == "fail" and "Invalid API Token" in c.messages[0]


# --- P7: least privilege (design §7 "Tokens"): warn about extra scopes where the API reports them -----


def test_tailscale_extra_scopes_warn() -> None:
    assert (
        tailscale.extra_scopes(
            "auth_keys devices:core:read devices:posture_attributes federated_keys policy_file"
        )
        == []
    )
    assert tailscale.extra_scopes("policy_file:read auth_keys") == []
    assert tailscale.extra_scopes("all") == ["all"]
    assert tailscale.extra_scopes("auth_keys devices:core dns oauth_keys") == [
        "devices:core",
        "dns",
        "oauth_keys",
    ]
    t = Replay("tailscale_ok.json")
    t.exchanges[0]["response"]["body"]["scope"] = "auth_keys policy_file oauth_keys"
    c = tailscale.check_oauth("id", "secret", t)
    assert c.level == "warn" and any("oauth_keys" in m for m in c.messages)
    assert tailscale.check_oauth("id", "secret", Replay("tailscale_ok.json")).level == "ok"


def test_github_classic_pat_warns() -> None:
    t = Replay("github_admin_ok.json")
    t.exchanges[0]["response"]["headers"]["x-oauth-scopes"] = "repo, workflow"
    c = github.check_admin("t", REPO, t)
    assert c.level == "warn" and any("classic PAT (scopes: repo, workflow)" in m for m in c.messages)
    assert github.check_admin("t", REPO, Replay("github_admin_ok.json")).level == "ok"
