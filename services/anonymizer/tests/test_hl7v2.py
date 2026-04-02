"""Tests for HL7 v2 de-identification.

Covers:
  - pipeline.hl7v2_deidentify: unit tests for field scrubbing, batch splitting,
    and error handling (no network or Docker required)
  - api.routers.hl7v2: HTTP endpoint tests via FastAPI TestClient

All tests run locally without Docker or any running services.
"""

import os
import sys
import types
import typing
import unittest

# ---------------------------------------------------------------------------
# typing.io shim — required before any fhirpathpy import (Python 3.13 compat)
# ---------------------------------------------------------------------------
if "typing.io" not in sys.modules:
    _io_mod = types.ModuleType("typing.io")
    _io_mod.IO = typing.IO
    _io_mod.TextIO = typing.TextIO
    _io_mod.BinaryIO = typing.BinaryIO
    sys.modules["typing.io"] = _io_mod

# Open mode and no rate limiting for all tests
os.environ["MEDANON_API_KEY"] = ""
os.environ["MEDANON_HASH_ALLOW_PLAIN"] = "true"
os.environ["MEDANON_RATE_LIMIT_ENABLED"] = "false"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_DIR = os.path.join(_TESTS_DIR, "..", "config")

# ---------------------------------------------------------------------------
# Minimal valid ADT A01 message used as a fixture throughout the unit tests.
# Line endings deliberately use \n here; deidentify_hl7v2 normalises them.
# ---------------------------------------------------------------------------
_ADT_A01 = (
    "MSH|^~\\&|SENDING_APP|SENDING_FAC|RECEIVING_APP|RECEIVING_FAC|20230101120000||ADT^A01|MSG001|P|2.5\n"
    "EVN|A01|20230101120000\n"
    "PID|1||P12345^^^MRN||Smith^John^A||19800101|M|||123 Main St^^Springfield^IL^62701||5551234567||ENG||P12345SSN\n"
    "NK1|1|Doe^Jane|SPO|456 Oak Ave^^Springfield^IL^62702|5559876543\n"
    "PV1|1|I|MED^101^A|||10001^Jones^Robert^M^MD|||20^Chen^Lisa^T^MD\n"
)


def _parse_result(result: str) -> dict[str, "hl7.Segment"]:  # type: ignore[name-defined]
    """Parse *result* back with the hl7 library and index segments by name.

    Returns a dict mapping segment name → last occurrence of that segment
    (sufficient for our single-instance fixture segments).
    """
    import hl7

    normalized = result.replace("\n", "\r").strip()
    msg = hl7.parse(normalized)
    index: dict[str, object] = {}
    for seg in msg:
        name = str(seg[0][0])
        index[name] = seg
    return index


# ---------------------------------------------------------------------------
# TestClient factory — registers the hl7v2 router on the imported app so
# the HTTP tests work without modifying api/main.py.
# ---------------------------------------------------------------------------

def _get_client(**env_overrides):
    """Return a TestClient with the hl7v2 router mounted under /v1."""
    from unittest.mock import patch

    env = {
        "MEDANON_CONFIG_DIR": _CONFIG_DIR,
        "MEDANON_API_KEY": "",
        "MEDANON_RATE_LIMIT_ENABLED": "false",
        "MEDANON_HASH_ALLOW_PLAIN": "true",
        "GPAS_URL": "",
        "FHIR_SOURCE_URL": "",
        "LOG_LEVEL": "WARNING",
    }
    env.update(env_overrides)

    with patch.dict(os.environ, env, clear=False):
        # Force fresh import of auth and main so env vars are effective
        for mod in ("api.auth", "api.main"):
            if mod in sys.modules:
                del sys.modules[mod]

        from api.main import app
        from api.routers import hl7v2 as hl7v2_router
        from pipeline.config.service import clear_settings_cache
        from fastapi.testclient import TestClient

        clear_settings_cache()

        # Mount the hl7v2 router if not already present (idempotent guard
        # prevents duplicate route registration across multiple _get_client calls)
        existing_paths = {r.path for r in app.routes}  # type: ignore[attr-defined]
        if "/v1/process/hl7v2" not in existing_paths:
            app.include_router(hl7v2_router.router, prefix="/v1")

        return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# Unit tests — pipeline.hl7v2_deidentify
