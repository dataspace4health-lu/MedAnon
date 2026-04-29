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
    "Patient":                     "spe.direct.patient-admin",
    "RelatedPerson":               "spe.direct.patient-admin",
    "Person":                      "spe.direct.patient-admin",
    # Clinical workforce
    "Practitioner":                "spe.operational.org-practitioner",
    "PractitionerRole":            "spe.operational.org-practitioner",
    "Organization":                "spe.operational.org-practitioner",
    "OrganizationAffiliation":     "spe.operational.org-practitioner",
    "HealthcareService":           "spe.operational.org-practitioner",
    "Location":                    "spe.operational.org-practitioner",
    "Endpoint":                    "spe.operational.org-practitioner",
    # Clinical observations
    "Observation":                 "spe.clinical.observation",
    "QuestionnaireResponse":       "spe.clinical.observation",
    "RiskAssessment":              "spe.clinical.observation",
    # Conditions, procedures, allergies, medications
    "Condition":                   "spe.clinical.condition-procedure",
    "Procedure":                   "spe.clinical.condition-procedure",
    "AllergyIntolerance":          "spe.clinical.condition-procedure",
    "FamilyMemberHistory":         "spe.clinical.condition-procedure",
    "ClinicalImpression":          "spe.clinical.condition-procedure",
    "DetectedIssue":               "spe.clinical.condition-procedure",
    "MedicationRequest":           "spe.clinical.condition-procedure",
    "MedicationAdministration":    "spe.clinical.condition-procedure",
    "MedicationDispense":          "spe.clinical.condition-procedure",
    "MedicationStatement":         "spe.clinical.condition-procedure",
    "Medication":                  "spe.clinical.condition-procedure",
    "Immunization":                "spe.clinical.condition-procedure",
    "ImmunizationEvaluation":      "spe.clinical.condition-procedure",
    "ImmunizationRecommendation":  "spe.clinical.condition-procedure",
    "NutritionOrder":              "spe.clinical.condition-procedure",
    "VisionPrescription":          "spe.clinical.condition-procedure",
    # Reports & documents
    "DiagnosticReport":            "spe.clinical.report-text",
    "DocumentReference":           "spe.clinical.report-text",
    "Composition":                 "spe.clinical.report-text",
    "Media":                       "spe.clinical.report-text",
    "DocumentManifest":            "spe.clinical.report-text",
    # Encounters & care
    "Encounter":                   "spe.direct.resource-id",
    "EpisodeOfCare":               "spe.direct.resource-id",
    "CarePlan":                    "spe.direct.resource-id",
    "CareTeam":                    "spe.direct.resource-id",
    "Goal":                        "spe.direct.resource-id",
    # Scheduling & workflow
    "ServiceRequest":              "spe.operational.scheduling",
    "Appointment":                 "spe.operational.scheduling",
    "AppointmentResponse":         "spe.operational.scheduling",
    "Schedule":                    "spe.operational.scheduling",
    "Slot":                        "spe.operational.scheduling",
    "Task":                        "spe.operational.scheduling",
    "Communication":               "spe.operational.scheduling",
    "CommunicationRequest":        "spe.operational.scheduling",
    # Financial / billing
    "Coverage":                    "spe.financial.coverage",
    "Claim":                       "spe.financial.claims",
    "ClaimResponse":               "spe.financial.claims",
    "ExplanationOfBenefit":        "spe.financial.claims",
    "CoverageEligibilityRequest":  "spe.financial.claims",
    "CoverageEligibilityResponse": "spe.financial.claims",
    "PaymentNotice":               "spe.financial.claims",
    "PaymentReconciliation":       "spe.financial.claims",
    # Devices & media
    "Device":                      "spe.direct.document-media",
    "DeviceRequest":               "spe.direct.document-media",
    "DeviceUseStatement":          "spe.direct.document-media",
    "Binary":                      "spe.direct.document-media",
    # Audit / provenance
    "Provenance":                  "spe.technical.references",
    "AuditEvent":                  "spe.technical.references",
    "Consent":                     "spe.technical.references",
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
                _MANAGED_ATTRS = frozenset({
                    "filename",
                    "processing_errors",
                    "processingError",
                    "rewrite_references",
                    "rewrite_text_ids",
                    "domain_map",
                    "general",
                    "config_hash",
                })

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
                        self.domain_map = {str(k): str(v) for k, v in raw_domain_map.items()}
                    else:
                        self.domain_map = {}
                else:
                    self.rewrite_references = False
                    self.rewrite_text_ids = False
                    self.domain_map = {}

                self._validate_rules()
                _config_log.info(
                    "Settings loaded: %d rules from %s",
                    len(getattr(self, "rules", [])),
                    filename,
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
