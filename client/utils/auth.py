"""Streamlit auth helpers — API-key authentication for API calls."""
import os

_API_KEY = os.environ.get("MEDANON_API_KEY", "").strip()

_ROLE_ORDER = {"viewer": 0, "analyst": 1, "admin": 2}


def require_login():
    """Return a synthetic admin user (no auth gateway).

    Returns a dict with keys: name, email, roles, access_token.
    """
    return {
        "name": "local-dev",
        "email": "",
        "roles": ["admin"],
        "access_token": None,
    }


def get_auth_headers() -> dict:
    """Return X-API-Key header for API calls (if configured)."""
    if _API_KEY:
        return {"X-API-Key": _API_KEY}
    return {}


def has_role(user: dict, required: str) -> bool:
    """Check if *user* has at least *required* role (hierarchy-aware)."""
    req = _ROLE_ORDER.get(required, 99)
    return any(_ROLE_ORDER.get(r, -1) >= req for r in user.get("roles", []))
