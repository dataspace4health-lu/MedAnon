"""Integration tests for CLI — mocks only HTTP, tests real logic.

These tests verify that the CLI correctly integrates with:
- Config loading and validation (pipeline.config.Settings)
- De-identification pipeline (pipeline.processor.process_data)
- FHIR client (fetch, upload) with real retry/pagination logic
- Error handling and propagation

Pattern: Mock at HTTP layer (urllib3), let ALL real code run.

Contrast with test_cli.py:
- test_cli.py: mocks process_data, FHIR functions → only tests arg parsing
- test_cli_integration.py: mocks HTTP → tests real de-identification logic
"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from cli.main import main


# ---------------------------------------------------------------------------
# HTTP Mock Helpers
# ---------------------------------------------------------------------------

def _mock_response(status: int, body: dict):
    """Create urllib3-compatible HTTPResponse mock."""
    resp = MagicMock()
    resp.status = status
    resp.headers = {"content-type": "application/json"}
    resp.data = json.dumps(body).encode()
    return resp


def _capability_statement():
    """FHIR CapabilityStatement with Patient + Observation."""
    return {
        "resourceType": "CapabilityStatement",
        "rest": [{"mode": "server", "resource": [
            {"type": "Patient"},
            {"type": "Observation"},
        ]}]
    }


def _bundle(resources: list[dict], next_url: str | None = None):
    """Build FHIR Bundle."""
    b = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": r} for r in resources],
    }
    if next_url:
        b["link"] = [{"relation": "next", "url": next_url}]
    return b


# ---------------------------------------------------------------------------
# TestProcessIntegration
# ---------------------------------------------------------------------------

class TestProcessIntegration(unittest.TestCase):
    """Test CLI process command with real de-identification logic."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    def test_process_applies_real_redaction_rule(self):
        """Verify process loads config YAML and applies real redaction."""
        # Create REAL config file with redaction rule
        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("""
rules:
  - match: Patient.name
    action: redact
""")

        # Create input patient WITH name
        input_path = self.tmpdir / "input.json"
        patient = {
            "resourceType": "Patient",
            "id": "p1",
            "name": [{"family": "Smith", "given": ["John"]}],
            "gender": "male"
        }
        input_path.write_text(json.dumps(patient))

        output_path = self.tmpdir / "output.json"

        # Run REAL CLI (no mocking of process_data!)
        main(["process", str(input_path), str(output_path),
              "--config", str(config_path)])

        # Verify REAL redaction happened
        result = json.loads(output_path.read_text())
        self.assertEqual(result["resourceType"], "Patient")
        self.assertEqual(result["id"], "p1")
        self.assertEqual(result["gender"], "male")
        self.assertNotIn("name", result)  # Real redaction removed it!

    def test_process_applies_cryptohash_rule(self):
        """Verify process applies real cryptohash with HMAC key."""
        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("""
rules:
  - match: Patient.id
    action: cryptohash
""")

        input_path = self.tmpdir / "input.json"
        patient = {"resourceType": "Patient", "id": "original-id-123"}
        input_path.write_text(json.dumps(patient))

        output_path = self.tmpdir / "output.json"

        with patch.dict(os.environ, {"MEDANON_HASH_KEY": "test-key-32-bytes-long-for-hmac!"}):
            main(["process", str(input_path), str(output_path),
                  "--config", str(config_path)])

        result = json.loads(output_path.read_text())
        self.assertNotEqual(result["id"], "original-id-123")  # Hash changed it
        self.assertEqual(len(result["id"]), 64)  # SHA256 hex digest

    def test_process_handles_invalid_config(self):
        """Verify process propagates config validation errors correctly."""
        config_path = self.tmpdir / "bad.yaml"
        config_path.write_text("rules: not-a-list")  # Invalid YAML structure

        input_path = self.tmpdir / "input.json"
        input_path.write_text('{"resourceType": "Patient"}')
        output_path = self.tmpdir / "output.json"

        # CLI raises ValueError directly (doesn't wrap in SystemExit)
        with self.assertRaises(ValueError) as ctx:
            main(["process", str(input_path), str(output_path),
                  "--config", str(config_path)])
        self.assertIn("rules must be a list", str(ctx.exception))

    def test_process_handles_malformed_fhir(self):
        """Verify process handles malformed FHIR JSON."""
        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("rules: []")

        input_path = self.tmpdir / "input.json"
        input_path.write_text("{not valid json")
        output_path = self.tmpdir / "output.json"

        # CLI raises JSONDecodeError (doesn't wrap in SystemExit)
        with self.assertRaises(json.JSONDecodeError):
            main(["process", str(input_path), str(output_path),
                  "--config", str(config_path)])

    def test_process_ndjson_streaming(self):
        """Verify process handles NDJSON with multiple lines."""
        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("""
rules:
  - match: Patient.name
    action: redact
""")

        input_path = self.tmpdir / "input.ndjson"
        p1 = {"resourceType": "Patient", "id": "p1", "name": [{"family": "A"}]}
        p2 = {"resourceType": "Patient", "id": "p2", "name": [{"family": "B"}]}
        input_path.write_text(json.dumps(p1) + "\n" + json.dumps(p2) + "\n")

        output_path = self.tmpdir / "output.ndjson"

        main(["process", str(input_path), str(output_path),
              "--config", str(config_path),
              "--input-format", "ndjson",
              "--output-format", "ndjson"])

        lines = output_path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)
        r1 = json.loads(lines[0])
        r2 = json.loads(lines[1])
        self.assertNotIn("name", r1)  # Both redacted
        self.assertNotIn("name", r2)


