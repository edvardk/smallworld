# --- Admin panel in cloudflare mode (design §5a, §8 P9): panel.<zone> behind Cloudflare Access ----------------
#
# The watchdog Worker also serves the panel: the UI from watchdog/panel/ (Workers static assets, uploaded with
# the script; run_worker_first so every request, assets included, passes the Worker's own Access JWT check) and
# a read API over D1/R2. Cloudflare Access (one-time PIN or GitHub) lets only panel.access.emails in; the Worker
# verifies Cf-Access-Jwt-Assertion against the team's certs and this application's AUD tag on every request, so
# a deleted or misconfigured Access app fails closed (403). workers.dev stays off (watchdog.tf), so the custom
# domain is the only way in. Everything here exists only in cloudflare mode. Resources and arguments checked
# against the cloudflare provider 5.28.1 schema (`terraform providers schema -json`).

locals {
  panel_hostname = "${var.panel_hostname}.${var.zone_name}"
  # P10 review: destructive actions (stop, plugin switches, restore) run only here, behind a second Access
  # application with a reauth_minutes session and the one-time PIN as its only login method.
  panel_confirm_hostname = "${var.panel_hostname}-confirm.${var.zone_name}"
  # The panel's GitHub App (whitelist pull requests): the Worker mints its installation tokens.
  panel_github_app = local.cloudflare_mode && var.panel_github_app_id != "" && nonsensitive(var.panel_github_app_private_key != "")

  # Plain-text binding PANEL (not secret: the AUD tag and the allowed emails are checks, not credentials).
  panel_config = local.cloudflare_mode ? {
    host              = local.panel_hostname
    team_domain       = data.cloudflare_zero_trust_organization.this[0].auth_domain
    aud               = cloudflare_zero_trust_access_application.panel[0].aud
    confirm_host      = local.panel_confirm_hostname
    confirm_aud       = cloudflare_zero_trust_access_application.confirm[0].aud
    emails            = var.panel_access_emails
    reauth_minutes    = var.panel_reauth_minutes
    title             = var.panel_title
    server_host       = local.fqdn
    flavour           = var.panel_flavour
    minecraft_version = var.panel_minecraft_version
    backup_schedule   = var.panel_backup_schedule
    max_players       = var.panel_max_players
  } : null

  # P14: Discord slash commands (POST /discord/interactions on the watchdog host). A plain-text binding: the ids
  # and the application's public key verify and allowlist requests; nothing here is a credential. The Worker
  # holds no bot token (setup registers the commands from the laptop; replies use each interaction's own token).
  discord_commands = local.cloudflare_mode && var.discord_commands.enabled
  discord_config = {
    app_id      = var.discord_commands.application_id
    public_key  = var.discord_commands.public_key
    guild_id    = var.discord_commands.guild_id
    channel_ids = var.discord_commands.channel_ids
    user_ids    = var.discord_commands.user_ids
    # v1.0.1: helpers (the read commands and /whitelist add, never /backup now); ids only, nothing secret
    helper_user_ids = var.discord_commands.helper_user_ids
    helper_role_id  = var.discord_commands.helper_role_id
    ephemeral       = var.discord_commands.ephemeral
    title           = var.panel_title
  }

  panel_bindings = concat(
    local.discord_commands ? [
      { type = "plain_text", name = "DISCORD", text = jsonencode(local.discord_config) },
    ] : [],
    # (the ASSETS binding is in watchdog.tf: P17's resource pack is served in every panel mode)
    local.cloudflare_mode ? [
      { type = "plain_text", name = "PANEL", text = jsonencode(local.panel_config) },
    ] : [],
    # The App's private key reaches nothing but this Worker secret: not the server, not CI (SECURITY.md).
    local.panel_github_app ? [
      {
        type = "plain_text", name = "GITHUB_APP",
        text = jsonencode({ app_id = var.panel_github_app_id, repository = "${var.github_owner}/${var.github_repository}" })
      },
      { type = "secret_text", name = "GITHUB_APP_KEY", text = base64decode(var.panel_github_app_private_key) },
    ] : [],
  )
}

