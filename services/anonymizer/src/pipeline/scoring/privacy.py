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
    "ip": re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    "mrn": re.compile(r"\b(?:MRN|mrn)[:\s#]?\d{4,}\b"),
}


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
            deidentified, manifest_entries, evidence
        )
        text_risk = self._text_risk(deidentified, evidence)

        risk_score = max(attacker_risk, identifier_risk, text_risk)
        passed = risk_score <= RISK_THRESHOLD

        return PrivacyDecision(
            risk_score=risk_score,
            passed=passed,
            threshold=RISK_THRESHOLD,
            attacker_risk=attacker_risk,
            identifier_risk=identifier_risk,
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
            text_risk=text_risk,
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
            from medanon_core.analytics.risk import _extract_patient_qi
        except ImportError:
            evidence.append(
                Evidence(
                    check="attacker_model",
                    value=0.0,
                    details={"reason": "medanon_core not available"},
                )
            )
            return 0.0

        qi = _extract_patient_qi(deidentified)
        suppressed = sum(1 for v in qi if not v or v in REDACTED_SENTINELS)
        total = len(qi)

        risk_map = {3: 0.0, 2: 0.10, 1: 0.30, 0: 0.60}
        risk = risk_map.get(suppressed, 0.60)

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
            from medanon_core.analytics.risk import (
                extract_quasi_identifiers,
                compute_k_anonymity,
            )
        except ImportError:
            evidence.append(
                Evidence(
                    check="attacker_model_batch",
                    value=0.0,
                    details={"reason": "medanon_core not available"},
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

    # ----- Sub-evaluator 1b: Direct identifier detection --------------------

    def _identifier_detection(
        self,
        deidentified: dict,
        manifest_entries: list[dict],
        evidence: list[Evidence],
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

        unmatched = []
        for s_path in sensitive:
            if not any(
                s_path == cp
                or cp.startswith(s_path + ".")
                or s_path.startswith(cp + ".")
                for cp in covered_paths
            ):
                # Also check if the field actually exists in the resource
                field_name = s_path.split(".")[0]
                if field_name in deidentified:
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

    # ----- Sub-evaluator 1c: Text risk (NER + regex) ------------------------

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
                    entities = adapter.detect(text, threshold=NER_THRESHOLD)
                    for entity in entities:
                        etype = entity.get("entity_type", entity.get("type", "UNKNOWN"))
                        score = entity.get("score", 0.0)
                        if score >= NER_THRESHOLD:
                            detections.append(
                                {
                                    "type": etype,
                                    "source": "ner",
                                    "confidence": round(score, 3),
                                }
                            )
                except Exception:
                    _log.debug("ner_scan_error", exc_info=True)
        except ImportError:
            pass
        return detections
