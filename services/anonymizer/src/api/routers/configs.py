"""Config profile management endpoints.

CRUD for de-identification configuration profiles:

    GET    /v1/configs              — list all (system + user-defined)
    GET    /v1/configs/{name}       — fetch YAML for a named config
    POST   /v1/configs              — create a user-defined config
    PUT    /v1/configs/{name}       — replace rules of a user-defined config
    DELETE /v1/configs/{name}       — delete a user-defined config

System configs (the six bundled profiles) are read-only — PUT and DELETE
return 403. All write operations require the 'admin' role.

Config names are validated to ``^[a-zA-Z0-9_-]{1,64}$`` before any
filesystem or database operation to prevent path traversal.
"""

import logging
import os
import re

import yaml
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from api.auth import AuthContext

router = APIRouter()
logger = logging.getLogger("medanon")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_USER_CONFIG_DIR = os.environ.get(
    "MEDANON_USER_CONFIG_DIR", "/output/user-configs"
)

# Valid action names — used for request validation.
_VALID_ACTIONS = frozenset({
    "redact", "cryptohash", "encrypt", "decrypt",
    "perturb", "substitute", "generalize",
    "scrub_text", "nlp_scrub", "nlp_detect", "gpas_pseudonymize",
})


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------

class RuleIn(BaseModel):
    match: str
    action: str
    params: dict | None = None
    name: str | None = None


class ConfigCreateRequest(BaseModel):
    name: str
    description: str = ""
    rules: list[RuleIn]
    general: dict | None = None


class ConfigUpdateRequest(BaseModel):
    description: str | None = None
    rules: list[RuleIn]
    general: dict | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_name(name: str) -> None:
    if not _NAME_RE.match(name):
        raise HTTPException(
            status_code=422,
            detail=(
                "Config name must be 1–64 characters and contain only "
                "letters, digits, hyphens, and underscores."
            ),
        )


def _user_config_path(name: str) -> str:
    return os.path.join(_USER_CONFIG_DIR, f"{name}.yaml")


def _require_admin(request: Request) -> None:
    """Raise 403 if the caller does not have admin role."""
    auth: AuthContext | None = getattr(request.state, "auth", None)
    if auth is None or not auth.has_role("admin"):
        raise HTTPException(status_code=403, detail="Admin role required for config writes.")


def _rules_to_yaml(
    name: str,
    description: str,
    rules: list[RuleIn],
    general: dict | None,
) -> str:
    """Serialise a rule list to the canonical YAML config format."""
    general_block: dict = general or {}
    general_block.setdefault("appname", "SPE-FHIR-BlackBox")

    rule_dicts = []
    for r in rules:
        entry: dict = {"match": r.match, "action": r.action}
        if r.name:
            entry["name"] = r.name
        if r.params:
            entry["params"] = r.params
        rule_dicts.append(entry)

    doc = {
        "general": general_block,
        "rules": rule_dicts,
    }

    header = (
        f"# =============================================================================\n"
        f"# MedAnon User Config: {name}\n"
        f"# {description}\n"
        f"# =============================================================================\n\n"
    )
    return header + yaml.dump(doc, default_flow_style=False, allow_unicode=True, sort_keys=False)


def _validate_and_write(name: str, description: str, rules: list[RuleIn], general: dict | None) -> str:
    """Validate rules via config.Settings then write to disk. Returns the YAML string."""
    import tempfile

    from pipeline.config import Settings

    # Validate action names
    for idx, rule in enumerate(rules, start=1):
        if rule.action not in _VALID_ACTIONS:
            raise HTTPException(
                status_code=422,
                detail=f"rules[{idx}].action '{rule.action}' is not a valid action. "
                       f"Valid actions: {', '.join(sorted(_VALID_ACTIONS))}",
            )
        if not rule.match.strip():
            raise HTTPException(
                status_code=422,
                detail=f"rules[{idx}].match must be a non-empty string.",
            )

    yaml_text = _rules_to_yaml(name, description, rules, general)

    # Write to a temp file and run through the existing Settings validator.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(yaml_text)
        tmp_path = tmp.name

    try:
        Settings(tmp_path)           # raises ValueError on schema errors
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=422, detail=f"Invalid config: {exc}") from exc
    finally:
        os.unlink(tmp_path)

    # Write to the real destination
    os.makedirs(_USER_CONFIG_DIR, exist_ok=True)
    dest = _user_config_path(name)
    with open(dest, "w", encoding="utf-8") as fh:
        fh.write(yaml_text)

    return yaml_text


