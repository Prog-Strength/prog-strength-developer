# --------------------------------------------------------------------
# Run registry: one item per SOW — the lock that guarantees at most one
# active worker per SOW. Read/written by the `fleet` control-plane
# package: the dispatch workflow acquires (conditional write) before
# launching, and the worker releases on finalize. Stale locks self-heal
# via the `expires_at` TTL. See
# prog-strength-docs/sows/fleet-dispatch-gating.md.
#
# Pay-per-request: dispatch volume is a handful of writes per day, so
# there is no capacity worth provisioning or paying for at rest.
# --------------------------------------------------------------------

resource "aws_dynamodb_table" "runs" {
  name         = "prog-strength-developer-runs"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "sow"

  attribute {
    name = "sow"
    type = "S"
  }

  # TTL is cleanup only. Lock *correctness* (reclaiming a stale lock) is
  # enforced by the `expires_at <= now` term in the acquire condition,
  # because DynamoDB applies TTL deletion only on a best-effort, delayed
  # basis and must never be relied on to free a SOW promptly.
  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}
