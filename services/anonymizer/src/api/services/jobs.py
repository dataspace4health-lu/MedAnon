"""Job queue service — manages async job lifecycle."""

import json
import logging
import os

from domain.jobs import (  # noqa: F401 — re-exported for routers
    JobNotComplete,
    JobNotFound,
    JobQueueFull,
    JobResultMissing,
    JobStoreUnavailable,
)

logger = logging.getLogger("medanon")


class JobService:
    """Manages async job creation, status queries, and result retrieval."""

    def _get_store(self):
        import pipeline.jobs.store as _store_mod

        if _store_mod._job_store is None:
            raise JobStoreUnavailable("Job store not initialised")
        return _store_mod._job_store

    # ------------------------------------------------------------------
    # Backpressure (PR #3)
    # ------------------------------------------------------------------

    def _max_pending(self) -> int:
        """Read MEDANON_MAX_PENDING_JOBS at call time.

        Read on every call (not cached) so operators can change the cap
        live via env-var update + SIGHUP-style restart without redeploy.
        ``<= 0`` disables the cap entirely (useful for load tests).
        """
        try:
            return int(os.environ.get("MEDANON_MAX_PENDING_JOBS", "200"))
        except ValueError:
            return 200

    def _assert_capacity(self, store) -> None:
        """Raise ``JobQueueFull`` when the pending-job count exceeds the cap.

        We probe with ``limit=cap+1`` so the underlying store only fetches at
        most one row beyond the threshold — avoids the O(n) scan that a full
        list would cause when the queue is large.
        """
        cap = self._max_pending()
        if cap <= 0:
            return
        # All three job stores expose the same ``list_jobs`` signature.
        # Status filter uses the canonical lowercase string value.
        try:
            pending = store.list_jobs(status="pending", limit=cap + 1)
        except Exception as exc:
            # Fail open: never block submissions because the count probe
            # itself failed. Log so the operator can investigate.
            logger.warning("queue_capacity_probe_failed: %s", exc)
            return
        if len(pending) > cap:
            raise JobQueueFull(pending=len(pending), cap=cap)

    def _job_to_dict(self, job) -> dict:
        checkpoint = job.checkpoint_data or {}
        # "uploaded" is set by bulk-import to count only successful uploads.
        # Export jobs use "lines_written" (resources written to NDJSON).
        # Fall back through all keys for backward compatibility.
        processed = checkpoint.get(
            "uploaded", checkpoint.get("processed", checkpoint.get("lines_written", 0))
        )
        params = getattr(job, "params", None) or {}
        return {
            "job_id": job.id,
            "type": job.type,
            "status": job.status.value,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "result_path": job.result_path,
            "error": job.error,
            "processed": processed,
            "staged_count": checkpoint.get("staged_count"),
            "phase": checkpoint.get("phase", "queued"),
            "summary": checkpoint.get("summary"),
            "config_profile": params.get("config_profile", "auto"),
            "upload_errors": checkpoint.get("errors", 0),
            "upload_error_details": checkpoint.get("error_details", []),
        }

    def submit_bulk_export(self, server_url: str, params: dict) -> dict:
        """Create a bulk-export job. Returns the job dict."""
        store = self._get_store()
        self._assert_capacity(store)
        job = store.create("bulk-export", {"server_url": server_url, **params})
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def submit_cohort(self, server_url: str, params: dict) -> dict:
        """Create a cohort job. Returns the job dict."""
        store = self._get_store()
        self._assert_capacity(store)
        job = store.create("cohort", {"server_url": server_url, **params})
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def submit_patient_export(self, server_url: str, params: dict) -> dict:
        """Create a patient $everything export job. Returns the job dict."""
        store = self._get_store()
        self._assert_capacity(store)
        job = store.create("patient-export", {"server_url": server_url, **params})
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def submit_batch_patient_export(self, server_url: str, params: dict) -> dict:
        """Create a batch patient $everything export job. Returns the job dict."""
        store = self._get_store()
        self._assert_capacity(store)
        job = store.create("batch-patient-export", {"server_url": server_url, **params})
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def get_status(self, job_id: str) -> dict:
        """Get job status. Raises JobNotFound if missing."""
        store = self._get_store()
        job = store.get(job_id)
        if job is None:
            raise JobNotFound()
        return self._job_to_dict(job)

    def get_result_path(self, job_id: str) -> str:
        """Get the result key (local path or ``s3://`` URI).

        Raises:
            JobNotFound: if the job does not exist.
            JobNotComplete: if the job is not yet done.
            JobResultMissing: if the result has been cleaned up.
        """
        store = self._get_store()
        job = store.get(job_id)
        if job is None:
            raise JobNotFound()
        if job.status.value != "done":
            raise JobNotComplete(job.status.value)
        from integrations.storage import get_result_storage

        # Recovery path: an executor may have written the NDJSON to disk but
        # crashed before persisting ``result_path`` (historical bug in the
        # batch-patient-export staged executor, now fixed). When the canonical
        # ``<MEDANON_OUTPUT_DIR>/{job_id}.ndjson`` file exists, surface it
        # rather than returning HTTP 410 to the client.
        if not job.result_path:
            import os
            canonical = os.path.join(
                os.environ.get("MEDANON_OUTPUT_DIR", "/output"),
                f"{job_id}.ndjson",
            )
            if get_result_storage().exists(canonical):
                # Persist the discovered path back to the job store so future
                # lookups don't repeat the filesystem probe.
                job.result_path = canonical
                try:
                    store.update(job)
                except Exception:
                    logger.debug("backfill_result_path_persist_failed job=%s", job_id, exc_info=True)
                logger.info("backfill_result_path job=%s path=%s", job_id, canonical)
                return canonical
            raise JobResultMissing()

        if not get_result_storage().exists(job.result_path):
            raise JobResultMissing()
        return job.result_path

    def list_jobs(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict]:
        """List jobs with optional filtering."""
        store = self._get_store()
        jobs = store.list_jobs(
            status=status, job_type=job_type, limit=limit, offset=offset
        )
        return [self._job_to_dict(j) for j in jobs]

    def cancel_job(self, job_id: str) -> dict:
        """Cancel a pending or running job. Returns the updated job dict."""
        store = self._get_store()
        job = store.get(job_id)
        if job is None:
            raise JobNotFound()
        store.cancel(job_id)
        job = store.get(job_id)
        return self._job_to_dict(job)

    def submit_reprocess(
        self, source_job_id: str, config_profile: str = "auto"
    ) -> dict:
        """Queue a reprocess job that re-runs de-identification on staged rows.

        Raises JobNotFound if the source job does not exist.
        Raises JobStoreUnavailable if the job store is not initialised.
        """
        store = self._get_store()
        if store.get(source_job_id) is None:
            raise JobNotFound()
        job = store.create(
            "reprocess",
            {
                "source_job_id": source_job_id,
                "config_profile": config_profile,
            },
        )
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def requeue_dead_job(self, job_id: str) -> dict:
        """Manually rescue a poisoned job from the DLQ.

        Resets ``status`` to PENDING and clears the ``_retry_count`` checkpoint
        so the worker gives the job a fresh budget.  Caller (admin) accepts
        responsibility for the retry — typically after fixing the upstream
        cause (config bug, gPAS outage, malformed input).

        Raises:
            JobNotFound: if the job does not exist.
            ValueError: if the job is not in DEAD status (use ``cancel`` for
                pending/running jobs; nothing to do for done/error/cancelled).
        """
        from domain.jobs import JobStatus

        store = self._get_store()
        job = store.get(job_id)
        if job is None:
            raise JobNotFound()
        if job.status != JobStatus.DEAD:
            raise ValueError(
                f"Job is not in DLQ (status={job.status.value}); only "
                f"'dead' jobs can be requeued"
            )
        # Clear retry counter so the rescue gets a full budget.
        cp = dict(job.checkpoint_data or {})
        cp.pop("_retry_count", None)
        job.checkpoint_data = cp
        job.status = JobStatus.PENDING
        job.error = None
        store.update(job)
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def submit_bulk_import(self, params: dict) -> dict:
        """Queue a bulk-import job that uploads a completed NDJSON to a target FHIR server.

        *params* must contain at least one of ``job_id`` or ``ndjson_path``, plus
        ``target_url``.  Optional keys: ``target_token``, ``timeout``,
        ``parallel``, ``batch_size``.

        Raises JobStoreUnavailable if the job store is not initialised.
        """
        store = self._get_store()
        self._assert_capacity(store)
        job = store.create("bulk-import", params)
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    _UPLOAD_CHUNK = 10_000

    def upload_job_to_target(
        self,
        job_id: str,
        target_url: str,
        target_token: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Read a completed job's NDJSON result and upload resources to target FHIR server.

        Uses idempotent PUT (via upload_resources) so repeated calls are safe.
        Returns {job_id, uploaded, errors, total}.
        Supports both local-file and S3 result keys transparently.

        Streams the NDJSON in chunks of ``_UPLOAD_CHUNK`` resources to cap
        peak memory regardless of total file size.
        """
        result_path = self.get_result_path(
            job_id
        )  # raises JobNotFound / JobNotComplete / JobResultMissing

        from integrations.fhir.client import upload_resources
        from integrations.storage import get_result_storage

        uploaded = 0
        errors = 0
        stream = get_result_storage().open_stream(result_path)
        try:
            chunk: list[dict] = []
            for line in stream:
                if isinstance(line, (bytes, bytearray)):
                    line = line.decode("utf-8")
                line = line.strip()
                if not line:
                    continue
                try:
                    resource = json.loads(line)
                    if (
                        isinstance(resource, dict)
                        and resource.get("resourceType")
                        and "error" not in resource
                    ):
                        chunk.append(resource)
                except (json.JSONDecodeError, TypeError):
                    pass
                if len(chunk) >= self._UPLOAD_CHUNK:
                    uploaded, errors = self._flush_upload_chunk(
                        chunk,
                        target_url,
                        target_token,
                        timeout,
                        uploaded,
                        errors,
                        upload_resources,
                    )
                    chunk = []
            if chunk:
                uploaded, errors = self._flush_upload_chunk(
                    chunk,
                    target_url,
                    target_token,
                    timeout,
                    uploaded,
                    errors,
                    upload_resources,
                )
        finally:
            if hasattr(stream, "close"):
                stream.close()

        logger.info(
            "upload_job_to_target job=%s uploaded=%d errors=%d target=%s",
            job_id,
            uploaded,
            errors,
            target_url,
        )
        return {
            "job_id": job_id,
            "uploaded": uploaded,
            "errors": errors,
            "total": uploaded + errors,
        }

    @staticmethod
    def _flush_upload_chunk(
        chunk, target_url, target_token, timeout, uploaded, errors, upload_fn
    ):
        for result in upload_fn(target_url, chunk, token=target_token, timeout=timeout):
            if result["success"]:
                uploaded += 1
            else:
                errors += 1
        return uploaded, errors

    def get_staged_stats(self, job_id: str) -> dict:
        """Return staging row counts for a bulk-export or cohort job.

        Raises JobNotFound if the source job does not exist.
        Raises JobStoreUnavailable otherwise.
        Returns a dict with pending/done/error/total counts, or
        ``{"staging": "unavailable"}`` when staging is not configured.
        """
        store = self._get_store()
        if store.get(job_id) is None:
            raise JobNotFound()

        from pipeline.jobs import worker as _worker

        staging = _worker._staging
        if staging is None:
            return {"job_id": job_id, "staging": "unavailable"}

        counts = staging.count_by_status(job_id)
        return {"job_id": job_id, **counts}

    def delete_result(self, job_id: str) -> bool:
        """Delete the result file for a completed job.

        Returns True if the file was deleted, False if it did not exist.
        Raises JobNotFound / JobNotComplete / JobResultMissing as appropriate.
        """
        store = self._get_store()
        job = store.get(job_id)
        if job is None:
            raise JobNotFound()
        if job.status.value != "done":
            raise JobNotComplete(job.status.value)
        if not job.result_path:
            raise JobResultMissing()
        from integrations.storage import get_result_storage

        storage = get_result_storage()
        deleted = storage.delete(job.result_path)
        if deleted:
            logger.info("result_deleted job=%s path=%s", job_id, job.result_path)
        return deleted
