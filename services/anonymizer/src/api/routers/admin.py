"""Admin operations  cache management and diagnostics.

POST /v1/admin/cache/flush      flush all gPAS cache entries (admin role)
GET  /v1/admin/cache/canary     cache coherence status (admin role)
"""

import logging
import os

from fastapi import APIRouter, HTTPException, Request

from api.auth import AuthContext

router = APIRouter(tags=["admin"])
logger = logging.getLogger("medanon")


def _require_admin(request: Request) -> None:
    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin role required")


@router.post("/admin/cache/flush")
def flush_cache_endpoint(request: Request):
    """Flush all gPAS pseudonym cache entries (L1 local + L2 Redis)."""
    _require_admin(request)
    from utils.cache import flush_cache

    count = flush_cache()
    logger.warning(
        "admin_cache_flush requested_by=%s flushed=%d",
        getattr(getattr(request.state, "auth", None), "subject", "unknown"),
        count,
    )
    return {"flushed": True, "count": count}


@router.get("/admin/cache/canary")
def cache_canary_status(request: Request):
    """Run the gPAS canary probe and report cache coherence status.

    Does NOT automatically flush  use POST /admin/cache/flush for that.
    """
    _require_admin(request)
    redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    if not redis_url:
        return {"checked": False, "reason": "Redis not configured"}

    try:
        from integrations.gpas.canary import CANARY_ORIGINAL, CANARY_KEY_PREFIX
        from integrations.gpas.transport import _resolve_gpas_base, _call_gpas_operation
        from integrations.gpas.protocol import (
            _build_pseudonymize_params,
            _parse_pseudonymize_response,
        )

        domain = os.environ.get("GPAS_DOMAIN", "").strip()
        gpas_url = os.environ.get("GPAS_URL", "").strip()
        if not domain or not gpas_url:
            return {"checked": False, "reason": "gPAS not configured"}

        base_url = _resolve_gpas_base({"gpas_url": gpas_url})
        fhir_request = _build_pseudonymize_params(domain, [CANARY_ORIGINAL])
        resp_json = _call_gpas_operation(
            base_url,
            "pseudonymizeAllowCreate",
            fhir_request,
            {"gpas_url": gpas_url, "gpas_timeout_sec": 10, "gpas_retry_count": 1},
        )
        mapping = _parse_pseudonymize_response(resp_json)
        current = mapping.get(CANARY_ORIGINAL)

        from utils.redis_pool import get_redis

        client = get_redis(redis_url, decode_responses=True)
        if client is None:
            return {"checked": False, "reason": "Redis client unavailable"}
        stored = client.get(CANARY_KEY_PREFIX + domain)

        coherent = stored is not None and stored == current
        # Do not expose pseudonym values (stored/current)  they are PHI-adjacent
        # secrets that should not appear in API responses, logs, or browser history.
        return {
            "checked": True,
            "coherent": coherent,
            "domain": domain,
        }
    except Exception as exc:
        logger.error("cache_canary_check_failed: %s", exc, exc_info=False)
        raise HTTPException(
            status_code=500, detail=f"Canary check failed: {exc}"
        ) from exc
