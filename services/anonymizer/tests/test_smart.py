"""Tests for SMART on FHIR discovery and token introspection."""
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.smart_scopes import _scope_matches, parse_smart_scopes


# ---------------------------------------------------------------------------
# Scope parsing
# ---------------------------------------------------------------------------

class TestParseSMARTScopes:
    def test_full_admin(self):
        assert parse_smart_scopes("user/*.*") == "admin"

    def test_user_write_is_admin(self):
        assert parse_smart_scopes("user/*.write") == "admin"

    def test_user_read_is_analyst(self):
        assert parse_smart_scopes("user/*.read") == "analyst"

    def test_patient_any_is_analyst(self):
        assert parse_smart_scopes("patient/*.*") == "analyst"

    def test_patient_read_is_analyst(self):
        assert parse_smart_scopes("patient/*.read") == "analyst"

    def test_patient_write_is_analyst(self):
        assert parse_smart_scopes("patient/*.write") == "analyst"

    def test_launch_is_viewer(self):
        assert parse_smart_scopes("launch") == "viewer"

    def test_openid_is_viewer(self):
        assert parse_smart_scopes("openid fhirUser") == "viewer"

    def test_highest_role_wins(self):
        assert parse_smart_scopes("launch openid user/*.*") == "admin"

    def test_analyst_beats_viewer(self):
        assert parse_smart_scopes("launch patient/*.read") == "analyst"

    def test_empty_defaults_to_viewer(self):
        assert parse_smart_scopes("") == "viewer"

    def test_unknown_scope_defaults_to_viewer(self):
        assert parse_smart_scopes("system/*.read") == "viewer"

    def test_custom_scope_map_from_env(self, monkeypatch):
        monkeypatch.setenv("SMART_SCOPE_ROLE_MAP", '{"system/*.read": "analyst"}')
        assert parse_smart_scopes("system/*.read") == "analyst"

    def test_malformed_env_falls_back_to_defaults(self, monkeypatch):
        monkeypatch.setenv("SMART_SCOPE_ROLE_MAP", "not-json")
        assert parse_smart_scopes("user/*.*") == "admin"

    def test_custom_map_takes_precedence_over_defaults(self, monkeypatch):
        monkeypatch.setenv("SMART_SCOPE_ROLE_MAP", '{"user/*.*": "viewer"}')
        assert parse_smart_scopes("user/*.*") == "viewer"


class TestScopeMatches:
    def test_exact_match(self):
        assert _scope_matches("user/*.*", "user/*.*") is True

    def test_no_match(self):
        assert _scope_matches("user/*.read", "user/*.*") is False

    def test_empty_scope(self):
        assert _scope_matches("", "user/*.*") is False


# ---------------------------------------------------------------------------
# SMART router
# ---------------------------------------------------------------------------

@pytest.fixture
def smart_client():
    from api.routers.smart import router
    app = FastAPI()
    app.include_router(router)
    return TestClient(app, raise_server_exceptions=False)


class TestSMARTConfiguration:
    def test_returns_200(self, smart_client):
        assert smart_client.get("/.well-known/smart-configuration").status_code == 200

    def test_content_type_json(self, smart_client):
        resp = smart_client.get("/.well-known/smart-configuration")
        assert "application/json" in resp.headers["content-type"]

    def test_required_fields_present(self, smart_client):
        body = smart_client.get("/.well-known/smart-configuration").json()
        for field in ("issuer", "authorization_endpoint", "token_endpoint", "capabilities", "scopes_supported"):
            assert field in body, f"missing field: {field}"

    def test_capabilities_include_launch(self, smart_client):
        caps = smart_client.get("/.well-known/smart-configuration").json()["capabilities"]
        assert "launch-ehr" in caps
        assert "launch-standalone" in caps

    def test_scopes_include_patient_and_user(self, smart_client):
        scopes = smart_client.get("/.well-known/smart-configuration").json()["scopes_supported"]
        assert "patient/*.*" in scopes
        assert "user/*.*" in scopes

    def test_custom_issuer_from_env(self, smart_client, monkeypatch):
        monkeypatch.setenv("SMART_ISSUER", "https://auth.example.com")
        resp = smart_client.get("/.well-known/smart-configuration")
        assert resp.json()["issuer"] == "https://auth.example.com"

    def test_custom_token_url_from_env(self, smart_client, monkeypatch):
        monkeypatch.setenv("SMART_TOKEN_URL", "https://auth.example.com/token")
        resp = smart_client.get("/.well-known/smart-configuration")
        assert resp.json()["token_endpoint"] == "https://auth.example.com/token"


class TestIntrospectToken:
    def test_unknown_token_returns_inactive(self, smart_client, monkeypatch):
        monkeypatch.delenv("MEDANON_API_KEY", raising=False)
        monkeypatch.delenv("SMART_INTROSPECTION_URL", raising=False)
        resp = smart_client.post("/oauth2/introspect", data={"token": "bad-token"})
        assert resp.status_code == 200
        assert resp.json() == {"active": False}

    def test_api_key_token_returns_active(self, smart_client, monkeypatch):
        monkeypatch.setenv("MEDANON_API_KEY", "test-secret")
        monkeypatch.delenv("SMART_INTROSPECTION_URL", raising=False)
        resp = smart_client.post("/oauth2/introspect", data={"token": "test-secret"})
        body = resp.json()
        assert body["active"] is True
        assert "user/*.*" in body["scope"]

    def test_wrong_api_key_returns_inactive(self, smart_client, monkeypatch):
        monkeypatch.setenv("MEDANON_API_KEY", "correct-key")
        monkeypatch.delenv("SMART_INTROSPECTION_URL", raising=False)
        resp = smart_client.post("/oauth2/introspect", data={"token": "wrong-key"})
        assert resp.json()["active"] is False

    def test_no_api_key_configured_returns_inactive(self, smart_client, monkeypatch):
        monkeypatch.delenv("MEDANON_API_KEY", raising=False)
        monkeypatch.delenv("SMART_INTROSPECTION_URL", raising=False)
        resp = smart_client.post("/oauth2/introspect", data={"token": "any-token"})
        assert resp.json()["active"] is False

    def test_proxies_to_upstream(self, smart_client, monkeypatch):
        monkeypatch.setenv("SMART_INTROSPECTION_URL", "https://auth.example.com/introspect")
        upstream_payload = json.dumps({"active": True, "scope": "openid"}).encode()

        class _FakeResp:
            status = 200
            def read(self): return upstream_payload
            def __enter__(self): return self
            def __exit__(self, *a): return False

        with patch("urllib.request.urlopen", return_value=_FakeResp()):
            resp = smart_client.post("/oauth2/introspect", data={"token": "bearer123"})
        assert resp.json()["active"] is True
        assert resp.json()["scope"] == "openid"

    def test_upstream_http_error_returns_502(self, smart_client, monkeypatch):
        import urllib.error
        monkeypatch.setenv("SMART_INTROSPECTION_URL", "https://auth.example.com/introspect")
        with patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(None, 401, "Unauthorized", {}, None)):
            resp = smart_client.post("/oauth2/introspect", data={"token": "xyz"})
        assert resp.status_code == 502

    def test_upstream_network_failure_returns_502(self, smart_client, monkeypatch):
        monkeypatch.setenv("SMART_INTROSPECTION_URL", "https://auth.example.com/introspect")
        with patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            resp = smart_client.post("/oauth2/introspect", data={"token": "xyz"})
        assert resp.status_code == 502

    def test_missing_token_field_returns_422(self, smart_client):
        resp = smart_client.post("/oauth2/introspect", data={})
        assert resp.status_code == 422
