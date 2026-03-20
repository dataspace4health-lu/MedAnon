import os
import re

import yaml
from rich import print


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
                print(f":thumbs_up: Settings loaded: {cfg}")
        except IOError as e:
            print(
                f":sad_but_relieved_face: Settings file {filename} does not exist.")
            print(e)
            raise FileNotFoundError(f"Settings file not found: {filename}") from e
        except yaml.YAMLError as e:
            print(
                ":sad_but_relieved_face: Cannot parse settings yaml data.")
            print(e)
            raise ValueError(f"Settings YAML parse error in {filename}: {e}") from e
        except ValueError:
            print(":sad_but_relieved_face: Invalid settings configuration.")
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
