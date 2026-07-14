"""Utility Evaluator — continuous data usability metric.

Measures how much analytical value survives de-identification via seven
sub-evaluators:

1. Field retention — percentage of fields preserved
2. Semantic preservation — clinical codes (LOINC, SNOMED) remain valid
3. Temporal consistency — event date ordering preserved
4. Information loss — aggregate action severity (cf. NCP / discernibility,
   Xu et al. 2006; LeFevre et al. 2006)
5. Pseudonym consistency — ID mapping is injective (no entity collisions),
   so cohort joins survive (ENISA; ISO/TS 25237)
6. Longitudinal linkability — identity fields pseudonymized deterministically
   so a subject links across encounters (ENISA; ISO/TS 25237)

Batch-only (``evaluate_batch``):
7. Distribution fidelity — aggregate distributions agree with source
   (Kahn et al. 2016 *atemporal plausibility*, via total-variation distance)
"""

from __future__ import annotations

from typing import Any

from models import Evidence, ModuleScore
from constants import (
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

        retention = self._field_retention(
            original, deidentified, manifest_entries, evidence
        )
        semantic = self._semantic_preservation(deidentified, evidence)
        temporal = self._temporal_consistency(original, deidentified, evidence)
        info_loss = self._information_loss(manifest_entries, evidence)
        pseudonym_consistency = self._pseudonym_consistency(
            original, deidentified, evidence
        )
        longitudinal = self._longitudinal_linkability(
            original, deidentified, manifest_entries, evidence
        )

        # Weights adjusted to accommodate new metrics while keeping existing
        # ones dominant. Pseudonym-consistency and longitudinal each get 0.10.
        score = (
            retention * 0.20
            + semantic * 0.25
            + temporal * 0.15
            + info_loss * 0.20
            + pseudonym_consistency * 0.10
            + longitudinal * 0.10
        )

        return ModuleScore(name="utility", score=score, evidence=evidence)

    def evaluate_batch(
        self,
        originals: list[dict] | None,
        deidentified_batch: list[dict],
        all_manifest_entries: list[list[dict]],
    ) -> ModuleScore:
        """Batch-level utility evaluation including statistical distribution fidelity."""
        evidence: list[Evidence] = []

        # Per-resource scores averaged.
        per_scores: list[float] = []
        for i, deid in enumerate(deidentified_batch):
            orig = originals[i] if originals and i < len(originals) else None
            manifest = all_manifest_entries[i] if i < len(all_manifest_entries) else []
            retention = self._field_retention(orig, deid, manifest, [])
            semantic = self._semantic_preservation(deid, [])
            temporal = self._temporal_consistency(orig, deid, [])
            info_loss = self._information_loss(manifest, [])
            pseudonym_consistency = self._pseudonym_consistency(orig, deid, [])
            longitudinal = self._longitudinal_linkability(orig, deid, manifest, [])
            per_scores.append(
                retention * 0.20
                + semantic * 0.25
                + temporal * 0.15
                + info_loss * 0.20
                + pseudonym_consistency * 0.10
                + longitudinal * 0.10
            )

        avg_per_resource = sum(per_scores) / len(per_scores) if per_scores else 1.0

        # Batch-only metric: statistical distribution fidelity.
        stat_fidelity = self._statistical_distribution_fidelity(
            originals or [], deidentified_batch, evidence
        )

        # stat_fidelity contributes 10% of the batch score.
        score = avg_per_resource * 0.90 + stat_fidelity * 0.10

        evidence.append(
            Evidence(
                check="batch_avg_per_resource",
                value=avg_per_resource,
                details={"resource_count": len(deidentified_batch)},
            )
        )

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
                evidence.append(
                    Evidence(
                        check="field_retention",
                        value=0.5,
                        details={
                            "reason": "no original and no manifest — indeterminate"
                        },
                    )
                )
                return 0.5
            redact_count = sum(
                1 for e in manifest_entries if e.get("action") == "redact"
            )
            total = len(manifest_entries)
            score = 1.0 - (redact_count / total) if total > 0 else 1.0

        evidence.append(
            Evidence(
                check="field_retention",
                value=score,
                details={"has_original": original is not None},
            )
        )
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

        evidence.append(
            Evidence(
                check="semantic_preservation",
                value=score,
                details={
                    "coding_count": len(codings),
                    "reference_count": len(refs),
                    "checks_pass": checks_pass,
                    "checks_total": checks_total,
                },
            )
        )
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

        evidence.append(
            Evidence(
                check="temporal_consistency",
                value=score,
                details={"valid_orderings": valid, "total_orderings": total},
            )
        )
        return score

    # ----- 2d: Information loss ---------------------------------------------

    def _information_loss(
        self,
        manifest_entries: list[dict],
        evidence: list[Evidence],
    ) -> float:
        if not manifest_entries:
            evidence.append(
                Evidence(
                    check="information_loss",
                    value=0.5,
                    details={"reason": "no manifest entries — indeterminate"},
                    severity="warning",
                )
            )
            return 0.5

        total_loss = sum(
            INFO_LOSS_WEIGHTS.get(e.get("action", ""), 0.5) for e in manifest_entries
        )
        avg_loss = total_loss / len(manifest_entries)
        score = 1.0 - avg_loss

        evidence.append(
            Evidence(
                check="information_loss",
                value=score,
                details={
                    "total_actions": len(manifest_entries),
                    "avg_loss": round(avg_loss, 4),
                },
            )
        )
        return score

    # ----- 2e: Pseudonym consistency (injectivity for linkage) --------------

    def _pseudonym_consistency(
        self,
        original: dict | None,
        deidentified: dict,
        evidence: list[Evidence],
    ) -> float:
        """Verify ID pseudonymization preserves the reference graph 1:1.

        This is the *utility* facet of reference handling (relational-conformance
        format validity is checked by QualityEvaluator under Kahn relational
        conformance — it is not duplicated here). For analytic linkage to
        survive, the mapping original-ID → pseudonym-ID must be **injective**:
        N distinct referenced IDs in the source must remain N distinct
        referenced IDs in the output. A collision (two source IDs → one
        pseudonym) silently merges two entities' records and corrupts every
        downstream cohort join, so it is penalised in proportion to the collapse.

        Returns 1.0 when no original is available (injectivity is not
        measurable from the output alone).
        """
        deid_refs = self._collect_references(deidentified)
        deid_relative = {
            r
            for r in deid_refs
            if not r.startswith("http")
            and not r.startswith("#")
            and not r.startswith("urn:")
        }

        if original is None:
            evidence.append(
                Evidence(
                    check="pseudonym_consistency",
                    value=1.0,
                    details={"reason": "no original — injectivity not measurable"},
                )
            )
            return 1.0

        orig_relative = {
            r
            for r in self._collect_references(original)
            if not r.startswith("http")
            and not r.startswith("#")
            and not r.startswith("urn:")
        }

        if not orig_relative:
            evidence.append(
                Evidence(check="pseudonym_consistency", value=1.0, details={"refs": 0})
            )
            return 1.0

        # Injective mapping ⇒ |distinct out| == |distinct in|. A drop signals
        # collisions; ratio is the fraction of distinct entities that survived.
        score = min(1.0, len(deid_relative) / len(orig_relative))
        evidence.append(
            Evidence(
                check="pseudonym_consistency",
                value=score,
                details={
                    "distinct_refs_original": len(orig_relative),
                    "distinct_refs_deidentified": len(deid_relative),
                    "collisions": max(0, len(orig_relative) - len(deid_relative)),
                },
                severity="warning" if score < 1.0 else "info",
            )
        )
        return score

    # ----- 2f: Distribution fidelity — Kahn atemporal plausibility ----------

    def _statistical_distribution_fidelity(
        self,
        originals: list[dict],
        deidentified_batch: list[dict],
        evidence: list[Evidence],
    ) -> float:
        """Atemporal-plausibility check (Kahn et al. 2016): do the cohort's
        aggregate distributions still agree with the source after de-id?

        Kahn's *atemporal plausibility* asks whether values, distributions and
        densities match expected values. De-identification should leave
        population-level distributions of non-identifying attributes intact; a
        material shift signals over-scrubbing or a systematic transformation
        bug. Each dimension is compared by **total variation distance**
        (½·Σ|p_i − q_i|), the same statistical-distance family used for
        t-closeness, converted to a [0,1] fidelity score.

        Dimensions checked:
        - Resource type distribution (the mix should be identical).
        - Gender distribution for Patient resources (preserved or
          sentinel-replaced, not zeroed out).
        - Clinical code presence rate (fraction of resources retaining codes).

        Returns 1.0 when no originals are available (batch utility evaluates
        per-resource only in that case).
        """
        if not originals or len(originals) != len(deidentified_batch):
            evidence.append(
                Evidence(
                    check="stat_distribution_fidelity",
                    value=1.0,
                    details={"reason": "no originals available — not measurable"},
                )
            )
            return 1.0

        checks: list[float] = []

        # 1. Resource type distribution (must be identical).
        def _rtype_dist(batch: list[dict]) -> dict:
            dist: dict[str, int] = {}
            for r in batch:
                rt = r.get("resourceType", "")
                dist[rt] = dist.get(rt, 0) + 1
            return dist

        orig_dist = _rtype_dist(originals)
        deid_dist = _rtype_dist(deidentified_batch)
        if orig_dist == deid_dist:
            checks.append(1.0)
        else:
            all_types = set(orig_dist) | set(deid_dist)
            n = len(originals)
            tvd = sum(
                abs(orig_dist.get(t, 0) - deid_dist.get(t, 0)) for t in all_types
            ) / (2 * n)
            checks.append(max(0.0, 1.0 - tvd))

        # 2. Gender distribution for Patient resources.
        orig_patients = [r for r in originals if r.get("resourceType") == "Patient"]
        deid_patients = [
            r for r in deidentified_batch if r.get("resourceType") == "Patient"
        ]
        if orig_patients and deid_patients:

            def _gender_dist(pts: list[dict]) -> dict:
                d: dict[str, int] = {}
                for p in pts:
                    g = (p.get("gender") or "unknown").lower()
                    d[g] = d.get(g, 0) + 1
                return d

            og = _gender_dist(orig_patients)
            dg = _gender_dist(deid_patients)
            all_g = set(og) | set(dg)
            n_p = len(orig_patients)
            tvd_g = sum(abs(og.get(g, 0) - dg.get(g, 0)) for g in all_g) / (2 * n_p)
            checks.append(max(0.0, 1.0 - tvd_g * 2))  # gender TVD weighted x2

        # 3. Clinical code presence rate (fraction of resources with ≥1 coding).
        def _has_coding(r: dict) -> bool:
            return bool(self._collect_codings(r))

        orig_code_rate = sum(1 for r in originals if _has_coding(r)) / len(originals)
        deid_code_rate = sum(1 for r in deidentified_batch if _has_coding(r)) / len(
            deidentified_batch
        )
        code_rate_delta = abs(orig_code_rate - deid_code_rate)
        checks.append(max(0.0, 1.0 - code_rate_delta * 2))

        score = sum(checks) / len(checks) if checks else 1.0
        evidence.append(
            Evidence(
                check="stat_distribution_fidelity",
                value=score,
                details={
                    "resource_count": len(originals),
                    "checks": len(checks),
                    "orig_code_rate": round(orig_code_rate, 3),
                    "deid_code_rate": round(deid_code_rate, 3),
                },
                severity="warning" if score < 0.85 else "info",
            )
        )
        return score

    # ----- 2g: Longitudinal linkability -------------------------------------

    def _longitudinal_linkability(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        evidence: list[Evidence],
    ) -> float:
        """Check that pseudonymization is consistent for longitudinal linkage.

        ENISA (*Pseudonymisation techniques and best practices*) and ISO/TS
        25237 require that, where longitudinal linkage is intended, the same
        input must map to the same pseudonym deterministically — otherwise a
        subject's records across encounters can no longer be joined. We cannot
        verify cross-encounter consistency from a single resource, but we can
        check that the transformation chosen for identity fields is itself
        deterministic:

        1. The resource still has an ``id`` field (not redacted to nothing).
        2. The ``id`` action in the manifest used a deterministic action
           (``cryptohash``, ``gpas_pseudonymize``, ``tokenize``, ``encrypt``)
           rather than ``redact`` or ``substitute`` (which produce random values
           on each call).
        3. All ``identifier`` array entries that were transformed used
           deterministic actions (same logic).

        Returns 1.0 when no manifest indicates non-deterministic actions on
        identity fields, or when no identity fields exist.
        """
        # A de-identified resource without an id cannot be tracked longitudinally.
        if not deidentified.get("id"):
            evidence.append(
                Evidence(
                    check="longitudinal_linkability",
                    value=0.0,
                    details={
                        "reason": "id field absent — longitudinal tracking impossible"
                    },
                    severity="warning",
                )
            )
            return 0.0

        _DETERMINISTIC = frozenset(
            {"cryptohash", "gpas_pseudonymize", "tokenize", "encrypt"}
        )
        _IDENTITY_FIELDS = frozenset({"id", "identifier"})

        if not manifest_entries:
            # No manifest — assume deterministic (can't tell either way).
            evidence.append(
                Evidence(
                    check="longitudinal_linkability",
                    value=1.0,
                    details={"reason": "no manifest — determinism assumed"},
                )
            )
            return 1.0

        identity_entries = [
            e
            for e in manifest_entries
            if any(f in (e.get("path", "") or "") for f in _IDENTITY_FIELDS)
        ]

        if not identity_entries:
            evidence.append(
                Evidence(
                    check="longitudinal_linkability",
                    value=1.0,
                    details={"identity_entries": 0},
                )
            )
            return 1.0

        deterministic = sum(
            1 for e in identity_entries if e.get("action", "") in _DETERMINISTIC
        )
        score = deterministic / len(identity_entries)

        evidence.append(
            Evidence(
                check="longitudinal_linkability",
                value=score,
                details={
                    "identity_entries": len(identity_entries),
                    "deterministic": deterministic,
                    "non_deterministic": [
                        e.get("action")
                        for e in identity_entries
                        if e.get("action", "") not in _DETERMINISTIC
                    ][:5],
                },
                severity="warning" if score < 1.0 else "info",
            )
        )
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
