"""pipeline.config.loader — YAML config loading and validation.

Loads de-identification rule profiles from YAML files.  Supports
``${VAR:-default}`` environment variable interpolation in values.
Validates rule shapes and raises ``ValueError`` (never ``sys.exit``) on
invalid config so callers can handle errors gracefully.

Public API:
    load_config(path)         — parse and validate a YAML config file
    load_config_str(yaml_str) — parse from an in-memory YAML string
"""

import logging
import os
import re

import yaml

_config_log = logging.getLogger("medanon.config")

# Default resource-type → gPAS domain routing table.
# Applied automatically when a profile's general.domain_map is absent.
# Any resource type NOT listed here falls back to the profile's GPAS_DOMAIN env var.
_DEFAULT_DOMAIN_MAP: dict[str, str] = {
    # Patient demographics
    "Patient": "spe.direct.patient-admin",
    "RelatedPerson": "spe.direct.patient-admin",
    "Person": "spe.direct.patient-admin",
    # Clinical workforce
    "Practitioner": "spe.operational.org-practitioner",
    "PractitionerRole": "spe.operational.org-practitioner",
    "Organization": "spe.operational.org-practitioner",
    "OrganizationAffiliation": "spe.operational.org-practitioner",
    "HealthcareService": "spe.operational.org-practitioner",
    "Location": "spe.operational.org-practitioner",
    "Endpoint": "spe.operational.org-practitioner",
    # Clinical observations
    "Observation": "spe.clinical.observation",
    "QuestionnaireResponse": "spe.clinical.observation",
    "RiskAssessment": "spe.clinical.observation",
    # Conditions, procedures, allergies, medications
    "Condition": "spe.clinical.condition-procedure",
    "Procedure": "spe.clinical.condition-procedure",
    "AllergyIntolerance": "spe.clinical.condition-procedure",
    "FamilyMemberHistory": "spe.clinical.condition-procedure",
    "ClinicalImpression": "spe.clinical.condition-procedure",
    "DetectedIssue": "spe.clinical.condition-procedure",
    "MedicationRequest": "spe.clinical.condition-procedure",
    "MedicationAdministration": "spe.clinical.condition-procedure",
    "MedicationDispense": "spe.clinical.condition-procedure",
    "MedicationStatement": "spe.clinical.condition-procedure",
    "Medication": "spe.clinical.condition-procedure",
    "Immunization": "spe.clinical.condition-procedure",
    "ImmunizationEvaluation": "spe.clinical.condition-procedure",
    "ImmunizationRecommendation": "spe.clinical.condition-procedure",
    "NutritionOrder": "spe.clinical.condition-procedure",
    "VisionPrescription": "spe.clinical.condition-procedure",
    # Reports & documents
    "DiagnosticReport": "spe.clinical.report-text",
    "DocumentReference": "spe.clinical.report-text",
    "Composition": "spe.clinical.report-text",
    "Media": "spe.clinical.report-text",
    "DocumentManifest": "spe.clinical.report-text",
    # Encounters & care
    "Encounter": "spe.direct.resource-id",
    "EpisodeOfCare": "spe.direct.resource-id",
    "CarePlan": "spe.direct.resource-id",
    "CareTeam": "spe.direct.resource-id",
    "Goal": "spe.direct.resource-id",
    # Scheduling & workflow
    "ServiceRequest": "spe.operational.scheduling",
    "Appointment": "spe.operational.scheduling",
    "AppointmentResponse": "spe.operational.scheduling",
    "Schedule": "spe.operational.scheduling",
    "Slot": "spe.operational.scheduling",
    "Task": "spe.operational.scheduling",
    "Communication": "spe.operational.scheduling",
    "CommunicationRequest": "spe.operational.scheduling",
    # Financial / billing
    "Coverage": "spe.financial.coverage",
    "Claim": "spe.financial.claims",
    "ClaimResponse": "spe.financial.claims",
    "ExplanationOfBenefit": "spe.financial.claims",
    "CoverageEligibilityRequest": "spe.financial.claims",
    "CoverageEligibilityResponse": "spe.financial.claims",
    "PaymentNotice": "spe.financial.claims",
    "PaymentReconciliation": "spe.financial.claims",
    # Devices & media
    "Device": "spe.direct.document-media",
    "DeviceRequest": "spe.direct.document-media",
    "DeviceUseStatement": "spe.direct.document-media",
    "Binary": "spe.direct.document-media",
    # Audit / provenance
    "Provenance": "spe.technical.references",
    "AuditEvent": "spe.technical.references",
    "Consent": "spe.technical.references",
}


