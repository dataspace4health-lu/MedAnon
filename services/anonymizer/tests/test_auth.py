"""Tests for api.auth — API-key authentication module.

Tests API-key enforcement, open access mode, role hierarchy, and RBAC.
"""

import os
import sys
import unittest
from unittest.mock import patch


# Allow plain hashing in tests (no HMAC key configured)
os.environ["MEDANON_HASH_ALLOW_PLAIN"] = "true"


def _clear_and_import(env: dict):
    """Clear cached auth module, patch env, and re-import."""
    for mod in ("api.auth", "api.main"):
        if mod in sys.modules:
            del sys.modules[mod]
    with patch.dict(os.environ, env, clear=False):
        # Remove env vars that should not be set for a specific test
        for key in ("MEDANON_API_KEY", "MEDANON_RATE_LIMIT_ENABLED"):
            if key not in env:
                os.environ.pop(key, None)
        from api.main import app
        from starlette.testclient import TestClient
        return TestClient(app, raise_server_exceptions=False)


# ===========================================================================
# Tests
# ===========================================================================

class TestRoleHierarchy(unittest.TestCase):
    """Role comparison and has_role logic."""

    def setUp(self):
        for mod in ("api.auth",):
            if mod in sys.modules:
                del sys.modules[mod]

    def test_admin_ge_analyst(self):
        from api.auth import Role
        self.assertGreaterEqual(Role.ADMIN, Role.ANALYST)

    def test_analyst_ge_viewer(self):
        from api.auth import Role
        self.assertGreaterEqual(Role.ANALYST, Role.VIEWER)

    def test_viewer_not_ge_analyst(self):
        from api.auth import Role
        self.assertFalse(Role.VIEWER >= Role.ANALYST)

    def test_auth_context_has_role(self):
        from api.auth import AuthContext
        ctx = AuthContext(subject="u", roles=frozenset({"analyst"}))
        self.assertTrue(ctx.has_role("viewer"))
        self.assertTrue(ctx.has_role("analyst"))
        self.assertFalse(ctx.has_role("admin"))


class TestNoAuthConfigured(unittest.TestCase):
    """When MEDANON_API_KEY is not set -> open access."""

    def test_open_access(self):
        client = _clear_and_import({
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"})
        self.assertEqual(resp.status_code, 200)


class TestApiKeyAuth(unittest.TestCase):
    """When MEDANON_API_KEY is set."""

    def test_no_key_returns_401(self):
        client = _clear_and_import({
            "MEDANON_API_KEY": "test-secret",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.post("/process", json={"resourceType": "Patient"})
        self.assertEqual(resp.status_code, 401)

    def test_wrong_key_returns_401(self):
        client = _clear_and_import({
            "MEDANON_API_KEY": "test-secret",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.post("/process", json={"resourceType": "Patient"},
                           headers={"X-API-Key": "wrong"})
        self.assertEqual(resp.status_code, 401)

    def test_valid_key_returns_200(self):
        client = _clear_and_import({
            "MEDANON_API_KEY": "test-secret",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"},
                           headers={"X-API-Key": "test-secret"})
        self.assertEqual(resp.status_code, 200)

    def test_admin_role_granted_with_api_key(self):
        """API-key users get full admin role."""
        client = _clear_and_import({
            "MEDANON_API_KEY": "test-secret",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        # admin-only endpoint should work with API key
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"},
                           headers={"X-API-Key": "test-secret"})
        self.assertEqual(resp.status_code, 200)


class TestOpenPaths(unittest.TestCase):
    """Open paths accessible without any auth."""

    def test_health_open(self):
        client = _clear_and_import({
            "MEDANON_API_KEY": "test-secret",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_ready_open(self):
        client = _clear_and_import({
            "MEDANON_API_KEY": "test-secret",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.get("/ready")
        self.assertIn(resp.status_code, (200, 503))

    def test_metrics_open(self):
        client = _clear_and_import({
            "MEDANON_API_KEY": "test-secret",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.get("/metrics")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
