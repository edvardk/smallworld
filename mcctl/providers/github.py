"""GitHub fine-grained PATs (design §3, §4 init step 5, `doctor`).

- `GET /repos/{owner}/{repo}`: the token sees the repository (Metadata read); `private` must be true for a
  config repo (sharing the private product's workflows only works with private repos).
- `GET /repos/{owner}/{repo}/actions/secrets/public-key`: Secrets read (Terraform writes the Actions secrets).
- `GET /repos/{owner}/{repo}/commits/{sha}`: Contents read on the product, and the pinned commit exists.
- P10 review: `GET /repos/{owner}/{repo}/branches/main/protection` tells whether the plan allows rulesets /
  branch protection on the (private) config repo. The panel and the update job use GitHub Apps
  (mcctl/ghapp.py).
- The `sub` claim of the config repo's Actions OIDC tokens (`oidc_subject_info`, docs.github.com "OpenID
  Connect reference" and REST "GitHub Actions OIDC"): `GET /repos/{owner}/{repo}` (`id`, `owner.id`,
  `owner.type`, `created_at`), `GET /repos/{owner}/{repo}/actions/oidc/customization/sub` (`use_default`,
  `use_immutable_subject`) and, for an organization, `GET /orgs/{org}/actions/oidc/customization/sub`
  (`use_immutable_subject`). Repositories created (or renamed/transferred) since 2026-07-15 or opted in use
  `repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:...`; older ones `repo:OWNER/REPO:ref:...`.
- Expiry: the `GitHub-Authentication-Token-Expiration` response header (e.g. `2026-12-31 00:00:00 UTC`), sent
  only for tokens that expire (GitHub changelog 2021-07-26). Absent = no expiry date.
"""

from __future__ import annotations

import datetime
from dataclasses import dataclass
from typing import Literal

from mcctl import http
from mcctl.providers import CredentialCheck, parse_date

API = "https://api.github.com"
EXPIRY_HEADER = "github-authentication-token-expiration"
ADMIN = "github-admin"
PRODUCT_READ = "github-product-read"


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _repo(check: CredentialCheck, token: str, repository: str, transport: http.Transport) -> dict | None:
    resp = http.get(transport, f"{API}/repos/{repository}", _headers(token))
    if resp.status == 401:
        check.fail("token rejected (HTTP 401): expired, revoked or mistyped")
        return None
    if resp.status in (403, 404):
        check.fail(
            f"{repository} isn't visible to this token (HTTP {resp.status}): select the repository in the "
            "token's repository access"
        )
        return None
    data = resp.json()
    if resp.status != 200 or not isinstance(data, dict):
        check.fail(f"GET /repos/{repository} failed (HTTP {resp.status})")
        return None
    header = resp.header(EXPIRY_HEADER)
    check.expires(parse_date(header) if header else None)
    # Classic PATs report their scopes in X-OAuth-Scopes and reach every repository of the account; a
    # fine-grained PAT (no scopes header) can be limited to one repository (design §7 "Tokens").
    scopes = resp.header("X-OAuth-Scopes")
    if scopes and scopes.strip():
        check.warn(
            f"a classic PAT (scopes: {scopes.strip()}) reaches every repository of the account: use a "
            f"fine-grained PAT limited to {repository}"
        )
    return data


def check_admin(token: str, repository: str, transport: http.Transport) -> CredentialCheck:
    """The config repo's admin PAT (Terraform's GitHub provider: Actions secrets)."""
    check = CredentialCheck(ADMIN)
    data = _repo(check, token, repository, transport)
    if data is None:
        return check
    if data.get("private") is not True:
        check.fail(f"{repository} is public: the config repo must be private")
    resp = http.get(transport, f"{API}/repos/{repository}/actions/secrets/public-key", _headers(token))
    if resp.status != 200:
        check.fail(
            f"can't read Actions secrets of {repository} (HTTP {resp.status}): needs Secrets read/write"
        )
    if check.level != "fail":
        when = f"expires {check.expires_on}" if check.expires_on else "no expiry date"
        check.info(f"{repository}: private, Secrets readable, {when}")
    return check