# The Zero Trust organization: its auth domain (<team>.cloudflareaccess.com) issues the JWTs and serves the certs
# the Worker verifies them with. Signing up for Zero Trust (team name, Free plan) is a dashboard step that
# `mcctl init` detects and links to. Terraform only READS the organization (data source, GET).
#
# First live apply (2026-11-01): adopting the organization as a resource (import + ignore_changes on all but
# session_duration) failed: provider 5.28.1 PUTs the whole object, and the API rejected it with 400 code 12062
# "service_token_inactivity must include enabled, inactivity_threshold_days, and action" (the imported state
# didn't round-trip that nested object). Owning an account-wide singleton just for one field is fragile, so
# `mcctl setup` (step zero-trust) checks the global session_duration and only when it is longer than
# max(30m, 2 x reauth_minutes) sets it (to max(15m, reauth_minutes): Cloudflare's minimum is 15m) through the API
# (mcctl/providers/cloudflare.py global_session_bounds, set_session_duration); `mcctl doctor` and the check
# below report it. The removed block drops an adopted organization from the state without touching it (Terraform
# >= 1.7; a no-op when it was never adopted).
removed {
  from = cloudflare_zero_trust_organization.this

  lifecycle {
    destroy = false
  }
}

data "cloudflare_zero_trust_organization" "this" {
  count = local.cloudflare_mode ? 1 : 0

  account_id = local.account_id
}

# The global session bounds how long after the last one-time PIN Access may issue a confirmation-host token
# without a new login (it re-issues expired application tokens while the global one lasts; it does not shorten
# the panel's own 24h session). A warning only: Terraform never writes the organization (SECURITY.md "Re-auth").
locals {
  zt_session         = local.cloudflare_mode ? coalesce(data.cloudflare_zero_trust_organization.this[0].session_duration, "") : ""
  zt_session_minutes = try(tonumber(regex("([0-9]+)h", local.zt_session)[0]), 0) * 60 + try(tonumber(regex("([0-9]+)m([0-9]|$)", local.zt_session)[0]), 0)
  zt_session_limit   = max(30, 2 * var.panel_reauth_minutes)
}

check "zero_trust_global_session" {
  assert {
    condition     = !local.cloudflare_mode || (local.zt_session_minutes > 0 && local.zt_session_minutes <= local.zt_session_limit)
    error_message = "The Zero Trust global session is ${local.zt_session == "" ? "unset" : local.zt_session} (more than ${local.zt_session_limit}m), so the panel's confirmation host may not ask for a fresh one-time PIN. Set it in Zero Trust > Access controls > Access settings > Set your global session duration (15 minutes), or run `mcctl setup` (step zero-trust)."
  }
}

# Login methods. The one-time PIN provider: Cloudflare keeps (at most) one per account in practice, so an
# existing one is reused instead of creating another (first live apply: creating one failed with 403 code 1010
# auth.forbidden on an account that already had Access set up). Providers this configuration created itself
# carry OTP_NAME_PREFIX and are left out of "existing", so the count doesn't flip once ours exists.
data "cloudflare_zero_trust_access_identity_providers" "all" {
  count = local.cloudflare_mode ? 1 : 0

  account_id = local.account_id
}

locals {
  otp_name = "One-time PIN (${var.deployment_name} panel)"
  existing_otp = local.cloudflare_mode ? [
    for p in data.cloudflare_zero_trust_access_identity_providers.all[0].result : p.id
    if p.type == "onetimepin" && !try(startswith(p.name, local.otp_name), false)
  ] : []
}

resource "cloudflare_zero_trust_access_identity_provider" "otp" {
  count = local.cloudflare_mode && length(local.existing_otp) == 0 ? 1 : 0

  account_id = local.account_id
  name       = local.otp_name
  type       = "onetimepin"
  config     = {}
}

moved {
  from = cloudflare_zero_trust_access_identity_provider.confirm_otp
  to   = cloudflare_zero_trust_access_identity_provider.otp
}

