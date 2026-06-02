# S3 + DynamoDB backend for state. Both resources are created MANUALLY
# during initial bootstrap (chicken-and-egg — see docs/setup.md). After
# that, this block tells Terraform where to store state for every
# subsequent apply.
#
# Bucket name and DynamoDB table name are owner-specific and configured
# via `terraform init -backend-config=...` rather than hard-coded here,
# so the same Terraform code is reusable across AWS accounts in the
# unlikely event the project ever moves.

terraform {
  backend "s3" {
    key          = "prog-strength-developer/terraform.tfstate"
    encrypt      = true
    use_lockfile = true
  }
}
