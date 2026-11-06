"""`mcctl product-setup [--check]`: the product repository's GitHub settings, through the REST API.

The product repo isn't a deployment (no Terraform state of its own), so its few settings are applied here,
idempotently: read each one, show what differs, change only that. `--check` changes nothing and exits 1 when
something differs (`mcctl doctor` runs the same comparison). Endpoints (GitHub REST, API version 2022-11-28,
docs checked 2026-11-01):

- `GET|PATCH /repos/{o}/{r}`: `allow_auto_merge` (Dependabot's auto-merge, P11) and `delete_branch_on_merge`;
  `allow_forking: false` while the product is private (security review).
- `GET|PATCH /repos/{o}/{r}`: `security_and_analysis.secret_scanning` and
  `secret_scanning_push_protection` (`{"status": "enabled"}`) on a public repository (free there): GitHub
  scans every push for known credential formats and push protection blocks one that carries a secret.
- `GET|PUT /repos/{o}/{r}/actions/permissions/access`: `access_level` `user` (`organization` for an org) while
  the product is private, so the owner's private config repos can call the reusable workflow.
- `GET|PUT /repos/{o}/{r}/actions/permissions/workflow`: `can_approve_pull_request_reviews` ("Allow GitHub
  Actions to create and approve pull requests": update-lock.yml opens PRs with GITHUB_TOKEN);
  `default_workflow_permissions` is sent back unchanged.
- `GET|PUT /repos/{o}/{r}/vulnerability-alerts` (204 on, 404 off) and `GET|PUT
  /repos/{o}/{r}/automated-security-fixes` (`{enabled}`): Dependabot alerts + security updates.
- `GET|POST /repos/{o}/{r}/rulesets`, `GET|PUT /repos/{o}/{r}/rulesets/{id}`: the `main` ruleset requiring
  the CI checks (lint, gitleaks, publication, actionlint, config-repo / validate, e2e) from the GitHub
  Actions app, changes only through pull requests (no approvals required), no force-push or deletion, and
  no bypass actor: the admin too goes through a pull request whose checks passed (the ruleset can still be
  edited in Settings in an emergency). Private repos of personal accounts need a paid plan for rulesets
  (403 "Upgrade to GitHub Pro"). A second ruleset (target `tag`, `refs/tags/v*`): creation, update and
  deletion for the admin only (security review: config repos pin releases by tag).

The token: `GH_TOKEN` from the environment, else the output of `gh auth token` (captured, never on a command
line), else a hidden prompt. It is never stored or printed.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

from mcctl import http
from mcctl.providers import ProviderError

API = "https://api.github.com"
RULESET_NAME = "main: CI must pass (mcctl product-setup)"
# Security review: release tags are what config repos pin (`uses: ...@<sha> # vX.Y.Z`, `mcctl upgrade`), so
# only the admin may create, move or delete a v* tag.
TAG_RULESET_NAME = "v* tags: admin only (mcctl product-setup)"
GITHUB_ACTIONS_APP_ID = 15368  # = terraform/github.tf local.github_actions_app_id (tests/test_consistency.py)
ADMIN_ROLE_ID = 5  # repository role "admin" (GitHub REST rulesets: bypass_actors)
# Check-run names: ci.yml jobs lint/gitleaks/publication/actionlint, its reusable call config-repo ->
# "config-repo / validate", e2e-mock.yml job e2e. update-lock.yml dispatches ci.yml and e2e-mock.yml on its PR
# branch.
REQUIRED_CHECKS = ("lint", "gitleaks", "publication", "actionlint", "config-repo / validate", "e2e")
# Public repositories only (GitHub Advanced Security features, free for public repositories).
SECRET_SCANNING = ("secret_scanning", "secret_scanning_push_protection")
Runner = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class SetupError(ProviderError):
    pass


def _headers(token: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def desired_ruleset() -> dict[str, Any]:
    return {
        "name": RULESET_NAME,
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["~DEFAULT_BRANCH"], "exclude": []}},
        # Nobody bypasses, the admin included: every change to main is a pull request whose checks passed.
        "bypass_actors": [],
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": False,
                    "required_status_checks": [
                        {"context": c, "integration_id": GITHUB_ACTIONS_APP_ID} for c in REQUIRED_CHECKS
                    ],
                },
            },
            {
                "type": "pull_request",  # no direct pushes; one maintainer, so no approvals required
                "parameters": {
                    "required_approving_review_count": 0,
                    "dismiss_stale_reviews_on_push": False,
                    "require_code_owner_review": False,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            },
        ],
    }


def desired_tag_ruleset() -> dict[str, Any]:
    return {
        "name": TAG_RULESET_NAME,
        "target": "tag",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": ["refs/tags/v*"], "exclude": []}},
        "bypass_actors": [
            {"actor_id": ADMIN_ROLE_ID, "actor_type": "RepositoryRole", "bypass_mode": "always"}
        ],
        "rules": [{"type": "creation"}, {"type": "update"}, {"type": "deletion"}],
    }


def _ruleset_key(rs: dict[str, Any]) -> Any:  # noqa: ANN401
    """What product-setup manages in a ruleset, normalised (GitHub adds fields of its own)."""
    rules: dict[str, Any] = {}
    for rule in rs.get("rules") or []:
        params = rule.get("parameters") or {}
        if rule.get("type") == "required_status_checks":
            params = {
                "strict": bool(params.get("strict_required_status_checks_policy")),
                "checks": sorted(
                    (str(c.get("context")), c.get("integration_id"))
                    for c in params.get("required_status_checks") or []
                ),
            }
        else:
            params = {}
        rules[str(rule.get("type"))] = params
    ref = (rs.get("conditions") or {}).get("ref_name") or {}
    return {
        "target": rs.get("target"),
        "enforcement": rs.get("enforcement"),
        "include": sorted(ref.get("include") or []),
        "exclude": sorted(ref.get("exclude") or []),
        "bypass": sorted(
            (b.get("actor_id"), b.get("actor_type"), b.get("bypass_mode"))
            for b in rs.get("bypass_actors") or []
        ),
        "rules": rules,
    }


@dataclass
class Change:
    name: str
    current: str
    wanted: str
    apply: Callable[[], None]


@dataclass
class Plan:
    repository: str
    changes: list[Change]
    in_sync: list[str]
    notes: list[str]


class Client:
    def __init__(self, token: str, repository: str, transport: http.Transport) -> None:
        self.token, self.repo, self.transport = token, repository, transport

    def get(self, path: str) -> http.Response:
        return http.get(self.transport, f"{API}/repos/{self.repo}{path}", _headers(self.token))

    def send(self, method: str, path: str, body: Any = None) -> http.Response:  # noqa: ANN401
        if body is None:
            return self.transport(
                http.Request(method, f"{API}/repos/{self.repo}{path}", _headers(self.token))
            )
        return http.send_json(
            self.transport, method, f"{API}/repos/{self.repo}{path}", body, _headers(self.token)
        )

    def must(self, method: str, path: str, body: Any = None, ok: tuple[int, ...] = (200, 201, 204)) -> None:  # noqa: ANN401
        resp = self.send(method, path, body)
        if resp.status not in ok:
            data = resp.json()
            message = data.get("message") if isinstance(data, dict) else None
            fix = ""
            if resp.status == 403 and "upgrade" in str(message).lower():
                fix = "rulesets on a private repo of a personal account need a paid GitHub plan"
            raise SetupError(
                f"{method} /repos/{self.repo}{path}: HTTP {resp.status} {message or ''}".strip(), fix
            )


def plan(token: str, repository: str, transport: http.Transport) -> Plan:
    c = Client(token, repository, transport)
    resp = c.get("")
    repo = resp.json()
    if resp.status != 200 or not isinstance(repo, dict):
        raise SetupError(
            f"GET /repos/{repository}: HTTP {resp.status}",
            "check the token (gh auth login) and the repository name",
        )
    if not (repo.get("permissions") or {}).get("admin"):
        raise SetupError(f"you aren't an admin of {repository}", "run this as the product repo's owner")
    changes: list[Change] = []
    in_sync: list[str] = []
    notes: list[str] = []

    def item(name: str, current: object, wanted: object, fn: Callable[[], None]) -> None:
        if current == wanted:
            in_sync.append(f"{name}: {wanted}")
        else:
            changes.append(Change(name, str(current), str(wanted), fn))

    for key in ("allow_auto_merge", "delete_branch_on_merge"):
        item(key, bool(repo.get(key)), True, lambda k=key: c.must("PATCH", "", {k: True}))

    analysis = repo.get("security_and_analysis") or {}
    if repo.get("private"):
        notes.append("private repository: secret scanning and push protection are set once it is public")
    else:
        for key in SECRET_SCANNING:
            status = str((analysis.get(key) or {}).get("status") or "disabled")
            item(
                key.replace("_", " "),
                status,
                "enabled",
                lambda k=key: c.must("PATCH", "", {"security_and_analysis": {k: {"status": "enabled"}}}),
            )

    if repo.get("private"):
        owner_type = (repo.get("owner") or {}).get("type")
        level = "organization" if owner_type == "Organization" else "user"
        data = c.get("/actions/permissions/access").json() or {}
        item(
            "actions access",
            data.get("access_level"),
            level,
            lambda: c.must("PUT", "/actions/permissions/access", {"access_level": level}),
        )
    else:
        notes.append("public repository: the Actions access setting doesn't apply")

    wf = c.get("/actions/permissions/workflow").json() or {}
    default = wf.get("default_workflow_permissions") or "read"
    item(
        "actions may create pull requests",
        bool(wf.get("can_approve_pull_request_reviews")),
        True,
        lambda: c.must(
            "PUT",
            "/actions/permissions/workflow",
            {"default_workflow_permissions": default, "can_approve_pull_request_reviews": True},
        ),
    )

    alerts = c.get("/vulnerability-alerts").status == 204
    item("dependabot alerts", alerts, True, lambda: c.must("PUT", "/vulnerability-alerts"))
    fixes = c.get("/automated-security-fixes")
    enabled = bool((fixes.json() or {}).get("enabled")) if fixes.status == 200 else False
    item("dependabot security updates", enabled, True, lambda: c.must("PUT", "/automated-security-fixes"))

    listing = c.get("/rulesets?includes_parents=false")
    existing = (
        [r for r in (listing.json() or []) if isinstance(r, dict)]
        if (listing.status == 200 and isinstance(listing.json(), list))
        else []
    )
    if listing.status == 403:
        notes.append("rulesets: HTTP 403 (a private personal repo needs a paid GitHub plan)")
    rulesets = (
        ("ruleset on main", desired_ruleset(), f"required checks {', '.join(REQUIRED_CHECKS)}"),
        ("ruleset on v* tags", desired_tag_ruleset(), "create/update/delete: admin only"),
    )
    for label, want, summary in rulesets:
        found = [r for r in existing if r.get("name") == want["name"]]
        if found:
            rid = found[0].get("id")
            current = c.get(f"/rulesets/{rid}").json() or {}
            same = _ruleset_key(current) == _ruleset_key(want)
            item(
                label,
                "as wanted" if same else "differs",
                "as wanted",
                lambda rid=rid, want=want: c.must("PUT", f"/rulesets/{rid}", want),
            )
        else:
            item(label, "missing", summary, lambda want=want: c.must("POST", "/rulesets", want))

    # Security review: no forks of a private product. GitHub offers the setting only for private repositories
    # owned by an organization (422 "can only be changed on org-owned private repositories" otherwise); a
    # private personal repository can only be forked by its collaborators anyway, and a public one always can.
    owner_type = str((repo.get("owner") or {}).get("type") or "")
    if repo.get("private") and owner_type != "Organization":
        notes.append("private personal repository: only collaborators can fork it (GitHub has no setting)")
    elif repo.get("private"):
        item(
            "allow_forking",
            bool(repo.get("allow_forking", True)),
            False,
            lambda: c.must("PATCH", "", {"allow_forking": False}),
        )
    else:
        notes.append("public repository: forking can't be turned off")
    return Plan(repository, changes, in_sync, notes)


def token(env: dict[str, str] | Any, run: Runner, ask: Callable[[], str] | None) -> str | None:  # noqa: ANN401
    """GH_TOKEN, else `gh auth token` (stdout), else `ask()` (a hidden prompt); None if there is none."""
    if value := (env.get("GH_TOKEN") or "").strip():
        return value
    proc = run(["gh", "auth", "token"])
    if proc.returncode == 0 and proc.stdout.strip():
        return proc.stdout.strip()
    return ask() if ask else None


def format_plan(p: Plan) -> list[str]:
    lines = [f"[ok] {s}" for s in p.in_sync]
    lines += [f"[->] {c.name}: {c.current} -> {c.wanted}" for c in p.changes]
    lines += [f"[--] {n}" for n in p.notes]
    return lines


def plan_rows(p: Plan) -> list[list[str]]:
    """format_plan as table rows (mcctl.ui): status, setting, now, wanted."""
    rows = [["ok: in sync", s, "", ""] for s in p.in_sync]
    rows += [["warn: change", c.name, str(c.current), str(c.wanted)] for c in p.changes]
    rows += [["info: note", n, "", ""] for n in p.notes]
    return rows


def apply(p: Plan) -> list[str]:
    done = []
    for change in p.changes:
        change.apply()
        done.append(change.name)
    return done
