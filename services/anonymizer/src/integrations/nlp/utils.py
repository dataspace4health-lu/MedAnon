"""NLP utilities — no Presidio/spaCy dependency.

Contains the entity catalogue, entity-spec resolver, surrogate-token helpers,
and the XHTML text-node walker used by the pipeline.

IMPORTANT — ENTITY CATALOGUE SYNC:
    This file is the canonical source for ``HEALTHCARE_ENTITIES``.
    The NLP service mirror is ``services/nlp/src/recognizers.py``.
    Any addition here MUST also be added to the NLP service recognizers list,
    and vice-versa, to prevent silent detection/validation mismatches.
"""

from __future__ import annotations

from html import escape
from html.parser import HTMLParser

# ---------------------------------------------------------------------------
# Entity catalogue
# ---------------------------------------------------------------------------

HEALTHCARE_ENTITIES: list[str] = [
    # --- Presidio built-in recognizers ---
    "PERSON",
    "DATE_TIME",
    "LOCATION",
    "PHONE_NUMBER",
    "EMAIL_ADDRESS",
    "US_SSN",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
    "MEDICAL_LICENSE",
    "AGE",
    "NRP",
    "CREDIT_CARD",
    "IBAN_CODE",
    "US_BANK_NUMBER",
    "US_ITIN",
    "IP_ADDRESS",
    "URL",
    # --- Custom recognizers (registered in the NLP service) ---
    "STREET_ADDRESS",
    "INTL_PHONE",
    "FAX_NUMBER",
    # EU national identifiers
    "UK_NINO",
    "FR_NIR",
    "DE_SVNR",
    "NL_BSN",
    "IT_CF",
    "ES_DNI",
    "CH_AHV",
    "BE_NN",
    # Healthcare IDs
    "MRN",
    "UK_NHS",
    "DE_KVNR",
    "EU_EHIC",
    # Postcodes
    "UK_POSTCODE",
    "EU_POSTCODE",
    # Labeled markers
    "DOB_MARKER",
    "AGE_MARKER",
    "NATIONAL_ID_LABEL",
    "ACCOUNT_LABEL",
    "LICENSE_PLATE",
    # GDPR Art.9 special categories (labeled)
    "NATIONALITY_LABEL",
    "RELIGION_LABEL",
    "POLITICAL_LABEL",
    "ETHNICITY_LABEL",
    # Date formats not covered by built-in DATE_TIME
    "EU_DATE",
    "EU_DATE_WRITTEN",
    # Synthetic data artifacts
    "SYNTHEA_SEED",
    # Clinical note PHI — inline demographic / administrative
    "RACE_ETHNICITY",      # bare inline race/ethnicity (not label-prefixed)
    "INSURANCE_STATUS",    # coverage status, named payers
    "GEO_COORDINATES",     # decimal/DMS lat-long pairs
    # Organizations (hospitals, clinics, payers) — relies on spaCy NER
    "ORGANIZATION",
    # Financial / fiscal identifiers
    "SWIFT_BIC",
    "EU_VAT",
    "US_ROUTING",
    "CRYPTO_WALLET",
    # HIPAA Safe Harbor extra identifiers (provider/device/vehicle/web)
    "US_DEA",
    "US_NPI",
    "MAC_ADDRESS",
    "IMEI",
    "VIN",
    "FDA_UDI",
    "UUID",
    "BEARER_TOKEN",
    "USERNAME_HANDLE",
    # GDPR Art.9 — genetic data
    "GENETIC_VARIANT",
]


# ---------------------------------------------------------------------------
# Entity-spec resolver
# ---------------------------------------------------------------------------


def _resolve_entities(param) -> list[str]:
    """Resolve the ``entities`` action param to a concrete list of entity types."""
    if param in (None, "healthcare", "all"):
        return list(HEALTHCARE_ENTITIES)
    if isinstance(param, str):
        return [e.strip() for e in param.split(",")]
    return list(param)


# ---------------------------------------------------------------------------
# Surrogate-token helpers  (used in ``tokenize`` scrubbing mode)
# ---------------------------------------------------------------------------

_TOKEN_STATE_MAX_ENTRIES = 100_000


def _evict_if_needed(token_state: dict, limit: int = _TOKEN_STATE_MAX_ENTRIES) -> None:
    """Drop the oldest 25 % of entries when the map exceeds *limit*."""
    if len(token_state["map"]) <= limit:
        return
    evict_count = len(token_state["map"]) // 4
    for key in list(token_state["map"].keys())[:evict_count]:
        token = token_state["map"].pop(key, None)
        if token:
            token_state["reverse"].pop(token, None)


def _tokenize_unlocked(value: str, entity_type: str, token_state: dict) -> str:
    """Assign or retrieve the surrogate token — assumes any lock is already held."""
    key = (entity_type, value)
    if key in token_state["map"]:
        return token_state["map"][key]
    _evict_if_needed(token_state)
    seq = token_state["next"].get(entity_type, 0) + 1
    token_state["next"][entity_type] = seq
    token = f"[[{entity_type}_{seq}]]"
    token_state["map"][key] = token
    token_state["reverse"][token] = value
    return token


def _tokenize(value: str, entity_type: str, token_state: dict, lock=None) -> str:
    """Return a deterministic surrogate token for *value*."""
    if lock:
        with lock:
            return _tokenize_unlocked(value, entity_type, token_state)
    return _tokenize_unlocked(value, entity_type, token_state)


# ---------------------------------------------------------------------------
# XHTML text-node walker
# ---------------------------------------------------------------------------


class _XHTMLTextScrubber(HTMLParser):
    """Walk XHTML, applying *scrub_fn* to every text node."""

    def __init__(self, scrub_fn):
        super().__init__(convert_charrefs=False)
        self._scrub = scrub_fn
        self._parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        attr_str = "".join(
            f" {n}" if v is None else f' {n}="{escape(v)}"' for n, v in attrs
        )
        self._parts.append(f"<{tag}{attr_str}>")

    def handle_endtag(self, tag):
        self._parts.append(f"</{tag}>")

    def handle_startendtag(self, tag, attrs):
        attr_str = "".join(
            f" {n}" if v is None else f' {n}="{escape(v)}"' for n, v in attrs
        )
        self._parts.append(f"<{tag}{attr_str}/>")

    def handle_data(self, data):
        self._parts.append(self._scrub(data))

    def handle_entityref(self, name):
        self._parts.append(f"&{name};")

    def handle_charref(self, name):
        self._parts.append(f"&#{name};")

    def handle_comment(self, data):
        self._parts.append(f"<!--{data}-->")

    def result(self) -> str:
        return "".join(self._parts)


def _scrub_xhtml_text_nodes(xhtml: str, scrub_fn) -> str:
    """Apply *scrub_fn* to every text node in *xhtml*, preserving markup."""
    p = _XHTMLTextScrubber(scrub_fn)
    p.feed(xhtml)
    return p.result()
