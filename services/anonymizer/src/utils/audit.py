"""Centralized audit logging.

Audit events are:
1. Always logged as structured JSON to the ``medanon.audit`` logger (stdout
   in containers, captured by Docker/K8s log aggregators).
2. Optionally appended to a Redis Stream (``medanon:audit``) when
   ``MEDANON_REDIS_URL`` is configured, providing a centralized,
   append-only, queryable event store.
3. Optionally written to a local rotating file (backward compat) when
   ``MEDANON_AUDIT_LOG_FILE`` is set.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any

from utils.json_fast import dumps as _json_dumps

_log = logging.getLogger("medanon.audit")
_REDIS_STREAM = "medanon:audit"
_STREAM_MAXLEN = int(os.environ.get("MEDANON_AUDIT_STREAM_MAXLEN", "50000"))
_redis_client = None
_redis_checked = False


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
    _log.info(_json_dumps(entry))

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
        except Exception:
            pass  # Best-effort; stdout log is the authoritative record


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
