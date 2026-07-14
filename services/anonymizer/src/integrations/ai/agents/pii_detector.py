"""PII Leak Detector agent.

Enhances the existing scoring text_risk evaluator with contextual LLM
analysis of free-text fields in de-identified FHIR resources.

PHI Safety: This agent MUST use a LOCAL/self-hosted model because it receives
de-identified text that may contain residual PII. The model string is forced
to MEDANON_AI_PII_PROVIDER regardless of the global MEDANON_AI_PROVIDER.

Local-only enforcement (C4): when MEDANON_AI_PII_REQUIRE_LOCAL is true
(the default) the PII model is verified to resolve to a loopback/private
endpoint before any de-identified text is sent. If it cannot be proven local
the AI layer is skipped (fail-closed) so residual PHI never reaches an
external LLM.
"""

import logging
import os
import re

from integrations.ai.local_guard import (  # noqa: F401  (re-exported for compat)
    PiiModelNotLocalError,
    _host_is_local,
    _local_model_prefixes,
    _pii_require_local,
    assert_endpoint_local,
)

_log = logging.getLogger("medanon.ai.pii_detector")


def _extract_json_array(text: str) -> list:
    """Parse a JSON array from an LLM response.

    Chat models (MedGemma, gemma, etc.) commonly wrap JSON in ```json fences
    and add prose despite instructions. Try a direct parse first, then a
    fenced block, then the first bracketed ``[...]`` span. Returns [] if no
    valid array is found rather than raising.
    """
    import json

    for candidate in _json_candidates(text):
        try:
            parsed = json.loads(candidate)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(parsed, list):
            return parsed
    return []


def _json_candidates(text: str):
    text = text.strip()
    yield text
    fence = re.search(r"```(?:json)?\s*\n?(.*?)```", text, re.DOTALL)
    if fence:
        yield fence.group(1).strip()
    span = re.search(r"\[.*\]", text, re.DOTALL)
    if span:
        yield span.group(0)


def _assert_pii_model_is_local(model: str, api_base: str | None) -> None:
    """Enforce the local-only PII model contract (C4).

    No-op when MEDANON_AI_PII_REQUIRE_LOCAL is disabled. Otherwise raises
    PiiModelNotLocalError unless the endpoint is provably local  either an
    explicit local ``api_base`` or a recognised local provider prefix. The
    caller MUST treat a raise as fail-closed (do not send PHI).

    The actual check lives in ``integrations.ai.local_guard`` and is ALSO
    enforced unconditionally inside ``LLMProvider`` for ``phi_payload=True``
    calls  this wrapper exists for the early caller-side check (precise
    error before any work) and for backward compatibility.
    """
    if not _pii_require_local():
        return
    assert_endpoint_local(model, api_base)


def pii_enforcement_status() -> dict:
    """Report the C4 local-only PII enforcement posture for /v1/ai/status.

    Returns the configured PII model/endpoint, whether enforcement is active,
    and whether the resolved endpoint is provably local (loopback/private).
    Performs no PHI processing  safe to call from a status handler.
    """
    model = os.environ.get("MEDANON_AI_PII_PROVIDER", "").strip()
    api_base = (
        os.environ.get("MEDANON_AI_PII_API_BASE", "").strip()
        or os.environ.get("MEDANON_AI_API_BASE", "").strip()
        or None
    )
    require_local = _pii_require_local()
    configured = bool(model)
    local_verified = False
    detail = ""
    if not configured:
        detail = "AI PII scan disabled (MEDANON_AI_PII_PROVIDER unset)."
    else:
        try:
            _assert_pii_model_is_local(model, api_base)
            local_verified = True
            detail = (
                "PII model endpoint verified local/self-hosted."
                if require_local
                else "Local-only enforcement disabled (opt-out)."
            )
        except PiiModelNotLocalError as exc:
            local_verified = False
            detail = str(exc)
    return {
        "configured": configured,
        "model": model,
        "api_base": api_base or "",
        "require_local": require_local,
        "local_verified": local_verified,
        "detail": detail,
    }


# Regex patterns matching pipeline/scoring/privacy.py:_PII_PATTERNS
PII_PATTERNS: dict[str, re.Pattern] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(
        r"\b(?:\+?1[\s-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b",
    ),
    "email": re.compile(
        r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b",
    ),
    "date_iso": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    "ip": re.compile(
        r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}"
        r"(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
    ),
    "mrn": re.compile(r"\b(?:MRN|mrn)[:\s#]?\d{4,}\b"),
}

