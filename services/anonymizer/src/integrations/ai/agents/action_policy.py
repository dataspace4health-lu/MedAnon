"""Shared action-selection policy for the AI config agents.

A single source of truth for *which de-identification action fits which kind of
field*. Both the conversational config agent (``config_chat``) and the field
scanner (``field_scanner``) inject this so they recommend consistent, utility-
preserving actions instead of redacting everything.

The policy encodes the project's de-identification stance:
- Stable identifiers / references → pseudonymise (keep referential integrity).
- Dates → generalise to year / year-month (keep temporal analytics).
- Free-text / narrative → NLP scrub (keep clinical content, remove embedded PHI).
- Coded clinical data (code/system/status/category) → NOT PII, leave untouched.

``action_policy_block(gpas_available)`` returns the prompt text. When no gPAS
server is configured the identifier guidance falls back to ``cryptohash`` so the
model never proposes an action the deployment can't run.
"""

from __future__ import annotations

# The actionable policy table, rendered into both agents' system prompts. Kept
# terse and example-driven because the local models are small (Gemma 3 4B) and
# follow concrete path→action mappings far better than prose.
_POLICY_TEMPLATE = """\
ACTION SELECTION  choose the action that fits the FIELD, never redact \
everything. Match each PII field to the closest class below:

1. STABLE IDENTIFIERS & REFERENCES (a resource id, identifier.value, any \
*.reference, MRN, account number): action = {id_action}. These must stay \
consistent across resources so links survive de-identification  NEVER redact \
them (redaction breaks subject.reference / cross-resource joins).
   e.g. Patient.id, Patient.identifier.value, Condition.subject.reference, \
Encounter.identifier.value → {id_action}

2. DATES & TIMESTAMPS (birthDate, *.onsetDateTime, *.recordedDate, \
*.effectiveDateTime, period.start/end, issued, authoredOn): action = \
generalize. Use strategy date_year for birthDate; date_year_month for clinical \
event dates. Keeps temporal analysis while removing day-level identifiability.
   e.g. Patient.birthDate → generalize (strategy: date_year); \
Condition.onsetDateTime → generalize (strategy: date_year_month)

3. NAMES: Patient/Practitioner .name.family / .given / .prefix → redact. \
A free-text name field (.text) → nlp_scrub.

4. POSTAL CODES (address.postalCode / zip): action = generalize \
(strategy: zip_prefix). Keep the 3-digit prefix, drop the rest.

5. GEO-PRECISION (address.extension carrying lat/long, position.latitude, \
position.longitude): action = redact (removes precise geolocation).

6. TELECOM VALUES (telecom.value holding a phone or email): action = mask \
(strategy: keep_domain for email, keep_country_code for phone)  preserves \
shape for validation without exposing the contact point.

7. FREE-TEXT / NARRATIVE (code.text, *.text, note, dosageInstruction.text, \
presentedForm.data, content.attachment.data, any human-written string): \
action = nlp_scrub. Detects and removes names/dates/locations embedded in the \
prose while keeping the clinical content.

8. CODED / STRUCTURAL DATA  NOT PII, emit NO rule: coding.code, \
coding.system, coding.display, *.system, status, category, clinicalStatus, \
verificationStatus, intent, criticality, type codes, resourceType, url. These \
are standard terminology and carry no identity  leaving them intact preserves \
clinical meaning. Do not propose rules for them.

If a field does not clearly fit a class above, prefer the least destructive \
action that still removes identity (generalize/mask over redact), and explain \
your choice."""

# Identifier action when gPAS is available vs not. gPAS pseudonyms are
# reversible-by-the-TTP and consistent; cryptohash is the deterministic,
# dependency-free fallback. Both preserve referential integrity.
_ID_ACTION_GPAS = "gpas_pseudonymize"
_ID_ACTION_FALLBACK = "cryptohash"


def action_policy_block(gpas_available: bool = False) -> str:
    """Return the action-selection policy prompt text.

    When ``gpas_available`` is True, identifiers are pseudonymised via gPAS;
    otherwise the model is told to use ``cryptohash`` so it never proposes an
    action the deployment can't execute.
    """
    id_action = _ID_ACTION_GPAS if gpas_available else _ID_ACTION_FALLBACK
    block = _POLICY_TEMPLATE.format(id_action=id_action)
    if not gpas_available:
        block += (
            "\n\nNOTE: no gPAS server is configured, so use cryptohash (not "
            "gpas_pseudonymize) for identifiers  it is deterministic and keeps "
            "links consistent without a pseudonymisation service."
        )
    return block


def gpas_is_available() -> bool:
    """Best-effort check for a configured gPAS server (env-driven).

    Mirrors the engine's own gPAS auto-selection signal (``GPAS_URL``). Kept
    here so the agents don't import the gPAS client just to read one env var.
    """
    import os

    return bool(os.environ.get("GPAS_URL", "").strip())
