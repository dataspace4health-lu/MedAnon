"""Quality Evaluator — continuous pipeline execution metric.

Measures correctness of the transformation process. Several sub-evaluators map
to the Kahn et al. 2016 harmonized DQ framework (conformance / completeness /
plausibility), the framework named in the project's article:

1. Transformation success rate (with error-rate gates) — pipeline execution
2. Rule coverage completeness — policy coverage
3. Lightweight FHIR schema validation — FHIR StructureDefinition conformance
4. Reference integrity — Kahn *relational conformance* (refs resolve to a
   valid Type/id target; cross-resource version in ``evaluate_batch_refs``)
5. Real-world data quality (Kahn conformance + completeness + temporal &
   value plausibility)
6. Terminology binding — Kahn *value conformance*: SNOMED/LOINC/RxNorm codes
   not corrupted or sentinel-replaced by scrubbing
7. FHIR cardinality constraints — *conformance*: required (cardinality-1)
   fields intact after de-id
8. Structural diff — information-loss signal (NCP/discernibility family):
   element-count shift vs. original flags over-scrubbing
"""

from __future__ import annotations

from typing import Any

from models import Evidence, ModuleScore

# Redaction sentinels that count as "still populated / conformant" for DQ —
# a field replaced with one of these is acceptable (not over-scrubbed).
_DQ_REDACTED: frozenset[str] = frozenset(
    {"", "[redacted]", "redacted", "unknown", "masked", "removed", "anonymous"}
)

# Minimal field set each resource type should RETAIN after de-id. These are
# analytically essential, non-PII (or sentinel-replaced) fields; nulling them
# entirely is over-scrubbing and hurts data quality. Dotted paths allowed.
_DQ_EXPECTED_FIELDS: dict[str, tuple[str, ...]] = {
    "Patient": ("gender",),
    "Observation": ("status", "code"),
    "Condition": ("code",),
    "Procedure": ("status", "code"),
    "MedicationRequest": ("status", "intent"),
    "Encounter": ("status", "class"),
    "DiagnosticReport": ("status", "code"),
    "Immunization": ("status", "vaccineCode"),
    "AllergyIntolerance": ("code",),
}

# Keys whose string value is treated as a date for temporal-plausibility.
_DQ_DATE_KEYS: frozenset[str] = frozenset(
    {
        "birthDate",
        "date",
        "issued",
        "authoredOn",
        "recordedDate",
        "onsetDateTime",
        "effectiveDateTime",
        "deceasedDateTime",
        "start",
        "end",
    }
)


def _parse_date(val):
    """Parse a FHIR date / dateTime string to a ``datetime.date``; None if bad.

    FHIR allows ``YYYY``, ``YYYY-MM``, ``YYYY-MM-DD`` and full dateTimes; we take
    the leading date and try progressively shorter precisions.
    """
    if not isinstance(val, str) or len(val) < 4:
        return None
    import datetime as _dt

    head = val[:10]
    for length, fmt in ((10, "%Y-%m-%d"), (7, "%Y-%m"), (4, "%Y")):
        try:
            return _dt.datetime.strptime(head[:length], fmt).date()
        except ValueError:
            continue
    return None


