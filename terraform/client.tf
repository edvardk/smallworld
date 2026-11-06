# --- v1.1: the client modpack's setup page and the pinned artifact mirror (watchdog/worker.js "client") ---------
#
# The same Worker serves the setup page on <client_hostname>.<zone> (only with server.yml client.enabled), and in
# every cloudflare-mode deployment keeps the pinned artifact mirror: R2 bucket <name>-mc-artifacts, written only by
# the deploy job (`mcctl client-publish`, bearer PUBLISH_TOKEN: an Actions secret, never on the server) under
# artifacts/<sha256>/<filename> (immutable) and client/<tier>.json (the packs' plans), read by the Worker (mirror
# links for the launchers; the server's own downloads with the agent token). CLIENT_KEY signs the setup page's
# sessions and links and keys the one-time codes' hashes; only the Worker holds it.
# Resources and arguments checked against the cloudflare provider 5.28.1 schema (terraform providers schema).

locals {
  artifacts_bucket = "${var.deployment_name}-mc-artifacts"
  client           = local.cloudflare_mode && var.client_enabled
  client_hostname  = "${var.client_hostname}.${var.zone_name}"

  client_bindings = concat(
    local.cloudflare_mode ? [
      { type = "r2_bucket", name = "ARTIFACTS", bucket_name = cloudflare_r2_bucket.artifacts[0].name },
      { type = "secret_text", name = "PUBLISH_TOKEN", text = random_password.publish_token.result },
    ] : [],
    local.client ? [
      { type = "plain_text", name = "CLIENT", text = jsonencode({ host = local.client_hostname, title = var.client_title, colors = var.client_colors }) },
      { type = "secret_text", name = "CLIENT_KEY", text = random_password.client_key.result },
    ] : [],
  )
}

# Created in every mode (cheap; the Actions secret set stays the same); only used in cloudflare mode.
resource "random_password" "publish_token" {
  length  = 48
  special = false
}

resource "random_password" "client_key" {
  length  = 64
  special = false
}

resource "cloudflare_r2_bucket" "artifacts" {
  count = local.cloudflare_mode ? 1 : 0

  account_id = local.account_id
  name       = local.artifacts_bucket
  location   = var.r2_location
}

# play.<zone>: covered by Universal SSL (one level below the zone); Cloudflare creates the DNS record.
resource "cloudflare_workers_custom_domain" "client" {
  count = local.client ? 1 : 0

  account_id = local.account_id
  zone_id    = data.cloudflare_zone.this.id
  hostname   = local.client_hostname
  service    = cloudflare_workers_script.watchdog.script_name
}
