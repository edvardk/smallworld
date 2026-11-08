"""Security review M2: the config repo's Actions secrets live in GitHub Environments that admit main only.

Repository-level secrets are readable by any workflow run, including one the updater App (Workflows write) could
push to an update/* branch. With server.yml github.environments, Terraform (terraform/github.tf) puts the deploy
secrets into environment `deploy` and the updater App's into `update`, both with the deployment branch policy
"main"; the reusable workflow's jobs that read them run in that environment (validate job outputs), and the tailnet's
CI identity trusts the `environment:deploy` OIDC subject. Without the setting everything stays as before."""

from __future__ import annotations

import dataclasses
import re

import pytest
import yaml
from conftest import REPO

from mcctl import cli, configrepo, schema, secrets
from mcctl.providers import github

REUSABLE = yaml.safe_load((REPO / ".github/workflows/server.yml").read_text())
GITHUB_TF = (REPO / "terraform/github.tf").read_text()
ENV_EXPR = {
    secrets.DEPLOY_ENVIRONMENT: "${{ needs.validate.outputs.deploy_environment }}",
    secrets.UPDATE_ENVIRONMENT: "${{ needs.validate.outputs.update_environment }}",
}
ALL_NAMES = (
    {s.name for s in secrets.SECRETS}
    | set(secrets.CI_IDENTITY_SECRETS)
    | set(secrets.UPDATE_SECRETS)
    | set(secrets.REPOSITORY_SECRETS)
    | set(secrets.PUBLISH_SECRETS)
)


def _secrets_used(job: dict) -> set[str]:
    return set(re.findall(r"secrets\.([A-Z0-9_]+)", yaml.safe_dump(job)))


@pytest.mark.parametrize("name", sorted(REUSABLE["jobs"]))
def test_each_job_reads_only_its_environments_secrets(name: str) -> None:
    job = REUSABLE["jobs"][name]
    used = _secrets_used(job) - set(secrets.REPOSITORY_SECRETS)
    homes = {secrets.environment_of(s) for s in used}
    assert len(homes) <= 1, f"{name} reads secrets of several environments: {used}"
    if homes:
        (home,) = homes
        assert job.get("environment") == ENV_EXPR[home], f"{name} reads {used}: environment {home}"
    else:  # validate, gate, panel-merge, update-test, boot-test, product-update: no environment, no deploy secret
        assert "environment" not in job, name


def test_the_jobs_with_secrets() -> None:
    jobs = REUSABLE["jobs"]
    with_env = {n: j["environment"] for n, j in jobs.items() if "environment" in j}
    assert with_env == {
        "deploy": ENV_EXPR["deploy"],
        "backup-check": ENV_EXPR["deploy"],
        "expiry-check": ENV_EXPR["deploy"],
        "update": ENV_EXPR["update"],
        "product-update-pr": ENV_EXPR["update"],  # v1.5: the manual product update's pull request
    }
    # only the deploy job asks for an OIDC token (its sub then names the environment)
    assert [n for n, j in jobs.items() if (j.get("permissions") or {}).get("id-token")] == ["deploy"]
    outputs = jobs["validate"]["outputs"]
    assert outputs["deploy_environment"] == "${{ steps.config.outputs.deploy_environment }}"
    assert outputs["update_environment"] == "${{ steps.config.outputs.update_environment }}"


def test_every_secret_has_one_home() -> None:
    assert {secrets.environment_of(n) for n in ALL_NAMES} == {None, "deploy", "update"}
    assert [n for n in ALL_NAMES if secrets.environment_of(n) is None] == ["PRODUCT_READ_TOKEN"]
    assert {n for n in ALL_NAMES if secrets.environment_of(n) == "update"} == set(secrets.UPDATE_SECRETS)


def _tf_set(name: str) -> set[str]:
    m = re.search(rf"{name}\s*=\s*toset\(\[([^\]]*)\]\)", GITHUB_TF)
    assert m, name
    return set(re.findall(r'"([A-Z0-9_]+)"', m.group(1)))