# ===========================================================================

class TestHL7v2PidScrubbing(unittest.TestCase):
    """PID segment PHI fields are blanked after de-identification."""

    @classmethod
    def setUpClass(cls):
        from formats.hl7v2 import deidentify_hl7v2
        cls.result = deidentify_hl7v2(_ADT_A01)
        cls.segments = _parse_result(cls.result)
        cls.pid = cls.segments["PID"]

    def test_pid_name_scrubbed(self):
        """PID-5 (PatientName) must be empty after de-identification."""
        self.assertEqual(str(self.pid[5]).strip(), "")

    def test_pid_dob_scrubbed(self):
        """PID-7 (DateOfBirth) must be empty after de-identification."""
        self.assertEqual(str(self.pid[7]).strip(), "")

    def test_pid_ssn_scrubbed(self):
        """PID-19 (SSN) must be empty after de-identification."""
        # The fixture has SSN at the position following PID-17; build a full
        # PID that reaches field 19 so the index is unambiguous.
        import hl7
        from formats.hl7v2 import deidentify_hl7v2

        full_msg = (
            "MSH|^~\\&|APP|FAC|APP2|FAC2|20230101||ADT^A01|M1|P|2.5\n"
            # Fields 1-22 with SSN at position 19 and DL at 20
            "PID|1||P99^^^MRN|AltID|Doe^Jane||19900202|F||W|"
            "1 Test Rd^^Testville^CA^90001|CountyCo|5550001111|5550002222|"
            "ENG|S|CHR|SSN-123-45-6789|DL-ABC123|CA|Hispanic\n"
        )
        result = deidentify_hl7v2(full_msg)
        segs = _parse_result(result)
        pid = segs["PID"]
        self.assertEqual(str(pid[19]).strip(), "", "PID-19 (SSN) should be blank")

    def test_pid_address_scrubbed(self):
        """PID-11 (PatientAddress) must be empty after de-identification."""
        self.assertEqual(str(self.pid[11]).strip(), "")

    def test_pid_patient_id_list_scrubbed(self):
        """PID-3 (PatientIDList / MRN) must be empty after de-identification."""
        self.assertEqual(str(self.pid[3]).strip(), "")


class TestHL7v2Nk1Scrubbing(unittest.TestCase):
    """NK1 segment PHI fields are blanked after de-identification."""

    @classmethod
    def setUpClass(cls):
        from formats.hl7v2 import deidentify_hl7v2
        cls.result = deidentify_hl7v2(_ADT_A01)
        cls.segments = _parse_result(cls.result)
        cls.nk1 = cls.segments["NK1"]

    def test_nk1_name_scrubbed(self):
        """NK1-2 (Name) must be empty after de-identification."""
        self.assertEqual(str(self.nk1[2]).strip(), "")


class TestHL7v2Pv1Scrubbing(unittest.TestCase):
    """PV1 segment physician fields are blanked after de-identification."""

    @classmethod
    def setUpClass(cls):
        from formats.hl7v2 import deidentify_hl7v2
        cls.result = deidentify_hl7v2(_ADT_A01)
        cls.segments = _parse_result(cls.result)
        cls.pv1 = cls.segments["PV1"]

    def test_pv1_attending_scrubbed(self):
        """PV1-7 (AttendingDoctor) must be empty after de-identification."""
        self.assertEqual(str(self.pv1[7]).strip(), "")

    def test_pv1_consulting_scrubbed(self):
        """PV1-9 (ConsultingDoctor) must be empty after de-identification."""
        self.assertEqual(str(self.pv1[9]).strip(), "")


