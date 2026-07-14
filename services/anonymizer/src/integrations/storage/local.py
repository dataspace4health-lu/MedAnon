"""Local-filesystem result storage (default, single-replica)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import IO


class LocalResultStorage:
    """Stores job results on the local filesystem.

    The *result key* is the local file path unchanged, so the behaviour is
    100 % backwards-compatible with code that used ``job.result_path`` directly.
    """

    def write_from_path(self, job_id: str, local_path: str) -> str:
        """No-op: the local path IS the result key."""
        return local_path

    def open_stream(self, result_key: str) -> IO:
        """Open the NDJSON file for line-by-line reading (text mode)."""
        return open(result_key, encoding="utf-8")

    def iter_bytes(self, result_key: str, chunk_size: int = 65536) -> Iterator[bytes]:
        """Yield raw byte chunks  binary-safe (results may be .zip, not NDJSON)."""
        with open(result_key, "rb") as fh:
            while chunk := fh.read(chunk_size):
                yield chunk

    def get_download_url(self, result_key: str, expires: int = 3600) -> str | None:
        """Local storage has no presigned URLs  callers use FileResponse."""
        return None

    def exists(self, result_key: str) -> bool:
        return os.path.exists(result_key)

    def delete(self, result_key: str) -> bool:
        """Remove the NDJSON file. Returns True if it existed."""
        try:
            os.remove(result_key)
            return True
        except FileNotFoundError:
            return False
