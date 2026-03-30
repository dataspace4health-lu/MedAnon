"""Redis-backed job store for scalable multi-worker deployments.

Uses Redis hashes for per-job storage, a list (BLPOP) for event-driven
worker notification, and sorted sets for time-ordered listing.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone

from medanon_core.domain import Job, JobStatus

_log = logging.getLogger("medanon.jobs.redis")

_KEY_PREFIX = "medanon:job:"
_QUEUE_KEY = "medanon:job_queue"
_INDEX_KEY = "medanon:jobs_by_time"
_STATUS_PREFIX = "medanon:jobs:status:"
_TYPE_PREFIX = "medanon:jobs:type:"
_DEFAULT_TTL = 604800  # 7 days


class RedisJobStore:
    """Implements JobStorePort using Redis hashes + a BLPOP notification queue."""

    def __init__(self, redis_url: str, ttl: int = _DEFAULT_TTL) -> None:
        import redis as _redis

        self._client = _redis.StrictRedis.from_url(redis_url, decode_responses=True)
        self._ttl = ttl
        # Verify connectivity
        self._client.ping()
        _log.info("redis_job_store_connected url=%s", redis_url.split("@")[-1])

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
        """Return the oldest PENDING job (polling fallback)."""
        pending_ids = self._client.smembers(f"{_STATUS_PREFIX}pending")
        if not pending_ids:
            return None
        scores = {}
        for jid in pending_ids:
            score = self._client.zscore(_INDEX_KEY, jid)
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
            return [j for jid in all_ids if (j := self.get(jid)) is not None]

        if not candidates:
            return []
        scored = []
        for jid in candidates:
            score = self._client.zscore(_INDEX_KEY, jid)
            if score is not None:
                scored.append((jid, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        page = scored[offset : offset + limit]
        return [j for jid, _ in page if (j := self.get(jid)) is not None]

    def notify_new_job(self, job_id: str) -> None:
        """Push job_id onto the notification list for BLPOP-based workers."""
        self._client.rpush(_QUEUE_KEY, job_id)

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

    def wait_for_job(self, timeout: int = 5) -> str | None:
        """Block until a job_id appears on the queue, or timeout.

        Returns the job_id string, or None on timeout.
        """
        result = self._client.blpop(_QUEUE_KEY, timeout=timeout)
        if result is None:
            return None
        _, job_id = result
        return job_id

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
