import logging
import os
import re

import yaml

_config_log = logging.getLogger("medanon.config")


class Settings():

    _ENV_EXPR = re.compile(r"\$\{([A-Z0-9_]+)(?::-(.*?))?\}")
    
    def __init__(self, filename=""):
        if not filename:
            filename = "config.yaml"
        self.parse(filename)

    def parse(self, filename):
        try:
            with open(filename, "r") as ymlfile:
                cfg = yaml.safe_load(ymlfile)
                if not isinstance(cfg, dict):
                    raise ValueError("Settings YAML must contain a top-level mapping")

                cfg = self._expand_env(cfg)

                # Set values of the dictionary as class attributes
                for key in cfg:
                    setattr(self, key, cfg[key])

                # Backward-compatible processing error setting inspired by
                # Microsoft anonymizer's processingErrors policy.
                self.processing_errors = str(
                    cfg.get('processing_errors', cfg.get('processingError', 'raise'))
                ).lower()
                if self.processing_errors not in ('raise', 'skip'):
                    raise ValueError("processing_errors must be one of: raise, skip")

                # Cross-resource reference rewriting: when enabled, the
                # processor deep-walks each resource after rule application
                # and pseudonymizes all FHIR reference IDs via gPAS.
                general = cfg.get('general', {})
                if isinstance(general, dict):
                    self.rewrite_references = general.get('rewrite_references', False)
                else:
                    self.rewrite_references = False

                self._validate_rules()
                _config_log.info(
                    "Settings loaded: %d rules from %s",
                    len(getattr(self, 'rules', [])), filename,
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
        rules = getattr(self, 'rules', None)
        if not isinstance(rules, list):
            raise ValueError("rules must be a list")

        for idx, rule in enumerate(rules, start=1):
            if not isinstance(rule, dict):
                raise ValueError(f"rules[{idx}] must be a mapping")
            if 'match' not in rule or 'action' not in rule:
                raise ValueError(f"rules[{idx}] requires both 'match' and 'action'")
            if not isinstance(rule['match'], str) or not rule['match'].strip():
                raise ValueError(f"rules[{idx}].match must be a non-empty string")
            if not isinstance(rule['action'], str) or not rule['action'].strip():
                raise ValueError(f"rules[{idx}].action must be a non-empty string")
            if 'params' in rule and not isinstance(rule['params'], dict):
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
            frozenset({"redact", "cryptohash", "encrypt", "substitute",
                        "generalize", "gpas_pseudonymize", "perturb"}),
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
                            match_expr, prev_name, prev_action, prev_idx,
                            name, action, idx,
                        )
                        break
            else:
                seen[match_expr] = (action, name, idx)
