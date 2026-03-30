"""Port interfaces for the MedAnon pipeline.

Pure Protocol definitions — no imports from integrations/ or pipeline/.
This keeps the domain layer dependency-free from HTTP infrastructure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterator, Protocol

if TYPE_CHECKING:
    from medanon_core.domain import Job


class PseudonymizerPort(Protocol):
    """Interface for pseudonymization backends (gPAS, mock, etc.)."""

    def pseudonymize_batch(
        self,
        values: list[str],
        params: dict,
    ) -> dict[str, str]:
        """Pseudonymize a batch of string values.

        Args:
            values: Original values to pseudonymize.
            params: Backend-specific parameters (domain, operation, auth, etc.).

        Returns:
            Mapping of {original_value: pseudonym}.
        """
        ...


class NlpDetectorPort(Protocol):
    """Interface for NLP-based PHI/PII detection backends."""

    def analyze_and_replace(
        self,
        text: str,
        entities: list[str],
        threshold: float,
        language: str,
        mode: str,
        token_state: dict,
    ) -> str:
        """Detect PHI/PII in *text* and return scrubbed version.

        Args:
            text: Input text to scan.
            entities: Presidio entity type names to detect.
            threshold: Minimum confidence score (0.0–1.0).
            language: Language code (e.g. 'en').
            mode: 'tokenize' for surrogate tokens, 'redact' for static placeholders.
            token_state: Mutable dict tracking deterministic token mappings.

        Returns:
            Text with detected PHI/PII replaced.
        """
        ...


class FhirClientPort(Protocol):
    """Interface for FHIR server HTTP interactions."""

    def fetch_resource_type(
        self,
        server_url: str,
        resource_type: str,
        params: dict | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        """Fetch all resources of a given type from a FHIR server."""
        ...

    def fetch_everything(
        self,
        server_url: str,
        resource_type: str,
        resource_id: str,
        params: dict | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        """Fetch a Patient/$everything Bundle."""
        ...

    def post_resource(
        self,
        server_url: str,
        resource: dict,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Upload a single resource (PUT if id present, POST otherwise)."""
        ...

    def upload_resources(
        self,
        server_url: str,
        resources: list[dict],
        token: str | None = None,
        timeout: float = 30.0,
    ) -> Iterator[dict]:
        """Upload multiple resources, yielding per-resource results."""
        ...

    def bulk_export(
        self,
        server_url: str,
        level: str = "system",
        resource_type: str | None = None,
        type_filter: str | None = None,
        since: str | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        """Run the FHIR Bulk Data Export protocol."""
        ...

    def fetch_cohort(
        self,
        server_url: str,
        search_type: str,
        search_params: dict | None = None,
        everything_params: dict | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        """Fetch a cohort of patients based on search criteria."""
        ...

    def get_capability_statement(
        self,
        server_url: str,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        """Retrieve the server's CapabilityStatement."""
        ...


class JobStorePort(Protocol):
    """Interface for async job persistence backends (SQLite, Redis, etc.)."""

    def create(self, job_type: str, params: dict) -> Job:
        """Persist a new PENDING job and return it."""
        ...

    def get(self, job_id: str) -> Job | None:
        """Fetch a job by ID; returns None if not found."""
        ...

    def update(self, job: Job) -> None:
        """Persist status, result_path and error changes for *job*."""
        ...

    def next_pending(self) -> Job | None:
        """Return the oldest PENDING job, or None if the queue is empty."""
        ...

    def list_jobs(
        self,
        status: str | None = None,
        job_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Job]:
        """List jobs with optional filtering, ordered by created_at descending."""
        ...

    def notify_new_job(self, job_id: str) -> None:
        """Signal the worker that a new job is available.

        For event-driven backends (Redis BLPOP), this pushes the job_id onto
        the notification queue.  For polling backends (SQLite), this is a no-op.
        """
        ...
