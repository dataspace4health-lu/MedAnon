"""MedAnon domain types.

Canonical definitions of:
    ``Job``                  — async job dataclass
    ``JobStatus``            — job lifecycle enum
    ``JobStoreUnavailable``  — job store not initialized
    ``JobNotFound``          — job ID does not exist
    ``JobNotComplete``       — job is still running
    ``JobResultMissing``     — result file has been cleaned up
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum


class JobStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    ERROR = "error"


@dataclass
class Job:
    id: str
    type: str
    params: dict
    status: JobStatus = JobStatus.PENDING
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    result_path: str | None = None
    error: str | None = None
    checkpoint_data: dict | None = None


class JobStoreUnavailable(Exception):
    """Raised when the job store singleton is not initialized."""


class JobNotFound(Exception):
    """Raised when a job ID does not exist."""


class JobNotComplete(Exception):
    """Raised when attempting to access results of an incomplete job."""

    def __init__(self, status: str):
        super().__init__(f"Job is not complete (status={status})")
        self.status = status


class JobResultMissing(Exception):
    """Raised when the result file has been cleaned up."""
