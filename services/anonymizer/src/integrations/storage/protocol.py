"""ResultStoragePort — protocol for job result storage backends."""

from __future__ import annotations

from collections.abc import Iterator
from typing import IO, Protocol, runtime_checkable


@runtime_checkable
class ResultStoragePort(Protocol):
    """Minimal interface all storage backends must satisfy.

    ``write_from_path`` stores a local NDJSON file and returns a *result key*.
    For local storage the key is the file path; for S3 it is ``s3://bucket/obj``.
    ``open_stream`` returns a line-iterable *text* stream for that key.
    ``iter_bytes`` yields raw byte chunks — the seam the download endpoint uses.
    ``get_download_url`` returns a presigned URL (S3) or ``None`` (local).
    ``exists`` reports whether the result is still available.
    ``delete`` removes the result and returns True if it existed.
    """

    def write_from_path(self, job_id: str, local_path: str) -> str: ...
    def open_stream(self, result_key: str) -> IO: ...
    def iter_bytes(
        self, result_key: str, chunk_size: int = 65536
    ) -> Iterator[bytes]: ...
    def get_download_url(self, result_key: str, expires: int = 3600) -> str | None: ...
    def exists(self, result_key: str) -> bool: ...
    def delete(self, result_key: str) -> bool: ...
