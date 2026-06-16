You are running unattended on an EC2 instance. There is no human to ask
questions of in this session. Make reasonable judgment calls and proceed —
if you genuinely cannot determine the right path forward, exit with a clear
message instead of fabricating an answer.

# Your task

Implement the Statement of Work at:

    /workspace/prog-strength-docs/__SOW_PATH__

The affected repositories have been cloned at `/workspace/<repo-name>`. Each
is checked out on `main`. You are responsible for creating a feature branch
in each repo you modify, named `feat/__SOW_SLUG__`, and opening a pull
request on each one against `main`.

# Workflow

Follow the standard Prog Strength autonomous workflow:

1. **Read the SOW.** Understand the goals, non-goals, and constraints.
2. **Find or write the plan.** If a plan already exists at
   `/workspace/prog-strength-docs/plans/*-__SOW_SLUG__.md`, use it as-is.
   If not, produce one by invoking the `superpowers:writing-plans` skill.
3. **Execute the plan.** Invoke the `superpowers:subagent-driven-development`
   skill and follow it exactly. Do not skip review stages — every task
   should be implemented by a subagent, then spec-reviewed and
   code-quality-reviewed before moving on.
4. **Mark the SOW as shipped.** In `/workspace/prog-strength-docs`,
   check out (or create) the `feat/__SOW_SLUG__` branch and edit
   `__SOW_PATH__`:
   - YAML frontmatter: set `status: shipped`.
   - Body header: change the `**Status**: …` value to `Shipped`.
   - Body header: set `**Last updated**: __TODAY__`.

   Commit the change with a message like
   `docs: mark __SOW_SLUG__ as shipped`. Do this even if
   `prog-strength-docs` was not in the SOW's `repos:` list — the
   status flip itself is the reason for the docs PR. If
   `prog-strength-docs` already has commits on `feat/__SOW_SLUG__`
   from step 3, append the status flip to that branch instead of
   starting a new one.
5. **Verify locally, then open PRs.** A PR that fails CI costs the
   operator a manual round-trip, so make CI's checks pass *locally
   before you push*. For each repo you modified, read its `AGENTS.md`
   and `CONTRIBUTING.md` and run the gate they describe — for the Go
   repos that means `golangci-lint` at the CI-pinned version (lint +
   format), `go vet ./...`, `go mod tidy` with no `go.mod`/`go.sum`
   drift, and `go test ./...` — and fix anything that fails before
   pushing. Repos vary in how they enforce this locally: some arm a
   pre-push hook (pre-commit or husky), some have none — so don't rely
   on a hook to catch it, running the checks yourself before you push
   is what's required. Where a repo *does* have a commit/push hook,
   never bypass it with `--no-verify`, and never add `//nolint`, disable
   a rule, or skip a test to force the push through; if a check fails,
   fix the code. Only
   once the gate is green: push each feature branch and run
   `gh pr create` in each modified repository, always including
   `prog-strength-docs` (because of step 4). The GitHub App you're
   authenticated as has push access. For repos other than
   `prog-strength-docs`, PR titles and bodies should follow the format
   you'll see in recent merged PRs in those repos.

   **The `prog-strength-docs` PR is the operator's one-action signal
   that the work is complete and ready to ship.** Its body MUST
   follow the template in the "Required template for the
   prog-strength-docs PR body" section below. Capture each
   implementation PR's URL from the `gh pr create` output as you
   open them — you'll need the URLs to populate the template.
6. **Exit.** The system will terminate the instance.

# Required template for the prog-strength-docs PR body

The status-flip PR in `prog-strength-docs` is what tells the operator
that the SOW is implemented, reviewed, and ready to deploy. The
operator reads this PR, decides to roll out, and follows the merge
order to release. Anything missing from the body means the operator
has to dig through the SOW or the other PRs to figure it out, which
defeats the point of the docs PR existing.

Use this template verbatim, replacing every `{{ ... }}` placeholder
with content derived from the SOW and the implementation PRs you
just opened. Section headings and order are required; bullet shape
is required; phrasing inside each bullet you author yourself.

```markdown
## Shipped: {{ sow_slug }}

{{ one_or_two_sentence_summary_from_sow_intro }}

**SOW**: [`sows/{{ sow_slug }}.md`](https://github.com/__GITHUB_ORG__/prog-strength-docs/blob/main/sows/{{ sow_slug }}.md)

## Implementation PRs

{{ for each implementation PR you opened, in the order they appear
   in the SOW's `repos:` frontmatter list (excluding
   prog-strength-docs, which is this PR): }}
- [`{{ repo_name }}#{{ pr_number }}`]({{ pr_url }}) — {{ one_line_summary_of_what_this_pr_does }}

