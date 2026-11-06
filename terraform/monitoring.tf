# --- Monitoring in cloudflare mode (design §5a, §8 P8): mc-agent -> the watchdog Worker ---------------------
#
# The same Worker (hb.<hostname>.<zone>) takes the agent's samples and logs (bearer AGENT_TOKEN, separate from
# the heartbeat token) into D1 day tables, archives the raw journal to its own R2 bucket (deleted after
# logs.r2_days by a lifecycle rule; the backup bucket has lock rules, so it isn't shared), and holds the agent's
# WebSocket in the Control Durable Object (SQLite storage, hibernation). Commands are signed with
# COMMAND_HMAC_KEY, which only the Worker and the agent's env file hold. Everything here exists only when
# panel.mode is cloudflare. Arguments checked against the cloudflare provider 5.28.1 schema.

locals {
  cloudflare_mode = var.panel_mode == "cloudflare"
  logs_bucket     = "${var.deployment_name}-mc-logs"

  monitoring_config = {
    retention_days     = var.monitoring_retention_days
    rollup_after_days  = 7
    metrics_interval_s = var.monitoring_metrics_interval_s
    logs = {
      d1_days          = var.monitoring_logs_d1_days
      max_rows_per_day = var.monitoring_logs_max_rows_per_day
    }
  }

  # P13: player experience (watchdog/worker.js "player experience"): its own D1 database (the Free plan's 500 MB is
  # per database), the R2 archive under telemetry/ in the logs bucket (kept: the lifecycle rule covers journal/ only).
  experience = local.cloudflare_mode && var.experience_enabled
  experience_config = {
    raw_days          = var.experience_raw_days
    minute_days       = var.experience_minute_days
    hour_days         = var.experience_hour_days
    minecraft_version = var.panel_minecraft_version
    chunk_entities    = var.experience_chunk_entities_alert
    messages = {
      enabled            = var.experience_messages_enabled
      per_player_minutes = var.experience_message_per_player_minutes
      broadcast_minutes  = var.experience_broadcast_minutes
      join_summary       = var.experience_join_summary
    }
  }

  monitoring_bindings = local.cloudflare_mode ? concat([
    { type = "plain_text", name = "MONITORING", text = jsonencode(local.monitoring_config) },
    { type = "secret_text", name = "AGENT_TOKEN", text = random_password.agent_token.result },
    { type = "secret_text", name = "COMMAND_HMAC_KEY", text = random_password.command_hmac_key.result },
    { type = "r2_bucket", name = "LOGS", bucket_name = cloudflare_r2_bucket.logs[0].name },
    { type = "durable_object_namespace", name = "CONTROL", class_name = "Control" },
    ], local.experience ? [
    { type = "d1", name = "TELEMETRY", id = cloudflare_d1_database.telemetry[0].id },
    { type = "plain_text", name = "EXPERIENCE", text = jsonencode(local.experience_config) },
  ] : []) : []
}

# Created in every mode (cheap, and the deploy secrets stay the same set); only used in cloudflare mode.
resource "random_password" "agent_token" {
  length  = 48
  special = false
}

resource "random_password" "command_hmac_key" {
  length  = 64
  special = false
}

resource "cloudflare_r2_bucket" "logs" {
  count = local.cloudflare_mode ? 1 : 0

  account_id = local.account_id
  name       = local.logs_bucket
  location   = var.r2_location
}

resource "cloudflare_d1_database" "telemetry" {
  count = local.experience ? 1 : 0

  account_id = local.account_id
  name       = "${var.deployment_name}-telemetry"

  lifecycle {
    ignore_changes = [read_replication] # perpetual update otherwise (watchdog.tf, cloudflare_d1_database)
  }
}

resource "cloudflare_r2_bucket_lifecycle" "logs" {
  count = local.cloudflare_mode ? 1 : 0

  account_id  = local.account_id
  bucket_name = cloudflare_r2_bucket.logs[0].name
  rules = [{
    id         = "journal-${var.monitoring_logs_r2_days}d"
    enabled    = true
    conditions = { prefix = "journal/" } # P13: telemetry/ (the experience archive) is kept
    delete_objects_transition = {
      condition = { type = "Age", max_age = var.monitoring_logs_r2_days * 86400 }
    }
  }]
}
