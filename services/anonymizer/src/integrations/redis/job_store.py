"""Redis-backed job store — Redis Streams for guaranteed delivery.

Uses Redis hashes for per-job storage, Redis Streams (XADD/XREADGROUP/XACK)
for the worker notification queue, and sorted sets for time-ordered listing.

Streams improvements over BLPOP:
- Consumer groups guarantee at-least-once delivery: if a worker crashes after
  popping a message but before ACKing it, the message stays in the Pending
  Entry List (PEL) and is reclaimed by ``claim_stale_jobs()``.
- ``XAUTOCLAIM`` replaces the fragile ``_recover_running_jobs()`` scan.
- ``get_queue_depth()`` exposes pending message count for monitoring.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from medanon_core.domain import Job, JobStatus

_log = logging.getLogger("medanon.jobs.redis")

_KEY_PREFIX = "medanon:job:"
_STREAM_KEY = "medanon:job_stream"
_STREAM_GROUP = "workers"
_INDEX_KEY = "medanon:jobs_by_time"
_STATUS_PREFIX = "medanon:jobs:status:"
_TYPE_PREFIX = "medanon:jobs:type:"
_DEFAULT_TTL = 604800  # 7 days


class RedisJobStore:
    """Implements JobStorePort using Redis hashes + Redis Streams job queue."""

    def __init__(self, redis_url: str, ttl: int = _DEFAULT_TTL) -> None:
        import redis as _redis

        self._client = _redis.StrictRedis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl
        self._consumer_id = f"worker-{uuid.uuid4().hex[:8]}"
        # Verify connectivity
        self._client.ping()
        # Create the consumer group (MKSTREAM also creates the stream if missing)
        self._ensure_stream_group()
        _log.info(
            "redis_job_store_connected url=%s consumer=%s",
            redis_url.split("@")[-1],
            self._consumer_id,
        )

    def _ensure_stream_group(self) -> None:
        """Idempotent consumer-group creation.  BUSYGROUP = already exists, ignore."""
        try:
            self._client.xgroup_create(
                _STREAM_KEY, _STREAM_GROUP, id="$", mkstream=True
            )
        except Exception as exc:
            if "BUSYGROUP" not in str(exc):
                _log.warning("stream_group_init_error: %s", exc)

    def _job_key(self, job_id: str) -> str:
        return f"{_KEY_PREFIX}{job_id}"

    def create(self, job_type: str, params: dict) -> Job:
        """Persist a new PENDING job and return it."""
        now = datetime.now(timezone.utc).isoformat()
        job = Job(
            id=str(uuid.uuid4()),
            type=job_type,
            params=params,
            status=JobStatus.PENDING,
            created_at=now,
            updated_at=now,
        )
        ts = datetime.fromisoformat(now).timestamp()
        pipe = self._client.pipeline()
        pipe.hset(
            self._job_key(job.id),
            mapping={
                "id": job.id,
                "type": job.type,
                "params": json.dumps(job.params),
                "status": job.status.value,
                "created_at": job.created_at,
                "updated_at": job.updated_at,
                "result_path": "",
                "error": "",
                "checkpoint_data": "",
            },
        )
        pipe.expire(self._job_key(job.id), self._ttl)
        pipe.zadd(_INDEX_KEY, {job.id: ts})
        pipe.sadd(f"{_STATUS_PREFIX}{job.status.value}", job.id)
        pipe.sadd(f"{_TYPE_PREFIX}{job.type}", job.id)
        pipe.execute()
        _log.info("job_created id=%s type=%s backend=redis", job.id, job.type)
        return job

    def get(self, job_id: str) -> Job | None:
        """Fetch a job by ID; returns None if not found."""
        data = self._client.hgetall(self._job_key(job_id))
        if not data:
            return None
        return self._hash_to_job(data)

    def update(self, job: Job) -> None:
        """Persist status, result_path, error, and checkpoint_data changes for *job*."""
        job.updated_at = datetime.now(timezone.utc).isoformat()
        key = self._job_key(job.id)
        old_status = self._client.hget(key, "status")
        pipe = self._client.pipeline()
        pipe.hset(
            key,
            mapping={
                "status": job.status.value,
                "updated_at": job.updated_at,
                "result_path": job.result_path or "",
                "error": job.error or "",
                "checkpoint_data": json.dumps(job.checkpoint_data) if job.checkpoint_data else "",
            },
        )
        pipe.expire(key, self._ttl)
        if old_status and old_status != job.status.value:
            pipe.srem(f"{_STATUS_PREFIX}{old_status}", job.id)
            pipe.sadd(f"{_STATUS_PREFIX}{job.status.value}", job.id)
        pipe.execute()

    def next_pending(self) -> Job | None:
        """Return the oldest PENDING job (polling fallback for non-Streams callers)."""
        pending_ids = self._client.smembers(f"{_STATUS_PREFIX}pending")
        if not pending_ids:
            return None
        # Batch ZSCORE via pipeline to avoid N round-trips
        pipe = self._client.pipeline(transaction=False)
        id_list = list(pending_ids)
        for jid in id_list:
            pipe.zscore(_INDEX_KEY, jid)
        scores_raw = pipe.execute()
        scores = {}
        for jid, score in zip(id_list, scores_raw):
            if score is not None:
                scores[jid] = score
        if not scores:
            return None
        oldest_id = min(scores, key=scores.get)
        return self.get(oldest_id)

    def list_jobs(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """List jobs with optional filtering, ordered by created_at descending."""
        if status and job_type:
            candidates = self._client.sinter(
                f"{_STATUS_PREFIX}{status}",
                f"{_TYPE_PREFIX}{job_type}",
            )
        elif status:
            candidates = self._client.smembers(f"{_STATUS_PREFIX}{status}")
        elif job_type:
            candidates = self._client.smembers(f"{_TYPE_PREFIX}{job_type}")
        else:
            all_ids = self._client.zrevrange(_INDEX_KEY, offset, offset + limit - 1)
            return self._batch_get_jobs(all_ids)

        if not candidates:
            return []
        # Batch ZSCORE via pipeline
        id_list = list(candidates)
        pipe = self._client.pipeline(transaction=False)
        for jid in id_list:
            pipe.zscore(_INDEX_KEY, jid)
        scores_raw = pipe.execute()
        scored = []
        for jid, score in zip(id_list, scores_raw):
            if score is not None:
                scored.append((jid, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        page = scored[offset : offset + limit]
        return self._batch_get_jobs([jid for jid, _ in page])

    def _batch_get_jobs(self, job_ids: list[str]) -> list[Job]:
        """Fetch multiple jobs in a single pipeline round-trip."""
        if not job_ids:
            return []
        pipe = self._client.pipeline(transaction=False)
        for jid in job_ids:
            pipe.hgetall(self._job_key(jid))
        results = pipe.execute()
        jobs = []
        for data in results:
            if data:
                try:
                    jobs.append(self._hash_to_job(data))
                except (KeyError, ValueError):
                    pass  # stale/corrupt hash — skip
        return jobs

    # -------------------------------------------------------------------------
    # Stream-based notification (replaces RPUSH / BLPOP)
    # -------------------------------------------------------------------------

    def notify_new_job(self, job_id: str) -> None:
        """Publish a job notification to the Redis Stream."""
        self._client.xadd(_STREAM_KEY, {"job_id": job_id})

    def wait_for_job(self, timeout: int = 5) -> tuple[str, str] | None:
        """Block until a job message appears on the Stream.

        Returns ``(job_id, message_id)`` or ``None`` on timeout.

        Uses XREADGROUP for at-least-once delivery: messages remain in the
        Pending Entry List until ``ack_job()`` is called.
        """
        try:
            results = self._client.xreadgroup(
                _STREAM_GROUP,
                self._consumer_id,
                {_STREAM_KEY: ">"},
                count=1,
                block=timeout * 1000,
            )
        except Exception as exc:
            _log.warning("xreadgroup_error: %s — retrying", type(exc).__name__)
            return None

        if not results:
            return None
        _, messages = results[0]
        if not messages:
            return None
        message_id, fields = messages[0]
        job_id = fields.get("job_id", "")
        if not job_id:
            # Malformed message — ack it to unblock the queue
            try:
                self._client.xack(_STREAM_KEY, _STREAM_GROUP, message_id)
            except Exception:
                pass
            return None
        return job_id, message_id

    def ack_job(self, message_id: str) -> None:
        """Acknowledge a stream message after a job reaches a terminal state.

        Removes the message from the Pending Entry List so it is never redelivered.
        """
        try:
            self._client.xack(_STREAM_KEY, _STREAM_GROUP, message_id)
        except Exception as exc:
            _log.warning("xack_error message_id=%s: %s", message_id, exc)

    def claim_stale_jobs(self, min_idle_ms: int = 90_000) -> list[str]:
        """Claim stream messages idle for more than *min_idle_ms* milliseconds.

        Returns the job_ids that were claimed.  The worker resets their status
        from RUNNING to PENDING so they are retried.  Replaces the fragile
        RUNNING-job scan used with BLPOP.
        """
        try:
            # XAUTOCLAIM returns (next_start_id, [(msg_id, {fields})], deleted_ids)
            result = self._client.xautoclaim(
                _STREAM_KEY,
                _STREAM_GROUP,
                self._consumer_id,
                min_idle_time=min_idle_ms,
                start_id="0-0",
                count=10,
            )
            _, claimed_messages, _ = result
            job_ids = []
            for message_id, fields in claimed_messages:
                job_id = fields.get("job_id", "")
                if job_id:
                    job_ids.append(job_id)
            if job_ids:
                _log.info("stream_claimed_stale count=%d", len(job_ids))
            return job_ids
        except Exception as exc:
            _log.warning("claim_stale_jobs_error: %s", type(exc).__name__)
            return []

    def get_queue_depth(self) -> int:
        """Return the total number of messages in the stream."""
        try:
            return self._client.xlen(_STREAM_KEY)
        except Exception:
            return 0

    def cancel(self, job_id: str) -> bool:
        """Mark a pending or running job as cancelled."""
        key = self._job_key(job_id)
        old_status = self._client.hget(key, "status")
        if old_status not in ("pending", "running"):
            return False
        updated_at = datetime.now(timezone.utc).isoformat()
        pipe = self._client.pipeline()
        pipe.hset(key, mapping={"status": "cancelled", "updated_at": updated_at})
        pipe.srem(f"{_STATUS_PREFIX}{old_status}", job_id)
        pipe.sadd(f"{_STATUS_PREFIX}cancelled", job_id)
        pipe.execute()
        return True

    def update_checkpoint(self, job_id: str, data: dict) -> None:
        """Persist only checkpoint_data for an in-progress job."""
        key = self._job_key(job_id)
        updated_at = datetime.now(timezone.utc).isoformat()
        self._client.hset(
            key,
            mapping={
                "checkpoint_data": json.dumps(data),
                "updated_at": updated_at,
            },
        )

    @staticmethod
    def _hash_to_job(data: dict) -> Job:
        raw_cp = data.get("checkpoint_data", "")
        return Job(
            id=data["id"],
            type=data["type"],
            params=json.loads(data["params"]),
            status=JobStatus(data["status"]),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
            result_path=data["result_path"] or None,
            error=data["error"] or None,
            checkpoint_data=json.loads(raw_cp) if raw_cp else None,
        )