def test_terraform_agrees_with_mcctl_secrets() -> None:
    assert _tf_set("update_environment_secrets") == set(secrets.UPDATE_SECRETS)
    assert _tf_set("repository_level_secrets") == set(secrets.REPOSITORY_SECRETS)
    assert 'toset(["deploy", "update"])' in GITHUB_TF
    assert '? "update" : "deploy"' in GITHUB_TF


def _block(kind: str, name: str) -> str:
    start = GITHUB_TF.index(f'resource "{kind}" "{name}" {{')
    return GITHUB_TF[start : GITHUB_TF.index("\n}\n", start)]


def test_environments_admit_main_only() -> None:
    env = _block("github_repository_environment", "this")
    assert "protected_branches     = false" in env and "custom_branch_policies = true" in env
    assert "reviewers" not in env  # no reviewer gate: the scheduled jobs run unattended
    policy = _block("github_repository_environment_deployment_policy", "main")
    assert 'branch_pattern = "main"' in policy and "tag_pattern" not in policy
    secret = _block("github_actions_environment_secret", "this")
    assert "depends_on = [github_repository_environment_deployment_policy.main]" in secret
    assert "for_each = local.environment_of_secret" in secret
    repo_level = _block("github_actions_secret", "this")
    assert "!contains(keys(local.environment_of_secret), k)" in repo_level


def test_terraform_attributes_exist_in_the_provider_schema() -> None:
    """The arguments used exist in the github provider 6.13.0 (recorded in CLAUDE.md's schema check; here the
    names only, so a typo fails offline as well as in `terraform validate`)."""
    for kind, args in {
        "github_repository_environment": ("repository", "environment", "deployment_branch_policy"),
        "github_repository_environment_deployment_policy": ("repository", "environment", "branch_pattern"),
        "github_actions_environment_secret": ("repository", "environment", "secret_name", "value"),
    }.items():
        block = GITHUB_TF[GITHUB_TF.index(f'resource "{kind}"') :]
        for arg in args:
            assert re.search(rf"^\s+{arg}\s", block, re.M), (kind, arg)


def _cfg(environments: bool) -> configrepo.ConfigRepo:
    repo = configrepo.load(REPO / "examples/config-repo")
    server = dataclasses.replace(
        repo.server, github=dataclasses.replace(repo.server.github, environments=environments)
    )
    return dataclasses.replace(repo, server=server)


@pytest.mark.parametrize("on", [True, False])
def test_validate_outputs_name_the_environments(on: bool) -> None:
    out = cli.ci_outputs(_cfg(on))
    assert out["deploy_environment"] == ("deploy" if on else "")
    assert out["update_environment"] == ("update" if on else "")


def test_backward_compatible_default() -> None:
    """An older server.yml without the field keeps repository-level secrets and the ref subject; new config
    repos (template) and the full example turn it on."""
    assert schema.GitHub().environments is False
    for path in ("template/server.yml", "examples/config-repo/server.yml", "examples/full/server.yml"):
        assert yaml.safe_load((REPO / path).read_text())["github"]["environments"] is True, path


@pytest.mark.parametrize(
    ("fmt", "subject"),
    [
        ("immutable", "repo:octocat@4242/mc-config@9090:environment:deploy"),
        ("legacy", "repo:octocat/mc-config:environment:deploy"),
    ],
)
def test_environment_subject(fmt: github.OidcFormat, subject: str) -> None:
    got = github.oidc_subject("octocat", "mc-config", fmt, 4242, 9090, environment="deploy")
    assert got == subject and "*" not in got


def test_mcctl_migrates_in_two_applies() -> None:
    """Terraform destroys orphaned repository secrets before it creates the environment ones: mcctl applies the
    environment secrets first (targeted) when the state still has the repository-level deploy secrets."""
    from mcctl import tf

    old = ['github_actions_secret.this["RCON_PASSWORD"]', 'github_actions_secret.this["PRODUCT_READ_TOKEN"]']
    assert tf.environments_migration(True, old)
    assert not tf.environments_migration(False, old)
    done = [*old, 'github_actions_environment_secret.this["RCON_PASSWORD"]']
    assert not tf.environments_migration(True, done)
    assert not tf.environments_migration(True, [])  # a fresh setup: one apply
    assert not tf.environments_migration(True, None)
    assert f"mcctl apply --target {tf.ENVIRONMENT_SECRETS}" in GITHUB_TF
