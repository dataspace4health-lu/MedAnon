"""Result storage  local filesystem or S3-compatible object store (MinIO).

Backends:
    ``local`` (default)  writes NDJSON to ``/output/{job_id}.ndjson`` on disk.
    ``s3``                uploads to MinIO/S3; result key is ``s3://bucket/object``.

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
    """Override the storage backend  used for testing or explicit startup init."""
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
        storage = S3ResultStorage(
            endpoint, access_key, secret_key, bucket, secure=secure
        )
        _log.info("result_storage=s3 endpoint=%s bucket=%s", endpoint, bucket)
        return storage
    from integrations.storage.local import LocalResultStorage

    _log.info("result_storage=local")
    return LocalResultStorage()


def delete_result(result_key: str) -> bool:
    """Delete a stored result by key (local path or s3:// key).

    Returns True if the delete succeeded, False on error (best-effort).
    """
    try:
        return get_result_storage().delete(result_key)
    except Exception as exc:
        _log.warning("delete_result_failed key=%s: %s", result_key, exc)
        return False


def store_result(job_id: str, local_path: str) -> str:
    """Upload *local_path* to the configured result storage and return the result key.

    For local storage (default) the key is the file path unchanged.
    For S3 (``MEDANON_RESULT_STORAGE=s3``) the file is uploaded to MinIO and the
    key is ``s3://<bucket>/<job_id>.ndjson``.
    """
    return get_result_storage().write_from_path(job_id, local_path)


def _compress_manifest() -> bool:
    return os.environ.get("MEDANON_COMPRESS_MANIFEST", "true").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _maybe_gzip(path: str) -> str:
    """Gzip *path* to a sibling ``.gz`` and return it; return *path* unchanged on opt-out.

    The manifest sidecar is one line per released resource and is almost pure
    repetition: on a 131k-resource export it measured 104.6 MB raw with only 195
    distinct (rule, action, path) triples, and gzip -6 compressed it ~65x. That
    turns the largest delivered artifact into the smallest at no semantic cost
    it stays newline-delimited JSON, just compressed.

    Streamed through ``copyfileobj`` so a multi-hundred-MB sidecar never lands in
    memory. Best-effort: on any failure the raw file is delivered.

    The output is byte-reproducible for a given input: ``mtime=0`` suppresses the
    timestamp and ``filename=""`` suppresses the gzip FNAME header field, which
    would otherwise embed the job-id-bearing source name and make two identical
    manifests hash differently. An audit artifact should hash the same for the
    same content.
    """
    if not _compress_manifest() or path.endswith(".gz"):
        return path

    import gzip
    import shutil

    gz_path = f"{path}.gz"
    try:
        with (
            open(path, "rb") as src,
            open(gz_path, "wb") as raw,
            gzip.GzipFile(
                filename="", mode="wb", fileobj=raw, compresslevel=6, mtime=0
            ) as dst,
        ):
            shutil.copyfileobj(src, dst, length=1 << 20)
    except Exception:
        _log.warning(
            "manifest_gzip_failed path=%s  delivering raw", path, exc_info=True
        )
        try:
            os.unlink(gz_path)
        except OSError:
            pass
        return path

    raw, comp = os.path.getsize(path), os.path.getsize(gz_path)
    _log.info(
        "manifest_compressed raw_bytes=%d gz_bytes=%d ratio=%.1fx",
        raw,
        comp,
        (raw / comp) if comp else 1.0,
    )
    return gz_path
