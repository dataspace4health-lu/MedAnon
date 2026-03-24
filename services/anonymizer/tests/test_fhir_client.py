"""Tests for integrations.fhir.client — FHIR REST client module.

Covers: input validation, SSRF prevention, retry logic, pagination,
read operations (metadata, search, $everything), write operations
(POST/PUT), and bulk upload with per-resource error capture.

Runs locally — no Docker or FHIR server required.
All HTTP calls are mocked via unittest.mock.patch on the module-level _pool.
"""

import json
import os
import sys
import unittest
from unittest.mock import MagicMock, patch, call

# ---------------------------------------------------------------------------
# Environment: disable retries and backoff so tests run instantly.
# These must be set BEFORE the module is imported because _do_request
# reads them at call time via os.environ.get.
# ---------------------------------------------------------------------------
os.environ.setdefault("FHIR_RETRY_COUNT", "0")
os.environ.setdefault("FHIR_RETRY_BACKOFF_SEC", "0")

# Ensure src is on the path (same as other test files in this project)
_SRC = os.path.join(os.path.dirname(__file__), "..", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from integrations.fhir import client as fhir_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(status=200, body=None, headers=None):
    """Create a mock urllib3.HTTPResponse with .status, .data, and .headers."""
    resp = MagicMock()
    resp.status = status
    if body is None:
        body = {}
    if isinstance(body, bytes):
        resp.data = body
    elif isinstance(body, str):
        resp.data = body.encode("utf-8")
    else:
        resp.data = json.dumps(body).encode("utf-8")
    resp.headers = headers or {}
    return resp


def _bundle(entries, next_url=None):
    """Build a minimal FHIR Bundle dict with optional next link."""
    bundle = {
        "resourceType": "Bundle",
        "type": "searchset",
        "entry": [{"resource": e} for e in entries],
        "link": [{"relation": "self", "url": "http://fhir:8080/fhir/Patient"}],
    }
    if next_url:
        bundle["link"].append({"relation": "next", "url": next_url})
    return bundle


# ===========================================================================
# TestValidation
# ===========================================================================

class TestValidation(unittest.TestCase):
    """_validate_resource_type and _validate_resource_id."""

    # -- resource type -------------------------------------------------------

    def test_valid_resource_types(self):
        for rt in ("Patient", "Observation", "MedicationRequest", "Bundle"):
            result = fhir_client._validate_resource_type(rt)
            self.assertEqual(result, rt)

    def test_resource_type_lowercase_rejected(self):
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_type("patient")

    def test_resource_type_with_digit_rejected(self):
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_type("Patient1")

    def test_resource_type_empty_rejected(self):
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_type("")

    def test_resource_type_with_slash_rejected(self):
        """Prevents path traversal in URL construction."""
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_type("Patient/../admin")

    def test_resource_type_with_spaces_rejected(self):
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_type("Patient Resource")

    # -- resource ID ---------------------------------------------------------

    def test_valid_resource_ids(self):
        for rid in ("123", "abc-def", "patient.1", "A_B-C.0"):
            result = fhir_client._validate_resource_id(rid)
            self.assertEqual(result, rid)

    def test_resource_id_with_slash_rejected(self):
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_id("123/456")

    def test_resource_id_with_spaces_rejected(self):
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_id("id with spaces")

    def test_resource_id_empty_rejected(self):
        with self.assertRaises(ValueError):
            fhir_client._validate_resource_id("")

    def test_resource_id_special_chars_rejected(self):
        for bad in ("id;drop", "id&x=1", "id<script>", "id%00"):
            with self.assertRaises(ValueError, msg=f"Should reject {bad!r}"):
                fhir_client._validate_resource_id(bad)


# ===========================================================================
# TestSafeNextUrl
# ===========================================================================

class TestSafeNextUrl(unittest.TestCase):
    """SSRF prevention on pagination links."""

    def test_same_origin_passes(self):
        base = "http://fhir:8080/fhir"
        next_url = "http://fhir:8080/fhir/Patient?_count=10000&_offset=200"
        result = fhir_client._safe_next_url(next_url, base)
        self.assertEqual(result, next_url)

    def test_different_host_accepted(self):
        """Different host is accepted — HAPI often returns its server_address origin."""
        base = "http://fhir:8080/fhir"
        next_url = "http://evil-host:8080/fhir/Patient?_count=10000"
        result = fhir_client._safe_next_url(next_url, base)
        self.assertEqual(result, next_url)

    def test_non_http_scheme_rejected(self):
        base = "http://fhir:8080/fhir"
        next_url = "file:///etc/passwd"
        with self.assertRaises(ValueError) as ctx:
            fhir_client._safe_next_url(next_url, base)
        self.assertIn("disallowed scheme", str(ctx.exception))

    def test_different_scheme_accepted_if_http(self):
        """http vs https difference is accepted (both are valid HTTP schemes)."""
        base = "https://fhir:8080/fhir"
        next_url = "http://fhir:8080/fhir/Patient?_count=10000"
        result = fhir_client._safe_next_url(next_url, base)
        self.assertEqual(result, next_url)

    def test_different_port_accepted(self):
        """Different port is accepted — HAPI server_address may use a different port."""
        base = "http://fhir:8080/fhir"
        next_url = "http://fhir:9999/fhir/Patient?_count=10000"
        result = fhir_client._safe_next_url(next_url, base)
        self.assertEqual(result, next_url)

    def test_internal_metadata_ip_accepted(self):
        """Cloud metadata IP is accepted at this layer since scheme is http.

        SSRF protection for private IPs is handled at the API layer (_validate_server_url),
        not during pagination of an already-trusted server's responses.
        """
        base = "http://fhir:8080/fhir"
        next_url = "http://169.254.169.254/latest/meta-data/"
        result = fhir_client._safe_next_url(next_url, base)
        self.assertEqual(result, next_url)

    def test_same_origin_different_path_passes(self):
        base = "http://fhir:8080/fhir"
        next_url = "http://fhir:8080/fhir/Patient?page=2"
        result = fhir_client._safe_next_url(next_url, base)
        self.assertEqual(result, next_url)


# ===========================================================================
# TestDoRequest
# ===========================================================================

class TestDoRequest(unittest.TestCase):
    """_do_request: retry logic, error handling, happy path."""

    def test_successful_200(self):
        body = {"resourceType": "Patient", "id": "1"}
        mock_resp = _mock_response(200, body)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client._do_request(
                "GET", "http://fhir:8080/fhir/Patient/1",
                headers={}, timeout=30, operation="get",
            )
        self.assertEqual(result, body)

    def test_non_retryable_400_fails_immediately(self):
        """A 400 Bad Request should NOT be retried."""
        mock_resp = _mock_response(400, {"issue": "bad request"})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            with self.assertRaises(ValueError) as ctx:
                fhir_client._do_request(
                    "GET", "http://fhir:8080/fhir/Patient/1",
                    headers={}, timeout=30, operation="get",
                )
            self.assertIn("HTTP 400", str(ctx.exception))
            # Called exactly once — no retries for 400
            self.assertEqual(mock_req.call_count, 1)

    def test_non_retryable_404_fails_immediately(self):
        mock_resp = _mock_response(404, {"issue": "not found"})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            with self.assertRaises(ValueError):
                fhir_client._do_request(
                    "GET", "http://fhir:8080/fhir/Patient/1",
                    headers={}, timeout=30, operation="get",
                )
            self.assertEqual(mock_req.call_count, 1)

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "2", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_retry_on_500(self):
        """Server error 500 should be retried up to FHIR_RETRY_COUNT times."""
        fail = _mock_response(500, {"error": "internal"})
        success = _mock_response(200, {"resourceType": "Patient", "id": "1"})
        with patch.object(fhir_client._pool, "request", side_effect=[fail, fail, success]) as mock_req:
            result = fhir_client._do_request(
                "GET", "http://fhir:8080/fhir/Patient/1",
                headers={}, timeout=30, operation="get",
            )
        self.assertEqual(result["id"], "1")
        self.assertEqual(mock_req.call_count, 3)

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "2", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_retry_on_429(self):
        """429 Too Many Requests should be retried."""
        fail = _mock_response(429, {"error": "rate limited"})
        success = _mock_response(200, {"resourceType": "Patient"})
        with patch.object(fhir_client._pool, "request", side_effect=[fail, success]):
            result = fhir_client._do_request(
                "GET", "http://fhir:8080/fhir/Patient",
                headers={}, timeout=30, operation="get",
            )
        self.assertEqual(result["resourceType"], "Patient")

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "2", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_retry_on_502_503_504(self):
        """Gateway errors (502, 503, 504) should all be retried."""
        for status in (502, 503, 504):
            fail = _mock_response(status, {})
            success = _mock_response(200, {"ok": True})
            with patch.object(fhir_client._pool, "request", side_effect=[fail, success]):
                result = fhir_client._do_request(
                    "GET", "http://fhir:8080/fhir/metadata",
                    headers={}, timeout=30, operation="get",
                )
            self.assertTrue(result["ok"], f"Retry failed for HTTP {status}")

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "1", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_retries_exhausted_raises(self):
        """When all retries are exhausted on 500, raises ValueError."""
        fail = _mock_response(500, {})
        with patch.object(fhir_client._pool, "request", return_value=fail) as mock_req:
            with self.assertRaises(ValueError) as ctx:
                fhir_client._do_request(
                    "GET", "http://fhir:8080/fhir/Patient",
                    headers={}, timeout=30, operation="get",
                )
            self.assertIn("HTTP 500", str(ctx.exception))
            # 1 initial + 1 retry = 2 calls
            self.assertEqual(mock_req.call_count, 2)

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "1", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_connection_error_retries(self):
        """urllib3.exceptions.HTTPError triggers retry."""
        import urllib3.exceptions
        success = _mock_response(200, {"ok": True})
        with patch.object(
            fhir_client._pool, "request",
            side_effect=[urllib3.exceptions.HTTPError("conn refused"), success],
        ):
            result = fhir_client._do_request(
                "GET", "http://fhir:8080/fhir/Patient",
                headers={}, timeout=30, operation="get",
            )
        self.assertTrue(result["ok"])

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "1", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_os_error_retries(self):
        """OSError (e.g. DNS failure) triggers retry."""
        success = _mock_response(200, {"ok": True})
        with patch.object(
            fhir_client._pool, "request",
            side_effect=[OSError("Name or service not known"), success],
        ):
            result = fhir_client._do_request(
                "GET", "http://fhir:8080/fhir/Patient",
                headers={}, timeout=30, operation="get",
            )
        self.assertTrue(result["ok"])

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "0", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_connection_error_no_retries_raises(self):
        """With FHIR_RETRY_COUNT=0, connection error raises immediately."""
        import urllib3.exceptions
        with patch.object(
            fhir_client._pool, "request",
            side_effect=urllib3.exceptions.HTTPError("refused"),
        ):
            with self.assertRaises(ValueError) as ctx:
                fhir_client._do_request(
                    "GET", "http://fhir:8080/fhir/Patient",
                    headers={}, timeout=30, operation="get",
                )
            self.assertIn("connection error", str(ctx.exception))


# ===========================================================================
# TestMakeHeaders
# ===========================================================================

class TestMakeHeaders(unittest.TestCase):
    """_make_headers and _make_post_headers."""

    def test_default_headers_no_token(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FHIR_SOURCE_TOKEN", None)
            headers = fhir_client._make_headers()
        self.assertEqual(headers["Accept"], "application/fhir+json")
        self.assertNotIn("Authorization", headers)

    def test_explicit_token(self):
        headers = fhir_client._make_headers(token="my-token")
        self.assertEqual(headers["Authorization"], "Bearer my-token")

    @patch.dict(os.environ, {"FHIR_SOURCE_TOKEN": "env-token"})
    def test_env_token_fallback(self):
        headers = fhir_client._make_headers()
        self.assertEqual(headers["Authorization"], "Bearer env-token")

    def test_explicit_token_overrides_env(self):
        with patch.dict(os.environ, {"FHIR_SOURCE_TOKEN": "env-token"}):
            headers = fhir_client._make_headers(token="explicit-token")
        self.assertEqual(headers["Authorization"], "Bearer explicit-token")

    def test_post_headers_include_content_type(self):
        headers = fhir_client._make_post_headers()
        self.assertEqual(headers["Content-Type"], "application/fhir+json")
        self.assertEqual(headers["Accept"], "application/fhir+json")


# ===========================================================================
# TestGetCapabilityStatement
# ===========================================================================

class TestGetCapabilityStatement(unittest.TestCase):
    """get_capability_statement parses /metadata response."""

    def test_parses_resource_types(self):
        cs = {
            "resourceType": "CapabilityStatement",
            "rest": [
                {
                    "resource": [
                        {"type": "Patient"},
                        {"type": "Observation"},
                        {"type": "Condition"},
                    ]
                }
            ],
        }
        mock_resp = _mock_response(200, cs)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client.get_capability_statement("http://fhir:8080/fhir")
        self.assertEqual(result, ["Patient", "Observation", "Condition"])

    def test_empty_rest_returns_empty(self):
        cs = {"resourceType": "CapabilityStatement", "rest": []}
        mock_resp = _mock_response(200, cs)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client.get_capability_statement("http://fhir:8080/fhir")
        self.assertEqual(result, [])

    def test_missing_rest_returns_empty(self):
        cs = {"resourceType": "CapabilityStatement"}
        mock_resp = _mock_response(200, cs)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client.get_capability_statement("http://fhir:8080/fhir")
        self.assertEqual(result, [])

    def test_url_trailing_slash_stripped(self):
        cs = {"resourceType": "CapabilityStatement", "rest": []}
        mock_resp = _mock_response(200, cs)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            fhir_client.get_capability_statement("http://fhir:8080/fhir/")
        # The URL passed to _pool.request should end with /metadata (no double slash)
        called_url = mock_req.call_args[0][1]
        self.assertEqual(called_url, "http://fhir:8080/fhir/metadata")

    def test_multiple_rest_entries(self):
        """Multiple rest entries (server + client) should all be collected."""
        cs = {
            "resourceType": "CapabilityStatement",
            "rest": [
                {"resource": [{"type": "Patient"}]},
                {"resource": [{"type": "Encounter"}]},
            ],
        }
        mock_resp = _mock_response(200, cs)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client.get_capability_statement("http://fhir:8080/fhir")
        self.assertEqual(result, ["Patient", "Encounter"])


# ===========================================================================
# TestFetchResourceType
# ===========================================================================

class TestFetchResourceType(unittest.TestCase):
    """fetch_resource_type: yields resources, pagination, page limit."""

    def test_yields_resources_from_bundle(self):
        patients = [
            {"resourceType": "Patient", "id": f"p{i}"} for i in range(3)
        ]
        bundle = _bundle(patients)
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = list(fhir_client.fetch_resource_type(
                "http://fhir:8080/fhir", "Patient",
            ))
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["id"], "p0")
        self.assertEqual(result[2]["id"], "p2")

    def test_follows_pagination(self):
        page1_patients = [{"resourceType": "Patient", "id": "p1"}]
        page2_patients = [{"resourceType": "Patient", "id": "p2"}]
        page1 = _bundle(page1_patients, next_url="http://fhir:8080/fhir/Patient?_count=10000&_offset=1")
        page2 = _bundle(page2_patients)
        resp1 = _mock_response(200, page1)
        resp2 = _mock_response(200, page2)
        with patch.object(fhir_client._pool, "request", side_effect=[resp1, resp2]):
            result = list(fhir_client.fetch_resource_type(
                "http://fhir:8080/fhir", "Patient",
            ))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "p1")
        self.assertEqual(result[1]["id"], "p2")

    def test_stops_at_max_pages(self):
        """When page limit is reached, pagination stops."""
        # Create bundles where every page has a next link
        patient = {"resourceType": "Patient", "id": "p1"}
        bundle_with_next = _bundle([patient], next_url="http://fhir:8080/fhir/Patient?page=next")
        mock_resp = _mock_response(200, bundle_with_next)

        with patch.object(fhir_client, "_FHIR_MAX_PAGES", 3):
            with patch.object(fhir_client._pool, "request", return_value=mock_resp):
                result = list(fhir_client.fetch_resource_type(
                    "http://fhir:8080/fhir", "Patient",
                ))
        # 3 pages * 1 resource each = 3 resources
        self.assertEqual(len(result), 3)

    def test_empty_bundle_yields_nothing(self):
        bundle = {"resourceType": "Bundle", "type": "searchset", "entry": [], "link": []}
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = list(fhir_client.fetch_resource_type(
                "http://fhir:8080/fhir", "Patient",
            ))
        self.assertEqual(result, [])

    def test_non_bundle_raises(self):
        """If response is not a Bundle, raise ValueError."""
        mock_resp = _mock_response(200, {"resourceType": "Patient", "id": "1"})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                list(fhir_client.fetch_resource_type(
                    "http://fhir:8080/fhir", "Patient",
                ))
            self.assertIn("Expected Bundle", str(ctx.exception))

    def test_invalid_resource_type_rejected(self):
        with self.assertRaises(ValueError):
            list(fhir_client.fetch_resource_type(
                "http://fhir:8080/fhir", "invalid_type",
            ))

    def test_extra_params_included(self):
        bundle = _bundle([])
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            list(fhir_client.fetch_resource_type(
                "http://fhir:8080/fhir", "Patient",
                params={"_since": "2024-01-01"},
            ))
        called_url = mock_req.call_args[0][1]
        self.assertIn("_since=2024-01-01", called_url)

    def test_no_count_by_default(self):
        """When FHIR_PAGE_SIZE is 0 (default), no _count param is sent."""
        bundle = _bundle([])
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client, "_FHIR_PAGE_SIZE", 0):
            with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
                list(fhir_client.fetch_resource_type(
                    "http://fhir:8080/fhir", "Patient",
                ))
        called_url = mock_req.call_args[0][1]
        self.assertNotIn("_count", called_url)

    def test_page_size_env_sets_count(self):
        """When FHIR_PAGE_SIZE > 0, _count param is included."""
        bundle = _bundle([])
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client, "_FHIR_PAGE_SIZE", 500):
            with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
                list(fhir_client.fetch_resource_type(
                    "http://fhir:8080/fhir", "Patient",
                ))
        called_url = mock_req.call_args[0][1]
        self.assertIn("_count=500", called_url)

    def test_non_http_pagination_link_rejected(self):
        """Pagination link with file:// scheme should raise ValueError."""
        patient = {"resourceType": "Patient", "id": "p1"}
        bundle = _bundle([patient], next_url="file:///etc/passwd")
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                list(fhir_client.fetch_resource_type(
                    "http://fhir:8080/fhir", "Patient",
                ))
            self.assertIn("disallowed scheme", str(ctx.exception))

    def test_entries_without_resource_skipped(self):
        """Entries missing the 'resource' key should be silently skipped."""
        bundle = {
            "resourceType": "Bundle",
            "type": "searchset",
            "entry": [
                {"resource": {"resourceType": "Patient", "id": "p1"}},
                {"search": {"mode": "include"}},  # no "resource" key
                {"resource": {"resourceType": "Patient", "id": "p3"}},
            ],
            "link": [],
        }
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = list(fhir_client.fetch_resource_type(
                "http://fhir:8080/fhir", "Patient",
            ))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "p1")
        self.assertEqual(result[1]["id"], "p3")


