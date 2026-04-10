"""Processing service — business logic for FHIR de-identification endpoints."""

import asyncio
import logging
import os
from typing import AsyncIterator

from utils.json_fast import loads as _json_loads, dumps as _json_dumps

from pipeline.processor import process_data, process_data_batch, _BATCH_SIZE
from integrations.gpas.circuit_breaker import GpasUnavailableError
from api.services import GPAS_FATAL_JSON

logger = logging.getLogger("medanon")

_REQUEST_TIMEOUT = float(os.environ.get("MEDANON_REQUEST_TIMEOUT_SEC", "300"))


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
            result = await asyncio.wait_for(
                asyncio.to_thread(process_data, resource, settings),
                timeout=_REQUEST_TIMEOUT,
            )
            logger.info("Processing complete: resourceType=%s", resource_type)
            return result
        except asyncio.TimeoutError:
            logger.error(
                "Processing timeout after %ss: resourceType=%s",
                _REQUEST_TIMEOUT,
                resource_type,
            )
            raise ProcessingError(
                f"Processing exceeded {_REQUEST_TIMEOUT}s timeout budget",
                status=504,
            )
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

        Results are yielded incrementally as each chunk completes, preserving
        input order (parse errors interleaved at their original position).
        """
        # Parse all lines, collecting valid resources and tracking parse errors.
        # Each slot is either ("error", error_json) or ("valid", index_into_valid_list).
        slots: list[tuple[str, str | int]] = []
        valid_resources: list[dict] = []
        for lineno, raw in enumerate(lines, start=1):
            line = raw.strip()
            if not line or line.startswith("//"):
                continue
            try:
                resource = _json_loads(line)
                slots.append(("valid", len(valid_resources)))
                valid_resources.append(resource)
            except (ValueError, TypeError) as exc:
                logger.warning("NDJSON line %d: JSON parse error — %s", lineno, exc)
                slots.append(
                    (
                        "error",
                        _json_dumps({"error": f"line {lineno}: invalid JSON — {exc}"}),
                    )
                )

        if not slots:
            return

        # Process valid resources in chunks, filling results as we go
        valid_results: list[str | None] = [None] * len(valid_resources)
        emit_cursor = 0  # tracks how far we've yielded in `slots`

        for chunk_start in range(0, len(valid_resources), _BATCH_SIZE):
            chunk_end = min(chunk_start + _BATCH_SIZE, len(valid_resources))
            chunk = valid_resources[chunk_start:chunk_end]
            try:
                results = await asyncio.to_thread(process_data_batch, chunk, settings)
                for i, r in enumerate(results):
                    valid_results[chunk_start + i] = _json_dumps(r)
            except GpasUnavailableError as exc:
                logger.error(
                    "ndjson_stream: gPAS unavailable at chunk %d: %s",
                    chunk_start,
                    exc,
                    exc_info=False,
                )
                # Yield everything ready so far, then fatal error
                for idx in range(emit_cursor, len(slots)):
                    tag, val = slots[idx]
                    if tag == "error":
                        yield val
                    elif valid_results[val] is not None:
                        yield valid_results[val]
                    else:
                        break
                yield GPAS_FATAL_JSON
                return
            except Exception:
                # Per-resource fallback for this chunk
                for idx, res in enumerate(chunk):
                    vi = chunk_start + idx
                    try:
                        r = await asyncio.to_thread(process_data_batch, [res], settings)
                        valid_results[vi] = _json_dumps(r[0])
                    except GpasUnavailableError as gexc:
                        logger.error(
                            "ndjson_stream: gPAS unavailable: %s", gexc, exc_info=False
                        )
                        for si in range(emit_cursor, len(slots)):
                            tag, val = slots[si]
                            if tag == "error":
                                yield val
                            elif valid_results[val] is not None:
                                yield valid_results[val]
                            else:
                                break
                        yield GPAS_FATAL_JSON
                        return
                    except Exception as exc2:
                        logger.error(
                            "NDJSON resource: processing error: %s",
                            type(exc2).__name__,
                            exc_info=False,
                        )
                        valid_results[vi] = _json_dumps({"error": "processing error"})

            # Yield all contiguous ready slots from the cursor
            while emit_cursor < len(slots):
                tag, val = slots[emit_cursor]
                if tag == "error":
                    yield val
                    emit_cursor += 1
                elif valid_results[val] is not None:
                    yield valid_results[val]
                    valid_results[val] = None  # free memory
                    emit_cursor += 1
                else:
                    break  # next valid result not ready yet

        # Emit any remaining slots (trailing parse errors after last chunk)
        while emit_cursor < len(slots):
            tag, val = slots[emit_cursor]
            if tag == "error":
                yield val
            elif valid_results[val] is not None:
                yield valid_results[val]
            emit_cursor += 1

    async def process_resource_stream(
        self, resources: list[dict], settings
    ) -> AsyncIterator[str]:
        """Process a list of resources in chunks, yielding JSON result strings.

        If gPAS becomes unavailable mid-stream, a fatal error sentinel is yielded
        and the generator stops immediately — no partial results are silently dropped.
        """
        for chunk_start in range(0, len(resources), _BATCH_SIZE):
            chunk = resources[chunk_start : chunk_start + _BATCH_SIZE]
            try:
                results = await asyncio.to_thread(process_data_batch, chunk, settings)
                for result in results:
                    yield _json_dumps(result)
            except GpasUnavailableError as exc:
                logger.error(
                    "batch_stream: gPAS unavailable at resource %d: %s",
                    chunk_start,
                    exc,
                    exc_info=False,
                )
                yield _json_dumps(
                    {
                        "error": "gPAS service unavailable — stream halted to prevent partial results",
                        "fatal": True,
                        "stopped_at_resource": chunk_start,
                    }
                )
                return
            except Exception:
                # Per-resource fallback for the failed chunk
                for idx, res in enumerate(chunk):
                    try:
                        result = await asyncio.to_thread(
                            process_data_batch, [res], settings
                        )
                        yield _json_dumps(result[0])
                    except GpasUnavailableError as gexc:
                        logger.error(
                            "batch_stream: gPAS unavailable: %s", gexc, exc_info=False
                        )
                        yield GPAS_FATAL_JSON
                        return
                    except Exception as exc2:
                        logger.error(
                            "process_batch resource %d: %s",
                            chunk_start + idx,
                            type(exc2).__name__,
                            exc_info=False,
                        )
                        yield _json_dumps(
                            {"error": f"resource {chunk_start + idx}: processing error"}
                        )

    async def process_bundle_stream(self, bundle: dict, settings) -> AsyncIterator[str]:
        """Process a FHIR Bundle as a whole, then yield each entry's resource as NDJSON.

        Unlike process_resource_stream, this preserves cross-resource reference
        rewriting that _process_bundle() performs after all entries are processed.
        """
        try:
            result = await asyncio.to_thread(process_data, bundle, settings, None, True)
        except GpasUnavailableError as exc:
            logger.error("bundle_stream: gPAS unavailable: %s", exc, exc_info=False)
            yield GPAS_FATAL_JSON
            return
        except Exception as exc:
            logger.error("bundle_stream: %s", type(exc).__name__, exc_info=False)
            yield _json_dumps({"error": "bundle processing error"})
            return

        # Emit each processed entry resource as an NDJSON line
        entries = result.get("entry", []) if isinstance(result, dict) else []
        for entry in entries:
            r = entry.get("resource") if isinstance(entry, dict) else None
            if isinstance(r, dict):
                yield _json_dumps(r)
