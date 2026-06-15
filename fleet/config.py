"""Environment-driven configuration for the fleet control plane.

Pure stdlib — no boto3 import here, so the CLI/tests can import config
without pulling AWS in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

#: DynamoDB table holding one lock row per SOW.
DEFAULT_TABLE_NAME = "prog-strength-developer-runs"

#: us-east-2 is where the developer platform lives.
DEFAULT_REGION = "us-east-2"

#: Lock lifetime. Workers self-terminate at a 6h max runtime; a 1h
#: buffer past that makes a lock reclaimable only once the worker is
#: unambiguously gone. Enforced in the acquire condition, not via TTL
#: deletion (which DynamoDB applies only best-effort).
DEFAULT_TTL_SECONDS = 7 * 3600


@dataclass(frozen=True)
class Config:
    table_name: str = DEFAULT_TABLE_NAME
    region: str = DEFAULT_REGION
    ttl_seconds: int = DEFAULT_TTL_SECONDS

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            table_name=os.environ.get("FLEET_TABLE_NAME", DEFAULT_TABLE_NAME),
            region=os.environ.get("AWS_REGION", DEFAULT_REGION),
            ttl_seconds=int(os.environ.get("FLEET_TTL_SECONDS", DEFAULT_TTL_SECONDS)),
        )