# ===========================================================================
# TestFetchEverything
# ===========================================================================

class TestFetchEverything(unittest.TestCase):
    """fetch_everything: $everything operation."""

    def test_yields_resources(self):
        resources = [
            {"resourceType": "Patient", "id": "p1"},
            {"resourceType": "Observation", "id": "o1"},
            {"resourceType": "Condition", "id": "c1"},
        ]
        bundle = _bundle(resources)
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            result = list(fhir_client.fetch_everything(
                "http://fhir:8080/fhir", "Patient", "p1",
            ))
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["resourceType"], "Patient")
        self.assertEqual(result[1]["resourceType"], "Observation")
        # Verify the URL includes $everything
        called_url = mock_req.call_args[0][1]
        self.assertIn("/Patient/p1/$everything", called_url)

    def test_follows_pagination(self):
        page1 = _bundle(
            [{"resourceType": "Patient", "id": "p1"}],
            next_url="http://fhir:8080/fhir?_getpages=xyz&_pageId=2",
        )
        page2 = _bundle([{"resourceType": "Observation", "id": "o1"}])
        resp1 = _mock_response(200, page1)
        resp2 = _mock_response(200, page2)
        with patch.object(fhir_client._pool, "request", side_effect=[resp1, resp2]):
            result = list(fhir_client.fetch_everything(
                "http://fhir:8080/fhir", "Patient", "p1",
            ))
        self.assertEqual(len(result), 2)

    def test_invalid_resource_type_rejected(self):
        with self.assertRaises(ValueError):
            list(fhir_client.fetch_everything(
                "http://fhir:8080/fhir", "bad-type", "p1",
            ))

    def test_invalid_resource_id_rejected(self):
        with self.assertRaises(ValueError):
            list(fhir_client.fetch_everything(
                "http://fhir:8080/fhir", "Patient", "id/../../etc/passwd",
            ))

    def test_params_appended(self):
        bundle = _bundle([])
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            list(fhir_client.fetch_everything(
                "http://fhir:8080/fhir", "Patient", "p1",
                params={"_count": "50"},
            ))
        called_url = mock_req.call_args[0][1]
        self.assertIn("$everything?_count=50", called_url)

    def test_no_count_by_default(self):
        """When FHIR_PAGE_SIZE is 0, $everything URL has no _count."""
        bundle = _bundle([])
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client, "_FHIR_PAGE_SIZE", 0):
            with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
                list(fhir_client.fetch_everything(
                    "http://fhir:8080/fhir", "Patient", "p1",
                ))
        called_url = mock_req.call_args[0][1]
        self.assertIn("/$everything", called_url)
        self.assertNotIn("_count", called_url)

    def test_page_size_env_sets_count(self):
        """When FHIR_PAGE_SIZE > 0, $everything includes _count."""
        bundle = _bundle([])
        mock_resp = _mock_response(200, bundle)
        with patch.object(fhir_client, "_FHIR_PAGE_SIZE", 1000):
            with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
                list(fhir_client.fetch_everything(
                    "http://fhir:8080/fhir", "Patient", "p1",
                ))
        called_url = mock_req.call_args[0][1]
        self.assertIn("_count=1000", called_url)

    def test_non_bundle_raises(self):
        mock_resp = _mock_response(200, {"resourceType": "OperationOutcome"})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                list(fhir_client.fetch_everything(
                    "http://fhir:8080/fhir", "Patient", "p1",
                ))
            self.assertIn("Expected Bundle", str(ctx.exception))

    def test_stops_at_max_pages(self):
        patient = {"resourceType": "Patient", "id": "p1"}
        bundle_with_next = _bundle([patient], next_url="http://fhir:8080/fhir?_getpages=x&page=next")
        mock_resp = _mock_response(200, bundle_with_next)

        with patch.object(fhir_client, "_FHIR_MAX_PAGES", 2):
            with patch.object(fhir_client._pool, "request", return_value=mock_resp):
                result = list(fhir_client.fetch_everything(
                    "http://fhir:8080/fhir", "Patient", "p1",
                ))
        self.assertEqual(len(result), 2)


