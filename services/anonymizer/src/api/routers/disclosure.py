"""Disclosure-control endpoint: /export/decision (D7.2 §5.4 Fig 6, Five Safes).

Output checking for an SPE export. JSON body::

    {
      "resources":      [ ...FHIR resource dicts... ],   # required
      "synthetic":      [ ... ],                          # optional
      "declared_paths": [ "Patient.birthDate", ... ],     # optional (purpose)
      "thresholds":     { "min_k": 5 }                    # optional
      "permit_id":      "permit-abc123",                  # optional (D7.2 §4.4/§4.8)
      "recipient":      "hospital-x"                      # optional — checked
                                                            # against the permit
    }

When ``permit_id`` is supplied, the release is additionally bound to that
permit (WS4/WS8 permit-scoping, rules R6-R8 in
:func:`pipeline.disclosure.assess_export_decision`): the permit must exist
(422 if not), and its active/recipient/scope status feeds the decision
record's rule-level rationale rather than short-circuiting as a bare HTTP
error — a REVOKED or expired permit still produces a documented REFUSE, which
is what a Transformation Passport needs to show.

Returns an export-decision record (approve / refer / refuse) with the rule-level
rationale, suitable for embedding in a Transformation Passport.
"""

import json
import logging

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from api.deps import MAX_BODY_BYTES, limiter

router = APIRouter()
logger = logging.getLogger("medanon")


def _resolve_permit_for_decision(permit_id: str | None):
    """Look up *permit_id* (existence only — inactive permits are returned,
    not rejected, so the disclosure engine can report R6/R7/R8 in the
    decision record itself)."""
    if not permit_id:
        return None
    from api.services.permits import PermitNotFoundError, PermitService

    try:
        return PermitService().get(permit_id)
    except PermitNotFoundError:
        raise HTTPException(
            status_code=422, detail=f"permit_id {permit_id!r} does not exist"
        )


@router.post("/export/decision")
@limiter.limit("30/minute")
async def export_decision(request: Request):
    """Assess a Statistical Disclosure Control export decision."""
    body = await request.body()
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Request body exceeds the {MAX_BODY_BYTES // (1024 * 1024)} MB limit",
        )
    try:
        payload = json.loads(body) if body else {}
        if not isinstance(payload, dict):
            raise ValueError("body must be a JSON object")
        resources = payload.get("resources") or []
        synthetic = payload.get("synthetic")
        declared_paths = payload.get("declared_paths")
        thresholds = payload.get("thresholds")
        permit_id = payload.get("permit_id")
        recipient = payload.get("recipient")
    except Exception as exc:
        raise HTTPException(
            status_code=422, detail=f"Could not parse input: {exc}"
        ) from exc

    permit = _resolve_permit_for_decision(permit_id)

    auth = getattr(request.state, "auth", None)
    actor = auth.subject if auth is not None else "system"
    try:
        from pipeline.disclosure import assess_export_decision

        record = assess_export_decision(
            resources,
            synthetic=synthetic,
            declared_paths=declared_paths,
            thresholds=thresholds,
            permit=permit,
            recipient=recipient,
            decided_by=str(actor),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.error("export_decision error: %s", type(exc).__name__, exc_info=False)
        raise HTTPException(status_code=500, detail="Export-decision error") from exc

    return JSONResponse(content=record)
