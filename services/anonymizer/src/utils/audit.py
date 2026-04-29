"""Centralized audit logging.

Audit events are:
1. Always logged as structured JSON to the ``medanon.audit`` logger (stdout
   in containers, captured by Docker/K8s log aggregators).
2. Optionally appended to a Redis Stream (``medanon:audit``) when
   ``MEDANON_REDIS_URL`` is configured, providing a centralized,
   append-only, queryable event store.
3. Optionally written to a local rotating file (backward compat) when
   ``MEDANON_AUDIT_LOG_FILE`` is set.
4. Optionally written to MinIO/S3 object storage when
   ``MEDANON_S3_AUDIT_BUCKET`` is set.  Events are batched in memory and
   flushed as a daily NDJSON object so the bucket can be configured with
   object-lock (WORM) to satisfy HIPAA §164.312(b) audit integrity.

MinIO/S3 audit configuration:
  MEDANON_S3_AUDIT_BUCKET   — bucket name (enables S3 sink)
  MEDANON_S3_ENDPOINT       — e.g. http://minio:9000 (default: AWS)
  MEDANON_S3_ACCESS_KEY     — access key / AWS_ACCESS_KEY_ID
  MEDANON_S3_SECRET_KEY     — secret key / AWS_SECRET_ACCESS_KEY
  MEDANON_S3_SECURE         — "true"/"false" (TLS, default false for internal)
  MEDANON_AUDIT_FLUSH_EVERY — flush after this many events (default: 100)
"""

from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timezone
from typing import Any

from utils.json_fast import dumps as _json_dumps

_log = logging.getLogger("medanon.audit")
_REDIS_STREAM = "medanon:audit"
_STREAM_MAXLEN = int(os.environ.get("MEDANON_AUDIT_STREAM_MAXLEN", "50000"))
_redis_client = None
_redis_checked = False
_redis_audit_warned = False  # one-shot rate limit for Redis xadd failures

# ---------------------------------------------------------------------------
# MinIO/S3 audit sink
# ---------------------------------------------------------------------------
_S3_BUCKET = os.environ.get("MEDANON_S3_AUDIT_BUCKET", "")
_S3_FLUSH_EVERY = int(os.environ.get("MEDANON_AUDIT_FLUSH_EVERY", "100"))
_s3_client = None
_s3_checked = False
_s3_buffer: list[str] = []
_s3_lock = threading.Lock()


def _get_s3():
    """Lazy-init MinIO client; returns None if minio package or config is missing."""
    global _s3_client, _s3_checked
    if _s3_checked:
        return _s3_client
    _s3_checked = True
    if not _S3_BUCKET:
        return None
    try:
        from minio import Minio

        endpoint = os.environ.get("MEDANON_S3_ENDPOINT", "s3.amazonaws.com")
        # Strip scheme — minio client takes host:port only
        endpoint = endpoint.removeprefix("https://").removeprefix("http://")
        secure = os.environ.get("MEDANON_S3_SECURE", "false").lower() in ("1", "true", "yes")
        access_key = os.environ.get("MEDANON_S3_ACCESS_KEY", "")
        secret_key = os.environ.get("MEDANON_S3_SECRET_KEY", "")
        _s3_client = Minio(
            endpoint,
            access_key=access_key or None,
            secret_key=secret_key or None,
            secure=secure,
        )
        # Ensure bucket exists (best-effort; object-lock must be set at bucket creation)
        if not _s3_client.bucket_exists(_S3_BUCKET):
            _s3_client.make_bucket(_S3_BUCKET)
        _log.info("audit_s3_connected bucket=%s", _S3_BUCKET)
    except Exception as exc:
        _log.warning("audit_s3_unavailable: %s — audit will not write to S3", exc)
        _s3_client = None
    return _s3_client


def _flush_s3_buffer(buffer: list[str]) -> None:
    """Upload *buffer* as a dated NDJSON object to S3. Called without _s3_lock held."""
    s3 = _s3_client
    if s3 is None or not buffer:
        return
    try:
        import io

        day = datetime.now(timezone.utc).strftime("%Y/%m/%d")
        ts = datetime.now(timezone.utc).strftime("%H%M%S%f")
        key = f"audit/{day}/events_{ts}.ndjson"
        payload = "\n".join(buffer).encode()
        s3.put_object(
            _S3_BUCKET,
            key,
            io.BytesIO(payload),
            length=len(payload),
            content_type="application/x-ndjson",
        )
    except Exception as exc:
        _log.warning("audit_s3_flush_error: %s", exc)