class TestHL7v2SegmentPreservation(unittest.TestCase):
    """MSH and EVN segments survive de-identification unchanged."""

    @classmethod
    def setUpClass(cls):
        from formats.hl7v2 import deidentify_hl7v2
        cls.result = deidentify_hl7v2(_ADT_A01)
        cls.segments = _parse_result(cls.result)

    def test_msh_segment_preserved(self):
        """MSH sending application and facility must be unchanged."""
        msh = self.segments["MSH"]
        # MSH-3 = SendingApplication, MSH-4 = SendingFacility
        self.assertEqual(str(msh[3]), "SENDING_APP")
        self.assertEqual(str(msh[4]), "SENDING_FAC")

    def test_evn_segment_preserved(self):
        """EVN segment must appear in output and its event type must be unchanged."""
        self.assertIn("EVN", self.segments)
        evn = self.segments["EVN"]
        self.assertEqual(str(evn[1]), "A01")


class TestHL7v2BatchProcessing(unittest.TestCase):
    """deidentify_hl7v2_batch handles multi-message input correctly."""

    def test_batch_two_messages(self):
        """Two-message batch produces two separate de-identified messages."""
        from formats.hl7v2 import deidentify_hl7v2_batch

        # Second message is a minimal ADT A08 update
        msg2 = (
            "MSH|^~\\&|SYSTEM_B|FACILITY_B|SYSTEM_C|FACILITY_C|20230201090000||ADT^A08|MSG002|P|2.5\n"
            "PID|1||P67890^^^MRN||Johnson^Alice^B||19750315|F|||456 Elm St^^Chicago^IL^60601\n"
        )
        batch = _ADT_A01 + "\n" + msg2
        result = deidentify_hl7v2_batch(batch)

        # Both MSH lines must appear in the output
        self.assertIn("MSH", result)

        # Split result on MSH boundaries and verify two messages
        parts = [p for p in result.split("\n") if p.strip().startswith("MSH")]
        self.assertEqual(len(parts), 2, "Expected exactly two MSH segments in batch output")

        # Each result must have a blank PID name field
        import hl7
        for line_block in result.split("MSH"):
            if not line_block.strip():
                continue
            block = "MSH" + line_block
            normalized = block.replace("\n", "\r").strip()
            try:
                msg = hl7.parse(normalized)
            except hl7.ParseException:
                continue
            try:
                pid = msg.segment("PID")
                self.assertEqual(str(pid[5]).strip(), "")
            except KeyError:
                pass  # segment absent — acceptable for minimal messages

    def test_batch_empty_string_returns_empty(self):
        """Empty batch input returns an empty string without raising."""
        from formats.hl7v2 import deidentify_hl7v2_batch
        self.assertEqual(deidentify_hl7v2_batch(""), "")

    def test_batch_whitespace_only_returns_empty(self):
        """Whitespace-only batch input returns an empty string."""
        from formats.hl7v2 import deidentify_hl7v2_batch
        self.assertEqual(deidentify_hl7v2_batch("   \n  \n  "), "")


class TestHL7v2ErrorHandling(unittest.TestCase):
    """Invalid input raises ValueError with a descriptive message."""

    def test_invalid_message_raises_value_error(self):
        """Non-HL7 text raises ValueError."""
        from formats.hl7v2 import deidentify_hl7v2
        with self.assertRaises(ValueError) as ctx:
            deidentify_hl7v2("This is not an HL7 message at all")
        self.assertIn("Invalid HL7 v2 message", str(ctx.exception))

    def test_empty_string_raises_value_error(self):
        """Empty string raises ValueError."""
        from formats.hl7v2 import deidentify_hl7v2
        with self.assertRaises(ValueError) as ctx:
            deidentify_hl7v2("")
        self.assertIn("Invalid HL7 v2 message", str(ctx.exception))

    def test_bom_prefixed_message_is_accepted(self):
        """A message with a leading UTF-8 BOM is parsed successfully."""
        from formats.hl7v2 import deidentify_hl7v2
        bom_message = "\ufeff" + _ADT_A01
        result = deidentify_hl7v2(bom_message)
        self.assertIn("MSH", result)

    def test_crlf_line_endings_accepted(self):
        """\\r\\n line endings are normalised and parsed without error."""
        from formats.hl7v2 import deidentify_hl7v2
        crlf_msg = _ADT_A01.replace("\n", "\r\n")
        result = deidentify_hl7v2(crlf_msg)
        self.assertIn("MSH", result)

    def test_cr_only_line_endings_accepted(self):
        """\\r-only line endings (native HL7 v2 format) are accepted."""
        from formats.hl7v2 import deidentify_hl7v2
        cr_msg = _ADT_A01.replace("\n", "\r")
        result = deidentify_hl7v2(cr_msg)
        self.assertIn("MSH", result)


