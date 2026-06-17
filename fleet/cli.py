"""Thin CLI over the run registry — the interface the dispatch workflow
and the worker call.

Exit codes are part of the contract:

* ``0`` — success.
* ``1`` — error (bad usage, attaching to a lock you don't own, etc.).
* ``3`` — acquire refused because the SOW is already in progress. A
  distinct code so the workflow can tell "already running" apart from a
  real failure.

``run()`` takes the registry and ``now`` as arguments so it is fully
testable; ``main()`` wires the production DynamoDB registry and the wall
clock.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid

from fleet import service
from fleet.config import DEFAULT_TTL_SECONDS, Config
from fleet.models import RunStatus
from fleet.registry import FleetError, RunRegistry

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_CONFLICT = 3


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="fleet", description="prog-strength developer fleet control")
    sub = p.add_subparsers(dest="command", required=True)

    acq = sub.add_parser("acquire", help="claim a SOW before dispatching a worker")
    acq.add_argument("--sow", required=True)
    acq.add_argument("--dispatch-id", default=None, help="defaults to a fresh UUID")
    acq.add_argument("--dispatched-by", default=None)
    acq.add_argument("--ttl", type=int, default=DEFAULT_TTL_SECONDS)
    acq.add_argument("--json", action="store_true")

    att = sub.add_parser("attach", help="record the launched instance on a held lock")
    att.add_argument("--sow", required=True)
    att.add_argument("--dispatch-id", required=True)
    att.add_argument("--instance-id", required=True)

    rel = sub.add_parser("release", help="free a SOW when a run finishes")
    rel.add_argument("--sow", required=True)
    rel.add_argument("--instance-id", required=True)
    rel.add_argument("--outcome", default="success", choices=["success", "error", "timeout"])
    rel.add_argument("--force", action="store_true", help="operator override; ignore instance match")
    rel.add_argument(
        "--prs-opened", type=int, default=0, help="PRs this run opened; recorded in run history"
    )

    lst = sub.add_parser("list", help="show SOWs currently being built")
    lst.add_argument("--json", action="store_true")

    return p


def run(argv: list[str], *, registry: RunRegistry, now: int) -> int:
    args = _build_parser().parse_args(argv)

    if args.command == "acquire":
        dispatch_id = args.dispatch_id or uuid.uuid4().hex
        result = registry.try_acquire(
            args.sow,
            dispatch_id=dispatch_id,
            now=now,
            ttl_seconds=args.ttl,
            dispatched_by=args.dispatched_by,
        )
        if result.acquired:
            if args.json:
                print(json.dumps({"acquired": True, "sow": args.sow, "dispatch_id": dispatch_id}))
            else:
                print(f"Acquired {args.sow!r} (dispatch_id={dispatch_id}).")
            return EXIT_OK
        holder = result.conflict
        if args.json:
            print(
                json.dumps(
                    {
                        "acquired": False,
                        "sow": args.sow,
                        "holder_instance_id": holder.instance_id if holder else None,
                        "holder_started_at": holder.started_at if holder else None,
                    }
                )
            )
        else:
            print(service.conflict_message(holder))
        return EXIT_CONFLICT

    if args.command == "attach":
        try:
            registry.attach_instance(
                args.sow, dispatch_id=args.dispatch_id, instance_id=args.instance_id, now=now
            )
        except FleetError as exc:
            print(f"attach failed: {exc}", file=sys.stderr)
            return EXIT_ERROR
        print(f"Attached {args.instance_id} to {args.sow!r}.")
        return EXIT_OK

    if args.command == "release":
        released = registry.release(
            args.sow,
            instance_id=args.instance_id,
            outcome=args.outcome,
            now=now,
            force=args.force,
            prs_opened=args.prs_opened,
        )
        if released:
            print(f"Released {args.sow!r} (outcome={args.outcome}).")
        else:
            # Best-effort: a superseded worker (or a missing lock) must not
            # fail the caller. The lock's expiry is the backstop.
            print(f"Skipped release of {args.sow!r}: not held by {args.instance_id}.")
        return EXIT_OK

    if args.command == "list":
        active = registry.list_active(now=now)
        if args.json:
            print(
                json.dumps(
                    [
                        {
                            "sow": r.sow,
                            "instance_id": r.instance_id,
                            "started_at": r.started_at,
                            "expires_at": r.expires_at,
                            "status": RunStatus(r.status).value,
                        }
                        for r in sorted(active, key=lambda r: r.started_at)
                    ]
                )
            )
        else:
            print(service.active_table(active))
        return EXIT_OK

    return EXIT_ERROR  # unreachable: argparse enforces a known command


def main(argv: list[str] | None = None) -> int:
    from fleet.dynamo import DynamoRunRegistry

    cfg = Config.from_env()
    registry = DynamoRunRegistry(table_name=cfg.table_name, region=cfg.region)
    return run(list(sys.argv[1:] if argv is None else argv), registry=registry, now=int(time.time()))


if __name__ == "__main__":
    raise SystemExit(main())
