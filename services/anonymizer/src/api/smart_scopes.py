"""SMART on FHIR scope-to-role mapping.

Maps SMART App Launch scopes to the internal role hierarchy
(admin > analyst > viewer).

SMART scopes reference:
  https://hl7.org/fhir/smart-app-launch/scopes-and-launch-context.html
"""

# Maps SMART scope patterns to internal role names.
# More permissive scopes map to higher roles.
# Order matters: more specific patterns should come first.
_DEFAULT_SCOPE_ROLES: list[tuple[str, str]] = [
    # Full user access → admin
    ("user/*.*", "admin"),
    ("user/*.write", "admin"),
    # Read-only user access → analyst (can process data)
    ("user/*.read", "analyst"),
    # Patient-context access → analyst
    ("patient/*.*", "analyst"),
    ("patient/*.write", "analyst"),
    ("patient/*.read", "analyst"),
    # Launch / openid → viewer (read-only)
    ("launch", "viewer"),
    ("launch/patient", "viewer"),
    ("openid", "viewer"),
    ("fhirUser", "viewer"),
    ("profile", "viewer"),
    ("offline_access", "viewer"),
]

# Role precedence for choosing the highest role from multiple scopes
_ROLE_PRECEDENCE = {"admin": 2, "analyst": 1, "viewer": 0}


def parse_smart_scopes(scopes_str: str) -> str:
    """Map a space-separated SMART scopes string to the highest matching internal role.

    Returns the role name string ('admin', 'analyst', 'viewer').
    Defaults to 'viewer' if no scope matches.

    Args:
        scopes_str: Space-separated SMART scopes, e.g. "patient/*.read launch openid"

    Examples:
        >>> parse_smart_scopes("user/*.*")
        'admin'
        >>> parse_smart_scopes("patient/*.read launch")
        'analyst'
        >>> parse_smart_scopes("launch openid")
        'viewer'
        >>> parse_smart_scopes("")
        'viewer'
    """
    import os
    import json

    # Allow site-specific overrides via env var
    custom_map_json = os.environ.get("SMART_SCOPE_ROLE_MAP", "")
    scope_roles = _DEFAULT_SCOPE_ROLES
    if custom_map_json:
        try:
            custom = json.loads(custom_map_json)
            # custom is a dict {scope_pattern: role_name}
            scope_roles = list(custom.items()) + _DEFAULT_SCOPE_ROLES
        except Exception:
            pass  # Ignore malformed env var, use defaults

    best_role = "viewer"
    best_precedence = -1

    scopes = scopes_str.strip().split() if scopes_str.strip() else []

    for scope in scopes:
        for pattern, role in scope_roles:
            # Simple glob-style matching: exact or wildcard in the resource part
            if _scope_matches(scope, pattern):
                prec = _ROLE_PRECEDENCE.get(role, 0)
                if prec > best_precedence:
                    best_precedence = prec
                    best_role = role
                break

    return best_role


def _scope_matches(scope: str, pattern: str) -> bool:
    """Return True if scope matches pattern (exact match)."""
    return scope == pattern
