"""Full-pipeline integration test: process → risk → synthetic.

Uses diabetes.json (3 Patient + 9 Observation resources) as realistic input.
Runs with TestClient — no Docker or live FHIR server required.

Chain:
  1. POST Bundle to /process           → de-identified NDJSON
  2. POST de-identified NDJSON to /analyse/risk  → k-anonymity report
  3. POST de-identified patients to /generate/synthetic → synthetic cohort
  4. Verify X-Synthetic-Engine response header
"""

import json
import os
import sys
import types
import typing
import unittest

# ── typing.io shim (fhirpathpy / antlr4 compatibility on Python 3.13) ────────
if "typing.io" not in sys.modules:
    _io_mod = types.ModuleType("typing.io")
    _io_mod.IO = typing.IO
    _io_mod.TextIO = typing.TextIO
    _io_mod.BinaryIO = typing.BinaryIO
    sys.modules["typing.io"] = _io_mod

os.environ["MEDANON_HASH_ALLOW_PLAIN"] = "true"
os.environ["MEDANON_RATE_LIMIT_ENABLED"] = "false"

_TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
_CONFIG_DIR = os.path.join(_TESTS_DIR, "..", "config")
_DIABETES_FILE = os.path.join(_TESTS_DIR, "data", "diabetes.json")


def _get_client():
    env = {
        "MEDANON_CONFIG_DIR": _CONFIG_DIR,
        "MEDANON_API_KEY": "",
        "MEDANON_RATE_LIMIT_ENABLED": "false",
        "MEDANON_HASH_ALLOW_PLAIN": "true",
        "GPAS_URL": "",
        "FHIR_SOURCE_URL": "",
        "LOG_LEVEL": "WARNING",
    }
    with unittest.mock.patch.dict(os.environ, env, clear=False):
        for mod in ("api.auth", "api.main"):
            if mod in sys.modules:
                del sys.modules[mod]
        from api.main import app
        from starlette.testclient import TestClient
        return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# Full-pipeline test — shares processed output across tests via setUpClass
# ---------------------------------------------------------------------------

