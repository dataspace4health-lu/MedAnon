"""Processing service — business logic for FHIR de-identification endpoints."""

import asyncio
import json
import logging
from typing import AsyncIterator

from pipeline.processor import process_data

logger = logging.getLogger("medanon")


class ProcessingError(Exception):
    """Domain error with an HTTP-status hint for the router layer."""

    def __init__(self, message: str, status: int = 500):
        super().__init__(message)
        self.status = status


class ProcessingService:
    """Orchestrates FHIR resource de-identification.

    All methods are HTTP-unaware.  Callers (routers) handle HTTP
    request/response formatting.
    """

    async def process_resource(self, resource, settings):
        """Process a single FHIR resource or Bundle.

        Raises ProcessingError with an appropriate status hint.
        """
        resource_type = (
            resource.get("resourceType", "unknown")
            if isinstance(resource, dict)
            else "array"
        )
        logger.info("Processing request: resourceType=%s", resource_type)
        try:
            result = await asyncio.to_thread(process_data, resource, settings)
            logger.info("Processing complete: resourceType=%s", resource_type)
            return result
        except ValueError as exc:
            logger.warning(
                "Validation error processing resourceType=%s: %s", resource_type, exc
            )
            raise ProcessingError("Invalid input", status=422) from exc
        except NotImplementedError as exc:
            logger.warning(
                "Not-implemented action for resourceType=%s: %s", resource_type, exc
            )
            raise ProcessingError("Unsupported operation", status=400) from exc
        except Exception as exc:
            logger.error(
                "Unexpected error processing resourceType=%s: %s",
                resource_type,
                type(exc).__name__,
                exc_info=False,
            )
            raise ProcessingError("Unexpected processing error", status=500) from exc

    async def process_ndjson_lines(
        self, lines: list[str], settings
    ) -> AsyncIterator[str]:
        """Process NDJSON lines, yielding JSON result strings (no trailing newline)."""
        for lineno, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            try:
                resource = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("NDJSON line %d: JSON parse error — %s", lineno, exc)
                yield json.dumps(
                    {"error": f"line {lineno}: invalid JSON — {exc}"}
                )
                continue
            try:
                result = await asyncio.to_thread(process_data, resource, settings)
                yield json.dumps(result)
            except Exception as exc:
                logger.error(
                    "NDJSON line %d: processing error: %s",
                    lineno,
                    type(exc).__name__,
                    exc_info=False,
                )
                yield json.dumps({"error": f"line {lineno}: processing error"})

    async def process_resource_stream(
        self, resources: list[dict], settings
    ) -> AsyncIterator[str]:
        """Process a list of resources, yielding JSON result strings."""
        for idx, resource in enumerate(resources):
            try:
                result = await asyncio.to_thread(process_data, resource, settings)
                yield json.dumps(result)
            except Exception as exc:
                logger.error(
                    "process_batch resource %d: %s",
                    idx,
                    type(exc).__name__,
                    exc_info=False,
                )
                yield json.dumps({"error": f"resource {idx}: processing error"})
