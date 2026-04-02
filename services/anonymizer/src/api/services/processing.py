"""Processing service — business logic for FHIR de-identification endpoints."""

import asyncio
import json
import logging
from typing import AsyncIterator

from pipeline.processor import process_data, process_data_batch, _BATCH_SIZE
from integrations.gpas.circuit_breaker import GpasUnavailableError

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
        except GpasUnavailableError as exc:
            logger.error("gPAS unavailable: %s", exc, exc_info=False)
            raise ProcessingError(
                "gPAS pseudonymization service is unavailable. "
                "Processing has been halted to prevent partial results. "
                "Retry once gPAS recovers.",
                status=503,
            ) from exc
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
        """Process NDJSON lines in batches, yielding JSON result strings.

        If gPAS becomes unavailable mid-stream, a fatal error sentinel is yielded
        and the generator stops immediately — no partial results are silently dropped.
        """
        # Parse all lines, collecting valid resources and tracking parse errors
        parsed: list[tuple[int, dict | None, str | None]] = []
        for lineno, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            try:
                resource = json.loads(line)
                parsed.append((lineno, resource, None))
            except json.JSONDecodeError as exc:
                logger.warning("NDJSON line %d: JSON parse error — %s", lineno, exc)
                parsed.append((lineno, None, json.dumps(
                    {"error": f"line {lineno}: invalid JSON — {exc}"}
                )))

        # Extract valid resources for batching (preserving order)
        valid_resources = [r for _, r, _ in parsed if r is not None]

        # Batch-process in chunks
        all_results: list[str] = []
        for chunk_start in range(0, len(valid_resources), _BATCH_SIZE):
            chunk = valid_resources[chunk_start:chunk_start + _BATCH_SIZE]
            try:
                results = await asyncio.to_thread(process_data_batch, chunk, settings)
                all_results.extend(json.dumps(r) for r in results)
            except GpasUnavailableError as exc:
                logger.error("ndjson_stream: gPAS unavailable at chunk %d: %s", chunk_start, exc, exc_info=False)
                # Yield everything processed so far, then fatal error
                result_idx = 0
                for _lineno, resource, error_json in parsed:
                    if error_json:
                        yield error_json
                    elif result_idx < len(all_results):
                        yield all_results[result_idx]
                        result_idx += 1
                    else:
                        break
                yield json.dumps({
                    "error": "gPAS service unavailable — stream halted to prevent partial results",
                    "fatal": True,
                })
                return
            except Exception:
                # Per-resource fallback for this chunk
                for res in chunk:
                    try:
                        r = await asyncio.to_thread(process_data_batch, [res], settings)
                        all_results.append(json.dumps(r[0]))
                    except GpasUnavailableError as gexc:
                        logger.error("ndjson_stream: gPAS unavailable: %s", gexc, exc_info=False)
                        result_idx = 0
                        for _lineno, resource, error_json in parsed:
                            if error_json:
                                yield error_json
                            elif result_idx < len(all_results):
                                yield all_results[result_idx]
                                result_idx += 1
                            else:
                                break
                        yield json.dumps({
                            "error": "gPAS service unavailable — stream halted to prevent partial results",
                            "fatal": True,
                        })
                        return
                    except Exception as exc2:
                        logger.error("NDJSON resource: processing error: %s", type(exc2).__name__, exc_info=False)
                        all_results.append(json.dumps({"error": "processing error"}))

        # Interleave results with parse errors in input order
        result_idx = 0
        for _lineno, resource, error_json in parsed:
            if error_json:
                yield error_json
            else:
                if result_idx < len(all_results):
                    yield all_results[result_idx]
                    result_idx += 1

    async def process_resource_stream(
        self, resources: list[dict], settings
    ) -> AsyncIterator[str]:
        """Process a list of resources in chunks, yielding JSON result strings.

        If gPAS becomes unavailable mid-stream, a fatal error sentinel is yielded
        and the generator stops immediately — no partial results are silently dropped.
        """
        for chunk_start in range(0, len(resources), _BATCH_SIZE):
            chunk = resources[chunk_start:chunk_start + _BATCH_SIZE]
            try:
                results = await asyncio.to_thread(process_data_batch, chunk, settings)
                for result in results:
                    yield json.dumps(result)
            except GpasUnavailableError as exc:
                logger.error("batch_stream: gPAS unavailable at resource %d: %s", chunk_start, exc, exc_info=False)
                yield json.dumps({
                    "error": "gPAS service unavailable — stream halted to prevent partial results",
                    "fatal": True,
                    "stopped_at_resource": chunk_start,
                })
                return
            except Exception:
                # Per-resource fallback for the failed chunk
                for idx, res in enumerate(chunk):
                    try:
                        result = await asyncio.to_thread(process_data_batch, [res], settings)
                        yield json.dumps(result[0])
                    except GpasUnavailableError as gexc:
                        logger.error("batch_stream: gPAS unavailable: %s", gexc, exc_info=False)
                        yield json.dumps({
                            "error": "gPAS service unavailable — stream halted to prevent partial results",
                            "fatal": True,
                        })
                        return
                    except Exception as exc2:
                        logger.error(
                            "process_batch resource %d: %s",
                            chunk_start + idx,
                            type(exc2).__name__,
                            exc_info=False,
                        )
                        yield json.dumps({"error": f"resource {chunk_start + idx}: processing error"})

    async def process_bundle_stream(
        self, bundle: dict, settings
    ) -> AsyncIterator[str]:
        """Process a FHIR Bundle as a whole, then yield each entry's resource as NDJSON.

        Unlike process_resource_stream, this preserves cross-resource reference
        rewriting that _process_bundle() performs after all entries are processed.
        """
        try:
            result = await asyncio.to_thread(process_data, bundle, settings)
        except GpasUnavailableError as exc:
            logger.error("bundle_stream: gPAS unavailable: %s", exc, exc_info=False)
            yield json.dumps({
                "error": "gPAS service unavailable — stream halted to prevent partial results",
                "fatal": True,
            })
            return
        except Exception as exc:
            logger.error("bundle_stream: %s", type(exc).__name__, exc_info=False)
            yield json.dumps({"error": "bundle processing error"})
            return

        # Emit each processed entry resource as an NDJSON line
        entries = result.get("entry", []) if isinstance(result, dict) else []
        for entry in entries:
            r = entry.get("resource") if isinstance(entry, dict) else None
            if isinstance(r, dict):
                yield json.dumps(r)
