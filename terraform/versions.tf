terraform {
  required_version = "~> 1.17.1"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.28.1"
    }
    tailscale = {
      source  = "tailscale/tailscale"
      version = "0.29.3"
    }
    github = {
      source  = "integrations/github"
      version = "6.13.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "3.9.1"
    }
  }
}

# Credentials come from the environment (see .env.example):
#   CLOUDFLARE_API_TOKEN, GITHUB_TOKEN,
#   TAILSCALE_OAUTH_CLIENT_ID + TAILSCALE_OAUTH_CLIENT_SECRET (a Tailscale OAuth client; README lists its
#   scopes and tags). No `scopes` argument: the access token then carries all of the client's scopes.
#   Exception: the very first apply of the ACL uses a one-off TAILSCALE_API_KEY (README, first-time setup).
provider "cloudflare" {}

provider "tailscale" {}

provider "github" {
  owner = var.github_owner
}
