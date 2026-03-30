"""FHIR server integration service — fetch, process, upload orchestration."""

import asyncio
import json
import logging
from typing import AsyncIterator

from pipeline.processor import process_data

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

    async def stream_from_server(
        self, server_url, resource_types, params, token, timeout, settings
    ) -> AsyncIterator[str]:
        """Fetch resources from a FHIR server, process each, yield JSON strings."""
        for _rt, resource in self._fhir.fetch_all_resource_types(
            server_url, resource_types, params=params, token=token, timeout=timeout
        ):
            try:
                result = await asyncio.to_thread(process_data, resource, settings)
                yield json.dumps(result)
            except Exception as exc:
                logger.error(
                    "Error processing resource type=%s: %s",
                    _rt, type(exc).__name__, exc_info=False,
                )
                yield json.dumps({"error": "processing error", "resourceType": _rt})

    async def stream_everything(
        self, server_url, resource_type, resource_id, params, token, timeout, settings
    ) -> AsyncIterator[str]:
        """Fetch $everything, process each resource, yield JSON strings."""
        for resource in self._fhir.fetch_everything(
            server_url, resource_type, resource_id,
            params=params, token=token, timeout=timeout,
        ):
            try:
                result = await asyncio.to_thread(process_data, resource, settings)
                yield json.dumps(result)
            except Exception as exc:
                _rtype = resource.get("resourceType", resource_type)
                logger.error(
                    "Error processing resource type=%s: %s",
                    _rtype, type(exc).__name__, exc_info=False,
                )
                yield json.dumps({"error": "processing error", "resourceType": _rtype})

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
        """Fetch from source, process, upload to target, yield status lines."""
        for _rt, resource in self._fhir.fetch_all_resource_types(
            source_url, resource_types, params=params,
            token=source_token, timeout=timeout,
        ):
            try:
                deidentified = await asyncio.to_thread(
                    process_data, resource, settings
                )
                resp = await asyncio.to_thread(
                    self._fhir.post_resource, target_url, deidentified,
                    token=target_token, timeout=timeout,
                )
                yield json.dumps({
                    "resourceType": _rt,
                    "target_id": resp.get("id"),
                    "status": "ok",
                })
            except Exception as exc:
                logger.error(
                    "round_trip error type=%s: %s",
                    _rt, type(exc).__name__, exc_info=False,
                )
                yield json.dumps({
                    "resourceType": _rt,
                    "status": "error",
                    "error": "processing error",
                })

    async def stream_bulk_export(
        self, server_url, level, resource_type, type_filter,
        since, token, timeout, settings,
    ) -> AsyncIterator[str]:
        """Run bulk export, process each resource, yield JSON strings."""
        _SENTINEL = object()
        gen = self._fhir.bulk_export(
            server_url, level=level, resource_type=resource_type,
            type_filter=type_filter, since=since,
            token=token, timeout=timeout,
        )
        try:
            while True:
                resource = await asyncio.to_thread(next, gen, _SENTINEL)
                if resource is _SENTINEL:
                    break
                try:
                    result = await asyncio.to_thread(process_data, resource, settings)
                    yield json.dumps(result)
                except Exception as exc:
                    _rtype = (
                        resource.get("resourceType", "Unknown")
                        if isinstance(resource, dict) else "Unknown"
                    )
                    logger.error(
                        "Error processing resource type=%s: %s",
                        _rtype, type(exc).__name__, exc_info=False,
                    )
                    yield json.dumps({"error": "processing error", "resourceType": _rtype})
        except ValueError as exc:
            logger.error("bulk-export error: %s", exc, exc_info=False)
            yield json.dumps({"error": str(exc)})

    async def stream_cohort(
        self, server_url, search_type, search_params, everything_params,
        token, timeout, settings,
    ) -> AsyncIterator[str]:
        """Run cohort search + $everything, process each, yield JSON strings."""
        _SENTINEL = object()
        gen = self._fhir.fetch_cohort(
            server_url, search_type=search_type,
            search_params=search_params,
            everything_params=everything_params,
            token=token, timeout=timeout,
        )
        try:
            while True:
                resource = await asyncio.to_thread(next, gen, _SENTINEL)
                if resource is _SENTINEL:
                    break
                try:
                    result = await asyncio.to_thread(process_data, resource, settings)
                    yield json.dumps(result)
                except Exception as exc:
                    _rtype = (
                        resource.get("resourceType", "Unknown")
                        if isinstance(resource, dict) else "Unknown"
                    )
                    logger.error(
                        "Error processing resource type=%s: %s",
                        _rtype, type(exc).__name__, exc_info=False,
                    )
                    yield json.dumps({"error": "processing error", "resourceType": _rtype})
        except ValueError as exc:
            logger.error("cohort error: %s", exc, exc_info=False)
            yield json.dumps({"error": str(exc)})
