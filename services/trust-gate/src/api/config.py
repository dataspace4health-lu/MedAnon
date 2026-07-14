"""Check configuration loaded once at import.

The rule/threshold/policy config is small and static, so it is read once here and
shared by the request handlers via ``RULES`` / ``THRESHOLDS`` / ``POLICY``.
"""

from __future__ import annotations

import logging

from rules import load_config, load_policy

_log = logging.getLogger("trust_gate")

RULES, THRESHOLDS = load_config()
POLICY = load_policy()

_log.info(
    "trust-gate loaded %d plausibility rules, %d threshold overrides, "
    "%d concordance rules, %d definitional bounds, %d resource thresholds",
    len(RULES),
    len(THRESHOLDS),
    len(POLICY["concordance_rules"]),
    len(POLICY["definitional_bounds"]),
    len(POLICY.get("resource_thresholds", {})),
)
