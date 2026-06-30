"""Scoring audit — concise post-export Markdown report generator.

``ScoreAuditCollector`` wraps ``ScoreCollector`` and accumulates per-resource
evidence during scoring.  After processing all resources, call
``generate_report()`` to produce a focused, actionable report.

Design principles:
- Executive summary at top with clear PASS/FAIL
- Only show problems (no filler for passing checks)
- Actionable recommendations with YAML snippets
- Reference material belongs in docs, not in every report
"""

from __future__ import annotations

import collections
import datetime
from typing import Any

from pipeline.scoring.engine import ScoreCollector
from pipeline.scoring.models import ScoreResult
from pipeline.scoring.constants import INFO_LOSS_WEIGHTS, RISK_THRESHOLD

_MAX_EXAMPLES = 15
_MAX_DIMENSION_SAMPLES = 5000
_MAX_PER_TYPE_DIM_SAMPLES = 2000
_MAX_WORST_RESOURCES_PER_TYPE = 5

# Sub-dimension metadata: (dim_name, module, weight). Single source of truth
# referenced by both the Sub-dimension Averages table and the new Methodology
# / Score Composition / Reference Appendix sections.
_DIM_META: list[tuple[str, str, float]] = [
    ("field_retention", "utility", 0.25),
    ("semantic_preservation", "utility", 0.30),
    ("temporal_consistency", "utility", 0.15),
    ("information_loss", "utility", 0.30),
    ("success_rate", "quality", 0.40),
    ("rule_coverage", "quality", 0.30),
    ("schema_validation", "quality", 0.15),
    ("reference_integrity", "quality", 0.15),
]


class _TypeStats:
    """Per-resource-type accumulator."""

    __slots__ = (
        "total",
        "pass_count",
        "fail_count",
        "composite_sum",
        "privacy_score_sum",
        "utility_sum",
        "quality_sum",
        "privacy_fail_reasons",
        "uncovered_hipaa",
        "text_detections",
        # Per-type detail tracking — powers the "Score Composition" section.
        "dim_samples",  # dict[str, list[float]] — sub-dim values seen on this type
        "action_counts",  # Counter — actions actually applied to this type
        "risk_scores",  # list[float] — per-resource privacy risk
        "worst_resources",  # list[(composite, resource_id)] — N lowest-scoring
    )

    def __init__(self) -> None:
        self.total: int = 0
        self.pass_count: int = 0
        self.fail_count: int = 0
        self.composite_sum: float = 0.0
        self.privacy_score_sum: float = 0.0
        self.utility_sum: float = 0.0
        self.quality_sum: float = 0.0
        self.privacy_fail_reasons: collections.Counter = collections.Counter()
        self.uncovered_hipaa: collections.Counter = collections.Counter()
        self.text_detections: list[dict] = []
        self.dim_samples: dict[str, list[float]] = {dim: [] for dim, _, _ in _DIM_META}
        self.action_counts: collections.Counter = collections.Counter()
        self.risk_scores: list[float] = []
        self.worst_resources: list[tuple[float, str]] = []