# ===========================================================================
# TestPostResource
# ===========================================================================

class TestPostResource(unittest.TestCase):
    """post_resource: PUT for resources with id, POST for resources without."""

    def test_put_when_resource_has_id(self):
        resource = {"resourceType": "Patient", "id": "p1", "gender": "male"}
        server_resp = {"resourceType": "Patient", "id": "p1", "gender": "male"}
        mock_resp = _mock_response(200, server_resp)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            result = fhir_client.post_resource("http://fhir:8080/fhir", resource)
        self.assertEqual(result["id"], "p1")
        # Verify PUT method was used
        called_method = mock_req.call_args[0][0]
        self.assertEqual(called_method, "PUT")
        # Verify URL includes resource type and id
        called_url = mock_req.call_args[0][1]
        self.assertEqual(called_url, "http://fhir:8080/fhir/Patient/p1")

    def test_post_when_no_id(self):
        resource = {"resourceType": "Patient", "gender": "female"}
        server_resp = {"resourceType": "Patient", "id": "server-assigned-1"}
        mock_resp = _mock_response(200, server_resp)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            result = fhir_client.post_resource("http://fhir:8080/fhir", resource)
        self.assertEqual(result["id"], "server-assigned-1")
        # Verify POST method was used
        called_method = mock_req.call_args[0][0]
        self.assertEqual(called_method, "POST")
        # Verify URL is just resource type (no id)
        called_url = mock_req.call_args[0][1]
        self.assertEqual(called_url, "http://fhir:8080/fhir/Patient")

    def test_missing_resource_type_raises(self):
        with self.assertRaises(ValueError) as ctx:
            fhir_client.post_resource("http://fhir:8080/fhir", {"id": "1"})
        self.assertIn("missing resourceType", str(ctx.exception))

    def test_invalid_resource_type_raises(self):
        with self.assertRaises(ValueError):
            fhir_client.post_resource(
                "http://fhir:8080/fhir",
                {"resourceType": "bad_type", "id": "1"},
            )

    def test_invalid_resource_id_raises(self):
        with self.assertRaises(ValueError):
            fhir_client.post_resource(
                "http://fhir:8080/fhir",
                {"resourceType": "Patient", "id": "../../etc/passwd"},
            )

    def test_body_is_json_encoded(self):
        resource = {"resourceType": "Observation", "status": "final"}
        mock_resp = _mock_response(200, resource)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            fhir_client.post_resource("http://fhir:8080/fhir", resource)
        # Verify body is JSON bytes
        called_body = mock_req.call_args[1].get("body") or mock_req.call_args[0][3] if len(mock_req.call_args[0]) > 3 else mock_req.call_args[1].get("body")
        parsed = json.loads(called_body)
        self.assertEqual(parsed["resourceType"], "Observation")

    def test_content_type_header_set(self):
        resource = {"resourceType": "Patient", "id": "p1"}
        mock_resp = _mock_response(200, resource)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            fhir_client.post_resource("http://fhir:8080/fhir", resource)
        called_headers = mock_req.call_args[1].get("headers", {})
        self.assertEqual(called_headers.get("Content-Type"), "application/fhir+json")