def _get_store():
    from pipeline.config.store import _config_store
    if _config_store is None:
        raise HTTPException(status_code=503, detail="Config store not initialised.")
    return _config_store


def _read_yaml(name: str, is_system: bool) -> str:
    """Read and return raw YAML for a config (system or user-defined)."""
    if is_system:
        system_dir = os.environ.get("MEDANON_CONFIG_DIR", "/code/config")
        profile_map = {
            "minimal":    "config.yaml",
            "gpas":       "config_gpas.yaml",
            "gdpr":       "config_gdpr_eu.yaml",
            "hipaa":      "config_hipaa_safe_harbor.yaml",
            "research":   "config_research_pseudonymous.yaml",
            "structural":    "config_structure_preserving.yaml",
            "value-masking": "config_value_masking.yaml",
        }
        filename = profile_map.get(name)
        if not filename:
            raise HTTPException(status_code=404, detail=f"Config '{name}' not found.")
        path = os.path.join(system_dir, filename)
    else:
        path = _user_config_path(name)

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"Config file for '{name}' not found on disk.")

    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/configs")
def list_configs(request: Request):
    """List all config profiles — system (read-only) and user-defined."""
    store = _get_store()
    return {"configs": store.list_all()}


@router.get("/configs/{name}")
def get_config(name: str, request: Request):
    """Return the raw YAML for a named config profile."""
    _validate_name(name)
    store = _get_store()
    meta = store.get(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Config '{name}' not found.")

    yaml_text = _read_yaml(name, meta["is_system"])
    return PlainTextResponse(yaml_text, media_type="text/yaml")


@router.post("/configs", status_code=201)
def create_config(body: ConfigCreateRequest, request: Request):
    """Create a new user-defined config profile.

    Validates every rule through the existing Settings validator before
    writing to disk. Requires admin role.
    """
    _require_admin(request)
    _validate_name(body.name)
    store = _get_store()

    if store.exists(body.name):
        raise HTTPException(
            status_code=409,
            detail=f"Config '{body.name}' already exists. Use PUT to update.",
        )

    _validate_and_write(body.name, body.description, body.rules, body.general)

    meta = store.create(body.name, body.description)
    logger.info("config_created name=%s", body.name)
    return {"config": meta}


@router.put("/configs/{name}")
def update_config(name: str, body: ConfigUpdateRequest, request: Request):
    """Replace the rules of an existing user-defined config.

    System configs are read-only and return 403. Requires admin role.
    """
    _require_admin(request)
    _validate_name(name)
    store = _get_store()

    meta = store.get(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Config '{name}' not found.")
    if meta["is_system"]:
        raise HTTPException(
            status_code=403,
            detail=f"System config '{name}' is read-only. Duplicate it first to create a custom version.",
        )

    description = body.description if body.description is not None else meta["description"]
    _validate_and_write(name, description, body.rules, body.general)

    if body.description is not None:
        store.update_description(name, body.description)

    logger.info("config_updated name=%s", name)
    return {"config": store.get(name)}


@router.delete("/configs/{name}", status_code=204)
def delete_config(name: str, request: Request):
    """Delete a user-defined config profile.

    System configs cannot be deleted (403). Requires admin role.
    """
    _require_admin(request)
    _validate_name(name)
    store = _get_store()

    meta = store.get(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Config '{name}' not found.")
    if meta["is_system"]:
        raise HTTPException(
            status_code=403,
            detail=f"System config '{name}' is read-only and cannot be deleted.",
        )

    # Remove file from disk
    path = _user_config_path(name)
    if os.path.isfile(path):
        os.remove(path)

    # Remove from index
    try:
        store.delete(name)
    except (KeyError, PermissionError) as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    logger.info("config_deleted name=%s", name)
