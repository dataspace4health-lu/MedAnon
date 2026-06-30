"""Offline code well-formedness validation (verification — no external server).

Validates a (system, code) pair's *format* and *check digit* for the major
clinical code systems, catching malformed or mistyped codes without a terminology
server. This is Kahn **verification** (internal well-formedness), distinct from
terminology **validation** (membership in a ValueSet), which needs
``CodeSystem/$validate-code`` (see ``terminology_client``). Returns ``None`` for
systems we cannot check offline so those codings are not counted (NA, never a
false flag).

References (code structure, not invented rules):
  - LOINC code format: numeric body + ``-`` + Mod-10 check digit (Regenstrief LOINC Users' Guide);
    LOINC Answer (LA) / Part (LP) codes are valid LOINC content (accepted on format).
  - SNOMED CT SCTID: 6–18 digits with a trailing **Verhoeff** dihedral check digit
    (SNOMED CT Technical Implementation Guide).
  - ICD-10 / ICD-10-CM / ICD-10-GM code structure (WHO; CMS/NCHS; BfArM).
  - RxNorm RXCUI: numeric concept identifier (NLM).
  - ATC (WHO Collaborating Centre for Drug Statistics Methodology): 5-level hierarchy.
  - EDQM Standard Terms: numeric concept codes (dose form / route / units of presentation).
"""

from __future__ import annotations

import re

LOINC = "http://loinc.org"
SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10"
ICD10CM = "http://hl7.org/fhir/sid/icd-10-cm"
RXNORM = "http://www.nlm.nih.gov/research/umls/rxnorm"
# EU code systems (additive — US systems above stay). ATC is the EU medication
# standard (vs RxNorm); EDQM Standard Terms cover dose form / route / units of
# presentation for ePrescription/eDispensation; ICD-10-GM is the German diagnosis
# modification (same structure as WHO ICD-10).
ATC = "http://www.whocc.no/atc"
EDQM = "http://standardterms.edqm.eu"
ICD10GM = "http://fhir.de/CodeSystem/bfarm/icd-10-gm"

_LOINC_RE = re.compile(r"^\d{1,8}-\d$")
# ATC (WHO ATC/DDD): 5 hierarchical levels, each level is a valid code
#   L1 anatomical (1 letter), L2 therapeutic (2 digits), L3 pharmacological
#   (1 letter), L4 chemical (1 letter), L5 substance (2 digits) -> e.g. C09AA05.
_ATC_RE = re.compile(r"^[A-Z](\d{2}([A-Z]([A-Z](\d{2})?)?)?)?$")
# EDQM Standard Terms concept codes are numeric identifiers.
_EDQM_RE = re.compile(r"^\d{3,11}$")
# LOINC Answer (LA) and Part (LP) codes — valid LOINC content used by survey /
# social-history / answer-list Observations (Synthea emits these heavily). They
# have the well-defined form L[AP]<digits>-<digit>; their check digit is NOT the
# plain numeric Mod-10, so we validate the format and do not assert a check digit
# we cannot reproduce offline (Regenstrief LOINC Users' Guide: Answers/Parts).
_LOINC_LALP_RE = re.compile(r"^L[AP]\d+-\d$")
# ICD-10/-CM: letter, digit, digit-or-letter, optional dotted 1–4 char subclass.
_ICD10_RE = re.compile(r"^[A-Z][0-9][0-9A-Z](\.[0-9A-Z]{1,4})?$")
_RXNORM_RE = re.compile(r"^\d+$")

# Verhoeff dihedral-group (D5) multiplication + permutation tables.
_VERHOEFF_D = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 2, 3, 4, 0, 6, 7, 8, 9, 5),
    (2, 3, 4, 0, 1, 7, 8, 9, 5, 6),
    (3, 4, 0, 1, 2, 8, 9, 5, 6, 7),
    (4, 0, 1, 2, 3, 9, 5, 6, 7, 8),
    (5, 9, 8, 7, 6, 0, 4, 3, 2, 1),
    (6, 5, 9, 8, 7, 1, 0, 4, 3, 2),
    (7, 6, 5, 9, 8, 2, 1, 0, 4, 3),
    (8, 7, 6, 5, 9, 3, 2, 1, 0, 4),
    (9, 8, 7, 6, 5, 4, 3, 2, 1, 0),
)
_VERHOEFF_P = (
    (0, 1, 2, 3, 4, 5, 6, 7, 8, 9),
    (1, 5, 7, 6, 2, 8, 3, 0, 9, 4),
    (5, 8, 0, 3, 7, 9, 6, 1, 4, 2),
    (8, 9, 1, 6, 0, 4, 3, 5, 2, 7),
    (9, 4, 5, 3, 1, 2, 6, 8, 7, 0),
    (4, 2, 8, 6, 5, 7, 3, 9, 0, 1),
    (2, 7, 9, 3, 8, 0, 6, 4, 1, 5),
    (7, 0, 4, 6, 9, 1, 3, 2, 5, 8),
)


def _verhoeff_ok(number: str) -> bool:
    c = 0
    for i, ch in enumerate(reversed(number)):
        c = _VERHOEFF_D[c][_VERHOEFF_P[i % 8][int(ch)]]
    return c == 0


def _luhn_ok(number: str) -> bool:
    """Standard Mod-10 (Luhn) checksum: the digit string (body + trailing check
    digit) is valid when its weighted sum is divisible by 10. The LOINC check
    digit is computed with this algorithm (Regenstrief LOINC Users' Guide)."""
    total = 0
    for i, ch in enumerate(reversed(number)):
        d = int(ch)
        if i % 2 == 1:  # double every second digit from the right (check digit = i0)
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _loinc_ok(code: str) -> bool:
    """LOINC: numeric body + '-' + Mod-10 check digit. Validate both the format
    AND the check digit (a transposed/mistyped digit is the error a check digit
    exists to catch), mirroring the SNOMED Verhoeff treatment.

    LOINC Answer (LA) / Part (LP) codes are accepted on their well-defined format
    without a check-digit assertion — they are valid LOINC content but do not use
    the plain numeric Mod-10, so asserting it would false-flag legitimate codes.
    """
    if _LOINC_LALP_RE.match(code):
        return True
    if not _LOINC_RE.match(code):
        return False
    body, _, check = code.partition("-")
    return _luhn_ok(body + check)


def _snomed_ok(code: str) -> bool:
    # SCTID: all digits, 6–18 long, valid Verhoeff check digit.
    if not code.isdigit() or not (6 <= len(code) <= 18):
        return False
    return _verhoeff_ok(code)


def validate_code_format(system: str, code: str) -> bool | None:
    """Return True/False for a checkable system, or None when not checkable offline."""
    if not code:
        return None
    if system == LOINC:
        return _loinc_ok(code)
    if system == SNOMED:
        return _snomed_ok(code)
    if system in (ICD10, ICD10CM, ICD10GM):
        return bool(_ICD10_RE.match(code))
    if system == RXNORM:
        return bool(_RXNORM_RE.match(code))
    if system == ATC:
        return bool(_ATC_RE.match(code))
    if system == EDQM:
        return bool(_EDQM_RE.match(code))
    return None  # e.g. UCUM and others — needs a real terminology server
