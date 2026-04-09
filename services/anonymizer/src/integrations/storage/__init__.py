"""Result storage — local filesystem or S3-compatible object store (MinIO).

Backends:
    ``local`` (default) — writes NDJSON to ``/output/{job_id}.ndjson`` on disk.
    ``s3``               — uploads to MinIO/S3; result key is ``s3://bucket/object``.

Usage::

    from integrations.storage import get_result_storage

    storage = get_result_storage()
    key   = storage.write_from_path(job_id, local_path)   # returns result key
    url   = storage.get_download_url(key)                  # presigned URL or None
    valid = storage.exists(key)
    with storage.open_stream(key) as stream:
        for line in stream: ...

Selected by ``MEDANON_RESULT_STORAGE`` env var (``local`` / ``s3``).
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.storage")

_storage = None


def get_result_storage():
    """Return the configured result storage backend (lazy-init on first call)."""
    global _storage
    if _storage is None:
        _storage = _init_from_env()
    return _storage


def configure_result_storage(storage) -> None:
    """Override the storage backend — used for testing or explicit startup init."""
    global _storage
    _storage = storage


def _init_from_env():
    mode = os.environ.get("MEDANON_RESULT_STORAGE", "local").lower().strip()
    if mode == "s3":
        from integrations.storage.s3 import S3ResultStorage
        endpoint = os.environ.get("MINIO_ENDPOINT", "minio:9000")
        access_key = os.environ.get("MINIO_ROOT_USER", "minioadmin")
        secret_key = os.environ.get("MINIO_ROOT_PASSWORD", "minioadmin")
        bucket = os.environ.get("MINIO_BUCKET", "medanon-results")
        secure = os.environ.get("MINIO_SECURE", "false").lower() == "true"
        storage = S3ResultStorage(endpoint, access_key, secret_key, bucket, secure=secure)
        _log.info("result_storage=s3 endpoint=%s bucket=%s", endpoint, bucket)
        return storage
    from integrations.storage.local import LocalResultStorage
    _log.info("result_storage=local")
    return LocalResultStorage()


def store_result(job_id: str, local_path: str) -> str:
    """Upload *local_path* to the configured result storage and return the result key.

    For local storage (default) the key is the file path unchanged.
    For S3 (``MEDANON_RESULT_STORAGE=s3``) the file is uploaded to MinIO and the
    key is ``s3://<bucket>/<job_id>.ndjson``.
    """
    return get_result_storage().write_from_path(job_id, local_path)
