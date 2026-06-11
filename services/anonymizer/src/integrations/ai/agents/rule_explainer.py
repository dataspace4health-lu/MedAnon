"""Rule Explainer agent.

Explains config profile rules in plain language and maps them to
regulatory requirements. Safe for external LLM — config YAML contains
no PHI (only rule definitions).
"""

import logging

import yaml

_log = logging.getLogger("medanon.ai.rule_explainer")

_EXPLAIN_SYSTEM_PROMPT = """\
You are a healthcare data privacy expert explaining FHIR de-identification \
configuration rules.

For each rule in the configuration, explain:
1. What data it targets (which FHIR resource field)
2. What transformation it applies
3. Why this is needed for privacy protection
4. Which regulatory requirement(s) it satisfies (HIPAA, GDPR, etc.)

Use clear, non-technical language. Format your response in markdown with \
headers for each rule group.

Key regulatory mappings:
- HIPAA Safe Harbor 45 CFR 164.514(b): 18 PHI identifier categories
- GDPR Art. 4(5): pseudonymization requirements
- GDPR Art. 89: research derogation
- Common Rule 45 CFR 46: IRB/research data requirements

Action meanings:
- redact: Permanently removes the value (irreversible)
- cryptohash: One-way hash — preserves linkability within dataset
- encrypt: Reversible with key — used when re-identification is needed
- generalize: Reduces precision (e.g., exact date to year only)
- scrub_text: Regex-based text pattern replacement
- nlp_scrub: AI-based named entity recognition and replacement
- gpas_pseudonymize: External pseudonym service (reversible pseudonyms)
"""


def explain_config(yaml_text: str, *, streaming: bool = False):
    """Explain a config profile's rules in plain language.

    When streaming=True, returns a generator of text chunks (for SSE).
    When streaming=False, returns the complete explanation string.
    """
    from integrations.ai.prompt_guard import sanitize_untrusted
    from integrations.ai.provider import (
        NotAvailableError,
        ProviderUnavailableError,
        get_provider,
    )

    safe_yaml = sanitize_untrusted(yaml_text, max_len=65536)
    messages = [
        {"role": "system", "content": _EXPLAIN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                "Explain this de-identification configuration:\n\n"
                f"```yaml\n{safe_yaml}\n```"
            ),
        },
    ]

    try:
        provider = get_provider()
    except NotAvailableError:
        result = _static_explain(yaml_text)
        return iter([result]) if streaming else result

    try:
        # YAML rule text only — no resource content (phi_payload=False).
        if streaming:
            return provider.complete_streaming(
                messages, temperature=0.3, phi_payload=False
            )
        return provider.complete(messages, temperature=0.3, phi_payload=False)
    except ProviderUnavailableError:
        result = _static_explain(yaml_text)
        return iter([result]) if streaming else result


def _static_explain(yaml_text: str) -> str:
    """Deterministic fallback explanation when AI is unavailable."""
    try:
        config = yaml.safe_load(yaml_text)
    except Exception:
        return "Unable to parse configuration for explanation."

    rules = config.get("rules", []) if isinstance(config, dict) else []
    lines = ["# Configuration Explanation\n"]
    for i, rule in enumerate(rules, 1):
        match = rule.get("match", "unknown")
        action = rule.get("action", "unknown")
        name = rule.get("name", "")
        lines.append(f"**Rule {i}**: `{match}` -> `{action}`")
        if name:
            lines.append(f"  *{name}*")
        lines.append("")
    if not rules:
        lines.append("No rules found in this configuration.")
    return "\n".join(lines)


def explain_regulatory_alignment(yaml_text: str, regulation: str) -> str:
    """Analyse config against a specific regulation."""
    from integrations.ai.prompt_guard import clean_label, sanitize_untrusted
    from integrations.ai.provider import (
        NotAvailableError,
        ProviderUnavailableError,
        get_provider,
    )

    safe_yaml = sanitize_untrusted(yaml_text, max_len=65536)
    safe_regulation = clean_label(regulation) or "the specified regulation"
    messages = [
        {"role": "system", "content": _EXPLAIN_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Analyse this configuration's alignment with {safe_regulation} "
                "requirements. Identify gaps and recommendations.\n\n"
                f"```yaml\n{safe_yaml}\n```"
            ),
        },
    ]

    try:
        provider = get_provider()
        return provider.complete(messages, temperature=0.2, phi_payload=False)
    except (NotAvailableError, ProviderUnavailableError):
        return f"AI unavailable. Manual {regulation} alignment review required."
