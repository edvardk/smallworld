output "server_fqdn" {
  description = "Hostname players connect to."
  value       = local.fqdn
}

output "watchdog_url" {
  description = "The watchdog Worker (GET /status: freshness only, no addresses)."
  value       = "https://${local.watchdog_hostname}"
}

output "r2_backup_bucket" {
  value = cloudflare_r2_bucket.backups.name
}

output "r2_endpoint" {
  value = local.r2_endpoint
}

output "r2_access_key_id" {
  value     = local.r2_access_key_id
  sensitive = true
}

output "r2_secret_access_key" {
  value     = local.r2_secret_access_key
  sensitive = true
}

output "server_tailscale_auth_key" {
  description = "Single-use, 24 h key for bootstrap.yml. Mint a fresh one first: terraform apply -replace=tailscale_tailnet_key.server"
  value       = tailscale_tailnet_key.server.key
  sensitive   = true
}

# --- Deploy secrets: `mcctl deploy` reads these (names in mcctl/secrets.py); CI gets the same values as
# Actions secrets (github.tf). ---

output "rcon_password" {
  value     = random_password.rcon.result
  sensitive = true
}

output "restic_password" {
  description = "Keep a copy somewhere safe (e.g. a password manager): without it the backups are unreadable."
  value       = random_password.restic.result
  sensitive   = true
}

output "restic_repository" {
  value = local.restic_repository
}

output "heartbeat_token" {
  description = "Bearer token of the watchdog Worker (health/backup heartbeats, maintenance windows)."
  value       = random_password.heartbeat.result
  sensitive   = true
}

output "agent_token" {
  description = "Bearer token of mc-agent at the Worker (ingest, commands); cloudflare mode."
  value       = random_password.agent_token.result
  sensitive   = true
}

output "command_hmac_key" {
  description = "Signs panel commands (Worker) and verifies them (mc-agent); cloudflare mode."
  value       = random_password.command_hmac_key.result
  sensitive   = true
}

output "discord_webhook_url" {
  value     = var.discord_webhook_url
  sensitive = true
}

output "panel_url" {
  description = "The admin panel (cloudflare mode; behind Cloudflare Access)."
  value       = local.cloudflare_mode ? "https://${local.panel_hostname}" : null
}

output "panel_confirm_url" {
  description = "Where the panel's destructive actions are confirmed (short Access session, one-time PIN)."
  value       = local.cloudflare_mode ? "https://${local.panel_confirm_hostname}" : null
}

output "ci_oidc_subject" {
  description = "Not secret: the OIDC `sub` claim the tailnet's CI identity trusts (`mcctl doctor` checks it)."
  value       = local.ci_oidc_subject
}

output "artifact_publish_token" {
  description = "v1.1: the deploy job's bearer token for the Worker's /publish routes (Actions secret ARTIFACT_PUBLISH_TOKEN)."
  value       = random_password.publish_token.result
  sensitive   = true
}

output "client_url" {
  description = "v1.1: the client modpack's setup page (players open it with a code from /modpack)."
  value       = local.client ? "https://${local.client_hostname}/" : null
}
