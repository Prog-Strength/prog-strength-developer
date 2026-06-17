# Lock table → lock + run-history table

**Date:** 2026-06-16
**Status:** approved, implementing
**Scope:** schema + write path only (the periodic scanner/metrics emitter is a follow-on)

## Problem

`prog-strength-developer-runs` is keyed on `sow` alone (one mutable item per ticket)
with a TTL on `expires_at`. Each item is overwritten per ticket and eventually
TTL-deleted, so the table holds *current lock state* and never *history*. The real
completion facts (duration, outcome, PRs opened) live only in Pushgateway/Prometheus
(15-day retention) and CloudWatch (7-day). There is no durable record of past
autonomous-developer sessions to scan for aggregate metrics.

## Goal

Keep the existing lock semantics intact while turning the same table into a
permanent, append-only record of every dispatch — so periodic scans can emit
aggregate metrics (counts, durations, failure/retry rates, per-doc-type breakdowns).

## Data model

Move to a **composite key** with two item types sharing each ticket's partition:

```
Table: prog-strength-developer-runs   (PAY_PER_REQUEST, TTL on `expires_at`)
  PK = sow   (ticket path, e.g. "sows/foo.md" | "dx/surface.md")
  SK = sk    (item discriminator)
```

**Lock item** — `sk = "LOCK"`, fixed key, mutable, one per ticket. The atomic
conditional acquire/release happens here — *identical* semantics to today. Carries
`expires_at`, so it stays TTL-eligible for stale-lock reclamation.

```
{ sow, sk:"LOCK", status, dispatch_id, started_at, updated_at,
  expires_at, instance_id?, dispatched_by? }
```

**Run item** — `sk = "RUN#<zero-padded started_at>#<dispatch_id>"`, one per dispatch,
time-sortable, immutable history, **no `expires_at`** so TTL never touches it. A
re-dispatch of the same ticket gets a fresh SK and therefore appends rather than
clobbers.

```
{ sow, sk:"RUN#…",
  dispatch_id, doc_type,            # written at acquire  (sow | dx | future)
  compute_type,                     # written at acquire  (default "ec2"; gha/local later)
  status, started_at, updated_at, dispatched_by?,
  instance_id,                      # written at attach
  outcome, finished_at,             # written at release  (success|error|timeout)
  duration_seconds, prs_opened }    # written at release
```

One TTL config covers both: only LOCK items carry `expires_at`, so RUN rows persist
indefinitely by simply omitting the attribute.

## Write path

Every place the lifecycle touches the LOCK item, it also writes the RUN item — no
new lifecycle stages:

- **`fleet acquire`** → conditional write on the LOCK item (unchanged) **plus** an
  unconditional `PutItem` of a fresh RUN row with `status=working`, `doc_type`,
  `compute_type`, `started_at`, `dispatch_id`, `dispatched_by`.
- **`fleet attach`** → set `instance_id` on the LOCK (unchanged) **plus** on the RUN
  row. The RUN row's SK is recovered from the LOCK item's `started_at`/`dispatch_id`
  returned by the conditional update (`ReturnValues=ALL_NEW`).
- **`fleet release`** → set terminal `status` on the LOCK (unchanged, conditional on
  the instance match) **plus**, only when that conditional succeeds, update the RUN
  row with `outcome`, `finished_at`, `duration_seconds = finished_at - started_at`,
  and `prs_opened`. The worker already computes duration and reads `prs_opened` for
  Pushgateway; this just persists values it has in hand.

A run whose instance dies before release (or a superseded worker whose release is a
no-op) leaves its RUN row stuck at `status=working` with no `finished_at` — a useful
crash/supersede signal a scan can detect, while the LOCK item's TTL independently
frees the ticket.

## doc_type derivation

`doc_type` is derived from the ticket path's leading directory at acquire time via a
`doc_type_for_path()` helper: `sows/…` → `"sow"`, `dx/…` → `"dx"`, otherwise the
leading segment verbatim. The convention is one top-level directory per work type in
prog-strength-docs; new types add a mapping entry. This needs no ticket-file access
in the dispatch workflow (which doesn't check out prog-strength-docs) and no new
worker plumbing. The frontmatter `type:` stays the worker's authoritative routing
key; the run-row `doc_type` is a recordkeeping denormalization that is 1:1 with it.

## Lock semantics — unchanged

The conditional acquire (`attribute_not_exists OR status IN terminal OR
expires_at <= now`) and holder-verified release operate **only on the `sk="LOCK"`
item**. RUN rows are never conditional and never contend. The `RunRecord` lock type
and the existing contract tests' invariants carry over verbatim; lock operations
just gain `sk="LOCK"` on their key.

## Migration

DynamoDB cannot add a sort key in place, so this **forces a table recreate** in
Terraform. The table holds only ephemeral lock state today (real completion data
lives in Pushgateway/CloudWatch), so recreating is low-risk — apply during a window
with no active dispatch. No backfill; history accrues from first dispatch onward.

## Out of scope (follow-on)

The periodic scanner/metrics emitter is not in this design. The table is shaped so a
future scheduled job can `Scan` with `FilterExpression: begins_with(sk, "RUN#")` (or
add a GSI on `doc_type`/`status` if scan cost ever matters). Capturing token/cost per
run is deferred — the worker doesn't surface that today.