# ---------------------------------------------------------------------------
# TestFetchIntegration
# ---------------------------------------------------------------------------

class TestFetchIntegration(unittest.TestCase):
    """Test CLI fetch with real FHIR client + de-identification pipeline."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    @patch("integrations.fhir.client._pool.request")
    def test_fetch_calls_real_client_and_deidentifies(self, mock_http):
        """Verify fetch uses real FHIR client AND applies real de-identification."""
        # Mock HTTP responses (FHIR server metadata + patient data)
        capability = _capability_statement()
        patient_bundle = _bundle([{
            "resourceType": "Patient",
            "id": "p1",
            "identifier": [{"system": "SSN", "value": "123-45-6789"}]
        }])

        mock_http.side_effect = [
            _mock_response(200, capability),
            _mock_response(200, patient_bundle),
        ]

        # Create REAL config with cryptohash rule
        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("""
rules:
  - match: Patient.identifier.value
    action: cryptohash
""")
        output_path = self.tmpdir / "output.ndjson"

        # Run REAL CLI with real hash key
        with patch.dict(os.environ, {"MEDANON_HASH_KEY": "test-key-32-bytes-long-for-hmac!"}):
            main(["fetch", "--server", "http://fhir:8080/fhir",
                  "--config", str(config_path),
                  "--output", str(output_path)])

        # Verify real FHIR client was called (metadata + search)
        self.assertEqual(mock_http.call_count, 2)

        # Verify real de-identification happened
        result = json.loads(output_path.read_text().strip())
        self.assertNotEqual(result["identifier"][0]["value"], "123-45-6789")  # Hash changed it!
        self.assertEqual(len(result["identifier"][0]["value"]), 64)  # SHA256 hex

    @patch("integrations.fhir.client._pool.request")
    def test_fetch_handles_server_500_error(self, mock_http):
        """Verify fetch handles server errors gracefully."""
        # FHIR server returns 500 error
        mock_http.return_value = _mock_response(500, {"error": "internal"})

        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("rules: []")
        output_path = self.tmpdir / "output.ndjson"

        with self.assertRaises(SystemExit) as ctx:
            main(["fetch", "--server", "http://fhir:8080/fhir",
                  "--config", str(config_path),
                  "--output", str(output_path)])
        self.assertNotEqual(ctx.exception.code, 0)

    @patch("integrations.fhir.client._pool.request")
    def test_fetch_pagination_works_with_processing(self, mock_http):
        """Verify fetch handles pagination and processes all pages."""
        capability = _capability_statement()
        page1 = _bundle(
            [{"resourceType": "Patient", "id": "p1", "name": [{"family": "A"}]}],
            next_url="http://fhir:8080/fhir?page=2"
        )
        page2 = _bundle([{"resourceType": "Patient", "id": "p2", "name": [{"family": "B"}]}])

        mock_http.side_effect = [
            _mock_response(200, capability),
            _mock_response(200, page1),
            _mock_response(200, page2),
        ]

        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("""
