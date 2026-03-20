"""Tests for the FastAPI main API (api/main.py).

Covers: /health, /ready, /metrics, /process, /process/raw, /process/ndjson,
/process/batch, /analyse/risk, /generate/synthetic, body-size enforcement,
API key auth, request-id propagation, SSRF validation, and dynamic settings.

Runs locally — no Docker required. Uses FastAPI TestClient with mocked
config so fhirpathpy (and the real YAML loader) are exercised.
"""

import json
import os
import unittest
from unittest.mock import patch, MagicMock

from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Bootstrap: ensure the typing.io shim is loaded before fhirpathpy
# ---------------------------------------------------------------------------
import sys, types, typing  # noqa: E401
if "typing.io" not in sys.modules:
    _io_mod = types.ModuleType("typing.io")
    _io_mod.IO = typing.IO
    _io_mod.TextIO = typing.TextIO
    _io_mod.BinaryIO = typing.BinaryIO
    sys.modules["typing.io"] = _io_mod

# Point config dir at the real config directory so Settings resolves correctly
_CONFIG_DIR = os.path.join(os.path.dirname(__file__), "..", "config")


def _get_client(**env_overrides):
    """Import the app with a clean env and return a TestClient."""
    env = {
        "MEDANON_CONFIG_DIR": _CONFIG_DIR,
        "MEDANON_API_KEY": "",
        "MEDANON_RATE_LIMIT_ENABLED": "false",
        "MEDANON_CORS_ORIGINS": "",
        "GPAS_URL": "",
        "FHIR_SOURCE_URL": "",
        "LOG_LEVEL": "WARNING",
    }
    env.update(env_overrides)
    with patch.dict(os.environ, env, clear=False):
        # Force re-import so env vars are picked up fresh
        if "api.main" in sys.modules:
            del sys.modules["api.main"]
        from api.main import app, get_settings
        # Clear the lru_cache on get_settings so it reads from the test config dir
        get_settings.cache_clear()
        return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SIMPLE_PATIENT = {
    "resourceType": "Patient",
    "id": "test-1",
    "name": [{"family": "Doe", "given": ["John"]}],
    "birthDate": "1990-05-15",
}

_SIMPLE_OBSERVATION = {
    "resourceType": "Observation",
    "id": "obs-1",
    "status": "final",
    "code": {"coding": [{"system": "http://loinc.org", "code": "1234-5"}]},
}

_BUNDLE = {
    "resourceType": "Bundle",
    "type": "collection",
    "entry": [
        {"resource": _SIMPLE_PATIENT},
        {"resource": _SIMPLE_OBSERVATION},
    ],
}


class TestHealthAndReady(unittest.TestCase):
    """Tests for /health, /ready, and /metrics."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_health_returns_200(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"status": "ok"})

    def test_ready_no_upstreams(self):
        """With no GPAS_URL or FHIR_SOURCE_URL, ready returns 200 + ready=true."""
        resp = self.client.get("/ready")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertTrue(body["ready"])

    def test_metrics_endpoint(self):
        resp = self.client.get("/metrics")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("medanon_requests_total", resp.text)

    def test_root_redirects_to_docs(self):
        resp = self.client.get("/", follow_redirects=False)
        self.assertIn(resp.status_code, (302, 307))
        self.assertIn("/docs", resp.headers.get("location", ""))


class TestProcessEndpoint(unittest.TestCase):
    """Tests for POST /process."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_process_single_patient(self):
        resp = self.client.post("/process", json=_SIMPLE_PATIENT)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["resourceType"], "Patient")
        # id should be cryptohashed (64 hex chars for sha3_256)
        self.assertEqual(len(body["id"]), 64)
        # name should be redacted (removed)
        self.assertNotIn("name", body)

    def test_process_bundle(self):
        resp = self.client.post("/process", json=_BUNDLE)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["resourceType"], "Bundle")
        self.assertEqual(len(body["entry"]), 2)
        # Patient name redacted
        self.assertNotIn("name", body["entry"][0]["resource"])

    def test_process_invalid_body(self):
        resp = self.client.post("/process", content=b'"just a string"',
                                headers={"Content-Type": "application/json"})
        self.assertEqual(resp.status_code, 422)

    def test_process_observation(self):
        resp = self.client.post("/process", json=_SIMPLE_OBSERVATION)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["resourceType"], "Observation")
        self.assertEqual(len(body["id"]), 64)


