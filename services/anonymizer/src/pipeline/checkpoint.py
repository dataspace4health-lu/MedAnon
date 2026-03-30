"""Checkpoint helpers for retry-from-checkpoint in long-running jobs.

Usage inside a job executor:

    from pipeline.checkpoint import load_checkpoint, save_checkpoint, CHECKPOINT_INTERVAL

    checkpoint = load_checkpoint(job) or {}
    lines_written = checkpoint.get("lines_written", 0)

    for i, resource in enumerate(generator):
        if i < lines_written:
            continue  # already written in a previous run

        write(resource)

        if (i + 1) % CHECKPOINT_INTERVAL == 0:
            save_checkpoint(store, job, {"lines_written": i + 1})
"""
from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.checkpoint")

CHECKPOINT_INTERVAL: int = int(os.environ.get("MEDANON_CHECKPOINT_INTERVAL", "100"))


def save_checkpoint(store, job, data: dict) -> None:
    """Persist incremental checkpoint data for a running job.

    Updates ``job.checkpoint_data`` in memory and delegates persistence
    to ``store.update_checkpoint()`` when available, otherwise falls
    back to a full ``store.update()``.  Errors are logged and swallowed
    so that a checkpoint failure never aborts the job itself.
    """
    job.checkpoint_data = data
    try:
        if hasattr(store, "update_checkpoint"):
            store.update_checkpoint(job.id, data)
        else:
            store.update(job)
    except Exception as exc:
        _log.warning("checkpoint_save_failed job=%s: %s", job.id, type(exc).__name__)


def load_checkpoint(job) -> dict | None:
    """Return the persisted checkpoint dict, or None if no checkpoint exists."""
    return job.checkpoint_data
