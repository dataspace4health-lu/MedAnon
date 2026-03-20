"""Streamlit Keycloak auth helpers — OIDC PKCE for browser login."""
import os

import streamlit as st

_KEYCLOAK_URL = os.environ.get("KEYCLOAK_URL", "").strip()
_KEYCLOAK_REALM = os.environ.get("KEYCLOAK_REALM", "medanon")
_KEYCLOAK_UI_CLIENT_ID = os.environ.get("KEYCLOAK_UI_CLIENT_ID", "medanon-ui")
_API_KEY = os.environ.get("MEDANON_API_KEY", "").strip()

_ROLE_ORDER = {"viewer": 0, "analyst": 1, "admin": 2}


def require_login():
    """Gate the Streamlit page behind Keycloak OIDC (PKCE).

    Returns a dict with keys: name, email, roles, access_token.
    When Keycloak is not configured, returns a synthetic admin user.
    """
    if not _KEYCLOAK_URL:
        return {
            "name": "local-dev",
            "email": "",
            "roles": ["admin"],
            "access_token": None,
        }

    try:
        from streamlit_keycloak import login as kc_login
    except ImportError:
        st.error("streamlit-keycloak is not installed. Run: pip install streamlit-keycloak")
        st.stop()

    kc = kc_login(
        url=_KEYCLOAK_URL,
        realm=_KEYCLOAK_REALM,
        client_id=_KEYCLOAK_UI_CLIENT_ID,
        init_options={
            "checkLoginIframe": False,
            "pkceMethod": "S256",
        },
    )

    if not kc.authenticated:
        st.warning("Please log in to continue.")
        st.stop()

    decoded = kc.user_info or {}
    realm_roles = decoded.get("realm_access", {}).get("roles", [])
    return {
        "name": decoded.get("preferred_username", "user"),
        "email": decoded.get("email", ""),
        "roles": [r for r in realm_roles if r in _ROLE_ORDER],
        "access_token": kc.access_token,
    }


def get_auth_headers() -> dict:
    """Return Authorization / X-API-Key headers for API calls."""
    user = st.session_state.get("kc_user")
    if user and user.get("access_token"):
        return {"Authorization": f"Bearer {user['access_token']}"}
    if _API_KEY:
        return {"X-API-Key": _API_KEY}
    return {}


def has_role(user: dict, required: str) -> bool:
    """Check if *user* has at least *required* role (hierarchy-aware)."""
    req = _ROLE_ORDER.get(required, 99)
    return any(_ROLE_ORDER.get(r, -1) >= req for r in user.get("roles", []))
