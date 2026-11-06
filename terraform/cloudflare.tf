data "cloudflare_zone" "this" {
  filter = {
    name = var.zone_name
  }
}

locals {
  account_id  = data.cloudflare_zone.this.account.id
  fqdn        = "${var.hostname}.${var.zone_name}"
  r2_endpoint = "https://${local.account_id}.r2.cloudflarestorage.com"
}

# --- DNS: DNS-only (grey cloud); the Cloudflare proxy can't carry Minecraft traffic ---

resource "cloudflare_dns_record" "server_a" {
  count = var.server_ipv4 == null ? 0 : 1

  zone_id = data.cloudflare_zone.this.id
  name    = local.fqdn
  type    = "A"
  content = var.server_ipv4
  ttl     = var.dns_ttl
  proxied = false
  comment = "Minecraft server (managed by Terraform)"
}

resource "cloudflare_dns_record" "server_aaaa" {
  count = var.server_ipv6 == null ? 0 : 1

  zone_id = data.cloudflare_zone.this.id
  name    = local.fqdn
  type    = "AAAA"
  content = var.server_ipv6
  ttl     = var.dns_ttl
  proxied = false
  comment = "Minecraft server (managed by Terraform)"
}

# --- Mail hardening (server.yml dns.mail_hardening; security review LOW) ---
# The zone exists for the game server only and sends or receives no mail, so tell the world: a null MX (RFC 7505:
# "accepts no mail"), an SPF record that authorises no sender, and DMARC p=reject (which covers subdomains too:
# without `sp`, `p` applies to them), so receivers refuse mail forged as @<zone>. TXT values are quoted, the form
# Cloudflare stores and returns. No CAA: Cloudflare adds CAA records for its own Universal SSL CAs automatically
# only once a zone has CAA records, and which CAs it uses changes; a wrong CAA would break the panel's
# certificate (docs/reference.md "DNS"). Arguments checked against the cloudflare provider 5.28.1 schema.
data "cloudflare_dns_records" "mx" {
  count = var.dns_mail_hardening ? 1 : 0

  zone_id = data.cloudflare_zone.this.id
  type    = "MX"
  name    = { exact = var.zone_name }
}

locals {
  # Another MX at the apex means the zone does have mail: the null MX would break it, so the plan refuses.
  foreign_mx = var.dns_mail_hardening ? [
    for r in data.cloudflare_dns_records.mx[0].result : r.content if r.content != "."
  ] : []
}

resource "cloudflare_dns_record" "null_mx" {
  count = var.dns_mail_hardening ? 1 : 0

  zone_id  = data.cloudflare_zone.this.id
  name     = var.zone_name
  type     = "MX"
  content  = "."
  priority = 0
  ttl      = 3600
  comment  = "No mail for this zone: null MX, RFC 7505 (managed by Terraform)"

  lifecycle {
    precondition {
      condition     = length(local.foreign_mx) == 0
      error_message = "The zone has MX records (it receives mail): set server.yml dns.mail_hardening: false."
    }
  }
}

resource "cloudflare_dns_record" "spf" {
  count = var.dns_mail_hardening ? 1 : 0

  zone_id = data.cloudflare_zone.this.id
  name    = var.zone_name
  type    = "TXT"
  content = "\"v=spf1 -all\""
  ttl     = 3600
  comment = "No mail for this zone: no sender is authorised (managed by Terraform)"
}

resource "cloudflare_dns_record" "dmarc" {
  count = var.dns_mail_hardening ? 1 : 0

  zone_id = data.cloudflare_zone.this.id
  name    = "_dmarc.${var.zone_name}"
  type    = "TXT"
  content = "\"v=DMARC1; p=reject\""
  ttl     = 3600
  comment = "No mail for this zone: reject anything claiming to be from it (managed by Terraform)"
}

# --- R2: backup bucket + S3 credentials scoped to that bucket only ---

resource "cloudflare_r2_bucket" "backups" {
  account_id = local.account_id
  name       = var.r2_backup_bucket
  location   = var.r2_location

  lifecycle {
    prevent_destroy = true
  }
}

