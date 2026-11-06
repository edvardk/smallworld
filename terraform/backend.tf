# State lives in a Cloudflare R2 bucket (server.yml `state.*`). This block holds only the product's fixed
# settings; the deployment's bucket and key come from the config repo:
#   mcctl render  ->  <config>/.mcctl/rendered/terraform/state.tfbackend
#   terraform init -backend-config=<that file>
# Credentials and endpoint come from the environment (the config repo's .env):
#   AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_ENDPOINT_URL_S3
terraform {
  backend "s3" {
    region = "auto"

    use_path_style              = true
    use_lockfile                = true
    skip_credentials_validation = true
    skip_region_validation      = true
    skip_requesting_account_id  = true
    skip_metadata_api_check     = true
    skip_s3_checksum            = true
  }
}
