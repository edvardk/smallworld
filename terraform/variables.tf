# Non-secret inputs come from the config repo's server.yml: `mcctl render` writes
# <config>/.mcctl/rendered/terraform/terraform.tfvars.json, passed with -var-file. There is no tfvars file in
# the product. Secrets come from TF_VAR_* in the config repo's .env.

variable "deployment_name" {
  description = "server.yml `name`: derives the watchdog Worker and D1 database names (<name>-watchdog)."
  type        = string

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,20}$", var.deployment_name))
    error_message = "deployment_name must match ^[a-z][a-z0-9-]{1,20}$ (server.yml name)."
  }
}

variable "watchdog_backup_period_minutes" {
  description = "Expected interval between backups (server.yml monitoring.alerts.backup_period_minutes); the watchdog alerts 30 min after it."
  type        = number
  default     = 60
}

variable "watchdog_discord" {
  description = "server.yml alerts.discord: give the watchdog Worker the Discord webhook (TF_VAR_discord_webhook_url)."
  type        = bool
  default     = false
}

variable "watchdog_assets_dir" {
  description = <<-EOT
    v1.2: the watchdog Worker's static assets, staged by `mcctl render` (<rendered>/watchdog: the product's
    watchdog/panel/ files plus the resource pack built from the config repo's art, pack/<sha1>.zip). An absolute
    path; "" = the product's watchdog/panel/ (no resource pack).
  EOT
  type        = string
  default     = ""
}

variable "watchdog_probe_by_name" {
  description = <<-EOT
    The watchdog probes v4.<fqdn> (A only) and v6.<fqdn> (AAAA only), DNS-only records created for this purpose,
    so each probe still uses exactly one address family. Default true: on the first real setup Cloudflare's
    connect() refused the IPv4 literal ("proxy request failed, cannot connect to the specified address") while
    players connected fine; the TCP sockets docs only document hostnames. false probes the IP literals.
  EOT
  type        = bool
  default     = true
}

variable "panel_mode" {
  description = "server.yml panel.mode: cloudflare = mc-agent pushes to the Worker (R2 log bucket, Control Durable Object)."
  type        = string
  default     = "cloudflare"

  validation {
    condition     = contains(["cloudflare", "onserver", "off"], var.panel_mode)
    error_message = "panel_mode must be cloudflare, onserver or off."
  }
}

variable "monitoring_metrics_interval_s" {
  description = "server.yml monitoring.metrics_interval_s (mc-agent sample interval; the Worker's freshness check)."
  type        = number
  default     = 60
}

variable "monitoring_retention_days" {
  description = "server.yml monitoring.retention_days: metric rollups kept in D1."
  type        = number
  default     = 90
}

variable "monitoring_logs_d1_days" {
  description = "server.yml monitoring.logs.d1_days: searchable log days in D1."
  type        = number
  default     = 14
}

variable "monitoring_logs_r2_days" {
  description = "server.yml monitoring.logs.r2_days: raw journal archives kept in R2 (lifecycle rule)."
  type        = number
  default     = 30
}

variable "monitoring_logs_max_rows_per_day" {
  description = "server.yml monitoring.logs.max_rows_per_day: D1 log rows per UTC day (the Worker enforces it too)."
  type        = number
  default     = 30000
}

variable "zone_name" {
  description = "Cloudflare zone that holds the server's DNS record."
  type        = string
}

variable "hostname" {
  description = "Record name within the zone, e.g. \"mc\" for mc.<zone>."
  type        = string
}

variable "server_ipv4" {
  description = "Public IPv4 of the server. Leave null until the host exists; no A record is created while null."
  type        = string
  default     = null

  validation {
    condition     = var.server_ipv4 == null || can(cidrhost("${var.server_ipv4}/32", 0))
    error_message = "server_ipv4 must be a valid IPv4 address."
  }
}

