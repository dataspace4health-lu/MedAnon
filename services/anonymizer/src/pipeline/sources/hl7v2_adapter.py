"""Hl7v2Adapter — map HL7 v2 messages onto the FHIR intermediate representation.

Unlike the legacy ``formats/hl7v2.py`` scrubber (which blanks a fixed list of
segment fields), this adapter maps known PHI fields onto a FHIR-shaped
``Patient`` resource, runs the *full* rule engine over it (so an MRN can be
gPAS-pseudonymised, a name NLP-scrubbed, a DOB generalised — not merely blanked),
then writes the de-identified values back into the original HL7 message
structure.

Round-trip contract
-------------------
``parse`` keeps the parsed :class:`hl7.Message` and a field-origin map on the
adapter instance.  ``serialize`` reads the (mutated) IR Patient and writes each
field back to its originating ``(segment, field_index)``.  The adapter is
therefore single-use per message — construct one, ``parse`` then ``serialize``.

Mapping is intentionally focused on the highest-value PID demographics; the
field map is data-driven (`_PID_FIELD_MAP`) so it is easy to extend.  Fields
not in the map are left untouched (the legacy blanket-blank behaviour is
available via ``formats/hl7v2.py`` for callers that want it).
"""

from __future__ import annotations

from pipeline.exceptions import NormalizationError

try:
    import hl7 as _hl7
except ImportError:  # pragma: no cover - exercised only without the hl7 package
    _hl7 = None


# FHIR Patient path  →  (segment, 1-based field index).  The FHIR path is what
# the rule engine matches on; the segment/field is where the value came from and
# where the de-identified value is written back.
_PID_FIELD_MAP: dict[str, tuple[str, int]] = {
    "Patient.identifier.value": ("PID", 3),  # MRN / patient ID list
    "Patient.name": ("PID", 5),  # patient name
    "Patient.birthDate": ("PID", 7),  # DOB
    "Patient.address": ("PID", 11),  # address
    "Patient.telecom.home": ("PID", 13),  # home phone
    "Patient.telecom.work": ("PID", 14),  # business phone
    "Patient.identifier.ssn": ("PID", 19),  # SSN
}


class Hl7v2Adapter:
    """Map an HL7 v2 message to a FHIR Patient IR and back."""

    source_format = "hl7v2"

    def __init__(self) -> None:
        self._message = None
        # FHIR path → (segment, field_index) for the fields actually present.
        self._origin: dict[str, tuple[str, int]] = {}

    # -- parse ----------------------------------------------------------------

    def parse(self, raw: bytes | str) -> list[dict]:
        """Parse an HL7 v2 message into a single-element list ``[patient_ir]``."""
        if _hl7 is None:
            raise NormalizationError(
                "the 'hl7' package is required for HL7 v2 normalization"
            )

        text = (
            raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw)
        )
        text = text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")
        normalized = text.replace("\n", "\r")
        if not normalized.strip():
            raise NormalizationError("Invalid HL7 v2 message: message is empty")

        try:
            message = _hl7.parse(normalized)
        except Exception as exc:
            raise NormalizationError(f"Invalid HL7 v2 message: {exc}") from exc

        self._message = message
        patient: dict = {"resourceType": "Patient", "id": "hl7v2-patient"}
        self._origin = {}

        try:
            pid = message.segment("PID")
        except Exception:
            # No PID segment — nothing to map; return an empty patient.
            return [patient]

        for fhir_path, (seg_name, field_idx) in _PID_FIELD_MAP.items():
            if seg_name != "PID":
                continue
            try:
                value = str(pid[field_idx]).strip()
            except (IndexError, TypeError):
                continue
            if not value:
                continue
            self._set_ir_field(patient, fhir_path, value)
            self._origin[fhir_path] = (seg_name, field_idx)

        return [patient]

    @staticmethod
    def _set_ir_field(patient: dict, fhir_path: str, value: str) -> None:
        """Populate the FHIR Patient IR at *fhir_path* with *value*.

        The IR shapes are minimal but valid enough for FHIRPath matching:
        ``name`` → ``[{"text": value}]``, ``address`` → ``[{"text": value}]``,
        ``telecom`` → ``[{"value": value}]``, identifiers → ``[{"value": value}]``.
        """
        if fhir_path == "Patient.name":
            patient.setdefault("name", []).append({"text": value})
        elif fhir_path == "Patient.address":
            patient.setdefault("address", []).append({"text": value})
        elif fhir_path == "Patient.birthDate":
            patient["birthDate"] = value
        elif fhir_path.startswith("Patient.telecom"):
            patient.setdefault("telecom", []).append({"value": value})
        elif fhir_path.startswith("Patient.identifier"):
            patient.setdefault("identifier", []).append({"value": value})

    # -- serialize ------------------------------------------------------------

    def serialize(self, resources: list[dict]) -> bytes:
        """Write de-identified IR values back into the original HL7 message."""
        if self._message is None:
            raise NormalizationError("serialize called before parse")
        if not resources:
            return str(self._message).replace("\r", "\n").encode("utf-8")

        patient = resources[0]
        try:
            pid = self._message.segment("PID")
        except Exception:
            return str(self._message).replace("\r", "\n").encode("utf-8")

        # Pull the (possibly transformed) value back out of the IR for each
        # mapped field and write it to its originating segment field.
        for fhir_path, (_seg, field_idx) in self._origin.items():
            new_value = self._get_ir_field(patient, fhir_path)
            if new_value is None:
                # Field was removed entirely (e.g. redact deleted it) — blank it.
                new_value = ""
            try:
                pid[field_idx] = new_value
            except IndexError:
                pass

        return str(self._message).replace("\r", "\n").encode("utf-8")

    @staticmethod
    def _get_ir_field(patient: dict, fhir_path: str) -> str | None:
        """Extract the current value of *fhir_path* from the IR Patient."""
        if fhir_path == "Patient.name":
            names = patient.get("name") or []
            return _first_text(names)
        if fhir_path == "Patient.address":
            addrs = patient.get("address") or []
            return _first_text(addrs)
        if fhir_path == "Patient.birthDate":
            return patient.get("birthDate")
        if fhir_path.startswith("Patient.telecom"):
            tele = patient.get("telecom") or []
            return tele[0].get("value") if tele and isinstance(tele[0], dict) else None
        if fhir_path.startswith("Patient.identifier"):
            ids = patient.get("identifier") or []
            return ids[0].get("value") if ids and isinstance(ids[0], dict) else None
        return None

    def can_target_fhir_server(self) -> bool:
        # HL7 v2 output is not FHIR — never upload to the FHIR target server.
        return False


def _first_text(nodes: list) -> str | None:
    """Return the ``text`` (or ``value``) of the first dict in *nodes*."""
    for n in nodes:
        if isinstance(n, dict):
            return n.get("text") or n.get("value")
        if isinstance(n, str):
            return n
    return None
