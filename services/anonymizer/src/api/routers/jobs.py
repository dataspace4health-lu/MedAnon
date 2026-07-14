"""Async job queue endpoints  aggregator.

The endpoints are split across two modules for readability:

  - :mod:`api.routers.jobs_submit`   POST /v1/jobs/* submission endpoints
  - :mod:`api.routers.jobs_manage`   list / poll / cancel / result / detail

This module re-exports a single ``router`` (with both sub-routers merged) so
``api/main.py`` keeps including ``jobs.router`` unchanged.  Submission routes
are included first; management routes (which include the literal ``/jobs/dead``
ahead of the parameterised ``/jobs/{job_id}``) follow.

Endpoint map:
    POST   /jobs/bulk-export           queue a bulk export job, returns 202
    POST   /jobs/tabular-batch         de-identify many tabular files, returns 202
    POST   /jobs/sql-export            de-identify source-DB tables, returns 202
    POST   /jobs/cohort                queue a cohort export job, returns 202
    POST   /jobs/patient-export        queue a patient $everything export, returns 202
    POST   /jobs/batch-patient-export  queue a multi-patient $everything export, returns 202
    POST   /jobs/bulk-import           upload completed NDJSON to a target server, returns 202
    POST   /jobs/risk-driven-export    k-anonymity guaranteed export, returns 202
    GET    /jobs                       list jobs with optional filtering
    GET    /jobs/dead                  list DLQ jobs
    POST   /jobs/{job_id}/requeue      rescue a poisoned DLQ job
    GET    /jobs/{job_id}              poll job status
    DELETE /jobs/{job_id}              cancel a pending or running job
    GET    /jobs/{job_id}/result       download completed NDJSON result
    DELETE /jobs/{job_id}/result       delete a completed NDJSON result file
    POST   /jobs/{job_id}/reprocess    re-process staged rows with a new config profile
    POST   /jobs/{job_id}/upload-to-target  upload completed results to target server
    GET    /jobs/{job_id}/staged-stats  staging row counts (pending/done/error/total)
    GET    /jobs/{job_id}/detail       cached parsed-result detail
    POST   /jobs/{job_id}/detail       save parsed-result detail
"""

from __future__ import annotations

from fastapi import APIRouter

from api.routers import jobs_manage, jobs_submit

router = APIRouter()
router.include_router(jobs_submit.router)
router.include_router(jobs_manage.router)
