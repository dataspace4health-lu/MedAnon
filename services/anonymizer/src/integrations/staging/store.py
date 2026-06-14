"""PostgreSQL staging store for large-scale de-identification jobs.

The staging table stores ONLY resource references — no patient data:

  (job_id, resource_id, resource_type, fhir_source_url, status, expires_at)

Patient FHIR resources are re-fetched from the source FHIR server on demand
during Phase 2.  This design ensures that no PHI ever enters our databases:
gPAS is the only authorised store for any identifier mapping.

Two-phase execution:

  Phase 1 — FHIR Fetch:
    Stream resources from the source FHIR server.  Record only the resource ID
    and type in staging (one row per resource, no content).

  Phase 2 — De-identification:
    Read staged references in batches.  Re-fetch each batch from the source
    FHIR server via ``fetch_resources_by_ids`` (``_id`` search parameter).
    De-identify the fetched resources and write to the NDJSON output file.

Crash recovery:
    A crash after Phase 1 → Phase 2 resumes from the staged references.
    No FHIR re-fetch for Phase 1; rows already staged are not re-inserted
    (ON CONFLICT DO NOTHING).

Retention:
    Rows have an ``expires_at`` column.  The worker runs ``cleanup_expired()``
    hourly to purge rows past their TTL (default 30 days via
    ``MEDANON_STAGING_RETENTION_DAYS``).
"""

from __future__ import annotations

import os
import logging
from typing import Iterable

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

logger = logging.getLogger("medanon.staging")

_PARTITION_TARGET_ROWS: int = int(
    os.environ.get("MEDANON_PARTITION_TARGET_ROWS", "50000")
)

_DDL = """
CREATE SCHEMA IF NOT EXISTS medanon;

-- Staging table stores ONLY resource references — no patient data.
-- Patient FHIR resources are re-fetched from the source FHIR server in Phase 2.
CREATE TABLE IF NOT EXISTS medanon.staged_resources (
    id               BIGSERIAL PRIMARY KEY,
    job_id           TEXT NOT NULL,
    resource_id      TEXT NOT NULL,
    resource_type    TEXT NOT NULL,
    fhir_source_url  TEXT NOT NULL DEFAULT '',
    status           TEXT NOT NULL DEFAULT 'pending',
    error            TEXT,
    fetched_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    processed_at     TIMESTAMPTZ,
    expires_at       TIMESTAMPTZ,
    CONSTRAINT uq_job_resource UNIQUE (job_id, resource_id)
);

-- Migration: drop resource_json if it exists from a prior schema version.
-- Patient data must not be stored in this table.
DO $$ BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = 'medanon'
           AND table_name   = 'staged_resources'
           AND column_name  = 'resource_json'
    ) THEN
        ALTER TABLE medanon.staged_resources DROP COLUMN resource_json;
    END IF;
END $$;

-- Migration: add fhir_source_url if it does not yet exist.
ALTER TABLE medanon.staged_resources
    ADD COLUMN IF NOT EXISTS fhir_source_url TEXT NOT NULL DEFAULT '';

-- Migration: add resource_blob for OPT-IN encrypted body staging (A1).
-- This is NULL by default (refs-only, no PHI at rest). It is populated ONLY when
-- MEDANON_STAGE_BODIES=encrypted, and then holds a gzip+Fernet (AES-128-CBC+HMAC)
-- encrypted body — NOT the plaintext resource_json that was removed above.
-- Lets Phase 2 decrypt in-process instead of re-fetching from FHIR.
ALTER TABLE medanon.staged_resources
    ADD COLUMN IF NOT EXISTS resource_blob BYTEA;

CREATE INDEX IF NOT EXISTS idx_staged_job_status
    ON medanon.staged_resources (job_id, status);

CREATE INDEX IF NOT EXISTS idx_staged_expires
    ON medanon.staged_resources (expires_at)
    WHERE expires_at IS NOT NULL;
"""

