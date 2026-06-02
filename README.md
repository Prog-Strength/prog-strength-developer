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
