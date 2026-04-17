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


class _TypeStats:
    """Per-resource-type accumulator."""

    __slots__ = (
        "total",
        "pass_count",
        "fail_count",
        "composite_sum",
        "utility_sum",
        "quality_sum",
        "privacy_fail_reasons",
        "uncovered_hipaa",
        "text_detections",
    )

    def __init__(self) -> None:
        self.total: int = 0
        self.pass_count: int = 0
        self.fail_count: int = 0
        self.composite_sum: float = 0.0
        self.utility_sum: float = 0.0
        self.quality_sum: float = 0.0
        self.privacy_fail_reasons: collections.Counter = collections.Counter()
        self.uncovered_hipaa: collections.Counter = collections.Counter()
        self.text_detections: list[dict] = []


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
        self._missed_rules_by_type: dict[str, set[str]] = {}  # resource_type -> missed rule names
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

        for entry in manifest_entries:
            action = entry.get("action", "unknown")
            self._action_totals[action] += 1
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
    sv_avg = _avg(dim_samples.get("schema_validation", []))

    # Collect all issues for summary
    issues: list[tuple[str, str]] = []  # (severity, message)

    # Uncovered HIPAA paths: severity depends on whether those resources
    # actually failed the privacy gate (fail_count > 0) or the risk was
    # below the threshold on every resource (gate passed, sub-threshold risk).
    if uncovered_hipaa:
        if fail_count > 0:
            issues.append(("CRITICAL", f"{len(uncovered_hipaa)} HIPAA paths not covered by rules (caused failures)"))
        else:
            issues.append(("LOW", f"{len(uncovered_hipaa)} HIPAA paths partially uncovered (below risk threshold — all resources passed)"))
    if text_pattern_counts:
        issues.append(("CRITICAL", f"PII detected in {sum(text_pattern_counts.values())} text fields"))
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
        truly_missed_count = [r for r in never_fired_for_count if fired_rules.get(r, 0) == 0]
        if truly_missed_count:
            issues.append(("LOW", f"{len(truly_missed_count)} rules never fired"))
    if error_count > 0:
        issues.append(("MEDIUM", f"{error_count} processing errors"))

    # -----------------------------------------------------------------------
    # Executive Summary (always show)
    # -----------------------------------------------------------------------
    W("# Scoring Report")
    W("")
    W(f"**Job:** `{job_id or '(ad-hoc)'}`  |  **Profile:** `{config_profile}`  |  **Resources:** {total:,}")
    W("")

    # Overall verdict
    if fail_count == 0 and not issues:
        W("## ✓ PASS — No Issues")
        W("")
        W(f"All {total:,} resources passed privacy checks. Composite score: **{avg_composite:.1f}%**")
    elif fail_count == 0:
        W(f"## ⚠ PASS with Warnings — {len(issues)} Issue(s)")
        W("")
        W(f"All resources passed privacy gate, but there are optimization opportunities.")
    else:
        W(f"## ✗ FAIL — {fail_count:,} Resources Failed")
        W("")
        pct_fail = fail_count / max(total, 1) * 100
        W(f"**{pct_fail:.0f}%** of resources failed the privacy gate (composite = 0).")
    W("")

    # Quick stats
    W("| Metric | Value |")
    W("|--------|-------|")
    W(f"| Composite | **{avg_composite:.1f}%** |")
    W(f"| Utility | {_pct(avg_utility)} |")
    W(f"| Quality | {_pct(avg_quality)} |")
    W(f"| Pass / Fail | {pass_count:,} / {fail_count:,} |")
    W("")

    # Sub-dimension averages — all 8 dimensions with their module and weight
    _DIM_META: list[tuple[str, str, float]] = [
        ("field_retention",       "utility",  0.25),
        ("semantic_preservation", "utility",  0.30),
        ("temporal_consistency",  "utility",  0.15),
        ("information_loss",      "utility",  0.30),
        ("success_rate",          "quality",  0.40),
        ("rule_coverage",         "quality",  0.30),
        ("schema_validation",     "quality",  0.15),
        ("reference_integrity",   "quality",  0.15),
    ]
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
            icon = "🔴" if severity == "CRITICAL" else "🟠" if severity == "HIGH" else "🟡" if severity == "MEDIUM" else "🔵"
            W(f"- {icon} **{severity}:** {msg}")
        W("")

    # -----------------------------------------------------------------------
    # Problems Only — skip sections with no issues
    # -----------------------------------------------------------------------

    # Uncovered HIPAA paths
    if uncovered_hipaa:
        W("---")
        W("")
        if fail_count > 0:
            W("## 🔴 Uncovered HIPAA Paths")
            W("")
            W(f"**{len(uncovered_hipaa)} sensitive paths** have no transformation rule "
              f"and caused {fail_count:,} resource(s) to fail the privacy gate.")
        else:
            W("## 🔵 Partially Uncovered HIPAA Paths (below risk threshold)")
            W("")
            W(f"**{len(uncovered_hipaa)} sensitive path(s)** had no manifest entry on some resources, "
              f"but the per-resource risk score stayed below the threshold on all {total:,} resources — "
              f"**no failures**. This often means the field is covered by a conditional rule "
              f"(`nlp_detect_act`) that did not fire because no PII was detected.")
        W("")
        W("| Path | Occurrences |")
        W("|------|-------------|")
        for path, count in uncovered_hipaa.most_common(10):
            W(f"| `{path}` | {count:,} |")
        W("")
        W("**Fix — add explicit rules if the conditional coverage is insufficient:**")
        W("```yaml")
        for path, _ in uncovered_hipaa.most_common(5):
            action, note = _suggest_action(path)
            parts = path.split(".")
            name = f"{action} {parts[0].lower()} {parts[-1]}"
            W(f"- name: {name}")
            W(f'  match: "{path}"')
            W(f"  action: {action}  # {note}")
            W("")
        W("```")

    # CRITICAL: Text PII
    if text_pattern_counts:
        W("---")
        W("")
        W("## 🔴 PII Detected in Text Fields")
        W("")
        W(f"**{sum(text_pattern_counts.values())} matches** across {len(text_pattern_counts)} pattern types:")
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
                    W("## 🟠 k-Anonymity Risk")
                    W("")
                    W(f"**min_k = {min_k}** — {singletons} patient(s) are uniquely identifiable.")
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
        W("## 🟠 High Information Loss")
        W("")
        W(f"**{_pct(1 - il_avg)} of analytical value lost** — utility may be too low for research.")
        W("")
        # Show action breakdown
        total_actions = sum(action_totals.values())
        high_loss = [(a, c, INFO_LOSS_WEIGHTS.get(a, 0.5))
                     for a, c in action_totals.most_common()
                     if INFO_LOSS_WEIGHTS.get(a, 0.5) >= 0.5]
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
        W("## 🟡 Dangling References")
        W("")
        W(f"**{_pct(1 - ri_avg)} of references** are invalid after ID pseudonymization.")
        W("")
        W("**Fix:** Enable reference rewriting in your config:")
        W("```yaml")
        W("rewrite_references: true")
        W("```")

    # Missed rules (only if actually an issue)
    if missed_rules_by_type:
        # Only show if there are rules that NEVER fired (not just missed on some types)
        never_fired = set()
        for rules in missed_rules_by_type.values():
            never_fired.update(rules)
        # Filter to rules that also never appear in fired_rules
        truly_missed = [r for r in never_fired if fired_rules.get(r, 0) == 0]

        if truly_missed:
            W("---")
            W("")
            W("## 🔵 Inactive Rules")
            W("")
            W(f"**{len(truly_missed)} rules** defined in config but never fired:")
            W("")
            for rule in truly_missed[:10]:
                W(f"- `{rule}`")
            W("")
            W("Check if the FHIRPath expressions match your data or remove unused rules.")

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
        W("| Type | Total | Pass | Failed | Avg Composite | Avg Utility | Avg Quality | Notes |")
        W("|------|-------|------|--------|---------------|-------------|-------------|-------|")
        for rtype, ts in sorted(by_type.items(), key=lambda x: -x[1].total):
            avg_comp = ts.composite_sum / ts.total if ts.total else 0.0
            avg_u = ts.utility_sum / ts.pass_count if ts.pass_count else 0.0
            avg_q = ts.quality_sum / ts.pass_count if ts.pass_count else 0.0
            note = ""
            if ts.fail_count > 0 and ts.privacy_fail_reasons:
                reason = ts.privacy_fail_reasons.most_common(1)[0][0]
                note = _brief_reason(reason, ts)
            elif ts.total > 0 and avg_comp < 80:
                note = "low composite"
            W(
                f"| `{rtype}` | {ts.total:,} | {ts.pass_count:,} | {ts.fail_count:,} |"
                f" {avg_comp:.0f}% | {_pct(avg_u)} | {_pct(avg_q)} | {note} |"
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
    if "birth" in p or "date" in p:
        return "generalize", "year-only reduces loss"
    if "address" in p or "zip" in p:
        return "generalize", "partial suppression"
    if "telecom" in p or "phone" in p or "email" in p:
        return "redact", "contact info removed"
    return "redact", "sensitive field"


def _brief_reason(reason: str, ts: _TypeStats) -> str:
    """One-line explanation of failure reason."""
    if reason == "identifier_not_covered":
        paths = list(ts.uncovered_hipaa.keys())[:2]
        return f"uncovered: {', '.join(paths)}"
    if reason == "text_risk":
        return "PII in text"
    return reason.replace("_", " ")