# ---------------------------------------------------------------------------
# Idempotent migration: partition columns + staged_partitions table.
# Run after _DDL in ensure_schema() so both fresh installs and existing
# deployments get the partition support without manual intervention.
# ---------------------------------------------------------------------------
_PARTITION_DDL = """
ALTER TABLE medanon.staged_resources
    ADD COLUMN IF NOT EXISTS partition_id     INT,
    ADD COLUMN IF NOT EXISTS partition_status TEXT DEFAULT 'unclaimed';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'chk_staged_partition_status'
           AND conrelid = 'medanon.staged_resources'::regclass
    ) THEN
        ALTER TABLE medanon.staged_resources
            ADD CONSTRAINT chk_staged_partition_status
            CHECK (partition_status IN ('unclaimed', 'claimed', 'done', 'error'));
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS medanon.staged_partitions (
    job_id       TEXT NOT NULL,
    partition_id INT  NOT NULL,
    status       TEXT NOT NULL DEFAULT 'unclaimed'
        CHECK (status IN ('unclaimed', 'claimed', 'done', 'error')),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    claimed_at   TIMESTAMPTZ,
    PRIMARY KEY (job_id, partition_id)
);

ALTER TABLE medanon.staged_partitions
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ;

-- Batch-ledger extensions (roadmap P1): attempt accounting, idempotency
-- stamping, and output provenance per partition. All additive + nullable so
-- existing rows are unaffected.
ALTER TABLE medanon.staged_partitions
    ADD COLUMN IF NOT EXISTS attempt_count     INT NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS max_attempts      INT NOT NULL DEFAULT 5,
    ADD COLUMN IF NOT EXISTS last_error_code    TEXT,
    ADD COLUMN IF NOT EXISTS last_error_message TEXT,
    ADD COLUMN IF NOT EXISTS input_checksum     TEXT,
    ADD COLUMN IF NOT EXISTS output_uri         TEXT,
    ADD COLUMN IF NOT EXISTS output_checksum    TEXT,
    ADD COLUMN IF NOT EXISTS config_hash        TEXT,
    ADD COLUMN IF NOT EXISTS processor_version  TEXT,
    ADD COLUMN IF NOT EXISTS locked_by          TEXT,
    ADD COLUMN IF NOT EXISTS locked_until       TIMESTAMPTZ;

-- Dead-letter ledger (roadmap P1): partitions that exhausted max_attempts.
-- Separate table so the DLQ survives even if the partition row is later purged,
-- and so operator triage queries don't scan the live partition table.
CREATE TABLE IF NOT EXISTS medanon.dead_letter_partitions (
    id             BIGSERIAL PRIMARY KEY,
    job_id         TEXT NOT NULL,
    partition_id   INT  NOT NULL,
    stage          TEXT NOT NULL DEFAULT 'deid',
    attempt_count  INT  NOT NULL DEFAULT 0,
    error_code     TEXT,
    error_message  TEXT,
    config_hash    TEXT,
    dead_lettered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_dlp_job_partition_stage UNIQUE (job_id, partition_id, stage)
);

CREATE INDEX IF NOT EXISTS idx_dlp_job ON medanon.dead_letter_partitions (job_id);

CREATE INDEX IF NOT EXISTS idx_staged_partition_id
    ON medanon.staged_resources (job_id, partition_id)
    WHERE partition_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_staged_partitions_unclaimed
    ON medanon.staged_partitions (job_id, partition_id)
    WHERE status = 'unclaimed';

CREATE INDEX IF NOT EXISTS idx_staged_partitions_claimed
    ON medanon.staged_partitions (job_id, claimed_at)
    WHERE status = 'claimed';
"""