_SEVERITY_MAP: dict[str, str] = {
    "ssn": "critical",
    "mrn": "critical",
    "PERSON": "critical",
    "US_SSN": "critical",
    "phone": "high",
    "email": "high",
    "PHONE_NUMBER": "high",
    "EMAIL_ADDRESS": "high",
    "LOCATION": "high",
    "date_iso": "medium",
    "ip": "medium",
}

_PII_SYSTEM_PROMPT = """\
You are a medical data privacy auditor. Your task is to scan de-identified \
FHIR resource text fields for residual PII that was NOT properly removed.

IMPORTANT: You are receiving DE-IDENTIFIED data. Most fields should already \
be scrubbed. You are looking for LEAKS  PII that slipped through.

Categories of PII to detect:
1. Patient names (full, partial, or nicknames)
2. Provider/practitioner names
3. Phone numbers, fax numbers
4. Email addresses
5. Physical addresses (street, city  state/country alone are OK)
6. Social Security Numbers or national IDs
7. Medical Record Numbers (MRN)
8. Dates more specific than year (month-day, exact dates)
9. IP addresses
10. Device serial numbers
11. Account numbers

For each detection, output a JSON object with:
- "field_path": the FHIR field path where found
- "type": PII category (name, phone, email, address, ssn, mrn, date, ip, other)
- "evidence": brief description of what was found (max 30 chars)
- "confidence": float 0.0-1.0
- "severity": "critical" (names, SSN, MRN) / "high" (phone, email, address) \
/ "medium" (dates, IP)

Output ONLY a JSON array of detections. If no PII found, output [].
"""


