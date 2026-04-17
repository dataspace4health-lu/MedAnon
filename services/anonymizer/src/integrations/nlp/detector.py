"""NLP-based PHI/PII detection and tokenization using Microsoft Presidio.

Combines NER (spaCy), Presidio built-in pattern recognizers, and custom
``PatternRecognizer`` instances for comprehensive PII detection covering
HIPAA Safe Harbor, GDPR Art. 4(5) & Art. 9, and EU national identifiers.

**Built-in Presidio recognizers** handle:
  PERSON (NER), DATE_TIME, LOCATION (NER), PHONE_NUMBER, EMAIL_ADDRESS,
  US_SSN, US_PASSPORT, US_DRIVER_LICENSE, MEDICAL_LICENSE, AGE, NRP,
  CREDIT_CARD, IBAN_CODE, US_BANK_NUMBER, US_ITIN, IP_ADDRESS, URL

**Custom recognizers** (registered at engine init) extend coverage to:
  STREET_ADDRESS (US + EU + PO Box), INTL_PHONE, FAX_NUMBER,
  EU national IDs (UK_NINO, FR_NIR, DE_SVNR, NL_BSN, IT_CF, ES_DNI,
  CH_AHV, BE_NN), healthcare IDs (MRN, UK_NHS, DE_KVNR, EU_EHIC),
  postcodes (UK_POSTCODE, EU_POSTCODE), labeled markers (DOB_MARKER,
  AGE_MARKER, NATIONAL_ID_LABEL, ACCOUNT_LABEL, LICENSE_PLATE),
  GDPR Art. 9 labels (NATIONALITY_LABEL, RELIGION_LABEL,
  POLITICAL_LABEL, ETHNICITY_LABEL), EU_DATE, EU_DATE_WRITTEN,
  SYNTHEA_SEED.

Key parameters (``params``):
    entities    list | 'all' | 'healthcare'  (default: 'healthcare')
    threshold   float  (default: 0.4)
    language    str    (default: 'en')
    mode        'tokenize' | 'redact'        (default: 'tokenize')
    html        bool   (default: False)

Implementation split:
    detector_recognizers  — entity catalogue, Presidio engine singleton, custom recognizers
    detector_tokenizer    — token state, detection cache, span replacement, XHTML walker
"""

from __future__ import annotations

import logging

from utils.fhirpath import find_nodes

# Re-export public symbols so callers importing from this module are unaffected.
from integrations.nlp.detector_recognizers import (  # noqa: F401
    HEALTHCARE_ENTITIES,
    _get_analyzer,
)
from integrations.nlp.detector_tokenizer import (  # noqa: F401
    _GLOBAL_TOKEN_LOCK,
    _GLOBAL_TOKEN_STATE,
    _analyze_and_replace,
    _scrub_xhtml_text_nodes,
    reset_detection_cache,
    reset_global_token_state,
)

log = logging.getLogger("medanon.nlp")


# ---------------------------------------------------------------------------
# Parameter helpers
# ---------------------------------------------------------------------------


def _resolve_entities(param) -> list:
    if param in (None, "healthcare"):
        return list(HEALTHCARE_ENTITIES)
    if param == "all":
        return list(HEALTHCARE_ENTITIES)
    if isinstance(param, str):
        return [e.strip() for e in param.split(",")]
    return list(param)


# ---------------------------------------------------------------------------
# Public action entry point
# ---------------------------------------------------------------------------


def nlp_scrub_by_path(resource: dict, el: dict, params: dict) -> None:
    """Scrub PHI/PII from the field matched by *el* using NLP.

    Modifies *resource* in-place (same contract as all other actions).

    When NLP_SERVICE_URL is set, delegates to the remote NLP microservice.
    Otherwise runs Presidio locally.
    """
    import os

    entities = _resolve_entities(params.get("entities", "healthcare"))
    threshold = float(params.get("threshold", 0.4))
    language = str(params.get("language", "en"))
    mode = str(params.get("mode", "tokenize"))
    use_html = bool(params.get("html", False))
    token_state = params.get("_token_state") or {"next": {}, "map": {}, "reverse": {}}
    token_lock = _GLOBAL_TOKEN_LOCK if token_state is _GLOBAL_TOKEN_STATE else None

    if os.environ.get("NLP_SERVICE_URL", ""):
        from integrations.nlp.remote_detector import analyze_and_replace_remote

        def scrub_fn(text: str) -> str:
            return analyze_and_replace_remote(
                text, entities, threshold, language, mode, token_state
            )
    else:

        def scrub_fn(text: str) -> str:
            return _analyze_and_replace(
                text, entities, threshold, language, mode, token_state, token_lock
            )

    path = el["path"]
    parts = path.split(".")
    if len(parts) < 2:
        return

    key = parts[-1]
    parent_path = parts[1:-1]

    try:
        nodes = find_nodes(resource, parent_path, [])
    except Exception:
        log.error(
            "nlp_find_nodes_failed path=%s — redacting field as safety fallback (detector.py)",
            path,
        )
        from actions.redact import redact_by_path

        redact_by_path(resource, el, {})
        return

    def _apply(node, field):
        if isinstance(node, list):
            for item in node:
                _apply(item, field)
            return
        if not isinstance(node, dict) or field not in node:
            return
        current = node[field]
        if use_html:
            if isinstance(current, dict) and isinstance(current.get("div"), str):
                try:
                    current["div"] = _scrub_xhtml_text_nodes(current["div"], scrub_fn)
                except Exception:
                    log.error(
                        "nlp_scrub_failed path=%s.%s — redacting field", path, field
                    )
                    current["div"] = "[REDACTED]"
            elif isinstance(current, str):
                try:
                    node[field] = _scrub_xhtml_text_nodes(current, scrub_fn)
                except Exception:
                    log.error(
                        "nlp_scrub_failed path=%s.%s — redacting field", path, field
                    )
                    node[field] = "[REDACTED]"
        elif isinstance(current, str):
            try:
                node[field] = scrub_fn(current)
            except Exception:
                log.error("nlp_scrub_failed path=%s.%s — redacting field", path, field)
                node[field] = "[REDACTED]"
        elif isinstance(current, list):
            for i, v in enumerate(current):
                if isinstance(v, str):
                    try:
                        current[i] = scrub_fn(v)
                    except Exception:
                        log.error(
                            "nlp_scrub_failed path=%s.%s[%d] — redacting field",
                            path,
                            field,
                            i,
                        )
                        current[i] = "[REDACTED]"

    _apply(nodes, key)