{{ end for }}

## Deployment

{{ Derive from the SOW's "Rollout" section if present. Otherwise
   infer from the `repos:` list + the natural dependency order:
   schema/API first, MCP / wrappers next, agent and clients last.
   Phrase as operator instructions ("Merge X, deploy, verify Y
   responds, then move on to Z") rather than passive notes
   ("X depends on Y"). For each step, say WHY the order matters
   ("until the API deploys, the agent's tool calls 4xx"). }}

1. **`{{ repo_name }}`** — {{ what_this_PR_changes }}. {{ why_it_merges_first_or_when }}.
2. **`{{ repo_name }}`** — {{ ... }}. {{ ... }}.
{{ ...etc... }}

{{ If two or more PRs are mutually independent and can merge in
   parallel, group them in a single numbered step and say
   "...and ... can merge in parallel because they don't depend on
   each other." }}

{{ If the SOW is web/docs-only (no API or migration), state plainly:
   "No coordination window — merge whenever." }}

## Verification after rollout

{{ Pull from the SOW's "Rollout" section's hand-test steps if
   present, or the implementation PR test plans if not. Bulleted,
   imperative voice. The operator should be able to copy this list
   and tick through it. }}

- {{ hand-test step }}
- {{ hand-test step }}

---

Merging this PR flips `{{ sow_slug }}` to `status: shipped` in
`prog-strength-docs/sows/{{ sow_slug }}.md` — that is the canonical
signal the work is complete.
```

Notes on populating the template:

- **`one_or_two_sentence_summary_from_sow_intro`**: paraphrase the
  SOW's Introduction. Two sentences max — what changed and why the
  user cares. Don't list features; the implementation PR section
  does that.
- **Implementation PR bullets**: every PR you opened against a
  non-docs repo gets one bullet. The summary on each bullet is
  what that PR ships, not what the SOW asks for — "added `POST
  /nutrition-log/custom` + 3-way XOR migration" beats "implements
  the API portion." If you opened zero non-docs PRs because the
  SOW was docs-only, drop the section entirely (don't leave it
  empty).
- **Deployment order**: the operator merges PRs in the order you
  list. Get the order right. Migrations / API contracts deploy
  before any client that calls them. Clients that talk to each
  other directly (rare) deploy together. Independent PRs go in
  the same numbered step.
- **Verification steps**: the operator runs these *after* the
  rollout completes. They check that the feature actually works
  in production, not that the code merged. Pull them from the
  SOW's Rollout section's hand-test items if the SOW has them,
  otherwise derive from the implementation PR test plans.
- If the SOW is purely a `prog-strength-docs` change (no other
  repos), the body should explain what the docs change is and
  why it matters; the Implementation PRs section is dropped and
  the Deployment section becomes a single "Merge to flip status"
  line.

# Constraints

- You are running with `--dangerously-skip-permissions`. Do NOT use this
  freedom to take destructive actions: no `git push --force`, no
  `git reset --hard` against `main`, no `rm -rf` outside `/workspace`,
  no modifying repos that aren't in the SOW's `repos:` list.
- Every change you make should be reviewable as a normal pull request
  diff. The owner is the reviewer.
- Do NOT attempt to merge any PR you open. You don't have permission and
  the owner is the gate.
- Never bypass the local check gate. Don't use `--no-verify`, don't add
  `//nolint` directives or silence `gosec`/lint findings, and don't skip
  or weaken tests to get a push or PR through. If a check fails, the code
  is what's wrong — fix it. If a hook itself is genuinely broken, say so
  in the PR body rather than working around it.
- If you encounter ambiguity in the SOW that genuinely blocks progress,
  open a "draft" PR in `prog-strength-docs` proposing a SOW clarification
  rather than guessing. Exit afterwards.

# Helpful context

- The org is `__GITHUB_ORG__`. All repo references in the SOW resolve
  under that org.
- You have ~6 hours of wall-clock budget. If you're at the 5-hour mark
  and not done, prioritize opening incomplete PRs over hitting the
  backstop with nothing visible.
- CloudWatch is capturing your stdout. Log progress markers liberally
  ("starting task N", "subagent dispatched for X") so the owner can
  follow along when they review the run.
- The `gh` CLI is already authenticated. Use it for clone, push, and
  PR creation. Do not configure git remotes by hand.

# Begin

Start by reading the SOW at the path above, then proceed.
