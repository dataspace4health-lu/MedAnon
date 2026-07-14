"""Load-time rule schema validation (Pydantic).

The structural checks in ``Settings._validate_rules`` only verify that
``match``/``action`` are non-empty strings and ``params`` is a mapping  an
unknown action name or a typo'd strategy was previously discovered *at apply
time*, mid-run, per matched element (and in skip mode it quarantined resources
one by one). This module validates rules when the profile is loaded:

  - the action name must exist in the dispatch registries of
    ``pipeline.deidentify`` (lazy-imported to avoid a config↔deidentify cycle);
  - per-action params are checked against a typed model (enum strategies,
    integer bounds, required keys);
  - unknown top-level rule keys (e.g. ``patams``) are rejected.

Severity is controlled by ``MEDANON_RULE_SCHEMA_STRICT``:
  - unset / ``false`` (default): violations are logged as warnings  existing
    deployments keep working for one release while profiles are cleaned up;
  - ``true``: violations raise ``ValueError`` at load time.

The 8 bundled profiles pass strict validation; ``tests/pipeline/
test_rule_schema.py`` enforces that.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

_log = logging.getLogger("medanon.config")


# ---------------------------------------------------------------------------
# Per-action params models
# ---------------------------------------------------------------------------
# ``extra="allow"`` on params models: actions tolerate (and some read)
# additional keys, and strategy-specific keys vary  the models pin down the
# keys that have caused real misconfigurations (enums, bounds, required keys)
# without freezing the full param surface.


class ParamsBase(BaseModel):
    model_config = ConfigDict(extra="allow")


class RedactParams(ParamsBase):
    replacement: str | None = None


class GeneralizeParams(ParamsBase):
    strategy: Literal[
        "date_year",
        "date_year_month",
        "date_year_instant",
        "date_decade",
        "age_bracket",
        "number_round",
        "zip_prefix",
        "category",
        "redact_if_rare",
    ] = "date_year"
    bracket_size: int = Field(default=10, gt=0)
    precision: int = Field(default=10, gt=0)
    prefix_length: int = Field(default=3, gt=0)
    mapping: dict[str, Any] | None = None
    unmapped: str | None = None
    rare_values: list[str] | None = None
    replacement: str | None = None


class MaskParams(ParamsBase):
    strategy: Literal[
        "keep_prefix", "keep_suffix", "keep_domain", "keep_country_code", "full"
    ] = "keep_suffix"
    mask_char: str = Field(default="*", min_length=1, max_length=1)
    keep_chars: int = Field(default=4, ge=0)
    preserve_length: bool = True


class PerturbParams(ParamsBase):
    min: int
    max: int

    @model_validator(mode="after")
    def _max_ge_min(self):
        if self.max < self.min:
            raise ValueError(f"perturb: max ({self.max}) must be >= min ({self.min})")
        return self


class DateShiftParams(ParamsBase):
    max_days: int = Field(gt=0)
    direction: Literal["past", "future", "both"] = "past"
    preserve_age_bracket: bool = False
    anchor_path: str | None = None


class TokenizeParams(ParamsBase):
    format: str | None = None
    namespace: str | None = None
    preserve_length: bool = False


class SubstituteParams(ParamsBase):
    substitute_with: Any  # required  see commit "enforce required substitute_with"


class CryptohashParams(ParamsBase):
    hash_type: str | None = None
    secret_key: str | None = None
    secret_key_env: str | None = None


class EncryptParams(ParamsBase):
    algorithm: str | None = None


class NlpParams(ParamsBase):
    entities: str | list[str] | None = None
    threshold: float = Field(default=0.5, ge=0.0, le=1.0)
    language: str | None = None
    entity_actions: dict[str, str] | None = None
    default_action: str | None = None
    # Replacement mode for nlp_scrub (tokenize vs redact).
    mode: str | None = None
    # Treat the matched text as XHTML and scrub text nodes only.
    html: bool | None = None
    # Decode a Base64-encoded attachment payload before scrubbing, then
    # re-encode on write-back (used for content.attachment.data text payloads).
    base64_encoded: bool | None = None
    # Per-entity overlap-resolution priorities (entity name → int).
    entity_priorities: dict[str, int] | None = None
    # Behaviour when the NLP service is unavailable: "raise" or "redact".
    fail_mode: str | None = None
    # Scope for the token map reuse (e.g. per-resource vs global).
    mapping_scope: str | None = None


class GpasParams(ParamsBase):
    gpas_domain: str | None = None
    gpas_operation: str | None = None


_KNOWN_CONDITION_OPERATORS = frozenset(
    {
        "equals",
        "not_equals",
        "contains",
        "not_contains",
        "exists",
        "not_exists",
        "in",
        "not_in",
        "matches",
        "not_matches",
    }
)


class ConditionModel(BaseModel):
    """Shape of a single condition block (condition: / conditions: entries)."""

    model_config = ConfigDict(extra="allow")

    field: str | None = None
    operator: str | None = None
    value: Any = None

    @model_validator(mode="after")
    def check_operator(self) -> "ConditionModel":
        op = self.operator
        if op is not None and op not in _KNOWN_CONDITION_OPERATORS:
            raise ValueError(
                f"unknown condition operator {op!r}; "
                f"valid: {', '.join(sorted(_KNOWN_CONDITION_OPERATORS))}"
            )
        return self


_PARAMS_MODELS: dict[str, type[ParamsBase]] = {
    "redact": RedactParams,
    "generalize": GeneralizeParams,
    "mask": MaskParams,
    "perturb": PerturbParams,
    "date_shift": DateShiftParams,
    "tokenize": TokenizeParams,
    "substitute": SubstituteParams,
    "cryptohash": CryptohashParams,
    "encrypt": EncryptParams,
    "decrypt": EncryptParams,
    "nlp_scrub": NlpParams,
    "nlp_detect": NlpParams,
    "nlp_detect_act": NlpParams,
    "scrub_text": ParamsBase,
    "gpas_pseudonymize": GpasParams,
    "gpas_depseudonymize": GpasParams,
}


# ---------------------------------------------------------------------------
# Rule model
# ---------------------------------------------------------------------------


class RuleModel(BaseModel):
    """Top-level rule shape  unknown keys (typos) are rejected.

    ``priority`` is accepted for rule-ordering support; ``condition`` /
    ``conditions`` for conditional rules (E2.4).
    """

    model_config = ConfigDict(extra="forbid")

    match: str = Field(min_length=1)
    action: str = Field(min_length=1)
    name: str | None = None
    params: dict[str, Any] | None = None
    condition: dict[str, Any] | None = None
    conditions: list[dict[str, Any]] | None = None
    priority: int | None = None


def _known_action_names() -> frozenset[str]:
    """Canonical action vocabulary (domain contract, guarded against the engine)."""
    from domain.actions import ALL_ACTION_NAMES

    return ALL_ACTION_NAMES


def validate_rules_schema(rules: list[dict]) -> list[str]:
    """Validate *rules* against the schema; return a list of error strings.

    Pure check  never raises, never mutates. The caller decides severity
    (warn vs raise) based on ``MEDANON_RULE_SCHEMA_STRICT``.
    """
    errors: list[str] = []
    try:
        action_names = _known_action_names()
    except Exception:  # registry import failure must not block config loading
        action_names = frozenset()

    for idx, rule in enumerate(rules, start=1):
        if not isinstance(rule, dict):
            continue  # structural validation already rejects this
        label = rule.get("name") or f"rules[{idx}]"
        try:
            RuleModel.model_validate(rule)
        except ValidationError as exc:
            for err in exc.errors():
                loc = ".".join(str(p) for p in err["loc"]) or "(rule)"
                errors.append(f"{label}: {loc}: {err['msg']}")
            continue

        action = rule.get("action", "")
        if action_names and action not in action_names:
            errors.append(
                f"{label}: unknown action {action!r} "
                f"(valid: {', '.join(sorted(action_names))})"
            )
            continue

        params_model = _PARAMS_MODELS.get(action)
        params = rule.get("params")
        if params_model is not None and isinstance(params, dict):
            try:
                params_model.model_validate(params)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(p) for p in err["loc"]) or "(params)"
                    errors.append(f"{label}: params.{loc}: {err['msg']}")
        elif params_model is not None and params is None and action == "substitute":
            errors.append(f"{label}: params.substitute_with: field required")

        # Validate condition / conditions operators
        for cond_key, cond_val in (
            ("condition", rule.get("condition")),
            *[
                (f"conditions[{i}]", c)
                for i, c in enumerate(rule.get("conditions") or [])
            ],
        ):
            if not isinstance(cond_val, dict):
                continue
            try:
                ConditionModel.model_validate(cond_val)
            except ValidationError as exc:
                for err in exc.errors():
                    loc = ".".join(str(p) for p in err["loc"]) or "(condition)"
                    errors.append(f"{label}: {cond_key}.{loc}: {err['msg']}")

    return errors
