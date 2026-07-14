"""Stage message schema  versioned, metadata-only work pointers.

A message identifies a unit of work (a partition of a job at a given stage). It
deliberately carries NO patient data, NO FHIR resource bodies, and NO source
URLs that could embed identifiers  only the keys a consumer needs to claim the
work from the Postgres ledger and re-fetch/process it.

Security invariant (enforce in code review): every field here must be a
non-PHI identifier or small enum. Do not add resource content.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

# Ordered macro stages of the bulk pipeline. ``fetch`` runs in the submitting
# worker (it produces partitions); the rest are consumed off the broker.
STAGES = ("fetch", "deid", "score", "upload")

# Per-stage primary queue names are derived in topology.py; re-exported here for
# convenience so callers importing the package get both.
STAGE_QUEUES = {s: f"medanon.stage.{s}" for s in STAGES}

# Schema version  bump when the field set changes so consumers can branch.
SCHEMA_VERSION = 1


@dataclass
class StageMessage:
    """A single partition-of-work pointer for one workflow stage."""

    workflow_id: str
    job_id: str
    step_id: str
    stage: str
    partition_id: int
    config_hash: str = ""
    attempt: int = 1
    tenant: str = "default"
    trace_id: str = ""
    v: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.stage not in STAGES:
            raise ValueError(f"unknown stage {self.stage!r}; valid: {STAGES}")
        # Defensive: reject obviously-PHI-shaped fields sneaking in via subclass
        # misuse is impossible with a frozen field set, but keep ids as strings.
        self.partition_id = int(self.partition_id)
        self.attempt = int(self.attempt)

    @property
    def message_id(self) -> str:
        """Stable id for broker-side dedup hints (job:stage:partition)."""
        return f"{self.job_id}:{self.stage}:{self.partition_id}"

    def to_bytes(self) -> bytes:
        return json.dumps(asdict(self), separators=(",", ":")).encode()

    @classmethod
    def from_bytes(cls, raw: bytes) -> "StageMessage":
        data = json.loads(raw)
        # Tolerate unknown future fields by filtering to known ones.
        return cls(**{k: v for k, v in data.items() if k in cls.__dataclass_fields__})
