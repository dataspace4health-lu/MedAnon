"""Config Profile Generator agent.

Converts natural-language de-identification intent into a validated YAML
configuration profile.  Uses bundled profiles as few-shot RAG examples.

PHI Safety: This agent receives NO patient data. Inputs are:
  - User's natural-language description of their de-identification requirements
  - (Optionally) a regulation name (HIPAA, GDPR, etc.)
  - 7 bundled profile YAMLs as context (no PHI — only rule definitions)
"""

import hashlib
import logging
import os
import re

import yaml

_log = logging.getLogger("medanon.ai.config_generator")

_PROFILE_FILES = [
    "config.yaml",
    "config_gpas.yaml",
    "config_gdpr_eu.yaml",
    "config_hipaa_safe_harbor.yaml",
    "config_research_pseudonymous.yaml",
    "config_structure_preserving.yaml",
    "config_value_masking.yaml",
]

VALID_ACTIONS = frozenset({
    "redact", "cryptohash", "encrypt", "decrypt", "perturb",
    "substitute", "generalize", "scrub_text", "nlp_scrub",
    "nlp_detect", "nlp_detect_act", "gpas_pseudonymize",
})

_SYSTEM_PROMPT = """\
You are an expert FHIR de-identification configuration generator for the \
MedAnon privacy toolkit.

Your task: Generate a valid YAML configuration profile based on the user's \
requirements.

## YAML Schema

The config YAML has two sections:
1. `general:` — metadata and global settings (appname, hash_type, \
rewrite_references, rewrite_text_ids)
2. `rules:` — ordered list of match/action rules

Each rule has:
- `match:` — FHIRPath expression (e.g., "Patient.name", "*.id", \
"*.effectiveDateTime")
- `action:` — one of: {actions}
- `params:` — optional action-specific parameters
- `name:` — optional human-readable description

## Valid Actions
- redact: Replace with [REDACTED] or empty
- cryptohash: HMAC-SHA3-256 one-way hash (deterministic, unlinkable without \
key)
- encrypt: RSA-encrypt (reversible with private key)
- perturb: Random offset for numeric/date values
- substitute: Replace with static value (REQUIRED param: substitute_with: "<value>")
- generalize: Reduce precision (date_year, date_year_month, zip_prefix, \
age_bracket)
- scrub_text: Regex-based PII scrubbing (params: mode, patterns)
- nlp_scrub: NLP-based PHI detection and replacement (params: mode, \
threshold, html)
- gpas_pseudonymize: External gPAS pseudonymization server

## FHIRPath Conventions
- "*.field" — wildcard matches all resource types
- "ResourceType.field" — type-specific match
- "ResourceType.field.subfield" — nested path
- Rules are evaluated in order; first match wins

## Key FHIR PHI Fields
- Patient: name, telecom, address, birthDate, identifier, photo, gender, \
extension
- Practitioner: name, telecom, address, identifier, birthDate, photo
- All: *.id, *.text, *.meta.lastUpdated, *.performer, *.author, \
*.subject.reference

## Compliance Considerations
- HIPAA Safe Harbor (18 identifiers): redact/generalize all 18 PHI categories
- GDPR Art. 4(5): pseudonymization requires key-based separation
- Research (IRB): often needs date_year_month + cryptohash for longitudinal \
linkage

## Reference Profiles
Below are abbreviated versions of the 7 bundled profiles for reference:

{profiles_context}

## Output Requirements
1. Output ONLY valid YAML (no markdown fences, no explanation outside the YAML)
2. Include both `general:` and `rules:` sections
3. Use appropriate actions for the stated compliance/use-case requirements
4. Cover ALL relevant FHIR resource types (Patient, Practitioner, Organization)
5. Always include text scrubbing rules (scrub_text + nlp_scrub for narratives)
6. Always include *.id handling (cryptohash or gpas_pseudonymize)
"""

_FALLBACK_KEYWORD_MAP = {
    "hipaa": "config_hipaa_safe_harbor.yaml",
    "safe harbor": "config_hipaa_safe_harbor.yaml",
    "gdpr": "config_gdpr_eu.yaml",
    "european": "config_gdpr_eu.yaml",
    "research": "config_research_pseudonymous.yaml",
    "irb": "config_research_pseudonymous.yaml",
    "longitudinal": "config_research_pseudonymous.yaml",
    "pseudonymize": "config_gpas.yaml",
    "pseudonymise": "config_gpas.yaml",
    "gpas": "config_gpas.yaml",
    "minimal": "config.yaml",
    "basic": "config.yaml",
    "development": "config.yaml",
    "structure": "config_structure_preserving.yaml",
    "preserving": "config_structure_preserving.yaml",
    "masking": "config_value_masking.yaml",
    "encrypt": "config_value_masking.yaml",
}