class QualityEvaluator:
    """Evaluate pipeline execution quality."""

    def evaluate(
        self,
        deidentified: dict,
        manifest_entries: list[dict],
        error_count: int,
        total_count: int,
        settings: Any = None,
        original: dict | None = None,
    ) -> ModuleScore:
        evidence: list[Evidence] = []
        gates: list[str] = []

        success = self._success_rate(error_count, total_count, evidence)
        coverage = self._rule_coverage(
            deidentified, manifest_entries, settings, evidence
        )
        validation = self._schema_validation(deidentified, evidence)
        integrity = self._reference_integrity(deidentified, evidence)
        data_quality = self._data_quality(deidentified, evidence)
        terminology = self._terminology_binding(deidentified, evidence)
        cardinality = self._cardinality_constraints(deidentified, evidence)
        structural = self._structural_diff(original, deidentified, evidence)

        # Weights: correctness (success/coverage) still dominates; new metrics
        # share 0.20 that was previously split between validation+integrity.
        raw = (
            success * 0.30
            + coverage * 0.20
            + validation * 0.05
            + integrity * 0.05
            + data_quality * 0.15
            + terminology * 0.10
            + cardinality * 0.10
            + structural * 0.05
        )

        # Apply error-rate gates (non-compensatory)
        error_rate = error_count / max(total_count, 1)
        if error_rate > 0.20:
            raw = min(raw, 0.20)
            gates.append("error_rate_above_20pct")
        elif error_rate > 0.05:
            raw = min(raw, 0.60)
            gates.append("error_rate_above_5pct")

        return ModuleScore(
            name="quality",
            score=raw,
            evidence=evidence,
            gates_applied=gates,
        )

    # ----- 3a: Success rate -------------------------------------------------

    def _success_rate(
        self,
        error_count: int,
        total_count: int,
        evidence: list[Evidence],
    ) -> float:
        if total_count <= 0:
            evidence.append(
                Evidence(
                    check="success_rate",
                    value=0.0,
                    details={"reason": "no resources processed — insufficient data"},
                )
            )
            return 0.0
        rate = 1.0 - (error_count / total_count)
        severity = "info"
        if error_count / total_count > 0.20:
            severity = "critical"
        elif error_count / total_count > 0.05:
            severity = "warning"
        evidence.append(
            Evidence(
                check="success_rate",
                value=rate,
                details={"errors": error_count, "total": total_count},
                severity=severity,
            )
        )
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
            evidence.append(
                Evidence(
                    check="rule_coverage",
                    value=1.0,
                    details={
                        "reason": "no settings available — coverage not measurable"
                    },
                )
            )
            return 1.0

        rtype = deidentified.get("resourceType", "")

        # Determine which rules are applicable to this resource type AND whose
        # target root field actually exists in the resource.  Without the
        # field-existence check, wildcard rules like ``*.note.text`` are counted
        # as applicable to every resource type — even those without a ``note``
        # field — which deflates coverage to ~50%.
        applicable: set[str] = set()
        for rule in settings.rules:
            name = rule.get("name", rule.get("match", ""))
            match_expr = rule.get("match", "")
            # A rule applies if it's a wildcard (*.) or targets this resource type
            type_matches = False
            if match_expr.startswith("*.") or match_expr.startswith(f"{rtype}."):
                type_matches = True
            elif match_expr.startswith("{resourceType}."):
                type_matches = True

            if not type_matches:
                continue

            # Extract the root field targeted by this rule (e.g. "name" from
            # "Patient.name" or "*.name.family") and only count the rule as
            # applicable when the resource actually contains that field.
            parts = match_expr.split(".")
            root_field = parts[1] if len(parts) >= 2 else ""
            if root_field and root_field not in deidentified:
                continue

            # If the rule path has ≥ 3 segments (e.g. *.name.family) the rule
            # requires a nested field inside the root value.  When the root
            # value is a scalar or null it cannot contain sub-fields — the rule
            # can never fire and should not count as applicable.
            # Concrete case: Organization.name is a plain string, not a
            # HumanName object, so *.name.family is not applicable to it.
            if len(parts) >= 3 and root_field:
                root_val = deidentified.get(root_field)
                if not isinstance(root_val, (dict, list)):
                    continue

            applicable.add(name)

        if not applicable:
            evidence.append(
                Evidence(
                    check="rule_coverage",
                    value=1.0,
                    details={"reason": f"no rules applicable to {rtype}"},
                )
            )
            return 1.0

        fired = {e.get("rule", "") for e in manifest_entries}
        covered = applicable & fired
        score = len(covered) / len(applicable)

        evidence.append(
            Evidence(
                check="rule_coverage",
                value=score,
                details={
                    "applicable": len(applicable),
                    "fired": len(covered),
                    "missed": sorted(applicable - fired)[:10],
                },
            )
        )
        return score

    # ----- 3c: Schema validation (lightweight) ------------------------------

    def _schema_validation(
        self,
        deidentified: dict,
        evidence: list[Evidence],
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

        evidence.append(
            Evidence(
                check="schema_validation",
                value=score,
                details={"passed": sum(checks), "total": len(checks)},
            )
        )
        return score

    # ----- 3d: Reference integrity ------------------------------------------

    def _reference_integrity(
        self,
        deidentified: dict,
        evidence: list[Evidence],
    ) -> float:
        refs = self._collect_references(deidentified)
        if not refs:
            evidence.append(
                Evidence(
                    check="reference_integrity",
                    value=1.0,
                    details={"total_refs": 0},
                )
            )
            return 1.0

        valid = 0
        for ref in refs:
            if self._is_valid_ref(ref):
                valid += 1

        score = valid / len(refs)
        evidence.append(
            Evidence(
                check="reference_integrity",
                value=score,
                details={"valid": valid, "total": len(refs)},
            )
        )
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
        evidence.append(
            Evidence(
                check="batch_reference_integrity",
                value=max(0.0, score),
                details={"dangling": dangling, "total": len(all_refs)},
            )
        )
        return max(0.0, score)

    # ----- 3e: Real-world data quality (intrinsic, Kahn/OHDSI dimensions) ----

    def _data_quality(self, deidentified: dict, evidence: list[Evidence]) -> float:
        """Intrinsic data-quality score from the de-identified resource alone.

        Four output-only dimensions (no original needed → works on every path,
        incl. refs-only staging), each in [0,1]:

        * conformance     — values within valid ranges / value-sets
        * completeness     — expected fields still populated (not over-scrubbed)
        * temporal         — dates plausible (no future, birth ≤ death, +ve spans)
        * value_plausible  — numeric values within plausible clinical bounds

        Dimensions that don't apply to a resource return 1.0 (neutral) so a
        Patient isn't penalised for lacking Observation value ranges.
        """
        conformance = self._dq_conformance(deidentified)
        completeness = self._dq_completeness(deidentified)
        temporal = self._dq_temporal_plausibility(deidentified)
        plausible = self._dq_value_plausibility(deidentified)

        score = (conformance + completeness + temporal + plausible) / 4.0
        worst = min(conformance, completeness, temporal, plausible)
        evidence.append(
            Evidence(
                check="data_quality",
                value=score,
                details={
                    "conformance": round(conformance, 3),
                    "completeness": round(completeness, 3),
                    "temporal_plausibility": round(temporal, 3),
                    "value_plausibility": round(plausible, 3),
                },
                severity="warning" if worst < 0.7 else "info",
            )
        )
        return score

    def _dq_conformance(self, r: dict) -> float:
        """Fraction of conformance checks passed (valid codes / ranges)."""
        checks: list[bool] = []
        rtype = r.get("resourceType", "")

        # Patient.gender ∈ FHIR AdministrativeGender (or a redaction sentinel).
        if rtype == "Patient" and "gender" in r:
            g = r.get("gender")
            checks.append(
                g in ("male", "female", "other", "unknown")
                or (isinstance(g, str) and g.strip().lower() in _DQ_REDACTED)
            )

        # Status fields, when present, should be non-empty strings.
        for sf in ("status", "clinicalStatus", "verificationStatus"):
            if sf in r:
                v = r.get(sf)
                checks.append(isinstance(v, (str, dict)) and bool(v))

        # Coded fields should still carry a system+code (de-id must not strip
        # clinical codes — they're not PII). Checks any CodeableConcept.coding.
        codings = self._collect_codings(r)
        if codings:
            valid = sum(1 for c in codings if c.get("system") and c.get("code"))
            checks.append(valid / len(codings) >= 0.9)

        return sum(checks) / len(checks) if checks else 1.0

    def _dq_completeness(self, r: dict) -> float:
        """Expected-present fields still populated after de-id (not nulled).

        Over-scrubbing (deleting a field instead of replacing with a sentinel)
        destroys analytic value. We check the minimal field set each resource
        type should retain post-de-id.
        """
        rtype = r.get("resourceType", "")
        expected = _DQ_EXPECTED_FIELDS.get(rtype)
        if not expected:
            return 1.0
        present = sum(1 for f in expected if self._field_populated(r, f))
        return present / len(expected)

    def _dq_temporal_plausibility(self, r: dict) -> float:
        """Dates must be plausible: no future dates, birth ≤ death, +ve spans."""
        import datetime as _dt

        checks: list[bool] = []
        today = _dt.date.today()

        for val in self._collect_dates(r):
            d = _parse_date(val)
            if d is None:
                continue
            # No future dates (allow small clock skew → today+1).
            checks.append(d <= today + _dt.timedelta(days=1))

        # birthDate ≤ deceasedDateTime when both present.
        bd = _parse_date(r.get("birthDate"))
        dd = _parse_date(r.get("deceasedDateTime"))
        if bd and dd:
            checks.append(bd <= dd)

        # Period.start ≤ Period.end across all periods.
        for start, end in self._collect_periods(r):
            ds, de = _parse_date(start), _parse_date(end)
            if ds and de:
                checks.append(ds <= de)

        return sum(checks) / len(checks) if checks else 1.0

    def _dq_value_plausibility(self, r: dict) -> float:
        """Numeric Quantity values within broad clinically-plausible bounds.

        Catches perturbation/transformation bugs that push values to absurd
        magnitudes. Bounds are deliberately wide (we flag impossible, not
        merely unusual) and unit-aware only loosely.
        """
        checks: list[bool] = []
        for qty in self._collect_quantities(r):
            v = qty.get("value")
            if not isinstance(v, (int, float)):
                continue
            # Reject NaN/inf and absurd magnitudes; clinical values are finite
            # and within ~[-1e6, 1e6] across virtually all UCUM units.
            ok = (v == v) and abs(v) < 1_000_000  # v==v rejects NaN
            checks.append(ok)
        return sum(checks) / len(checks) if checks else 1.0

    # ----- 3f: Terminology binding ------------------------------------------

    def _terminology_binding(self, r: dict, evidence: list[Evidence]) -> float:
        """Check that clinical terminology codes are not corrupted by scrubbing.

        Scrubbing rules applied to free-text fields sometimes accidentally strip
        coding arrays or corrupt the system URI.  We verify:
        - Every coding still has a non-empty ``system`` URI.
        - Every coding still has a non-empty ``code`` value.
        - ``system`` URIs that were originally from a known vocabulary
          (LOINC, SNOMED, RxNorm, ICD-10, UCUM) still carry that URI prefix —
          de-id must not have replaced it with a sentinel.

        Returns 1.0 when no codings are present (not applicable).
        """
        from constants import CLINICAL_CODE_SYSTEMS

        codings = self._collect_codings(r)
        if not codings:
            evidence.append(
                Evidence(check="terminology_binding", value=1.0, details={"codings": 0})
            )
            return 1.0

        checks: list[bool] = []
        for c in codings:
            system = c.get("system") or ""
            code = c.get("code") or ""
            # Basic presence.
            checks.append(bool(system) and bool(code))
            # Known vocabulary systems must not be sentinel-replaced.
            if system in CLINICAL_CODE_SYSTEMS:
                # The system URI must still be the original vocabulary URI,
                # not a redaction sentinel like "[REDACTED]".
                checks.append(
                    not system.startswith("[")
                    and system.upper() not in {"REDACTED", "UNKNOWN"}
                )
                # Code must look like a code (non-empty, not a sentinel).
                checks.append(
                    bool(code)
                    and not code.startswith("[")
                    and code.upper() not in {"REDACTED", "UNKNOWN", "MASKED"}
                )

        score = sum(checks) / len(checks) if checks else 1.0
        evidence.append(
            Evidence(
                check="terminology_binding",
                value=score,
                details={
                    "coding_count": len(codings),
                    "checks_total": len(checks),
                    "checks_pass": sum(checks),
                },
                severity="warning" if score < 0.9 else "info",
            )
        )
        return score

    # ----- 3g: FHIR cardinality constraints ---------------------------------

    # Per-resource required fields: (field, min_cardinality, is_array)
    # Only the most analytically critical constraints are listed — a full
    # StructureDefinition validator is out of scope here.
    _CARDINALITY_RULES: dict[str, list[tuple[str, int, bool]]] = {
        "Patient": [
            ("resourceType", 1, False),
            ("id", 1, False),
        ],
        "Observation": [
            ("resourceType", 1, False),
            ("status", 1, False),
            ("code", 1, False),
            ("subject", 1, False),
        ],
        "Condition": [
            ("resourceType", 1, False),
            ("code", 1, False),
            ("subject", 1, False),
        ],
        "Procedure": [
            ("resourceType", 1, False),
            ("status", 1, False),
            ("code", 1, False),
            ("subject", 1, False),
        ],
        "MedicationRequest": [
            ("resourceType", 1, False),
            ("status", 1, False),
            ("intent", 1, False),
            ("subject", 1, False),
        ],
        "Encounter": [
            ("resourceType", 1, False),
            ("status", 1, False),
            ("class", 1, False),
            ("subject", 1, False),
        ],
        "DiagnosticReport": [
            ("resourceType", 1, False),
            ("status", 1, False),
            ("code", 1, False),
        ],
        "Immunization": [
            ("resourceType", 1, False),
            ("status", 1, False),
            ("vaccineCode", 1, False),
            ("patient", 1, False),
        ],
        "AllergyIntolerance": [
            ("resourceType", 1, False),
            ("patient", 1, False),
        ],
    }

    def _cardinality_constraints(self, r: dict, evidence: list[Evidence]) -> float:
        """Verify required FHIR fields were not removed by over-scrubbing.

        Checks a curated set of FHIR R4 cardinality-1 fields per resource type.
        A missing required field means de-identification destroyed structural
        validity (e.g. redacting ``Observation.status`` instead of replacing it
        with a sentinel).
        """
        rtype = r.get("resourceType", "")
        rules = self._CARDINALITY_RULES.get(
            rtype, [("resourceType", 1, False), ("id", 1, False)]
        )

        checks: list[bool] = []
        for field, _min_card, is_array in rules:
            val = r.get(field)
            if is_array:
                checks.append(isinstance(val, list) and len(val) >= _min_card)
            else:
                checks.append(val is not None and val != "" and val != [])

        score = sum(checks) / len(checks) if checks else 1.0
        failed = [rules[i][0] for i, ok in enumerate(checks) if not ok]
        evidence.append(
            Evidence(
                check="cardinality_constraints",
                value=score,
                details={
                    "resource_type": rtype,
                    "checks": len(checks),
                    "failed_fields": failed,
                },
                severity="warning" if failed else "info",
            )
        )
        return score

    # ----- 3h: Structural diff (element count shift) ------------------------

    def _structural_diff(
        self,
        original: dict | None,
        deidentified: dict,
        evidence: list[Evidence],
    ) -> float:
        """Detect over-scrubbing via element-count comparison (information loss).

        A coarse, resource-local information-loss signal in the spirit of the
        suppression component of de-identification utility metrics such as the
        Normalized Certainty Penalty (Xu et al. 2006) and discernibility
        (LeFevre et al. 2006): de-identification should *transform* values, not
        delete whole sub-trees. We count leaf values in the tree before and
        after; a large drop means elements were removed rather than
        sentinel-replaced.

        The cut-offs below are a **heuristic operating point**, not a published
        threshold — a small drop is expected (e.g. collapsing a multi-entry
        ``identifier`` array to one pseudonym), while a large drop indicates the
        config nulls fields instead of replacing them. Tune per profile.

          drop ≤ 5%   → 1.0  (expected pseudonymization shrinkage)
          drop 5–20%  → linear decay from 1.0 → 0.5
          drop > 20%  → 0.0  (severe over-scrubbing)
          no original → 1.0  (not measurable)
        """
        if original is None:
            evidence.append(
                Evidence(
                    check="structural_diff",
                    value=1.0,
                    details={"reason": "no original — not measurable"},
                )
            )
            return 1.0

        def _count_leaves(obj: Any, depth: int = 0) -> int:
            if depth > 20:
                return 0
            if isinstance(obj, dict):
                return sum(_count_leaves(v, depth + 1) for v in obj.values())
            if isinstance(obj, list):
                return sum(_count_leaves(item, depth + 1) for item in obj)
            return 1  # scalar leaf

        orig_count = _count_leaves(original)
        deid_count = _count_leaves(deidentified)

        if orig_count == 0:
            evidence.append(
                Evidence(
                    check="structural_diff",
                    value=1.0,
                    details={"reason": "empty original"},
                )
            )
            return 1.0

        drop_ratio = max(0.0, (orig_count - deid_count) / orig_count)

        if drop_ratio <= 0.05:
            score = 1.0
        elif drop_ratio <= 0.20:
            score = 1.0 - ((drop_ratio - 0.05) / 0.15) * 0.5
        else:
            score = 0.0

        evidence.append(
            Evidence(
                check="structural_diff",
                value=score,
                details={
                    "original_leaves": orig_count,
                    "deidentified_leaves": deid_count,
                    "drop_ratio": round(drop_ratio, 4),
                },
                severity="warning" if drop_ratio > 0.05 else "info",
            )
        )
        return score

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

    # ----- DQ tree collectors (bounded-depth walks) -------------------------

    def _field_populated(self, r: dict, dotted: str) -> bool:
        """True if a (possibly dotted) field path resolves to a non-empty value."""
        cur: Any = r
        for part in dotted.split("."):
            if isinstance(cur, dict):
                cur = cur.get(part)
            elif isinstance(cur, list) and cur:
                cur = cur[0].get(part) if isinstance(cur[0], dict) else None
            else:
                return False
        if cur is None:
            return False
        if isinstance(cur, (str, list, dict)):
            return len(cur) > 0
        return True

    def _collect_codings(self, obj: Any, depth: int = 0) -> list[dict]:
        if depth > 8:
            return []
        out: list[dict] = []
        if isinstance(obj, dict):
            coding = obj.get("coding")
            if isinstance(coding, list):
                out.extend(c for c in coding if isinstance(c, dict))
            for v in obj.values():
                out.extend(self._collect_codings(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                out.extend(self._collect_codings(item, depth + 1))
        return out

    def _collect_dates(self, obj: Any, depth: int = 0) -> list[str]:
        if depth > 8:
            return []
        out: list[str] = []
        if isinstance(obj, dict):
            for k, v in obj.items():
                if isinstance(v, str) and (
                    k in _DQ_DATE_KEYS or k.endswith("DateTime")
                ):
                    out.append(v)
                else:
                    out.extend(self._collect_dates(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                out.extend(self._collect_dates(item, depth + 1))
        return out

    def _collect_periods(self, obj: Any, depth: int = 0) -> list[tuple]:
        if depth > 8:
            return []
        out: list[tuple] = []
        if isinstance(obj, dict):
            if "start" in obj and "end" in obj:
                out.append((obj.get("start"), obj.get("end")))
            for v in obj.values():
                out.extend(self._collect_periods(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                out.extend(self._collect_periods(item, depth + 1))
        return out

    def _collect_quantities(self, obj: Any, depth: int = 0) -> list[dict]:
        if depth > 8:
            return []
        out: list[dict] = []
        if isinstance(obj, dict):
            # A Quantity-shaped dict has a numeric `value` (+ usually unit/code).
            if "value" in obj and isinstance(obj.get("value"), (int, float)):
                out.append(obj)
            for v in obj.values():
                out.extend(self._collect_quantities(v, depth + 1))
        elif isinstance(obj, list):
            for item in obj:
                out.extend(self._collect_quantities(item, depth + 1))
        return out