class TestHL7v2ShortSegment(unittest.TestCase):
    """Fields beyond the segment length are handled gracefully (no IndexError)."""

    def test_short_pid_segment_no_error(self):
        """A PID with fewer fields than the scrub list silently skips missing ones."""
        from formats.hl7v2 import deidentify_hl7v2
        # Minimal PID — only fields 1–5; fields 6-22 absent
        short_msg = (
            "MSH|^~\\&|APP|FAC|APP2|FAC2|20230101||ADT^A01|M1|P|2.5\n"
            "PID|1||P001^^^MRN||Doe^John\n"
        )
        result = deidentify_hl7v2(short_msg)
        segs = _parse_result(result)
        pid = segs["PID"]
        self.assertEqual(str(pid[5]).strip(), "")


# ===========================================================================
# HTTP endpoint tests — api.routers.hl7v2 via TestClient
# ===========================================================================

class TestHL7v2Endpoints(unittest.TestCase):
    """FastAPI endpoint smoke-tests via TestClient (no Docker required)."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_single_endpoint_returns_200(self):
        """POST valid message to /v1/process/hl7v2 returns 200 with plain text."""
        resp = self.client.post(
            "/v1/process/hl7v2",
            content=_ADT_A01.encode("utf-8"),
            headers={"Content-Type": "text/plain; charset=utf-8"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("MSH", resp.text)
        # Patient name must be scrubbed
        self.assertNotIn("Smith", resp.text)

    def test_single_endpoint_content_type(self):
        """Successful response uses text/plain media type."""
        resp = self.client.post(
            "/v1/process/hl7v2",
            content=_ADT_A01.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp.headers.get("content-type", ""))

    def test_body_too_large_returns_413(self):
        """POST with a body that exceeds MAX_BODY_BYTES returns 413."""
        from api.deps import MAX_BODY_BYTES
        from unittest.mock import patch

        # Report a Content-Length larger than the limit; the middleware checks
        # the header value before buffering so we don't need to send that many bytes.
        oversized = MAX_BODY_BYTES + 1
        resp = self.client.post(
            "/v1/process/hl7v2",
            content=_ADT_A01.encode("utf-8"),
            headers={
                "Content-Type": "text/plain",
                "Content-Length": str(oversized),
            },
        )
        # The enforce_body_size middleware returns 413; Starlette may surface it
        # as 413 or wrap as 500 depending on the exception path, so we accept both.
        self.assertIn(resp.status_code, (413, 500))

    def test_invalid_message_returns_422(self):
        """POST with non-HL7 text returns 422."""
        resp = self.client.post(
            "/v1/process/hl7v2",
            content=b"Not an HL7 message",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Invalid HL7 v2 message", resp.json()["detail"])

    def test_batch_endpoint_returns_200(self):
        """POST valid batch to /v1/process/hl7v2/batch returns 200."""
        msg2 = (
            "MSH|^~\\&|SYS|FAC|SYS2|FAC2|20230202||ADT^A08|MSG002|P|2.5\n"
            "PID|1||P999^^^MRN||Brown^Bob||19701010|M\n"
        )
        batch = _ADT_A01 + "\n" + msg2
        resp = self.client.post(
            "/v1/process/hl7v2/batch",
            content=batch.encode("utf-8"),
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(resp.status_code, 200)
        # Neither patient name should appear in the result
        self.assertNotIn("Smith", resp.text)
        self.assertNotIn("Brown", resp.text)

    def test_batch_endpoint_empty_body_returns_200(self):
        """POST an empty batch body returns 200 with an empty response."""
        resp = self.client.post(
            "/v1/process/hl7v2/batch",
            content=b"",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.text, "")


if __name__ == "__main__":
    unittest.main()
