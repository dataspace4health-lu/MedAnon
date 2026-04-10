"""HL7 v2 message de-identification.

Scrubs PHI from standard HL7 v2 segments:
  PID — Patient Identification (name, DOB, address, phone, SSN, MRN)
  NK1 — Next of Kin (name, address, phone)
  PV1 — Patient Visit (attending, referring, consulting physicians)
  PV2 — Patient Visit Additional Information (admission reason)
  DG1 — Diagnosis (when free-text description contains names)
  OBX — Observation (responsible observer)
  ORC — Common Order (ordering provider)
  OBR — Observation Request (ordering/results provider names)
  IN1 — Insurance (insured name, address, DOB)

Usage:
    from formats.hl7v2 import deidentify_hl7v2
    clean_message = deidentify_hl7v2(raw_message_text)
"""

try:
    import hl7
except ImportError as _hl7_import_err:
    raise ImportError(
        "The 'hl7' package is required for HL7 v2 de-identification. "
        "Install it with: pip install hl7"
    ) from _hl7_import_err

# ---------------------------------------------------------------------------
# Field scrub map
# ---------------------------------------------------------------------------
# Maps segment name → list of 1-based field indices to blank.
# The hl7 library stores the segment name at index 0 and HL7 field N at
# Python index N, so 1-based spec field numbers translate directly.

HL7V2_SCRUB_FIELDS: dict[str, list[int]] = {
    "PID": [3, 4, 5, 6, 7, 9, 11, 12, 13, 14, 19, 20, 22],
    # 3=PatientIDList, 4=AltPatientID, 5=PatientName, 6=MothersMaidenName,
    # 7=DOB, 9=PatientAlias, 11=PatientAddress,
    # 12=CountyCode, 13=HomePhone, 14=BusinessPhone, 19=SSN, 20=DriversLicense,
    # 22=EthnicGroup
    "NK1": [2, 4, 5, 6, 7],
    # 2=Name, 4=Address, 5=Phone, 6=BusinessPhone, 7=ContactRole
    "PV1": [7, 8, 9, 17, 19],
    # 7=AttendingDoctor, 8=ReferringDoctor, 9=ConsultingDoctor,
    # 17=AdmittingDoctor, 19=VisitNumber
    "PV2": [1],
    # 1=PriorPendingLocation (may contain ward/bed info)
    "OBX": [16],
    # 16=ResponsibleObserver
    "ORC": [10, 11, 12],
    # 10=EnteredBy, 11=VerifiedBy, 12=OrderingProvider
    "OBR": [16, 28, 32, 34, 35],
    # 16=OrderingProvider, 28=ResultCopiesTo, 32=PrincipalResultInterpreter,
    # 34=TechnicianName, 35=TranscriptionistName
    "IN1": [16, 17, 18, 19, 36, 43, 49],
    # 16=NameOfInsured, 17=InsuredRelationship, 18=InsuredDOB, 19=InsuredAddress,
    # 36=PolicyHolderName, 43=InsuredEmployerName, 49=InsuredOrganizationName
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _blank_field(segment: "hl7.Segment", field_index: int) -> None:
    """Set *segment* field at 1-based *field_index* to an empty string.

    Silently skips when the segment has fewer fields than expected so that
    non-standard or shortened messages are handled gracefully.
    """
    try:
        segment[field_index] = ""
    except IndexError:
        pass


def _scrub_segment(segment: "hl7.Segment") -> None:
    """Blank all PHI fields in *segment* according to ``HL7V2_SCRUB_FIELDS``."""
    try:
        name = str(segment[0][0])
    except (IndexError, TypeError):
        return
    indices = HL7V2_SCRUB_FIELDS.get(name)
    if not indices:
        return
    for idx in indices:
        _blank_field(segment, idx)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def deidentify_hl7v2(message_text: str) -> str:
    """De-identify a single HL7 v2 message string.

    Strips a leading BOM, normalises line endings to ``\\r`` (as the HL7
    specification requires), parses the message, blanks every PHI field
    listed in ``HL7V2_SCRUB_FIELDS``, and returns the result with ``\\r``
    replaced by ``\\n`` for convenient downstream handling.

    Args:
        message_text: Raw HL7 v2 message text. Accepts ``\\r``, ``\\n``, or
            ``\\r\\n`` line endings, and optionally a UTF-8 BOM.

    Returns:
        The de-identified message as a string with ``\\n`` segment separators.

    Raises:
        ValueError: If *message_text* is empty or cannot be parsed as a valid
            HL7 v2 message.
    """
    # Strip BOM and normalize line endings so the parser always sees \r
    text = message_text.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    normalized = text.replace("\n", "\r")

    if not normalized.strip():
        raise ValueError("Invalid HL7 v2 message: message is empty")

    try:
        message = hl7.parse(normalized)
    except hl7.ParseException as exc:
        raise ValueError(f"Invalid HL7 v2 message: {exc}") from exc
    except Exception as exc:
        raise ValueError(f"Invalid HL7 v2 message: {exc}") from exc

    for segment in message:
        _scrub_segment(segment)

    return str(message).replace("\r", "\n")


def deidentify_hl7v2_batch(batch_text: str) -> str:
    """De-identify a batch of concatenated HL7 v2 messages.

    Splits on ``MSH|`` message boundaries, de-identifies each message
    individually, and rejoins the results with ``\\n``.

    An empty or whitespace-only *batch_text* returns an empty string.

    Args:
        batch_text: One or more HL7 v2 messages concatenated together, each
            starting with ``MSH|``. Any line-ending style is accepted.

    Returns:
        The de-identified messages joined by ``\\n``.
    """
    if not batch_text or not batch_text.strip():
        return ""

    # Normalize line endings for consistent splitting
    text = batch_text.lstrip("\ufeff")
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    # Split into individual messages on each occurrence of MSH| after the first
    # character position.  We scan for "MSH|" that is preceded by a newline
    # (or starts the string) to avoid matching MSH within a field value.
    import re

    parts = re.split(r"(?=(?:^|\n)MSH\|)", text, flags=re.MULTILINE)
    messages = [p.strip() for p in parts if p.strip()]

    if not messages:
        return ""

    results = []
    for msg_text in messages:
        results.append(deidentify_hl7v2(msg_text))

    return "\n".join(results)
