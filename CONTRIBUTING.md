# Contributing to prog-strength-developer

This repo ships infrastructure and automation for the autonomous
developer platform. Contributions land via pull request and merge
through GitHub's **squash-merge** button. Releases are cut by
[`semantic-release`](https://semantic-release.gitbook.io/) on every push
to `main`, driven entirely by commit subjects.

If a commit subject isn't a [Conventional Commit](https://www.conventionalcommits.org/),
**no release is cut**, and whatever it shipped (worker behavior, dashboard
fix, dispatch UX) sits on `main` un-versioned and un-changelogged until
the next conventional commit happens to land.

That has bitten us once already. This document is the fix.

## The one rule

**Every PR title and every commit subject must be a Conventional Commit.**

Format:

```
<type>(<scope>): <imperative subject>
```

Examples:

- `fix(dashboards): render Running workers started_at as a date`
- `feat(worker): install superpowers plugin on boot`
- `ci(dispatch): surface aws logs tail command in dispatch summary`
- `docs: explain the manager + worker architecture`

The PR title rule is the one that gets forgotten because individual
commits look right while the title doesn't. **The squash-merge button
uses the PR title as the merge commit subject** — so an unconventional
PR title becomes an unconventional commit on `main` no matter how well
the individual commits were named.

## Types and release impact

`semantic-release`'s default
[`commit-analyzer`](https://github.com/semantic-release/commit-analyzer)
preset reads the type prefix and decides whether to release:

| Type | Release | Use for |
|---|---|---|
| `feat` | **minor** (0.4.2 → 0.5.0) | New user-visible capability — worker behavior, dashboard panel, workflow option |
| `fix` | **patch** (0.4.2 → 0.4.3) | Bug fix that restores intended behavior |
| `perf` | **patch** | Performance improvement with no behavioral change |
| `docs` | none | Documentation only |
| `ci` | none | GitHub Actions / release pipeline changes |
| `build` | none | Build system, dependency manager configuration |
| `chore` | none | Tooling, semantic-release housekeeping (don't author by hand — automated `chore(release):` commits come from semantic-release itself) |
| `refactor` | none | Code restructuring with no behavior change |
| `test` | none | Adding or fixing tests |
| `style` | none | Whitespace, formatting (rare here — `terraform fmt` and similar) |
| `revert` | depends | Inherits the bump of whatever it reverts |

Anything not in the table above will be treated as "no release" by the
default analyzer.

### Breaking changes

A breaking change cuts a **major** release (0.x.x → 1.0.0, 1.x.x → 2.0.0).
Mark with either:

- `!` after the type/scope: `feat(worker)!: drop the legacy SOW slug format`
- A `BREAKING CHANGE:` paragraph in the commit body explaining what
  breaks and how to migrate.

The project is intentionally pre-1.0; major bumps are reserved for
actually backwards-incompatible changes (launch template inputs,
manager API surface, dashboard JSON breakage). Don't paper them over.

## Scopes used in this repo

Pick the closest fit. Add a new one if nothing matches — but try first.

- `dashboards` — Grafana dashboard JSON, panel queries, transformations
- `monitoring` — Prometheus, Pushgateway, Loki, Caddy, manager compose stack
- `worker` — bootstrap/userdata.sh.tpl, worker_exporter, anything that
  changes how a worker boots, scrapes, or terminates
- `manager` — manager EC2 lifecycle, EBS, EIP, manager-userdata
- `iam` — IAM roles, policies, trust relationships
- `vpc` — VPC, subnets, security groups, route tables
- `dispatch` — `.github/workflows/dispatch-sow.yml` and the rendered
  userdata path it uses
- `ci` — other GitHub Actions workflows (`apply.yml`, `plan.yml`,
  `deploy-manager.yml`, `release.yml`)
- `readme`, `docs`, `agents` — when scoping `docs:` or `feat(docs):`
  to a specific surface

A subject with no obviously-right scope can drop it: `docs: bump
required terraform version`. Don't invent decorative scopes.

## Squash merge: the gotcha that ate v0.5.0

`semantic-release` analyzes commit subjects on `main` since the last
`v*` tag. Squash-merge collapses every commit on the PR branch into
**one** commit whose subject defaults to the PR title.

The implication:

- ✅ Individual conventional commits on the branch don't matter to
  the release analyzer — they're discarded on merge.
- ✅ The PR title is what matters. Make it conventional.
- ✅ If a PR ships multiple bumps (a `feat:` and a `fix:` in one
  branch), pick the highest-impact type for the PR title. Mention
  the other in the body.

If a PR sneaks through with an unconventional title and lands on
`main`, the release for that PR's work is lost. The next conventional
commit will cut a release whose CHANGELOG includes only the new
commit; the lost work has to be acknowledged in the body of the
follow-up commit so future readers can find it.

## Verify before merging

1. **Look at the PR title.** Does it start with one of the types
   above followed by `:` (optionally `(scope):`)?
2. **Run `npx semantic-release --dry-run`** on a clone of the PR branch
   (with `main` fast-forwarded) to preview the bump. Optional but
   removes all guessing.
3. **Squash-merge** when CI is green. The merge commit subject
   defaults to the PR title — confirm it looks right in the
   confirmation dialog before clicking through.

## A few worked examples

A bug fix in the dashboard pipeline:

```
fix(dashboards): hide noise columns from Running workers table

labelsToFields surfaces every label on developer_worker_info, which
includes __name__, job, and the exported_* duplicates Pushgateway
adds under honor_labels. Hide them via an organize transformation
so the table focuses on instance_id, sow, started_at.
```

A new capability, breaking change marked with `!`:

```
feat(worker)!: drop legacy SOW slug format

The old slug derived from the SOW filename; we now derive it from
the frontmatter's `slug:` field for stability across SOW renames.

BREAKING CHANGE: any in-flight worker still using the old slug
format will create branches under the wrong name. Drain the fleet
before rolling this.
```

A docs-only change with no release:

```
docs(readme): explain why the manager runs Caddy

The application Caddy can't reach the manager VPC, so the manager
hosts its own Caddy. Note the trade-off here so the next reader
doesn't try to consolidate them.
```

## When the rule doesn't apply

The only commits on `main` that aren't authored by a human are the
automated `chore(release): <version> [skip ci]` commits semantic-release
pushes itself. Those are excluded from the analyzer by the `[skip ci]`
marker. You should never write a `chore(release):` commit by hand.