def _extract_text_fields(resource: dict, min_len: int = 15) -> list[tuple[str, str]]:
    """Extract (field_path, text_value) tuples from a FHIR resource.

    ``min_len`` is the shortest string value that is scanned. The default (15)
    targets free-text narrative; the Resource Explorer lowers it (``min_len=1``)
    to scan EVERY string field, so short direct identifiers that live in
    structured fields  an SSN, a phone number, a ``name.family``  are seen by
    regex/NER too, not just prose.
    """
    results: list[tuple[str, str]] = []
    rtype = resource.get("resourceType", "")

    def _walk(obj: object, path: str, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(obj, str) and len(obj) >= min_len:
            results.append((path, obj))
        elif isinstance(obj, dict):
            for k, v in obj.items():
                if k in ("meta", "resourceType"):
                    continue
                _walk(v, f"{path}.{k}" if path else k, depth + 1)
        elif isinstance(obj, list):
            for i, item in enumerate(obj):
                _walk(item, f"{path}[{i}]", depth + 1)

    _walk(resource, rtype)
    return results


def _regex_detections(
    resource_id: str,
    resource_type: str,
    text_fields: list[tuple[str, str]],
) -> list[dict]:
    """Layer 1: deterministic pattern match over the resource's free text."""
    out: list[dict] = []
    for field_path, text in text_fields:
        for name, pattern in PII_PATTERNS.items():
            for match in pattern.finditer(text):
                out.append(
                    {
                        "resource_id": resource_id,
                        "resource_type": resource_type,
                        "field_path": field_path,
                        "type": name,
                        "evidence": match.group()[:30],
                        "confidence": 0.9,
                        "severity": _SEVERITY_MAP.get(name, "medium"),
                        "source": "regex",
                    }
                )
    return out


def _ner_detections(adapter, indexed: list[tuple[str, str, str, str]]) -> list[dict]:
    """Layer 2: one batched NER call for every text field across every resource.

    *indexed* is ``[(resource_id, resource_type, field_path, text), ...]``.

    Previously this issued ``adapter.detect(text)`` once per field per resource,
    serially  with ``NLP_SERVICE_URL`` set (always, in the shipped compose
    stack) that is one HTTP round-trip per field.  ``detect_batch`` collapses
    them into one request per chunk.

    Raises whatever the adapter raises.  The caller decides how to record the
    degradation; it must not be swallowed, or the gate silently drops a layer
    while still reporting that it ran.
    """
    if not indexed:
        return []
    hits_per_text = adapter.detect_batch(
        [text for _, _, _, text in indexed],
        [],  # entities: all
        0.5,  # threshold
        "en",  # language
    )
    if len(hits_per_text) != len(indexed):
        raise ValueError(
            f"detect_batch returned {len(hits_per_text)} results "
            f"for {len(indexed)} texts"
        )

    out: list[dict] = []
    for (resource_id, resource_type, field_path, _text), hits in zip(
        indexed, hits_per_text
    ):
        for hit in hits:
            etype = (
                hit[2]
                if isinstance(hit, (tuple, list)) and len(hit) >= 3
                else "UNKNOWN"
            )
            out.append(
                {
                    "resource_id": resource_id,
                    "resource_type": resource_type,
                    "field_path": field_path,
                    "type": etype.lower(),
                    "evidence": f"NER entity: {etype}",
                    "confidence": 0.7,
                    "severity": _SEVERITY_MAP.get(etype, "medium"),
                    "source": "ner",
                }
            )
    return out


def detect_pii_leaks(
    resources: list[dict],
    *,
    use_ai: bool = True,
    min_len: int = 15,
) -> dict:
    """Scan de-identified resources for residual PII in string content.

    By default this is a **free-text** content scanner: ``_extract_text_fields``
    only walks strings of >= 15 characters, so a leaked ``name.family`` or
    ``identifier.value`` in a structured field is invisible to it  those are the
    job of the structural coverage check in :mod:`pipeline.identifier_gate`.

    Lower ``min_len`` (the Resource Explorer passes ``min_len=1``) to scan EVERY
    string field, so regex/NER also see short structured identifiers. Recall
    rises at the cost of more NER noise; callers filter by severity.

    Three layers:
    1. Regex patterns (deterministic, no model needed)
    2. NER scan (Presidio, batched  when the adapter is available)
    3. LLM contextual analysis (when AI enabled + local model available)

    Returns: {
        "detections": [...],
        "summary": {"total": N, "critical": N, "high": N, "medium": N},
        "layers_used": ["regex", "ner", "ai"],
        "degraded": [<layer that was requested but failed>, ...],
    }

    ``layers_used`` lists layers that actually produced a result.  A layer that
    was attempted and failed appears in ``degraded`` instead  it previously
    appeared in ``layers_used`` regardless, so an NLP outage silently reduced
    the scan to regex while still reporting that NER had run.
    """
    all_detections: list[dict] = []
    layers_used: list[str] = ["regex"]
    degraded: list[str] = []

    # Layer 1 + text extraction, and the flat index the batched NER layer needs.
    ner_index: list[tuple[str, str, str, str]] = []
    for resource in resources:
        text_fields = _extract_text_fields(resource, min_len=min_len)
        resource_id = resource.get("id", "unknown")
        resource_type = resource.get("resourceType", "unknown")

        all_detections.extend(
            _regex_detections(resource_id, resource_type, text_fields)
        )
        ner_index.extend(
            (resource_id, resource_type, field_path, text)
            for field_path, text in text_fields
        )

        # Layer 3: LLM contextual analysis (per resource  needs the whole doc)
        if use_ai and text_fields:
            try:
                ai_hits = _ai_scan_resource(resource, text_fields)
                for d in ai_hits:
                    d["resource_id"] = resource_id
                    d["resource_type"] = resource_type
                all_detections.extend(ai_hits)
                if "ai" not in layers_used:
                    layers_used.append("ai")
            except Exception as exc:
                _log.debug("ai_pii_scan_skipped: %s", exc)

    # Layer 2: NER, one batched call for the whole chunk.
    adapter = None
    try:
        from integrations.nlp.adapter import _get_nlp_adapter

        adapter = _get_nlp_adapter()
    except ImportError:
        pass
    if adapter is not None and ner_index:
        try:
            all_detections.extend(_ner_detections(adapter, ner_index))
            layers_used.append("ner")
        except Exception as exc:
            degraded.append("ner")
            _log.error(
                "pii_gate_ner_layer_failed error_type=%s  scan degraded to regex only",
                type(exc).__name__,
            )

    # Deduplicate by (field_path, type, evidence prefix)
    seen: set[tuple[str, str, str]] = set()
    unique: list[dict] = []
    for d in all_detections:
        key = (d["field_path"], d["type"], d.get("evidence", "")[:20])
        if key not in seen:
            seen.add(key)
            unique.append(d)

    summary = {
        "total": len(unique),
        "critical": sum(1 for d in unique if d["severity"] == "critical"),
        "high": sum(1 for d in unique if d["severity"] == "high"),
        "medium": sum(1 for d in unique if d["severity"] == "medium"),
    }

    return {
        "detections": unique,
        "summary": summary,
        "layers_used": layers_used,
        "degraded": degraded,
    }


def detect_pii_fast(resources: list[dict]) -> list[dict]:
    """Fast PII scan using regex + NER only (no LLM). For the blocking gate."""
    result = detect_pii_leaks(resources, use_ai=False)
    return result["detections"]


# Severities the output gate hard-blocks on. The default blocks both
# ``critical`` (names, SSN, MRN) *and* ``high`` (phone, email, street address):
# all five are HIPAA Safe Harbor direct identifiers (45 CFR 164.514(b)(2) items
# B/C/L and the name/SSN/MRN items), so a residual one in de-identified output
# is a reportable leak, not a warning. ``medium`` (bare ISO date, IP) is left
# out by default because full-precision dates are legitimately retained by
# date-shift pipelines (shifting preserves intervals at day precision); blocking
# every ISO date would false-positive on intended output. Override with
# ``MEDANON_PII_GATE_BLOCK_SEVERITY`` (comma-separated, e.g. ``critical,high,medium``
# for strict Safe Harbor, or ``critical`` for the legacy names/SSN/MRN-only gate).
_DEFAULT_BLOCK_SEVERITIES: tuple[str, ...] = ("critical", "high")


def block_severities() -> frozenset[str]:
    """Severities the output gate blocks on, read at call time (test-toggleable)."""
    raw = os.environ.get("MEDANON_PII_GATE_BLOCK_SEVERITY", "").strip()
    if not raw:
        return frozenset(_DEFAULT_BLOCK_SEVERITIES)
    return frozenset(s.strip().lower() for s in raw.split(",") if s.strip())


def blocking_detections(detections: list[dict]) -> list[dict]:
    """Filter *detections* to those whose severity is in the blocking set."""
    block = block_severities()
    return [d for d in detections if d.get("severity") in block]


def _ai_scan_resource(
    resource: dict,
    text_fields: list[tuple[str, str]],
) -> list[dict]:
    """Use local LLM to scan text fields for contextual PII."""
    from integrations.ai.provider import (
        NotAvailableError,
        ProviderUnavailableError,
        get_provider,
    )

    pii_model = os.environ.get("MEDANON_AI_PII_PROVIDER", "").strip()
    if not pii_model:
        return []

    # Pin the PII scan to a dedicated self-hosted endpoint. When
    # MEDANON_AI_PII_API_BASE is unset, fall back to the global
    # MEDANON_AI_API_BASE (e.g. the Ollama VM) rather than letting litellm
    # default to localhost  otherwise a model_override silently routes to
    # 127.0.0.1 on THIS host instead of the configured remote VM.
    pii_api_base = (
        os.environ.get("MEDANON_AI_PII_API_BASE", "").strip()
        or os.environ.get("MEDANON_AI_API_BASE", "").strip()
        or None
    )

    # C4: refuse to ship de-identified text to a non-local LLM. Fail-closed
    # skip the AI layer rather than risk PHI exfiltration.
    try:
        _assert_pii_model_is_local(pii_model, pii_api_base)
    except PiiModelNotLocalError as exc:
        _log.error("ai_pii_scan_blocked_non_local_model: %s", exc)
        return []

    try:
        provider = get_provider()
    except NotAvailableError:
        return []

    field_texts = "\n".join(
        f"[{path}]: {text[:500]}" for path, text in text_fields[:20]
    )

    try:
        response = provider.complete(
            messages=[
                {"role": "system", "content": _PII_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": (
                        "Scan these de-identified text fields for residual PII:"
                        f"\n\n{field_texts}"
                    ),
                },
            ],
            model_override=pii_model,
            api_base_override=pii_api_base,
            temperature=0.0,
            max_tokens=2048,
            # De-identified text may carry residual PHI  the provider
            # re-enforces endpoint locality (defense in depth vs the early
            # _assert_pii_model_is_local check above).
            phi_payload=True,
        )
        detections = _extract_json_array(response)
        if detections:
            for d in detections:
                if isinstance(d, dict):
                    d["source"] = "ai"
            return [d for d in detections if isinstance(d, dict)]
        _log.info("ai_pii_scan_no_parseable_json len=%d", len(response or ""))
    except (ProviderUnavailableError, Exception) as exc:
        _log.debug("ai_pii_scan_failed: %s", exc)
    return []
