"""FHIR server integration service — fetch, process, upload orchestration."""

import asyncio
import json
import logging
from typing import AsyncIterator

from pipeline.processor import process_data, process_data_batch, _BATCH_SIZE
from integrations.gpas.circuit_breaker import GpasUnavailableError

logger = logging.getLogger("medanon")


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
            results = await asyncio.to_thread(process_data_batch, chunk, settings)
            for result in results:
                yield json.dumps(result)
        except GpasUnavailableError as exc:
            logger.error("%s: gPAS unavailable: %s", label, exc, exc_info=False)
            yield json.dumps({
                "error": "gPAS service unavailable — stream halted to prevent partial results",
                "fatal": True,
            })
            return
        except Exception:
            # Per-resource fallback for the failed chunk
            for res in chunk:
                try:
                    result = await asyncio.to_thread(process_data_batch, [res], settings)
                    yield json.dumps(result[0])
                except GpasUnavailableError as gexc:
                    logger.error("%s: gPAS unavailable: %s", label, gexc, exc_info=False)
                    yield json.dumps({
                        "error": "gPAS service unavailable — stream halted to prevent partial results",
                        "fatal": True,
                    })
                    return
                except Exception as exc2:
                    rtype = res.get("resourceType", "Unknown") if isinstance(res, dict) else "Unknown"
                    logger.error(
                        "%s: error processing resource type=%s: %s",
                        label, rtype, type(exc2).__name__, exc_info=False,
                    )
                    yield json.dumps({"error": "processing error", "resourceType": rtype})

    async def _stream_and_batch(
        self, gen, settings, *, label: str = "stream"
    ) -> AsyncIterator[str]:
        """Consume a synchronous resource generator in batches, yielding JSON results."""
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
                    if '"fatal": true' in line:
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

        async for line in self._stream_and_batch(_resources_only(), settings, label="from_server"):
            yield line

    async def stream_everything(
        self, server_url, resource_type, resource_id, params, token, timeout, settings
    ) -> AsyncIterator[str]:
        """Fetch $everything, process in batches, yield JSON strings."""
        gen = self._fhir.fetch_everything(
            server_url, resource_type, resource_id,
            params=params, token=token, timeout=timeout,
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
        deidentified = await asyncio.to_thread(process_data, resource, settings)

        # Flatten Bundle entries or wrap single resource into a list
        if isinstance(deidentified, dict) and deidentified.get("resourceType") == "Bundle":
            resources_to_upload = [
                entry["resource"]
                for entry in deidentified.get("entry", [])
                if isinstance(entry.get("resource"), dict)
            ]
        elif isinstance(deidentified, list):
            resources_to_upload = deidentified
        else:
            resources_to_upload = [deidentified]

        results = list(await asyncio.to_thread(
            lambda: list(self._fhir.upload_resources(
                target_url, resources_to_upload,
                token=target_token, timeout=timeout,
            ))
        ))
        uploaded = sum(1 for r in results if r["success"])
        errors = sum(1 for r in results if not r["success"])

        logger.info(
            "process_and_upload: uploaded=%d errors=%d target=%s",
            uploaded, errors, target_url,
        )
        return {"uploaded": uploaded, "errors": errors, "results": results}

    async def stream_round_trip(
        self, source_url, target_url, resource_types, params,
        source_token, target_token, timeout, settings,
    ) -> AsyncIterator[str]:
        """Fetch from source, process in batches, upload to target, yield status lines."""
        gen = self._fhir.fetch_all_resource_types(
            source_url, resource_types, params=params,
            token=source_token, timeout=timeout,
        )
        _SENTINEL = object()
        chunk: list[tuple[str, dict]] = []

        while True:
            item = await asyncio.to_thread(next, gen, _SENTINEL)
            if item is _SENTINEL:
                break
            _rt, resource = item
            chunk.append((_rt, resource))
            if len(chunk) >= _BATCH_SIZE:
                async for line in self._round_trip_chunk(
                    chunk, target_url, target_token, timeout, settings
                ):
                    yield line
                chunk = []

        if chunk:
            async for line in self._round_trip_chunk(
                chunk, target_url, target_token, timeout, settings
            ):
                yield line

    async def _round_trip_chunk(
        self, chunk: list[tuple[str, dict]], target_url, target_token, timeout, settings
    ) -> AsyncIterator[str]:
        """Process a chunk of (resource_type, resource) pairs and upload each."""
        resources = [res for _, res in chunk]
        resource_types = [rt for rt, _ in chunk]
        try:
            results = await asyncio.to_thread(process_data_batch, resources, settings)
        except Exception:
            # Per-resource fallback
            for rt, res in chunk:
                try:
                    result = (await asyncio.to_thread(process_data_batch, [res], settings))[0]
                    resp = await asyncio.to_thread(
                        self._fhir.post_resource, target_url, result,
                        token=target_token, timeout=timeout,
                    )
                    yield json.dumps({
                        "resourceType": rt, "target_id": resp.get("id"), "status": "ok",
                    })
                except Exception as exc:
                    logger.error("round_trip error type=%s: %s", rt, type(exc).__name__, exc_info=False)
                    yield json.dumps({
                        "resourceType": rt, "status": "error", "error": "processing error",
                    })
            return

        for rt, deidentified in zip(resource_types, results):
            try:
                resp = await asyncio.to_thread(
                    self._fhir.post_resource, target_url, deidentified,
                    token=target_token, timeout=timeout,
                )
                yield json.dumps({
                    "resourceType": rt, "target_id": resp.get("id"), "status": "ok",
                })
            except Exception as exc:
                logger.error("round_trip error type=%s: %s", rt, type(exc).__name__, exc_info=False)
                yield json.dumps({
                    "resourceType": rt, "status": "error", "error": "upload error",
                })

    async def stream_bulk_export(
        self, server_url, level, resource_type, type_filter,
        since, token, timeout, settings,
    ) -> AsyncIterator[str]:
        """Run bulk export, process in batches, yield JSON strings."""
        gen = self._fhir.bulk_export(
            server_url, level=level, resource_type=resource_type,
            type_filter=type_filter, since=since,
            token=token, timeout=timeout,
        )
        try:
            async for line in self._stream_and_batch(gen, settings, label="bulk_export"):
                yield line
        except Exception as exc:
            logger.error("bulk-export error: %s", exc, exc_info=False)
            yield json.dumps({"error": f"bulk export error: {type(exc).__name__}"})

    async def stream_cohort(
        self, server_url, search_type, search_params, everything_params,
        token, timeout, settings,
    ) -> AsyncIterator[str]:
        """Run cohort search + $everything, process in batches, yield JSON strings."""
        gen = self._fhir.fetch_cohort(
            server_url, search_type=search_type,
            search_params=search_params,
            everything_params=everything_params,
            token=token, timeout=timeout,
        )
        try:
            async for line in self._stream_and_batch(gen, settings, label="cohort"):
                yield line
        except Exception as exc:
            logger.error("cohort error: %s", exc, exc_info=False)
            yield json.dumps({"error": f"cohort error: {type(exc).__name__}"})
