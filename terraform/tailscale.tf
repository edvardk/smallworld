# Access model:
#   - the admin (tailscale_admin_login) can reach everything.
#   - tag:ci (ephemeral GitHub Actions runners) can reach tag:mc-server on TCP 22 only (tests assert it is
#     denied 25565, RCON 25575 and the onserver panel/Grafana ports 443/8443).
#   - SSH uses Tailscale SSH: no long-lived SSH private key lives in GitHub or Terraform state,
#     and who may log in as which user is defined here, in code.
# The policy itself is tailscale-policy.json.tftpl (plain JSON with the admin login filled in), so
# tests/test_tailscale_policy.py can evaluate its `tests`/`sshTests` against its grants without Terraform;
# Tailscale runs the same tests on every apply and rejects a policy that fails them.
resource "tailscale_acl" "this" {
  overwrite_existing_content = true

  acl = jsonencode(jsondecode(templatefile("${path.module}/tailscale-policy.json.tftpl", {
    admin = jsonencode(var.tailscale_admin_login)
  })))
}

# Single-use, pre-authorised, tagged key consumed once by bootstrap.yml. Short-lived on purpose: mint
# a fresh one right before every bootstrap (first setup, provider move, rebuild):
#   terraform apply -replace=tailscale_tailnet_key.server
# Once used (or expired) the key is invalid; recreate_if_invalid = "never" keeps Terraform from
# minting a new one on every later apply (a perpetual diff) - only -replace does.
resource "tailscale_tailnet_key" "server" {
  description         = "mc server bootstrap"
  reusable            = false
  ephemeral           = false
  preauthorized       = true
  tags                = ["tag:mc-server"]
  expiry              = 86400 # 24 h
  recreate_if_invalid = "never"

  depends_on = [tailscale_acl.this]
}

# GitHub Actions joins the tailnet with workload identity federation: the deploy job of the product's
# reusable workflow exchanges its GitHub OIDC token (permissions: id-token: write) for a Tailscale token.
# No long-lived secret exists. In a reusable workflow the standard claims (sub, repository, ref) describe
# the CALLER, so `subject` pins the config repo's main branch; the job_workflow_ref claim names the called
# workflow, so only the product's server.yml (any pinned ref) matches.
#
# The `sub` format (docs.github.com "OpenID Connect reference", immutable subject claims): repositories
# created, renamed or transferred since 2026-07-15 (or opted in) get `repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:...`
# with the numeric account and repository ids; older ones keep `repo:OWNER/REPO:ref:...`. server.yml
# github.oidc_subject picks the format (`mcctl setup` reads it from the GitHub API, `mcctl doctor` checks it);
# the ids come from the GitHub API here, never typed in. No wildcard in the owner/repo part: the ids are
# what stops a re-registered (squatted) owner or repository name from matching.
# Data sources checked against the github provider 6.13.0 schema: github_user.id is the account's numeric id
# (GET /users/{login}, which also answers for organizations), github_repository.repo_id the repository's.
data "github_user" "config_owner" {
  username = var.github_owner
}

data "github_repository" "config" {
  full_name = "${var.github_owner}/${var.github_repository}"
}

locals {
  ci_oidc_repo = (
    var.github_oidc_immutable_subject
    ? "${var.github_owner}@${data.github_user.config_owner.id}/${var.github_repository}@${data.github_repository.config.repo_id}"
    : "${var.github_owner}/${var.github_repository}"
  )
  # Security review M2: a job with `environment:` gets `...:environment:NAME` instead of `...:ref:...` (docs.github.com
  # "OpenID Connect reference", "Filtering for a specific environment"); the deploy job runs in environment
  # `deploy`, which admits main only (github.tf), so the subject still means "the config repo's main".
  ci_oidc_context = var.github_environments ? "environment:deploy" : "ref:refs/heads/main"
  ci_oidc_subject = "repo:${local.ci_oidc_repo}:${local.ci_oidc_context}"
}

resource "tailscale_federated_identity" "ci" {
  description = "GitHub Actions deploy"
  issuer      = "https://token.actions.githubusercontent.com"
  subject     = local.ci_oidc_subject
  custom_claim_rules = {
    job_workflow_ref = "${var.product_repository}/.github/workflows/server.yml@*"
  }
  scopes = ["auth_keys"]
  tags   = ["tag:ci"]

  depends_on = [tailscale_acl.this]
}
