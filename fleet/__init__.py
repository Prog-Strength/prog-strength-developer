"""fleet — the prog-strength-developer control plane.

This package is the extensible home for management/worker logic that
must not live as inline bash in the dispatch workflow. Its first
capability is the *run registry*: a record of which SOW each worker is
building, used to guarantee at most one active worker per SOW.

Layering mirrors the Go API's domains so the logic is unit-testable
without AWS:

- ``models``   — RunRecord / RunStatus / AcquireResult value types.
- ``registry`` — the RunRegistry interface + errors.
- ``memory``   — an in-memory implementation (tests + local use).
- ``dynamo``   — the DynamoDB implementation (production).
- ``service``  — orchestration (dispatch gating) over a registry.
- ``config``   — env-driven configuration.
- ``cli``      — a thin CLI adapter the workflow and worker call.
"""