variable "server_ipv6" {
  description = <<-EOT
    Public IPv6 of the server: one address you pick from the host's /64 (netcup shows the /64 in the
    SCP). No AAAA record is created while null. When set, Ansible checks the host really has global
    IPv6 (mc_expect_ipv6).
  EOT
  type        = string
  default     = null

  validation {
    condition     = var.server_ipv6 == null || can(cidrhost("${var.server_ipv6}/128", 0))
    error_message = "server_ipv6 must be a valid IPv6 address."
  }
}

variable "dns_mail_hardening" {
  description = <<-EOT
    server.yml dns.mail_hardening: the zone has no mail, so Terraform adds a null MX, `v=spf1 -all` and a
    p=reject DMARC record (cloudflare.tf). The plan refuses while the zone has another MX record.
  EOT
  type        = bool
  default     = true
}

variable "dns_ttl" {
  description = "TTL (seconds) for the server records. Short so a provider move propagates quickly."
  type        = number
  default     = 300
}

variable "cloudflare_token_owner" {
  description = <<-EOT
    Who owns the Cloudflare admin token (CLOUDFLARE_API_TOKEN): "account" (an account-owned token, recommended)
    or "user". The R2 backup credential Terraform creates is owned the same way (an account token can't create
    user tokens). server.yml cloudflare.token_owner, set by `mcctl init` from the token's verify.
  EOT
  type        = string
  default     = "user"
  validation {
    condition     = contains(["user", "account"], var.cloudflare_token_owner)
    error_message = "cloudflare_token_owner must be \"user\" or \"account\"."
  }
}

variable "r2_lock_days" {
  description = <<-EOT
    R2 bucket lock age for restic snapshots/ and data/ (config and keys/ are locked indefinitely).
    scripts/backup.py keeps every snapshot for r2_lock_days + 1 days (KEEP_WITHIN) so `forget` never
    hits a locked object; tests/test_consistency.py checks the two agree.
  EOT
  type        = number
  default     = 7

  validation {
    condition     = var.r2_lock_days >= 1 && var.r2_lock_days == floor(var.r2_lock_days)
    error_message = "r2_lock_days must be a whole number of days >= 1."
  }
}

variable "r2_backup_bucket" {
  description = "Name of the R2 bucket that holds restic backups (server.yml backups.bucket)."
  type        = string
}

variable "r2_location" {
  description = "R2 location hint for the backup bucket."
  type        = string
  default     = "weur"
}

variable "tailscale_admin_login" {
  description = "The admin's Tailscale login name (server.yml admin.tailscale_login)."
  type        = string
}

variable "github_owner" {
  description = "GitHub user or org that owns the config repo."
  type        = string
}

variable "github_repository" {
  description = "Config repo name (without owner): receives the Actions secrets; its main branch is the only CI identity trusted by the tailnet."
  type        = string
}

variable "github_oidc_immutable_subject" {
  description = <<-EOT
    server.yml github.oidc_subject == "immutable": the config repo's GitHub Actions OIDC tokens carry
    `sub` = repo:OWNER@OWNER_ID/REPO@REPO_ID:ref:... (GitHub's default for repositories created, renamed or
    transferred since 2026-07-15); false = the name-only repo:OWNER/REPO:ref:... of older repositories.
  EOT
  type        = bool
  default     = true
}

variable "product_repository" {
  description = <<-EOT
    owner/repo of the product, from the config repo's pinned `uses:` line. Only jobs of the product's
    reusable workflow (OIDC claim job_workflow_ref) may join the tailnet as tag:ci.
  EOT
  type        = string

  validation {
    condition     = can(regex("^[A-Za-z0-9-]+/[A-Za-z0-9._-]+$", var.product_repository))
    error_message = "product_repository must be owner/repo."
  }
}

variable "product_read_token" {
  description = <<-EOT
    Fine-grained PAT with Contents: read on the product repo, stored as the PRODUCT_READ_TOKEN Actions
    secret so the reusable workflow can check out the private product. Empty = product is public (no secret).
  EOT
  type        = string
  default     = ""
  sensitive   = true
}

variable "discord_webhook_url" {
  description = "Discord webhook for alerts and restart announcements (empty = disabled)."
  type        = string
  default     = ""
  sensitive   = true
}

