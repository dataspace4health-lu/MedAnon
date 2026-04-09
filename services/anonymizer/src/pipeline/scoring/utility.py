"""Utility Evaluator — continuous data usability metric.

Measures how much analytical value survives de-identification via four
sub-evaluators:

1. Field retention — percentage of fields preserved
2. Semantic preservation — clinical codes (LOINC, SNOMED) remain valid
3. Temporal consistency — event date ordering preserved
4. Information loss — aggregate action severity
"""
from __future__ import annotations

from typing import Any

from pipeline.scoring.models import Evidence, ModuleScore
from pipeline.scoring.constants import (
    CLINICAL_CODE_SYSTEMS,
    DATE_FIELDS,
    INFO_LOSS_WEIGHTS,
    PERIOD_FIELDS,
)

_META_KEYS = frozenset({"meta", "resourceType"})


class UtilityEvaluator:
    """Evaluate data usability after de-identification."""

    def evaluate(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
    ) -> ModuleScore:
        evidence: list[Evidence] = []

        retention = self._field_retention(original, deidentified, manifest_entries, evidence)
        semantic = self._semantic_preservation(deidentified, evidence)
        temporal = self._temporal_consistency(original, deidentified, evidence)
        info_loss = self._information_loss(manifest_entries, evidence)

        score = retention * 0.25 + semantic * 0.30 + temporal * 0.15 + info_loss * 0.30

        return ModuleScore(name="utility", score=score, evidence=evidence)

    # ----- 2a: Field retention ----------------------------------------------

    def _field_retention(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        evidence: list[Evidence],
    ) -> float:
        if original is not None:
            orig_keys = {k for k in original if k not in _META_KEYS}
            if not orig_keys:
                evidence.append(Evidence(check="field_retention", value=1.0))
                return 1.0
            retained = sum(1 for k in orig_keys if k in deidentified)
            score = retained / len(orig_keys)
        else:
            # Estimate from manifest (no original available)
            if not manifest_entries:
                evidence.append(Evidence(
                    check="field_retention", value=0.5,
                    details={"reason": "no original and no manifest — indeterminate"},
                ))
                return 0.5
            redact_count = sum(1 for e in manifest_entries if e.get("action") == "redact")
            total = len(manifest_entries)
            score = 1.0 - (redact_count / total) if total > 0 else 1.0

        evidence.append(Evidence(
            check="field_retention", value=score,
            details={"has_original": original is not None},
        ))
        return score

    # ----- 2b: Semantic preservation ----------------------------------------

    def _semantic_preservation(
        self,
        deidentified: dict,
        evidence: list[Evidence],
    ) -> float:
        checks_pass = 0
        checks_total = 0

        # Check coding arrays
        codings = self._collect_codings(deidentified)
        for coding in codings:
            checks_total += 2
            if coding.get("system") and isinstance(coding["system"], str):
                checks_pass += 1
                if coding["system"] in CLINICAL_CODE_SYSTEMS:
                    checks_pass += 1  # bonus for known vocabulary
                    checks_total += 1
            if coding.get("code") and isinstance(coding["code"], str):
                checks_pass += 1

        # Check references are well-formed
        refs = self._collect_references(deidentified)
        for ref in refs:
            checks_total += 1
            if isinstance(ref, str) and "/" in ref and not ref.startswith("#"):
                parts = ref.split("/", 1)
                if len(parts) == 2 and parts[0] and parts[1]:
                    checks_pass += 1

        # Structural basics
        checks_total += 1
        if deidentified.get("resourceType"):
            checks_pass += 1

        score = checks_pass / checks_total if checks_total > 0 else 1.0

        evidence.append(Evidence(
            check="semantic_preservation", value=score,
            details={
                "coding_count": len(codings),
                "reference_count": len(refs),
                "checks_pass": checks_pass,
                "checks_total": checks_total,
            },
        ))
        return score

    # ----- 2c: Temporal consistency -----------------------------------------

    def _temporal_consistency(
        self,
        original: dict | None,
        deidentified: dict,
        evidence: list[Evidence],
    ) -> float:
        valid = 0
        total = 0

        # Check period start <= end within the de-identified resource
        for field_name in PERIOD_FIELDS:
            period = deidentified.get(field_name)
            if isinstance(period, dict):
                start = period.get("start", "")
                end = period.get("end", "")
                if start and end:
                    total += 1
                    if str(start) <= str(end):
                        valid += 1

        # If original is available, check relative ordering is preserved
        if original is not None:
            orig_dates = self._extract_dates(original)
            deid_dates = self._extract_dates(deidentified)
            shared_keys = set(orig_dates) & set(deid_dates)
            sorted_keys = sorted(shared_keys)
            for i in range(len(sorted_keys)):
                for j in range(i + 1, min(i + 3, len(sorted_keys))):
                    k1, k2 = sorted_keys[i], sorted_keys[j]
                    total += 1
                    orig_order = orig_dates[k1] <= orig_dates[k2]
                    deid_order = deid_dates[k1] <= deid_dates[k2]
                    if orig_order == deid_order:
                        valid += 1

        score = valid / total if total > 0 else 1.0

        evidence.append(Evidence(
            check="temporal_consistency", value=score,
            details={"valid_orderings": valid, "total_orderings": total},
        ))
        return score

    # ----- 2d: Information loss ---------------------------------------------

    def _information_loss(
        self,
        manifest_entries: list[dict],
        evidence: list[Evidence],
    ) -> float:
        if not manifest_entries:
            evidence.append(Evidence(
                check="information_loss", value=1.0,
                details={"reason": "no manifest entries"},
            ))
            return 1.0

        total_loss = sum(
            INFO_LOSS_WEIGHTS.get(e.get("action", ""), 0.5)
            for e in manifest_entries
        )
        avg_loss = total_loss / len(manifest_entries)
        score = 1.0 - avg_loss

        evidence.append(Evidence(
            check="information_loss", value=score,
            details={
                "total_actions": len(manifest_entries),
                "avg_loss": round(avg_loss, 4),
            },
        ))
        return score

    # ----- Helpers ----------------------------------------------------------

    def _collect_codings(self, obj: Any, depth: int = 0) -> list[dict]:
        if depth > 10:
            return []
        results: list[dict] = []
        if isinstance(obj, dict):
            if "coding" in obj and isinstance(obj["coding"], list):
                for c in obj["coding"]:
                    if isinstance(c, dict):
                        results.append(c)
            for v in obj.values():
                results.extend(self._collect_codings(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                results.extend(self._collect_codings(item, depth + 1))
        return results

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

    def _extract_dates(self, resource: dict) -> dict[str, str]:
        dates: dict[str, str] = {}
        for field_name in DATE_FIELDS:
            val = resource.get(field_name)
            if isinstance(val, str) and val:
                dates[field_name] = val
        return dates
