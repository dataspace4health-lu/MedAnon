"""Config profile management endpoints.

CRUD for de-identification configuration profiles:

    GET    /v1/configs               list all (system + user-defined)
    GET    /v1/configs/{name}        fetch YAML for a named config
    POST   /v1/configs               create a user-defined config
    PUT    /v1/configs/{name}        replace rules of a user-defined config
    DELETE /v1/configs/{name}        delete a user-defined config

System configs (the six bundled profiles) are read-only  PUT and DELETE
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
from pydantic import BaseModel, field_validator

from api.auth import AuthContext

router = APIRouter()
logger = logging.getLogger("medanon")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

_USER_CONFIG_DIR = os.environ.get("MEDANON_USER_CONFIG_DIR", "/output/user-configs")

# Valid action names  used for request validation.
_VALID_ACTIONS = frozenset(
    {
        "redact",
        "cryptohash",
        "encrypt",
        "decrypt",
        "perturb",
        "date_shift",
        "mask",
        "tokenize",
        "substitute",
        "generalize",
        "scrub_text",
        "nlp_scrub",
        "nlp_detect",
        "nlp_detect_act",
        "gpas_pseudonymize",
    }
)


# ---------------------------------------------------------------------------
# Request / response schemas
# ---------------------------------------------------------------------------


# Keys that reference filesystem paths  must come from env vars, never from
# API callers.  Allowing these inline would enable path-traversal via the
# encrypt/decrypt actions which call open() on the resolved path.
_BLOCKED_PARAM_KEYS = frozenset({"public_key", "private_key", "key_file"})

# Scalar types allowed as param values.  Nested dicts/lists are blocked to
# prevent injection of complex structures that action handlers don't expect.
_ALLOWED_PARAM_VALUE_TYPES = (str, int, float, bool)


class RuleIn(BaseModel):
    match: str
    action: str
    params: dict | None = None
    name: str | None = None

    @field_validator("params")
    @classmethod
    def validate_params(cls, v: dict | None) -> dict | None:
        if v is None:
            return v
        for key, value in v.items():
            if key in _BLOCKED_PARAM_KEYS:
                raise ValueError(
                    f"'{key}' must be configured via environment variable, "
                    "not as an inline rule parameter"
                )
            if value is not None and not isinstance(value, _ALLOWED_PARAM_VALUE_TYPES):
                raise ValueError(
                    f"param '{key}' has unsupported type {type(value).__name__!r}; "
                    "only str, int, float, bool values are allowed"
                )
        return v


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
        raise HTTPException(
            status_code=403, detail="Admin role required for config writes."
        )


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
    return header + yaml.dump(
        doc, default_flow_style=False, allow_unicode=True, sort_keys=False
    )


def _validate_and_write(
    name: str, description: str, rules: list[RuleIn], general: dict | None
) -> str:
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
        if rule.action == "substitute" and (
            not rule.params or "substitute_with" not in rule.params
        ):
            raise HTTPException(
                status_code=422,
                detail=f"rules[{idx}]: action 'substitute' requires params.substitute_with.",
            )

    yaml_text = _rules_to_yaml(name, description, rules, general)

    # Write to a temp file and run through the existing Settings validator.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(yaml_text)
        tmp_path = tmp.name

    try:
        Settings(tmp_path)  # raises ValueError on schema errors
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


def _invalidate_settings_cache() -> None:
    """Invalidate settings + rule + gPAS params caches after a profile change."""
    try:
        from pipeline.config.service import clear_settings_cache

        clear_settings_cache()
    except Exception:
        pass


def _read_yaml(name: str, is_system: bool) -> str:
    """Read and return raw YAML for a config (system or user-defined)."""
    if is_system:
        system_dir = os.environ.get("MEDANON_CONFIG_DIR", "/code/config")
        profile_map = {
            "minimal": "config.yaml",
            "gpas": "config_gpas.yaml",
            "gdpr": "config_gdpr_eu.yaml",
            "hipaa": "config_hipaa_safe_harbor.yaml",
            "research": "config_research_pseudonymous.yaml",
            "structural": "config_structure_preserving.yaml",
            "value-masking": "config_value_masking.yaml",
        }
        filename = profile_map.get(name)
        if not filename:
            raise HTTPException(status_code=404, detail=f"Config '{name}' not found.")
        path = os.path.join(system_dir, filename)
    else:
        path = _user_config_path(name)

    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404, detail=f"Config file for '{name}' not found on disk."
        )

    with open(path, "r", encoding="utf-8") as fh:
        return fh.read()


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get("/configs")
def list_configs(request: Request):
    """List all config profiles  system (read-only) and user-defined."""
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


@router.get("/configs/{name}/conflicts")
def get_config_conflicts(name: str, request: Request):
    """Report rules that target the same FHIR path with a different action.

    Conflicts are usually a config mistake: the firing order then decides the
    outcome.  Each conflict increments ``medanon_rule_conflict_total`` so the
    condition is observable in Prometheus as well as the API response.
    """
    from pipeline.rule_matcher import detect_rule_conflicts
    from utils.metrics import RULE_CONFLICT_TOTAL

    _validate_name(name)
    store = _get_store()
    meta = store.get(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Config '{name}' not found.")

    yaml_text = _read_yaml(name, meta["is_system"])
    try:
        parsed = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=422, detail=f"Config '{name}' is not valid YAML: {exc}"
        ) from exc

    rules = parsed.get("rules") if isinstance(parsed, dict) else None
    conflicts = detect_rule_conflicts(rules if isinstance(rules, list) else [])

    for conflict in conflicts:
        actions = conflict["actions"]
        # Emit one labelled sample per action pair so the path/action_a/action_b
        # cardinality stays bounded and readable.
        for i in range(len(actions) - 1):
            RULE_CONFLICT_TOTAL.labels(
                path=conflict["path"],
                action_a=actions[i],
                action_b=actions[i + 1],
            ).inc()
        if conflicts:
            logger.warning(
                "rule_conflict config=%s path=%s actions=%s",
                name,
                conflict["path"],
                ",".join(actions),
            )

    return {"config": name, "conflict_count": len(conflicts), "conflicts": conflicts}


@router.get("/configs/{name}/coverage")
def get_config_coverage(name: str, request: Request):
    """Lint a profile for PHI-path coverage gaps (E1.5).

    Statically checks the profile's rules against ``KNOWN_PHI_PATHS`` and
    reports which well-known identifier paths are not covered by any rule, so
    an operator can spot omissions before running the profile on real data.
    """
    from pipeline.config.linter import lint_profile

    _validate_name(name)
    store = _get_store()
    meta = store.get(name)
    if meta is None:
        raise HTTPException(status_code=404, detail=f"Config '{name}' not found.")

    yaml_text = _read_yaml(name, meta["is_system"])
    try:
        parsed = yaml.safe_load(yaml_text) or {}
    except yaml.YAMLError as exc:
        raise HTTPException(
            status_code=422, detail=f"Config '{name}' is not valid YAML: {exc}"
        ) from exc

    report = lint_profile(parsed if isinstance(parsed, dict) else {})
    if report["uncovered"]:
        logger.warning(
            "config_coverage_gaps config=%s uncovered=%d/%d",
            name,
            len(report["uncovered"]),
            report["total_known_paths"],
        )

    # Schema violations (unknown actions, bad params) surface alongside
    # coverage gaps so the UI shows both before the profile is ever run.
    from pipeline.config.rule_schema import validate_rules_schema

    rules = parsed.get("rules", []) if isinstance(parsed, dict) else []
    schema_errors = validate_rules_schema(rules if isinstance(rules, list) else [])
    return {"config": name, **report, "schema_errors": schema_errors}


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
    _invalidate_settings_cache()
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

    description = (
        body.description if body.description is not None else meta["description"]
    )
    _validate_and_write(name, description, body.rules, body.general)

    if body.description is not None:
        store.update_description(name, body.description)

    _invalidate_settings_cache()
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

    _invalidate_settings_cache()
    logger.info("config_deleted name=%s", name)
