# --------------------------------------------------------------------
# Run registry: two item types per ticket, discriminated by sort key.
# Read/written by the `fleet` control-plane package: the dispatch
# workflow acquires (conditional write) before launching, and the worker
# releases on finalize. See prog-strength-docs/sows/fleet-dispatch-gating.md.
#
#   sk = "LOCK"           one mutable lock item per ticket — the
#                         conditional write that guarantees at most one
#                         active worker per ticket. Carries expires_at;
#                         stale locks self-heal via that TTL.
#   sk = "RUN#<ts>#<id>"  one immutable run-history item appended per
#                         dispatch — the durable record of every
#                         autonomous-developer session (status, outcome,
#                         duration, PRs opened, doc_type, compute_type)
#                         for periodic aggregate-metric scans. These
#                         carry NO expires_at, so the TTL never reaps them.
#
# NOTE: adding the sort key is a key-schema change DynamoDB cannot make
# in place — `terraform apply` REPLACES the table. The table holds only
# ephemeral lock state (real history starts accruing post-apply), so
# apply during a window with no active dispatch.
#
# Pay-per-request: dispatch volume is a handful of writes per day, so
# there is no capacity worth provisioning or paying for at rest.
# --------------------------------------------------------------------

resource "aws_dynamodb_table" "runs" {
  name         = "prog-strength-developer-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "sow"
  range_key    = "sk"

  attribute {
    name = "sow"
    type = "S"
  }

  attribute {
    name = "sk"
    type = "S"
  }

  # TTL is cleanup only, and applies only to LOCK items (RUN# items omit
  # expires_at and so persist indefinitely). Lock *correctness*
  # (reclaiming a stale lock) is enforced by the `expires_at <= now` term
  # in the acquire condition, because DynamoDB applies TTL deletion only
  # on a best-effort, delayed basis and must never be relied on to free a
  # ticket promptly.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
