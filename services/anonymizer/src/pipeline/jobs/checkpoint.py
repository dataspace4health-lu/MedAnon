"""Checkpoint helpers for retry-from-checkpoint in long-running jobs.

Usage inside a job executor:

    from pipeline.jobs.checkpoint import load_checkpoint, save_checkpoint, CHECKPOINT_INTERVAL

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


def _truncate_to_lines(path: str, line_count: int) -> int:
    """Truncate *path* to at most *line_count* newline-terminated lines.

    Returns the number of complete lines retained (``<= line_count``).

    Two crash-resume cases are handled:

    1. **File has more lines than checkpoint recorded** (crash after write
       but before checkpoint update): truncate the surplus lines.
    2. **File ends mid-line** (crash mid-flush, no trailing ``\\n``): drop
       the partial trailing line so subsequent appends don't concatenate
       with it and corrupt the JSONL stream.

    The implementation walks the file in 64 KiB chunks counting ``\\n``
    bytes; when it has seen ``line_count`` newlines, it truncates at the
    byte immediately after the Nth newline.  A file with fewer than
    ``line_count`` newlines is truncated to its last newline boundary
    (any partial trailing line is discarded).
    """
    import os

    if line_count <= 0 or not os.path.exists(path):
        return 0

    _CHUNK = 64 * 1024
    seen = 0
    last_complete_pos = 0  # byte offset right after the most recent \n
    cut_pos: int | None = None
    with open(path, "r+b") as f:
        pos = 0
        while True:
            chunk = f.read(_CHUNK)
            if not chunk:
                break
            for byte in chunk:
                pos += 1
                if byte == 0x0A:  # '\n'
                    seen += 1
                    last_complete_pos = pos
                    if seen == line_count:
                        cut_pos = pos
                        break
            if cut_pos is not None:
                break
        # If we hit the requested count, truncate the remainder.
        # Otherwise truncate any partial trailing line (drop bytes after
        # the last observed '\n'); on a clean file this is a no-op.
        #
        # Compare against the real file size, not the scan position: the scan
        # stops at the Nth newline, so ``pos == cut_pos`` there and a
        # ``target < pos`` guard would never fire — surplus lines written after
        # the last checkpoint (crash case 1) would survive and duplicate on
        # resume.
        target = cut_pos if cut_pos is not None else last_complete_pos
        f.seek(0, 2)
        size = f.tell()
        if target < size:
            f.seek(target)
            f.truncate()
    return seen
