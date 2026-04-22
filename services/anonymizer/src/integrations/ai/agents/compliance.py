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
    """Deterministic fallback compliance check."""
    try:
        config = yaml.safe_load(yaml_text)
    except Exception:
        return {"error": "Could not parse config"}

    if not isinstance(config, dict):
        return {"error": "Config is not a YAML mapping"}

    rules = config.get("rules", [])
    matches = {r.get("match", "") for r in rules}

    gaps: list[dict] = []
    if regulation.lower() in ("hipaa", "hipaa safe harbor"):
        required_paths = [
            "Patient.name", "Patient.telecom", "Patient.address",
            "Patient.birthDate", "Patient.identifier", "Patient.photo",
        ]
        for path in required_paths:
            if not any(path in m for m in matches):
                gaps.append({
                    "requirement": f"HIPAA identifier: {path}",
                    "description": f"No rule covers {path}",
                    "severity": "critical",
                })

    return {
        "regulation": regulation,
        "compliance_score": max(0.0, 1.0 - len(gaps) * 0.1),
        "gaps": gaps,
        "excess": [],
        "recommendations": [],
        "source": "static_fallback",
    }
