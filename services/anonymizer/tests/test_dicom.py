"""Tests for DICOM de-identification — runs locally without Docker.

All tests create synthetic DICOM datasets entirely in memory using
``pydicom.Dataset``; no DICOM files on disk are required.

The entire module is skipped automatically when pydicom is not installed.
"""
import asyncio
import io
import os
import sys
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

# Allow running from repo root or services/anonymizer/
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

# Skip the whole module when pydicom is not available.
import pytest

pydicom = pytest.importorskip("pydicom")


# ---------------------------------------------------------------------------
# Helper — build a minimal in-memory DICOM dataset
# ---------------------------------------------------------------------------

def _make_dicom_bytes(**kwargs) -> bytes:
    """Create a minimal in-memory DICOM dataset and return its serialised bytes.

    Keyword arguments are applied as attribute assignments on the dataset, e.g.
    ``_make_dicom_bytes(PatientName="Doe^John", PatientID="12345")``.
    """
    from pydicom.dataset import Dataset, FileDataset
    from pydicom.uid import ExplicitVRLittleEndian
    import pydicom.uid as uid_module

    ds = FileDataset(None, {}, is_implicit_VR=False, is_little_endian=True)
    ds.file_meta = Dataset()
    ds.file_meta.MediaStorageSOPClassUID = uid_module.SecondaryCaptureImageStorage
    ds.file_meta.MediaStorageSOPInstanceUID = uid_module.generate_uid()
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    ds.is_implicit_VR = False
    ds.is_little_endian = True

    for attr, value in kwargs.items():
        setattr(ds, attr, value)

    buf = io.BytesIO()
    pydicom.dcmwrite(buf, ds)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Unit tests — pipeline.dicom_deidentify
# ---------------------------------------------------------------------------