rules:
  - match: Patient.name
    action: redact
""")
        output_path = self.tmpdir / "output.ndjson"

        main(["fetch", "--server", "http://fhir:8080/fhir",
              "--config", str(config_path),
              "--output", str(output_path)])

        # Verify both pages were fetched (metadata + page1 + page2)
        self.assertEqual(mock_http.call_count, 3)

        # Verify both patients de-identified
        lines = output_path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)
        r1, r2 = json.loads(lines[0]), json.loads(lines[1])
        self.assertNotIn("name", r1)
        self.assertNotIn("name", r2)

    @patch("integrations.fhir.client._pool.request")
    def test_fetch_discover_mode_lists_types(self, mock_http):
        """Verify fetch --discover lists available resource types."""
        capability = _capability_statement()
        mock_http.return_value = _mock_response(200, capability)

        output_path = self.tmpdir / "output.txt"

        main(["fetch", "--server", "http://fhir:8080/fhir",
              "--discover",
              "--output", str(output_path)])

        # Verify metadata was called
        self.assertEqual(mock_http.call_count, 1)

        # Verify output contains resource types
        output = output_path.read_text()
        self.assertIn("Patient", output)
        self.assertIn("Observation", output)


# ---------------------------------------------------------------------------
# TestEverythingIntegration
# ---------------------------------------------------------------------------

class TestEverythingIntegration(unittest.TestCase):
    """Test CLI everything with real FHIR client + processor."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    @patch("integrations.fhir.client._pool.request")
    def test_everything_calls_real_client(self, mock_http):
        """Verify everything uses real FHIR $everything client."""
        bundle = _bundle([
            {"resourceType": "Patient", "id": "p1", "name": [{"family": "Smith"}]},
            {"resourceType": "Observation", "id": "obs1", "status": "final"},
        ])
        mock_http.return_value = _mock_response(200, bundle)

        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("""
rules:
  - match: Patient.name
    action: redact
""")
        output_path = self.tmpdir / "output.ndjson"

        main(["everything", "--server", "http://fhir:8080/fhir",
              "--resource-type", "Patient",
              "--id", "p1",
              "--config", str(config_path),
              "--output", str(output_path)])

        # Verify $everything was called
        self.assertEqual(mock_http.call_count, 1)
        call_url = mock_http.call_args[0][1]
        self.assertIn("/$everything", call_url)

        # Verify both resources in output
        lines = output_path.read_text().strip().split("\n")
        self.assertEqual(len(lines), 2)

        # Verify Patient was de-identified (name redacted)
        resources = [json.loads(line) for line in lines]
        patient = [r for r in resources if r["resourceType"] == "Patient"][0]
        self.assertNotIn("name", patient)

    @patch("integrations.fhir.client._pool.request")
    def test_everything_handles_missing_patient(self, mock_http):
        """Verify everything handles 404 not found errors."""
        mock_http.return_value = _mock_response(404, {"issue": [{"severity": "error"}]})

        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("rules: []")
        output_path = self.tmpdir / "output.ndjson"

        # CLI raises ValueError from FHIR client (not SystemExit)
        with self.assertRaises(ValueError) as ctx:
            main(["everything", "--server", "http://fhir:8080/fhir",
                  "--resource-type", "Patient",
                  "--id", "nonexistent",
                  "--config", str(config_path),
                  "--output", str(output_path)])
        self.assertIn("404", str(ctx.exception))