def _append_to_s3(line: str) -> None:
    """Buffer *line* and flush to S3 when batch is full."""
    flush_buf = None
    with _s3_lock:
        _s3_buffer.append(line)
        if len(_s3_buffer) >= _S3_FLUSH_EVERY:
            flush_buf = _s3_buffer.copy()
            _s3_buffer.clear()
    if flush_buf:
        _flush_s3_buffer(flush_buf)


def _get_redis():
    """Lazy init -- returns a Redis client or None."""
    global _redis_client, _redis_checked
    if _redis_checked:
        return _redis_client
    _redis_checked = True
    url = os.environ.get("MEDANON_REDIS_URL")
    if not url:
        return None
    try:
        import redis as _redis_mod

        _redis_client = _redis_mod.Redis.from_url(
            url, socket_timeout=2, decode_responses=True
        )
        _redis_client.ping()
        _log.info(
            "audit_redis_connected stream=%s maxlen=%d", _REDIS_STREAM, _STREAM_MAXLEN
        )
    except Exception as exc:
        _log.warning(
            "audit_redis_unavailable: %s -- audit events will only go to stdout/file",
            exc,
        )
        _redis_client = None
    return _redis_client


def emit(
    event_type: str,
    *,
    actor: str = "system",
    resource_type: str = "",
    resource_id: str = "",
    action: str = "",
    outcome: str = "success",
    detail: dict[str, Any] | None = None,
    request_id: str = "",
) -> None:
    """Emit a single audit event.

    Parameters
    ----------
    event_type : str
        Category -- e.g. ``process``, ``job.create``, ``job.complete``,
        ``config.change``, ``auth.login``, ``auth.deny``.
    actor : str
        Principal (API key name, "system", or IP).
    outcome : str
        ``success``, ``failure``, ``error``.
    """
    ts = datetime.now(timezone.utc).isoformat()
    entry: dict[str, Any] = {
        "ts": ts,
        "event": event_type,
        "actor": actor,
        "outcome": outcome,
    }
    if resource_type:
        entry["resource_type"] = resource_type
    if resource_id:
        entry["resource_id"] = resource_id
    if action:
        entry["action"] = action
    if detail:
        entry["detail"] = detail
    if request_id:
        entry["request_id"] = request_id

    # 1. Structured log line (always -- captured by Docker/K8s log driver)
    line = _json_dumps(entry)
    _log.info(line)

    # 2. Redis Stream (append-only, capped)
    r = _get_redis()
    if r is not None:
        try:
            r.xadd(
                _REDIS_STREAM,
                entry if not detail else {**entry, "detail": _json_dumps(detail)},
                maxlen=_STREAM_MAXLEN,
                approximate=True,
            )
        except Exception as exc:
            # Best-effort: the structured stdout log above is the authoritative
            # record, but a silent failure hides Redis auth/network problems.
            # Log at WARNING and rate-limit by only logging the first failure
            # per process to avoid log spam when Redis is permanently down.
            global _redis_audit_warned
            if not _redis_audit_warned:
                _log.warning("audit_redis_xadd_failed: %s", exc)
                _redis_audit_warned = True

    # 3. MinIO/S3 object storage (WORM / object-lock bucket for HIPAA audit durability)
    if _S3_BUCKET and _get_s3() is not None:
        _append_to_s3(line)


def query(
    count: int = 100,
    event_type: str | None = None,
    since: str | None = None,
) -> list[dict]:
    """Query recent audit events from the Redis Stream.

    Returns newest-first. When Redis is unavailable, returns [].
    """
    r = _get_redis()
    if r is None:
        return []
    try:
        raw = r.xrevrange(_REDIS_STREAM, count=count * 2 if event_type else count)
        results = []
        for msg_id, fields in raw:
            if event_type and fields.get("event") != event_type:
                continue
            fields["_id"] = msg_id
            if "detail" in fields and isinstance(fields["detail"], str):
                try:
                    from utils.json_fast import loads as _json_loads

                    fields["detail"] = _json_loads(fields["detail"])
                except (ValueError, TypeError):
                    pass
            results.append(fields)
            if len(results) >= count:
                break
        return results
    except Exception:
        return []