# ===========================================================================
# TestPostBundle
# ===========================================================================

class TestPostBundle(unittest.TestCase):
    """post_bundle: submit a FHIR Bundle."""

    def test_posts_bundle_to_base_url(self):
        bundle = {
            "resourceType": "Bundle",
            "type": "transaction",
            "entry": [],
        }
        resp_bundle = {"resourceType": "Bundle", "type": "transaction-response", "entry": []}
        mock_resp = _mock_response(200, resp_bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            result = fhir_client.post_bundle("http://fhir:8080/fhir", bundle)
        self.assertEqual(result["resourceType"], "Bundle")
        called_method = mock_req.call_args[0][0]
        self.assertEqual(called_method, "POST")
        called_url = mock_req.call_args[0][1]
        self.assertEqual(called_url, "http://fhir:8080/fhir/")

    def test_non_bundle_raises(self):
        with self.assertRaises(ValueError) as ctx:
            fhir_client.post_bundle(
                "http://fhir:8080/fhir",
                {"resourceType": "Patient", "id": "1"},
            )
        self.assertIn("expects a FHIR Bundle", str(ctx.exception))


# ===========================================================================
# TestUploadResources
# ===========================================================================

class TestUploadResources(unittest.TestCase):
    """upload_resources: yields success/error results, never raises."""

    def test_yields_success_results(self):
        resources = [
            {"resourceType": "Patient", "id": "p1"},
            {"resourceType": "Observation", "id": "o1"},
        ]
        # Each post_resource call returns the resource
        resp1 = _mock_response(200, {"resourceType": "Patient", "id": "p1"})
        resp2 = _mock_response(200, {"resourceType": "Observation", "id": "o1"})
        with patch.object(fhir_client._pool, "request", side_effect=[resp1, resp2]):
            results = list(fhir_client.upload_resources(
                "http://fhir:8080/fhir", resources,
            ))
        self.assertEqual(len(results), 2)
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["resourceType"], "Patient")
        self.assertEqual(results[0]["source_id"], "p1")
        self.assertEqual(results[0]["server_id"], "p1")
        self.assertIsNone(results[0]["error"])
        self.assertTrue(results[1]["success"])

    def test_captures_errors_without_raising(self):
        resources = [
            {"resourceType": "Patient", "id": "p1"},
            {"resourceType": "Patient", "id": "p2"},
        ]
        # First succeeds, second fails with 500
        resp_ok = _mock_response(200, {"resourceType": "Patient", "id": "p1"})
        resp_fail = _mock_response(500, {"error": "internal"})
        with patch.object(fhir_client._pool, "request", side_effect=[resp_ok, resp_fail]):
            results = list(fhir_client.upload_resources(
                "http://fhir:8080/fhir", resources,
            ))
        self.assertEqual(len(results), 2)
        # First resource succeeded
        self.assertTrue(results[0]["success"])
        self.assertEqual(results[0]["server_id"], "p1")
        # Second resource failed but did not raise
        self.assertFalse(results[1]["success"])
        self.assertIsNone(results[1]["server_id"])
        self.assertIn("FHIR server error", results[1]["error"])

    def test_empty_resources_yields_nothing(self):
        results = list(fhir_client.upload_resources(
            "http://fhir:8080/fhir", [],
        ))
        self.assertEqual(results, [])

    def test_missing_resource_type_captured(self):
        """A resource without resourceType should fail gracefully."""
        resources = [{"id": "no-type"}]
        results = list(fhir_client.upload_resources(
            "http://fhir:8080/fhir", resources,
        ))
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["success"])
        self.assertIn("error", results[0]["error"].lower())

    def test_server_assigns_new_id(self):
        """When resource has no id, server assigns one via POST."""
        resources = [{"resourceType": "Patient", "gender": "male"}]
        resp = _mock_response(200, {"resourceType": "Patient", "id": "server-new-1"})
        with patch.object(fhir_client._pool, "request", return_value=resp):
            results = list(fhir_client.upload_resources(
                "http://fhir:8080/fhir", resources,
            ))
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["success"])
        self.assertIsNone(results[0]["source_id"])
        self.assertEqual(results[0]["server_id"], "server-new-1")