# Bucket lock: objects matching a rule can't be deleted or overwritten until the rule releases them,
# even with the server's credentials (its token is object-scoped and can't change lock rules).
# Shaped around restic's repository layout (README "Design notes: R2 bucket lock"):
#   config, keys/  never deleted by restic        -> locked indefinitely (the repo can't be bricked)
#   snapshots/     deleted by `forget`            -> locked r2_lock_days; backup.py keeps every
#                                                    snapshot for r2_lock_days + 1 (--keep-within)
#   data/          deleted by `prune`             -> locked r2_lock_days; prune skips packs it can't
#                                                    delete yet and removes them on a later run
#   index/, locks/ rewritten/deleted on every prune/backup; delete failures there are fatal -> unlocked
# The rules are listed sorted by id: the API returns them in that order and provider 5.28.1 compares the list
# by position, so any other order is a perpetual in-place update (tests/test_tf_drift.py).
locals {
  r2_lock_rules_by_id = merge(
    { for p in ["config", "keys/"] : "restic-${trimsuffix(p, "/")}-indefinite" => {
      prefix    = p
      condition = { type = "Indefinite", max_age_seconds = null }
    } },
    { for p in ["snapshots/", "data/"] : "restic-${trimsuffix(p, "/")}-${var.r2_lock_days}d" => {
      prefix    = p
      condition = { type = "Age", max_age_seconds = var.r2_lock_days * 86400 }
    } },
  )
  r2_lock_rules = [
    for id in sort(keys(local.r2_lock_rules_by_id)) : {
      id        = id
      enabled   = true
      prefix    = local.r2_lock_rules_by_id[id].prefix
      condition = local.r2_lock_rules_by_id[id].condition
    }
  ]
}

resource "cloudflare_r2_bucket_lock" "backups" {
  account_id  = local.account_id
  bucket_name = cloudflare_r2_bucket.backups.name
  rules       = local.r2_lock_rules
}

locals {
  r2_write_permission_name = "Workers R2 Storage Bucket Item Write"
}

# The R2 credential is a token owned like the admin token (var.cloudflare_token_owner, recorded by `mcctl init` from
# the admin token's verify): a user-owned admin token creates user tokens (POST /user/tokens), an account-owned one
# (recommended: it can never reach another account) can only create account tokens (POST /accounts/{id}/tokens).
# Exactly one of each pair below exists. Permission groups come from the matching listing.
locals {
  cloudflare_account_owned = var.cloudflare_token_owner == "account"
}

data "cloudflare_api_token_permission_groups_list" "r2_bucket_item_write" {
  count = local.cloudflare_account_owned ? 0 : 1
  name  = replace(local.r2_write_permission_name, " ", "%20")
}

data "cloudflare_account_api_token_permission_groups_list" "r2_bucket_item_write" {
  count      = local.cloudflare_account_owned ? 1 : 0
  account_id = local.account_id
  name       = replace(local.r2_write_permission_name, " ", "%20")
}

locals {
  # Exact-match on the name so a renamed/ambiguous group fails the plan instead of granting the wrong thing.
  r2_write_permission_id = one([
    for g in concat(
      flatten(data.cloudflare_api_token_permission_groups_list.r2_bucket_item_write[*].result),
      flatten(data.cloudflare_account_api_token_permission_groups_list.r2_bucket_item_write[*].result),
    ) : g.id if g.name == local.r2_write_permission_name
  ])
  r2_backups_policies = [{
    effect            = "allow"
    permission_groups = [{ id = local.r2_write_permission_id }]
    resources = jsonencode({
      "com.cloudflare.edge.r2.bucket.${local.account_id}_default_${cloudflare_r2_bucket.backups.name}" = "*"
    })
  }]
}

# R2 S3 credentials derive from an API token, user- or account-owned alike (developers.cloudflare.com
# r2/api/tokens): access key ID = token ID, secret = SHA-256 of the token value.
resource "cloudflare_api_token" "r2_backups" {
  count    = local.cloudflare_account_owned ? 0 : 1
  name     = "r2-${var.r2_backup_bucket}-rw (terraform)"
  policies = local.r2_backups_policies
}

resource "cloudflare_account_token" "r2_backups" {
  count      = local.cloudflare_account_owned ? 1 : 0
  account_id = local.account_id
  name       = "r2-${var.r2_backup_bucket}-rw (terraform)"
  policies   = local.r2_backups_policies
}

# Deployments from before the switch keep their user token (no replacement, no credential churn).
moved {
  from = cloudflare_api_token.r2_backups
  to   = cloudflare_api_token.r2_backups[0]
}

locals {
  r2_access_key_id = one(concat(cloudflare_api_token.r2_backups[*].id, cloudflare_account_token.r2_backups[*].id))
  r2_secret_access_key = sha256(one(concat(
    cloudflare_api_token.r2_backups[*].value, cloudflare_account_token.r2_backups[*].value
  )))
}
