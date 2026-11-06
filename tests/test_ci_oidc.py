"""The tailnet's CI identity trusts exactly the OIDC `sub` GitHub issues for the config repo's main branch.

GitHub (docs.github.com "OpenID Connect reference", immutable subject claims): repositories created, renamed or
transferred since 2026-07-15, or opted in, get `repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:refs/heads/BRANCH`; older
ones keep `repo:OWNER/REPO:ref:...`. mcctl reads the format from the GitHub API (recorded fixtures here),
server.yml github.oidc_subject carries it to Terraform, which builds the subject with the ids from its data
sources (terraform/tailscale.tf)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from conftest import Replay

from mcctl.providers import github

REPO = Path(__file__).resolve().parents[1]
TOKEN = "github_pat_fake"


def test_subject_formats() -> None:
    assert (
        github.oidc_subject("octo-org", "octo-repo", "legacy")
        == "repo:octo-org/octo-repo:ref:refs/heads/main"
    )
    # docs.github.com's own example of the immutable format
    assert (
        github.oidc_subject("octo-org", "octo-repo", "immutable", 123456, 456789)
        == "repo:octo-org@123456/octo-repo@456789:ref:refs/heads/main"
    )
    with pytest.raises(ValueError):
        github.oidc_subject("octo-org", "octo-repo", "immutable")


def _terraform_subject(
    immutable: bool, owner: str, repo: str, owner_id: int, repo_id: int, environments: bool = False
) -> str:
    """Evaluate terraform/tailscale.tf's ci_oidc_repo/ci_oidc_subject locals for the given inputs."""
    tf = (REPO / "terraform/tailscale.tf").read_text()
    cond = re.search(
        r'ci_oidc_repo = \(\s*var\.github_oidc_immutable_subject\s*\?\s*"([^"]+)"\s*:\s*"([^"]+)"\s*\)', tf
    )
    assert cond, "ci_oidc_repo must be `var.github_oidc_immutable_subject ? <immutable> : <legacy>`"
    subject = re.search(r'ci_oidc_subject = "([^"]+)"', tf)
    assert subject
    context = re.search(r'ci_oidc_context = var\.github_environments \? "([^"]+)" : "([^"]+)"', tf)
    assert context, "ci_oidc_context must be `var.github_environments ? <environment> : <ref>`"
    values = {
        "var.github_owner": owner,
        "var.github_repository": repo,
        "data.github_user.config_owner.id": str(owner_id),
        "data.github_repository.config.repo_id": str(repo_id),
    }

    def interpolate(text: str) -> str:
        return re.sub(r"\$\{([^}]+)\}", lambda m: values[m.group(1)], text)

    values["local.ci_oidc_repo"] = interpolate(cond.group(1) if immutable else cond.group(2))
    values["local.ci_oidc_context"] = context.group(1) if environments else context.group(2)
    return interpolate(subject.group(1))


@pytest.mark.parametrize("environments", [False, True])
@pytest.mark.parametrize("fmt", ["immutable", "legacy"])
def test_terraform_builds_the_same_subject_as_mcctl(fmt: github.OidcFormat, environments: bool) -> None:
    """Security review M2: with github.environments the deploy job runs in environment `deploy`, and GitHub's
    sub names the environment instead of the ref (`repo:...:environment:deploy`)."""
    got = _terraform_subject(fmt == "immutable", "octocat", "mc-config", 4242, 9090, environments)
    env = "deploy" if environments else None
    assert got == github.oidc_subject("octocat", "mc-config", fmt, 4242, 9090, environment=env)
    assert "*" not in got and got.endswith(":environment:deploy" if environments else ":ref:refs/heads/main")


def test_terraform_reads_the_ids_from_github() -> None:
    tf = (REPO / "terraform/tailscale.tf").read_text()
    assert 'data "github_user" "config_owner" {\n  username = var.github_owner\n}' in tf
    assert (
        'data "github_repository" "config" {\n  full_name = "${var.github_owner}/${var.github_repository}"\n}'
        in tf
    )


REPOSITORY = "mcctl-tests/tests-mc-config"