# ===========================================================================
# TestFetchAllResourceTypes
# ===========================================================================

class TestFetchAllResourceTypes(unittest.TestCase):
    """fetch_all_resource_types yields (resource_type, resource) tuples."""

    def test_yields_tuples_for_multiple_types(self):
        patient_bundle = _bundle([{"resourceType": "Patient", "id": "p1"}])
        obs_bundle = _bundle([{"resourceType": "Observation", "id": "o1"}])
        resp1 = _mock_response(200, patient_bundle)
        resp2 = _mock_response(200, obs_bundle)
        with patch.object(fhir_client._pool, "request", side_effect=[resp1, resp2]):
            result = list(fhir_client.fetch_all_resource_types(
                "http://fhir:8080/fhir",
                ["Patient", "Observation"],
            ))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0], ("Patient", {"resourceType": "Patient", "id": "p1"}))
        self.assertEqual(result[1], ("Observation", {"resourceType": "Observation", "id": "o1"}))


# ===========================================================================
# TestFetchCohort
# ===========================================================================

class TestFetchCohort(unittest.TestCase):
    """fetch_cohort: condition-based cohort export via search + $everything."""

    def _condition(self, cid, patient_id):
        return {
            "resourceType": "Condition",
            "id": cid,
            "code": {"coding": [{"system": "http://hl7.org/fhir/sid/icd-10", "code": "E11"}]},
            "subject": {"reference": f"Patient/{patient_id}"},
        }

    def test_finds_patients_and_yields_everything(self):
        """Search Conditions → extract patient IDs → $everything per patient."""
        # Phase 1: Condition search returns 2 conditions for 2 different patients
        conditions = [self._condition("c1", "p1"), self._condition("c2", "p2")]
        condition_bundle = _bundle(conditions)

        # Phase 2: $everything for p1, then p2
        p1_resources = [
            {"resourceType": "Patient", "id": "p1"},
            {"resourceType": "Observation", "id": "o1"},
        ]
        p2_resources = [
            {"resourceType": "Patient", "id": "p2"},
        ]
        p1_bundle = _bundle(p1_resources)
        p2_bundle = _bundle(p2_resources)

        responses = [
            _mock_response(200, condition_bundle),  # search Conditions
            _mock_response(200, p1_bundle),          # $everything Patient/p1
            _mock_response(200, p2_bundle),          # $everything Patient/p2
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses):
            result = list(fhir_client.fetch_cohort(
                "http://fhir:8080/fhir", "Condition", {"code": "E11"},
            ))
        self.assertEqual(len(result), 3)
        resource_ids = [r["id"] for r in result]
        self.assertIn("p1", resource_ids)
        self.assertIn("o1", resource_ids)
        self.assertIn("p2", resource_ids)

    def test_deduplicates_patients(self):
        """Multiple conditions for the same patient → $everything called only once."""
        conditions = [self._condition("c1", "p1"), self._condition("c2", "p1")]
        condition_bundle = _bundle(conditions)
        p1_bundle = _bundle([{"resourceType": "Patient", "id": "p1"}])

        responses = [
            _mock_response(200, condition_bundle),
            _mock_response(200, p1_bundle),
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses) as mock_req:
            result = list(fhir_client.fetch_cohort(
                "http://fhir:8080/fhir", "Condition", {"code": "E11"},
            ))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "p1")
        # Only 2 HTTP calls: 1 condition search + 1 $everything (not 2)
        self.assertEqual(mock_req.call_count, 2)

    def test_no_matching_conditions_yields_nothing(self):
        empty_bundle = _bundle([])
        mock_resp = _mock_response(200, empty_bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = list(fhir_client.fetch_cohort(
                "http://fhir:8080/fhir", "Condition", {"code": "NONEXISTENT"},
            ))
        self.assertEqual(result, [])

    def test_conditions_without_subject_skipped(self):
        """Conditions missing subject.reference are silently skipped."""
        conditions = [
            {"resourceType": "Condition", "id": "c1"},  # no subject
            self._condition("c2", "p1"),
        ]
        condition_bundle = _bundle(conditions)
        p1_bundle = _bundle([{"resourceType": "Patient", "id": "p1"}])

        responses = [
            _mock_response(200, condition_bundle),
            _mock_response(200, p1_bundle),
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses):
            result = list(fhir_client.fetch_cohort(
                "http://fhir:8080/fhir", "Condition", {"code": "E11"},
            ))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["id"], "p1")

    def test_passes_search_params_to_condition_search(self):
        """Search params are forwarded to the Condition search query."""
        empty_bundle = _bundle([])
        mock_resp = _mock_response(200, empty_bundle)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            list(fhir_client.fetch_cohort(
                "http://fhir:8080/fhir", "Condition",
                {"code": "http://snomed.info/sct|73211009", "clinical-status": "active"},
            ))
        called_url = mock_req.call_args[0][1]
        self.assertIn("Condition", called_url)
        self.assertIn("clinical-status=active", called_url)

    def test_invalid_search_type_rejected(self):
        with self.assertRaises(ValueError):
            list(fhir_client.fetch_cohort(
                "http://fhir:8080/fhir", "bad-type", {"code": "E11"},
            ))


