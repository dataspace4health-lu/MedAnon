"""FHIR client adapter — production implementation of FhirClientPort.

Wraps the low-level HTTP functions in ``integrations.fhir.client`` so the
API layer can depend on the port protocol rather than the transport directly.
"""

from __future__ import annotations

from typing import Iterator

from integrations.fhir import client as _fhir


class HttpFhirClientAdapter:
    """Implements :class:`~pipeline.ports.FhirClientPort` via urllib3 HTTP calls."""

    def fetch_resource_type(
        self,
        server_url: str,
        resource_type: str,
        params: dict | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        return _fhir.fetch_resource_type(server_url, resource_type, params, token, timeout)

    def fetch_everything(
        self,
        server_url: str,
        resource_type: str,
        resource_id: str,
        params: dict | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        return _fhir.fetch_everything(server_url, resource_type, resource_id, params, token, timeout)

    def post_resource(
        self,
        server_url: str,
        resource: dict,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        return _fhir.post_resource(server_url, resource, token, timeout)

    def upload_resources(
        self,
        server_url: str,
        resources: list[dict],
        token: str | None = None,
        timeout: float = 30.0,
    ) -> Iterator[dict]:
        return _fhir.upload_resources(server_url, resources, token, timeout)

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
        return _fhir.bulk_export(server_url, level, resource_type, type_filter, since, token, timeout)

    def fetch_cohort(
        self,
        server_url: str,
        search_type: str,
        search_params: dict | None = None,
        everything_params: dict | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        return _fhir.fetch_cohort(server_url, search_type, search_params, everything_params, token, timeout)

    def get_capability_statement(
        self,
        server_url: str,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> dict:
        return _fhir.get_capability_statement(server_url, token, timeout)

    def fetch_all_resource_types(
        self,
        server_url: str,
        resource_types: list[str],
        params: dict | None = None,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> list[dict]:
        return _fhir.fetch_all_resource_types(server_url, resource_types, params, token, timeout)
