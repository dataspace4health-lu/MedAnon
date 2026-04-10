"""S3-compatible object storage backend using the MinIO Python SDK.

Activated by setting ``MEDANON_RESULT_STORAGE=s3``.
Configuration env vars: ``MINIO_ENDPOINT``, ``MINIO_ROOT_USER``,
``MINIO_ROOT_PASSWORD``, ``MINIO_BUCKET``, ``MINIO_SECURE``.

Result keys are stored as ``s3://<bucket>/<job_id>.ndjson`` in ``job.result_path``.
When ``GET /v1/jobs/{id}/result`` is requested the API generates a presigned URL
and returns HTTP 307 — the client downloads directly from MinIO, bypassing
the anonymizer.
"""

from __future__ import annotations

import logging
import os
from datetime import timedelta
from typing import IO

_log = logging.getLogger("medanon.storage.s3")

_S3_PREFIX = "s3://"


class _S3TextStream:
    """Line-iterating text wrapper around a MinIO HTTPResponse.

    MinIO's ``get_object`` returns a ``urllib3.response.HTTPResponse`` which
    yields raw bytes chunks, not newline-delimited lines.  This wrapper buffers
    chunks and yields complete UTF-8 lines suitable for JSON parsing.
    """

    def __init__(self, response) -> None:
        self._response = response
        self._buf = ""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def __iter__(self):
        return self

    def __next__(self) -> str:
        while "\n" not in self._buf:
            chunk = self._response.read(65536)
            if not chunk:
                if self._buf:
                    line, self._buf = self._buf, ""
                    return line
                raise StopIteration
            self._buf += chunk.decode("utf-8", errors="replace")
        idx = self._buf.index("\n") + 1
        line, self._buf = self._buf[:idx], self._buf[idx:]
        return line

    def close(self) -> None:
        try:
            self._response.close()
        except Exception:
            pass
        try:
            self._response.release_conn()
        except Exception:
            pass


class S3ResultStorage:
    """Stores job NDJSON results in an S3-compatible object store (MinIO)."""

    def __init__(
        self,
        endpoint: str,
        access_key: str,
        secret_key: str,
        bucket: str,
        *,
        secure: bool = False,
    ) -> None:
        from minio import Minio

        self._client = Minio(
            endpoint,
            access_key=access_key,
            secret_key=secret_key,
            secure=secure,
        )
        self._bucket = bucket
        self._ensure_bucket()

    def _ensure_bucket(self) -> None:
        try:
            if not self._client.bucket_exists(self._bucket):
                self._client.make_bucket(self._bucket)
                _log.info("s3_bucket_created bucket=%s", self._bucket)
        except Exception as exc:
            _log.warning("s3_bucket_init_error bucket=%s: %s", self._bucket, exc)

    def _object_name(self, result_key: str) -> str:
        """Extract the object name from a result key.

        Handles both ``s3://bucket/object`` URIs and bare ``job_id`` strings.
        """
        if result_key.startswith(_S3_PREFIX):
            # s3://bucket/object_name → object_name
            return result_key[len(_S3_PREFIX) + len(self._bucket) + 1 :]
        return f"{result_key}.ndjson"

    def write_from_path(self, job_id: str, local_path: str) -> str:
        """Upload the local NDJSON file to MinIO and return the S3 result key."""
        object_name = f"{job_id}.ndjson"
        file_size = os.path.getsize(local_path)
        with open(local_path, "rb") as data:
            self._client.put_object(
                self._bucket,
                object_name,
                data,
                file_size,
                content_type="application/x-ndjson",
            )
        key = f"{_S3_PREFIX}{self._bucket}/{object_name}"
        _log.info("s3_upload_done job=%s size_bytes=%d key=%s", job_id, file_size, key)
        return key

    def open_stream(self, result_key: str) -> IO:
        """Return a line-iterable text stream for the stored NDJSON object."""
        obj_name = self._object_name(result_key)
        response = self._client.get_object(self._bucket, obj_name)
        return _S3TextStream(response)

    def get_download_url(self, result_key: str, expires: int = 3600) -> str | None:
        """Return a presigned GET URL valid for *expires* seconds."""
        obj_name = self._object_name(result_key)
        return self._client.presigned_get_object(
            self._bucket,
            obj_name,
            expires=timedelta(seconds=expires),
        )

    def exists(self, result_key: str) -> bool:
        """Return True if the object exists in MinIO."""
        try:
            obj_name = self._object_name(result_key)
            self._client.stat_object(self._bucket, obj_name)
            return True
        except Exception:
            return False

    def delete(self, result_key: str) -> bool:
        """Remove the object from MinIO. Returns True if it existed."""
        obj_name = self._object_name(result_key)
        try:
            self._client.stat_object(self._bucket, obj_name)
        except Exception:
            return False
        try:
            self._client.remove_object(self._bucket, obj_name)
            _log.info("s3_delete_done key=%s", result_key)
            return True
        except Exception as exc:
            _log.warning("s3_delete_failed key=%s: %s", result_key, exc)
            return False
