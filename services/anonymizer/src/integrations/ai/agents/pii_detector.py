"""PII Leak Detector agent.

Enhances the existing scoring text_risk evaluator with contextual LLM
analysis of free-text fields in de-identified FHIR resources.

PHI Safety: This agent MUST use a LOCAL/self-hosted model because it receives
de-identified text that may contain residual PII. The model string is forced
to MEDANON_AI_PII_PROVIDER regardless of the global MEDANON_AI_PROVIDER.
"""

import logging
import os
import re

_log = logging.getLogger("medanon.ai.pii_detector")

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
be scrubbed. You are looking for LEAKS — PII that slipped through.

Categories of PII to detect:
1. Patient names (full, partial, or nicknames)
2. Provider/practitioner names
3. Phone numbers, fax numbers
4. Email addresses
5. Physical addresses (street, city — state/country alone are OK)
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


def _extract_text_fields(resource: dict) -> list[tuple[str, str]]:
    """Extract (field_path, text_value) tuples from a FHIR resource."""
    results: list[tuple[str, str]] = []
    rtype = resource.get("resourceType", "")

    def _walk(obj: object, path: str, depth: int = 0) -> None:
        if depth > 12:
            return
        if isinstance(obj, str) and len(obj) >= 15:
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


def detect_pii_leaks(
    resources: list[dict],
    *,
    use_ai: bool = True,
) -> dict:
    """Scan de-identified resources for residual PII.

    Combines three detection layers:
    1. Regex patterns (fast, no model needed)
    2. NER scan (Presidio — when available)
    3. LLM contextual analysis (when AI enabled + local model available)

    Returns: {
        "detections": [...],
        "summary": {"total": N, "critical": N, "high": N, "medium": N},
        "layers_used": ["regex", "ner", "ai"],
    }
    """
    all_detections: list[dict] = []
    layers_used: list[str] = ["regex"]

    for resource in resources:
        text_fields = _extract_text_fields(resource)
        resource_id = resource.get("id", "unknown")
        resource_type = resource.get("resourceType", "unknown")

        # Layer 1: Regex patterns
        for field_path, text in text_fields:
            for name, pattern in PII_PATTERNS.items():
                for match in pattern.finditer(text):
                    all_detections.append({
                        "resource_id": resource_id,
                        "resource_type": resource_type,
                        "field_path": field_path,
                        "type": name,
                        "evidence": match.group()[:30],
                        "confidence": 0.9,
                        "severity": _SEVERITY_MAP.get(name, "medium"),
                        "source": "regex",
                    })

        # Layer 2: NER scan (existing Presidio adapter)
        try:
            from pipeline.deidentify import _get_nlp_adapter

            adapter = _get_nlp_adapter()
            if adapter is not None:
                if "ner" not in layers_used:
                    layers_used.append("ner")
                for field_path, text in text_fields:
                    try:
                        hits = adapter.detect(
                            text, entities=[], threshold=0.5, language="en",
                        )
                        for hit in hits:
                            etype = (
                                hit[2]
                                if isinstance(hit, tuple) and len(hit) >= 3
                                else "UNKNOWN"
                            )
                            all_detections.append({
                                "resource_id": resource_id,
                                "resource_type": resource_type,
                                "field_path": field_path,
                                "type": etype.lower(),
                                "evidence": f"NER entity: {etype}",
                                "confidence": 0.7,
                                "severity": _SEVERITY_MAP.get(etype, "medium"),
                                "source": "ner",
                            })
                    except Exception:
                        pass
        except ImportError:
            pass

        # Layer 3: LLM contextual analysis
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
    }


def detect_pii_fast(resources: list[dict]) -> list[dict]:
    """Fast PII scan using regex + NER only (no LLM). For the blocking gate."""
    result = detect_pii_leaks(resources, use_ai=False)
    return result["detections"]


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

    try:
        provider = get_provider()
    except NotAvailableError:
        return []

    field_texts = "\n".join(
        f"[{path}]: {text[:500]}" for path, text in text_fields[:20]
    )

    try:
        import json

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
            temperature=0.0,
            max_tokens=2048,
        )
        detections = json.loads(response)
        if isinstance(detections, list):
            for d in detections:
                d["source"] = "ai"
            return detections
    except (ProviderUnavailableError, Exception) as exc:
        _log.debug("ai_pii_scan_failed: %s", exc)
    return []
