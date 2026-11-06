"""The secrets Ansible needs (design §3): one table for `mcctl deploy`, CI and the tests.

Terraform is the only store. Each entry NAME is, by construction:

- the Terraform output `name` (lower case, terraform/outputs.tf),
- the GitHub Actions secret `NAME` (terraform/github.tf creates it; optional ones only when set),
- the environment variable `MC_SECRET_NAME`, which ansible/group_vars/all/secrets.yml reads into the Ansible
  variable `mc_secret_name`. CI maps the Actions secret to it (.github/workflows/deploy.yml); `mcctl deploy`
  sets it from `terraform output -json`.

tests/test_consistency.py checks every one of those places against this table.

Where each Actions secret lives (security review M2, server.yml github.environments): with environments,
every secret of SECRETS plus the CI identity's is a secret of the config repo's GitHub Environment
DEPLOY_ENVIRONMENT, the updater App's of UPDATE_ENVIRONMENT (both admit main only); REPOSITORY_SECRETS stay
repository-level (every job reads them). Without environments all are repository-level.
"""

from __future__ import annotations

from dataclasses import dataclass

ENV_PREFIX = "MC_SECRET_"


@dataclass(frozen=True)
class Secret:
    name: str
    required: bool  # optional secrets may be unset ("" = feature off)

    @property
    def env(self) -> str:
        return ENV_PREFIX + self.name

    @property
    def output(self) -> str:
        return self.name.lower()

    @property
    def ansible_var(self) -> str:
        return "mc_secret_" + self.name.lower()


SECRETS: tuple[Secret, ...] = (
    Secret("RCON_PASSWORD", required=True),
    Secret("RESTIC_PASSWORD", required=True),
    Secret("RESTIC_REPOSITORY", required=True),
    Secret("R2_ACCESS_KEY_ID", required=True),
    Secret("R2_SECRET_ACCESS_KEY", required=True),
    Secret("HEARTBEAT_TOKEN", required=True),  # the watchdog Worker's bearer token (design §5)
    Secret("AGENT_TOKEN", required=True),  # mc-agent's bearer token at the Worker (design §5a, P8)
    Secret("COMMAND_HMAC_KEY", required=True),  # signs panel commands; the agent verifies them (§5a)
    Secret("DISCORD_WEBHOOK_URL", required=False),
    # (P10 review: no panel GitHub token any more: the panel's GitHub App key lives in the Worker only, which
    # mints short-lived installation tokens for mc-agent.)
)

# Security review M2 (terraform/github.tf locals, the reusable workflow's `environment:` keys;
# tests/test_environments.py).
DEPLOY_ENVIRONMENT = "deploy"
UPDATE_ENVIRONMENT = "update"
CI_IDENTITY_SECRETS = ("TS_OAUTH_CLIENT_ID", "TS_AUDIENCE")  # the deploy job's tailnet login
UPDATE_SECRETS = ("UPDATER_APP_ID", "UPDATER_APP_KEY")  # only the scheduled update job
REPOSITORY_SECRETS = ("PRODUCT_READ_TOKEN",)  # every job's product checkout (read-only, while private)
# v1.1: the deploy job's uploads to the artifact mirror (`mcctl client-publish`): environment deploy, never
# Ansible's (not in SECRETS: it must not reach the server).
PUBLISH_SECRETS = ("ARTIFACT_PUBLISH_TOKEN",)


def environment_of(name: str) -> str | None:
    """The environment that holds Actions secret `name` when github.environments is on (None: repository)."""
    if name in REPOSITORY_SECRETS:
        return None
    return UPDATE_ENVIRONMENT if name in UPDATE_SECRETS else DEPLOY_ENVIRONMENT
