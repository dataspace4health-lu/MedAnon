"""CdaAdapter  map CDA (HL7 v3) patient demographics onto the FHIR IR.

Unlike the legacy ``formats/cda.py`` scrubber (which blanks fixed elements),
this adapter maps the ``recordTarget/patientRole/patient`` demographics onto a
FHIR-shaped ``Patient`` resource, runs the *full* rule engine over it (so a
patient ID can be gPAS-pseudonymised, a name NLP-scrubbed, a birthTime
generalised  not merely blanked), then writes the de-identified values back
into the original XML elements by reference.

Scope: the highest-value patient demographics in ``recordTarget``  name,
birthTime, address, and patient ID.  Author / participant PHI is left to the
legacy ``formats/cda.py`` scrubber for now; this adapter establishes the
engine-driven path on the new normalization seam.

Round-trip contract: ``parse`` keeps the parsed XML tree and a map of
FHIR-path → source XML element on the adapter instance; ``serialize`` writes the
transformed IR values back to those elements and re-serialises the document.
Single-use per document.
"""

from __future__ import annotations

import xml.etree.ElementTree as _ET_WRITE

import defusedxml.ElementTree as _ET

from pipeline.exceptions import NormalizationError

CDA_NAMESPACE = "urn:hl7-org:v3"
_ROOT_TAG = f"{{{CDA_NAMESPACE}}}ClinicalDocument"


def _ns(tag: str) -> str:
    return f"{{{CDA_NAMESPACE}}}{tag}"


class CdaAdapter:
    """Map a CDA document's patient demographics to a FHIR Patient IR and back."""

    source_format = "cda"

    def __init__(self) -> None:
        self._root = None
        # FHIR path → list of source XML elements to write the value back into.
        self._name_parts: list = []  # given/family/prefix/suffix elements
        self._birth_time = None  # <birthTime> element (value attr)
        self._addr_parts: list = []  # street/city/postalCode/... elements
        self._id_elem = None  # <id> element (extension attr)

    # -- parse ----------------------------------------------------------------

    def parse(self, raw: bytes | str) -> list[dict]:
        text = (
            raw.decode("utf-8-sig") if isinstance(raw, (bytes, bytearray)) else str(raw)
        )
        try:
            root = _ET.fromstring(text)
        except Exception as exc:
            raise NormalizationError(f"Invalid CDA XML: {exc}") from exc
        if root.tag != _ROOT_TAG:
            raise NormalizationError(
                f"Not a CDA document: root element is {root.tag!r}, "
                f"expected {_ROOT_TAG!r}"
            )

        self._root = root
        patient_ir: dict = {"resourceType": "Patient", "id": "cda-patient"}

        record_target = root.find(_ns("recordTarget"))
        if record_target is None:
            return [patient_ir]
        patient_role = record_target.find(_ns("patientRole"))
        if patient_role is None:
            return [patient_ir]

        # Patient ID → identifier
        id_elem = patient_role.find(_ns("id"))
        if id_elem is not None and id_elem.get("extension"):
            self._id_elem = id_elem
            patient_ir.setdefault("identifier", []).append(
                {"value": id_elem.get("extension")}
            )

        # Address → address.text (concatenated parts)
        addr = patient_role.find(_ns("addr"))
        if addr is not None:
            parts = []
            for sub_tag in (
                "streetAddressLine",
                "city",
                "postalCode",
                "state",
                "country",
            ):
                for sub in addr.findall(_ns(sub_tag)):
                    if sub.text:
                        self._addr_parts.append(sub)
                        parts.append(sub.text)
            if parts:
                patient_ir.setdefault("address", []).append({"text": " ".join(parts)})

        patient = patient_role.find(_ns("patient"))
        if patient is not None:
            # Name → name.text (concatenated given/family)
            name_elem = patient.find(_ns("name"))
            if name_elem is not None:
                parts = []
                for sub_tag in ("prefix", "given", "family", "suffix"):
                    for sub in name_elem.findall(_ns(sub_tag)):
                        if sub.text:
                            self._name_parts.append(sub)
                            parts.append(sub.text)
                if parts:
                    patient_ir.setdefault("name", []).append({"text": " ".join(parts)})

            # birthTime value → birthDate
            birth_time = patient.find(_ns("birthTime"))
            if birth_time is not None and birth_time.get("value"):
                self._birth_time = birth_time
                patient_ir["birthDate"] = birth_time.get("value")

        return [patient_ir]

    # -- serialize ------------------------------------------------------------

    def serialize(self, resources: list[dict]) -> bytes:
        if self._root is None:
            raise NormalizationError("serialize called before parse")
        if not resources:
            return self._to_bytes()

        patient = resources[0]

        # Name: if the IR name is gone/redacted, blank all name part elements;
        # otherwise we cannot losslessly split a transformed single string back
        # into given/family, so a *changed* name blanks the parts (fail-safe
        # the original PHI must not survive).
        new_name = _first_text(patient.get("name") or [])
        if self._name_parts:
            original_name = " ".join(e.text for e in self._name_parts if e.text)
            if not new_name or new_name != original_name:
                for e in self._name_parts:
                    e.text = None

        # Address: same fail-safe  if the IR address changed, blank the parts.
        new_addr = _first_text(patient.get("address") or [])
        if self._addr_parts:
            original_addr = " ".join(e.text for e in self._addr_parts if e.text)
            if not new_addr or new_addr != original_addr:
                for e in self._addr_parts:
                    e.text = None

        # birthTime: write the transformed value back to the value attribute.
        if self._birth_time is not None:
            new_birth = patient.get("birthDate")
            self._birth_time.set("value", new_birth if new_birth else "")

        # Patient ID extension: write the (pseudonymised) value back.
        if self._id_elem is not None:
            ids = patient.get("identifier") or []
            new_id = ids[0].get("value") if ids and isinstance(ids[0], dict) else None
            self._id_elem.set("extension", new_id if new_id else "")

        return self._to_bytes()

    def _to_bytes(self) -> bytes:
        return _ET_WRITE.tostring(self._root, encoding="utf-8")

    def can_target_fhir_server(self) -> bool:
        # CDA output is not FHIR  never upload to the FHIR target server.
        return False


def _first_text(nodes: list) -> str | None:
    for n in nodes:
        if isinstance(n, dict):
            return n.get("text") or n.get("value")
        if isinstance(n, str):
            return n
    return None
