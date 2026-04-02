"""Tests for Phase 3 proxy/integration clients.

Covers:
- integrations/analytics/client.py:
  - proxy_analyse_risk: success, HTTP error, connection failure
  - proxy_generate_synthetic: success, HTTP error, connection failure
- integrations/nlp/remote_detector.py:
  - analyze_and_replace_remote: success (scrubbed text + token_state update)
  - graceful fallback on HTTPError, URLError, and generic exception
"""

import json
import os
import sys
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError


_ANALYTICS_URL = "http://analytics:8100"
_NLP_URL = "http://nlp:8200"


class TestProxyAnalyseRisk(unittest.TestCase):
    def _call(self, body: bytes, content_type: str, mock_response: dict):
        """Call proxy_analyse_risk with a mocked urlopen returning mock_response."""
        raw = json.dumps(mock_response).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = raw
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": _ANALYTICS_URL}):
            with patch("integrations.analytics.client._ureq.urlopen", return_value=mock_resp):
                from integrations.analytics.client import proxy_analyse_risk
                return proxy_analyse_risk(body, content_type)

    def test_success_returns_dict(self):
        result = self._call(
            b'{"resourceType":"Patient","id":"p1"}',
            "application/json",
            {"k_min": 2, "l_diversity": 1},
        )
        self.assertEqual(result["k_min"], 2)

    def test_http_error_raises_value_error(self):
        err = HTTPError(
            url=f"{_ANALYTICS_URL}/v1/analyse/risk",
            code=500,
            msg="Internal Server Error",
            hdrs={},
            fp=BytesIO(b"server error detail"),
        )
        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": _ANALYTICS_URL}):
            with patch("integrations.analytics.client._ureq.urlopen", side_effect=err):
                from integrations.analytics.client import proxy_analyse_risk
                with self.assertRaises(ValueError) as ctx:
                    proxy_analyse_risk(b"{}", "application/json")
        self.assertIn("500", str(ctx.exception))

    def test_url_error_raises_value_error(self):
        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": _ANALYTICS_URL}):
            with patch(
                "integrations.analytics.client._ureq.urlopen",
                side_effect=URLError("connection refused"),
            ):
                from integrations.analytics.client import proxy_analyse_risk
                with self.assertRaises(ValueError) as ctx:
                    proxy_analyse_risk(b"{}", "application/json")
        self.assertIn("unreachable", str(ctx.exception))

    def test_uses_analytics_service_url_env(self):
        """The proxy URL is built from ANALYTICS_SERVICE_URL."""
        raw = json.dumps({"k_min": 5}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = raw
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured = {}

        def capture_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": "http://analytics:8100"}):
            with patch("integrations.analytics.client._ureq.urlopen", side_effect=capture_urlopen):
                from integrations.analytics import client as analytics_client
                analytics_client.proxy_analyse_risk(b"{}", "application/json")

        self.assertTrue(captured["url"].startswith("http://analytics:8100"))
        self.assertIn("/v1/analyse/risk", captured["url"])


class TestProxyGenerateSynthetic(unittest.TestCase):
    def _call(self, body: bytes, content_type: str, params: dict, response_bytes: bytes):
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_bytes
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": _ANALYTICS_URL}):
            with patch("integrations.analytics.client._ureq.urlopen", return_value=mock_resp):
                from integrations.analytics.client import proxy_generate_synthetic
                return proxy_generate_synthetic(body, content_type, params)

    def test_success_returns_bytes(self):
        ndjson_line = b'{"resourceType":"Patient","id":"syn-1"}\n'
        result = self._call(b"{}", "application/json", {"count": 5}, ndjson_line)
        self.assertEqual(result, ndjson_line)

    def test_http_error_raises_value_error(self):
        err = HTTPError(
            url=f"{_ANALYTICS_URL}/v1/generate/synthetic",
            code=422,
            msg="Unprocessable Entity",
            hdrs={},
            fp=BytesIO(b"no patients found"),
        )
        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": _ANALYTICS_URL}):
            with patch("integrations.analytics.client._ureq.urlopen", side_effect=err):
                from integrations.analytics.client import proxy_generate_synthetic
                with self.assertRaises(ValueError):
                    proxy_generate_synthetic(b"{}", "application/json", {"count": 1})

    def test_url_error_raises_value_error(self):
        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": _ANALYTICS_URL}):
            with patch(
                "integrations.analytics.client._ureq.urlopen",
                side_effect=URLError("name resolution failed"),
            ):
                from integrations.analytics.client import proxy_generate_synthetic
                with self.assertRaises(ValueError):
                    proxy_generate_synthetic(b"{}", "application/json", {})

    def test_query_string_params_included(self):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b""
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)

        captured = {}

        def capture_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            return mock_resp

        with patch.dict(os.environ, {"ANALYTICS_SERVICE_URL": "http://analytics:8100"}):
            with patch("integrations.analytics.client._ureq.urlopen", side_effect=capture_urlopen):
                from integrations.analytics import client as analytics_client
                analytics_client.proxy_generate_synthetic(
                    b"{}", "application/json", {"count": "10", "seed": "42"}
                )

        self.assertIn("count=10", captured["url"])
        self.assertIn("seed=42", captured["url"])


