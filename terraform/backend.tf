# S3 + DynamoDB backend for state. Both resources are created MANUALLY
# during initial bootstrap (chicken-and-egg — see docs/setup.md). After
# that, this block tells Terraform where to store state for every
# subsequent apply.
#
# Bucket name and the state key are configured via
# `terraform init -backend-config=...` rather than hard-coded here, so the
# same Terraform code is reusable across AWS accounts and environments. The
# key resolves to prog-strength-developer/<env>/terraform.tfstate; CI
# defaults the env to prod and enables dev/stg by setting TF_STATE_ENV.

terraform {
  backend "s3" {
    encrypt      = true
    use_lockfile = true
  }
}