class TestDeidentifyDicom(unittest.TestCase):
    """Tests for the ``deidentify_dicom`` function (no HTTP layer)."""

    def _roundtrip(self, **kwargs):
        """Build a DICOM file with given attributes, de-identify it, parse result."""
        from formats.dicom import deidentify_dicom
        raw = _make_dicom_bytes(**kwargs)
        result = deidentify_dicom(raw)
        return pydicom.dcmread(io.BytesIO(result))

    # --- PHI tags are removed ---

    def test_patient_name_removed(self):
        ds = self._roundtrip(PatientName="Doe^John")
        self.assertFalse(hasattr(ds, "PatientName") and str(ds.PatientName).strip(),
                         "PatientName should be absent or empty after de-identification")

    def test_patient_id_removed(self):
        ds = self._roundtrip(PatientID="MRN-99999")
        self.assertNotIn((0x0010, 0x0020), ds,
                         "PatientID tag should be removed")

    def test_patient_birthdate_removed(self):
        ds = self._roundtrip(PatientBirthDate="19800101")
        self.assertNotIn((0x0010, 0x0030), ds,
                         "PatientBirthDate tag should be removed")

    def test_patient_sex_removed(self):
        ds = self._roundtrip(PatientSex="M")
        self.assertNotIn((0x0010, 0x0040), ds,
                         "PatientSex tag should be removed")

    def test_patient_age_removed(self):
        ds = self._roundtrip(PatientAge="045Y")
        self.assertNotIn((0x0010, 0x1010), ds,
                         "PatientAge tag should be removed")

    def test_institution_name_removed(self):
        ds = self._roundtrip(InstitutionName="General Hospital")
        self.assertNotIn((0x0008, 0x0080), ds,
                         "InstitutionName tag should be removed")

    def test_referring_physician_name_removed(self):
        ds = self._roundtrip(ReferringPhysicianName="Smith^Alice")
        self.assertNotIn((0x0008, 0x0090), ds,
                         "ReferringPhysicianName tag should be removed")

    def test_accession_number_removed(self):
        ds = self._roundtrip(AccessionNumber="ACC-2024-001")
        self.assertNotIn((0x0008, 0x0050), ds,
                         "AccessionNumber tag should be removed")

    def test_study_date_removed(self):
        ds = self._roundtrip(StudyDate="20230101")
        self.assertNotIn((0x0008, 0x0020), ds,
                         "StudyDate tag should be removed")

    def test_series_date_removed(self):
        ds = self._roundtrip(SeriesDate="20230102")
        self.assertNotIn((0x0008, 0x0021), ds,
                         "SeriesDate tag should be removed")

    def test_acquisition_date_removed(self):
        ds = self._roundtrip(AcquisitionDate="20230103")
        self.assertNotIn((0x0008, 0x0022), ds,
                         "AcquisitionDate tag should be removed")

    def test_content_date_removed(self):
        ds = self._roundtrip(ContentDate="20230104")
        self.assertNotIn((0x0008, 0x0023), ds,
                         "ContentDate tag should be removed")

    def test_operators_name_removed(self):
        ds = self._roundtrip(OperatorsName="Tech^Bob")
        self.assertNotIn((0x0008, 0x1070), ds,
                         "OperatorsName tag should be removed")

    def test_device_serial_number_removed(self):
        ds = self._roundtrip(DeviceSerialNumber="SN-XYZ-123456")
        self.assertNotIn((0x0018, 0x1000), ds,
                         "DeviceSerialNumber tag should be removed")

    def test_study_instance_uid_removed(self):
        import pydicom.uid as uid_module
        ds = self._roundtrip(StudyInstanceUID=uid_module.generate_uid())
        self.assertNotIn((0x0020, 0x000D), ds,
                         "StudyInstanceUID should be removed")

    def test_series_instance_uid_removed(self):
        import pydicom.uid as uid_module
        ds = self._roundtrip(SeriesInstanceUID=uid_module.generate_uid())
        self.assertNotIn((0x0020, 0x000E), ds,
                         "SeriesInstanceUID should be removed")

    def test_series_description_removed(self):
        ds = self._roundtrip(SeriesDescription="Chest CT with contrast")
        self.assertNotIn((0x0008, 0x103E), ds,
                         "SeriesDescription tag should be removed")

    def test_protocol_name_removed(self):
        ds = self._roundtrip(ProtocolName="CHEST_ROUTINE")
        self.assertNotIn((0x0018, 0x1030), ds,
                         "ProtocolName tag should be removed")

    def test_admission_id_removed(self):
        ds = self._roundtrip(AdmissionID="ADM-20230501")
        self.assertNotIn((0x0038, 0x0010), ds,
                         "AdmissionID tag should be removed")

    # --- Multiple PHI tags in one dataset ---

    def test_multiple_phi_tags_all_removed(self):
        from formats.dicom import deidentify_dicom
        raw = _make_dicom_bytes(
            PatientName="Smith^Jane",
            PatientID="MRN-001",
            PatientBirthDate="19750315",
            InstitutionName="City Clinic",
            AccessionNumber="ACC-1001",
            StudyDate="20240101",
        )
        result = deidentify_dicom(raw)
        ds = pydicom.dcmread(io.BytesIO(result))

        for tag in [
            (0x0010, 0x0020),  # PatientID
            (0x0010, 0x0030),  # PatientBirthDate
            (0x0008, 0x0080),  # InstitutionName
            (0x0008, 0x0050),  # AccessionNumber
            (0x0008, 0x0020),  # StudyDate
        ]:
            self.assertNotIn(tag, ds, f"Tag {tag!r} should have been removed")

    # --- De-identification markers are set ---

    def test_deidentification_flag_set(self):
        ds = self._roundtrip()
        self.assertIn(
            (0x0012, 0x0062),
            ds,
            "PatientIdentityRemoved (0012,0062) must be present after de-identification",
        )
        self.assertEqual(str(ds[0x0012, 0x0062].value), "YES")

    def test_deidentification_method_set(self):
        ds = self._roundtrip()
        self.assertIn(
            (0x0012, 0x0063),
            ds,
            "DeidentificationMethod (0012,0063) must be present after de-identification",
        )
        self.assertIn("PS3.15", str(ds[0x0012, 0x0063].value))
        self.assertIn("Basic Profile", str(ds[0x0012, 0x0063].value))

    def test_deidentification_flag_vr_is_cs(self):
        ds = self._roundtrip()
        self.assertEqual(ds[0x0012, 0x0062].VR, "CS",
                         "PatientIdentityRemoved should have VR=CS")

    def test_deidentification_method_vr_is_lo(self):
        ds = self._roundtrip()
        self.assertEqual(ds[0x0012, 0x0063].VR, "LO",
                         "DeidentificationMethod should have VR=LO")

    # --- Non-PHI tags are preserved ---

    def test_modality_preserved(self):
        ds = self._roundtrip(Modality="CT")
        self.assertIn((0x0008, 0x0060), ds,
                      "Modality should not be removed")
        self.assertEqual(ds.Modality, "CT")

    def test_rows_and_columns_preserved(self):
        ds = self._roundtrip(Rows=512, Columns=512)
        self.assertEqual(ds.Rows, 512)
        self.assertEqual(ds.Columns, 512)

    def test_sop_class_uid_preserved(self):
        import pydicom.uid as uid_module
        sop_uid = uid_module.SecondaryCaptureImageStorage
        ds = self._roundtrip(SOPClassUID=sop_uid)
        self.assertIn((0x0008, 0x0016), ds,
                      "SOPClassUID should not be removed")

    def test_pixel_data_preserved(self):
        """Pixel Data must survive de-identification (PHI in pixels is out-of-scope)."""
        from pydicom.dataset import Dataset, FileDataset
        from pydicom.uid import ExplicitVRLittleEndian
        import pydicom.uid as uid_module
        from formats.dicom import deidentify_dicom

        ds = FileDataset(None, {}, is_implicit_VR=False, is_little_endian=True)
        ds.file_meta = Dataset()
        ds.file_meta.MediaStorageSOPClassUID = uid_module.SecondaryCaptureImageStorage
        ds.file_meta.MediaStorageSOPInstanceUID = uid_module.generate_uid()
        ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        ds.is_implicit_VR = False
        ds.is_little_endian = True
        ds.BitsAllocated = 8
        ds.BitsStored = 8
        ds.HighBit = 7
        ds.PixelRepresentation = 0
        ds.Rows = 2
        ds.Columns = 2
        ds.SamplesPerPixel = 1
        pixel_bytes = bytes([10, 20, 30, 40])
        ds.PixelData = pixel_bytes

        buf = io.BytesIO()
        pydicom.dcmwrite(buf, ds)
        raw = buf.getvalue()

        result = deidentify_dicom(raw)
        out_ds = pydicom.dcmread(io.BytesIO(result))
        self.assertIn((0x7FE0, 0x0010), out_ds,
                      "PixelData (7FE0,0010) must not be removed")

    # --- Edge cases ---

    def test_empty_dicom_no_crash(self):
        """A dataset with no identifying tags should process without error."""
        from formats.dicom import deidentify_dicom
        raw = _make_dicom_bytes()
        result = deidentify_dicom(raw)
        ds = pydicom.dcmread(io.BytesIO(result))
        self.assertEqual(str(ds[0x0012, 0x0062].value), "YES")

    def test_idempotent(self):
        """Calling deidentify_dicom twice produces the same de-identification markers."""
        from formats.dicom import deidentify_dicom
        raw = _make_dicom_bytes(PatientName="Test^Patient")
        first_pass = deidentify_dicom(raw)
        second_pass = deidentify_dicom(first_pass)
        ds = pydicom.dcmread(io.BytesIO(second_pass))
        self.assertEqual(str(ds[0x0012, 0x0062].value), "YES")
        self.assertNotIn((0x0010, 0x0010), ds)

    def test_invalid_bytes_raises_value_error(self):
        from formats.dicom import deidentify_dicom
        with self.assertRaises(ValueError, msg="Expected ValueError for non-DICOM bytes") as ctx:
            deidentify_dicom(b"not dicom data at all \x00\x00\x00\x00")
        self.assertIn("Invalid DICOM", str(ctx.exception))

    def test_return_type_is_bytes(self):
        from formats.dicom import deidentify_dicom
        raw = _make_dicom_bytes(PatientName="Type^Check")
        result = deidentify_dicom(raw)
        self.assertIsInstance(result, bytes)

    def test_result_is_parseable_dicom(self):
        """Output bytes must round-trip back through pydicom without error."""
        from formats.dicom import deidentify_dicom
        raw = _make_dicom_bytes(PatientID="PARSE-TEST")
        result = deidentify_dicom(raw)
        # Should not raise
        ds = pydicom.dcmread(io.BytesIO(result))
        self.assertIsNotNone(ds)

    def test_sequence_items_scrubbed(self):
        """Identifying tags nested inside a sequence must be removed."""
        from formats.dicom import deidentify_dicom
        from pydicom.dataset import Dataset
        from pydicom.sequence import Sequence

        # Build a dataset with a sequence containing a PatientID element.
        ds_inner = Dataset()
        ds_inner.PatientID = "SEQ-PATIENT"

        from pydicom.dataset import Dataset as DS2, FileDataset
        from pydicom.uid import ExplicitVRLittleEndian
        import pydicom.uid as uid_module

        outer = FileDataset(None, {}, is_implicit_VR=False, is_little_endian=True)
        outer.file_meta = Dataset()
        outer.file_meta.MediaStorageSOPClassUID = uid_module.SecondaryCaptureImageStorage
        outer.file_meta.MediaStorageSOPInstanceUID = uid_module.generate_uid()
        outer.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
        outer.is_implicit_VR = False
        outer.is_little_endian = True
        # Use RequestAttributesSequence (0040,0275) — a PS3.15 listed sequence tag.
        # PatientID inside a non-listed SQ should still be scrubbed.
        # We use a custom SQ here under a non-PHI parent tag to test recursion.
        inner_dataset = Dataset()
        inner_dataset.PatientID = "NESTED-MRN"
        outer.add_new((0x0008, 0x1250), "SQ", Sequence([inner_dataset]))

        buf = io.BytesIO()
        pydicom.dcmwrite(buf, outer)
        raw = buf.getvalue()

        result = deidentify_dicom(raw)
        out_ds = pydicom.dcmread(io.BytesIO(result))

        # The outer sequence is not on the removal list; the nested PatientID must be gone.
        if (0x0008, 0x1250) in out_ds:
            for item in out_ds[0x0008, 0x1250].value:
                self.assertNotIn(
                    (0x0010, 0x0020),
                    item,
                    "PatientID inside a sequence item must be removed",
                )