def check_product_read(token: str, repository: str, sha: str, transport: http.Transport) -> CredentialCheck:
    """PRODUCT_READ_TOKEN: Contents read on the (private) product, including the pinned commit."""
    check = CredentialCheck(PRODUCT_READ)
    if _repo(check, token, repository, transport) is None:
        return check
    resp = http.get(transport, f"{API}/repos/{repository}/commits/{sha}", _headers(token))
    if resp.status != 200:
        check.fail(f"can't read commit {sha[:12]} of {repository} (HTTP {resp.status}): needs Contents read")
    else:
        when = f"expires {check.expires_on}" if check.expires_on else "no expiry date"
        check.info(f"{repository}@{sha[:12]} readable, {when}")
    return check


def rulesets_available(token: str, repository: str, transport: http.Transport) -> bool | None:
    """P10 review: can this repository have rulesets / branch protection? GitHub answers the branch-protection
    endpoint of a private repo on GitHub Free with 403 "Upgrade to GitHub Pro or make this repository public
    to enable this feature." (the same plan gate as rulesets); 404 "Branch not protected" or 200 = available.
    None = couldn't tell (e.g. the token lacks Administration read)."""
    resp = http.get(transport, f"{API}/repos/{repository}/branches/main/protection", _headers(token))
    data = resp.json()
    message = str(data.get("message", "")) if isinstance(data, dict) else ""
    if resp.status in (200, 404):
        return True
    if resp.status == 403 and "upgrade to github pro" in message.lower():
        return False
    return None


def enable_auto_merge(token: str, repository: str, transport: http.Transport) -> bool:
    """P11: allow auto-merge (the updater App enables it on its PRs; the gate decides) and delete merged
    branches (`PATCH /repos/{owner}/{repo}`, Administration write)."""
    resp = http.send_json(
        transport,
        "PATCH",
        f"{API}/repos/{repository}",
        {"allow_auto_merge": True, "delete_branch_on_merge": True},
        _headers(token),
    )
    return resp.status == 200


def disable_forking(token: str, repository: str, transport: http.Transport) -> bool:
    """Security review: no forks of the config repo (`PATCH /repos/{owner}/{repo}` `allow_forking: false`,
    Administration write). GitHub applies it to private repositories it offers the setting for; True when the
    repository reads back with forking off."""
    resp = http.send_json(
        transport, "PATCH", f"{API}/repos/{repository}", {"allow_forking": False}, _headers(token)
    )
    data = resp.json() if resp.status == 200 else None
    return isinstance(data, dict) and data.get("allow_forking") is False


def repo_is_public(repository: str, transport: http.Transport) -> bool | None:
    """True/False from an unauthenticated GET; None if GitHub couldn't be asked (rate limit, network)."""
    try:
        resp = http.get(transport, f"{API}/repos/{repository}", {"Accept": "application/vnd.github+json"})
    except http.HttpError:
        return None
    if resp.status == 200:
        return True
    return False if resp.status == 404 else None


# --- Actions OIDC `sub` claim (the tailnet's CI identity, terraform/tailscale.tf) -------------------------

OidcFormat = Literal["immutable", "legacy"]
# docs.github.com OIDC reference: "repositories created after July 15, 2026 now use an immutable default
# subject format"; "Repository renames and transfers after July 15, 2026 also move to the immutable
# subject format."
IMMUTABLE_SINCE = datetime.datetime(2026, 7, 15, tzinfo=datetime.UTC)


