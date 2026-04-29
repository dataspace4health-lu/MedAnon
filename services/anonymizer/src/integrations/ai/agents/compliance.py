"""Compliance Advisor agent.

Recommends config changes for specific regulatory frameworks
and performs gap analysis. Safe for external LLM — no PHI.
"""

import json
import logging
import re

import yaml

_log = logging.getLogger("medanon.ai.compliance")

_COMPLIANCE_SYSTEM_PROMPT = """\
You are a healthcare regulatory compliance advisor specializing in data \
de-identification.

Given a de-identification configuration and a target regulation, you must:
1. Identify GAPS — required protections not covered by the current config
2. Identify EXCESS — over-protective rules that reduce data utility \
unnecessarily
3. Recommend CHANGES — specific rules to add, modify, or remove
4. Provide a compliance SCORE — estimated percentage of requirements met

Output format (JSON):
{{
  "regulation": "...",
  "compliance_score": 0.0-1.0,
  "gaps": [{{"requirement": "...", "description": "...", \
"severity": "critical|high|medium", "recommended_rule": {{...}}}}],
  "excess": [{{"rule_match": "...", "reason": "..."}}],
  "recommendations": [{{"priority": 1, \
"action": "add|modify|remove", "rule": {{...}}, "reason": "..."}}]
}}

Regulations reference:
- HIPAA Safe Harbor: 18 PHI identifiers must ALL be addressed
- GDPR: Pseudonymization (Art. 4(5)), legitimate purpose, data minimization
- HIPAA Expert Determination: Statistical/scientific assessment (more flexible)
- FDA 21 CFR Part 11: Electronic records, audit trails
"""


def advise_compliance(yaml_text: str, regulation: str) -> dict:
    """Analyse config compliance with the specified regulation.

    Returns structured gap analysis with recommendations.
    """
    from integrations.ai.provider import (
        NotAvailableError,
        ProviderUnavailableError,
        get_provider,
    )

    messages = [
        {"role": "system", "content": _COMPLIANCE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": (
                f"Analyse this de-identification configuration for {regulation}"
                " compliance. Output ONLY the JSON analysis.\n\n"
                f"```yaml\n{yaml_text}\n```"
            ),
        },
    ]

    try:
        provider = get_provider()
        response = provider.complete(messages, temperature=0.1, max_tokens=4096)
        match = re.search(r"\{.*\}", response, re.DOTALL)
        if match:
            return json.loads(match.group())
        return {"error": "Could not parse compliance analysis", "raw": response}
    except NotAvailableError:
        return _static_compliance_check(yaml_text, regulation)
    except ProviderUnavailableError:
        return _static_compliance_check(yaml_text, regulation)


def _static_compliance_check(yaml_text: str, regulation: str) -> dict:
    """Deterministic fallback compliance check.

    Covers HIPAA Safe Harbor, GDPR, HIPAA Expert Determination, and FDA 21
    CFR Part 11. For regulations we cannot meaningfully grade without an LLM
    (e.g. Expert Determination requires statistical assessment), returns a
    neutral 0.5 score with `source="static_unsupported"` rather than an
    optimistic 1.0 (which would mask real gaps).
    """
    try:
        config = yaml.safe_load(yaml_text)
    except Exception:
        return {"error": "Could not parse config"}

    if not isinstance(config, dict):
        return {"error": "Config is not a YAML mapping"}

    rules = config.get("rules", [])
    matches = {r.get("match", "") for r in rules}

    reg_key = regulation.strip().lower()
    spec = _STATIC_REGULATIONS.get(reg_key)

    # Aliases — map common spellings to canonical keys.
    if spec is None:
        for canonical, aliases in _REGULATION_ALIASES.items():
            if reg_key in aliases:
                spec = _STATIC_REGULATIONS[canonical]
                break

    if spec is None:
        # Unknown regulation — be honest: we cannot grade it deterministically.
        return {
            "regulation": regulation,
            "compliance_score": 0.5,
            "gaps": [{
                "requirement": "Static fallback unavailable",
                "description": (
                    f"No deterministic fallback exists for '{regulation}'. "
                    "Enable the AI provider for substantive analysis."
                ),
                "severity": "medium",
            }],
            "excess": [],
            "recommendations": [],
            "source": "static_unsupported",
        }

    required_paths = spec["required_paths"]
    severity = spec["severity"]
    label_prefix = spec["label_prefix"]
    gaps: list[dict] = []
    for path in required_paths:
        if not any(path in m for m in matches):
            gaps.append({
                "requirement": f"{label_prefix}: {path}",
                "description": f"No rule covers {path}",
                "severity": severity,
            })

    # 0.1 deduction per gap; clamped to [0, 1].
    score = max(0.0, 1.0 - len(gaps) * 0.1)
    return {
        "regulation": regulation,
        "compliance_score": score,
        "gaps": gaps,
        "excess": [],
        "recommendations": [],
        "source": "static_fallback",
    }


# ---------------------------------------------------------------------------
# Regulation specs for the deterministic fallback. Keep conservative — these
# are required *paths* a config must touch; presence of a matching rule (any
# action) is treated as coverage. The LLM path performs deeper analysis.
# ---------------------------------------------------------------------------
_STATIC_REGULATIONS: dict[str, dict] = {
    "hipaa": {
        "label_prefix": "HIPAA identifier",
        "severity": "critical",
        "required_paths": [
            "Patient.name", "Patient.telecom", "Patient.address",
            "Patient.birthDate", "Patient.identifier", "Patient.photo",
        ],
    },
    "gdpr": {
        # GDPR Art. 4(5) — pseudonymization of direct identifiers; data
        # minimization across name/contact/location.
        "label_prefix": "GDPR Art. 4(5) / minimization",
        "severity": "high",
        "required_paths": [
            "Patient.identifier", "Patient.name", "Patient.telecom",
            "Patient.address",
        ],
    },
    "fda_21_cfr_part_11": {
        # 21 CFR Part 11 is mostly about audit trails + electronic signatures
        # which are infrastructure-level (manifest, audit log) rather than
        # rule-level. We grade only the data fields that should be controlled.
        "label_prefix": "21 CFR Part 11 record",
        "severity": "medium",
        "required_paths": [
            "Patient.identifier", "Practitioner.identifier",
        ],
    },
    "hipaa_expert_determination": {
        # Expert Determination requires statistical assessment we cannot do
        # deterministically. Grade only that *some* identifier handling exists;
        # rely on the LLM (or a human) for the real determination.
        "label_prefix": "Expert Determination prerequisite",
        "severity": "medium",
        "required_paths": [
            "Patient.identifier",
        ],
    },
}

_REGULATION_ALIASES: dict[str, set[str]] = {
    "hipaa": {"hipaa safe harbor", "safe harbor", "hipaa-safe-harbor"},
    "gdpr": {"gdpr eu", "eu gdpr", "general data protection regulation"},
    "fda_21_cfr_part_11": {
        "fda", "21 cfr part 11", "21 cfr 11", "fda 21 cfr part 11",
        "cfr part 11",
    },
    "hipaa_expert_determination": {
        "expert determination", "hipaa expert", "expert-determination",
    },
}