# ===========================================================================
# TestDoRawRequest
# ===========================================================================

class TestDoRawRequest(unittest.TestCase):
    """_do_raw_request: returns raw response, handles 202, retries."""

    def test_returns_raw_response_on_200(self):
        mock_resp = _mock_response(200, {"ok": True})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client._do_raw_request(
                "GET", "http://fhir:8080/fhir/Patient",
                headers={}, timeout=30, operation="test",
            )
        self.assertEqual(result.status, 200)

    def test_returns_raw_response_on_202(self):
        """202 Accepted is valid for bulk export — should NOT raise."""
        mock_resp = _mock_response(202, "", headers={"Content-Location": "http://fhir:8080/status"})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client._do_raw_request(
                "GET", "http://fhir:8080/fhir/$export",
                headers={}, timeout=30, operation="bulk_kickoff",
            )
        self.assertEqual(result.status, 202)
        self.assertEqual(result.headers["Content-Location"], "http://fhir:8080/status")

    @patch.dict(os.environ, {"FHIR_RETRY_COUNT": "1", "FHIR_RETRY_BACKOFF_SEC": "0"})
    def test_retries_on_500_then_succeeds(self):
        fail = _mock_response(500, {})
        success = _mock_response(200, {"ok": True})
        with patch.object(fhir_client._pool, "request", side_effect=[fail, success]) as mock_req:
            result = fhir_client._do_raw_request(
                "GET", "http://fhir:8080/fhir/test",
                headers={}, timeout=30,
            )
        self.assertEqual(result.status, 200)
        self.assertEqual(mock_req.call_count, 2)

    def test_non_retryable_400_raises(self):
        mock_resp = _mock_response(400, {})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                fhir_client._do_raw_request(
                    "GET", "http://fhir:8080/fhir/test",
                    headers={}, timeout=30,
                )
            self.assertIn("HTTP 400", str(ctx.exception))


