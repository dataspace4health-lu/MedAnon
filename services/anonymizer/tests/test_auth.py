"""Tests for api.auth — Keycloak OIDC + API-key dual-auth module.

Uses a self-signed RS256 key pair and mocked JWKS so no real Keycloak is needed.
"""

import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

# ---------------------------------------------------------------------------
# Generate a self-signed RS256 key pair for JWT tests
# ---------------------------------------------------------------------------
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization

_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_private_pem = _private_key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)
_public_key = _private_key.public_key()
_public_pem = _public_key.public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo,
)

# Allow plain hashing in tests (no HMAC key configured)
os.environ["MEDANON_HASH_ALLOW_PLAIN"] = "true"


def _make_jwt(claims: dict, expired: bool = False) -> str:
    """Sign a JWT with our test private key."""
    import jwt as pyjwt
    payload = {
        "iss": "http://keycloak-test:8080/realms/medanon",
        "sub": "test-user-id",
        "preferred_username": "tester",
        "aud": "account",
        "iat": int(time.time()) - 60,
        "exp": int(time.time()) - 10 if expired else int(time.time()) + 600,
        "realm_access": {"roles": ["analyst"]},
        **claims,
    }
    return pyjwt.encode(payload, _private_pem, algorithm="RS256")


def _mock_jwk_client():
    """Return a mock PyJWKClient that resolves our test public key."""
    mock_client = MagicMock()
    # Create a mock signing key with a .key attribute pointing to our RSA public key
    mock_signing_key = MagicMock()
    mock_signing_key.key = _public_key
    mock_client.get_signing_key_from_jwt.return_value = mock_signing_key
    return mock_client


def _clear_and_import(env: dict, mock_jwks: bool = False):
    """Clear cached auth module, patch env, and re-import.

    When mock_jwks=True, injects the mock JWK client into the freshly imported
    api.auth module so JWT validation uses our test keys instead of hitting
    a real Keycloak server.
    """
    for mod in ("api.auth", "api.main"):
        if mod in sys.modules:
            del sys.modules[mod]
    with patch.dict(os.environ, env, clear=False):
        # Remove env vars that should not be set for a specific test
        for key in ("KEYCLOAK_URL", "MEDANON_API_KEY", "MEDANON_RATE_LIMIT_ENABLED"):
            if key not in env:
                os.environ.pop(key, None)
        from api.main import app
        if mock_jwks:
            import api.auth
            api.auth._jwk_client = _mock_jwk_client()
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
    """When neither KEYCLOAK_URL nor MEDANON_API_KEY is set → open access."""

    def test_open_access(self):
        client = _clear_and_import({
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"})
        self.assertEqual(resp.status_code, 200)


class TestApiKeyOnly(unittest.TestCase):
    """When only MEDANON_API_KEY is set (no Keycloak)."""

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


class TestJWTAuth(unittest.TestCase):
    """When KEYCLOAK_URL is set — JWT must be validated."""

    def test_no_auth_returns_401(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.post("/process", json={"resourceType": "Patient"})
        self.assertEqual(resp.status_code, 401)

    def test_valid_jwt_returns_200(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        }, mock_jwks=True)
        token = _make_jwt({"realm_access": {"roles": ["analyst"]}})
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"},
                           headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)

    def test_expired_jwt_returns_401(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        }, mock_jwks=True)
        token = _make_jwt({}, expired=True)
        resp = client.post("/process", json={"resourceType": "Patient"},
                           headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 401)


class TestRBACEnforcement(unittest.TestCase):
    """Role-based access control per endpoint."""

    def test_viewer_cannot_process(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        }, mock_jwks=True)
        token = _make_jwt({"realm_access": {"roles": ["viewer"]}})
        resp = client.post("/process", json={"resourceType": "Patient"},
                           headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 403)

    def test_analyst_can_process(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        }, mock_jwks=True)
        token = _make_jwt({"realm_access": {"roles": ["analyst"]}})
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"},
                           headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)

    def test_analyst_cannot_round_trip(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        }, mock_jwks=True)
        token = _make_jwt({"realm_access": {"roles": ["analyst"]}})
        resp = client.post("/process/round-trip", json={},
                           headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 403)

    def test_admin_can_access_all(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        }, mock_jwks=True)
        token = _make_jwt({"realm_access": {"roles": ["admin"]}})
        # admin should be able to access analyst-level endpoint
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"},
                           headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)


class TestDualAuth(unittest.TestCase):
    """When both Keycloak and API key are configured."""

    def test_jwt_takes_precedence(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_API_KEY": "dual-key",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        }, mock_jwks=True)
        token = _make_jwt({"realm_access": {"roles": ["analyst"]}})
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"},
                           headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(resp.status_code, 200)

    def test_api_key_still_works(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "KEYCLOAK_REALM": "medanon",
            "MEDANON_API_KEY": "dual-key",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.post("/process", json={"resourceType": "Patient", "id": "1"},
                           headers={"X-API-Key": "dual-key"})
        self.assertEqual(resp.status_code, 200)


class TestOpenPaths(unittest.TestCase):
    """Open paths accessible without any auth."""

    def test_health_open(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)

    def test_ready_open(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.get("/ready")
        self.assertIn(resp.status_code, (200, 503))

    def test_metrics_open(self):
        client = _clear_and_import({
            "KEYCLOAK_URL": "http://keycloak-test:8080",
            "MEDANON_CONFIG_DIR": os.path.join(os.path.dirname(__file__), "..", "config"),
            "MEDANON_RATE_LIMIT_ENABLED": "false",
        })
        resp = client.get("/metrics")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