class TestRemoteNlpDetector(unittest.TestCase):
    def _mock_response(self, scrubbed_text: str, token_state: dict | None = None):
        payload = {"scrubbed_text": scrubbed_text, "token_state": token_state or {}}
        raw = json.dumps(payload).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = raw
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        return mock_resp

    def _call(self, text, mock_resp_or_exc, token_state=None):
        from integrations.nlp.remote_detector import analyze_and_replace_remote
        ts = token_state if token_state is not None else {}
        with patch.dict(os.environ, {"NLP_SERVICE_URL": _NLP_URL}):
            if isinstance(mock_resp_or_exc, Exception):
                with patch("integrations.nlp.remote_detector._ureq.urlopen", side_effect=mock_resp_or_exc):
                    return analyze_and_replace_remote(
                        text, entities=["PERSON"], threshold=0.4,
                        language="en", mode="tokenize", token_state=ts,
                    ), ts
            else:
                with patch("integrations.nlp.remote_detector._ureq.urlopen", return_value=mock_resp_or_exc):
                    return analyze_and_replace_remote(
                        text, entities=["PERSON"], threshold=0.4,
                        language="en", mode="tokenize", token_state=ts,
                    ), ts

    def test_success_returns_scrubbed_text(self):
        mock_resp = self._mock_response("[[PERSON_1]] lives at [[LOCATION_1]]")
        result, _ = self._call("John Doe lives at 123 Main St", mock_resp)
        self.assertEqual(result, "[[PERSON_1]] lives at [[LOCATION_1]]")

    def test_success_updates_token_state_in_place(self):
        returned_state = {"next": {"PERSON": 2}, "map": {"John Doe": "PERSON_1"}, "reverse": {}}
        mock_resp = self._mock_response("[[PERSON_1]]", token_state=returned_state)
        token_state = {}
        self._call("John Doe", mock_resp, token_state=token_state)
        self.assertEqual(token_state.get("map", {}).get("John Doe"), "PERSON_1")

    def test_http_error_returns_original_text(self):
        err = HTTPError(
            url=f"{_NLP_URL}/v1/detect",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=BytesIO(b"not ready"),
        )
        original = "John Doe has hypertension"
        result, _ = self._call(original, err)
        self.assertEqual(result, original)

    def test_url_error_returns_original_text(self):
        original = "Jane Smith"
        result, _ = self._call(original, URLError("Connection refused"))
        self.assertEqual(result, original)

    def test_generic_exception_returns_original_text(self):
        original = "Patient ID 99"
        result, _ = self._call(original, RuntimeError("unexpected"))
        self.assertEqual(result, original)

    def test_request_sent_to_correct_url(self):
        mock_resp = self._mock_response("safe text")
        captured = {}

        def capture_urlopen(req, timeout=None):
            captured["url"] = req.full_url
            captured["method"] = req.method
            return mock_resp

        with patch.dict(os.environ, {"NLP_SERVICE_URL": _NLP_URL}):
            with patch(
                "integrations.nlp.remote_detector._ureq.urlopen",
                side_effect=capture_urlopen,
            ):
                from integrations.nlp import remote_detector as rd
                rd.analyze_and_replace_remote(
                    "John", entities=["PERSON"], threshold=0.4,
                    language="en", mode="tokenize", token_state={},
                )

        self.assertEqual(captured["url"], f"{_NLP_URL}/v1/detect")
        self.assertEqual(captured["method"], "POST")


if __name__ == "__main__":
    unittest.main()