# ===========================================================================
# TestPollBulkStatus
# ===========================================================================

class TestPollBulkStatus(unittest.TestCase):
    """_poll_bulk_status: polling loop for bulk export."""

    def test_immediate_completion(self):
        manifest = {"output": [{"type": "Patient", "url": "http://fhir:8080/binary/1"}]}
        mock_resp = _mock_response(200, manifest)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = fhir_client._poll_bulk_status("http://fhir:8080/status/1")
        self.assertEqual(result["output"][0]["type"], "Patient")

    @patch("time.sleep")
    def test_polls_until_complete(self, mock_sleep):
        pending1 = _mock_response(202, "")
        pending2 = _mock_response(202, "")
        manifest = {"output": []}
        done = _mock_response(200, manifest)
        with patch.object(fhir_client._pool, "request", side_effect=[pending1, pending2, done]):
            result = fhir_client._poll_bulk_status("http://fhir:8080/status/1")
        self.assertEqual(result, manifest)
        self.assertEqual(mock_sleep.call_count, 2)

    @patch("time.sleep")
    def test_honors_retry_after_header(self, mock_sleep):
        pending = _mock_response(202, "", headers={"Retry-After": "10"})
        manifest = {"output": []}
        done = _mock_response(200, manifest)
        with patch.object(fhir_client._pool, "request", side_effect=[pending, done]):
            fhir_client._poll_bulk_status("http://fhir:8080/status/1")
        mock_sleep.assert_called_with(10)

    @patch("time.sleep")
    def test_clamps_retry_after(self, mock_sleep):
        """Retry-After > 120 gets clamped."""
        pending = _mock_response(202, "", headers={"Retry-After": "999"})
        manifest = {"output": []}
        done = _mock_response(200, manifest)
        with patch.object(fhir_client._pool, "request", side_effect=[pending, done]):
            fhir_client._poll_bulk_status("http://fhir:8080/status/1")
        mock_sleep.assert_called_with(120)

    def test_timeout_exceeded_raises(self):
        pending = _mock_response(202, "")
        with patch.object(fhir_client, "_FHIR_BULK_POLL_TIMEOUT", 0):
            with patch.object(fhir_client._pool, "request", return_value=pending):
                with self.assertRaises(ValueError) as ctx:
                    fhir_client._poll_bulk_status("http://fhir:8080/status/1")
                self.assertIn("timeout", str(ctx.exception).lower())

    @patch("time.sleep")
    def test_logs_x_progress(self, mock_sleep):
        pending = _mock_response(202, "", headers={"X-Progress": "50% done"})
        done = _mock_response(200, {"output": []})
        with patch.object(fhir_client._pool, "request", side_effect=[pending, done]):
            with self.assertLogs("medanon.fhir_server", level="INFO") as cm:
                fhir_client._poll_bulk_status("http://fhir:8080/status/1")
        self.assertTrue(any("50% done" in msg for msg in cm.output))


# ===========================================================================
# TestDownloadBulkNdjson
# ===========================================================================

class TestDownloadBulkNdjson(unittest.TestCase):
    """_download_bulk_ndjson: NDJSON file download."""

    def test_yields_resources_from_ndjson(self):
        ndjson = (
            '{"resourceType":"Patient","id":"p1"}\n'
            '{"resourceType":"Patient","id":"p2"}\n'
            '{"resourceType":"Observation","id":"o1"}\n'
        )
        mock_resp = _mock_response(200, ndjson)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = list(fhir_client._download_bulk_ndjson("http://fhir:8080/binary/1"))
        self.assertEqual(len(result), 3)
        self.assertEqual(result[0]["id"], "p1")
        self.assertEqual(result[2]["resourceType"], "Observation")

    def test_skips_empty_lines(self):
        ndjson = '{"resourceType":"Patient","id":"p1"}\n\n\n{"resourceType":"Patient","id":"p2"}\n'
        mock_resp = _mock_response(200, ndjson)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            result = list(fhir_client._download_bulk_ndjson("http://fhir:8080/binary/1"))
        self.assertEqual(len(result), 2)

    def test_http_error_raises(self):
        mock_resp = _mock_response(404, {})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError):
                list(fhir_client._download_bulk_ndjson("http://fhir:8080/binary/1"))

    def test_malformed_json_raises(self):
        ndjson = '{"valid":"json"}\nnot valid json\n'
        mock_resp = _mock_response(200, ndjson)
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                list(fhir_client._download_bulk_ndjson("http://fhir:8080/binary/1"))
            self.assertIn("Malformed NDJSON", str(ctx.exception))


# ===========================================================================
# TestBulkExport
# ===========================================================================