class ScoreAuditCollector:
    """Drop-in replacement for ``ScoreCollector`` that also builds audit data."""

    def __init__(
        self,
        config_profile: str = "auto",
        job_id: str = "",
        scored_at: str = "",
    ) -> None:
        self._inner = ScoreCollector(config_profile=config_profile)
        self._config_profile = config_profile
        self._job_id = job_id
        self._scored_at = scored_at or datetime.datetime.now(
            datetime.timezone.utc
        ).isoformat().replace("+00:00", "Z")

        self._by_type: dict[str, _TypeStats] = {}
        self._action_totals: collections.Counter = collections.Counter()
        self._fired_rules: collections.Counter = collections.Counter()
        self._missed_rules_by_type: dict[
            str, set[str]
        ] = {}  # resource_type -> missed rule names
        self._dim_samples: dict[str, list[float]] = {
            "field_retention": [],
            "semantic_preservation": [],
            "temporal_consistency": [],
            "information_loss": [],
            "success_rate": [],
            "rule_coverage": [],
            "schema_validation": [],
            "reference_integrity": [],
        }
        self._uncovered_hipaa: collections.Counter = collections.Counter()
        self._text_pattern_counts: collections.Counter = collections.Counter()
        self._text_detection_examples: list[dict] = []

    def _get_type_stats(self, rtype: str) -> _TypeStats:
        if rtype not in self._by_type:
            self._by_type[rtype] = _TypeStats()
        return self._by_type[rtype]

    def record_resource(
        self,
        original: dict | None,
        deidentified: dict,
        manifest_entries: list[dict],
        settings: Any = None,
    ) -> ScoreResult:
        result = self._inner.record_resource(
            original, deidentified, manifest_entries, settings
        )
        rtype = deidentified.get("resourceType", "Unknown")
        ts = self._get_type_stats(rtype)
        ts.total += 1

        if result.decision == "PASS":
            ts.pass_count += 1
        else:
            ts.fail_count += 1
        ts.composite_sum += result.composite

        # Normalized privacy score: 1 - risk/threshold (higher = better, same direction as composite)
        if result.privacy.threshold > 0:
            ts.privacy_score_sum += max(
                0.0, 1.0 - result.privacy.risk_score / result.privacy.threshold
            )

        if result.utility:
            ts.utility_sum += result.utility.score
            for ev in result.utility.evidence:
                samples = self._dim_samples.get(ev.check)
                if samples is not None and len(samples) < _MAX_DIMENSION_SAMPLES:
                    samples.append(ev.value)

        if result.quality:
            ts.quality_sum += result.quality.score
            for ev in result.quality.evidence:
                samples = self._dim_samples.get(ev.check)
                if samples is not None and len(samples) < _MAX_DIMENSION_SAMPLES:
                    samples.append(ev.value)
                # Track missed rules per resource type for clearer reporting
                if ev.check == "rule_coverage":
                    for missed in ev.details.get("missed", []):
                        if rtype not in self._missed_rules_by_type:
                            self._missed_rules_by_type[rtype] = set()
                        self._missed_rules_by_type[rtype].add(missed)

        for ev in result.privacy.evidence:
            if ev.check == "identifier_coverage":
                unmatched = ev.details.get("unmatched", [])
                for path in unmatched:
                    ts.uncovered_hipaa[path] += 1
                    self._uncovered_hipaa[path] += 1
                if ev.severity in ("warning", "critical"):
                    ts.privacy_fail_reasons["identifier_not_covered"] += 1

            elif ev.check in ("text_risk",) and ev.value > 0:
                for det in ev.details.get("detections", []):
                    pattern_name = det.get("type", "unknown")
                    self._text_pattern_counts[pattern_name] += 1
                    if len(self._text_detection_examples) < _MAX_EXAMPLES:
                        self._text_detection_examples.append(
                            {"resource_type": rtype, **det}
                        )
                ts.text_detections.extend(
                    ev.details.get("detections", [])[
                        : _MAX_EXAMPLES - len(ts.text_detections)
                    ]
                )
                ts.privacy_fail_reasons["text_risk"] += 1

            elif ev.severity == "critical" and result.decision == "FAIL":
                ts.privacy_fail_reasons[ev.check] += 1

        # Per-type sub-dimension samples — needed for Score Composition section
        if result.utility:
            for ev in result.utility.evidence:
                bucket = ts.dim_samples.get(ev.check)
                if bucket is not None and len(bucket) < _MAX_PER_TYPE_DIM_SAMPLES:
                    bucket.append(ev.value)
        if result.quality:
            for ev in result.quality.evidence:
                bucket = ts.dim_samples.get(ev.check)
                if bucket is not None and len(bucket) < _MAX_PER_TYPE_DIM_SAMPLES:
                    bucket.append(ev.value)

        # Per-type privacy risk distribution
        ts.risk_scores.append(result.privacy.risk_score)

        # Worst-N resources (lowest composite). Cheap O(N log N) maintenance
        # because the cap is small (5).
        if (
            len(ts.worst_resources) < _MAX_WORST_RESOURCES_PER_TYPE
            or result.composite < ts.worst_resources[-1][0]
        ):
            ts.worst_resources.append(
                (result.composite, result.resource_id or "(no id)")
            )
            ts.worst_resources.sort(key=lambda x: x[0])
            del ts.worst_resources[_MAX_WORST_RESOURCES_PER_TYPE:]

        for entry in manifest_entries:
            action = entry.get("action", "unknown")
            self._action_totals[action] += 1
            # Per-type action counts — drives the "actions on this resource type" view
            entry_path = entry.get("path") or ""
            entry_rtype = entry_path.split(".", 1)[0] if entry_path else ""
            if entry_rtype == rtype or not entry_rtype:
                ts.action_counts[action] += 1
            rule = entry.get("rule", "")
            if rule:
                self._fired_rules[rule] += 1

        return result

    def record_error(self) -> None:
        self._inner.record_error()
        ts = self._get_type_stats("(error)")
        ts.total += 1
        ts.fail_count += 1

    def aggregate(self) -> dict:
        return self._inner.aggregate()

    def generate_report(
        self,
        settings: Any = None,
        export_meta: dict | None = None,
    ) -> str:
        summary = self._inner.aggregate()
        meta = export_meta or {}
        return _render_report(
            summary=summary,
            by_type=self._by_type,
            action_totals=self._action_totals,
            fired_rules=self._fired_rules,
            missed_rules_by_type=self._missed_rules_by_type,
            dim_samples=self._dim_samples,
            uncovered_hipaa=self._uncovered_hipaa,
            text_pattern_counts=self._text_pattern_counts,
            text_detection_examples=self._text_detection_examples,
            config_profile=self._config_profile,
            job_id=self._job_id,
            scored_at=self._scored_at,
            settings=settings,
            export_meta=meta,
        )


