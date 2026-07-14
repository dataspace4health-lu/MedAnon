"""Dataspace connector endpoints.

Saved, encrypted **input sources** (FHIR servers) and **S3 output destinations**
that make connecting the de-identification engine to a dataspace a matter of
configuration: pick where data comes from, and the S3 location the de-identified
file is delivered to.

  POST   /v1/source-connections            create + encrypt a source (admin)
  GET    /v1/source-connections            list sources (no tokens)
  POST   /v1/source-connections/{id}/test  probe the FHIR server
  DELETE /v1/source-connections/{id}       remove a source (admin)

  POST   /v1/output-destinations           create + encrypt an S3 dest (admin)
  GET    /v1/output-destinations           list destinations (no secret keys)
  POST   /v1/output-destinations/{id}/test connect + ensure bucket exists
  DELETE /v1/output-destinations/{id}      remove a destination (admin)

Sources and destinations are referenced by id (``source_id`` / ``destination_id``)
on the export job requests; secrets never ride in the request body or job store.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api.auth import AuthContext
from api.deps import limiter
from api.services.connectors import (
    ConnectorNotFound,
    ConnectorStoreUnavailable,
    DestinationService,
    SourceService,
)
from integrations.sql_source.secrets import CredentialKeyError

router = APIRouter()
logger = logging.getLogger("medanon")

_sources = SourceService()
_destinations = DestinationService()


def _require_admin(request: Request) -> None:
    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(
            status_code=403, detail="Admin role required to manage connectors."
        )


def _map_error(exc: Exception) -> HTTPException:
    """Translate domain errors to clean HTTP responses (never leak internals)."""
    if isinstance(exc, ConnectorStoreUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, ConnectorNotFound):
        return HTTPException(status_code=404, detail=f"Connector not found: {exc}")
    if isinstance(exc, CredentialKeyError):
        return HTTPException(status_code=422, detail=str(exc))
    logger.error("connector_unexpected_error: %s: %s", type(exc).__name__, exc)
    return HTTPException(status_code=500, detail="Connector error")


# ── Input sources ──────────────────────────────────────────────────────────


class SourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    server_url: str = Field(min_length=1, max_length=1024)
    token: str = Field(default="", max_length=4096)
    kind: str = Field(default="fhir")
    role: str = Field(
        default="source",
        description="Where this saved server may be selected: source | target | both.",
    )

    @field_validator("server_url")
    @classmethod
    def _valid_url(cls, v: str) -> str:
        if not v.startswith(("http://", "https://")):
            raise ValueError("server_url must be an http(s) URL")
        return v

    @field_validator("kind")
    @classmethod
    def _valid_kind(cls, v: str) -> str:
        if v not in {"fhir"}:
            raise ValueError("kind must be 'fhir'")
        return v

    @field_validator("role")
    @classmethod
    def _valid_role(cls, v: str) -> str:
        if v not in {"source", "target", "both"}:
            raise ValueError("role must be source, target, or both")
        return v


@router.post("/source-connections", status_code=201)
@limiter.limit("30/minute")
async def create_source(body: SourceCreate, request: Request):
    """Save a new input source (bearer token encrypted at rest). Admin only."""
    _require_admin(request)
    try:
        return _sources.create(
            name=body.name,
            server_url=body.server_url,
            token=body.token,
            kind=body.kind,
            role=body.role,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/source-connections")
async def list_sources(request: Request, role: str | None = None):
    """List saved FHIR servers (never includes tokens).

    ``?role=source`` / ``?role=target`` filters to servers usable in that role
    (plus any saved as ``both``); omit for all.
    """
    try:
        return {"sources": _sources.list(role=role)}
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/source-connections/{source_id}/test")
@limiter.limit("30/minute")
async def test_source(source_id: str, request: Request):
    """Probe the FHIR server's CapabilityStatement to verify reachability + auth."""
    try:
        return await _sources.test(source_id)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.delete("/source-connections/{source_id}", status_code=204)
async def delete_source(source_id: str, request: Request):
    """Delete a saved input source. Admin only."""
    _require_admin(request)
    try:
        if not _sources.delete(source_id):
            raise HTTPException(status_code=404, detail="Source not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


# ── S3 output destinations ─────────────────────────────────────────────────


class DestinationCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=255, description="host:port")
    bucket: str = Field(min_length=1, max_length=255)
    access_key: str = Field(min_length=1, max_length=255)
    secret_key: str = Field(min_length=1, max_length=512)
    region: str | None = Field(default=None, max_length=64)
    key_prefix: str = Field(
        default="",
        max_length=512,
        description="Object-key template. Tokens: {job_id} {permit_id} {ts} "
        "{resource_type}. A trailing '/' or empty value appends {job_id}.ndjson.",
    )
    secure: bool = Field(default=True, description="Use TLS to the endpoint.")
    path_style: bool = Field(default=False)


@router.post("/output-destinations", status_code=201)
@limiter.limit("30/minute")
async def create_destination(body: DestinationCreate, request: Request):
    """Save a new S3 output destination (secret key encrypted at rest). Admin only."""
    _require_admin(request)
    try:
        return _destinations.create(
            name=body.name,
            endpoint=body.endpoint,
            bucket=body.bucket,
            access_key=body.access_key,
            secret_key=body.secret_key,
            region=body.region,
            key_prefix=body.key_prefix,
            secure=body.secure,
            path_style=body.path_style,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/output-destinations")
async def list_destinations(request: Request):
    """List saved S3 destinations (never includes secret keys)."""
    try:
        return {"destinations": _destinations.list()}
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/output-destinations/{dest_id}/test")
@limiter.limit("30/minute")
async def test_destination(dest_id: str, request: Request):
    """Connect to the destination and ensure the bucket exists."""
    try:
        return await _destinations.test(dest_id)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.delete("/output-destinations/{dest_id}", status_code=204)
async def delete_destination(dest_id: str, request: Request):
    """Delete a saved S3 destination. Admin only."""
    _require_admin(request)
    try:
        if not _destinations.delete(dest_id):
            raise HTTPException(status_code=404, detail="Destination not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc
