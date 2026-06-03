# prog-strength-developer

Autonomous developer for [Prog Strength](https://github.com/Prog-Strength). Spins up
an ephemeral EC2 worker on demand, runs Claude Code against a designated SOW
from [prog-strength-docs](https://github.com/Prog-Strength/prog-strength-docs),
opens PRs in each affected repo, and self-terminates.

See `docs/README.md` for the system overview and `docs/setup.md` for
first-time bootstrap.

## Quick links

- **Dispatch a SOW:** Actions tab → "Dispatch SOW" → Run workflow → paste the SOW path.
- **Watch a run:** CloudWatch log group `/aws/ec2/prog-strength-developer/<instance-id>`.
- **Debug a stuck worker:** `aws ssm start-session --target <instance-id>`.

## SOW

This repo implements `prog-strength-docs/sows/prog-strength-developer.md`.

## Infrastructure

Terraform changes flow through PRs:

- Opening a PR runs `terraform plan` and posts a sticky comment showing what would change. Don't merge without that comment saying either "No changes" or a reviewed diff.
- Merging to main runs `terraform apply -auto-approve` against the persistent infra (launch template, IAM, VPC, secrets, log group). Worker instances themselves are still launched separately via "Dispatch SOW".

The plan/apply, dispatch-SOW, and release workflows share the `terraform-apply-prod` concurrency group so they queue on the state lock instead of racing.

## Releases

Versioning is automated via [semantic-release](https://semantic-release.gitbook.io). Every push to main analyzes conventional-commit subjects, picks the next semver bump, writes `CHANGELOG.md`, creates a GitHub Release with notes, and pushes a `vX.Y.Z` tag.

One-time bootstrap (must run **before** the first PR merges to main after this is enabled):

```bash
git checkout main && git pull
git tag -a v0.0.0 -m "Initial baseline"
git push origin v0.0.0
```

Without this baseline, semantic-release would default the first release to `1.0.0`. The `v0.0.0` tag pins it to the `0.x` range until the project is ready to declare a stable API.
