"""gPAS cache coherence canary probe.

At startup, pseudonymizes a well-known canary value via gPAS.  If the
returned pseudonym differs from the one stored in Redis, the gPAS DB
has been wiped and all cached pseudonyms are stale — flush immediately.
"""

from __future__ import annotations

import logging
import os

_log = logging.getLogger("medanon.gpas.canary")

CANARY_ORIGINAL = "__medanon_cache_probe__"
CANARY_KEY_PREFIX = "medanon:gpas:canary:"


def check_gpas_cache_coherence(redis_url: str | None = None) -> dict:
    """Run the canary probe and flush caches if staleness is detected.

    Returns a dict with:
        checked: bool   — whether the probe ran
        flushed: bool   — whether caches were flushed
        reason: str     — human-readable explanation
    """
    if os.environ.get("MEDANON_GPAS_CANARY_ENABLED", "true").lower() in (
        "false", "0", "no",
    ):
        return {"checked": False, "flushed": False, "reason": "canary disabled via env"}

    gpas_url = os.environ.get("GPAS_URL", "").strip()
    domain = os.environ.get("GPAS_DOMAIN", "").strip()

    if not gpas_url or not domain:
        return {"checked": False, "flushed": False, "reason": "gPAS not configured"}

    if not redis_url:
        redis_url = os.environ.get("MEDANON_REDIS_URL", "").strip()
    if not redis_url:
        return {"checked": False, "flushed": False, "reason": "Redis not configured"}

    try:
        return _run_canary_probe(redis_url, gpas_url, domain)
    except Exception as exc:
        _log.warning("gpas_canary_probe_failed: %s", exc)
        return {"checked": False, "flushed": False, "reason": f"probe failed: {exc}"}


def _run_canary_probe(redis_url: str, gpas_url: str, domain: str) -> dict:
    import redis as _redis

    from .transport import _call_gpas_operation, _resolve_gpas_base
    from .protocol import _build_pseudonymize_params, _parse_pseudonymize_response

    # 1. Pseudonymize the canary value via gPAS
    base_url = _resolve_gpas_base({"gpas_url": gpas_url})
    params = {
        "gpas_url": gpas_url,
        "gpas_timeout_sec": 10,
        "gpas_retry_count": 1,
    }
    fhir_request = _build_pseudonymize_params(domain, [CANARY_ORIGINAL])
    resp_json = _call_gpas_operation(
        base_url, "pseudonymizeAllowCreate", fhir_request, params,
    )
    mapping = _parse_pseudonymize_response(resp_json)
    current_pseudonym = mapping.get(CANARY_ORIGINAL)
    if not current_pseudonym:
        return {"checked": False, "flushed": False, "reason": "canary not returned by gPAS"}

    # 2. Compare with stored canary in Redis
    canary_key = CANARY_KEY_PREFIX + domain
    client = _redis.StrictRedis.from_url(
        redis_url, decode_responses=True, socket_timeout=5, socket_connect_timeout=2,
    )
    stored_pseudonym = client.get(canary_key)

    if stored_pseudonym is None:
        # Fresh Redis or first run — store canary, no flush needed
        client.set(canary_key, current_pseudonym)
        _log.info("gpas_canary_stored domain=%s (first run)", domain)
        return {"checked": True, "flushed": False, "reason": "canary stored (first run)"}

    if stored_pseudonym == current_pseudonym:
        _log.info("gpas_canary_ok domain=%s (cache coherent)", domain)
        return {"checked": True, "flushed": False, "reason": "cache coherent"}

    # 3. Staleness detected — flush all caches
    _log.warning(
        "gpas_canary_mismatch domain=%s stored=%s current=%s — flushing caches",
        domain, stored_pseudonym, current_pseudonym,
    )
    from utils.cache import flush_cache

    flushed_count = flush_cache()

    # Update canary to new value
    client.set(canary_key, current_pseudonym)

    _log.warning("gpas_cache_flushed count=%d after DB wipe detection", flushed_count)
    return {
        "checked": True,
        "flushed": True,
        "flushed_count": flushed_count,
        "reason": "stale cache detected and flushed",
    }