class Settings:
    _ENV_EXPR = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")

    def __init__(self, filename=""):
        if not filename:
            filename = "config.yaml"
        self.filename = filename
        self.parse(filename)

    def parse(self, filename):
        try:
            with open(filename, "r") as ymlfile:
                cfg = yaml.safe_load(ymlfile)
                if not isinstance(cfg, dict):
                    raise ValueError("Settings YAML must contain a top-level mapping")

                cfg = self._expand_env(cfg)

                # Compute a deterministic SHA-256 over the *resolved* config
                # (post env-interpolation).  Stored on each processing run so
                # auditors can prove which exact ruleset was applied without
                # having to re-derive the YAML + env state at audit time.
                self.config_hash = self._compute_config_hash(cfg)

                # Attributes managed explicitly below — never let raw YAML keys
                # silently overwrite them via setattr (e.g. a YAML key "filename"
                # would corrupt the LRU cache key; "processing_errors" is
                # validated and sanitized separately).
                _MANAGED_ATTRS = frozenset(
                    {
                        "filename",
                        "processing_errors",
                        "processingError",
                        "rewrite_references",
                        "rewrite_text_ids",
                        "domain_map",
                        "general",
                        "config_hash",
                        "privacy_model",
                        "nlp",
                    }
                )

                # Set values of the dictionary as class attributes
                for key in cfg:
                    # Reject keys that target Python internals or methods on
                    # the Settings class — a YAML file containing a key like
                    # ``__class__`` or ``parse`` would otherwise hijack the
                    # object via ``setattr``.
                    if (
                        not isinstance(key, str)
                        or not key.isidentifier()
                        or key.startswith("_")
                        or hasattr(type(self), key)
                    ):
                        continue
                    if key not in _MANAGED_ATTRS:
                        setattr(self, key, cfg[key])

                # Backward-compatible processing error setting inspired by
                # Microsoft anonymizer's processingErrors policy.
                self.processing_errors = str(
                    cfg.get("processing_errors", cfg.get("processingError", "raise"))
                ).lower()
                if self.processing_errors not in ("raise", "skip"):
                    raise ValueError("processing_errors must be one of: raise, skip")

                # Cross-resource reference rewriting: when enabled, the
                # processor deep-walks each resource after rule application
                # and pseudonymizes all FHIR reference IDs via gPAS.
                general = cfg.get("general", {})
                if isinstance(general, dict):
                    self.rewrite_references = general.get("rewrite_references", False)
                    self.rewrite_text_ids = general.get("rewrite_text_ids", False)
                    _sentinel = object()
                    raw_domain_map = general.get("domain_map", _sentinel)
                    if raw_domain_map is _sentinel:
                        # Key absent from YAML — inherit the built-in default map.
                        self.domain_map: dict[str, str] = _DEFAULT_DOMAIN_MAP
                    elif isinstance(raw_domain_map, dict):
                        self.domain_map = {
                            str(k): str(v) for k, v in raw_domain_map.items()
                        }
                    else:
                        self.domain_map = {}
                else:
                    self.rewrite_references = False
                    self.rewrite_text_ids = False
                    self.domain_map = {}

                self._validate_rules()

                # Optional risk-driven generalization block.
                # Absent → privacy_model is None → engine behaves as today.
                raw_pm = cfg.get("privacy_model")
                if raw_pm is not None and isinstance(raw_pm, dict):
                    self.privacy_model: dict | None = self._validate_privacy_model(
                        raw_pm
                    )
                else:
                    self.privacy_model = None

                # Optional NLP configuration block (E3.1/E3.3).
                # Sets entity→action policy + fail mode at profile level so
                # operators don't have to repeat them on every NLP rule.
                raw_nlp = cfg.get("nlp")
                if raw_nlp is not None:
                    self.nlp: dict = self._validate_nlp(raw_nlp)
                else:
                    self.nlp = {}

                _config_log.info(
                    "Settings loaded: %d rules from %s%s",
                    len(getattr(self, "rules", [])),
                    filename,
                    " [privacy_model enabled]" if self.privacy_model else "",
                )
        except IOError as e:
            _config_log.error("Settings file %s does not exist.", filename)
            raise FileNotFoundError(f"Settings file not found: {filename}") from e
        except yaml.YAMLError as e:
            _config_log.error("Cannot parse settings YAML data in %s.", filename)
            raise ValueError(f"Settings YAML parse error in {filename}: {e}") from e
        except ValueError:
            _config_log.error("Invalid settings configuration in %s.", filename)
            raise

    def _expand_env(self, obj):
        if isinstance(obj, dict):
            return {k: self._expand_env(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._expand_env(v) for v in obj]
        if isinstance(obj, str):
            return self._expand_env_str(obj)
        return obj

    @staticmethod
    def _compute_config_hash(cfg) -> str:
        """Deterministic SHA-256 over *cfg* (post env-expansion).

        ``json.dumps(..., sort_keys=True)`` gives a stable byte representation
        regardless of YAML key ordering.  The returned value is the hex digest,
        prefixed with ``sha256:`` so it is self-describing in audit records.
        """
        import hashlib
        import json

        try:
            payload = json.dumps(cfg, sort_keys=True, default=str).encode("utf-8")
        except (TypeError, ValueError):
            # Fall back to repr() for any non-JSON-serialisable corner case.
            # The hash is still deterministic per Python build for the same input.
            payload = repr(cfg).encode("utf-8")
        return "sha256:" + hashlib.sha256(payload).hexdigest()

    def _expand_env_str(self, value):
        def repl(match):
            var_name = match.group(1)
            default = match.group(2)
            env_val = os.environ.get(var_name)
            if env_val is not None:
                return env_val
            if default is not None:
                return default
            raise ValueError(f"Missing required environment variable: {var_name}")

        return self._ENV_EXPR.sub(repl, value)

    def _validate_rules(self):
        rules = getattr(self, "rules", None)
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")

        for idx, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                raise ValueError(f"rules[{idx}] must be a mapping")
            if "match" not in rule or "action" not in rule:
                raise ValueError(f"rules[{idx}] requires both 'match' and 'action'")
            if not isinstance(rule["match"], str) or not rule["match"].strip():
                raise ValueError(f"rules[{idx}].match must be a non-empty string")
            if not isinstance(rule["action"], str) or not rule["action"].strip():
                raise ValueError(f"rules[{idx}].action must be a non-empty string")
            if "params" in rule and not isinstance(rule["params"], dict):
                raise ValueError(f"rules[{idx}].params must be a mapping when provided")
            if "condition" in rule and not isinstance(rule["condition"], dict):
                raise ValueError(
                    f"rules[{idx}].condition must be a mapping when provided"
                )
            if "conditions" in rule and not isinstance(rule["conditions"], list):
                raise ValueError(
                    f"rules[{idx}].conditions must be a list when provided"
                )

        # Schema validation: action names + per-action params (Pydantic).
        # MEDANON_RULE_SCHEMA_STRICT=true → raise; default → warn-only so
        # existing deployments keep working while profiles are cleaned up.
        from pipeline.config.rule_schema import validate_rules_schema

        schema_errors = validate_rules_schema(rules)
        if schema_errors:
            strict = os.environ.get("MEDANON_RULE_SCHEMA_STRICT", "false").lower() in (
                "true",
                "1",
                "yes",
            )
            if strict:
                raise ValueError(
                    "Rule schema validation failed:\n  " + "\n  ".join(schema_errors)
                )
            for err in schema_errors:
                _config_log.warning("rule_schema_violation %s", err)

        # Warn about potentially conflicting rules (same match, different actions)
        self._check_rule_conflicts(rules)

    @staticmethod
    def _check_rule_conflicts(rules):
        """Warn when multiple rules target the same FHIRPath with different actions.

        This catches misconfigurations like one rule hashing Patient.id and
        another redacting it — only the first (by YAML order) will take effect
        due to duplicate-path prevention.
        """
        # Mutually exclusive action groups — applying two from the same group
        # to the same path is almost certainly a misconfiguration.
        _CONFLICTING_GROUPS = [
            frozenset(
                {
                    "redact",
                    "cryptohash",
                    "encrypt",
                    "substitute",
                    "generalize",
                    "gpas_pseudonymize",
                    "perturb",
                }
            ),
        ]
        seen = {}  # match_expr -> (action, rule_name, index)
        for idx, rule in enumerate(rules, start=1):
            match_expr = rule.get("match", "")
            action = rule.get("action", "")
            name = rule.get("name", f"rules[{idx}]")

            if match_expr in seen:
                prev_action, prev_name, prev_idx = seen[match_expr]
                if prev_action == action:
                    continue  # same action on same path is harmless (idempotent)
                for group in _CONFLICTING_GROUPS:
                    if action in group and prev_action in group:
                        _config_log.warning(
                            "Potentially conflicting rules on '%s': "
                            "%s (action=%s, #%d) vs %s (action=%s, #%d). "
                            "Only the first rule will apply due to duplicate-path prevention.",
                            match_expr,
                            prev_name,
                            prev_action,
                            prev_idx,
                            name,
                            action,
                            idx,
                        )
                        break
            else:
                seen[match_expr] = (action, name, idx)

    @staticmethod
    def _validate_nlp(nlp_cfg) -> dict:
        """Validate the optional ``nlp:`` profile block (E3.1/E3.3).

        Accepted shape::

            nlp:
              fail_mode: redact          # redact (safe fallback) | raise (hard fail)
              entity_actions:
                PERSON: redact
                DATE_TIME:
                  action: generalize
                  params:
                    strategy: date_year
              entity_priorities:
                PERSON: 90
                DATE_TIME: 80

        All fields are optional — an absent ``nlp:`` block means the hard-coded
        defaults in ``deidentify.py`` apply unchanged.
        """
        if not isinstance(nlp_cfg, dict):
            raise ValueError("nlp must be a mapping")

        validated: dict = {}

        fail_mode = nlp_cfg.get("fail_mode")
        if fail_mode is not None:
            if fail_mode not in ("redact", "raise"):
                raise ValueError(
                    f"nlp.fail_mode must be 'redact' or 'raise' (got {fail_mode!r})"
                )
            validated["fail_mode"] = fail_mode

        entity_actions = nlp_cfg.get("entity_actions")
        if entity_actions is not None:
            if not isinstance(entity_actions, dict):
                raise ValueError("nlp.entity_actions must be a mapping")
            for entity, action_cfg in entity_actions.items():
                if isinstance(action_cfg, str):
                    pass
                elif isinstance(action_cfg, dict):
                    if "action" not in action_cfg:
                        raise ValueError(
                            f"nlp.entity_actions.{entity}: dict form requires an 'action' key"
                        )
                else:
                    raise ValueError(
                        f"nlp.entity_actions.{entity}: must be a string action name "
                        f"or {{action, params}} mapping (got {type(action_cfg).__name__})"
                    )
            validated["entity_actions"] = entity_actions

        entity_priorities = nlp_cfg.get("entity_priorities")
        if entity_priorities is not None:
            if not isinstance(entity_priorities, dict):
                raise ValueError("nlp.entity_priorities must be a mapping")
            coerced = {}
            for entity, pri in entity_priorities.items():
                try:
                    coerced[entity] = int(pri)
                except (TypeError, ValueError):
                    raise ValueError(
                        f"nlp.entity_priorities.{entity}: priority must be an integer "
                        f"(got {pri!r})"
                    )
            validated["entity_priorities"] = coerced

        return validated

    @staticmethod
    def _validate_privacy_model(pm: dict) -> dict:
        """Validate and normalise a ``privacy_model`` config block.

        Returns the validated dict (suitable for setting on ``self.privacy_model``).
        Raises ``ValueError`` on any invalid field.

        Expected shape::

            privacy_model:
              enabled: true                  # default true when block present
              target_k: 5                    # required; int >= 2
              target_l: 2                    # optional; int >= 2
              max_suppression: 0.05          # float in [0, 1]; default 0.05
              max_qi_count: 5                # max quasi-identifiers; default 5
              quasi_identifiers:
                - path: "Patient.birthDate"  kind: date
                - path: "Patient.address.postalCode"  kind: zip
              sensitive_attribute:           # optional; for l-diversity
                path: "Condition.code"
              on_unsatisfiable: "max_generalize"  # or "fail"
              suppress_linked: true          # suppress linked resources too
        """
        from pipeline.privacy.hierarchies import KNOWN_KINDS

        enabled = pm.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError("privacy_model.enabled must be a boolean")
        if not enabled:
            return None  # type: ignore[return-value]  # caller checks None

        # target_k
        target_k = pm.get("target_k")
        if target_k is None:
            raise ValueError(
                "privacy_model.target_k is required when privacy_model is enabled"
            )
        try:
            target_k = int(target_k)
        except (TypeError, ValueError):
            raise ValueError("privacy_model.target_k must be an integer")
        if target_k < 2:
            raise ValueError(f"privacy_model.target_k must be >= 2 (got {target_k})")

        # target_l (optional)
        target_l = pm.get("target_l")
        if target_l is not None:
            try:
                target_l = int(target_l)
            except (TypeError, ValueError):
                raise ValueError("privacy_model.target_l must be an integer")
            if target_l < 2:
                raise ValueError(
                    f"privacy_model.target_l must be >= 2 (got {target_l})"
                )

        # target_t (optional t-closeness threshold; total-variation distance)
        target_t = pm.get("target_t")
        if target_t is not None:
            try:
                target_t = float(target_t)
            except (TypeError, ValueError):
                raise ValueError("privacy_model.target_t must be a float")
            if not (0.0 <= target_t <= 1.0):
                raise ValueError(
                    f"privacy_model.target_t must be in [0, 1] (got {target_t})"
                )

        # max_suppression
        max_suppression = pm.get("max_suppression", 0.05)
        try:
            max_suppression = float(max_suppression)
        except (TypeError, ValueError):
            raise ValueError("privacy_model.max_suppression must be a float")
        if not (0.0 <= max_suppression <= 1.0):
            raise ValueError(
                f"privacy_model.max_suppression must be in [0, 1] (got {max_suppression})"
            )

        # max_qi_count
        max_qi_count = pm.get("max_qi_count", 5)
        try:
            max_qi_count = int(max_qi_count)
        except (TypeError, ValueError):
            raise ValueError("privacy_model.max_qi_count must be an integer")
        if max_qi_count < 1 or max_qi_count > 10:
            raise ValueError(
                f"privacy_model.max_qi_count must be in [1, 10] (got {max_qi_count})"
            )

        # quasi_identifiers
        qis_raw = pm.get("quasi_identifiers")
        if not isinstance(qis_raw, list) or len(qis_raw) == 0:
            raise ValueError("privacy_model.quasi_identifiers must be a non-empty list")
        if len(qis_raw) > max_qi_count:
            raise ValueError(
                f"privacy_model.quasi_identifiers has {len(qis_raw)} entries but "
                f"max_qi_count={max_qi_count}. Raise max_qi_count or reduce QIs."
            )
        quasi_identifiers = []
        for i, qi in enumerate(qis_raw, start=1):
            if not isinstance(qi, dict):
                raise ValueError(
                    f"privacy_model.quasi_identifiers[{i}] must be a mapping"
                )
            path = qi.get("path", "")
            if not isinstance(path, str) or not path.strip():
                raise ValueError(
                    f"privacy_model.quasi_identifiers[{i}].path must be a non-empty string"
                )
            kind = qi.get("kind", "")
            if kind not in KNOWN_KINDS:
                raise ValueError(
                    f"privacy_model.quasi_identifiers[{i}].kind={kind!r} is unknown. "
                    f"Supported: {', '.join(sorted(KNOWN_KINDS))}"
                )
            quasi_identifiers.append({"path": path.strip(), "kind": kind})

        # sensitive_attribute (optional, for l-diversity)
        sensitive_attribute = None
        sa_raw = pm.get("sensitive_attribute")
        if sa_raw is not None:
            if not isinstance(sa_raw, dict):
                raise ValueError("privacy_model.sensitive_attribute must be a mapping")
            sa_path = sa_raw.get("path", "")
            if not isinstance(sa_path, str) or not sa_path.strip():
                raise ValueError(
                    "privacy_model.sensitive_attribute.path must be a non-empty string"
                )
            sensitive_attribute = {"path": sa_path.strip()}

        # on_unsatisfiable
        on_unsatisfiable = pm.get("on_unsatisfiable", "max_generalize")
        if on_unsatisfiable not in ("max_generalize", "fail"):
            raise ValueError(
                f"privacy_model.on_unsatisfiable must be 'max_generalize' or 'fail' "
                f"(got {on_unsatisfiable!r})"
            )

        # suppress_linked
        suppress_linked = pm.get("suppress_linked", True)
        if not isinstance(suppress_linked, bool):
            raise ValueError("privacy_model.suppress_linked must be a boolean")

        return {
            "enabled": True,
            "target_k": target_k,
            "target_l": target_l,
            "target_t": target_t,
            "max_suppression": max_suppression,
            "max_qi_count": max_qi_count,
            "quasi_identifiers": quasi_identifiers,
            "sensitive_attribute": sensitive_attribute,
            "on_unsatisfiable": on_unsatisfiable,
            "suppress_linked": suppress_linked,
        }