# ---------------------------------------------------------------------------
# Report renderer
# ---------------------------------------------------------------------------


def _avg(samples: list[float]) -> float | None:
    return round(sum(samples) / len(samples), 4) if samples else None


def _pct(v: float | None) -> str:
    return f"{v * 100:.1f}%" if v is not None else "n/a"


def _letter_grade(composite: float) -> str:
    """Return A/B/C/D/F letter grade matching the bulk-export UI badge thresholds."""
    if composite >= 90:
        return "A"
    if composite >= 75:
        return "B"
    if composite >= 60:
        return "C"
    if composite >= 40:
        return "D"
    return "F"


def _render_report(
    summary: dict,
    by_type: dict[str, _TypeStats],
    action_totals: collections.Counter,
    fired_rules: collections.Counter,
    missed_rules_by_type: dict[str, set[str]],
    dim_samples: dict[str, list[float]],
    uncovered_hipaa: collections.Counter,
    text_pattern_counts: collections.Counter,
    text_detection_examples: list[dict],
    config_profile: str,
    job_id: str,
    scored_at: str,
    settings: Any,
    export_meta: dict,
) -> str:
    lines: list[str] = []
    W = lines.append

    total = summary.get("total_scored", 0)
    pass_count = summary.get("pass_count", 0)
    fail_count = summary.get("fail_count", 0)
    error_count = summary.get("error_count", 0)
    avg_composite = summary.get("avg_composite", 0.0)
    avg_utility = summary.get("avg_utility", 0.0)
    avg_quality = summary.get("avg_quality", 0.0)
    batch_privacy = summary.get("batch_privacy") or {}

    # Compute metrics for issue detection
    il_samples = dim_samples.get("information_loss", [])
    il_avg = _avg(il_samples)
    fr_avg = _avg(dim_samples.get("field_retention", []))
    ri_avg = _avg(dim_samples.get("reference_integrity", []))

    # Collect all issues for summary
    issues: list[tuple[str, str]] = []  # (severity, message)

    # Uncovered HIPAA paths: severity depends on whether those resources
    # actually failed the privacy gate (fail_count > 0) or the risk was
    # below the threshold on every resource (gate passed, sub-threshold risk).
    if uncovered_hipaa:
        if fail_count > 0:
            issues.append(
                (
                    "CRITICAL",
                    f"{len(uncovered_hipaa)} HIPAA paths not covered by rules (caused failures)",
                )
            )
        else:
            issues.append(
                (
                    "LOW",
                    f"{len(uncovered_hipaa)} HIPAA paths partially uncovered (below risk threshold — all resources passed)",
                )
            )
    if text_pattern_counts:
        issues.append(
            (
                "CRITICAL",
                f"PII detected in {sum(text_pattern_counts.values())} text fields",
            )
        )
    if batch_privacy:
        min_k = None
        for ev in batch_privacy.get("evidence", []):
            if ev.get("check") == "attacker_model_batch":
                min_k = ev.get("details", {}).get("min_k")
        if min_k is not None and isinstance(min_k, int) and min_k < 5:
            issues.append(("HIGH", f"k-anonymity low (min_k={min_k})"))
    if il_avg is not None and il_avg < 0.55:
        issues.append(("HIGH", f"High information loss ({_pct(1 - il_avg)} lost)"))
    if fr_avg is not None and fr_avg < 0.60:
        issues.append(("HIGH", f"Low field retention ({_pct(fr_avg)})"))
    if ri_avg is not None and ri_avg < 0.95:
        issues.append(("MEDIUM", f"Reference integrity {_pct(ri_avg)}"))
    if missed_rules_by_type:
        # Count unique rules that never fired at all (not per-type occurrences)
        never_fired_for_count = set()
        for rules in missed_rules_by_type.values():
            never_fired_for_count.update(rules)
        truly_missed_count = [
            r for r in never_fired_for_count if fired_rules.get(r, 0) == 0
        ]
        if truly_missed_count:
            issues.append(("LOW", f"{len(truly_missed_count)} rules never fired"))
    if error_count > 0:
        issues.append(("MEDIUM", f"{error_count} processing errors"))

    # -----------------------------------------------------------------------
    # Executive Summary (always show)
    # -----------------------------------------------------------------------
    grade = _letter_grade(avg_composite)
    W("# Scoring Report")
    W("")
    W(
        f"**Job:** `{job_id or '(ad-hoc)'}`  |  **Profile:** `{config_profile}`  |  **Resources:** {total:,}  |  **Grade:** {grade}"
    )
    W("")

    # Overall verdict
    if fail_count == 0 and not issues:
        W(f"## PASS — Grade {grade} — No Issues")
        W("")
        W(
            f"All {total:,} resources passed privacy checks. Composite score: **{avg_composite:.1f}%**"
        )
    elif fail_count == 0:
        W(f"## PASS — Grade {grade} — {len(issues)} Issue(s)")
        W("")
        W(
            "All resources passed privacy gate, but there are optimization opportunities."
        )
    else:
        W(f"## FAIL — Grade {grade} — {fail_count:,} Resources Failed")
        W("")
        pct_fail = fail_count / max(total, 1) * 100
        W(f"**{pct_fail:.0f}%** of resources failed the privacy gate (composite = 0).")
    W("")

    # Compute overall avg privacy score from per-type accumulators
    _privacy_count = sum(ts.total for ts in by_type.values())
    avg_privacy = (
        sum(ts.privacy_score_sum for ts in by_type.values()) / _privacy_count
        if _privacy_count > 0
        else 0.0
    )

    # Quick stats
    W("| Metric | Value |")
    W("|--------|-------|")
    W(f"| Composite | **{avg_composite:.1f}%** |")
    W(f"| Privacy Score | {avg_privacy * 100:.1f}% |")
    W(f"| Utility | {_pct(avg_utility)} |")
    W(f"| Quality | {_pct(avg_quality)} |")
    W(f"| Pass / Fail | {pass_count:,} / {fail_count:,} |")
    W("")

    # Sub-dimension averages — all 8 dimensions with their module and weight.
    # Uses module-level _DIM_META (single source of truth shared with the
    # Methodology and Score Composition sections below).
    if total > 0 and any(dim_samples.get(d) for d, _, _ in _DIM_META):
        W("### Sub-dimension Averages")
        W("")
        W("| Dimension | Module | Weight | Avg Score |")
        W("|-----------|--------|--------|-----------|")
        for dim, module, weight in _DIM_META:
            avg = _avg(dim_samples.get(dim, []))
            W(f"| {dim.replace('_', ' ')} | {module} | {weight:.2f} | {_pct(avg)} |")
        W("")

    # Issue summary
    if issues:
        W("### Issues Found")
        W("")
        for severity, msg in issues:
            W(f"- **{severity}:** {msg}")
        W("")

    #    # -----------------------------------------------------------------------
    #    # Scoring Methodology — explain WHAT we measured and HOW it was computed.
    #    # Always rendered so every report is self-documenting.
    #    # -----------------------------------------------------------------------
    #    W("### How These Numbers Are Computed")
    #    W("")
    #    W("**Composite formula (multiplicative — no dimension compensates for another):**")
    #    W("")
    #    W("```")
    #    W("composite = privacy_score × utility_score × quality_score      (range 0..1)")
    #    W(f"privacy_score = max(0, 1 − risk_score / risk_threshold)         (threshold = {RISK_THRESHOLD})")
    #    W("utility_score = Σ (sub_dim_value × sub_dim_weight)             (4 sub-dims, weights sum to 1.0)")
    #    W("quality_score = Σ (sub_dim_value × sub_dim_weight)             (4 sub-dims, weights sum to 1.0)")
    #    W("```")
    #    W("")
    #    W("**What each module measures:**")
    #    W("")
    #    W(f"- **Privacy** (gate) — runs 4 sub-evaluators per resource (attacker model, identifier coverage, config coverage, text-PII scan). The maximum risk across all of them must be ≤ `{RISK_THRESHOLD}` (configurable via `MEDANON_SCORE_RISK_THRESHOLD`) or the resource FAILs.")
    #    W("- **Utility** (informational) — how much analytical value survived the de-identification.")
    #    W("- **Quality** (informational) — how well the rule pipeline executed and produced valid FHIR.")
    #    W("")
    #    W("**Letter grade scale (applied to composite):**")
    #    W("`A ≥ 90%` · `B ≥ 75%` · `C ≥ 60%` · `D ≥ 40%` · `F < 40%`")
    #    W("")
    #
    #    # -----------------------------------------------------------------------
    #    # Problems Only — skip sections with no issues
    #    # -----------------------------------------------------------------------

    # Uncovered HIPAA paths — grouped by resource type with qualified paths
    if uncovered_hipaa:
        W("---")
        W("")

        # Build (rtype, qualified_path, bare_path, count) rows from per-type accumulators
        type_path_rows: list[tuple[str, str, str, int]] = []
        for rtype, ts in by_type.items():
            for path, count in ts.uncovered_hipaa.items():
                qualified = (
                    f"{rtype}.{path}" if not path.startswith(rtype + ".") else path
                )
                type_path_rows.append((rtype, qualified, path, count))
        type_path_rows.sort(key=lambda x: -x[3])

        failing_type_count = sum(1 for ts in by_type.values() if ts.uncovered_hipaa)

        if fail_count > 0:
            W("## Uncovered HIPAA Paths")
            W("")
            W(
                f"**{len(uncovered_hipaa)} fields** across **{failing_type_count} resource type(s)** "
                f"have no transformation rule and caused **{fail_count:,} resource(s) to fail** the privacy gate."
            )
        else:
            W("## Partially Uncovered HIPAA Paths (below risk threshold)")
            W("")
            W(
                f"**{len(uncovered_hipaa)} fields** across **{failing_type_count} resource type(s)** "
                f"had no rule but risk stayed below threshold — **no failures**. Often means a conditional "
                f"rule (`nlp_detect_act`) passed because no PII was detected."
            )
        W("")
        W("| Resource Type | Field | Resources Affected | Suggested Fix |")
        W("|---|---|---|---|")
        for rtype, qualified, bare, count in type_path_rows:
            action, _ = _suggest_action(bare)
            W(f"| `{rtype}` | `{qualified}` | {count:,} | `{action}` |")
        W("")

        # Per-type YAML fix blocks for types that are actually failing
        failing_types_ordered = sorted(
            {
                rtype
                for rtype, _, _, _ in type_path_rows
                if by_type[rtype].fail_count > 0
            },
            key=lambda rt: -by_type[rt].fail_count,
        )
        if failing_types_ordered:
            W("**Rules to add — by resource type:**")
            W("")
            for rtype in failing_types_ordered[:5]:
                ts = by_type[rtype]
                rows_for_type = [
                    (qp, bp) for rt, qp, bp, _ in type_path_rows if rt == rtype
                ]
                W(f"**`{rtype}`** — {ts.fail_count:,}/{ts.total:,} failed:")
                W("```yaml")
                for qualified, bare in rows_for_type[:6]:
                    action, note = _suggest_action(bare)
                    name = f"{action} {qualified.replace('.', '-').lower()}"
                    W(f"- name: {name}")
                    W(f'  match: "{qualified}"')
                    W(f"  action: {action}  # {note}")
                W("```")
                W("")

    # CRITICAL: Text PII
    if text_pattern_counts:
        W("---")
        W("")
        W("## PII Detected in Text Fields")
        W("")
        W(
            f"**{sum(text_pattern_counts.values())} matches** across {len(text_pattern_counts)} pattern types:"
        )
        W("")
        W("| Pattern | Count | Example |")
        W("|---------|-------|---------|")
        shown: dict[str, str] = {}
        for ex in text_detection_examples:
            ptype = ex.get("type", "?")
            if ptype not in shown:
                shown[ptype] = ex.get("value_preview", "—")[:30]
        for pattern, count in text_pattern_counts.most_common(5):
            W(f"| `{pattern}` | {count:,} | `{shown.get(pattern, '—')}` |")
        W("")
        W("**Fix:**")
        W("```yaml")
        W("- name: scrub narrative")
        W('  match: "*.text.div"')
        W("  action: nlp_scrub")
        W("")
        W("- name: scrub notes")
        W('  match: "*.note.text"')
        W("  action: nlp_scrub")
        W("```")

    # k-Anonymity issues
    if batch_privacy:
        for ev in batch_privacy.get("evidence", []):
            if ev.get("check") == "attacker_model_batch":
                details = ev.get("details", {})
                min_k = details.get("min_k")
                singletons = details.get("singleton_groups", 0)
                if min_k is not None and isinstance(min_k, int) and min_k < 5:
                    W("---")
                    W("")
                    W("## k-Anonymity Risk")
                    W("")
                    W(
                        f"**min_k = {min_k}** — {singletons} patient(s) are uniquely identifiable."
                    )
                    W("")
                    W("| Metric | Value |")
                    W("|--------|-------|")
                    W(f"| Smallest group | {min_k} patients |")
                    W(f"| Singleton groups | {singletons} |")
                    W(f"| Prosecutor risk | {details.get('prosecutor_risk', 0):.2%} |")
                    W("")
                    W("**Fix:** Use `config_hipaa_safe_harbor` or add:")
                    W("```yaml")
                    W("- name: generalize birth year")
                    W("  match: Patient.birthDate")
                    W("  action: generalize")
                    W("  params: { level: year }")
                    W("```")

    # Information loss / utility issues
    if il_avg is not None and il_avg < 0.55:
        W("---")
        W("")
        W("## High Information Loss")
        W("")
        W(
            f"**{_pct(1 - il_avg)} of analytical value lost** — utility may be too low for research."
        )
        W("")
        # Show action breakdown
        total_actions = sum(action_totals.values())
        high_loss = [
            (a, c, INFO_LOSS_WEIGHTS.get(a, 0.5))
            for a, c in action_totals.most_common()
            if INFO_LOSS_WEIGHTS.get(a, 0.5) >= 0.5
        ]
        if high_loss:
            W("**High-loss actions:**")
            W("")
            W("| Action | Count | Loss Weight |")
            W("|--------|-------|-------------|")
            for action, count, weight in high_loss[:5]:
                pct = count / max(total_actions, 1) * 100
                W(f"| `{action}` | {count:,} ({pct:.0f}%) | {weight} |")
            W("")
            W("**Lower-loss alternatives:**")
            W("- `redact` → `cryptohash` for IDs (loss: 1.0 → 0.1)")
            W("- `redact` → `generalize` for dates (loss: 1.0 → 0.5)")
            W("- `redact` → `substitute` for names (loss: 1.0 → 0.4)")

    # Reference integrity
    if ri_avg is not None and ri_avg < 0.95:
        W("---")
        W("")
        W("## Dangling References")
        W("")
        W(
            f"**{_pct(1 - ri_avg)} of references** are invalid after ID pseudonymization."
        )
        W("")
        W("**Fix:** Enable reference rewriting in your config:")
        W("```yaml")
        W("rewrite_references: true")
        W("```")

    # Missed rules (only if actually an issue)
    if missed_rules_by_type:
        never_fired = set()
        for rules in missed_rules_by_type.values():
            never_fired.update(rules)
        truly_missed = [r for r in never_fired if fired_rules.get(r, 0) == 0]

        if truly_missed:
            W("---")
            W("")
            W("## Inactive Rules")
            W("")
            W(
                f"**{len(truly_missed)} rule(s)** are defined in config but never fired on any resource:"
            )
            W("")
            W("| Rule | Expected on | Total Fired |")
            W("|------|-------------|-------------|")
            for rule in truly_missed[:10]:
                types_that_missed = sorted(
                    rt for rt, rules in missed_rules_by_type.items() if rule in rules
                )
                types_str = ", ".join(f"`{t}`" for t in types_that_missed[:3])
                if len(types_that_missed) > 3:
                    types_str += f" +{len(types_that_missed) - 3} more"
                W(f"| `{rule}` | {types_str} | 0 |")
            W("")
            W(
                "Check FHIRPath expressions: if the resource types listed are in your data but the rule never fires, "
                "the path may not match your FHIR profile. Remove the rule if it is no longer needed."
            )

    # -----------------------------------------------------------------------
    # Action Summary (always useful)
    # -----------------------------------------------------------------------
    if action_totals:
        W("---")
        W("")
        W("## Action Summary")
        W("")
        total_actions = sum(action_totals.values())
        W("| Action | Count | % | Info Loss |")
        W("|--------|-------|---|-----------|")
        for action, count in action_totals.most_common(8):
            pct = count / max(total_actions, 1) * 100
            weight = INFO_LOSS_WEIGHTS.get(action, 0.5)
            W(f"| `{action}` | {count:,} | {pct:.0f}% | {weight} |")
        W("")

    # -----------------------------------------------------------------------
    # Resource Type Breakdown — all types, highlight problems
    # -----------------------------------------------------------------------
    if by_type:
        W("---")
        W("")
        W("## Resource Type Breakdown")
        W("")
        W(
            "| Type | Grade | Total | Pass | Failed | Composite | Privacy | Utility | Quality | Notes |"
        )
        W(
            "|------|-------|-------|------|--------|-----------|---------|---------|---------|-------|"
        )
        for rtype, ts in sorted(by_type.items()):  # alphabetic A-Z
            avg_comp = ts.composite_sum / ts.total if ts.total else 0.0
            avg_priv = ts.privacy_score_sum / ts.total if ts.total else 0.0
            avg_u = ts.utility_sum / ts.pass_count if ts.pass_count else 0.0
            avg_q = ts.quality_sum / ts.pass_count if ts.pass_count else 0.0
            grade = _letter_grade(avg_comp)
            note = ""
            if ts.fail_count > 0 and ts.privacy_fail_reasons:
                reason = ts.privacy_fail_reasons.most_common(1)[0][0]
                note = _brief_reason(reason, ts, rtype)
            elif ts.pass_count > 0 and avg_priv < 0.6:
                note = f"privacy score low ({avg_priv * 100:.0f}%) — residual risk near threshold"
            elif ts.pass_count > 0 and avg_u < 0.70:
                note = f"low utility ({avg_u * 100:.0f}%)"
            elif ts.pass_count > 0 and avg_q < 0.70:
                note = f"low quality ({avg_q * 100:.0f}%)"
            elif ts.total > 0 and avg_comp < 80:
                note = f"composite {avg_comp:.0f}%"
            W(
                f"| `{rtype}` | {grade} | {ts.total:,} | {ts.pass_count:,} | {ts.fail_count:,} |"
                f" {avg_comp:.0f}% | {avg_priv * 100:.0f}% | {_pct(avg_u)} | {_pct(avg_q)} | {note} |"
            )
        W("")

    # -----------------------------------------------------------------------
    # Score Composition — show the exact arithmetic for every type that did
    # not earn an A grade. This is the "WHY did this type score lower" view.
    # -----------------------------------------------------------------------
    weak_types = [
        (rtype, ts)
        for rtype, ts in sorted(by_type.items())
        if ts.total > 0 and (ts.composite_sum / ts.total) < 90
    ]
    if weak_types:
        W("---")
        W("")
        W("## Score Composition — Why Some Types Scored Lower")
        W("")
        W(
            "For every resource type that did not earn an A, this section shows the exact"
        )
        W("arithmetic behind the composite score plus the weakest sub-dimension(s).")
        W("")
        for rtype, ts in weak_types:
            avg_comp = ts.composite_sum / ts.total if ts.total else 0.0
            avg_priv = ts.privacy_score_sum / ts.total if ts.total else 0.0
            avg_u = ts.utility_sum / ts.pass_count if ts.pass_count else 0.0
            avg_q = ts.quality_sum / ts.pass_count if ts.pass_count else 0.0
            grade = _letter_grade(avg_comp)
            avg_risk = (
                sum(ts.risk_scores) / len(ts.risk_scores) if ts.risk_scores else 0.0
            )

            W(f"### `{rtype}` — Grade {grade} ({avg_comp:.1f}%)")
            W("")
            W("**Composite arithmetic (multiplicative):**")
            W("")
            W("```")
            W(
                f"composite = privacy × utility × quality"
                f"  =  {avg_priv:.3f} × {avg_u:.3f} × {avg_q:.3f}"
                f"  =  {avg_priv * avg_u * avg_q:.3f}  ({avg_priv * avg_u * avg_q * 100:.1f}%)"
            )
            W(
                f"privacy   = 1 − (avg_risk / threshold)"
                f"   =  1 − ({avg_risk:.3f} / {RISK_THRESHOLD})"
                f"   =  {avg_priv:.3f}"
            )
            W("```")
            W("")

            # Sub-dimension breakdown — show the weighted contribution of each
            # sub-dim to its module score. This pinpoints WHICH sub-dim dragged
            # the overall score down.
            util_dims = [(d, w) for d, m, w in _DIM_META if m == "utility"]
            qual_dims = [(d, w) for d, m, w in _DIM_META if m == "quality"]

            def _dim_block(label: str, dims: list[tuple[str, float]]) -> None:
                W(f"**{label} — sub-dimension contributions:**")
                W("")
                W("| Sub-dimension | Weight | Avg Value | Weighted | Verdict |")
                W("|---|---|---|---|---|")
                for dim, w in dims:
                    avg = _avg(ts.dim_samples.get(dim, []))
                    if avg is None:
                        continue
                    weighted = avg * w
                    if avg >= 0.95:
                        verdict = "strong"
                    elif avg >= 0.80:
                        verdict = "ok"
                    elif avg >= 0.60:
                        verdict = "moderate"
                    else:
                        verdict = "weak (drives score down)"
                    W(
                        f"| {dim.replace('_', ' ')} | {w:.2f} | {avg * 100:.1f}% |"
                        f" {weighted * 100:.1f}% | {verdict} |"
                    )
                W("")

            _dim_block("Utility", util_dims)
            _dim_block("Quality", qual_dims)

            # Top actions applied to this type — explains the information_loss
            # sub-dim concretely (which actions did the work).
            if ts.action_counts:
                W("**Actions applied to this resource type (top 5):**")
                W("")
                W("| Action | Count | Loss Weight | Contribution |")
                W("|---|---|---|---|")
                total_acts_for_type = sum(ts.action_counts.values())
                for action, count in ts.action_counts.most_common(5):
                    weight = INFO_LOSS_WEIGHTS.get(action, 0.5)
                    pct = count / max(total_acts_for_type, 1) * 100
                    W(
                        f"| `{action}` | {count:,} | {weight} |"
                        f" {pct:.0f}% of this type's actions |"
                    )
                W("")

            # Worst N resources — concrete IDs the operator can investigate.
            if ts.worst_resources:
                W("**Lowest-scoring resources of this type (drill down candidates):**")
                W("")
                W("| Resource ID | Composite |")
                W("|---|---|")
                for comp, rid in ts.worst_resources:
                    W(f"| `{rid}` | {comp:.1f}% |")
                W("")

    # -----------------------------------------------------------------------
    # Scoring Reference Appendix — definitions for every action weight,
    # sub-dimension formula, and threshold used above.
    # -----------------------------------------------------------------------
    W("---")
    W("")
    W("## Scoring Reference Appendix")
    W("")
    W("### Action information-loss weights")
    W("")
    W(
        "Each transformation has a fixed information-loss weight (0 = no loss, 1 = total loss)."
    )
    W(
        "The `information_loss` sub-dimension averages these weights across all actions in a resource."
    )
    W("")
    W("| Action | Loss Weight | Effect |")
    W("|---|---|---|")
    _action_effect_hint = {
        "redact": "field removed entirely",
        "scrub_text": "free-text replaced with `[REDACTED]`",
        "nlp_scrub": "NLP-detected entities removed",
        "nlp_detect": "legacy alias for nlp_scrub",
        "nlp_detect_act": "conditional NLP — entity-specific action",
        "nlp_detect_act/clean": "no action (entity passes through)",
        "nlp_detect_act/redact": "matched entity redacted",
        "nlp_detect_act/generalize": "matched entity generalized",
        "nlp_detect_act/tokenize": "matched entity tokenized",
        "generalize": "value coarsened (e.g. date → year)",
        "substitute": "fixed replacement (e.g. `[REDACTED]`)",
        "perturb": "small numeric noise added",
        "cryptohash": "deterministic HMAC — preserves linkage",
        "gpas_pseudonymize": "external TTP pseudonymization",
        "encrypt": "reversible RSA encryption",
    }
    for action in sorted(INFO_LOSS_WEIGHTS, key=lambda a: INFO_LOSS_WEIGHTS[a]):
        W(
            f"| `{action}` | {INFO_LOSS_WEIGHTS[action]} |"
            f" {_action_effect_hint.get(action, '')} |"
        )
    W("")
    W("### Sub-dimension formulas")
    W("")
    W(
        "- **field_retention** — `1 − (fields_removed / fields_in_input)`. Penalises hard `redact`."
    )
    W(
        "- **semantic_preservation** — share of clinical codes (SNOMED/LOINC/RxNorm/ICD) preserved verbatim. `1.0` when codes are untouched."
    )
    W(
        "- **temporal_consistency** — share of date/period fields whose chronological ordering survived generalization."
    )
    W(
        "- **information_loss** — `1 − weighted_avg(action_loss_weights)` across all actions applied to the resource."
    )
    W("- **success_rate** — `1 − (errors / total_actions)` for this resource.")
    W(
        "- **rule_coverage** — share of declared rules that fired on at least one path in this resource."
    )
    W(
        "- **schema_validation** — `1.0` if the de-identified resource still conforms to FHIR R4 base schema, else `0.0`."
    )
    W(
        "- **reference_integrity** — share of `Reference` fields whose target ID was either rewritten or already valid."
    )
    W("")
    W("### Privacy sub-evaluators (max risk wins)")
    W("")
    W(
        "- **attacker_model** — per-resource quasi-identifier suppression score. Patient with all 3 QI fields (gender, birth_year, zip_prefix_3) suppressed → `risk = 0.0`. Each remaining QI raises risk."
    )
    W(
        "- **attacker_model_batch** — full k-anonymity computed across all Patients. `min_k < 5` triggers an issue."
    )
    W(
        "- **identifier_coverage** — every HIPAA-sensitive path defined in `HIPAA_SENSITIVE_PATHS` must have a matching transformation in the manifest. Missing paths add risk proportional to severity."
    )
    W(
        "- **text_risk** — regex (SSN/phone/email/IP/MRN/dates) + optional Presidio NER over all string fields ≥ 20 chars. Each detection adds `0.15` to risk."
    )
    W("")
    W(
        f"**Privacy threshold:** `risk_score ≤ {RISK_THRESHOLD}` (env: `MEDANON_SCORE_RISK_THRESHOLD`)."
    )
    W("")

    W("---")
    W(f"*{scored_at}*")

    return "\n".join(lines)


