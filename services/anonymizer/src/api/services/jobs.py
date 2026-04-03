"""Job queue service — manages async job lifecycle."""

import json
import logging

from medanon_core.domain import (  # noqa: F401 — re-exported for routers
    JobNotComplete,
    JobNotFound,
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

    def _job_to_dict(self, job) -> dict:
        checkpoint = job.checkpoint_data or {}
        # Support both old-style (lines_written) and staged (processed) checkpoint keys
        processed = checkpoint.get("processed", checkpoint.get("lines_written", 0))
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
        }

    def submit_bulk_export(self, server_url: str, params: dict) -> dict:
        """Create a bulk-export job. Returns the job dict."""
        store = self._get_store()
        job = store.create("bulk-export", {"server_url": server_url, **params})
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def submit_cohort(self, server_url: str, params: dict) -> dict:
        """Create a cohort job. Returns the job dict."""
        store = self._get_store()
        job = store.create("cohort", {"server_url": server_url, **params})
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def submit_patient_export(self, server_url: str, params: dict) -> dict:
        """Create a patient $everything export job. Returns the job dict."""
        store = self._get_store()
        job = store.create("patient-export", {"server_url": server_url, **params})
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
        if not job.result_path:
            raise JobResultMissing()
        from integrations.storage import get_result_storage
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

    def submit_reprocess(self, source_job_id: str, config_profile: str = "auto") -> dict:
        """Queue a reprocess job that re-runs de-identification on staged rows.

        Raises JobNotFound if the source job does not exist.
        Raises JobStoreUnavailable if the job store is not initialised.
        """
        store = self._get_store()
        if store.get(source_job_id) is None:
            raise JobNotFound()
        job = store.create("reprocess", {
            "source_job_id": source_job_id,
            "config_profile": config_profile,
        })
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
        job = store.create("bulk-import", params)
        store.notify_new_job(job.id)
        return self._job_to_dict(job)

    def upload_job_to_target(self, job_id: str, target_url: str, target_token: str | None = None, timeout: float = 30.0) -> dict:
        """Read a completed job's NDJSON result and upload resources to target FHIR server.

        Uses idempotent PUT (via upload_resources) so repeated calls are safe.
        Returns {job_id, uploaded, errors, total}.
        Supports both local-file and S3 result keys transparently.
        """
        result_path = self.get_result_path(job_id)  # raises JobNotFound / JobNotComplete / JobResultMissing

        from integrations.fhir.client import upload_resources
        from integrations.storage import get_result_storage

        resources: list[dict] = []
        stream = get_result_storage().open_stream(result_path)
        try:
            for line in stream:
                if isinstance(line, (bytes, bytearray)):
                    line = line.decode("utf-8")
                line = line.strip()
                if not line:
                    continue
                try:
                    resource = json.loads(line)
                    if isinstance(resource, dict) and resource.get("resourceType") and "error" not in resource:
                        resources.append(resource)
                except (json.JSONDecodeError, TypeError):
                    pass
        finally:
            if hasattr(stream, "close"):
                stream.close()

        uploaded = 0
        errors = 0
        for result in upload_resources(target_url, resources, token=target_token, timeout=timeout):
            if result["success"]:
                uploaded += 1
            else:
                errors += 1

        logger.info("upload_job_to_target job=%s uploaded=%d errors=%d target=%s", job_id, uploaded, errors, target_url)
        return {"job_id": job_id, "uploaded": uploaded, "errors": errors, "total": uploaded + errors}

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
