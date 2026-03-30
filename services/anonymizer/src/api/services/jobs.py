"""Job queue service — manages async job lifecycle."""

import logging
import os

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
        from pipeline.jobs import _job_store
        if _job_store is None:
            raise JobStoreUnavailable("Job store not initialised")
        return _job_store

    def _job_to_dict(self, job) -> dict:
        checkpoint = job.checkpoint_data or {}
        return {
            "job_id": job.id,
            "type": job.type,
            "status": job.status.value,
            "created_at": job.created_at,
            "updated_at": job.updated_at,
            "result_path": job.result_path,
            "error": job.error,
            "processed": checkpoint.get("lines_written", 0),
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

    def get_status(self, job_id: str) -> dict:
        """Get job status. Raises JobNotFound if missing."""
        store = self._get_store()
        job = store.get(job_id)
        if job is None:
            raise JobNotFound()
        return self._job_to_dict(job)

    def get_result_path(self, job_id: str) -> str:
        """Get the result file path.

        Raises:
            JobNotFound: if the job does not exist.
            JobNotComplete: if the job is not yet done.
            JobResultMissing: if the result file has been cleaned up.
        """
        store = self._get_store()
        job = store.get(job_id)
        if job is None:
            raise JobNotFound()
        if job.status.value != "done":
            raise JobNotComplete(job.status.value)
        if not job.result_path or not os.path.exists(job.result_path):
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