def _load_profile_context() -> str:
    """Load bundled profiles for RAG context (abbreviated)."""
    config_dir = os.environ.get("MEDANON_CONFIG_DIR", "/code/config")
    parts: list[str] = []
    for fname in _PROFILE_FILES:
        path = os.path.join(config_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
            lines = content.splitlines()
            truncated = "\n".join(lines[:120])
            parts.append(f"### {fname}\n```yaml\n{truncated}\n```\n")
        except FileNotFoundError:
            continue
    return "\n".join(parts)


def _load_fallback_profile(prompt: str) -> str | None:
    """Keyword-match fallback when AI is disabled."""
    prompt_lower = prompt.lower()
    for keyword, filename in _FALLBACK_KEYWORD_MAP.items():
        if keyword in prompt_lower:
            config_dir = os.environ.get("MEDANON_CONFIG_DIR", "/code/config")
            path = os.path.join(config_dir, filename)
            try:
                with open(path, encoding="utf-8") as f:
                    return f.read()
            except FileNotFoundError:
                continue
    return None


def _extract_yaml_from_response(text: str) -> str:
    """Extract YAML from LLM response, stripping markdown fences."""
    match = re.search(r"```ya?ml\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    match = re.search(r"```\s*\n(.*?)```", text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _validate_yaml_config(yaml_text: str) -> tuple[bool, str]:
    """Validate generated YAML through the existing Settings loader.

    Returns (is_valid, error_message). Error message is empty on success.
    """
    import tempfile

    try:
        parsed = yaml.safe_load(yaml_text)
        if not isinstance(parsed, dict):
            return False, "Generated config is not a valid YAML mapping"
        if "rules" not in parsed:
            return False, "Generated config missing 'rules' section"
        rules = parsed.get("rules", [])
        if not isinstance(rules, list) or len(rules) == 0:
            return False, "Generated config has no rules"
        for i, rule in enumerate(rules):
            action = rule.get("action", "")
            if action not in VALID_ACTIONS:
                return False, f"Rule {i + 1}: invalid action '{action}'"
            if not rule.get("match", "").strip():
                return False, f"Rule {i + 1}: empty match expression"

        from pipeline.config.loader import Settings

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False, encoding="utf-8",
        ) as tmp:
            tmp.write(yaml_text)
            tmp_path = tmp.name
        try:
            Settings(tmp_path)
        finally:
            os.unlink(tmp_path)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def generate_config(prompt: str, regulation: str = "") -> dict:
    """Generate a config profile from natural-language intent.

    Returns dict with keys: yaml, valid, validation_error, source
    """
    from integrations.ai.provider import (
        NotAvailableError,
        ProviderUnavailableError,
        get_provider,
    )

    # Try AI generation first
    try:
        provider = get_provider()
    except NotAvailableError:
        fallback = _load_fallback_profile(prompt + " " + regulation)
        if fallback:
            valid, err = _validate_yaml_config(fallback)
            return {
                "yaml": fallback, "valid": valid,
                "validation_error": err, "source": "fallback",
            }
        return {
            "yaml": "", "valid": False,
            "validation_error": "No matching profile found", "source": "fallback",
        }

    profiles_context = _load_profile_context()
    system_msg = _SYSTEM_PROMPT.format(
        actions=", ".join(sorted(VALID_ACTIONS)),
        profiles_context=profiles_context,
    )
    user_msg = (
        "Generate a de-identification configuration profile for the "
        f"following requirements:\n\n{prompt}"
    )
    if regulation:
        user_msg += f"\n\nCompliance framework: {regulation}"

    cache_key = hashlib.sha256(f"{prompt}:{regulation}".encode()).hexdigest()

    try:
        response_text = provider.complete(
            messages=[
                {"role": "system", "content": system_msg},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.2,
            cache_key=cache_key,
        )
    except ProviderUnavailableError:
        fallback = _load_fallback_profile(prompt + " " + regulation)
        if fallback:
            valid, err = _validate_yaml_config(fallback)
            return {
                "yaml": fallback, "valid": valid,
                "validation_error": err, "source": "fallback",
            }
        return {
            "yaml": "", "valid": False,
            "validation_error": "AI unavailable, no fallback match", "source": "error",
        }

    yaml_text = _extract_yaml_from_response(response_text)
    valid, err = _validate_yaml_config(yaml_text)

    # If invalid, try one retry with the error message
    if not valid:
        _log.info("config_gen_retry reason=%s", err)
        retry_msg = (
            f"The generated config had a validation error: {err}\n\n"
            "Please fix and regenerate. Output ONLY the corrected YAML."
        )
        try:
            response_text = provider.complete(
                messages=[
                    {"role": "system", "content": system_msg},
                    {"role": "user", "content": user_msg},
                    {"role": "assistant", "content": yaml_text},
                    {"role": "user", "content": retry_msg},
                ],
                temperature=0.1,
            )
            yaml_text = _extract_yaml_from_response(response_text)
            valid, err = _validate_yaml_config(yaml_text)
        except ProviderUnavailableError:
            pass  # Return first attempt's result

    return {
        "yaml": yaml_text, "valid": valid,
        "validation_error": err, "source": "ai",
    }