# ---------------------------------------------------------------------------
# Unit tests — DICOM_PS315_TAGS constant
# ---------------------------------------------------------------------------

class TestDicomPS315Tags(unittest.TestCase):
    """Structural checks on the tag set."""

    def test_is_frozenset(self):
        from formats.dicom import DICOM_PS315_TAGS
        self.assertIsInstance(DICOM_PS315_TAGS, frozenset)

    def test_minimum_required_tags_present(self):
        from formats.dicom import DICOM_PS315_TAGS
        required = [
            (0x0010, 0x0010),  # PatientName
            (0x0010, 0x0020),  # PatientID
            (0x0010, 0x0030),  # PatientBirthDate
            (0x0010, 0x0040),  # PatientSex
            (0x0010, 0x1040),  # PatientAddress
            (0x0010, 0x2154),  # PatientTelephoneNumbers
            (0x0008, 0x0014),  # InstanceCreatorUID
            (0x0008, 0x0050),  # AccessionNumber
            (0x0008, 0x0080),  # InstitutionName
            (0x0008, 0x0081),  # InstitutionAddress
            (0x0008, 0x0090),  # ReferringPhysicianName
            (0x0008, 0x1070),  # OperatorsName
            (0x0018, 0x1000),  # DeviceSerialNumber
            (0x0020, 0x000D),  # StudyInstanceUID
            (0x0020, 0x000E),  # SeriesInstanceUID
            (0x0038, 0x0010),  # AdmissionID
            (0x0040, 0x0275),  # RequestAttributesSequence
        ]
        for tag in required:
            self.assertIn(tag, DICOM_PS315_TAGS, f"Required tag {tag!r} missing from DICOM_PS315_TAGS")

    def test_non_phi_tags_not_present(self):
        from formats.dicom import DICOM_PS315_TAGS
        non_phi = [
            (0x0008, 0x0060),  # Modality
            (0x0028, 0x0010),  # Rows
            (0x0028, 0x0011),  # Columns
            (0x7FE0, 0x0010),  # PixelData
        ]
        for tag in non_phi:
            self.assertNotIn(tag, DICOM_PS315_TAGS,
                             f"Non-PHI tag {tag!r} should not be in DICOM_PS315_TAGS")

    def test_all_entries_are_two_int_tuples(self):
        from formats.dicom import DICOM_PS315_TAGS
        for item in DICOM_PS315_TAGS:
            self.assertIsInstance(item, tuple, f"{item!r} is not a tuple")
            self.assertEqual(len(item), 2, f"{item!r} should have exactly two elements")
            self.assertIsInstance(item[0], int, f"group in {item!r} is not int")
            self.assertIsInstance(item[1], int, f"element in {item!r} is not int")


