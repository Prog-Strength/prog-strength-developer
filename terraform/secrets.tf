# Secrets Manager refs. Both secrets are CREATED and SEEDED manually
# during initial bootstrap (see docs/setup.md). Terraform does not
# manage their contents because their values are owner-private (the
# Claude OAuth refresh token and the GitHub App private key), and
# committing them to state would be a leak risk.
#
# These data sources verify the secrets exist before Terraform proceeds
# and surface their ARNs for use in IAM policies + userdata.

data "aws_secretsmanager_secret" "claude_credentials" {
  name = "prog-strength-developer/claude-credentials"
}

data "aws_secretsmanager_secret" "github_app" {
  name = "prog-strength-developer/github-app"
}
