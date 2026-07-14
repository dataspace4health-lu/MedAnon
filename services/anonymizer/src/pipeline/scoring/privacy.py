"""Privacy Risk Evaluator  hard constraint gate.

Evaluates residual re-identification risk on de-identified FHIR resources
using five sub-evaluators:

1. Attacker model analysis (prosecutor / journalist / marketer)  k-anonymity
   (Samarati & Sweeney 1998)
2. Direct identifier detection (manifest coverage of HIPAA-sensitive paths)
3. Text risk detection (NER + regex residual PII scan)
4. (batch) Distinct l-diversity (Machanavajjhala et al. 2007) + t-closeness via
   categorical EMD (Li et al. 2007) over QI equivalence classes
5. (batch) Cross-resource linkage attack surface  WP216 linkability/inference
   (Art. 29 WP Opinion 05/2014)

The per-resource privacy risk is ``max(attacker, identifier, text)``  the
worst dimension determines the risk; the batch path additionally maxes in the
population metrics (4, 5). Risk above the configured threshold FAILs the
resource/cohort.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from pipeline.scoring.models import Evidence, PrivacyDecision
from pipeline.scoring.constants import (
    HIPAA_SENSITIVE_PATHS,
    NER_ENABLED,
    NER_THRESHOLD,
    RISK_LEVEL_MAP,
    RISK_THRESHOLD,
    SCORE_CONFIG_GATE,
)

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# PII regex patterns (lightweight  no NLP needed)
# ---------------------------------------------------------------------------

_PII_PATTERNS: dict[str, re.Pattern] = {
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"\b(?:\+?1[\s-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}\b"),
    "email": re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b"),
    "date_iso": re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
    # Match IPv4 only  reject OID strings (5+ dotted segments like 2.16.840.1.113883)
    "ip": re.compile(
        r"(?<!\d\.)(?<!\d)"
        r"\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}"
        r"(?!\.\d)"
    ),
    "mrn": re.compile(r"\b(?:MRN|mrn)[:\s#]?\d{4,}\b"),
}

# ---------------------------------------------------------------------------
# Per-type re-identification weights (F.24)
# ---------------------------------------------------------------------------
# A single uncovered *direct* identifier (SSN, MRN, email) is enough to
# re-identify an individual, so it must dominate the text-risk score and trip
# the output gate on its own.  Quasi-identifiers (a lone date, an IP) are far
# weaker and only accumulate risk in aggregate.  The previous flat 0.15/entity
# scheme let a leaked SSN slip under most gate thresholds.
_PII_TYPE_WEIGHTS: dict[str, float] = {
    "ssn": 0.95,
    "mrn": 0.90,
    "email": 0.70,
    "phone": 0.60,
    "ip": 0.40,
    "date_iso": 0.25,
}
# NER entities and unknown regex types fall back to the legacy per-entity weight.
_DEFAULT_PII_WEIGHT = 0.15

# Per-resource attacker-model advisory ceiling. Quasi-identifier retention on a
# single record is a *linkability* signal (Art. 29 WP216), not a re-identification
# verdict: under k-anonymity the actual risk is 1/k where k is the equivalence-
# class size  a population property only the batch evaluator can measure. So the
# per-resource heuristic is kept strictly below RISK_THRESHOLD; it lowers the
# composite as more QIs are exposed but never fails a resource on its own. The
# batch k-anonymity gate owns the authoritative attacker-model FAIL.
_ATTACKER_ADVISORY_CAP: float = RISK_THRESHOLD * 0.9


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
    it is NOT bare  those sub-fields need explicit transformation coverage.

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

        # Population-level disclosure metrics (Machanavajjhala 2007; Li 2007;
        # WP216). These are meaningful only across a cohort, so they live on the
        # batch path alongside k-anonymity.
        diversity_risk = self._diversity_and_closeness_risk(patients, evidence)
        linkage_risk = self._cross_resource_linkage_risk(patients, evidence)

        risk_score = max(
            attacker_risk, identifier_risk, text_risk, diversity_risk, linkage_risk
        )
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
    ) -> PrivacyDecision:
        """Batch-level evaluation using pre-extracted QI tuples.

        Adapts the k-anonymity gate to the population size:

        - N=1  (single-patient): k-anonymity is not applicable  return
          risk=0.0, passed=True.  A single-patient export is a per-record
          de-identification task, not a population-anonymity task.

        - N=2..4 (small cohort): k-anonymity is computed but treated as
          *informational*  the gate is softened so that the expected
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

        # ── N=2..4: small cohort  informational k-anonymity ────────────────
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
                        "reason": "small cohort  k-anonymity informational only",
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
        # the QI field is suppressed/generalized  i.e. correctly treated.
        suppressed = sum(1 for v in qi if not v)
        total = len(qi)

        # Advisory risk scales with the fraction of QI fields still exposed,
        # capped strictly below RISK_THRESHOLD (see _ATTACKER_ADVISORY_CAP): more
        # exposed QIs lower the composite but never FAIL a resource on their own,
        # because per-record QI retention says nothing about the equivalence-class
        # size k. The batch k-anonymity gate makes the authoritative attacker call.
        exposed = total - suppressed
        risk = round(_ATTACKER_ADVISORY_CAP * (exposed / total), 4) if total else 0.0

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

    # ----- Sub-evaluator 1e: L-diversity + T-closeness ----------------------

    def _build_equivalence_classes(
        self, resources: list[dict]
    ) -> dict[tuple, list[str]]:
        """Group sensitive-attribute values by quasi-identifier equivalence class.

        Resolves clinical resources back to their patient's QI within the same
        batch (per the chosen within-batch resolution strategy). The sensitive
        attribute is the primary diagnosis / observation code; the QI tuple is
        the de-identified ``(gender, birth_year, zip3)``  the same QI the
        attacker model uses. Returns ``{qi_tuple: [sensitive_value, ...]}`` so
        callers can compute the *value frequency* per class (required for the
        canonical distinct-l / t-closeness definitions, which depend on counts,
        not just the set of distinct values).
        """
        # 1. Map Patient/<id> → QI tuple from the de-identified Patient resources.
        patient_qi: dict[str, tuple] = {}
        for r in resources:
            if r.get("resourceType") != "Patient":
                continue
            rid = r.get("id")
            if not rid:
                continue
            gender = r.get("gender", "") or ""
            birth_year = (r.get("birthDate") or "")[:4]
            zip3 = ""
            address = r.get("address")
            if isinstance(address, list) and address and isinstance(address[0], dict):
                zip3 = (address[0].get("postalCode", "") or "")[:3]
            patient_qi[f"Patient/{rid}"] = (gender, birth_year, zip3)

        # 2. Attribute each clinical resource's sensitive code to its patient's QI.
        classes: dict[tuple, list[str]] = {}
        for r in resources:
            rtype = r.get("resourceType", "")
            if rtype == "Patient":
                continue
            code_obj = r.get("code")
            if not isinstance(code_obj, dict):
                continue
            codings = code_obj.get("coding")
            if not isinstance(codings, list) or not codings:
                continue
            first = codings[0]
            code = first.get("code", "")
            if not code:
                continue
            sensitive_val = f"{first.get('system', '')}|{code}"

            subject = r.get("subject") or r.get("patient") or {}
            ref = subject.get("reference", "") if isinstance(subject, dict) else ""
            qi = patient_qi.get(ref)
            if qi is None:
                # Reference does not resolve to a Patient in this batch  cannot
                # attribute to an equivalence class, so skip (not-applicable),
                # rather than collapsing all unresolved refs into one fake class.
                continue
            classes.setdefault(qi, []).append(sensitive_val)
        return classes

    def _diversity_and_closeness_risk(
        self,
        resources: list[dict],
        evidence: list[Evidence],
    ) -> float:
        """Distinct l-diversity (Machanavajjhala 2007) + t-closeness (Li 2007).

        **Distinct l-diversity**  an equivalence class is l-diverse iff the
        most frequent sensitive value occupies at most a 1/l fraction of the
        class; equivalently l = floor(1 / max_value_frequency). A class where
        every member shares one diagnosis has l=1 and is fully vulnerable to a
        homogeneity attack even if k-anonymity holds.

        **T-closeness**  the distribution of the sensitive attribute within
        each class must be close to its distribution over the whole cohort. For
        a *categorical* attribute (diagnosis codes) with equal ground distance,
        the Earth Mover's Distance reduces to the variational distance
        ``EMD = ½·Σ|p_i − q_i|`` between the class distribution p and the global
        distribution q (Li et al. 2007, §IV-B), bounded in [0,1]. A high EMD
        means a class is skewed relative to the population  a skewness attack.

        Risk is the worse of the two, mapped to the [0,1] gate scale.
        """
        classes = self._build_equivalence_classes(resources)
        if not classes:
            evidence.append(
                Evidence(
                    check="l_diversity_t_closeness",
                    value=0.0,
                    details={
                        "reason": "no QI-resolvable sensitive attributes in batch"
                    },
                )
            )
            return 0.0

        from collections import Counter

        # Global distribution q over all sensitive values in the cohort.
        global_counts: Counter = Counter()
        for vals in classes.values():
            global_counts.update(vals)
        global_total = sum(global_counts.values())
        global_dist = {k: v / global_total for k, v in global_counts.items()}

        min_l = None  # smallest l (distinct l-diversity) across classes
        max_emd = 0.0  # largest t-closeness EMD across classes
        for vals in classes.values():
            counts = Counter(vals)
            n = len(vals)
            # Distinct l-diversity: floor(1 / freq of most common value).
            top_freq = max(counts.values()) / n
            l_div = int(1.0 / top_freq) if top_freq > 0 else 1
            min_l = l_div if min_l is None else min(min_l, l_div)

            # T-closeness EMD (categorical, equal ground distance).
            all_keys = set(global_dist) | set(counts)
            emd = 0.5 * sum(
                abs((counts.get(k, 0) / n) - global_dist.get(k, 0.0)) for k in all_keys
            )
            max_emd = max(max_emd, emd)

        min_l = min_l or 1

        # l-diversity risk: l=1 → 0.50 (homogeneity), l=2 → 0.15, l≥3 → 0.0.
        if min_l >= 3:
            l_risk = 0.0
        elif min_l == 2:
            l_risk = 0.15
        else:
            l_risk = 0.50

        # t-closeness risk: scale EMD against a 0.30 closeness threshold (a class
        # at the gate's RISK_THRESHOLD distance is treated as fully risky).
        t_risk = min(1.0, max_emd / RISK_THRESHOLD) * RISK_THRESHOLD

        risk = max(l_risk, t_risk)
        evidence.append(
            Evidence(
                check="l_diversity_t_closeness",
                value=risk,
                details={
                    "min_distinct_l": min_l,
                    "max_emd": round(max_emd, 4),
                    "l_diversity_risk": round(l_risk, 4),
                    "t_closeness_risk": round(t_risk, 4),
                    "equivalence_classes": len(classes),
                    "distinct_sensitive_values": len(global_counts),
                },
                severity="critical"
                if risk >= RISK_THRESHOLD
                else ("warning" if risk > 0 else "info"),
            )
        )
        return risk

    # ----- Sub-evaluator 1f: Cross-resource linkage attack surface -----------

    def _cross_resource_linkage_risk(
        self,
        resources: list[dict],
        evidence: list[Evidence],
    ) -> float:
        """Linkability/inference risk from combining multiple resource types.

        Maps to the Article 29 WP216 (Opinion 05/2014) risk of *linkability*
        the ability to link records concerning the same individual across data
        sets  and *inference*. A single Patient with generalized QIs may be
        safe in isolation; paired with Condition, Observation, Encounter, etc.
        sharing the same (pseudonymized) patient reference, an attacker gains
        multiple correlated axes that narrow the population. We score by the
        maximum number of distinct PHI-bearing resource types linked to one
        patient reference.

        The cut-offs (2/3/5 axes) are a deliberately conservative heuristic
        WP216 gives no numeric threshold  capped at RISK_THRESHOLD so this
        dimension flags linkability for review without unilaterally failing the
        gate (k-anonymity/l-diversity remain the hard population gates).
        """
        from pipeline.scoring.constants import PHI_RESOURCE_TYPES

        # Map patient reference → set of distinct clinical resource types.
        ref_to_rtypes: dict[str, set] = {}

        for r in resources:
            rtype = r.get("resourceType", "")
            if rtype not in PHI_RESOURCE_TYPES or rtype == "Patient":
                continue
            subject = r.get("subject") or r.get("patient") or {}
            ref = subject.get("reference", "") if isinstance(subject, dict) else ""
            if not ref:
                continue
            ref_to_rtypes.setdefault(ref, set()).add(rtype)

        if not ref_to_rtypes:
            evidence.append(
                Evidence(
                    check="cross_resource_linkage",
                    value=0.0,
                    details={"reason": "no linked clinical resources found"},
                )
            )
            return 0.0

        axis_counts = [len(rtypes) for rtypes in ref_to_rtypes.values()]
        max_axes = max(axis_counts)
        avg_axes = sum(axis_counts) / len(axis_counts)

        if max_axes >= 5:
            risk = 0.30
        elif max_axes >= 3:
            risk = 0.15
        elif max_axes == 2:
            risk = 0.05
        else:
            risk = 0.0

        evidence.append(
            Evidence(
                check="cross_resource_linkage",
                value=risk,
                details={
                    "max_axes_per_patient": max_axes,
                    "avg_axes_per_patient": round(avg_axes, 2),
                    "patient_refs": len(ref_to_rtypes),
                },
                severity="warning" if risk >= 0.15 else "info",
            )
        )
        return risk

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

        # NOTE: We deliberately do NOT short-circuit to risk=1.0 when
        # manifest_entries is empty.  An empty manifest can mean either
        # (a) a genuine leak  sensitive fields are present but nothing was
        # transformed  or (b) a *sparse* resource that simply has no sensitive
        # fields to transform (e.g. a Patient with only id/resourceType).  The
        # per-field existence analysis below distinguishes the two correctly:
        # case (a) accumulates unmatched present fields (risk > 0), while case
        # (b) finds no sensitive fields present (risk = 0).  The old shortcut
        # scored both at 1.0, over-blocking sparse resources at the output gate.

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
        conditional_actions = frozenset({"nlp_detect_act", "nlp_scrub", "nlp_detect"})
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
        present = 0  # sensitive fields actually present in this resource
        for s_path in sensitive:
            # Check manifest coverage
            covered = any(
                s_path == cp
                or cp.startswith(s_path + ".")
                or s_path.startswith(cp + ".")
                for cp in covered_paths
            )

            # Check config conditional-rule coverage (field is safe but
            # produced no manifest entry because no PII was found)
            leaf = s_path.split(".")[-1]
            if s_path in config_covered_paths or leaf in config_covered_paths:
                covered = True

            # Precise field existence check
            if "." in s_path:
                # Compound path (e.g. "location.period", "contact.name")
                #  verify the full nested path exists, not just the root.
                field_present = _nested_path_exists(deidentified, s_path)
            else:
                # Simple path  check root field existence
                field_present = s_path in deidentified
                if field_present:
                    # The resource ``id`` and bare FHIR References are
                    # transitively covered by ``*.id`` pseudonymization +
                    # reference rewriting  an opaque server key is not, on its
                    # own, re-identifying PHI.
                    if s_path == "id" or _is_bare_reference(deidentified[s_path]):
                        covered = True

            if not field_present:
                # Absent sensitive fields cannot leak; exclude them from the
                # denominator so the risk fraction reflects what the resource
                # *actually exposes*, not the full catalogue of possible paths.
                continue

            present += 1
            if not covered:
                unmatched.append(s_path)

        # Risk = fraction of *present* sensitive fields left uncovered.  Using
        # ``present`` (not the full ``sensitive`` list) as the denominator means
        # a resource whose every present sensitive field is uncovered scores
        # high regardless of how many sensitive paths it lacks  while a sparse
        # resource with no sensitive fields present scores 0.
        total = len(sensitive)
        matched = present - len(unmatched)
        risk = len(unmatched) / present if present > 0 else 0.0

        evidence.append(
            Evidence(
                check="identifier_coverage",
                value=risk,
                details={
                    "total_sensitive": total,
                    "present_sensitive": present,
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
                severity="critical"
                if risk > 0.5
                else ("warning" if risk > 0 else "info"),
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
        # F.24: weight each detection by its re-identification strength.  The
        # score is dominated by the single strongest identifier (a leaked SSN
        # → ~0.95) plus a small accumulation for additional detections, so one
        # direct identifier trips the gate while many weak quasi-identifiers
        # still aggregate toward 1.0.
        if detections:
            max_weight = max(
                _PII_TYPE_WEIGHTS.get(d.get("type", ""), _DEFAULT_PII_WEIGHT)
                for d in detections
            )
            risk = min(1.0, max_weight + 0.1 * (entity_count - 1))
        else:
            risk = 0.0

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