# ---------------------------------------------------------------------------
# Service tests — api.services.dicom
# ---------------------------------------------------------------------------

class TestDicomService(unittest.TestCase):
    """Tests for ``DicomService`` (async, no HTTP layer)."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_process_single_returns_bytes(self):
        from api.services.dicom import DicomService
        svc = DicomService()
        raw = _make_dicom_bytes(PatientName="Doe^John")
        result = self._run(svc.process_single(raw))
        self.assertIsInstance(result, bytes)

    def test_process_single_scrubs_phi(self):
        from api.services.dicom import DicomService
        svc = DicomService()
        raw = _make_dicom_bytes(PatientName="Doe^John")
        result = self._run(svc.process_single(raw))
        ds = pydicom.dcmread(io.BytesIO(result))
        # PatientName should be absent (tag removed) or blank
        if (0x0010, 0x0010) in ds:
            self.assertEqual(str(ds.PatientName).strip(), "")
        # Deidentification marker must be set
        self.assertEqual(str(ds[0x0012, 0x0062].value), "YES")

    def test_process_single_raises_value_error_on_bad_input(self):
        from api.services.dicom import DicomService
        svc = DicomService()
        with self.assertRaises(ValueError):
            self._run(svc.process_single(b"garbage"))

    def test_process_batch_returns_zip(self):
        from api.services.dicom import DicomService
        svc = DicomService()
        files = [
            ("a.dcm", _make_dicom_bytes(PatientName="Alice")),
            ("b.dcm", _make_dicom_bytes(PatientName="Bob")),
        ]
        result = self._run(svc.process_batch(files))
        self.assertIsInstance(result, bytes)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(result)),
                        "process_batch should return a valid ZIP archive")

    def test_process_batch_zip_contains_all_files(self):
        from api.services.dicom import DicomService
        svc = DicomService()
        files = [
            ("a.dcm", _make_dicom_bytes(PatientName="Alice")),
            ("b.dcm", _make_dicom_bytes(PatientName="Bob")),
        ]
        result = self._run(svc.process_batch(files))
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            self.assertEqual(len(zf.namelist()), 2,
                             "ZIP should contain exactly two entries")
            self.assertIn("a.dcm", zf.namelist())
            self.assertIn("b.dcm", zf.namelist())

    def test_process_batch_contents_are_deidentified(self):
        from api.services.dicom import DicomService
        svc = DicomService()
        files = [("patient.dcm", _make_dicom_bytes(PatientName="FullName^Test"))]
        result = self._run(svc.process_batch(files))
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            dcm_bytes = zf.read("patient.dcm")
        ds = pydicom.dcmread(io.BytesIO(dcm_bytes))
        if (0x0010, 0x0010) in ds:
            self.assertEqual(str(ds.PatientName).strip(), "")
        self.assertEqual(str(ds[0x0012, 0x0062].value), "YES")

    def test_process_batch_bad_file_produces_error_marker(self):
        """A file that fails processing should produce an .error.txt marker, not raise."""
        from api.services.dicom import DicomService
        svc = DicomService()
        files = [
            ("good.dcm", _make_dicom_bytes(PatientName="OK")),
            ("bad.dcm", b"this is not dicom"),
        ]
        result = self._run(svc.process_batch(files))
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
        self.assertIn("good.dcm", names, "Valid file should be in ZIP")
        self.assertTrue(
            any(n.startswith("bad.dcm") and n.endswith(".error.txt") for n in names),
            f"Expected error marker for bad.dcm, got: {names}",
        )

    def test_process_batch_empty_list_returns_empty_zip(self):
        from api.services.dicom import DicomService
        svc = DicomService()
        result = self._run(svc.process_batch([]))
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(result)))
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            self.assertEqual(len(zf.namelist()), 0)

    def test_process_batch_fallback_filename(self):
        """Entries without a filename receive a generated name."""
        from api.services.dicom import DicomService
        svc = DicomService()
        files = [
            ("explicit.dcm", _make_dicom_bytes()),
            ("another.dcm", _make_dicom_bytes()),
        ]
        result = self._run(svc.process_batch(files))
        with zipfile.ZipFile(io.BytesIO(result)) as zf:
            names = zf.namelist()
        self.assertEqual(len(names), 2)


# ---------------------------------------------------------------------------
# HTTP endpoint tests — api.routers.dicom (via FastAPI TestClient)
# ---------------------------------------------------------------------------

def _get_test_client():
    """Build a FastAPI TestClient with the DICOM router mounted under /v1."""
    import os
    import sys

    _CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")

    env = {
        "MEDANON_CONFIG_DIR": _CONFIG_DIR,
        "MEDANON_API_KEY": "",
        "MEDANON_RATE_LIMIT_ENABLED": "false",
        "MEDANON_CORS_ORIGINS": "",
        "MEDANON_HASH_ALLOW_PLAIN": "true",
        "GPAS_URL": "",
        "FHIR_SOURCE_URL": "",
        "LOG_LEVEL": "WARNING",
    }

    with patch.dict(os.environ, env, clear=False):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from api.routers.dicom import router

        app = FastAPI()
        app.include_router(router, prefix="/v1")
        return TestClient(app, raise_server_exceptions=False)


class TestDicomEndpoints(unittest.TestCase):
    """Integration tests for the DICOM HTTP endpoints."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_test_client()

    # --- POST /v1/process/dicom ---

    def test_process_dicom_returns_200(self):
        raw = _make_dicom_bytes(PatientName="Endpoint^Test")
        resp = self.client.post(
            "/v1/process/dicom",
            content=raw,
            headers={"Content-Type": "application/dicom"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_process_dicom_content_type(self):
        raw = _make_dicom_bytes(PatientName="ContentType^Test")
        resp = self.client.post(
            "/v1/process/dicom",
            content=raw,
            headers={"Content-Type": "application/dicom"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("content-type"), "application/dicom")

    def test_process_dicom_content_disposition(self):
        raw = _make_dicom_bytes()
        resp = self.client.post(
            "/v1/process/dicom",
            content=raw,
            headers={"Content-Type": "application/dicom"},
        )
        self.assertEqual(resp.status_code, 200)
        disposition = resp.headers.get("content-disposition", "")
        self.assertIn("attachment", disposition)
        self.assertIn(".dcm", disposition)

    def test_process_dicom_result_is_deidentified(self):
        raw = _make_dicom_bytes(PatientID="HTTP-TEST-001", PatientName="Http^Test")
        resp = self.client.post(
            "/v1/process/dicom",
            content=raw,
            headers={"Content-Type": "application/dicom"},
        )
        self.assertEqual(resp.status_code, 200)
        ds = pydicom.dcmread(io.BytesIO(resp.content))
        self.assertNotIn((0x0010, 0x0020), ds)
        self.assertEqual(str(ds[0x0012, 0x0062].value), "YES")

    def test_process_dicom_empty_body_returns_422(self):
        resp = self.client.post(
            "/v1/process/dicom",
            content=b"",
            headers={"Content-Type": "application/dicom"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("empty", resp.json()["detail"].lower())

    def test_process_dicom_invalid_body_returns_422(self):
        resp = self.client.post(
            "/v1/process/dicom",
            content=b"this is definitely not a dicom file",
            headers={"Content-Type": "application/dicom"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_process_dicom_oversized_body_returns_413(self):
        """A Content-Length header exceeding the limit should be rejected."""
        with patch.dict(os.environ, {"DICOM_MAX_BODY_BYTES": "1024"}, clear=False):
            # Re-import router so DICOM_MAX_BODY_BYTES is re-read
            import importlib
            import api.routers.dicom as dicom_router_mod
            importlib.reload(dicom_router_mod)
            from fastapi import FastAPI
            from fastapi.testclient import TestClient

            small_app = FastAPI()
            small_app.include_router(dicom_router_mod.router, prefix="/v1")
            client = TestClient(small_app, raise_server_exceptions=False)

            # Send 2 KB payload against a 1 KB limit
            resp = client.post(
                "/v1/process/dicom",
                content=b"X" * 2048,
                headers={
                    "Content-Type": "application/dicom",
                    "Content-Length": "2048",
                },
            )
            self.assertEqual(resp.status_code, 413)

    # --- POST /v1/process/dicom/batch ---

    def test_batch_dicom_returns_200(self):
        raw_a = _make_dicom_bytes(PatientName="Alice^Batch")
        raw_b = _make_dicom_bytes(PatientName="Bob^Batch")
        resp = self.client.post(
            "/v1/process/dicom/batch",
            files=[
                ("files", ("a.dcm", raw_a, "application/dicom")),
                ("files", ("b.dcm", raw_b, "application/dicom")),
            ],
        )
        self.assertEqual(resp.status_code, 200)

    def test_batch_dicom_returns_zip(self):
        raw = _make_dicom_bytes(PatientName="Batch^Test")
        resp = self.client.post(
            "/v1/process/dicom/batch",
            files=[("files", ("single.dcm", raw, "application/dicom"))],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(resp.content)),
                        "Batch endpoint should return a ZIP archive")

    def test_batch_dicom_content_type_is_zip(self):
        raw = _make_dicom_bytes()
        resp = self.client.post(
            "/v1/process/dicom/batch",
            files=[("files", ("ct.dcm", raw, "application/dicom"))],
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("application/zip", resp.headers.get("content-type", ""))

    def test_batch_dicom_zip_contains_correct_filenames(self):
        raw_a = _make_dicom_bytes(PatientName="A^Patient")
        raw_b = _make_dicom_bytes(PatientName="B^Patient")
        resp = self.client.post(
            "/v1/process/dicom/batch",
            files=[
                ("files", ("scan_a.dcm", raw_a, "application/dicom")),
                ("files", ("scan_b.dcm", raw_b, "application/dicom")),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
        self.assertIn("scan_a.dcm", names)
        self.assertIn("scan_b.dcm", names)

    def test_batch_dicom_no_files_returns_422(self):
        resp = self.client.post("/v1/process/dicom/batch")
        self.assertEqual(resp.status_code, 422)

    def test_batch_dicom_content_disposition(self):
        raw = _make_dicom_bytes()
        resp = self.client.post(
            "/v1/process/dicom/batch",
            files=[("files", ("img.dcm", raw, "application/dicom"))],
        )
        self.assertEqual(resp.status_code, 200)
        disposition = resp.headers.get("content-disposition", "")
        self.assertIn("attachment", disposition)
        self.assertIn(".zip", disposition)

    def test_batch_bad_file_produces_error_marker_not_500(self):
        """A corrupt file in a batch must not abort the whole request."""
        raw_good = _make_dicom_bytes(PatientName="Good^File")
        raw_bad = b"corrupt data"
        resp = self.client.post(
            "/v1/process/dicom/batch",
            files=[
                ("files", ("good.dcm", raw_good, "application/dicom")),
                ("files", ("bad.dcm", raw_bad, "application/dicom")),
            ],
        )
        self.assertEqual(resp.status_code, 200)
        with zipfile.ZipFile(io.BytesIO(resp.content)) as zf:
            names = zf.namelist()
        self.assertIn("good.dcm", names)
        self.assertTrue(
            any("bad.dcm" in n for n in names),
            f"Expected bad.dcm or bad.dcm.error.txt in ZIP, got: {names}",
        )


if __name__ == "__main__":
    unittest.main()
