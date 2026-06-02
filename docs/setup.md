# Setup runbook (one-time)

The autonomous developer cannot build itself the first time. Walk through
these steps in order. Estimated time: 30–45 minutes.

## Prerequisites

- An AWS account where you have admin (or close-to-admin) permissions for IAM,
  VPC, EC2, Secrets Manager, CloudWatch, S3, and DynamoDB.
- The AWS CLI installed locally and configured (`aws configure`).
- Terraform 1.10+ installed locally (tfenv recommended; `.terraform-version`
  in this repo will be honored).
- The GitHub CLI (`gh`) installed and authenticated against your account.
- Admin access to the `Prog-Strength` GitHub organization.

## 1. Create the prog-strength-developer repository on GitHub

```bash
gh repo create Prog-Strength/prog-strength-developer \
  --public \
  --description "Autonomous developer for Prog Strength" \
  --confirm
```

(Or skip and create via the GitHub UI.)

## 2. Create the GitHub App

Go to https://github.com/organizations/Prog-Strength/settings/apps/new and
create an app named **Prog Strength Developer** with:

- **Homepage URL:** `https://github.com/Prog-Strength/prog-strength-developer`
- **Webhook:** uncheck "Active" (we don't use webhooks).
- **Repository permissions:**
  - Contents: Read & write
  - Pull requests: Read & write
  - Issues: Read & write
  - Workflows: Read & write
  - Metadata: Read-only
- **Organization permissions:** none.
- **Where can this app be installed?** Only on this account.

After creating:

1. Generate a private key (Settings → General → Private keys → Generate
   a private key). A `.pem` file downloads. Keep it safe; you'll paste
   it into Secrets Manager.
2. Install the App on the org (left sidebar → Install App → Prog-Strength).
   Choose "All repositories" for v1. Note the installation ID from the
   URL after install (it's the last path segment).
3. Note the App ID from the General settings page.

## 3. Set up the AWS GitHub Actions OIDC provider

The provider is account-level and only needs to exist once. If you've used
GHA OIDC against this account before, skip.

```bash
aws iam create-open-id-connect-provider \
  --url https://token.actions.githubusercontent.com \
  --client-id-list sts.amazonaws.com \
  --thumbprint-list 6938fd4d98bab03faadb97b34396831e3780aea1
```

(The thumbprint is GitHub's documented value; AWS will accept it.)

## 4. Create the Terraform state bucket

The state bucket is managed OUTSIDE Terraform (chicken-and-egg — Terraform
can't create the bucket it needs to store its own state). Pick a name that
includes your AWS account ID for uniqueness.

State locking uses Terraform 1.10+'s native S3 lockfile mode
(`use_lockfile = true` in `backend.tf`). No DynamoDB table is required.

```bash
export TF_BUCKET="prog-strength-tfstate-$(aws sts get-caller-identity --query Account --output text)"

aws s3api create-bucket --bucket "$TF_BUCKET" --region us-east-2 \
  --create-bucket-configuration LocationConstraint=us-east-2
aws s3api put-bucket-versioning --bucket "$TF_BUCKET" --versioning-configuration Status=Enabled
aws s3api put-bucket-encryption --bucket "$TF_BUCKET" \
  --server-side-encryption-configuration '{"Rules":[{"ApplyServerSideEncryptionByDefault":{"SSEAlgorithm":"AES256"}}]}'
```

## 5. Push this repository

If you haven't already pushed the local repo to GitHub:

```bash
cd /Users/jimmywallace/Desktop/prog-strength/repos/prog-strength-developer
git remote add origin https://github.com/Prog-Strength/prog-strength-developer.git
git push -u origin main
git push -u origin feat/initial-bootstrap
```

## 6. First terraform apply (local, NOT from CI)

This creates VPC + IAM roles + log group + Secrets Manager references. The
references will fail unless the secrets exist, so we'll create empty secrets
first.

```bash
aws secretsmanager create-secret \
  --name prog-strength-developer/claude-credentials \
  --secret-string '{"placeholder": true}'

aws secretsmanager create-secret \
  --name prog-strength-developer/github-app \
  --secret-string '{"placeholder": true}'

cd terraform
terraform init \
  -backend-config="bucket=$TF_BUCKET" \
  -backend-config="region=us-east-2"

terraform apply
```

Confirm and apply. Output includes the GitHub Actions role ARN — save it.

## 7. Configure GitHub Actions secrets and variables

In the prog-strength-developer repo on GitHub → Settings → Secrets and
variables → Actions:

**Variables (not Secrets):**
- `TF_STATE_BUCKET` — the bucket name from step 4.

**Secrets:**
- `AWS_GHA_ROLE_ARN` — the `github_actions_role_arn` Terraform output.

## 8. Seed the real secrets

```bash
# Claude OAuth credentials — must do `claude login` locally first if not done.
aws secretsmanager put-secret-value \
  --secret-id prog-strength-developer/claude-credentials \
  --secret-string "$(cat ~/.claude/credentials.json)"

# GitHub App. Replace APP_ID, INSTALLATION_ID, and the key path.
APP_ID=123456
INSTALLATION_ID=7890123
PRIVATE_KEY_PATH=~/Downloads/prog-strength-developer.YYYY-MM-DD.private-key.pem

cat > /tmp/gh-app.json <<EOF
{
  "app_id": $APP_ID,
  "installation_id": $INSTALLATION_ID,
  "private_key": $(jq -Rs . < "$PRIVATE_KEY_PATH")
}
EOF

aws secretsmanager put-secret-value \
  --secret-id prog-strength-developer/github-app \
  --secret-string "file:///tmp/gh-app.json"

rm /tmp/gh-app.json
```

## 9. Trivial-SOW smoke test

Add a minimal SOW to `prog-strength-docs/sows/test-developer-bootstrap.md`:

```markdown
---
status: ready_for_implementation
repos:
  - prog-strength-docs
---

# Test: Developer Bootstrap

**Status:** Test only · **Last updated:** TODAY

Adds a single line "Tested by prog-strength-developer on YYYY-MM-DD" to
the bottom of `README.md` in prog-strength-docs.
```

Push it, then in the prog-strength-developer repo on GitHub: Actions →
Dispatch SOW → run with `sows/test-developer-bootstrap.md`.

Within ~15 minutes you should see a PR open on prog-strength-docs and the
EC2 instance terminate. Watch progress in CloudWatch
(`/aws/ec2/prog-strength-developer/<instance-id>`).

## You're done

Subsequent SOWs are dispatched via the same workflow. Re-seed
`claude-credentials` every few months when the OAuth refresh token expires
(see `troubleshooting.md`).