@pytest.mark.parametrize(
    ("fixture", "fmt", "subject", "reason"),
    [
        (
            "github_oidc_new_repo.json",
            "immutable",
            "repo:mcctl-tests@2/tests-mc-config@1:ref:refs/heads/main",
            "created 2026-11-01",
        ),
        (
            "github_oidc_old_repo.json",
            "legacy",
            "repo:mcctl-tests/tests-mc-config:ref:refs/heads/main",
            "not opted in",
        ),
        (
            "github_oidc_old_repo_opted_in.json",
            "immutable",
            "repo:mcctl-tests@2/tests-mc-config@1:ref:refs/heads/main",
            "opted in",
        ),
        (
            "github_oidc_org_opted_in.json",
            "immutable",
            "repo:mcctl-tests@2/tests-mc-config@1:ref:refs/heads/main",
            "organization mcctl-tests opted in",
        ),
    ],
)
def test_oidc_subject_info(fixture: str, fmt: str, subject: str, reason: str) -> None:
    replay = Replay(fixture)
    info = github.oidc_subject_info(TOKEN, REPOSITORY, replay)
    assert info.fmt == fmt and info.subject() == subject and reason in info.reason, info
    assert all(r.headers["Authorization"] == f"Bearer {TOKEN}" for r in replay.requests)


def test_custom_sub_template_is_reported() -> None:
    info = github.oidc_subject_info(TOKEN, REPOSITORY, Replay("github_oidc_custom_template.json"))
    assert (
        info.fmt is None
        and "custom OIDC subject template" in info.problem
        and "use_default=true" in info.problem
    )


def test_customization_endpoint_unreadable_falls_back_to_creation_date() -> None:
    """A PAT without access to the customization endpoint (403): the creation date still decides."""
    info = github.oidc_subject_info(TOKEN, REPOSITORY, Replay("github_oidc_sub_forbidden.json"))
    assert info.fmt == "immutable" and info.owner_id == 2 and info.repo_id == 1


class _Ctx:
    """Just what init._tailnet_join_refused uses: gh, repo().server, env(), deps.transport, set, commit."""

    def __init__(self, log: str, oidc_subject: str, transport: Replay) -> None:
        import subprocess
        import types

        from mcctl import schema

        self.log, self.sets, self.commits = log, [], []
        server = schema.ServerConfig(
            github=schema.GitHub(
                owner="mcctl-tests", config_repo="tests-mc-config", oidc_subject=oidc_subject
            )
        )
        self._repo = types.SimpleNamespace(server=server)
        self.deps = types.SimpleNamespace(transport=transport)
        self._proc = lambda argv: subprocess.CompletedProcess(argv, 0, self.log, "")

    def gh(self, *args: str):
        return self._proc(["gh", *args])

    def repo(self):
        return self._repo

    def env(self) -> dict[str, str]:
        return {"GITHUB_TOKEN": TOKEN}

    def set(self, values) -> None:
        self.sets.append(dict(values))

    def commit(self, message: str, paths, *, push: bool = True) -> str:
        self.commits.append((message, push))
        return "0" * 40


LOG_403 = (
    "deploy\tJoin the tailnet (ephemeral tag:ci node)\t2026-11-02T10:00:00Z ##[error]token exchange failed: "
    "403 Forbidden\n"
)


def test_setup_explains_the_tailnet_join_403() -> None:
    from mcctl import init

    ctx = _Ctx(LOG_403, "immutable", Replay("github_oidc_new_repo.json"))
    cause, fix = init._tailnet_join_refused(ctx, REPOSITORY, 42)  # type: ignore[arg-type,misc]
    assert (
        "repo:mcctl-tests@2/tests-mc-config@1:ref:refs/heads/main" in cause
        and "Cannot validate subject" in cause
    )
    assert "re-run `mcctl setup`" in fix and not ctx.sets
    # server.yml named the wrong format: setup corrects it (committed with the next push), so the re-run applies.
    ctx = _Ctx(LOG_403, "legacy", Replay("github_oidc_new_repo.json"))
    cause, _fix = init._tailnet_join_refused(ctx, REPOSITORY, 42)  # type: ignore[arg-type,misc]
    assert ctx.sets == [{"github.oidc_subject": "immutable"}] and ctx.commits[0][1] is False
    assert "now immutable" in cause
    other = _Ctx("deploy\tansible-playbook site.yml\tfailed: 403\n", "immutable", Replay())
    assert init._tailnet_join_refused(other, REPOSITORY, 42) is None  # type: ignore[arg-type]