# ---------------------------------------------------------------------------
# TestPushIntegration
# ---------------------------------------------------------------------------

class TestPushIntegration(unittest.TestCase):
    """Test CLI push with real processor + upload integration."""

    def setUp(self):
        self.tmpdir = Path(tempfile.mkdtemp())

    @patch("integrations.fhir.client._pool.request")
    def test_push_processes_then_uploads(self, mock_http):
        """Verify push de-identifies THEN uploads to FHIR server."""
        # Mock a successful batch-response Bundle (upload_resources uses batch Bundles)
        batch_response = {
            "resourceType": "Bundle",
            "type": "batch-response",
            "entry": [
                {"response": {"status": "201 Created", "location": "Patient/server-assigned-1"}},
                {"response": {"status": "201 Created", "location": "Patient/server-assigned-2"}},
            ],
        }
        mock_http.return_value = _mock_response(200, batch_response)

        # Create input with two patients with names
        input_path = self.tmpdir / "input.ndjson"
        p1 = {"resourceType": "Patient", "id": "p1", "name": [{"family": "A"}]}
        p2 = {"resourceType": "Patient", "id": "p2", "name": [{"family": "B"}]}
        input_path.write_text(json.dumps(p1) + "\n" + json.dumps(p2))

        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("""
rules:
  - match: Patient.name
    action: redact
""")

        main(["push", str(input_path),
              "--server", "http://fhir:8080/fhir",
              "--config", str(config_path)])

        # Verify 1 batch upload happened (batch Bundle with 2 entries)
        self.assertEqual(mock_http.call_count, 1)

        # Verify the batch Bundle contains 2 de-identified resources
        uploaded_body = mock_http.call_args[1]["body"]
        uploaded_bundle = json.loads(uploaded_body)
        self.assertEqual(uploaded_bundle["resourceType"], "Bundle")
        self.assertEqual(uploaded_bundle["type"], "batch")
        self.assertEqual(len(uploaded_bundle["entry"]), 2)

        # Verify uploaded resources had names redacted
        for entry in uploaded_bundle["entry"]:
            resource = entry["resource"]
            self.assertNotIn("name", resource)  # Redaction applied before upload!

    @patch("integrations.fhir.client._pool.request")
    def test_push_handles_upload_403(self, mock_http):
        """Verify push handles auth errors gracefully (no crash)."""
        mock_http.return_value = _mock_response(403, {"issue": [{"severity": "error"}]})

        input_path = self.tmpdir / "input.json"
        input_path.write_text('{"resourceType": "Patient", "id": "p1"}')

        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("rules: []")

        # Push should report the error but not crash
        main(["push", str(input_path),
              "--server", "http://fhir:8080/fhir",
              "--config", str(config_path)])

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "2", "FHIR_RETRY_BACKOFF_SEC": "0"})
    @patch("integrations.fhir.client._pool.request")
    def test_push_retries_on_500(self, mock_http):
        """Verify push retries transient server errors."""
        # First upload fails with 500, second succeeds
        mock_http.side_effect = [
            _mock_response(500, {"error": "internal"}),
            _mock_response(500, {"error": "internal"}),
            _mock_response(201, {"resourceType": "Patient", "id": "uploaded"}),
        ]

        input_path = self.tmpdir / "input.json"
        input_path.write_text('{"resourceType": "Patient", "id": "p1"}')

        config_path = self.tmpdir / "config.yaml"
        config_path.write_text("rules: []")

        main(["push", str(input_path),
              "--server", "http://fhir:8080/fhir",
              "--config", str(config_path)])

        # Verify 3 attempts (2 failures + 1 success)
        self.assertEqual(mock_http.call_count, 3)


if __name__ == "__main__":
    unittest.main()
