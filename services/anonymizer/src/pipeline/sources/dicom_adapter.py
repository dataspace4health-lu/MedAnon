"""DicomAdapter  map DICOM patient/study attributes onto the FHIR IR.

Unlike the legacy ``formats/dicom.py`` scrubber (which removes a fixed PS3.15
tag set), this adapter maps the highest-value patient-identifying tags onto a
FHIR-shaped ``Patient`` resource, runs the *full* rule engine over it (so a
PatientID can be gPAS-pseudonymised, a name NLP-scrubbed, a birth date
generalised  not merely zeroed), then writes the de-identified values back into
the original DICOM dataset by tag.

Scope: patient identification tags (group 0x0010) plus a few study identifiers.
The exhaustive PS3.15 Basic-Profile tag removal remains available via the legacy
``formats/dicom.py`` for callers that want blanket zeroing; this adapter is the
engine-driven path on the new normalization seam.

Pixel data is NOT touched here  burned-in PHI in pixels needs an OCR pass
(scoped follow-on).  The adapter stamps the mandatory PS3.15 §E.3.1
de-identification markers on serialize.

Round-trip contract: ``parse`` keeps the parsed dataset + a tag-origin map on
the instance; ``serialize`` writes transformed IR values back to those tags.
Single-use per object.  Requires the ``pydicom`` package (Docker-only).
"""

from __future__ import annotations

import io

from pipeline.exceptions import NormalizationError

# FHIR Patient path → DICOM (group, element) tag.  The FHIR path is what the
# rule engine matches; the tag is where the value came from and goes back to.
_TAG_MAP: dict[str, tuple[int, int]] = {
    "Patient.name": (0x0010, 0x0010),  # PatientName
    "Patient.identifier.value": (0x0010, 0x0020),  # PatientID
    "Patient.birthDate": (0x0010, 0x0030),  # PatientBirthDate
    "Patient.gender": (0x0010, 0x0040),  # PatientSex
    "Patient.address": (0x0010, 0x1040),  # PatientAddress
    "Patient.telecom.phone": (0x0010, 0x2154),  # PatientTelephoneNumbers
}


class DicomAdapter:
    """Map a DICOM object's patient attributes to a FHIR Patient IR and back."""

    source_format = "dicom"

    def __init__(self) -> None:
        self._ds = None
        # FHIR path → (group, element) for tags actually present in the object.
        self._origin: dict[str, tuple[int, int]] = {}

    # -- parse ----------------------------------------------------------------

    def parse(self, raw: bytes | str) -> list[dict]:
        try:
            import pydicom
            import pydicom.errors
        except ImportError as exc:  # pragma: no cover - exercised only w/o pydicom
            raise NormalizationError(
                "the 'pydicom' package is required for DICOM normalization"
            ) from exc

        if isinstance(raw, str):
            raw = raw.encode("latin-1", errors="ignore")
        try:
            ds = pydicom.dcmread(io.BytesIO(raw), force=True)
        except pydicom.errors.InvalidDicomError as exc:
            raise NormalizationError("Invalid DICOM data") from exc
        except Exception as exc:
            raise NormalizationError(f"Invalid DICOM data: {exc}") from exc

        # ``force=True`` will read non-DICOM bytes into an empty dataset; treat a
        # dataset with no recognisable patient/study tags AND no preamble as
        # malformed so garbage input does not silently "succeed".
        if not getattr(ds, "_dict", None) and not getattr(ds, "preamble", None):
            raise NormalizationError("Invalid DICOM data: no recognisable elements")

        self._ds = ds
        patient: dict = {"resourceType": "Patient", "id": "dicom-patient"}
        self._origin = {}

        for fhir_path, tag in _TAG_MAP.items():
            try:
                if tag not in ds:
                    continue
                value = str(ds[tag].value).strip()
            except Exception:
                continue
            if not value:
                continue
            self._set_ir_field(patient, fhir_path, value)
            self._origin[fhir_path] = tag

        return [patient]

    @staticmethod
    def _set_ir_field(patient: dict, fhir_path: str, value: str) -> None:
        if fhir_path == "Patient.name":
            patient.setdefault("name", []).append({"text": value})
        elif fhir_path == "Patient.address":
            patient.setdefault("address", []).append({"text": value})
        elif fhir_path == "Patient.birthDate":
            patient["birthDate"] = value
        elif fhir_path == "Patient.gender":
            patient["gender"] = value
        elif fhir_path.startswith("Patient.telecom"):
            patient.setdefault("telecom", []).append({"value": value})
        elif fhir_path.startswith("Patient.identifier"):
            patient.setdefault("identifier", []).append({"value": value})

    # -- serialize ------------------------------------------------------------

    def serialize(self, resources: list[dict]) -> bytes:
        if self._ds is None:
            raise NormalizationError("serialize called before parse")

        import pydicom

        if resources:
            patient = resources[0]
            for fhir_path, tag in self._origin.items():
                new_value = self._get_ir_field(patient, fhir_path)
                if new_value is None:
                    new_value = ""  # field removed (redacted) → blank the tag
                try:
                    self._ds[tag].value = new_value
                except Exception:
                    pass

        # Stamp the mandatory PS3.15 §E.3.1 de-identification markers.
        try:
            self._ds[0x0012, 0x0062] = pydicom.DataElement(
                tag=(0x0012, 0x0062), VR="CS", value="YES"
            )
            self._ds[0x0012, 0x0063] = pydicom.DataElement(
                tag=(0x0012, 0x0063), VR="LO", value="MedAnon engine (Patient profile)"
            )
        except Exception:
            pass

        buf = io.BytesIO()
        pydicom.dcmwrite(buf, self._ds)
        return buf.getvalue()

    @staticmethod
    def _get_ir_field(patient: dict, fhir_path: str) -> str | None:
        if fhir_path == "Patient.name":
            return _first_text(patient.get("name") or [])
        if fhir_path == "Patient.address":
            return _first_text(patient.get("address") or [])
        if fhir_path == "Patient.birthDate":
            return patient.get("birthDate")
        if fhir_path == "Patient.gender":
            return patient.get("gender")
        if fhir_path.startswith("Patient.telecom"):
            tele = patient.get("telecom") or []
            return tele[0].get("value") if tele and isinstance(tele[0], dict) else None
        if fhir_path.startswith("Patient.identifier"):
            ids = patient.get("identifier") or []
            return ids[0].get("value") if ids and isinstance(ids[0], dict) else None
        return None

    def can_target_fhir_server(self) -> bool:
        # DICOM output is not FHIR  never upload to the FHIR target server.
        return False


def _first_text(nodes: list) -> str | None:
    for n in nodes:
        if isinstance(n, dict):
            return n.get("text") or n.get("value")
        if isinstance(n, str):
            return n
    return None
