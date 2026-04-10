"""Audit event query endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Query

from utils import audit

router = APIRouter(tags=["audit"])


@router.get("/audit/events")
async def list_audit_events(
    count: int = Query(default=100, ge=1, le=1000),
    event_type: str | None = Query(default=None),
):
    """Return recent audit events from the centralized store."""
    events = audit.query(count=count, event_type=event_type)
    return {"events": events, "count": len(events)}
