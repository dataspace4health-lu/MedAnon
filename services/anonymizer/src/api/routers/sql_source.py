"""SQL source de-identification endpoints.

Saved connections (read-only, host allow-listed, encrypted passwords) to external
PostgreSQL databases, schema inspection (the column explorer at DB scale), and an
async export job that de-identifies selected tables to files.

  POST   /v1/sql-connections            create + validate a connection (admin)
  GET    /v1/sql-connections            list connections (no passwords)
  POST   /v1/sql-connections/{id}/test  open+close a read-only connection
  DELETE /v1/sql-connections/{id}       remove a connection (admin)
  POST   /v1/process/sql/inspect        reflect schema → tables/columns + recommendations

The async ``sql-export`` job is submitted via ``POST /v1/jobs/sql-export``
(in ``api/routers/jobs.py``).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from api.auth import AuthContext
from api.deps import limiter
from api.services.sql_source import (
    SqlConnectionNotFound,
    SqlSourceService,
    SqlStoreUnavailable,
)
from integrations.sql_source.connect import SqlSourceError
from integrations.sql_source.secrets import CredentialKeyError

router = APIRouter()
logger = logging.getLogger("medanon")

_service = SqlSourceService()


def _require_admin(request: Request) -> None:
    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(
            status_code=403, detail="Admin role required to manage SQL connections."
        )


class SqlConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(default=5432, ge=1, le=65535)
    dbname: str = Field(min_length=1, max_length=128)
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(default="", max_length=512)
    sslmode: str = Field(default="prefer")

    @field_validator("sslmode")
    @classmethod
    def _valid_sslmode(cls, v: str) -> str:
        allowed = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
        if v not in allowed:
            raise ValueError(f"sslmode must be one of {sorted(allowed)}")
        return v


class SqlInspectRequest(BaseModel):
    connection_id: str
    schema_name: str = Field(default="public", max_length=128, alias="schema")

    model_config = {"populate_by_name": True}


def _map_error(exc: Exception) -> HTTPException:
    """Translate domain errors to clean HTTP responses (never leak internals)."""
    if isinstance(exc, SqlStoreUnavailable):
        return HTTPException(status_code=503, detail=str(exc))
    if isinstance(exc, SqlConnectionNotFound):
        return HTTPException(status_code=404, detail=f"Connection not found: {exc}")
    if isinstance(exc, (SqlSourceError, CredentialKeyError)):
        return HTTPException(status_code=422, detail=str(exc))
    # Unknown failure  log the real cause (type + message) so it is diagnosable
    # without leaking internals to the client.
    logger.error("sql_source_unexpected_error: %s: %s", type(exc).__name__, exc)
    return HTTPException(status_code=500, detail="SQL source error")


@router.post("/sql-connections", status_code=201)
@limiter.limit("30/minute")
async def create_sql_connection(body: SqlConnectionCreate, request: Request):
    """Save a new SQL source connection (password encrypted at rest). Admin only."""
    _require_admin(request)
    try:
        return _service.create_connection(
            name=body.name,
            host=body.host,
            port=body.port,
            dbname=body.dbname,
            username=body.username,
            password=body.password,
            sslmode=body.sslmode,
        )
    except Exception as exc:
        raise _map_error(exc) from exc


@router.get("/sql-connections")
async def list_sql_connections(request: Request):
    """List saved connections (never includes passwords)."""
    try:
        return {"connections": _service.list_connections()}
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/sql-connections/{conn_id}/test")
@limiter.limit("30/minute")
async def test_sql_connection(conn_id: str, request: Request):
    """Open and immediately close a read-only connection to verify it works."""
    try:
        return await _service.test_connection(conn_id)
    except Exception as exc:
        raise _map_error(exc) from exc


@router.delete("/sql-connections/{conn_id}", status_code=204)
async def delete_sql_connection(conn_id: str, request: Request):
    """Delete a saved connection. Admin only."""
    _require_admin(request)
    try:
        if not _service.delete_connection(conn_id):
            raise HTTPException(status_code=404, detail="Connection not found")
    except HTTPException:
        raise
    except Exception as exc:
        raise _map_error(exc) from exc


@router.post("/process/sql/inspect")
@limiter.limit("30/minute")
async def inspect_sql(body: SqlInspectRequest, request: Request):
    """Reflect a connection's schema for the column explorer (no data leaves as-is).

    Returns ``{schema, tables: [{name, row_estimate, columns: [{name, data_type,
    samples, recommended_action}]}]}``.
    """
    try:
        return await _service.inspect(body.connection_id, body.schema_name)
    except Exception as exc:
        raise _map_error(exc) from exc