class StagingStore:
    """Thread-safe PostgreSQL staging store.

    All public methods acquire a connection from the pool for the duration of
    the call and release it immediately after.  Safe to call from multiple
    threads (asyncio.to_thread workers).
    """

    def __init__(
        self,
        db_url: str,
        retention_days: int = 30,
        pool: ThreadedConnectionPool | None = None,
    ) -> None:
        self._db_url = db_url
        # Coerce + range-check at the boundary so the value used in the
        # ``expires_sql`` interval string later cannot be anything other than
        # a small non-negative integer.  This is structural defence-in-depth
        # alongside the f-string in ``stage_resources``.
        try:
            retention_days = int(retention_days)
        except (TypeError, ValueError):
            raise ValueError(
                f"retention_days must be an integer (got {retention_days!r})"
            ) from None
        if retention_days < 0 or retention_days > 3650:
            raise ValueError(
                f"retention_days must be between 0 and 3650 (got {retention_days})"
            )
        self._retention_days = retention_days
        self._pool: ThreadedConnectionPool | None = pool
        self._owns_pool = pool is None  # only close pool if we created it

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def ensure_pool(self) -> None:
        """Create the connection pool WITHOUT running schema DDL.

        Used by process-executor child workers: the parent already ran
        ``ensure_schema`` before fanning out, so children must NOT re-run the
        ``ALTER TABLE`` / ``CREATE INDEX`` migrations — N concurrent DDL
        statements take ``AccessExclusiveLock`` on ``staged_resources`` and
        deadlock (observed with 8 children). This sets up only what a child
        needs to read/claim partitions, plus the A1 fail-closed key check.
        """
        from integrations.staging.blob_crypto import _fernet, stage_bodies_enabled

        if stage_bodies_enabled():
            _fernet()  # raises StageBlobKeyError if key missing/invalid
        if self._pool is None:
            from utils.pool_budget import pg_staging_budget

            self._pool = ThreadedConnectionPool(2, pg_staging_budget(), self._db_url)
            self._owns_pool = True

    def ensure_schema(self) -> None:
        """Create schema + table + indexes if they don't already exist."""
        # Fail-closed at setup: if encrypted body staging is requested, validate
        # the key NOW rather than discovering it mid-fetch (A1). A bad/missing
        # key raises StageBlobKeyError so the job fails fast and loud.
        from integrations.staging.blob_crypto import (
            _fernet,
            stage_bodies_enabled,
        )

        if stage_bodies_enabled():
            _fernet()  # raises StageBlobKeyError if key missing/invalid

        if self._pool is None:
            from utils.pool_budget import pg_staging_budget

            self._pool = ThreadedConnectionPool(2, pg_staging_budget(), self._db_url)
            self._owns_pool = True
        conn = self._pool.getconn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(_DDL)
            with conn:
                with conn.cursor() as cur:
                    cur.execute(_PARTITION_DDL)
            logger.info(
                "staging schema ready (retention_days=%d)", self._retention_days
            )
        finally:
            self._pool.putconn(conn)

    def _get_conn(self):
        if self._pool is None:
            raise RuntimeError("StagingStore.ensure_schema() must be called before use")
        return self._pool.getconn()

    def _put_conn(self, conn) -> None:
        if self._pool:
            try:
                if conn.closed:
                    self._pool.putconn(conn, close=True)
                else:
                    self._pool.putconn(conn)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def stage_batch(
        self,
        job_id: str,
        resources: list[dict],
        fhir_source_url: str = "",
    ) -> int:
        """Record resource references in staging — stores NO patient data.

        Only the resource ID and type are persisted alongside the source FHIR
        server URL needed for Phase 2 re-fetch.  The ``resource_json`` column
        no longer exists; patient data is never written to this table.

        Returns the number of rows inserted (< len(resources) when duplicates
        are skipped via ON CONFLICT DO NOTHING).
        """
        if not resources:
            return 0

        expires_sql = (
            f"NOW() + INTERVAL '{self._retention_days} days'"
            if self._retention_days > 0
            else "NULL"
        )

        # A1: optionally persist an ENCRYPTED body so Phase 2 decrypts in-process
        # instead of re-fetching. Default OFF → blob is NULL and behaviour is the
        # historical refs-only path (no PHI at rest). Fail-closed if enabled
        # without a key (encrypt_resource raises StageBlobKeyError).
        from integrations.staging.blob_crypto import (
            encrypt_resource,
            stage_bodies_enabled,
        )

        _stage_bodies = stage_bodies_enabled()

        rows = []
        for idx, resource in enumerate(resources):
            rtype = resource.get("resourceType", "Unknown")
            rid = resource.get("id")
            resource_id = f"{rtype}/{rid}" if rid else f"{rtype}/auto-{idx}"
            blob = (
                psycopg2.Binary(encrypt_resource(resource)) if _stage_bodies else None
            )
            rows.append((job_id, resource_id, rtype, fhir_source_url, blob))

        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    psycopg2.extras.execute_values(
                        cur,
                        """
                        INSERT INTO medanon.staged_resources
                            (job_id, resource_id, resource_type, fhir_source_url,
                             resource_blob, expires_at)
                        VALUES %s
                        ON CONFLICT (job_id, resource_id) DO NOTHING
                        """,
                        rows,
                        template=f"(%s, %s, %s, %s, %s, {expires_sql})",
                        page_size=len(rows),
                    )
                    return cur.rowcount
        finally:
            self._put_conn(conn)

    def mark_done(self, job_id: str, staged_ids: list[int]) -> None:
        """Mark a list of staged row IDs as processed.

        Accepts rows in either ``processing`` (streaming path that calls
        ``get_pending_batch`` first) or ``pending`` (partition-claim path
        where ``iter_partition`` doesn't transition status — the partition
        lock provides exclusivity instead).
        """
        if not staged_ids:
            return
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'done', processed_at = NOW()
                         WHERE job_id = %s AND id = ANY(%s)
                           AND status IN ('processing', 'pending')
                        """,
                        (job_id, staged_ids),
                    )
        finally:
            self._put_conn(conn)

    def mark_error(self, job_id: str, staged_id: int, error: str) -> None:
        """Mark a single staged row as errored."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'error', error = %s, processed_at = NOW()
                         WHERE job_id = %s AND id = %s
                        """,
                        (error[:2000], job_id, staged_id),
                    )
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get_pending_batch(self, job_id: str, limit: int = 100) -> list[dict]:
        """Return up to *limit* pending rows, atomically marking them 'processing'.

        Uses a CTE with ``FOR UPDATE SKIP LOCKED`` to claim rows in one
        statement — no window between SELECT and UPDATE where a crash could
        leave rows invisible to other workers.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        WITH batch AS (
                            SELECT id
                              FROM medanon.staged_resources
                             WHERE job_id = %s AND status = 'pending'
                             ORDER BY id
                             LIMIT %s
                             FOR UPDATE SKIP LOCKED
                        )
                        UPDATE medanon.staged_resources sr
                           SET status = 'processing'
                          FROM batch
                         WHERE sr.id = batch.id
                        RETURNING sr.id, sr.resource_id, sr.resource_type, sr.fhir_source_url
                        """,
                        (job_id, limit),
                    )
                    return [dict(row) for row in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def get_all_resources(self, job_id: str, page_size: int = 500) -> Iterable[dict]:
        """Iterate over every resource for a job (e.g. for re-processing).

        Uses keyset pagination on the integer ``id`` PK — releases and
        re-acquires the connection between pages so the pool is never starved
        during long-running jobs.  Pages of *page_size* rows at a time.
        """
        after_id = 0
        while True:
            conn = self._get_conn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        """
                        SELECT id, resource_id, resource_type, fhir_source_url
                          FROM medanon.staged_resources
                         WHERE job_id = %s AND id > %s
                         ORDER BY id
                         LIMIT %s
                        """,
                        (job_id, after_id, page_size),
                    )
                    rows = cur.fetchall()
            finally:
                self._put_conn(conn)

            if not rows:
                break
            for row in rows:
                yield dict(row)
            after_id = rows[-1]["id"]
            if len(rows) < page_size:
                break

    def count_by_status(self, job_id: str) -> dict[str, int]:
        """Return ``{pending, done, error, total}`` counts for a job."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT status, COUNT(*) AS n
                          FROM medanon.staged_resources
                         WHERE job_id = %s
                         GROUP BY status
                        """,
                        (job_id,),
                    )
                    result: dict[str, int] = {"pending": 0, "done": 0, "error": 0}
                    for status, n in cur.fetchall():
                        result[status] = int(n)
                    result["total"] = sum(result.values())
                    return result
        finally:
            self._put_conn(conn)

    def reset_pending(self, job_id: str) -> int:
        """Reset done/error rows for a job back to 'pending' (for re-processing).

        Rows in ``processing`` state are left untouched to avoid interfering
        with an active worker.  Use ``recover_stale_processing`` to reclaim
        rows that have been stuck in ``processing`` for too long.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'pending', processed_at = NULL, error = NULL
                         WHERE job_id = %s AND status != 'processing'
                        """,
                        (job_id,),
                    )
                    return cur.rowcount
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Maintenance
    # ------------------------------------------------------------------

    def cleanup_expired(self) -> int:
        """Delete rows whose ``expires_at`` has passed, in batches.

        Deletes up to 10 000 rows per transaction to avoid long-running
        locks.  Loops until no more expired rows remain.  Returns total
        deleted count.
        """
        total_deleted = 0
        conn = self._get_conn()
        try:
            while True:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            DELETE FROM medanon.staged_resources
                             WHERE id IN (
                                SELECT id FROM medanon.staged_resources
                                 WHERE expires_at < NOW()
                                 LIMIT 10000
                             )
                            """
                        )
                        deleted = cur.rowcount
                total_deleted += deleted
                if deleted < 10000:
                    break
            return total_deleted
        finally:
            self._put_conn(conn)

    def recover_stale_processing(self, timeout_minutes: int = 10) -> int:
        """Reset rows stuck in ``processing`` for longer than *timeout_minutes*.

        Rows that have been in ``processing`` state for too long indicate a
        worker crash between ``get_pending_batch`` and ``mark_done``.
        Resetting them to ``pending`` allows them to be picked up again.

        Returns the number of rows recovered.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources
                           SET status = 'pending'
                         WHERE status = 'processing'
                           AND fetched_at < NOW() - INTERVAL '%s minutes'
                        """,
                        (timeout_minutes,),
                    )
                    recovered = cur.rowcount
            if recovered:
                logger.info(
                    "recovered %d stale processing rows (timeout=%dmin)",
                    recovered,
                    timeout_minutes,
                )
            return recovered
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Partition-claim API (Phase 1 / Argo fan-out)
    # ------------------------------------------------------------------

    def plan_partitions(self, job_id: str, target_rows: int | None = None) -> int:
        """Assign ``partition_id`` to all unpartitioned rows for *job_id*.

        Uses ``ROW_NUMBER() OVER (PARTITION BY job_id ORDER BY id)`` bucketed
        by *target_rows* (default ``MEDANON_PARTITION_TARGET_ROWS``, 50 000)
        so each partition covers exactly ``target_rows`` consecutive rows.

        Inserts one row per distinct partition into ``medanon.staged_partitions``
        (upsert — safe to call more than once).

        Returns the total number of distinct partitions for the job.
        """
        n = target_rows or _PARTITION_TARGET_ROWS
        conn = self._get_conn()
        try:
            # Fast idempotency guard: when partitions already exist for this job,
            # return the count WITHOUT re-running the heavy UPDATE. The shards
            # dispatcher pre-plans once, then N partition-claim workers each call
            # plan_partitions again; without this guard those N concurrent
            # ``UPDATE staged_resources ... FROM (SELECT staged_resources)``
            # statements deadlock on the same rows. This makes the later calls
            # cheap no-ops so only the first does the bucketing work.
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "SELECT COUNT(*) FROM medanon.staged_partitions "
                        "WHERE job_id = %s",
                        (job_id,),
                    )
                    existing = cur.fetchone()[0]
            if existing:
                return existing
            with conn:
                with conn.cursor() as cur:
                    # Serialize the heavy bucketing UPDATE across any concurrent
                    # first-callers (workers/pods that all saw 0 partitions at the
                    # same instant) with a transaction-scoped advisory lock keyed
                    # on the job. This converts a deadlock into an ordered wait;
                    # the second caller then finds partitions present and the
                    # guard above (re-checked below) makes it a no-op.
                    cur.execute(
                        "SELECT pg_advisory_xact_lock(hashtext(%s))",
                        (f"plan_partitions:{job_id}",),
                    )
                    cur.execute(
                        "SELECT COUNT(*) FROM medanon.staged_partitions "
                        "WHERE job_id = %s",
                        (job_id,),
                    )
                    _already = cur.fetchone()[0]
                    if _already:
                        # A concurrent first-caller won the lock and planned;
                        # nothing to do — return the partition count it created.
                        return _already
                    cur.execute(
                        """
                        UPDATE medanon.staged_resources AS sr
                           SET partition_id     = sub.pid,
                               partition_status = 'unclaimed'
                          FROM (
                                SELECT id,
                                       ((ROW_NUMBER() OVER (
                                            PARTITION BY job_id ORDER BY id
                                       ) - 1) / %s)::INT AS pid
                                  FROM medanon.staged_resources
                                 WHERE job_id = %s AND partition_id IS NULL
                               ) AS sub
                         WHERE sr.id = sub.id
                        """,
                        (n, job_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO medanon.staged_partitions (job_id, partition_id)
                        SELECT DISTINCT %s, partition_id
                          FROM medanon.staged_resources
                         WHERE job_id = %s AND partition_id IS NOT NULL
                        ON CONFLICT (job_id, partition_id) DO NOTHING
                        """,
                        (job_id, job_id),
                    )
                    cur.execute(
                        "SELECT COUNT(*) FROM medanon.staged_partitions WHERE job_id = %s",
                        (job_id,),
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
        finally:
            self._put_conn(conn)

    def claim_next_partition(self, job_id: str) -> tuple[str, int] | None:
        """Atomically claim the next unclaimed partition for *job_id*.

        Uses ``FOR UPDATE SKIP LOCKED`` on the ``staged_partitions`` row so
        concurrent workers each claim distinct partitions — at-most-one-worker-
        per-partition guarantee.

        Returns ``(resource_type, partition_id)`` or ``None`` when all
        partitions are claimed or done.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        WITH next AS (
                            SELECT job_id, partition_id
                              FROM medanon.staged_partitions
                             WHERE job_id = %s AND status = 'unclaimed'
                             ORDER BY partition_id
                             LIMIT 1
                             FOR UPDATE SKIP LOCKED
                        )
                        UPDATE medanon.staged_partitions sp
                           SET status = 'claimed', claimed_at = NOW()
                          FROM next
                         WHERE sp.job_id = next.job_id
                           AND sp.partition_id = next.partition_id
                        RETURNING sp.partition_id
                        """,
                        (job_id,),
                    )
                    row = cur.fetchone()
                    if row is None:
                        return None
                    partition_id: int = row[0]
                    # Resolve the representative resource_type for this partition.
                    cur.execute(
                        """
                        SELECT resource_type
                          FROM medanon.staged_resources
                         WHERE job_id = %s AND partition_id = %s
                         LIMIT 1
                        """,
                        (job_id, partition_id),
                    )
                    rtype_row = cur.fetchone()
                    resource_type = rtype_row[0] if rtype_row else "Unknown"
                    return resource_type, partition_id
        finally:
            self._put_conn(conn)

    def iter_partition(
        self,
        job_id: str,
        resource_type: str,
        partition_id: int,
        page_size: int = 500,
    ) -> Iterable[dict]:
        """Stream rows for a single claimed partition via keyset pagination.

        Releases and re-acquires the connection between pages so the pool is
        never starved during large partitions (same pattern as
        ``get_all_resources``).
        """
        from integrations.staging.blob_crypto import stage_bodies_enabled

        # Only pull the (large) BYTEA blob when body-staging is on, so the
        # default refs-only path keeps its lean SELECT.
        _blob_col = ", resource_blob" if stage_bodies_enabled() else ""

        after_id = 0
        while True:
            conn = self._get_conn()
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"""
                        SELECT id, resource_id, resource_type, fhir_source_url{_blob_col}
                          FROM medanon.staged_resources
                         WHERE job_id = %s
                           AND partition_id = %s
                           AND id > %s
                         ORDER BY id
                         LIMIT %s
                        """,
                        (job_id, partition_id, after_id, page_size),
                    )
                    rows = cur.fetchall()
            finally:
                self._put_conn(conn)

            if not rows:
                break
            for row in rows:
                yield dict(row)
            after_id = rows[-1]["id"]
            if len(rows) < page_size:
                break

    def mark_partition_done(
        self, job_id: str, resource_type: str, partition_id: int
    ) -> None:
        """Mark a partition as ``done`` in ``staged_partitions``."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'done'
                         WHERE job_id = %s AND partition_id = %s
                        """,
                        (job_id, partition_id),
                    )
        finally:
            self._put_conn(conn)

    def release_partition(
        self, job_id: str, resource_type: str, partition_id: int
    ) -> None:
        """Reset a ``claimed`` partition back to ``unclaimed``.

        Called from the ``deid`` step's exception handler so that the next
        worker or Argo retry pod can reclaim and reprocess the partition.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'unclaimed', claimed_at = NULL
                         WHERE job_id = %s
                           AND partition_id = %s
                           AND status = 'claimed'
                        """,
                        (job_id, partition_id),
                    )
        finally:
            self._put_conn(conn)

    def recover_stale_partitions(self, timeout_minutes: int = 10) -> int:
        """Reset partitions stuck in ``claimed`` for longer than *timeout_minutes*.

        A worker that crashes mid-shard without reaching the ``release_partition``
        call in its exception handler leaves the partition ``claimed`` forever.
        This method resets those partitions to ``unclaimed`` so a sibling worker
        or Argo retry pod can reclaim them.

        Returns the number of partitions recovered.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'unclaimed', claimed_at = NULL
                         WHERE status = 'claimed'
                           AND claimed_at < NOW() - INTERVAL '%s minutes'
                        """,
                        (timeout_minutes,),
                    )
                    recovered = cur.rowcount
            if recovered:
                logger.info(
                    "recovered %d stale claimed partitions (timeout=%dmin)",
                    recovered,
                    timeout_minutes,
                )
            return recovered
        finally:
            self._put_conn(conn)

    # ------------------------------------------------------------------
    # Targeted partition CAS + dead-letter (RabbitMQ stage streaming, P1)
    # ------------------------------------------------------------------
    # The AMQP consumer already knows which partition a message refers to, so
    # it claims THAT partition (not "the next one"). The CAS guard absorbs
    # at-least-once duplicate deliveries: a second delivery for an already-
    # claimed/done partition simply fails to claim → the consumer acks+skips.

    def claim_partition(
        self, job_id: str, partition_id: int, worker_id: str = ""
    ) -> bool:
        """Atomically claim a specific unclaimed partition. True iff claimed.

        Returns False when the partition is already claimed/done (duplicate
        delivery) or does not exist — the caller acks and skips.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'claimed',
                               claimed_at = NOW(),
                               locked_by = %s,
                               attempt_count = attempt_count + 1
                         WHERE job_id = %s
                           AND partition_id = %s
                           AND status = 'unclaimed'
                        """,
                        (worker_id or None, job_id, partition_id),
                    )
                    return cur.rowcount > 0
        finally:
            self._put_conn(conn)

    def is_partition_done(
        self, job_id: str, partition_id: int, *, input_checksum: str = "",
        config_hash: str = "", processor_version: str = "",
    ) -> bool:
        """Idempotency stamp check: True if this partition was already processed
        with the SAME (input_checksum, config_hash, processor_version).

        Lets a re-delivered or replayed message short-circuit recompute (zero
        gPAS/NLP calls) when nothing relevant changed. When the stamp args are
        empty, only the terminal ``done`` status is checked.
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT status, input_checksum, config_hash, processor_version
                      FROM medanon.staged_partitions
                     WHERE job_id = %s AND partition_id = %s
                    """,
                    (job_id, partition_id),
                )
                row = cur.fetchone()
            if not row or row[0] != "done":
                return False
            if not (input_checksum or config_hash or processor_version):
                return True
            return (
                row[1] == (input_checksum or None)
                and row[2] == (config_hash or None)
                and row[3] == (processor_version or None)
            )
        finally:
            self._put_conn(conn)

    def complete_partition(
        self,
        job_id: str,
        partition_id: int,
        *,
        output_uri: str = "",
        output_checksum: str = "",
        input_checksum: str = "",
        config_hash: str = "",
        processor_version: str = "",
    ) -> None:
        """Mark a partition ``done`` and stamp idempotency + output provenance."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'done',
                               output_uri = %s,
                               output_checksum = %s,
                               input_checksum = %s,
                               config_hash = %s,
                               processor_version = %s,
                               last_error_code = NULL,
                               last_error_message = NULL
                         WHERE job_id = %s AND partition_id = %s
                        """,
                        (
                            output_uri or None,
                            output_checksum or None,
                            input_checksum or None,
                            config_hash or None,
                            processor_version or None,
                            job_id,
                            partition_id,
                        ),
                    )
        finally:
            self._put_conn(conn)

    def record_partition_error(
        self,
        job_id: str,
        partition_id: int,
        *,
        error_code: str = "",
        error_message: str = "",
    ) -> int:
        """Release a failed partition for retry and record the error.

        Returns the partition's current ``attempt_count`` so the caller can
        decide whether to retry or dead-letter.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'unclaimed',
                               claimed_at = NULL,
                               locked_by = NULL,
                               last_error_code = %s,
                               last_error_message = %s
                         WHERE job_id = %s AND partition_id = %s
                        RETURNING attempt_count, max_attempts
                        """,
                        (error_code or None, error_message or None, job_id, partition_id),
                    )
                    row = cur.fetchone()
                    return int(row[0]) if row else 0
        finally:
            self._put_conn(conn)

    def dead_letter_partition(
        self,
        job_id: str,
        partition_id: int,
        *,
        stage: str = "deid",
        error_code: str = "",
        error_message: str = "",
        config_hash: str = "",
    ) -> None:
        """Route a partition to the dead-letter ledger and mark it ``error``."""
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'error', locked_by = NULL
                         WHERE job_id = %s AND partition_id = %s
                        """,
                        (job_id, partition_id),
                    )
                    cur.execute(
                        """
                        INSERT INTO medanon.dead_letter_partitions
                            (job_id, partition_id, stage, attempt_count,
                             error_code, error_message, config_hash)
                        VALUES (
                            %s, %s, %s,
                            COALESCE((SELECT attempt_count FROM medanon.staged_partitions
                                       WHERE job_id = %s AND partition_id = %s), 0),
                            %s, %s, %s)
                        ON CONFLICT (job_id, partition_id, stage) DO UPDATE
                           SET attempt_count = EXCLUDED.attempt_count,
                               error_code = EXCLUDED.error_code,
                               error_message = EXCLUDED.error_message,
                               dead_lettered_at = NOW()
                        """,
                        (
                            job_id, partition_id, stage,
                            job_id, partition_id,
                            error_code or None, error_message or None,
                            config_hash or None,
                        ),
                    )
        finally:
            self._put_conn(conn)

    def count_open_partitions(self, job_id: str) -> int:
        """Number of partitions not yet ``done`` — the stage-join condition.

        When this hits zero, all partitions of the stage are complete and the
        winner publishes the next-stage message.
        """
        conn = self._get_conn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT count(*) FROM medanon.staged_partitions
                     WHERE job_id = %s AND status <> 'done'
                    """,
                    (job_id,),
                )
                return int(cur.fetchone()[0])
        finally:
            self._put_conn(conn)

    def list_dead_letter_partitions(self, job_id: str) -> list[dict]:
        """Return the dead-letter ledger rows for a job (operator triage)."""
        conn = self._get_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    """
                    SELECT job_id, partition_id, stage, attempt_count,
                           error_code, error_message, config_hash, dead_lettered_at
                      FROM medanon.dead_letter_partitions
                     WHERE job_id = %s
                     ORDER BY partition_id
                    """,
                    (job_id,),
                )
                return [dict(r) for r in cur.fetchall()]
        finally:
            self._put_conn(conn)

    def retry_dead_letter_partition(self, job_id: str, partition_id: int) -> bool:
        """Reset a dead-lettered partition to ``unclaimed`` for a fresh attempt.

        Clears the attempt counter and removes the DLQ ledger row. Returns True
        if a partition row was reset.
        """
        conn = self._get_conn()
        try:
            with conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE medanon.staged_partitions
                           SET status = 'unclaimed',
                               attempt_count = 0,
                               claimed_at = NULL,
                               locked_by = NULL,
                               last_error_code = NULL,
                               last_error_message = NULL
                         WHERE job_id = %s AND partition_id = %s
                        """,
                        (job_id, partition_id),
                    )
                    reset = cur.rowcount > 0
                    cur.execute(
                        """
                        DELETE FROM medanon.dead_letter_partitions
                         WHERE job_id = %s AND partition_id = %s
                        """,
                        (job_id, partition_id),
                    )
            return reset
        finally:
            self._put_conn(conn)

    def close(self) -> None:
        """Close the connection pool if we own it."""
        if self._pool and self._owns_pool:
            try:
                self._pool.closeall()
            except Exception:
                pass
            self._pool = None
