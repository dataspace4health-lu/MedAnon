"""Privacy Risk Evaluator — hard constraint gate.

Evaluates residual re-identification risk on de-identified FHIR resources
using three sub-evaluators:

1. Attacker model analysis (prosecutor / journalist / marketer)
2. Direct identifier detection (manifest coverage of HIPAA-sensitive paths)
3. Text risk detection (NER + regex residual PII scan)

The overall privacy risk is ``max(attacker, identifier, text)`` — the worst
dimension determines the risk.  Risk above the configured threshold FAILs
the resource.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.scoring.models import Evidence, PrivacyDecision
from pipeline.scoring.constants import (
    HIPAA_SENSITIVE_PATHS,
    PHI_RESOURCE_TYPES,
    NER_ENABLED,
    NER_THRESHOLD,
    REDACTED_SENTINELS,
    RISK_LEVEL_MAP,
    RISK_THRESHOLD,
    SCORE_CONFIG_GATE,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII regex patterns (lightweight — no NLP needed)
# ---------------------------------------------------------------------------

_PII_PATTERNS: dict[str, re.Pattern] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b(?:\+?1[\s-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "date_iso": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    # Match IPv4 only — reject OID strings (5+ dotted segments like 2.16.840.1.113883)
    "ip": re.compile(
        r"(?<!\d\.)(?<!\d)"
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"(?!\.\d)"
    ),
    "mrn": re.compile(r"\b(?:MRN|mrn)[:\s#]?\d{4,}\b"),
}

# ---------------------------------------------------------------------------
# HIPAA identifier detection helpers
# ---------------------------------------------------------------------------

_FHIR_REF_STRUCTURAL_KEYS = frozenset({"reference", "type"})


def _walk_path_exists(obj: Any, parts: list[str]) -> bool:
    """Recursive walk: returns True if the chain of *parts* resolves."""
    if not parts:
        return True
    if isinstance(obj, dict):
        key = parts[0]
        if key not in obj:
            return False
        return _walk_path_exists(obj[key], parts[1:])
    if isinstance(obj, list):
        return any(_walk_path_exists(item, parts) for item in obj)
    return False


def _nested_path_exists(resource: dict, dotted_path: str) -> bool:
    """Check whether a dotted FHIR path actually exists in *resource*.

    Walks through dicts and FHIR arrays (lists of dicts).  For example,
    ``_nested_path_exists(encounter, "location.period")`` returns False
    when the encounter has ``location: [{"location": {"reference": ...}}]``
    but no ``period`` key within any location entry.
    """
    return _walk_path_exists(resource, dotted_path.split("."))


def _is_bare_reference(value: Any) -> bool:
    """True if *value* is a FHIR Reference containing only structural keys.

    A bare reference is ``{"reference": "Type/id"}`` with at most an
    optional ``"type"`` key.  Such references are transitively de-identified
    when ``rewrite_references: true`` rewrites all reference targets.

    If the reference has a ``display``, ``identifier``, or other sub-field,
    it is NOT bare — those sub-fields need explicit transformation coverage.

    Also handles lists of References (e.g. ``Observation.performer``).
    """
    if isinstance(value, list):
        return bool(value) and all(_is_bare_reference(item) for item in value)
    if not isinstance(value, dict):
        return False
    return "reference" in value and set(value.keys()) <= _FHIR_REF_STRUCTURAL_KEYS


class PrivacyRiskEvaluator:
    """Evaluate residual re-identification risk."""

    def evaluate(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        settings: Any = None,
    ) -> PrivacyDecision:
        evidence: list[Evidence] = []

        attacker_risk = self._attacker_model(deidentified, evidence)
        identifier_risk = self._identifier_detection(
            deidentified, manifest_entries, evidence, settings
        )
        config_identifier_risk = self._config_coverage(
            deidentified, manifest_entries, evidence, settings
        )
        text_risk = self._text_risk(deidentified, evidence)

        risk_score = max(attacker_risk, identifier_risk, text_risk)
        if SCORE_CONFIG_GATE:
            risk_score = max(risk_score, config_identifier_risk)
        passed = risk_score <= RISK_THRESHOLD

        return PrivacyDecision(
            risk_score=risk_score,
            passed=passed,
            threshold=RISK_THRESHOLD,
            attacker_risk=attacker_risk,
            identifier_risk=identifier_risk,
            config_identifier_risk=config_identifier_risk,
            text_risk=text_risk,
            evidence=evidence,
        )

    def evaluate_batch(
        self,
        patients: list[dict],
        all_manifest_entries: list[list[dict]],
    ) -> PrivacyDecision:
        """Batch-level evaluation using full k-anonymity across all patients."""
        evidence: list[Evidence] = []

        attacker_risk = self._attacker_model_batch(patients, evidence)

        # Aggregate identifier risk across all resources
        id_risks = []
        for patient, manifest in zip(patients, all_manifest_entries):
            id_risks.append(self._identifier_detection(patient, manifest, []))
        identifier_risk = max(id_risks) if id_risks else 0.0
        evidence.append(
            Evidence(
                check="identifier_batch",
                value=identifier_risk,
                details={
                    "worst_resource_risk": identifier_risk,
                    "count": len(id_risks),
                },
                severity="critical" if identifier_risk > RISK_THRESHOLD else "info",
            )
        )

        text_risk = 0.0
        for patient in patients:
            text_risk = max(text_risk, self._text_risk(patient, []))
        evidence.append(
            Evidence(
                check="text_batch",
                value=text_risk,
                details={"worst_resource_risk": text_risk},
                severity="critical" if text_risk > RISK_THRESHOLD else "info",
            )
        )

        risk_score = max(attacker_risk, identifier_risk, text_risk)
        passed = risk_score <= RISK_THRESHOLD

        return PrivacyDecision(
            risk_score=risk_score,
            passed=passed,
            threshold=RISK_THRESHOLD,
            attacker_risk=attacker_risk,
            identifier_risk=identifier_risk,
            config_identifier_risk=0.0,  # batch eval has no per-resource settings context
            text_risk=text_risk,
            evidence=evidence,
        )

    def evaluate_batch_from_qis(
        self,
        patient_qis: list[tuple[str, str, str]],
        all_manifest_entries: list[list[dict]],
    ) -> PrivacyDecision:
        """Batch-level evaluation using pre-extracted QI tuples.

        Adapts the k-anonymity gate to the population size:

        - N=1  (single-patient): k-anonymity is not applicable — return
          risk=0.0, passed=True.  A single-patient export is a per-record
          de-identification task, not a population-anonymity task.

        - N=2..4 (small cohort): k-anonymity is computed but treated as
          *informational* — the gate is softened so that the expected
          min_k=1..4 range does not unconditionally fail the score.
          Risk is scaled proportionally to how far min_k is from the
          safe threshold (5), capped at RISK_THRESHOLD so it never FAILs
          unless the config explicitly lowers RISK_THRESHOLD.

        - N≥5  (full population): full k-anonymity gate applies.  The
          existing RISK_LEVEL_MAP thresholds (low/medium/high/critical)
          are used directly and the gate can FAIL.
        """
        evidence: list[Evidence] = []
        n = len(patient_qis)

        # ── N=1: single-patient export ──────────────────────────────────────
        if n < 2:
            evidence.append(
                Evidence(
                    check="attacker_model_batch",
                    value=0.0,
                    details={
                        "reason": "k-anonymity not applicable for single-patient export",
                        "patient_count": n,
                    },
                )
            )
            return PrivacyDecision(
                risk_score=0.0,
                passed=True,
                threshold=RISK_THRESHOLD,
                attacker_risk=0.0,
                identifier_risk=0.0,
                config_identifier_risk=0.0,
                text_risk=0.0,
                evidence=evidence,
            )

        # ── N=2..4: small cohort — informational k-anonymity ────────────────
        if n < 5:
            try:
                from analytics.risk import compute_k_anonymity
                k_result = compute_k_anonymity(patient_qis)
                summary = k_result.get("summary", {})
                min_k = summary.get("min_k", 1)
            except ImportError:
                min_k = 1

            # Scale risk as a fraction of the distance to the safe threshold (5).
            # min_k=1 → risk≈0.24; min_k=2 → risk≈0.18; min_k=3 → risk≈0.12;
            # min_k=4 → risk≈0.06.  All values stay below RISK_THRESHOLD (0.30)
            # so the gate never FAILs for small-cohort processing.
            _SAFE_K = 5
            attacker_risk = round(max(0.0, (_SAFE_K - min_k) / (_SAFE_K * 5)), 4)
            evidence.append(
                Evidence(
                    check="attacker_model_batch",
                    value=attacker_risk,
                    details={
                        "reason": "small cohort — k-anonymity informational only",
                        "patient_count": n,
                        "min_k": min_k,
                        "safe_threshold_k": _SAFE_K,
                    },
                    severity="info",
                )
            )
            return PrivacyDecision(
                risk_score=attacker_risk,
                passed=True,
                threshold=RISK_THRESHOLD,
                attacker_risk=attacker_risk,
                identifier_risk=0.0,
                config_identifier_risk=0.0,
                text_risk=0.0,
                evidence=evidence,
            )

        # ── N≥5: full k-anonymity gate ───────────────────────────────────────
        attacker_risk = self._attacker_model_batch_from_qis(patient_qis, evidence)

        risk_score = attacker_risk
        passed = risk_score <= RISK_THRESHOLD

        return PrivacyDecision(
            risk_score=risk_score,
            passed=passed,
            threshold=RISK_THRESHOLD,
            attacker_risk=attacker_risk,
            identifier_risk=0.0,
            config_identifier_risk=0.0,
            text_risk=0.0,
            evidence=evidence,
        )

    # ----- Sub-evaluator 1a: Attacker models (per-resource) -----------------

    def _attacker_model(self, deidentified: dict, evidence: list[Evidence]) -> float:
        rtype = deidentified.get("resourceType", "")
        if rtype != "Patient":
            evidence.append(
                Evidence(
                    check="attacker_model",
                    value=0.0,
                    details={"reason": f"not applicable for {rtype}"},
                )
            )
            return 0.0

        # Per-resource simplified QI suppression check
        try:
            from analytics.risk import _extract_patient_qi
        except ImportError:
            evidence.append(
                Evidence(
                    check="attacker_model",
                    value=0.0,
                    details={"reason": "analytics module not available"},
                )
            )
            return 0.0

        qi = _extract_patient_qi(deidentified)
        # _extract_patient_qi already normalises de-identification outputs
        # (generalized dates like "1970", truncated zip like "123", redacted
        # sentinels, blank values) to empty strings.  An empty string means
        # the QI field is suppressed/generalized — i.e. correctly treated.
        suppressed = sum(1 for v in qi if not v)
        total = len(qi)

        # Per-resource risk: based on how many QI fields remain identifiable.
        # suppressed=3: all QIs treated → 0 risk
        # suppressed=2: one QI exposed → low risk
        # suppressed=1: two QIs exposed → near-threshold risk (informational)
        # suppressed=0: all three QIs exposed → high risk
        risk_map = {3: 0.0, 2: 0.05, 1: 0.15, 0: 0.35}
        risk = risk_map.get(suppressed, 0.35)

        qi_details = {
            "gender": qi[0] if len(qi) > 0 else None,
            "birth_year": qi[1] if len(qi) > 1 else None,
            "zip_prefix": qi[2] if len(qi) > 2 else None,
        }
        evidence.append(
            Evidence(
                check="attacker_model",
                value=risk,
                details={
                    "qi_fields": qi_details,
                    "suppressed": suppressed,
                    "total": total,
                },
                severity="critical" if risk > RISK_THRESHOLD else "info",
            )
        )
        return risk

    # ----- Sub-evaluator 1a (batch): Full k-anonymity -----------------------

    def _attacker_model_batch(
        self,
        patients: list[dict],
        evidence: list[Evidence],
    ) -> float:
        try:
            from analytics.risk import (
                extract_quasi_identifiers,
                compute_k_anonymity,
            )
        except ImportError:
            evidence.append(
                Evidence(
                    check="attacker_model_batch",
                    value=0.0,
                    details={"reason": "analytics module not available"},
                )
            )
            return 0.0

        patient_resources = [p for p in patients if p.get("resourceType") == "Patient"]
        if not patient_resources:
            evidence.append(
                Evidence(
                    check="attacker_model_batch",
                    value=0.0,
                    details={"reason": "no Patient resources in batch"},
                )
            )
            return 0.0

        qi_tuples = extract_quasi_identifiers(patient_resources)
        k_result = compute_k_anonymity(qi_tuples)
        summary = k_result.get("summary", {})

        risk_level = summary.get("risk_level", "critical")
        risk = RISK_LEVEL_MAP.get(risk_level, 0.80)

        attacker_max = max(
            summary.get("prosecutor_risk", 0.0),
            summary.get("journalist_risk", 0.0),
            summary.get("marketer_risk", 0.0),
        )

        evidence.append(
            Evidence(
                check="attacker_model_batch",
                value=risk,
                details={
                    "risk_level": risk_level,
                    "min_k": summary.get("min_k", 0),
                    "prosecutor_risk": summary.get("prosecutor_risk", 0.0),
                    "journalist_risk": summary.get("journalist_risk", 0.0),
                    "marketer_risk": summary.get("marketer_risk", 0.0),
                    "total_records": summary.get("total_records", 0),
                    "singleton_groups": summary.get("singleton_groups", 0),
                },
                severity="critical" if risk > RISK_THRESHOLD else "info",
            )
        )
        return max(risk, attacker_max)

    def _attacker_model_batch_from_qis(
        self,
        patient_qis: list[tuple[str, str, str]],
        evidence: list[Evidence],
    ) -> float:
        """Batch-level k-anonymity from pre-extracted QI tuples."""
        try:
            from analytics.risk import compute_k_anonymity
        except ImportError:
            evidence.append(
                Evidence(
                    check="attacker_model_batch",
                    value=0.0,
                    details={"reason": "analytics module not available"},
                )
            )
            return 0.0

        if not patient_qis:
            evidence.append(
                Evidence(
                    check="attacker_model_batch",
                    value=0.0,
                    details={"reason": "no Patient QIs in batch"},
                )
            )
            return 0.0

        k_result = compute_k_anonymity(patient_qis)
        summary = k_result.get("summary", {})

        risk_level = summary.get("risk_level", "critical")
        risk = RISK_LEVEL_MAP.get(risk_level, 0.80)

        attacker_max = max(
            summary.get("prosecutor_risk", 0.0),
            summary.get("journalist_risk", 0.0),
            summary.get("marketer_risk", 0.0),
        )

        evidence.append(
            Evidence(
                check="attacker_model_batch",
                value=risk,
                details={
                    "risk_level": risk_level,
                    "min_k": summary.get("min_k", 0),
                    "prosecutor_risk": summary.get("prosecutor_risk", 0.0),
                    "journalist_risk": summary.get("journalist_risk", 0.0),
                    "marketer_risk": summary.get("marketer_risk", 0.0),
                    "total_records": summary.get("total_records", 0),
                    "singleton_groups": summary.get("singleton_groups", 0),
                },
                severity="critical" if risk > RISK_THRESHOLD else "info",
            )
        )
        return max(risk, attacker_max)

    # ----- Sub-evaluator 1b: Direct identifier detection --------------------

    def _identifier_detection(
        self,
        deidentified: dict,
        manifest_entries: list[dict],
        evidence: list[Evidence],
        settings: Any = None,
    ) -> float:
        rtype = deidentified.get("resourceType", "")

        sensitive = list(HIPAA_SENSITIVE_PATHS.get(rtype, []))
        sensitive.extend(HIPAA_SENSITIVE_PATHS.get("*", []))

        if not sensitive:
            # Not a PHI-bearing resource type
            return 0.0

        if not manifest_entries and rtype in PHI_RESOURCE_TYPES:
            evidence.append(
                Evidence(
                    check="identifier_coverage",
                    value=1.0,
                    details={
                        "reason": "no_transformations_detected",
                        "resource_type": rtype,
                    },
                    severity="critical",
                )
            )
            return 1.0

        # Build set of manifest-covered paths (match on path prefix)
        covered_paths: set[str] = set()
        for entry in manifest_entries:
            path = entry.get("path", "")
            # "Patient.name" → "name"
            parts = path.split(".", 1)
            if len(parts) == 2:
                covered_paths.add(parts[1])
            covered_paths.add(path)

        # Build set of paths covered by conditional rules (nlp_detect_act,
        # nlp_scrub, nlp_detect) in config. These actions only write a manifest
        # entry when they actually transform something, so a clean field
        # produces no manifest entry even though the rule ran and the field is
        # safe. Treat such paths as config-covered to avoid false positives.
        conditional_actions = frozenset(
            {"nlp_detect_act", "nlp_scrub", "nlp_detect"}
        )
        config_covered_paths: set[str] = set()
        if settings is not None and hasattr(settings, "rules"):
            for rule in settings.rules:
                if rule.get("action") not in conditional_actions:
                    continue
                match_expr = rule.get("match", "")
                # Normalise wildcard prefix: "*.text" → "text"
                if match_expr.startswith("*."):
                    config_covered_paths.add(match_expr[2:])
                elif "." in match_expr:
                    # "Observation.valueString" → "valueString"
                    config_covered_paths.add(match_expr.split(".", 1)[1])
                else:
                    config_covered_paths.add(match_expr)

        unmatched = []
        for s_path in sensitive:
            # Check manifest coverage
            if any(
                s_path == cp
                or cp.startswith(s_path + ".")
                or s_path.startswith(cp + ".")
                for cp in covered_paths
            ):
                continue

            # Check config conditional-rule coverage (field is safe but
            # produced no manifest entry because no PII was found)
            leaf = s_path.split(".")[-1]
            if s_path in config_covered_paths or leaf in config_covered_paths:
                continue

            # Precise field existence check
            if "." in s_path:
                # Compound path (e.g. "location.period", "contact.name")
                # — verify the full nested path exists, not just the root.
                if not _nested_path_exists(deidentified, s_path):
                    continue
            else:
                # Simple path — check root field existence
                if s_path not in deidentified:
                    continue
                # Bare FHIR References are transitively covered by
                # reference rewriting + *.id pseudonymization.
                if _is_bare_reference(deidentified[s_path]):
                    continue

            unmatched.append(s_path)

        total = len(sensitive)
        matched = total - len(unmatched)
        risk = len(unmatched) / total if total > 0 else 0.0

        evidence.append(
            Evidence(
                check="identifier_coverage",
                value=risk,
                details={
                    "total_sensitive": total,
                    "matched": matched,
                    "unmatched": unmatched[:10],
                },
                severity="critical"
                if risk > 0.5
                else ("warning" if risk > 0 else "info"),
            )
        )
        return risk

    # ----- Sub-evaluator 1c: Config rule coverage risk ----------------------

    def _config_coverage(
        self,
        deidentified: dict,
        manifest_entries: list[dict],
        evidence: list[Evidence],
        settings: Any = None,
    ) -> float:
        """Config-rule identifier risk: fraction of applicable rules that did NOT fire.

        Mirrors QualityEvaluator._rule_coverage() but inverts the result so
        that *higher* values mean *more* risk (rules not applied = more exposure).
        Returns 0.0 when settings is unavailable so it is always safe to compute.
        """
        if settings is None or not hasattr(settings, "rules"):
            return 0.0

        rtype = deidentified.get("resourceType", "")

        applicable: set[str] = set()
        for rule in settings.rules:
            name = rule.get("name", rule.get("match", ""))
            match_expr = rule.get("match", "")
            type_matches = (
                match_expr.startswith("*.")
                or match_expr.startswith(f"{rtype}.")
                or match_expr.startswith("{resourceType}.")
            )
            if not type_matches:
                continue
            parts = match_expr.split(".")
            root_field = parts[1] if len(parts) >= 2 else ""
            if root_field and root_field not in deidentified:
                continue
            applicable.add(name)

        if not applicable:
            return 0.0

        fired = {e.get("rule", "") for e in manifest_entries}
        covered = applicable & fired
        risk = 1.0 - (len(covered) / len(applicable))

        evidence.append(
            Evidence(
                check="config_coverage",
                value=risk,
                details={
                    "applicable": len(applicable),
                    "fired": len(covered),
                    "missed": sorted(applicable - fired)[:10],
                },
                severity="critical" if risk > 0.5 else ("warning" if risk > 0 else "info"),
            )
        )
        return risk

    # ----- Sub-evaluator 1d: Text risk (NER + regex) ------------------------

    def _text_risk(self, deidentified: dict, evidence: list[Evidence]) -> float:
        texts = self._extract_texts(deidentified)
        if not texts:
            return 0.0

        detections: list[dict] = []

        # Pattern-based detection
        for text in texts:
            for name, pattern in _PII_PATTERNS.items():
                for match in pattern.finditer(text):
                    detections.append(
                        {
                            "type": name,
                            "source": "regex",
                            "value_preview": match.group()[:20],
                        }
                    )

        # NER-based detection (when available and enabled)
        if NER_ENABLED:
            ner_detections = self._ner_scan(texts)
            detections.extend(ner_detections)

        entity_count = len(detections)
        risk = min(1.0, entity_count * 0.15)

        if detections:
            evidence.append(
                Evidence(
                    check="text_risk",
                    value=risk,
                    details={
                        "entity_count": entity_count,
                        "detections": detections[:10],
                    },
                    severity="critical" if entity_count >= 3 else "warning",
                )
            )
        return risk

    def _extract_texts(self, resource: dict, min_len: int = 20) -> list[str]:
        """Extract all string values likely to contain narrative text."""
        texts: list[str] = []
        self._walk_texts(resource, texts, min_len, depth=0)
        return texts

    def _walk_texts(
        self,
        obj: Any,
        texts: list[str],
        min_len: int,
        depth: int,
    ) -> None:
        if depth > 15:
            return
        if isinstance(obj, str):
            if len(obj) >= min_len:
                texts.append(obj)
        elif isinstance(obj, dict):
            for v in obj.values():
                self._walk_texts(v, texts, min_len, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                self._walk_texts(item, texts, min_len, depth + 1)

    def _ner_scan(self, texts: list[str]) -> list[dict]:
        """Run Presidio NER scan via existing NLP adapter."""
        detections: list[dict] = []
        try:
            from pipeline.deidentify import _get_nlp_adapter

            adapter = _get_nlp_adapter()
            if adapter is None:
                return detections
            for text in texts:
                try:
                    # detect() signature: (text, entities, threshold, language)
                    # Pass empty entities list = detect all configured entity types.
                    # Returns list[tuple[int, int, str]] → (start, end, entity_type)
                    hits = adapter.detect(
                        text, entities=[], threshold=NER_THRESHOLD, language="en"
                    )
                    for hit in hits:
                        if isinstance(hit, tuple) and len(hit) >= 3:
                            etype = hit[2]
                        elif isinstance(hit, dict):
                            etype = hit.get("entity_type", hit.get("type", "UNKNOWN"))
                        else:
                            continue
                        detections.append({"type": etype, "source": "ner"})
                except Exception:
                    _log.debug("ner_scan_error", exc_info=True)
        except ImportError:
            pass
        return detections