class TestBulkExport(unittest.TestCase):
    """bulk_export: full kick-off → poll → download → cleanup flow."""

    def _kickoff_resp(self, status_url="http://fhir:8080/status/1"):
        return _mock_response(202, "", headers={"Content-Location": status_url})

    def _manifest_resp(self, output_urls=None):
        output = [{"type": "Patient", "url": u} for u in (output_urls or [])]
        return _mock_response(200, {"output": output, "error": []})

    def _ndjson_resp(self, resources):
        lines = [json.dumps(r) for r in resources]
        return _mock_response(200, "\n".join(lines))

    def _delete_resp(self):
        return _mock_response(202, "")

    @patch("time.sleep")
    def test_system_level_full_flow(self, _sleep):
        """System-level export: kickoff → poll → download → cleanup → yields resources."""
        resources = [{"resourceType": "Patient", "id": "p1"}, {"resourceType": "Patient", "id": "p2"}]
        responses = [
            self._kickoff_resp(),                                    # kickoff
            self._manifest_resp(["http://fhir:8080/binary/1"]),     # poll
            self._ndjson_resp(resources),                            # download
            self._delete_resp(),                                     # cleanup
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses):
            result = list(fhir_client.bulk_export("http://fhir:8080/fhir"))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "p1")
        self.assertEqual(result[1]["id"], "p2")

    @patch("time.sleep")
    def test_type_level_export_url(self, _sleep):
        """Type-level export uses {base}/{Type}/$export URL."""
        responses = [
            self._kickoff_resp(),
            self._manifest_resp([]),
            self._delete_resp(),
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses) as mock_req:
            list(fhir_client.bulk_export(
                "http://fhir:8080/fhir", level="type", resource_type="Patient",
            ))
        kickoff_url = mock_req.call_args_list[0][0][1]
        self.assertIn("/Patient/$export", kickoff_url)

    @patch("time.sleep")
    def test_system_level_with_type_filter(self, _sleep):
        responses = [
            self._kickoff_resp(),
            self._manifest_resp([]),
            self._delete_resp(),
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses) as mock_req:
            list(fhir_client.bulk_export(
                "http://fhir:8080/fhir", type_filter="Patient,Observation",
            ))
        kickoff_url = mock_req.call_args_list[0][0][1]
        self.assertIn("_type=Patient%2CObservation", kickoff_url)

    @patch("time.sleep")
    def test_since_parameter_included(self, _sleep):
        responses = [
            self._kickoff_resp(),
            self._manifest_resp([]),
            self._delete_resp(),
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses) as mock_req:
            list(fhir_client.bulk_export(
                "http://fhir:8080/fhir", since="2024-01-01T00:00:00Z",
            ))
        kickoff_url = mock_req.call_args_list[0][0][1]
        self.assertIn("_since=2024-01-01", kickoff_url)

    def test_kickoff_non_202_raises(self):
        mock_resp = _mock_response(400, {"error": "bad request"})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                list(fhir_client.bulk_export("http://fhir:8080/fhir"))
            self.assertIn("HTTP 400", str(ctx.exception))

    def test_kickoff_missing_content_location_raises(self):
        mock_resp = _mock_response(202, "", headers={})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            with self.assertRaises(ValueError) as ctx:
                list(fhir_client.bulk_export("http://fhir:8080/fhir"))
            self.assertIn("Content-Location", str(ctx.exception))

    @patch("time.sleep")
    def test_multiple_output_files(self, _sleep):
        """All output files are downloaded and resources from each are yielded."""
        responses = [
            self._kickoff_resp(),
            self._manifest_resp(["http://fhir:8080/binary/1", "http://fhir:8080/binary/2"]),
            self._ndjson_resp([{"resourceType": "Patient", "id": "p1"}]),
            self._ndjson_resp([{"resourceType": "Observation", "id": "o1"}]),
            self._delete_resp(),
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses):
            result = list(fhir_client.bulk_export("http://fhir:8080/fhir"))
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["resourceType"], "Patient")
        self.assertEqual(result[1]["resourceType"], "Observation")

    @patch("time.sleep")
    def test_cleanup_failure_does_not_raise(self, _sleep):
        """DELETE failure during cleanup should not propagate."""
        responses = [
            self._kickoff_resp(),
            self._manifest_resp(["http://fhir:8080/binary/1"]),
            self._ndjson_resp([{"resourceType": "Patient", "id": "p1"}]),
            _mock_response(500, {}),  # delete fails
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses):
            result = list(fhir_client.bulk_export("http://fhir:8080/fhir"))
        self.assertEqual(len(result), 1)

    def test_type_level_requires_resource_type(self):
        with self.assertRaises(ValueError) as ctx:
            list(fhir_client.bulk_export("http://fhir:8080/fhir", level="type"))
        self.assertIn("resource_type is required", str(ctx.exception))

    def test_type_level_validates_resource_type(self):
        with self.assertRaises(ValueError):
            list(fhir_client.bulk_export(
                "http://fhir:8080/fhir", level="type", resource_type="bad-type",
            ))

    @patch("time.sleep")
    def test_prefer_respond_async_header_sent(self, _sleep):
        """Kick-off must include Prefer: respond-async header."""
        responses = [
            self._kickoff_resp(),
            self._manifest_resp([]),
            self._delete_resp(),
        ]
        with patch.object(fhir_client._pool, "request", side_effect=responses) as mock_req:
            list(fhir_client.bulk_export("http://fhir:8080/fhir"))
        kickoff_headers = mock_req.call_args_list[0][1].get("headers", {})
        self.assertEqual(kickoff_headers.get("Prefer"), "respond-async")


# ===========================================================================
# TestDeleteBulkExport
# ===========================================================================

class TestDeleteBulkExport(unittest.TestCase):
    """delete_bulk_export: cleanup DELETE request."""

    def test_sends_delete(self):
        mock_resp = _mock_response(202, "")
        with patch.object(fhir_client._pool, "request", return_value=mock_resp) as mock_req:
            fhir_client.delete_bulk_export("http://fhir:8080/status/1")
        called_method = mock_req.call_args[0][0]
        self.assertEqual(called_method, "DELETE")

    def test_failure_does_not_raise(self):
        mock_resp = _mock_response(500, {})
        with patch.object(fhir_client._pool, "request", return_value=mock_resp):
            # Should not raise
            fhir_client.delete_bulk_export("http://fhir:8080/status/1")


if __name__ == "__main__":
    unittest.main()