# --- GitHub Apps (P10 review: the panel's whitelist PRs; P11: the weekly update PRs) ---
# Created by `mcctl init` through GitHub's App manifest flow; the ids aren't secret, the private keys are
# (base64 of the PEM the manifest conversion returned, one line in .env). Empty = the feature is off.

variable "panel_github_app_id" {
  description = <<-EOT
    The panel's GitHub App (Contents + Pull requests write, Metadata read; installed on the config repo only).
    Its private key goes to the Worker only (secret binding GITHUB_APP_KEY), which mints short-lived installation
    tokens for mc-agent's whitelist pull requests. Empty = panel changes stay pending on the server (deploys keep
    them) until they expire after 24 h.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^([0-9]{1,12})?$", var.panel_github_app_id))
    error_message = "panel_github_app_id is the App's numeric id (or empty)."
  }
}

variable "panel_github_app_private_key" {
  description = "Base64 of the panel App's PEM private key (TF_VAR_panel_github_app_private_key). Worker secret only."
  type        = string
  default     = ""
  sensitive   = true
}

variable "updater_github_app_id" {
  description = <<-EOT
    P11: the updater GitHub App (Contents, Pull requests and Workflows write, Metadata read; the config repo
    only). The weekly update task opens the lock/product update PRs with it (PRs by GITHUB_TOKEN start no
    workflow, and GITHUB_TOKEN can't change a workflow file) and enables auto-merge. Its key is an Actions secret
    of the config repo, used only by the scheduled update job. Empty = update PRs aren't opened.
  EOT
  type        = string
  default     = ""

  validation {
    condition     = can(regex("^([0-9]{1,12})?$", var.updater_github_app_id))
    error_message = "updater_github_app_id is the App's numeric id (or empty)."
  }
}

variable "updater_github_app_private_key" {
  description = "Base64 of the updater App's PEM private key (TF_VAR_updater_github_app_private_key)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "github_protect_main" {
  description = <<-EOT
    server.yml github.protect_main: rulesets on the config repo (main only through pull requests with the
    required checks, no force-push/deletion; other branches than panel/* only for the admin and the updater
    App). Rulesets on a private repo need a paid GitHub plan for personal accounts (GitHub Pro).
  EOT
  type        = bool
  default     = true
}

variable "github_environments" {
  description = <<-EOT
    server.yml github.environments (security review M2): the deploy secrets are secrets of the config repo's
    environment `deploy`, the updater App's of `update`, both restricted to main; the CI identity trusts the
    `environment:deploy` OIDC subject. false = repository-level secrets (before v1 security review). Needs
    GitHub Pro (or a public repo) for a private personal repo, like rulesets.
  EOT
  type        = bool
  default     = false
}

# --- panel (cloudflare mode, panel.tf) ---

variable "panel_hostname" {
  description = "server.yml panel.hostname: the panel lives at <panel_hostname>.<zone_name> (cloudflare mode)."
  type        = string
  default     = "panel"
}

variable "panel_access_emails" {
  description = "server.yml panel.access.emails: who Cloudflare Access (and the Worker's JWT check) lets in."
  type        = list(string)
  default     = []

  validation {
    condition     = var.panel_mode != "cloudflare" || length(var.panel_access_emails) >= 1
    error_message = "panel_access_emails needs at least one email in cloudflare mode."
  }
}

variable "panel_access_idp" {
  description = "server.yml panel.access.idp: otp (one-time PIN by email) or github (needs an OAuth app)."
  type        = string
  default     = "otp"

  validation {
    condition     = contains(["otp", "github"], var.panel_access_idp)
    error_message = "panel_access_idp must be otp or github."
  }
}

variable "panel_github_client_id" {
  description = "server.yml panel.access.github_client_id: the GitHub OAuth app's client id (idp github)."
  type        = string
  default     = ""
}

variable "panel_github_client_secret" {
  description = "The GitHub OAuth app's client secret (TF_VAR_panel_github_client_secret; idp github only)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "panel_reauth_minutes" {
  description = <<-EOT
    server.yml panel.access.reauth_minutes: destructive actions (P10) run only on the confirmation host
    (<panel_hostname>-confirm.<zone>), whose Access application issues sessions this long (one-time PIN only).
  EOT
  type        = number
  default     = 10

  validation {
    condition     = var.panel_reauth_minutes >= 1 && var.panel_reauth_minutes <= 60
    error_message = "panel_reauth_minutes must be 1-60."
  }
}

variable "panel_title" {
  description = "The panel's heading (server.yml minecraft.motd without formatting codes)."
  type        = string
  default     = "Minecraft server"
}

variable "panel_flavour" {
  description = "server.yml minecraft.flavour (shown in the panel header)."
  type        = string
  default     = ""
}

variable "panel_minecraft_version" {
  description = "server.yml minecraft.version (shown in the panel header)."
  type        = string
  default     = ""
}

variable "panel_backup_schedule" {
  description = "server.yml backups.schedule (shown on the panel's Backups tab)."
  type        = string
  default     = ""
}

variable "panel_max_players" {
  description = "server.yml minecraft.max_players (the panel's free slots before the first sample)."
  type        = number
  default     = 20
}

# --- P13: player-experience analytics (server.yml experience.*) ---------------------------------------------

variable "experience_enabled" {
  description = "server.yml experience.enabled: the TELEMETRY D1 database + the Worker's experience ingest (cloudflare mode)."
  type        = bool
  default     = true
}

variable "experience_raw_days" {
  description = "server.yml experience.retention.raw_days: raw 10 s samples kept in D1."
  type        = number
  default     = 14
}

variable "experience_minute_days" {
  description = "server.yml experience.retention.minute_days: 1-minute rollups kept in D1."
  type        = number
  default     = 180
}

variable "experience_hour_days" {
  description = "server.yml experience.retention.hour_days: hourly rollups kept in D1."
  type        = number
  default     = 730
}

variable "experience_messages_enabled" {
  description = "server.yml experience.messages.enabled: the verdict engine may send in-game messages."
  type        = bool
  default     = true
}

variable "experience_message_per_player_minutes" {
  description = "server.yml experience.messages.per_player_minutes."
  type        = number
  default     = 30
}

variable "experience_broadcast_minutes" {
  description = "server.yml experience.messages.broadcast_minutes."
  type        = number
  default     = 30
}

variable "experience_join_summary" {
  description = "server.yml experience.messages.join_summary."
  type        = bool
  default     = true
}

variable "experience_chunk_entities_alert" {
  description = "server.yml monitoring.alerts.chunk_entities: Discord alert when a loaded chunk has this many entities (0 = off)."
  type        = number
  default     = 300
}

variable "discord_commands" {
  description = "server.yml discord.*: the Worker's Discord slash commands (P14). Ids and the application's public key only (nothing secret)."
  type = object({
    enabled        = bool
    application_id = string
    public_key     = string
    guild_id       = string
    channel_ids    = list(string)
    user_ids       = list(string)
    # v1.0.1: the helper tier (the read commands and /whitelist add); optional so older tfvars still load
    helper_user_ids = optional(list(string), [])
    helper_role_id  = optional(string, "")
    ephemeral       = bool
  })
  default = {
    enabled         = false
    application_id  = ""
    public_key      = ""
    guild_id        = ""
    channel_ids     = []
    user_ids        = []
    helper_user_ids = []
    helper_role_id  = ""
    ephemeral       = true
  }
}

variable "client_enabled" {
  description = "server.yml client.enabled (v1.1): the client modpack's setup page on <client_hostname>.<zone> (cloudflare mode)."
  type        = bool
  default     = false
}

variable "client_hostname" {
  description = "server.yml client.hostname: the setup page's record name."
  type        = string
  default     = "play"
}

variable "client_title" {
  description = "The server's name on the setup page (server.yml branding.name)."
  type        = string
  default     = "Minecraft"
}

variable "client_colors" {
  description = "The setup page's colours (server.yml branding.colors: text, accent, secondary, subtle; #RRGGBB)."
  type        = map(string)
  default     = {}
}