# idp github: the panel logs in with GitHub (an OAuth app); the confirmation host always with the one-time PIN
# (a GitHub login would be re-issued silently from a live GitHub session).
resource "cloudflare_zero_trust_access_identity_provider" "github" {
  count = local.cloudflare_mode && var.panel_access_idp == "github" ? 1 : 0

  account_id = local.account_id
  name       = "GitHub (${var.deployment_name} panel)"
  type       = "github"
  config = {
    client_id     = var.panel_github_client_id
    client_secret = var.panel_github_client_secret
  }
}

locals {
  otp_idp = local.cloudflare_mode ? (
    length(local.existing_otp) > 0 ? local.existing_otp[0] : cloudflare_zero_trust_access_identity_provider.otp[0].id
  ) : null
  panel_idp   = local.cloudflare_mode && var.panel_access_idp == "github" ? cloudflare_zero_trust_access_identity_provider.github[0].id : local.otp_idp
  confirm_idp = local.otp_idp
}

resource "cloudflare_zero_trust_access_policy" "panel" {
  count = local.cloudflare_mode ? 1 : 0

  account_id = local.account_id
  name       = "${var.deployment_name} panel admins"
  decision   = "allow"
  include    = [for email in var.panel_access_emails : { email = { email = email } }]
}

resource "cloudflare_zero_trust_access_application" "panel" {
  count = local.cloudflare_mode ? 1 : 0

  account_id                 = local.account_id
  name                       = "${var.deployment_name} panel"
  type                       = "self_hosted"
  destinations               = [{ type = "public", uri = local.panel_hostname }]
  session_duration           = "24h"
  allowed_idps               = [local.panel_idp]
  auto_redirect_to_identity  = true
  app_launcher_visible       = false
  http_only_cookie_attribute = true
  same_site_cookie_attribute = "lax" # strict loops: the cookie set after the cross-site IdP redirect is not sent back
  policies                   = [{ id = cloudflare_zero_trust_access_policy.panel[0].id, precedence = 1 }]
}

# P10 review: <panel>-confirm.<zone>, the only host where stop, plugin switches and restores are accepted (the
# Worker checks this application's AUD and that the token is at most reauth_minutes old). A short session and
# one-time PIN only: Access re-issues its token silently only while the organization's global session (checked
# above, at most 30m by default) lasts, so a confirmation means a PIN at most global + reauth_minutes ago
# (SECURITY.md "Re-auth").
resource "cloudflare_zero_trust_access_application" "confirm" {
  count = local.cloudflare_mode ? 1 : 0

  account_id                 = local.account_id
  name                       = "${var.deployment_name} panel confirmations"
  type                       = "self_hosted"
  destinations               = [{ type = "public", uri = local.panel_confirm_hostname }]
  session_duration           = "${var.panel_reauth_minutes}m"
  allowed_idps               = [local.confirm_idp]
  auto_redirect_to_identity  = true
  app_launcher_visible       = false
  http_only_cookie_attribute = true
  same_site_cookie_attribute = "lax" # strict loops: the cookie set after the cross-site IdP redirect is not sent back
  policies                   = [{ id = cloudflare_zero_trust_access_policy.panel[0].id, precedence = 1 }]
}

resource "cloudflare_workers_custom_domain" "confirm" {
  count = local.cloudflare_mode ? 1 : 0

  account_id = local.account_id
  zone_id    = data.cloudflare_zone.this.id
  hostname   = local.panel_confirm_hostname
  service    = cloudflare_workers_script.watchdog.script_name

  depends_on = [cloudflare_zero_trust_access_application.confirm]
}

# panel.<zone> -> the same Worker (it routes by host). Cloudflare creates the DNS record and the certificate.
resource "cloudflare_workers_custom_domain" "panel" {
  count = local.cloudflare_mode ? 1 : 0

  account_id = local.account_id
  zone_id    = data.cloudflare_zone.this.id
  hostname   = local.panel_hostname
  service    = cloudflare_workers_script.watchdog.script_name

  # Access must guard the hostname before the Worker answers on it (the Worker refuses without a JWT anyway).
  depends_on = [cloudflare_zero_trust_access_application.panel]
}