class TestFullPipeline(unittest.TestCase):
    """Chain: process diabetes bundle → risk → synthetic."""

    @classmethod
    def setUpClass(cls):
        import unittest.mock
        cls.mock = unittest.mock
        cls.client = _get_client()

        with open(_DIABETES_FILE, encoding="utf-8") as f:
            cls.bundle = json.load(f)

        # Run de-identification once; all tests share the result
        resp = cls.client.post(
            "/v1/process",
            content=json.dumps(cls.bundle).encode(),
            headers={"Content-Type": "application/json"},
        )
        cls.process_status = resp.status_code
        cls.process_body = resp.json() if resp.status_code == 200 else {}

        # Build NDJSON of de-identified resources for downstream tests
        if cls.process_status == 200:
            body = cls.process_body
            if body.get("resourceType") == "Bundle":
                resources = [e["resource"] for e in body.get("entry", []) if e.get("resource")]
            else:
                resources = [body]
            cls.deid_ndjson = "\n".join(json.dumps(r) for r in resources).encode()
            cls.deid_patients = [r for r in resources if r.get("resourceType") == "Patient"]
            cls.patients_ndjson = "\n".join(
                json.dumps(r) for r in cls.deid_patients
            ).encode()
        else:
            cls.deid_ndjson = b""
            cls.deid_patients = []
            cls.patients_ndjson = b""

    # ── Test 1: de-identification ────────────────────────────────────────────

    def test_process_bundle_returns_200(self):
        self.assertEqual(self.process_status, 200,
                         f"Expected 200, got {self.process_status}: {self.process_body}")

    def test_process_result_is_bundle(self):
        self.assertEqual(self.process_body.get("resourceType"), "Bundle")

    def test_process_patient_ids_changed(self):
        """All original Patient IDs (DDME, LDME, CDME) must be replaced."""
        original_ids = {"DDME", "LDME", "CDME"}
        for entry in self.process_body.get("entry", []):
            res = entry.get("resource", {})
            if res.get("resourceType") == "Patient":
                self.assertNotIn(
                    res.get("id"), original_ids,
                    f"Patient ID {res.get('id')!r} was not de-identified"
                )

    def test_process_patient_names_removed(self):
        """Patient names must be redacted (field absent or empty)."""
        for entry in self.process_body.get("entry", []):
            res = entry.get("resource", {})
            if res.get("resourceType") == "Patient":
                name = res.get("name")
                self.assertTrue(
                    not name,
                    f"Patient still has name field after de-identification: {name}"
                )

    def test_process_entry_count_preserved(self):
        """Bundle must still contain all 12 resources (3 Patient + 9 Observation)."""
        original_count = len(self.bundle.get("entry", []))
        deid_count = len(self.process_body.get("entry", []))
        self.assertEqual(deid_count, original_count,
                         f"Entry count changed: {original_count} → {deid_count}")

    # ── Test 2: risk assessment ──────────────────────────────────────────────

    def test_risk_returns_200(self):
        if not self.deid_ndjson:
            self.skipTest("No de-identified output from previous step")
        resp = self.client.post(
            "/v1/analyse/risk",
            content=self.deid_ndjson,
            headers={"Content-Type": "application/x-ndjson"},
        )
        self.assertEqual(resp.status_code, 200,
                         f"Risk endpoint returned {resp.status_code}: {resp.text[:200]}")

    def test_risk_response_has_required_keys(self):
        if not self.deid_ndjson:
            self.skipTest("No de-identified output from previous step")
        resp = self.client.post(
            "/v1/analyse/risk",
            content=self.deid_ndjson,
            headers={"Content-Type": "application/x-ndjson"},
        )
        body = resp.json()
        for key in ("groups", "summary"):
            self.assertIn(key, body, f"Risk report missing top-level key {key!r}")
        self.assertIn("min_k", body.get("summary", {}),
                      "Risk report summary missing 'min_k'")

    def test_risk_min_k_is_positive_integer(self):
        if not self.deid_ndjson:
            self.skipTest("No de-identified output from previous step")
        resp = self.client.post(
            "/v1/analyse/risk",
            content=self.deid_ndjson,
            headers={"Content-Type": "application/x-ndjson"},
        )
        min_k = resp.json().get("summary", {}).get("min_k")
        self.assertIsInstance(min_k, int)
        self.assertGreater(min_k, 0)

    # ── Test 3: synthetic generation (stdlib) ───────────────────────────────

    def test_synthetic_stdlib_returns_5_patients(self):
        if not self.patients_ndjson:
            self.skipTest("No de-identified patients from previous step")
        # Wrap de-identified patients as a collection bundle for synthetic endpoint
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": p} for p in self.deid_patients],
        }
        resp = self.client.post(
            "/v1/generate/synthetic?count=5&engine=stdlib&seed=42",
            content=json.dumps(bundle).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200,
                         f"Synthetic endpoint returned {resp.status_code}: {resp.text[:200]}")
        lines = [l for l in resp.text.strip().split("\n") if l]
        self.assertEqual(len(lines), 5, f"Expected 5 synthetic patients, got {len(lines)}")

    def test_synthetic_patients_are_tagged_syn(self):
        if not self.deid_patients:
            self.skipTest("No de-identified patients from previous step")
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": p} for p in self.deid_patients],
        }
        resp = self.client.post(
            "/v1/generate/synthetic?count=3&engine=stdlib&seed=0",
            content=json.dumps(bundle).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        for line in resp.text.strip().split("\n"):
            if not line:
                continue
            resource = json.loads(line)
            tags = resource.get("meta", {}).get("tag", [])
            codes = [t.get("code") for t in tags]
            self.assertIn("SYN", codes,
                          f"Synthetic resource missing SYN tag: {resource.get('id')}")

    def test_synthetic_patient_ids_are_unique(self):
        if not self.deid_patients:
            self.skipTest("No de-identified patients from previous step")
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": p} for p in self.deid_patients],
        }
        resp = self.client.post(
            "/v1/generate/synthetic?count=5&engine=stdlib&seed=1",
            content=json.dumps(bundle).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        ids = [json.loads(l)["id"] for l in resp.text.strip().split("\n") if l]
        self.assertEqual(len(ids), len(set(ids)), "Synthetic patient IDs are not unique")

    # ── Test 4: engine header ────────────────────────────────────────────────

    def test_synthetic_response_has_engine_header(self):
        if not self.deid_patients:
            self.skipTest("No de-identified patients from previous step")
        bundle = {
            "resourceType": "Bundle",
            "type": "collection",
            "entry": [{"resource": p} for p in self.deid_patients],
        }
        resp = self.client.post(
            "/v1/generate/synthetic?count=2&engine=stdlib",
            content=json.dumps(bundle).encode(),
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(resp.status_code, 200)
        engine_header = resp.headers.get("X-Synthetic-Engine")
        self.assertIsNotNone(engine_header,
                             "Response missing X-Synthetic-Engine header")
        self.assertIn(engine_header, ("stdlib", "sdv"),
                      f"Unexpected engine value: {engine_header!r}")


if __name__ == "__main__":
    unittest.main()
