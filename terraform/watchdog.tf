# --- Watchdog: a Cloudflare Worker that watches the server from outside (design §5, §5a, §8 P6) ---
#
# Every 5 minutes (cron) it sends a Minecraft status ping to the server over IPv4 and IPv6 separately, and it
# takes heartbeats from the server's health and backup jobs (POST /hb/..., bearer token). It alerts on Discord
# (when alerts.discord is on) after 2 failed runs or an overdue/failed heartbeat, announces recoveries, and
# stays quiet during maintenance windows (POST /maintenance: deploy restarts, restores). State lives in D1.
# Resources and arguments checked against the cloudflare provider 5.28.1 schema (terraform providers schema).

locals {
  watchdog_name     = "${var.deployment_name}-watchdog"
  watchdog_hostname = "hb.${local.fqdn}"

  # By name (default, watchdog_probe_by_name): v4.<fqdn> has only an A record and v6.<fqdn> only an AAAA, so
  # the per-family report stays exact (connect() has no address-family option). On the first real setup
  # Cloudflare refused the IPv4 literal ("proxy request failed, cannot connect to the specified address"; the
  # TCP sockets docs document hostnames only). With watchdog_probe_by_name = false the literals are probed,
  # IPv6 bracketed ("[2001:db8::1]:25565", the authority form workerd's connect() accepts).
  watchdog_targets = concat(
    var.server_ipv4 == null ? [] : [{
      family  = "v4"
      address = var.watchdog_probe_by_name ? "v4.${local.fqdn}:25565" : "${var.server_ipv4}:25565"
    }],
    var.server_ipv6 == null ? [] : [{
      family  = "v6"
      address = var.watchdog_probe_by_name ? "v6.${local.fqdn}:25565" : "[${var.server_ipv6}]:25565"
    }],
  )

  watchdog_config = {
    server_host = local.fqdn # sent in the handshake, like a client would
    targets     = local.watchdog_targets
    checks = {
      # grace = how long after the expected interval a check with no *finished* run alerts (design §5);
      # lateness counts from the last success, never from a /start. max_run_minutes: a /start without an end
      # alerts after this long. alert_on_fail: health.py alerts on Discord itself (single alerting brain).
      health = { period_minutes = 5, grace_minutes = 10, alert_on_fail = false, max_run_minutes = null }
      backup = {
        period_minutes  = var.watchdog_backup_period_minutes
        grace_minutes   = 30
        alert_on_fail   = true
        max_run_minutes = 60
      }
    }
    probe = { attempts = 3, retry_seconds = 20, timeout_seconds = 10, alert_after_runs = 2 }
  }
}

# Durable Object migrations (cloudflare mode: the Control class, monitoring.tf). The API applies a migration
# only when the upload's old_tag equals the Worker's current tag, and provider 5.28.1 uploads `migrations` as
# configured on every create and update. A fixed { new_tag = "v1", ... } therefore works exactly once: the
# next upload (any binding or code change, or a re-run after a partly failed apply) sends it again without
# old_tag and fails with 412 / code 10079 "Actor migration tag precondition failed, got tag '' when expected
# tag is 'v1'". So, like wrangler, read the live tag at plan time and send the migration only while it is
# missing; with the tag live, `migrations` is null and the upload leaves the Durable Objects alone. A list
# (not the single-script data source) so that a Worker that doesn't exist yet reads as "no tag".
# A later migration: bump watchdog_do_tag and send { old_tag = <live tag>, new_tag, steps } for the missing steps.
data "cloudflare_workers_scripts" "account" {
  count      = local.cloudflare_mode ? 1 : 0
  account_id = local.account_id
}

locals {
  watchdog_do_tag = "v1"
  watchdog_live_script = local.cloudflare_mode ? one([
    for s in data.cloudflare_workers_scripts.account[0].result : s if s.id == local.watchdog_name
  ]) : null
  watchdog_live_do_tag = try(coalesce(local.watchdog_live_script.migration_tag, ""), "")
  watchdog_migrations = local.cloudflare_mode && local.watchdog_live_do_tag == "" ? {
    new_tag            = local.watchdog_do_tag
    new_sqlite_classes = ["Control"]
  } : null
}

# Workers Logs keep what the Worker logs itself (console.error on a failed cron run: no addresses, no
# secrets). Invocation logs are off: they record each request's metadata, headers included (Cloudflare
# says Authorization is redacted in its dashboard logs; not relied on), so the heartbeat bearer token
# never reaches a log. Spelled out to the values the API reads back (sampling 1, traces off): provider 5.28.1
# compares these optional attributes with the API's answer, so leaving them out was a perpetual in-place
# update of the Worker (tests/test_tf_drift.py; `persist` is computed and may stay unset).
locals {
  watchdog_observability = {
    enabled            = true
    head_sampling_rate = 1
    logs = {
      enabled            = true
      head_sampling_rate = 1
      invocation_logs    = false
    }
    traces = {
      enabled            = false
      head_sampling_rate = 1
    }
  }
}

resource "random_password" "heartbeat" {
  length  = 48
  special = false
}

resource "cloudflare_d1_database" "watchdog" {
  account_id = local.account_id
  name       = local.watchdog_name

  lifecycle {
    # Provider 5.28.1 plans an in-place update on every run while read_replication isn't configured, though
    # the API reports the default ({ mode = "disabled" }) and nothing changes. Ignoring it keeps the live
    # value (and sends nothing on create, as before). tests/test_tf_drift.py.
    ignore_changes = [read_replication]
  }
}