class TestProcessRawEndpoint(unittest.TestCase):
    """Tests for POST /process/raw."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_raw_json_in_json_out(self):
        resp = self.client.post(
            "/process/raw?output_format=json&input_format=json",
            content=json.dumps(_SIMPLE_PATIENT).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.text)
        self.assertEqual(body["resourceType"], "Patient")
        self.assertEqual(len(body["id"]), 64)

    def test_raw_invalid_json(self):
        resp = self.client.post(
            "/process/raw?input_format=json",
            content=b"not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)


class TestProcessNdjsonEndpoint(unittest.TestCase):
    """Tests for POST /process/ndjson."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_ndjson_stream(self):
        lines = [
            json.dumps(_SIMPLE_PATIENT),
            json.dumps(_SIMPLE_OBSERVATION),
        ]
        body = "\n".join(lines) + "\n"
        resp = self.client.post(
            "/process/ndjson",
            content=body.encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        self.assertEqual(resp.status_code, 200)
        result_lines = [l for l in resp.text.strip().split("\n") if l]
        self.assertEqual(len(result_lines), 2)
        patient = json.loads(result_lines[0])
        self.assertEqual(patient["resourceType"], "Patient")
        self.assertEqual(len(patient["id"]), 64)

    def test_ndjson_skips_comments(self):
        lines = [
            "// this is a comment",
            json.dumps(_SIMPLE_PATIENT),
        ]
        body = "\n".join(lines) + "\n"
        resp = self.client.post(
            "/process/ndjson",
            content=body.encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        self.assertEqual(resp.status_code, 200)
        result_lines = [l for l in resp.text.strip().split("\n") if l]
        self.assertEqual(len(result_lines), 1)

    def test_ndjson_invalid_line_returns_error(self):
        body = "not valid json\n"
        resp = self.client.post(
            "/process/ndjson",
            content=body.encode(),
            headers={"Content-Type": "application/x-ndjson"},
        )
        self.assertEqual(resp.status_code, 200)
        result = json.loads(resp.text.strip())
        self.assertIn("error", result)


class TestProcessBatchEndpoint(unittest.TestCase):
    """Tests for POST /process/batch."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_batch_json_bundle(self):
        resp = self.client.post(
            "/process/batch",
            content=json.dumps(_BUNDLE).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        result_lines = [l for l in resp.text.strip().split("\n") if l]
        self.assertEqual(len(result_lines), 2)

    def test_batch_single_resource(self):
        resp = self.client.post(
            "/process/batch",
            content=json.dumps(_SIMPLE_PATIENT).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        result_lines = [l for l in resp.text.strip().split("\n") if l]
        self.assertEqual(len(result_lines), 1)


class TestAnalyseRisk(unittest.TestCase):
    """Tests for POST /analyse/risk."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_risk_with_patients(self):
        patients = [
            {"resourceType": "Patient", "id": f"p{i}", "gender": "male",
             "birthDate": "1990", "address": [{"postalCode": "123"}]}
            for i in range(5)
        ]
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": p} for p in patients],
        }
        resp = self.client.post(
            "/analyse/risk",
            content=json.dumps(bundle).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        # Risk report uses a "summary" key with k-anonymity metrics
        self.assertIn("summary", body)
        self.assertIn("min_k", body["summary"])
        self.assertEqual(body["summary"]["min_k"], 5)

    def test_risk_invalid_input(self):
        resp = self.client.post(
            "/analyse/risk",
            content=b"not valid",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)


class TestGenerateSynthetic(unittest.TestCase):
    """Tests for POST /generate/synthetic."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_synthetic_generation(self):
        patients = [
            {"resourceType": "Patient", "id": f"p{i}", "gender": "male",
             "birthDate": "1990-01-01", "address": [{"postalCode": "12345"}]}
            for i in range(3)
        ]
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": p} for p in patients],
        }
        resp = self.client.post(
            "/generate/synthetic?count=5&seed=42",
            content=json.dumps(bundle).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        result_lines = [l for l in resp.text.strip().split("\n") if l]
        self.assertEqual(len(result_lines), 5)
        first = json.loads(result_lines[0])
        self.assertEqual(first["resourceType"], "Patient")
        # Check SYN tag
        tags = first.get("meta", {}).get("tag", [])
        syn_codes = [t["code"] for t in tags if t.get("code") == "SYN"]
        self.assertTrue(len(syn_codes) > 0, "Synthetic patient should have SYN tag")

    def test_synthetic_no_patients_returns_422(self):
        obs = {"resourceType": "Observation", "id": "o1", "status": "final"}
        resp = self.client.post(
            "/generate/synthetic?count=5",
            content=json.dumps(obs).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)


class TestBodySizeEnforcement(unittest.TestCase):
    """Test the body-size middleware rejects oversized requests."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_oversized_content_length_rejected(self):
        resp = self.client.post(
            "/process",
            content=b'{"resourceType":"Patient"}',
            headers={
                "Content-Type": "application/json",
                "Content-Length": str(20 * 1024 * 1024),  # 20 MB
            },
        )
        # Middleware raises HTTPException(413); Starlette may surface as 413 or 500
        self.assertIn(resp.status_code, (413, 500))


class TestApiKeyAuth(unittest.TestCase):
    """Test API key middleware."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client(MEDANON_API_KEY="test-secret-key")

    def test_health_no_auth_required(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_process_requires_api_key(self):
        resp = self.client.post("/process", json=_SIMPLE_PATIENT)
        self.assertEqual(resp.status_code, 401)

    def test_process_with_valid_key(self):
        resp = self.client.post(
            "/process",
            json=_SIMPLE_PATIENT,
            headers={"X-API-Key": "test-secret-key"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_process_with_wrong_key(self):
        resp = self.client.post(
            "/process",
            json=_SIMPLE_PATIENT,
            headers={"X-API-Key": "wrong-key"},
        )
        self.assertEqual(resp.status_code, 401)


class TestRequestId(unittest.TestCase):
    """Test X-Request-ID propagation."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_request_id_echoed(self):
        resp = self.client.get("/health", headers={"X-Request-ID": "abc-123"})
        self.assertEqual(resp.headers.get("X-Request-ID"), "abc-123")

    def test_request_id_generated(self):
        resp = self.client.get("/health")
        rid = resp.headers.get("X-Request-ID")
        self.assertIsNotNone(rid)
        self.assertGreater(len(rid), 0)


class TestSSRFValidation(unittest.TestCase):
    """Test SSRF protection on endpoints that accept server URLs."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_from_server_rejects_private_ip(self):
        resp = self.client.post(
            "/process/from-server",
            content=json.dumps({
                "server_url": "http://127.0.0.1:8080/fhir",
                "resource_types": ["Patient"],
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)
        self.assertIn("private", resp.json()["detail"].lower())

    def test_from_server_rejects_non_http_scheme(self):
        resp = self.client.post(
            "/process/from-server",
            content=json.dumps({
                "server_url": "file:///etc/passwd",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)

    def test_everything_rejects_metadata_ip(self):
        resp = self.client.post(
            "/process/everything",
            content=json.dumps({
                "server_url": "http://169.254.169.254/latest",
                "resource_type": "Patient",
                "resource_id": "1",
            }).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 422)


class TestParametersWrapper(unittest.TestCase):
    """Test the FHIR Parameters dynamic settings wrapper."""

    @classmethod
    def setUpClass(cls):
        cls.client = _get_client()

    def test_parameters_wrapper_unwraps_resource(self):
        payload = {
            "resourceType": "Parameters",
            "parameter": [
                {
                    "name": "resource",
                    "resource": {
                        "resourceType": "Patient",
                        "id": "wrapped-1",
                        "name": [{"family": "Wrapped"}],
                    }
                },
                {
                    "name": "settings",
                    "part": []
                }
            ]
        }
        resp = self.client.post("/process", json=payload)
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["resourceType"], "Patient")
        self.assertEqual(len(body["id"]), 64)

    def test_parameters_rejects_unknown_dynamic_setting(self):
        payload = {
            "resourceType": "Parameters",
            "parameter": [
                {
                    "name": "resource",
                    "resource": {"resourceType": "Patient", "id": "p1"},
                },
                {
                    "name": "settings",
                    "part": [
                        {"name": "evil_setting", "valueString": "bad"}
                    ],
                },
            ],
        }
        resp = self.client.post("/process", json=payload)
        self.assertEqual(resp.status_code, 422)
        self.assertIn("Unsupported dynamic setting", resp.json()["detail"])


if __name__ == "__main__":
    unittest.main()
