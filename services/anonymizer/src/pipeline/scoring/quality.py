"""Quality Evaluator — continuous pipeline execution metric.

Measures correctness of the transformation process via four sub-evaluators:

1. Transformation success rate (with error-rate gates)
2. Rule coverage completeness
3. Lightweight FHIR schema validation
4. Reference integrity
"""
from __future__ import annotations

from typing import Any

from pipeline.scoring.models import Evidence, ModuleScore


class QualityEvaluator:
    """Evaluate pipeline execution quality."""

    def evaluate(
        self,
        deidentified: dict,
        manifest_entries: list[dict],
        error_count: int,
        total_count: int,
        settings: Any = None,
    ) -> ModuleScore:
        evidence: list[Evidence] = []
        gates: list[str] = []

        success = self._success_rate(error_count, total_count, evidence)
        coverage = self._rule_coverage(deidentified, manifest_entries, settings, evidence)
        validation = self._schema_validation(deidentified, evidence)
        integrity = self._reference_integrity(deidentified, evidence)

        raw = success * 0.40 + coverage * 0.30 + validation * 0.15 + integrity * 0.15

        # Apply error-rate gates (non-compensatory)
        error_rate = error_count / max(total_count, 1)
        if error_rate > 0.20:
            raw = min(raw, 0.20)
            gates.append("error_rate_above_20pct")
        elif error_rate > 0.05:
            raw = min(raw, 0.60)
            gates.append("error_rate_above_5pct")

        return ModuleScore(
            name="quality", score=raw, evidence=evidence, gates_applied=gates,
        )

    # ----- 3a: Success rate -------------------------------------------------

    def _success_rate(
        self, error_count: int, total_count: int, evidence: list[Evidence],
    ) -> float:
        if total_count <= 0:
            evidence.append(Evidence(
                check="success_rate", value=0.0,
                details={"reason": "no resources processed — insufficient data"},
            ))
            return 0.0
        rate = 1.0 - (error_count / total_count)
        severity = "info"
        if error_count / total_count > 0.20:
            severity = "critical"
        elif error_count / total_count > 0.05:
            severity = "warning"
        evidence.append(Evidence(
            check="success_rate", value=rate,
            details={"errors": error_count, "total": total_count},
            severity=severity,
        ))
        return max(0.0, rate)

    # ----- 3b: Rule coverage ------------------------------------------------

    def _rule_coverage(
        self,
        deidentified: dict,
        manifest_entries: list[dict],
        settings: Any,
        evidence: list[Evidence],
    ) -> float:
        if settings is None or not hasattr(settings, "rules"):
            evidence.append(Evidence(
                check="rule_coverage", value=0.5,
                details={"reason": "no settings available — indeterminate"},
                severity="warning",
            ))
            return 0.5

        rtype = deidentified.get("resourceType", "")

        # Determine which rules are applicable to this resource type
        applicable: set[str] = set()
        for rule in settings.rules:
            name = rule.get("name", rule.get("match", ""))
            match_expr = rule.get("match", "")
            # A rule applies if it's a wildcard (*.) or targets this resource type
            if match_expr.startswith("*.") or match_expr.startswith(f"{rtype}."):
                applicable.add(name)
            elif match_expr.startswith("{resourceType}."):
                applicable.add(name)

        if not applicable:
            evidence.append(Evidence(
                check="rule_coverage", value=1.0,
                details={"reason": f"no rules applicable to {rtype}"},
            ))
            return 1.0

        fired = {e.get("rule", "") for e in manifest_entries}
        covered = applicable & fired
        score = len(covered) / len(applicable)

        evidence.append(Evidence(
            check="rule_coverage", value=score,
            details={
                "applicable": len(applicable),
                "fired": len(covered),
                "missed": sorted(applicable - fired)[:10],
            },
        ))
        return score

    # ----- 3c: Schema validation (lightweight) ------------------------------

    def _schema_validation(
        self, deidentified: dict, evidence: list[Evidence],
    ) -> float:
        checks: list[bool] = []

        # Has resourceType
        checks.append(bool(deidentified.get("resourceType")))

        # Has id (even if pseudonymized)
        checks.append(bool(deidentified.get("id")))

        # No invalid empty arrays where FHIR expects at least 1 element
        checks.append(not self._has_empty_required_arrays(deidentified))

        # meta.tag well-formed if present
        checks.append(self._meta_valid(deidentified))

        score = sum(checks) / len(checks) if checks else 1.0

        evidence.append(Evidence(
            check="schema_validation", value=score,
            details={"passed": sum(checks), "total": len(checks)},
        ))
        return score

    # ----- 3d: Reference integrity ------------------------------------------

    def _reference_integrity(
        self, deidentified: dict, evidence: list[Evidence],
    ) -> float:
        refs = self._collect_references(deidentified)
        if not refs:
            evidence.append(Evidence(
                check="reference_integrity", value=1.0,
                details={"total_refs": 0},
            ))
            return 1.0

        valid = 0
        for ref in refs:
            if self._is_valid_ref(ref):
                valid += 1

        score = valid / len(refs)
        evidence.append(Evidence(
            check="reference_integrity", value=score,
            details={"valid": valid, "total": len(refs)},
        ))
        return score

    def evaluate_batch_refs(
        self,
        batch: list[dict],
        evidence: list[Evidence],
    ) -> float:
        """Check cross-resource reference integrity within a batch."""
        all_ids: set[str] = set()
        all_refs: list[str] = []
        for r in batch:
            rtype = r.get("resourceType", "")
            rid = r.get("id", "")
            if rtype and rid:
                all_ids.add(f"{rtype}/{rid}")
            all_refs.extend(self._collect_references(r))

        if not all_refs:
            return 1.0

        dangling = 0
        for ref in all_refs:
            if ref.startswith("http") or ref.startswith("#") or ref.startswith("urn:"):
                continue
            if ref not in all_ids:
                dangling += 1

        score = 1.0 - (dangling / len(all_refs))
        evidence.append(Evidence(
            check="batch_reference_integrity", value=max(0.0, score),
            details={"dangling": dangling, "total": len(all_refs)},
        ))
        return max(0.0, score)

    # ----- Helpers ----------------------------------------------------------

    def _has_empty_required_arrays(self, resource: dict) -> bool:
        """Check for empty arrays that should have at least 1 element."""
        for key in ("name", "identifier", "telecom", "address"):
            val = resource.get(key)
            if isinstance(val, list) and len(val) == 0:
                return True
        return False

    def _meta_valid(self, resource: dict) -> bool:
        meta = resource.get("meta")
        if meta is None:
            return True
        if not isinstance(meta, dict):
            return False
        tags = meta.get("tag")
        if tags is None:
            return True
        if not isinstance(tags, list):
            return False
        for tag in tags:
            if not isinstance(tag, dict):
                return False
        return True

    def _collect_references(self, obj: Any, depth: int = 0) -> list[str]:
        if depth > 10:
            return []
        refs: list[str] = []
        if isinstance(obj, dict):
            ref = obj.get("reference")
            if isinstance(ref, str) and ref:
                refs.append(ref)
            for v in obj.values():
                refs.extend(self._collect_references(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                refs.extend(self._collect_references(item, depth + 1))
        return refs

    def _is_valid_ref(self, ref: str) -> bool:
        if not ref:
            return False
        if ref.startswith("#") or ref.startswith("urn:"):
            return True
        if ref.startswith("http://") or ref.startswith("https://"):
            return True
        parts = ref.split("/", 1)
        return len(parts) == 2 and bool(parts[0]) and bool(parts[1])