resource "cloudflare_workers_script" "watchdog" {
  account_id         = local.account_id
  script_name        = local.watchdog_name
  main_module        = "worker.js"
  content_file       = "${path.module}/../watchdog/worker.js"
  content_sha256     = filesha256("${path.module}/../watchdog/worker.js")
  compatibility_date = "2026-10-01"

  bindings = concat(
    [
      { type = "d1", name = "DB", id = cloudflare_d1_database.watchdog.id },
      { type = "secret_text", name = "HEARTBEAT_TOKEN", text = random_password.heartbeat.result },
      { type = "plain_text", name = "TARGET", text = jsonencode(local.watchdog_config) },
      # The static files below: the panel UI (served only on the panel host, cloudflare mode) and P17's resource
      # pack (pack/<sha1>.zip, public on this host in every mode; worker.js servePack).
      { type = "assets", name = "ASSETS" },
    ],
    # Only when alerts.discord is on (the webhook URL itself stays sensitive).
    var.watchdog_discord ? [
      { type = "secret_text", name = "DISCORD_WEBHOOK", text = var.discord_webhook_url },
    ] : [],
    # Agent ingest, the R2 log archive and the Control Durable Object (monitoring.tf; cloudflare mode only).
    local.monitoring_bindings,
    # The panel's config and its static files (panel.tf; cloudflare mode only).
    local.panel_bindings,
    # v1.1: the artifact mirror (cloudflare mode) and the client modpack's setup page (client.tf).
    local.client_bindings,
  )

  # The panel UI and P17's resource pack. run_worker_first: the Worker answers every request, assets included, so
  # its Access JWT check can't be skipped by asking for a file; it serves the panel's files through the ASSETS
  # binding only on the panel host (cloudflare mode), and nothing but pack/<40 hex>.zip elsewhere. Uploaded in every
  # mode: a conditional value here is unknown during `terraform validate`, which provider 5.28.1 can't handle for
  # this attribute; without the PANEL binding nothing serves the panel's files. v1.2: the directory is the one
  # `mcctl render` stages (var.watchdog_assets_dir: the panel's files + the pack built from the config's art).
  assets = {
    directory = var.watchdog_assets_dir != "" ? var.watchdog_assets_dir : "${path.module}/../watchdog/panel"
    config = {
      run_worker_first   = true
      html_handling      = "none"
      not_found_handling = "none"
    }
  }

  # The Control class gets SQLite storage (the only kind on the Free plan; design §5a). Sent only while the
  # live Worker hasn't got the tag yet (local.watchdog_migrations); see the comment there.
  migrations = local.watchdog_migrations

  observability = local.watchdog_observability

  lifecycle {
    # No ignore_changes on migrations: the provider stores the configured value, so a planned value kept from
    # the state would make the apply fail as inconsistent. The one update to null after the first apply is
    # harmless (an upload without migrations).
    precondition {
      condition     = contains(["", local.watchdog_do_tag], local.watchdog_live_do_tag)
      error_message = "The Worker ${local.watchdog_name} has Durable Object migration tag \"${local.watchdog_live_do_tag}\", which this product never set (it only knows \"${local.watchdog_do_tag}\"). Check the Worker in the Cloudflare dashboard (docs/setup.md, troubleshooting: 10079)."
    }
  }
}

resource "cloudflare_workers_cron_trigger" "watchdog" {
  account_id  = local.account_id
  script_name = cloudflare_workers_script.watchdog.script_name
  schedules   = [{ cron = "*/5 * * * *" }]
}

# hb.<hostname>.<zone>: Cloudflare creates the DNS record and an Advanced Certificate for it (a two-level
# subdomain isn't covered by Universal SSL).
resource "cloudflare_workers_custom_domain" "watchdog" {
  account_id = local.account_id
  zone_id    = data.cloudflare_zone.this.id
  hostname   = local.watchdog_hostname
  service    = cloudflare_workers_script.watchdog.script_name
}

# No <name>-watchdog.<account>.workers.dev: the custom domain is the only way in.
resource "cloudflare_workers_script_subdomain" "watchdog" {
  account_id       = local.account_id
  script_name      = cloudflare_workers_script.watchdog.script_name
  enabled          = false
  previews_enabled = false
}

# The probe targets (watchdog_probe_by_name, default): single-family names, so each probe uses exactly one
# address family. DNS-only: a proxied name would send the probe through Cloudflare (and could loop).
resource "cloudflare_dns_record" "probe_v4" {
  count = var.watchdog_probe_by_name && var.server_ipv4 != null ? 1 : 0

  zone_id = data.cloudflare_zone.this.id
  name    = "v4.${local.fqdn}"
  type    = "A"
  content = var.server_ipv4
  ttl     = var.dns_ttl
  proxied = false
  comment = "Watchdog IPv4 probe target (managed by Terraform)"
}

resource "cloudflare_dns_record" "probe_v6" {
  count = var.watchdog_probe_by_name && var.server_ipv6 != null ? 1 : 0

  zone_id = data.cloudflare_zone.this.id
  name    = "v6.${local.fqdn}"
  type    = "AAAA"
  content = var.server_ipv6
  ttl     = var.dns_ttl
  proxied = false
  comment = "Watchdog IPv6 probe target (managed by Terraform)"
}