def _suggest_action(path: str) -> tuple[str, str]:
    """Suggest a transformation action for an uncovered path."""
    p = path.lower()
    if "id" in p or "identifier" in p:
        return "cryptohash", "preserves linkage"
    if "name" in p:
        return "redact", "names must be removed"
    if "birth" in p or "date" in p or "period" in p or "time" in p:
        return "generalize", "year-only reduces loss"
    if "address" in p or "zip" in p or "postal" in p:
        return "generalize", "partial suppression"
    if "telecom" in p or "phone" in p or "email" in p:
        return "redact", "contact info removed"
    if "performer" in p or "author" in p or "requester" in p or "participant" in p:
        return "redact", "practitioner reference"
    if "custodian" in p or "organization" in p:
        return "redact", "organization reference"
    return "redact", "sensitive field"


def _brief_reason(reason: str, ts: _TypeStats, rtype: str = "") -> str:
    """One-line explanation of failure reason."""
    if reason == "identifier_not_covered":
        paths = list(ts.uncovered_hipaa.keys())[:2]
        if rtype:
            qualified = [
                f"{rtype}.{p}" if not p.startswith(rtype + ".") else p for p in paths
            ]
        else:
            qualified = paths
        return f"missing rules: {', '.join(f'`{p}`' for p in qualified)}"
    if reason == "text_risk":
        return "PII detected in text fields"
    if reason == "attacker_model":
        return "k-anonymity failure — patient re-identifiable"
    return reason.replace("_", " ")
