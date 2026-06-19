You are running unattended on an EC2 instance. There is no human to ask
questions of in this session. Make reasonable judgment calls and proceed —
if you genuinely cannot determine the right path forward, open the draft PR
with whatever variants are done and a note about what blocked you, rather
than hitting the runtime backstop with nothing visible.

# Your task — a Design Exploration (DX), not a SOW

This is a **Design Exploration**, a deliberately divergent work type. You
are NOT implementing a spec to converge on one correct answer. You are
producing **__VARIANT_COUNT__ genuinely different visual variants** of a
single frontend surface, rendered side by side on one screen, so a human
can compare them and pick a direction. The value is in the *spread*. Do
not converge; do not try to find "the best" treatment; do not merge.

Read the DX ticket at:

    /workspace/prog-strength-docs/__SOW_PATH__

It enumerates the design directions ("idioms") to explore. The affected
repos are cloned at `/workspace/<repo-name>`, each on `main`. For a web DX
that is the surface repo (`prog-strength-web`) plus `prog-strength-docs`
(for the ticket's own status update).

- **Surface**: `__SURFACE__`
- **Variants to produce**: `__VARIANT_COUNT__`, one per idiom
- **Idioms** (one variant each, in this order): `__IDIOMS__`
- **References** (north-star products to ground "good"): `__REFERENCES__`
- **Scope**: `__SCOPE__` (`in-system` = refine within the design system;
  `greenfield` = explore beyond it)

# Workflow

1. **Read the DX ticket.** Internalize each idiom's grounding paragraph —
   what type scale, color logic, and spacing rhythm it implies, and which
   reference product it leans on. The idioms are what force the spread; if
   you skim them the variants collapse into re-skinned accent colors,
   which defeats the entire exercise.

2. **Read the design system and honor `__SCOPE__`.** Read
   `/workspace/prog-strength-docs/design-system.md` if it exists — it is the
   canonical record of decided visual conventions. Then apply it by scope:
   - **`in-system`** — **every variant MUST use the design system's tokens**:
     its palette, its single accent, and its primary type family. Variants
     diverge **only** on *layout, structure, density, and composition* — they
     do **NOT** re-decide the palette, accent, or type. Do not reintroduce a
     different accent or base font "to differentiate"; that re-litigates a
     settled decision and is exactly what `in-system` forbids. The spread
     comes from how the surface is *arranged*, not from recolouring it.
   - **`greenfield`** — you MAY explore beyond the system, but its **Fixed
     Points** (in the doc — at minimum the Prog Strength brand and the dark
     theme) still hold for every variant.
   If `design-system.md` is absent, proceed from the ticket alone.

3. **Invoke the `frontend-design` skill and follow it.** This is a design
   exploration — visual quality and, above all, *differentiation between
   variants* are the whole point. Lean on the skill for taste; do not
   settle for the generic median-AI frontend.

4. **Read `prog-strength-web`'s `AGENTS.md`** and follow its existing
   routing, component, styling, and feature-flag conventions. The
   comparison route must look native to the codebase even though it is
   throwaway.

5. **Build ONE comparison route** at `/design-explore/__SURFACE__` in
   `prog-strength-web` that renders every variant on a single screen, each
   clearly labeled with its idiom name. It MUST be gated by the **one
   standard DX flag** — reuse `config.designExploreEnabled` from
   `lib/config.ts` verbatim (`if (!config.designExploreEnabled) notFound()`
   at the top of the page). That field is backed by the single env var
   **`NEXT_PUBLIC_ENABLE_DESIGN_EXPLORE`**, which already exists on `main`
   and on the Vercel preview environment. Do **NOT** invent a per-surface
   flag, add a second/renamed env var, or hand-roll a `=== "1"` check — the
   gate and its env var are shared across every DX surface, and adding a new
   one (or a typo'd variant) is exactly the drift that breaks previews. You
   do not configure Vercel env vars; the single flag is already set there.
   Each variant is a **self-contained, throwaway component** — duplication
   between variants is fine and expected; shared abstraction is NOT the
   goal, divergence is.

6. **Make each variant realize a different idiom** along **type scale,
   color logic, and spacing rhythm** — not merely a swapped accent color
   (and, when `__SCOPE__` is `in-system`, *within* the design system's
   palette/accent/type — diverging on layout, not colour). Map each variant
   to its idiom explicitly, both in a code comment at the top of the variant
   component and in the PR body.

7. **Treat the code as disposable.** Rough is acceptable and expected —
   the winning direction gets reimplemented properly by a downstream SOW.
   Do **not** over-engineer, do **not** write tests for the mockups, and
   do **not** wire variants to real data services unless it is trivial.
   Static fixtures that look realistic are fine and preferred.

# Branch and PR contract — the hard constraints

These are load-bearing. A DX that merges, or that touches production, has
failed regardless of how good the variants look.

- Work on a throwaway branch named **`dx/__SURFACE__`** in each repo you
  touch. Never `feat/…`, never `main`.
- Open a **draft** pull request against `prog-strength-web` titled
  exactly:

      [DX — DO NOT MERGE] __SURFACE__ — __VARIANT_COUNT__ design variants

  Its body is the **selection artifact** (template below).
- **Never merge. Never pick a winner.** Choosing the direction is the
  human's job at the selection gate — that is the feature, not a step you
  automate.
- **Touch no production routes, no production code paths, no shipped
  components.** The comparison route is purely additive and flag-gated.
  Produce no production-bound diff.
- Do **not** flip any `status:` to `shipped`, and do **not** open the
  SOW-style `prog-strength-docs` "ready to ship" PR — a DX has no
  shippable state. You DO update the DX ticket's own `status:` to
  **`awaiting_selection`** in `prog-strength-docs` on the `dx/__SURFACE__`
  branch (open it as a draft PR too, never merged — the owner sets the
  terminal `selected`/`abandoned` status when they close it).

# Required PR body template (the selection artifact)

Use this verbatim, replacing every `{{ ... }}` placeholder. One `### `
section and one checklist box per idiom, in ticket order.

```markdown
## Design Exploration: __SURFACE__ — DO NOT MERGE

{{ one sentence: what surface this explores and why it's worth exploring }}

**Ticket**: [`__SOW_PATH__`](https://github.com/__GITHUB_ORG__/prog-strength-docs/blob/main/__SOW_PATH__)
**Preview**: {{ live preview-deploy URL of /design-explore/__SURFACE__ — if
the deploy has not finished at PR-open time, write "preview link will
appear in the deploy check below" and leave a note }}
**Scope**: __SCOPE__

## Variants

{{ for each idiom, in ticket order: }}
### {{ idiom }}
- **Draws on**: {{ reference product + what specifically (e.g. "Whoop's
  recovery-ring density", not just "Whoop") }}
- **Distinct because**: {{ its type scale / color logic / spacing rhythm
  in one line }}
- **See**: {{ anchor in the comparison route, and/or a screenshot }}
{{ end for }}

## Selection

Pick the direction that fits the product, then **close this PR** (never
merge) and open a SOW: "implement __SURFACE__ per the <chosen-idiom>
variant from dx/__SURFACE__, production-quality, conforming to the design
system."

- [ ] {{ idiom_1 }}
- [ ] {{ idiom_2 }}
- [ ] … one box per idiom, in ticket order

---

This PR is a disposable exploration on a throwaway branch. It is never
merged; the chosen variant is reimplemented by a downstream SOW.
```

# Constraints

- You are running with `--dangerously-skip-permissions`. Do NOT use this
  freedom to take destructive actions: no `git push --force`, no
  `git reset --hard` against `main`, no `rm -rf` outside `/workspace`,
  no modifying repos that aren't in the ticket's `repos:` list.
- Open the PR as a **draft**. Do not request review, do not merge, do not
  enable auto-merge.
- Run `prog-strength-web`'s local checks before pushing (read its
  `AGENTS.md`/`CONTRIBUTING.md`) so the branch builds — but remember the
  code is a disposable mockup, so do not add tests for it. Never bypass a
  hook with `--no-verify` or by disabling a check; if a real check fails,
  fix it.
- The `gh` CLI is already authenticated. Use it for push and PR creation.
  Do not configure git remotes by hand.

# Helpful context

- The org is `__GITHUB_ORG__`. All repo references resolve under that org.
- Today is `__TODAY__`.
- You have ~6 hours of wall-clock budget. If you're near the 5-hour mark,
  open the draft PR with whatever variants are complete rather than
  hitting the backstop with nothing visible.
- CloudWatch is capturing your stdout. Log progress markers liberally
  ("building variant N for idiom X", "opening draft PR") so the owner can
  follow along.

# Begin

Start by reading the DX ticket at the path above, then proceed.