def oidc_subject(
    owner: str,
    repo: str,
    fmt: OidcFormat,
    owner_id: int | None = None,
    repo_id: int | None = None,
    ref: str = "refs/heads/main",
    environment: str | None = None,
) -> str:
    """The `sub` claim GitHub issues for a job on `ref` of owner/repo, or for a job in `environment` (then the
    context is `environment:NAME`, not the ref), the same string terraform/tailscale.tf builds. `@` separates
    name and id ("`@` cannot appear in GitHub usernames or repository names")."""
    context = f"environment:{environment}" if environment else f"ref:{ref}"
    if fmt == "legacy":
        return f"repo:{owner}/{repo}:{context}"
    if owner_id is None or repo_id is None:
        raise ValueError("the immutable format needs the owner and repository ids")
    return f"repo:{owner}@{owner_id}/{repo}@{repo_id}:{context}"


@dataclass(frozen=True)
class OidcInfo:
    """What GitHub puts in the config repo's OIDC `sub`. `fmt` None = couldn't tell (see `problem`)."""

    fmt: OidcFormat | None
    owner: str = ""
    repo: str = ""
    owner_id: int | None = None
    repo_id: int | None = None
    reason: str = ""
    problem: str = ""  # set when no format of ours can match (custom template) or GitHub couldn't be asked

    def subject(self, ref: str = "refs/heads/main", environment: str | None = None) -> str:
        assert self.fmt is not None
        return oidc_subject(self.owner, self.repo, self.fmt, self.owner_id, self.repo_id, ref, environment)


def _immutable_opt_in(resp: http.Response) -> bool | None:
    data = resp.json() if resp.status == 200 else None
    if not isinstance(data, dict) or not isinstance(data.get("use_immutable_subject"), bool):
        return None
    return bool(data["use_immutable_subject"])


def oidc_subject_info(token: str, repository: str, transport: http.Transport) -> OidcInfo:
    """Which `sub` format the config repo's Actions OIDC tokens carry, from the GitHub API (admin PAT)."""
    headers = _headers(token)
    resp = http.get(transport, f"{API}/repos/{repository}", headers)
    data = resp.json() if resp.status == 200 else None
    owner_data = data.get("owner") if isinstance(data, dict) else None
    if not isinstance(data, dict) or not isinstance(owner_data, dict):
        return OidcInfo(None, problem=f"GET /repos/{repository} failed (HTTP {resp.status})")
    owner, repo = str(owner_data.get("login") or ""), str(data.get("name") or "")
    info = {"owner": owner, "repo": repo, "owner_id": owner_data.get("id"), "repo_id": data.get("id")}

    resp = http.get(transport, f"{API}/repos/{repository}/actions/oidc/customization/sub", headers)
    template = resp.json() if resp.status == 200 else None
    if isinstance(template, dict) and template.get("use_default") is False:
        return OidcInfo(
            None,
            **info,
            problem=f"{repository} uses a custom OIDC subject template (include_claim_keys "
            f"{template.get('include_claim_keys')}), which the tailnet's CI identity doesn't match: reset it "
            f"with `gh api -X PUT repos/{repository}/actions/oidc/customization/sub -F use_default=true`",
        )
    if _immutable_opt_in(resp):
        return OidcInfo("immutable", **info, reason="opted in to immutable subject claims")
    if owner_data.get("type") == "Organization":
        org = http.get(transport, f"{API}/orgs/{owner}/actions/oidc/customization/sub", headers)
        if _immutable_opt_in(org):
            return OidcInfo("immutable", **info, reason=f"organization {owner} opted in (immutable subjects)")
    try:
        created = datetime.datetime.fromisoformat(str(data.get("created_at")).replace("Z", "+00:00"))
    except ValueError:
        return OidcInfo(None, **info, problem=f"GET /repos/{repository}: no created_at")
    if created >= IMMUTABLE_SINCE:
        return OidcInfo(
            "immutable",
            **info,
            reason=f"created {created.date()}: GitHub's default for repositories created since 2026-07-15",
        )
    return OidcInfo(
        "legacy",
        **info,
        reason=f"created {created.date()} and not opted in (a rename or transfer since 2026-07-15 also "
        "switches it to the immutable format, which the API doesn't show)",
    )
