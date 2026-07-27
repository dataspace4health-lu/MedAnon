"""FHIR server integration service  fetch, process, upload orchestration."""

import asyncio
import logging
import os
from typing import AsyncIterator

from utils.json_fast import dumps as _json_dumps

from api.services import GPAS_FATAL_JSON

from pipeline.processor import process_data, process_data_batch, _BATCH_SIZE
from integrations.gpas.circuit_breaker import GpasUnavailableError

logger = logging.getLogger("medanon")

_PREFETCH_BATCHES = int(os.environ.get("MEDANON_PREFETCH_BATCHES", "2"))

try:
    from scoring.constants import SCORING_ENABLED as _SCORING_ON
except ImportError:
    _SCORING_ON = False


class FhirServerService:
    """Orchestrates FHIR server fetch-process-upload workflows.

    Accepts a ``FhirClientPort``-compatible adapter for testability.
    """

    def __init__(self, fhir_client):
        self._fhir = fhir_client

    def resolve_resource_types(
        self,
        server_url: str,
        resource_types: list | None,
        token,
        timeout: float,
    ) -> list:
        """Return resource_types or auto-discover from /metadata.

        Raises ValueError if the server is unreachable.
        """
        if resource_types:
            return resource_types
        return self._fhir.get_capability_statement(
            server_url, token=token, timeout=timeout
        )

    # ------------------------------------------------------------------
    # Chunked batch helpers
    # ------------------------------------------------------------------

    async def _process_chunk(
        self, chunk: list[dict], settings, label: str
    ) -> AsyncIterator[str]:
        """Process a single chunk via process_data_batch, yielding results."""
        try:
            results = await asyncio.to_thread(
                process_data_batch,
                chunk,
                settings,
                None,
                _SCORING_ON,
            )
            for result in results:
                yield _json_dumps(result)
        except GpasUnavailableError as exc:
            logger.error("%s: gPAS unavailable: %s", label, exc, exc_info=False)
            yield GPAS_FATAL_JSON
            return
        except Exception:
            # Per-resource fallback for the failed chunk
            for res in chunk:
                try:
                    result = await asyncio.to_thread(
                        process_data_batch,
                        [res],
                        settings,
                        None,
                        _SCORING_ON,
                    )
                    yield _json_dumps(result[0])
                except GpasUnavailableError as gexc:
                    logger.error(
                        "%s: gPAS unavailable: %s", label, gexc, exc_info=False
                    )
                    yield GPAS_FATAL_JSON
                    return
                except Exception as exc2:
                    rtype = (
                        res.get("resourceType", "Unknown")
                        if isinstance(res, dict)
                        else "Unknown"
                    )
                    logger.error(
                        "%s: error processing resource type=%s: %s",
                        label,
                        rtype,
                        type(exc2).__name__,
                        exc_info=False,
                    )
                    yield _json_dumps(
                        {"error": "processing error", "resourceType": rtype}
                    )

    async def _stream_and_batch(
        self, gen, settings, *, label: str = "stream"
    ) -> AsyncIterator[str]:
        """Consume a synchronous resource generator in batches, yielding JSON results.

        When ``MEDANON_PREFETCH_BATCHES`` > 0 (default 2), a background producer
        task fetches resources into a bounded asyncio.Queue while the consumer
        processes and yields batches.  This pipelines I/O with CPU processing.
        """
        if _PREFETCH_BATCHES <= 0:
            async for line in self._stream_and_batch_sequential(
                gen, settings, label=label
            ):
                yield line
            return

        _SENTINEL = object()
        _ERROR = object()
        queue: asyncio.Queue = asyncio.Queue(maxsize=_PREFETCH_BATCHES * _BATCH_SIZE)

        async def _producer():
            try:
                while True:
                    resource = await asyncio.to_thread(next, gen, _SENTINEL)
                    if resource is _SENTINEL:
                        break
                    await queue.put(resource)
            except Exception as exc:
                await queue.put((_ERROR, exc))
            finally:
                await queue.put(_SENTINEL)

        producer_task = asyncio.create_task(_producer())

        try:
            chunk: list[dict] = []
            while True:
                item = await queue.get()
                if item is _SENTINEL:
                    break
                if isinstance(item, tuple) and len(item) == 2 and item[0] is _ERROR:
                    raise item[1]
                chunk.append(item)
                if len(chunk) >= _BATCH_SIZE:
                    fatal = False
                    async for line in self._process_chunk(chunk, settings, label):
                        yield line
                        if line is GPAS_FATAL_JSON:
                            fatal = True
                    if fatal:
                        # `finally` cancels + awaits the producer on this exit.
                        return
                    chunk = []

            if chunk:
                async for line in self._process_chunk(chunk, settings, label):
                    yield line
        except Exception:
            producer_task.cancel()
            raise
        finally:
            if not producer_task.done():
                producer_task.cancel()
                try:
                    await producer_task
                except asyncio.CancelledError:
                    pass

    async def _stream_and_batch_sequential(
        self, gen, settings, *, label: str = "stream"
    ) -> AsyncIterator[str]:
        """Fallback sequential implementation (MEDANON_PREFETCH_BATCHES=0)."""
        _SENTINEL = object()
        chunk: list[dict] = []

        while True:
            resource = await asyncio.to_thread(next, gen, _SENTINEL)
            if resource is _SENTINEL:
                break
            chunk.append(resource)
            if len(chunk) >= _BATCH_SIZE:
                fatal = False
                async for line in self._process_chunk(chunk, settings, label):
                    yield line
                    if line is GPAS_FATAL_JSON:
                        fatal = True
                if fatal:
                    return
                chunk = []

        if chunk:
            async for line in self._process_chunk(chunk, settings, label):
                yield line

    # ------------------------------------------------------------------
    # Streaming endpoints
    # ------------------------------------------------------------------

    async def stream_from_server(
        self, server_url, resource_types, params, token, timeout, settings
    ) -> AsyncIterator[str]:
        """Fetch resources from a FHIR server, process in batches, yield JSON strings."""
        gen = self._fhir.fetch_all_resource_types(
            server_url, resource_types, params=params, token=token, timeout=timeout
        )

        # Unwrap (resource_type, resource) tuples from the generator
        def _resources_only():
            for _rt, resource in gen:
                yield resource

        async for line in self._stream_and_batch(
            _resources_only(), settings, label="from_server"
        ):
            yield line

    async def stream_everything(
        self, server_url, resource_type, resource_id, params, token, timeout, settings
    ) -> AsyncIterator[str]:
        """Fetch $everything, process in batches, yield JSON strings."""
        gen = self._fhir.fetch_everything(
            server_url,
            resource_type,
            resource_id,
            params=params,
            token=token,
            timeout=timeout,
        )
        async for line in self._stream_and_batch(gen, settings, label="everything"):
            yield line

    async def process_and_upload(
        self, resource, target_url, target_token, timeout, settings
    ) -> dict:
        """De-identify resource and upload to target server.

        Returns ``{"uploaded": int, "errors": int, "results": list}``.
        Raises ValueError on de-identification failure.
        """
        deidentified = await asyncio.to_thread(
            process_data, resource, settings, None, _SCORING_ON
        )

        # Flatten Bundle entries or wrap single resource into a list
        if (
            isinstance(deidentified, dict)
            and deidentified.get("resourceType") == "Bundle"
        ):
            resources_to_upload = [
                entry["resource"]
                for entry in deidentified.get("entry", [])
                if isinstance(entry.get("resource"), dict)
            ]
        elif isinstance(deidentified, list):
            resources_to_upload = deidentified
        else:
            resources_to_upload = [deidentified]

        def _collect_upload():
            results = []
            for r in self._fhir.upload_resources(
                target_url,
                resources_to_upload,
                token=target_token,
                timeout=timeout,
            ):
                results.append(r)
            return results

        results = await asyncio.to_thread(_collect_upload)
        uploaded = sum(1 for r in results if r["success"])
        errors = sum(1 for r in results if not r["success"])

        logger.info(
            "process_and_upload: uploaded=%d errors=%d target=%s",
            uploaded,
            errors,
            target_url,
        )
        return {"uploaded": uploaded, "errors": errors, "results": results}

    async def stream_round_trip(
        self,
        source_url,
        target_url,
        resource_types,
        params,
        source_token,
        target_token,
        timeout,
        settings,
    ) -> AsyncIterator[str]:
        """Fetch from source, process in batches, upload to target, yield status lines.

        De-identification and upload are split into two phases so that
        ``upload_resources`` receives the full dataset and can apply a global
        topological sort  preventing HAPI-1094 referential integrity failures
        when Conditions (or other dependent types) are sent before the Patient
        or Encounter they reference.

        Phase 1 uses prefetch pipelining (fetch overlaps with de-identification)
        when ``MEDANON_PREFETCH_BATCHES > 0``.  Phase 2 streams upload results
        from the generator without materialising them all at once.
        """
        gen = self._fhir.fetch_all_resource_types(
            source_url,
            resource_types,
            params=params,
            token=source_token,
            timeout=timeout,
        )

        async def _deidentify(ch: list[tuple[str, dict]]) -> tuple[list[dict], bool]:
            """De-identify one chunk; returns (results, is_fatal)."""
            resources = [res for _, res in ch]
            try:
                return await asyncio.to_thread(
                    process_data_batch, resources, settings
                ), False
            except GpasUnavailableError as exc:
                logger.error("round_trip: gPAS unavailable: %s", exc, exc_info=False)
                return [], True
            except Exception:
                collected: list[dict] = []
                for rt, res in ch:
                    try:
                        result = (
                            await asyncio.to_thread(process_data_batch, [res], settings)
                        )[0]
                        collected.append(result)
                    except GpasUnavailableError as exc:
                        logger.error(
                            "round_trip: gPAS unavailable: %s", exc, exc_info=False
                        )
                        return collected, True
                    except Exception as exc2:
                        logger.error(
                            "round_trip error type=%s: %s",
                            rt,
                            type(exc2).__name__,
                            exc_info=False,
                        )
                return collected, False

        # Phase 1: de-identify in chunks with prefetch pipelining
        _SENTINEL = object()
        _ERROR = object()
        all_deidentified: list[dict] = []

        if _PREFETCH_BATCHES > 0:
            # Pipelined: fetch and process overlap via bounded queue
            fetch_q: asyncio.Queue = asyncio.Queue(
                maxsize=_PREFETCH_BATCHES * _BATCH_SIZE
            )

            async def _producer():
                try:
                    while True:
                        item = await asyncio.to_thread(next, gen, _SENTINEL)
                        if item is _SENTINEL:
                            break
                        await fetch_q.put(item)
                except Exception as exc:
                    await fetch_q.put((_ERROR, exc))
                finally:
                    await fetch_q.put(_SENTINEL)

            producer_task = asyncio.create_task(_producer())
            try:
                chunk: list[tuple[str, dict]] = []
                while True:
                    item = await fetch_q.get()
                    if item is _SENTINEL:
                        break
                    if isinstance(item, tuple) and len(item) == 2 and item[0] is _ERROR:
                        raise item[1]
                    _rt, resource = item
                    chunk.append((_rt, resource))
                    if len(chunk) >= _BATCH_SIZE:
                        results, fatal = await _deidentify(chunk)
                        all_deidentified.extend(results)
                        chunk = []
                        if fatal:
                            yield _json_dumps(
                                {"status": "error", "error": "gPAS unavailable"}
                            )
                            return

                if chunk:
                    results, fatal = await _deidentify(chunk)
                    all_deidentified.extend(results)
                    if fatal:
                        yield _json_dumps(
                            {"status": "error", "error": "gPAS unavailable"}
                        )
                        return
            except Exception:
                producer_task.cancel()
                raise
            finally:
                if not producer_task.done():
                    producer_task.cancel()
                    try:
                        await producer_task
                    except asyncio.CancelledError:
                        pass
        else:
            # Sequential fallback
            chunk = []
            while True:
                item = await asyncio.to_thread(next, gen, _SENTINEL)
                if item is _SENTINEL:
                    break
                _rt, resource = item
                chunk.append((_rt, resource))
                if len(chunk) >= _BATCH_SIZE:
                    results, fatal = await _deidentify(chunk)
                    all_deidentified.extend(results)
                    chunk = []
                    if fatal:
                        yield _json_dumps(
                            {"status": "error", "error": "gPAS unavailable"}
                        )
                        return

            if chunk:
                results, fatal = await _deidentify(chunk)
                all_deidentified.extend(results)
                if fatal:
                    yield _json_dumps({"status": "error", "error": "gPAS unavailable"})
                    return

        # Phase 2: upload  _infer_upload_tiers needs the full list for
        # topological ordering, but we stream results from the generator
        # instead of materialising all upload dicts at once.
        def _upload_gen():
            return self._fhir.upload_resources(
                target_url,
                all_deidentified,
                token=target_token,
                timeout=timeout,
            )

        upload_gen = await asyncio.to_thread(_upload_gen)
        _UP_SENTINEL = object()
        while True:
            r = await asyncio.to_thread(next, upload_gen, _UP_SENTINEL)
            if r is _UP_SENTINEL:
                break
            if r["success"]:
                yield _json_dumps(
                    {
                        "resourceType": r["resourceType"],
                        "target_id": r.get("server_id"),
                        "status": "ok",
                    }
                )
            else:
                logger.error(
                    "round_trip upload error type=%s: %s",
                    r["resourceType"],
                    r.get("error"),
                )
                yield _json_dumps(
                    {
                        "resourceType": r["resourceType"],
                        "status": "error",
                        "error": r.get("error", "upload error"),
                    }
                )

    async def stream_bulk_export(
        self,
        server_url,
        level,
        resource_type,
        type_filter,
        since,
        token,
        timeout,
        settings,
    ) -> AsyncIterator[str]:
        """Run bulk export, process in batches, yield JSON strings.

        Uses async polling (asyncio.sleep) to avoid blocking a thread-pool
        worker for the entire poll duration (up to 1 hour).
        """
        from integrations.fhir.bulk import (
            bulk_export_kick_off,
            _poll_bulk_status_single,
            _download_manifest_files,
            delete_bulk_export,
            _t,
        )
        import time as _time

        # Phase 1: Kick off (single HTTP round-trip in thread)
        try:
            status_url = await asyncio.to_thread(
                bulk_export_kick_off,
                server_url,
                level,
                resource_type,
                type_filter,
                since,
                token,
                timeout,
            )
        except Exception as exc:
            logger.error("bulk-export kick-off error: %s", exc, exc_info=False)
            yield _json_dumps(
                {"error": f"bulk export kick-off error: {type(exc).__name__}"}
            )
            return

        # Phase 2: Poll with asyncio.sleep (no thread held)
        try:
            deadline = _time.monotonic() + _t._FHIR_BULK_POLL_TIMEOUT
            manifest = None
            while _time.monotonic() < deadline:
                done, result = await asyncio.to_thread(
                    _poll_bulk_status_single,
                    status_url,
                    token,
                    timeout,
                )
                if done:
                    manifest = result
                    break
                await asyncio.sleep(result)  # non-blocking wait
            else:
                raise ValueError(
                    f"Bulk export poll timeout ({_t._FHIR_BULK_POLL_TIMEOUT}s) exceeded"
                )

            # Phase 3: Download and process.  Pass the source base URL so manifest
            # file URLs are origin-clamped to the reachable, trusted server
            # (servers advertise their own, often private/unreachable, host).
            gen = _download_manifest_files(
                manifest, token=token, timeout=timeout, source_base_url=server_url
            )
            async for line in self._stream_and_batch(
                gen, settings, label="bulk_export"
            ):
                yield line
        except Exception as exc:
            logger.error("bulk-export error: %s", exc, exc_info=False)
            yield _json_dumps({"error": f"bulk export error: {type(exc).__name__}"})
        finally:
            try:
                await asyncio.to_thread(delete_bulk_export, status_url, token, timeout)
            except Exception:
                pass  # best-effort cleanup

    async def stream_cohort(
        self,
        server_url,
        search_type,
        search_params,
        everything_params,
        token,
        timeout,
        settings,
    ) -> AsyncIterator[str]:
        """Run cohort search + $everything, process in batches, yield JSON strings."""
        gen = self._fhir.fetch_cohort(
            server_url,
            search_type=search_type,
            search_params=search_params,
            everything_params=everything_params,
            token=token,
            timeout=timeout,
        )
        try:
            async for line in self._stream_and_batch(gen, settings, label="cohort"):
                yield line
        except Exception as exc:
            logger.error("cohort error: %s", exc, exc_info=False)
            yield _json_dumps({"error": f"cohort error: {type(exc).__name__}"})
