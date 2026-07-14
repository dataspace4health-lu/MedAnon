"""Redis-backed job store  Redis Streams for guaranteed delivery.

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

from domain.jobs import Job, JobStatus

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

        self._client = _redis.StrictRedis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=30,  # must exceed XREADGROUP block timeout (5 s)
            socket_connect_timeout=2,
            retry_on_timeout=True,
        )
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
        """Idempotent consumer-group creation.  BUSYGROUP = already exists, ignore.

        Uses id="0" (not "$") so the group starts from the beginning of the
        stream  any messages enqueued before the group was created are still
        delivered rather than silently skipped.
        """
        import redis as _redis

        try:
            self._client.xgroup_create(
                _STREAM_KEY, _STREAM_GROUP, id="0", mkstream=True
            )
        except _redis.exceptions.ResponseError as exc:
            # BUSYGROUP means the group already exists  expected on restart.
            if "BUSYGROUP" not in str(exc):
                _log.warning("stream_group_init_error: %s", exc)
        except Exception as exc:
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
        """Persist status, result_path, error, and checkpoint_data changes for *job*.

        Uses a Lua script to atomically read old_status + write new fields +
        update secondary index sets, preventing race conditions.
        """
        job.updated_at = datetime.now(timezone.utc).isoformat()
        key = self._job_key(job.id)
        lua = """
        local key = KEYS[1]
        local new_status = ARGV[1]
        local updated_at = ARGV[2]
        local result_path = ARGV[3]
        local error_val = ARGV[4]
        local checkpoint = ARGV[5]
        local ttl = tonumber(ARGV[6])
        local job_id = ARGV[7]
        local status_prefix = ARGV[8]
        local old_status = redis.call('HGET', key, 'status')
        redis.call('HSET', key, 'status', new_status, 'updated_at', updated_at,
                    'result_path', result_path, 'error', error_val,
                    'checkpoint_data', checkpoint)
        redis.call('EXPIRE', key, ttl)
        if old_status and old_status ~= new_status then
            redis.call('SREM', status_prefix .. old_status, job_id)
            redis.call('SADD', status_prefix .. new_status, job_id)
        end
        return 1
        """
        self._client.eval(
            lua,
            1,
            key,
            job.status.value,
            job.updated_at,
            job.result_path or "",
            job.error or "",
            json.dumps(job.checkpoint_data) if job.checkpoint_data else "",
            str(self._ttl),
            job.id,
            _STATUS_PREFIX,
        )

    # Lua script: atomically claim the oldest PENDING job.
    # Reads all pending IDs, finds the one with the lowest score in the time
    # index, then transitions it from pending→running in the secondary set
    # all in a single server-side operation so no two workers can claim the
    # same job via the polling fallback path.
    _CLAIM_OLDEST_PENDING_LUA = """
    local pending_key   = KEYS[1]
    local index_key     = KEYS[2]
    local status_prefix = ARGV[1]
    local updated_at    = ARGV[2]

    local ids = redis.call('SMEMBERS', pending_key)
    if #ids == 0 then return nil end

    local oldest_id    = nil
    local oldest_score = nil
    for _, jid in ipairs(ids) do
        local score = redis.call('ZSCORE', index_key, jid)
        if score then
            score = tonumber(score)
            if oldest_score == nil or score < oldest_score then
                oldest_score = score
                oldest_id    = jid
            end
        end
    end

    if oldest_id == nil then return nil end

    redis.call('SREM', pending_key, oldest_id)
    redis.call('SADD', status_prefix .. 'running', oldest_id)
    local job_key = 'medanon:job:' .. oldest_id
    redis.call('HSET', job_key, 'status', 'running', 'updated_at', updated_at)
    return oldest_id
    """

    def next_pending(self) -> Job | None:
        """Atomically claim and return the oldest PENDING job.

        Polling fallback for non-Streams callers.  Uses a Lua script so the
        select-oldest + pending→running transition is a single server-side
        operation; two concurrent workers cannot claim the same job.
        """
        from datetime import datetime, timezone

        updated_at = datetime.now(timezone.utc).isoformat()
        job_id = self._client.eval(
            self._CLAIM_OLDEST_PENDING_LUA,
            2,
            f"{_STATUS_PREFIX}pending",
            _INDEX_KEY,
            _STATUS_PREFIX,
            updated_at,
        )
        if not job_id:
            return None
        return self.get(job_id)

    def list_jobs(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
        before_created_at: str | None = None,
    ) -> list[Job]:
        """List jobs with optional filtering, ordered by created_at descending.

        *before_created_at* enables keyset pagination: pass the ``created_at``
        ISO string of the last job from the previous page as the cursor.
        """
        # Convert ISO cursor to a Unix timestamp score for sorted-set range queries.
        max_score = "+inf"
        if before_created_at:
            try:
                max_score = str(
                    datetime.fromisoformat(before_created_at).timestamp() - 0.001
                )
            except ValueError:
                pass

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
            if before_created_at and max_score != "+inf":
                all_ids = self._client.zrevrangebyscore(
                    _INDEX_KEY, max_score, "-inf", start=0, num=limit
                )
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
        max_score_float = float(max_score) if max_score != "+inf" else float("inf")
        for jid, score in zip(id_list, scores_raw):
            if score is not None and float(score) < max_score_float:
                scored.append((jid, float(score)))
        scored.sort(key=lambda x: x[1], reverse=True)
        page = scored[offset : offset + limit]
        return self._batch_get_jobs([jid for jid, _ in page])

    def _batch_get_jobs(self, job_ids: list[str]) -> list[Job]:
        """Fetch multiple jobs in a single pipeline round-trip.

        Side effect: when a job_id has no hash data (TTL expired) it is
        also removed from the time index and any cached secondary sets so
        the ``medanon:jobs:status:*`` and ``medanon:jobs:type:*`` sets do
        not accumulate ghost IDs in long-running deployments.
        """
        if not job_ids:
            return []
        pipe = self._client.pipeline(transaction=False)
        for jid in job_ids:
            pipe.hgetall(self._job_key(jid))
        results = pipe.execute()
        jobs: list[Job] = []
        ghosts: list[str] = []
        for jid, data in zip(job_ids, results):
            if not data:
                ghosts.append(jid)
                continue
            try:
                jobs.append(self._hash_to_job(data))
            except (KeyError, ValueError):
                pass  # stale/corrupt hash  skip
        if ghosts:
            try:
                cleanup = self._client.pipeline(transaction=False)
                cleanup.zrem(_INDEX_KEY, *ghosts)
                # Sweep across known status/type sets; SREM on a missing
                # member is a no-op so over-broad cleanup is safe.
                for status_val in ("pending", "running", "done", "failed", "cancelled"):
                    cleanup.srem(f"{_STATUS_PREFIX}{status_val}", *ghosts)
                cleanup.execute()
                _log.debug("redis_job_store cleaned %d ghost job ids", len(ghosts))
            except Exception:
                # Cleanup is best-effort; never let it break the read path.
                pass
        return jobs

    def cleanup_orphan_index(self, batch_size: int = 500) -> int:
        """Remove zset/secondary-set members whose job hash has expired.

        ``_batch_get_jobs`` already cleans ghosts opportunistically, but only
        for IDs returned by a query. Members that are never read again (e.g.
        old ``done`` jobs after the hash TTL expires) would accumulate in
        ``medanon:jobs_by_time`` and the per-status sets indefinitely.

        This method walks the time index in pages, EXISTS-checks each ID,
        and removes orphans. It is safe to call concurrently  Redis SREM /
        ZREM on missing members is a no-op.

        Returns the number of orphan IDs removed (for diagnostics / metrics).
        """
        removed = 0
        cursor: int | str = 0
        # ZSCAN guarantees we see every member at least once even when the
        # set is being mutated concurrently.
        while True:
            cursor, items = self._client.zscan(
                _INDEX_KEY, cursor=cursor, count=batch_size
            )
            if items:
                ids = [member for member, _score in items]
                pipe = self._client.pipeline(transaction=False)
                for jid in ids:
                    pipe.exists(self._job_key(jid))
                exists_flags = pipe.execute()
                ghosts = [jid for jid, ex in zip(ids, exists_flags) if not ex]
                if ghosts:
                    cleanup = self._client.pipeline(transaction=False)
                    cleanup.zrem(_INDEX_KEY, *ghosts)
                    for status_val in (
                        "pending",
                        "running",
                        "done",
                        "failed",
                        "cancelled",
                    ):
                        cleanup.srem(f"{_STATUS_PREFIX}{status_val}", *ghosts)
                    # Type sets are unbounded by name, so scan them too.
                    type_keys = self._client.keys(f"{_TYPE_PREFIX}*")
                    for tk in type_keys:
                        cleanup.srem(tk, *ghosts)
                    cleanup.execute()
                    removed += len(ghosts)
            if cursor == 0 or cursor == "0":
                break
        if removed:
            _log.info("redis_job_store_orphan_sweep removed=%d", removed)
        return removed

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
            err_str = str(exc)
            # NOGROUP means the consumer group vanished (e.g. Redis restart with
            # no persistence, or first deployment race).  Re-create it and let
            # the caller retry on the next loop iteration.
            if "NOGROUP" in err_str:
                _log.warning("xreadgroup_nogroup  recreating consumer group")
                self._ensure_stream_group()
            else:
                _log.warning("xreadgroup_error: %s  retrying", type(exc).__name__)
            return None

        if not results:
            return None
        _, messages = results[0]
        if not messages:
            return None
        message_id, fields = messages[0]
        job_id = fields.get("job_id", "")
        if not job_id:
            # Malformed message  ack it to unblock the queue
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

    def refresh_claim(self, message_id: str) -> bool:
        """Reset a message's idle timer so a long-running job isn't reclaimed.

        Re-claims the message to THIS consumer with ``min_idle_time=0``, which
        resets its idle clock in the Pending Entry List. The worker calls this
        on a heartbeat while a job runs, so neither the periodic stale-recovery
        loop nor a sibling worker's startup recovery mistakes an actively
        running long job for a crashed one (the redelivery/double-run hazard).

        Returns True if the claim was refreshed (we still own it), False
        otherwise (e.g. another consumer already reclaimed it  the job will be
        re-run there and this worker's eventual XACK is a harmless no-op).
        """
        try:
            # XCLAIM with min_idle_time=0 + JUSTID: cheap, returns the ids we
            # still hold. Owning consumer re-claiming itself just resets idle.
            held = self._client.xclaim(
                _STREAM_KEY,
                _STREAM_GROUP,
                self._consumer_id,
                min_idle_time=0,
                message_ids=[message_id],
                justid=True,
            )
            return bool(held)
        except Exception as exc:
            _log.debug("refresh_claim_error message_id=%s: %s", message_id, exc)
            return False

    def claim_stale_jobs(self, min_idle_ms: int = 90_000) -> list[tuple[str, str]]:
        """Claim stream messages idle for more than *min_idle_ms* milliseconds.

        Returns ``[(job_id, message_id), ...]`` for every message claimed.
        The caller uses the message_id to ACK entries whose jobs are already
        terminal (done/error/cancelled/dead)  those must be ACK'd so they
        don't accumulate in the PEL across restarts.
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
            pairs: list[tuple[str, str]] = []
            for message_id, fields in claimed_messages:
                job_id = fields.get("job_id", "")
                if job_id:
                    pairs.append((job_id, message_id))
            if pairs:
                _log.info("stream_claimed_stale count=%d", len(pairs))
            return pairs
        except Exception as exc:
            _log.warning("claim_stale_jobs_error: %s", type(exc).__name__)
            return []

    def prune_dead_consumers(self, min_idle_ms: int = 3_600_000) -> int:
        """Delete consumers with no pending messages that have been idle too long.

        Every worker process mints a fresh ``worker-<uuid>`` at startup and never
        removes the old one, so each restart leaks a consumer into the group's
        ``XINFO CONSUMERS`` list. A production incident accumulated 150+; they are
        harmless individually but inflate every XINFO scan and obscure the real
        worker count in dashboards.

        Only prunes consumers that hold **zero** pending entries -- a consumer
        with pending work is either alive or holds messages a stale-claim pass
        still needs to reclaim, so deleting it would drop that ownership. Runs on
        a long interval (default 1 h idle) so a briefly-quiet live worker is never
        pruned. Returns the number deleted.
        """
        try:
            consumers = self._client.xinfo_consumers(_STREAM_KEY, _STREAM_GROUP)
        except Exception as exc:
            _log.warning("prune_dead_consumers_list_error: %s", type(exc).__name__)
            return 0

        deleted = 0
        for c in consumers:
            name = c.get("name", "")
            # Never prune this process's own consumer, whatever its idle time.
            if not name or name == self._consumer_id:
                continue
            if c.get("pending", 0) != 0:
                continue
            if c.get("idle", 0) < min_idle_ms:
                continue
            try:
                self._client.xgroup_delconsumer(_STREAM_KEY, _STREAM_GROUP, name)
                deleted += 1
            except Exception as exc:
                _log.warning(
                    "prune_dead_consumers_del_error consumer=%s: %s",
                    name,
                    type(exc).__name__,
                )
        if deleted:
            _log.info("pruned_dead_consumers count=%d", deleted)
        return deleted

    def get_queue_depth(self) -> int:
        """Return the number of pending (unacknowledged) messages in the stream.

        Uses XPENDING summary to get the actual backlog count, not XLEN which
        includes already-acknowledged messages not yet trimmed from the stream.
        """
        try:
            info = self._client.xpending(_STREAM_KEY, _STREAM_GROUP)
            return (
                info.get("pending", 0)
                if isinstance(info, dict)
                else (info[0] if info else 0)
            )
        except Exception:
            return 0

    def cancel(self, job_id: str) -> bool:
        """Mark a pending or running job as cancelled.

        Uses a Lua script for atomic read-check-write to prevent race conditions
        when concurrent cancel/update calls target the same job.
        """
        key = self._job_key(job_id)
        updated_at = datetime.now(timezone.utc).isoformat()
        lua = """
        local key = KEYS[1]
        local job_id = ARGV[1]
        local updated_at = ARGV[2]
        local status_prefix = ARGV[3]
        local old_status = redis.call('HGET', key, 'status')
        if old_status ~= 'pending' and old_status ~= 'running' then
            return 0
        end
        redis.call('HSET', key, 'status', 'cancelled', 'updated_at', updated_at)
        redis.call('SREM', status_prefix .. old_status, job_id)
        redis.call('SADD', status_prefix .. 'cancelled', job_id)
        return 1
        """
        result = self._client.eval(lua, 1, key, job_id, updated_at, _STATUS_PREFIX)
        return bool(result)

    def update_checkpoint(self, job_id: str, data: dict) -> None:
        """Persist only checkpoint_data for an in-progress job.

        Also refreshes the hash TTL and the status/type set TTLs to prevent
        secondary index inconsistency when job hashes outlive their index entries.
        """
        key = self._job_key(job_id)
        updated_at = datetime.now(timezone.utc).isoformat()
        lua = """
        local key = KEYS[1]
        local checkpoint = ARGV[1]
        local updated_at = ARGV[2]
        local ttl = tonumber(ARGV[3])
        local job_id = ARGV[4]
        local status_prefix = ARGV[5]
        local type_prefix = ARGV[6]
        redis.call('HSET', key, 'checkpoint_data', checkpoint, 'updated_at', updated_at)
        redis.call('EXPIRE', key, ttl)
        local status = redis.call('HGET', key, 'status')
        local job_type = redis.call('HGET', key, 'type')
        if status then
            redis.call('EXPIRE', status_prefix .. status, ttl)
        end
        if job_type then
            redis.call('EXPIRE', type_prefix .. job_type, ttl)
        end
        return 1
        """
        self._client.eval(
            lua,
            1,
            key,
            json.dumps(data),
            updated_at,
            str(self._ttl),
            job_id,
            _STATUS_PREFIX,
            _TYPE_PREFIX,
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
