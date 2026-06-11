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

import atexit
import logging
import os
import queue
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
        secure = os.environ.get("MEDANON_S3_SECURE", "false").lower() in (
            "1",
            "true",
            "yes",
        )
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


# ---------------------------------------------------------------------------
# Async sink dispatch (F.18) — keep Redis/S3 writes off the request hot path
# ---------------------------------------------------------------------------
# When MEDANON_AUDIT_ASYNC=true, emit() drops the (entry, line) tuple onto a
# bounded queue drained by a single daemon thread. The authoritative stdout
# JSON line is ALWAYS written synchronously in emit() first, so a full queue
# degrades durability (Redis/S3 lag) but never loses the primary record.
_async_enabled = os.environ.get("MEDANON_AUDIT_ASYNC", "false").strip().lower() in (
    "true",
    "1",
    "yes",
)
_ASYNC_QUEUE_MAX = int(os.environ.get("MEDANON_AUDIT_QUEUE_MAX", "10000"))
_async_queue: "queue.Queue[tuple[dict, str] | None] | None" = None
_async_thread: threading.Thread | None = None
_async_lock = threading.Lock()
_SENTINEL = None


def _ensure_async_worker() -> "queue.Queue | None":
    """Start the drain thread on first use; returns the queue (or None if off)."""
    global _async_queue, _async_thread
    if not _async_enabled:
        return None
    if _async_queue is not None:
        return _async_queue
    with _async_lock:
        if _async_queue is None:
            _async_queue = queue.Queue(maxsize=_ASYNC_QUEUE_MAX)
            _async_thread = threading.Thread(
                target=_async_drain_loop,
                name="audit-async-writer",
                daemon=True,
            )
            _async_thread.start()
            atexit.register(_flush_async_on_shutdown)
    return _async_queue


def _async_drain_loop() -> None:
    assert _async_queue is not None
    while True:
        item = _async_queue.get()
        try:
            if item is _SENTINEL:
                return
            entry, line = item
            _write_secondary_sinks(entry, line)
        except Exception as exc:  # never let the writer thread die
            _log.warning("audit_async_writer_error: %s", exc)
        finally:
            _async_queue.task_done()


def _flush_async_on_shutdown() -> None:
    """Drain queued events and stop the writer thread (atexit / shutdown hook)."""
    q = _async_queue
    if q is None:
        return
    try:
        q.put(_SENTINEL, timeout=2.0)
    except queue.Full:
        return
    if _async_thread is not None:
        _async_thread.join(timeout=5.0)


def _write_secondary_sinks(entry: dict, line: str) -> None:
    """Redis Stream + S3 writes (the slow sinks). Runs sync OR on the worker."""
    r = _get_redis()
    if r is not None:
        try:
            detail = entry.get("detail")
            r.xadd(
                _REDIS_STREAM,
                entry if not detail else {**entry, "detail": _json_dumps(detail)},
                maxlen=_STREAM_MAXLEN,
                approximate=True,
            )
        except Exception as exc:
            global _redis_audit_warned
            if not _redis_audit_warned:
                _log.warning("audit_redis_xadd_failed: %s", exc)
                _redis_audit_warned = True

    if _S3_BUCKET and _get_s3() is not None:
        _append_to_s3(line)


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
        # Reuse the shared connection pool (utils.redis_pool) so the audit
        # writer doesn't open its own pool of 50 sockets.
        from utils.redis_pool import get_redis as _shared_get_redis

        _redis_client = _shared_get_redis(url, decode_responses=True)
        if _redis_client is None:
            return None
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

    # 1. Structured log line (ALWAYS, synchronous -- this is the authoritative
    #    record, captured by the Docker/K8s log driver).
    line = _json_dumps(entry)
    _log.info(line)

    # 2 + 3. Redis Stream + S3 (the slow sinks). Run inline by default; when
    #    MEDANON_AUDIT_ASYNC=true, hand off to the background writer so the
    #    request hot path doesn't pay the network/IO cost.
    q = _ensure_async_worker()
    if q is None:
        _write_secondary_sinks(entry, line)
    else:
        try:
            q.put_nowait((entry, line))
        except queue.Full:
            # Queue saturated: count it and fall back to a synchronous write so
            # the event still reaches Redis/S3 (completeness over latency).
            try:
                from utils.metrics import AUDIT_DROPPED

                AUDIT_DROPPED.inc()
            except Exception:
                pass
            _write_secondary_sinks(entry, line)


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
