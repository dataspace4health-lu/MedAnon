"""Scoring audit — rich post-export Markdown report generator.

``ScoreAuditCollector`` wraps ``ScoreCollector`` and accumulates per-resource
evidence during scoring.  After processing all resources, call
``generate_report()`` to produce a human-readable Markdown file that:

- Explains why each score is what it is
- Identifies specific HIPAA paths that are uncovered
- Lists which text-risk patterns were detected and where
- Shows action distribution and information-loss breakdown
- Renders per-resource-type PASS/FAIL table
- Generates priority-ordered, actionable YAML config snippets

Usage::

    collector = ScoreAuditCollector(config_profile="auto", job_id="abc")
    for resource in resources:
        manifest = extract_manifests(resource)
        collector.record_resource(None, resource, manifest, settings)
    report_md = collector.generate_report(settings=settings)
"""

from __future__ import annotations

import collections
import datetime
from typing import Any

from pipeline.scoring.engine import ScoreCollector
from pipeline.scoring.models import ScoreResult
from pipeline.scoring.constants import INFO_LOSS_WEIGHTS, HIPAA_SENSITIVE_PATHS, RISK_THRESHOLD

# Cap per-failure example lists to bound memory for very large exports.
_MAX_EXAMPLES = 15
_MAX_DIMENSION_SAMPLES = 5000  # cap sum-list length per sub-dimension


class _TypeStats:
    """Per-resource-type accumulator."""
    __slots__ = (
        "total", "pass_count", "fail_count",
        "composite_sum", "utility_sum", "quality_sum",
        "privacy_fail_reasons", "uncovered_hipaa", "text_detections",
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
    """Drop-in replacement for ``ScoreCollector`` that also builds audit data.

    Delegates all scoring to the inner ``ScoreCollector`` so existing aggregate
    logic is unchanged.  The extra state it accumulates is used only when
    generating the Markdown report.
    """

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

        # Per-resource-type breakdown
        self._by_type: dict[str, _TypeStats] = {}

        # Action distribution across all manifest entries
        self._action_totals: collections.Counter = collections.Counter()

        # Fired and missed rule names (from manifest + quality evidence)
        self._fired_rules: collections.Counter = collections.Counter()
        self._missed_rules: collections.Counter = collections.Counter()

        # Sub-dimension value samples (capped to _MAX_DIMENSION_SAMPLES)
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

        # Global uncovered HIPAA path counter
        self._uncovered_hipaa: collections.Counter = collections.Counter()

        # Text risk pattern counter
        self._text_pattern_counts: collections.Counter = collections.Counter()
        self._text_detection_examples: list[dict] = []

    # ------------------------------------------------------------------
    # Delegation
    # ------------------------------------------------------------------

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

        # -- Harvest utility evidence -------------------------------------------
        if result.utility:
            ts.utility_sum += result.utility.score
            for ev in result.utility.evidence:
                samples = self._dim_samples.get(ev.check)
                if samples is not None and len(samples) < _MAX_DIMENSION_SAMPLES:
                    samples.append(ev.value)

        # -- Harvest quality evidence -------------------------------------------
        if result.quality:
            ts.quality_sum += result.quality.score
            for ev in result.quality.evidence:
                samples = self._dim_samples.get(ev.check)
                if samples is not None and len(samples) < _MAX_DIMENSION_SAMPLES:
                    samples.append(ev.value)
                if ev.check == "rule_coverage":
                    for missed in ev.details.get("missed", []):
                        self._missed_rules[missed] += 1

        # -- Harvest privacy evidence --------------------------------------------
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
                        self._text_detection_examples.append({
                            "resource_type": rtype,
                            **det,
                        })
                ts.text_detections.extend(
                    ev.details.get("detections", [])[:_MAX_EXAMPLES - len(ts.text_detections)]
                )
                ts.privacy_fail_reasons["text_risk"] += 1

            elif ev.severity == "critical" and result.decision == "FAIL":
                ts.privacy_fail_reasons[ev.check] += 1

        # -- Harvest action distribution from manifest entries ------------------
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
        """Return same aggregate dict as ScoreCollector."""
        return self._inner.aggregate()

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------

    def generate_report(
        self,
        settings: Any = None,
        export_meta: dict | None = None,
    ) -> str:
        """Render and return the full Markdown audit report string."""
        summary = self._inner.aggregate()
        meta = export_meta or {}
        return _render_report(
            summary=summary,
            by_type=self._by_type,
            action_totals=self._action_totals,
            fired_rules=self._fired_rules,
            missed_rules=self._missed_rules,
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
    if v is None:
        return "n/a"
    return f"{v * 100:.1f}%"


def _status_icon(score: float | None, fail_if_below: float = 0.7) -> str:
    if score is None:
        return "—"
    if score >= 0.85:
        return "✓"
    if score >= fail_if_below:
        return "⚠"
    return "✗"


def _render_report(
    summary: dict,
    by_type: dict[str, _TypeStats],
    action_totals: collections.Counter,
    fired_rules: collections.Counter,
    missed_rules: collections.Counter,
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

    # -----------------------------------------------------------------------
    # Header
    # -----------------------------------------------------------------------
    W("# De-identification Score Audit Report")
    W("")
    W(f"**Job ID:** `{job_id or '(ad-hoc)'}`  ")
    W(f"**Scored at:** {scored_at}  ")
    W(f"**Config profile:** `{config_profile}`  ")
    if export_meta.get("fhir_source"):
        W(f"**FHIR source:** `{export_meta['fhir_source']}`  ")
    W("")

    # -----------------------------------------------------------------------
    # Overall summary table
    # -----------------------------------------------------------------------
    W("## Overall Scores")
    W("")
    comp_icon = _status_icon(avg_composite / 100 if avg_composite else 0)
    util_icon = _status_icon(avg_utility)
    qual_icon = _status_icon(avg_quality)
    W("| Dimension | Score | Status |")
    W("|-----------|-------|--------|")
    W(f"| **Composite** | {avg_composite:.1f}% | {comp_icon} |")
    W(f"| **Utility**   | {_pct(avg_utility)} | {util_icon} |")
    W(f"| **Quality**   | {_pct(avg_quality)} | {qual_icon} |")
    W("")
    W(f"**Resources scored:** {total}  ")
    W(f"**PASS:** {pass_count} ✓  **FAIL:** {fail_count} ✗  **Errors:** {error_count}  ")
    W("")
    if fail_count > 0:
        pct_fail = fail_count / max(total, 1) * 100
        W(f"> ⚠ **{pct_fail:.0f}% of resources failed the privacy gate.** "
          f"See [Privacy Analysis](#privacy-analysis) below.")
        W("")

    # -----------------------------------------------------------------------
    # How composite is calculated
    # -----------------------------------------------------------------------
    W("### How composite is calculated")
    W("")
    W("```")
    W("composite = privacy_norm × utility × quality × 100")
    W("")
    W("  privacy_norm  = 1.0 − (privacy_risk / threshold)   (0.0 if FAIL)")
    W("  utility       = 0.25 × field_retention")
    W("                + 0.30 × semantic_preservation")
    W("                + 0.15 × temporal_consistency")
    W("                + 0.30 × information_loss_preservation")
    W("  quality       = 0.40 × success_rate")
    W("                + 0.30 × rule_coverage")
    W("                + 0.15 × schema_validation")
    W("                + 0.15 × reference_integrity")
    W("```")
    W("")

    # -----------------------------------------------------------------------
    # Privacy Analysis
    # -----------------------------------------------------------------------
    W("---")
    W("")
    W("## Privacy Analysis (Hard Gate — PASS/FAIL)")
    W("")
    W(f"*Risk threshold: {RISK_THRESHOLD}  "
      f"Resources above this threshold score composite = 0.*")
    W("")

    # k-anonymity (batch)
    if batch_privacy:
        r_score = batch_privacy.get("risk_score", 0.0)
        r_passed = batch_privacy.get("passed", True)
        attacker = batch_privacy.get("attacker_risk", 0.0)
        identifier = batch_privacy.get("identifier_risk", 0.0)
        text_r = batch_privacy.get("text_risk", 0.0)

        W("### Batch Privacy (k-Anonymity across all Patients)")
        W("")
        W(f"| Sub-score | Value | Threshold |")
        W(f"|-----------|-------|-----------|")
        W(f"| Attacker model risk    | {attacker:.4f} | {RISK_THRESHOLD} |")
        W(f"| Identifier coverage risk | {identifier:.4f} | {RISK_THRESHOLD} |")
        W(f"| Text risk              | {text_r:.4f} | {RISK_THRESHOLD} |")
        W(f"| **Overall risk**       | **{r_score:.4f}** | **{RISK_THRESHOLD}** |")
        W(f"| **Result**             | {'**PASS** ✓' if r_passed else '**FAIL** ✗'} | — |")
        W("")

        # k-anonymity detail from evidence
        for ev in batch_privacy.get("evidence", []):
            check = ev.get("check", "")
            details = ev.get("details", {})
            if check == "attacker_model_batch" and details:
                W("#### k-Anonymity Detail")
                W("")
                min_k = details.get("min_k", "?")
                singletons = details.get("singleton_groups", 0)
                risk_level = details.get("risk_level", "?")
                W(f"- **min_k:** {min_k} — smallest group of patients with identical quasi-identifiers")
                W(f"- **Risk level:** {risk_level}")
                W(f"- **Singleton groups:** {singletons} "
                  f"(patients uniquely identifiable from quasi-identifiers)")
                W(f"- **Prosecutor risk:** {details.get('prosecutor_risk', 0):.4f}")
                W(f"- **Journalist risk:** {details.get('journalist_risk', 0):.4f}")
                W(f"- **Marketer risk:** {details.get('marketer_risk', 0):.4f}")
                W("")
                if min_k is not None and isinstance(min_k, int) and min_k < 5:
                    W("> ⚠ **k < 5:** Patients in small groups are vulnerable to linkage attacks.")
                    W("> Quasi-identifiers are: `gender`, `birth_year`, `zip_prefix`.")
                    W("")
                    W("**To raise k-anonymity:**")
                    W("```yaml")
                    W("# Option 1 — Generalize birth year to decade")
                    W("- name: generalize birth decade")
                    W("  match: Patient.birthDate")
                    W("  action: generalize")
                    W("  params: { level: decade }")
                    W("")
                    W("# Option 2 — Suppress zip prefix (use config_hipaa_safe_harbor)")
                    W("- name: generalize zip")
                    W("  match: Patient.address")
                    W("  action: generalize")
                    W("  params: { keep_zip_prefix: 3 }")
                    W("```")
                    W("")
    else:
        W("*No Patient resources in export — k-anonymity not evaluated.*")
        W("")

    # HIPAA identifier coverage gaps
    W("### HIPAA Identifier Coverage")
    W("")
    if uncovered_hipaa:
        W(f"**{sum(uncovered_hipaa.values())} instances** where sensitive fields were "
          f"present in a resource but not covered by any transformation rule:")
        W("")
        W("| FHIR Path | Resources Affected |")
        W("|-----------|-------------------|")
        for path, count in uncovered_hipaa.most_common(20):
            W(f"| `{path}` | {count} |")
        W("")
        W("**To fix — add rules for each uncovered path:**")
        W("```yaml")
        for path, _ in uncovered_hipaa.most_common(10):
            # Derive a sensible action from the path
            action, action_note = _suggest_action(path)
            parts = path.split(".")
            rtype = parts[0] if len(parts) > 1 else "*"
            field = parts[1] if len(parts) > 1 else parts[0]
            W(f"- name: \"{action} {rtype.lower()} {field}\"")
            W(f"  match: \"{path}\"  # {action_note}")
            W(f"  action: \"{action}\"")
            W("")
        W("```")
    else:
        W("✓ All sensitive HIPAA fields present in resources were covered by "
          "at least one transformation rule.")
    W("")

    # Text risk detections
    W("### Text Risk Detections")
    W("")
    if text_pattern_counts:
        W(f"**{sum(text_pattern_counts.values())} PII pattern matches** found in "
          f"narrative or text fields across {len(set(e.get('resource_type') for e in text_detection_examples))} "
          f"resource type(s):")
        W("")
        W("| Pattern | Occurrences | Example |")
        W("|---------|-------------|---------|")
        shown_examples: dict[str, str] = {}
        for ex in text_detection_examples:
            ptype = ex.get("type", "?")
            if ptype not in shown_examples:
                shown_examples[ptype] = ex.get("value_preview", "—")
        for pattern, count in text_pattern_counts.most_common():
            example_val = shown_examples.get(pattern, "—")
            W(f"| `{pattern}` | {count} | `{example_val}` |")
        W("")
        W("**To fix — scrub narrative text:**")
        W("```yaml")
        W("- name: scrub narrative text")
        W("  match: \"*.text.div\"")
        W("  action: scrub_text")
        W("")
        W("- name: scrub note text")
        W("  match: \"*.note.text\"")
        W("  action: scrub_text")
        W("```")
    else:
        W("✓ No PII patterns detected in narrative/text fields.")
    W("")

    # -----------------------------------------------------------------------
    # Utility Analysis
    # -----------------------------------------------------------------------
    W("---")
    W("")
    W("## Utility Analysis")
    W("")
    W(f"**Average utility score: {_pct(avg_utility)}**  "
      f"*(higher = more analytical value is preserved)*")
    W("")

    fr = _avg(dim_samples.get("field_retention", []))
    sp = _avg(dim_samples.get("semantic_preservation", []))
    tc = _avg(dim_samples.get("temporal_consistency", []))
    il = _avg(dim_samples.get("information_loss", []))

    W("### Sub-dimension Breakdown (weights)")
    W("")
    W("| Sub-dimension | Score | Weight | Interpretation |")
    W("|---------------|-------|--------|----------------|")
    W(f"| Field retention      | {_pct(fr)} | 25% | {_interpret_fr(fr)} |")
    W(f"| Semantic preservation | {_pct(sp)} | 30% | {_interpret_sp(sp)} |")
    W(f"| Temporal consistency | {_pct(tc)} | 15% | {_interpret_tc(tc)} |")
    W(f"| Information loss preservation | {_pct(il)} | 30% | {_interpret_il(il)} |")
    W("")

    # Information loss = action distribution
    if action_totals:
        total_actions = sum(action_totals.values())
        W("### Action Distribution (information loss drivers)")
        W("")
        W("Actions with higher loss weights (>0.4) reduce utility most. "
          "Consider substituting destructive actions with lower-loss equivalents where safe.")
        W("")
        W("| Action | Count | Info Loss Weight | Contribution to Loss |")
        W("|--------|-------|-----------------|----------------------|")
        for action, count in action_totals.most_common():
            weight = INFO_LOSS_WEIGHTS.get(action, 0.5)
            contribution = (count / max(total_actions, 1)) * weight
            flag = " ← high loss" if weight >= 0.6 else ""
            W(f"| `{action}` | {count} | {weight} | {contribution:.3f}{flag} |")
        W("")

        # Suggest lower-loss alternatives if redact is dominant
        top_actions = [a for a, _ in action_totals.most_common(3)]
        if "redact" in top_actions:
            W("**To improve utility — replace high-loss actions where safe:**")
            W("")
            W("| Instead of | Use | Loss | Use when |")
            W("|------------|-----|------|----------|")
            W("| `redact` on patient IDs | `cryptohash` | 0.1 | Longitudinal linkage needed |")
            W("| `redact` on birth date | `generalize` | 0.5 | Age-band analysis sufficient |")
            W("| `redact` on birth date | `perturb` | 0.3 | Date shift preserves relative timing |")
            W("| `redact` on zip/city | `generalize` | 0.5 | Regional analysis needed |")
            W("| `redact` on practitioner name | `substitute` | 0.4 | Structure must be preserved |")
            W("")

    # -----------------------------------------------------------------------
    # Quality Analysis
    # -----------------------------------------------------------------------
    W("---")
    W("")
    W("## Quality Analysis")
    W("")
    W(f"**Average quality score: {_pct(avg_quality)}**  "
      f"*(measures pipeline correctness, rule coverage, schema validity)*")
    W("")

    sr = _avg(dim_samples.get("success_rate", []))
    rc = _avg(dim_samples.get("rule_coverage", []))
    sv = _avg(dim_samples.get("schema_validation", []))
    ri = _avg(dim_samples.get("reference_integrity", []))

    W("### Sub-dimension Breakdown (weights)")
    W("")
    W("| Sub-dimension | Score | Weight | Interpretation |")
    W("|---------------|-------|--------|----------------|")
    W(f"| Success rate       | {_pct(sr)} | 40% | {_interpret_sr(sr, error_count, total)} |")
    W(f"| Rule coverage      | {_pct(rc)} | 30% | {_interpret_rc(rc, missed_rules)} |")
    W(f"| Schema validation  | {_pct(sv)} | 15% | {_interpret_sv(sv)} |")
    W(f"| Reference integrity| {_pct(ri)} | 15% | {_interpret_ri(ri)} |")
    W("")

    # Missed rules
    if missed_rules:
        W("### Missed Rules (fired 0 times on at least one applicable resource)")
        W("")
        W("These rules were defined in the config but never fired for at least "
          "one resource type they should apply to.  This could mean the target "
          "field was absent, or the FHIRPath expression needs adjustment.")
        W("")
        W("| Rule name | Resources where missed |")
        W("|-----------|----------------------|")
        for rule, count in missed_rules.most_common(20):
            W(f"| `{rule}` | {count} |")
        W("")

    # Fired rules
    if fired_rules:
        W("### Rules That Fired (from manifest entries)")
        W("")
        W("| Rule name | Total applications |")
        W("|-----------|-------------------|")
        for rule, count in fired_rules.most_common(20):
            W(f"| `{rule}` | {count} |")
        W("")

    if ri is not None and ri < 0.98:
        W("> ⚠ **Reference integrity below 98%.**  "
          "Some FHIR references may be dangling after de-identification.  "
          "Enable `rewrite_references: true` in your config profile to "
          "automatically rewrite references when IDs change.")
        W("")

    # -----------------------------------------------------------------------
    # Per-resource-type breakdown
    # -----------------------------------------------------------------------
    W("---")
    W("")
    W("## Per-Resource-Type Breakdown")
    W("")
    if by_type:
        W("| Resource Type | Total | PASS | FAIL | Avg Composite | Primary Failure |")
        W("|---------------|-------|------|------|---------------|-----------------|")
        for rtype, ts in sorted(by_type.items(), key=lambda x: -x[1].total):
            avg_comp = ts.composite_sum / ts.total if ts.total else 0.0
            if ts.fail_count > 0 and ts.privacy_fail_reasons:
                top_reason = ts.privacy_fail_reasons.most_common(1)[0][0]
                primary = _explain_failure_reason(top_reason, ts, rtype)
            elif ts.pass_count == ts.total:
                primary = "—"
            else:
                primary = "processing error"
            pass_icon = "✓" if ts.fail_count == 0 else "⚠" if ts.pass_count > 0 else "✗"
            W(f"| `{rtype}` | {ts.total} | {ts.pass_count} {pass_icon} | {ts.fail_count} | "
              f"{avg_comp:.1f}% | {primary} |")
        W("")
    W("")

    # -----------------------------------------------------------------------
    # Recommendations (priority ordered)
    # -----------------------------------------------------------------------
    W("---")
    W("")
    W("## Recommendations (Priority Order)")
    W("")
    recs = _build_recommendations(
        summary=summary,
        by_type=by_type,
        uncovered_hipaa=uncovered_hipaa,
        text_pattern_counts=text_pattern_counts,
        missed_rules=missed_rules,
        action_totals=action_totals,
        avg_utility=avg_utility,
        avg_quality=avg_quality,
        fail_count=fail_count,
        total=total,
        dim_samples=dim_samples,
        ri=ri,
        batch_privacy=batch_privacy,
    )
    for priority, title, body in recs:
        W(f"### [{priority}] {title}")
        W("")
        W(body)
        W("")

    if not recs:
        W("✓ No issues found. All scores within acceptable ranges.")
        W("")

    # -----------------------------------------------------------------------
    # Config profile reference
    # -----------------------------------------------------------------------
    W("---")
    W("")
    W("## Config Profile Reference")
    W("")
    W("| Action | Info Loss | Best for |")
    W("|--------|-----------|----------|")
    W("| `redact`           | 1.0 (high)    | PHI that must be fully removed |")
    W("| `scrub_text`       | 0.6           | Narrative text with embedded PII |")
    W("| `nlp_scrub`        | 0.6           | Unstructured text (NER-based) |")
    W("| `generalize`       | 0.5           | Dates, addresses, age → coarser values |")
    W("| `substitute`       | 0.4           | Names → realistic fake values |")
    W("| `perturb`          | 0.3           | Numeric values (add noise) |")
    W("| `cryptohash`       | 0.1 (low)     | IDs that need longitudinal linkage |")
    W("| `gpas_pseudonymize`| 0.1 (low)     | IDs with external pseudonym registry |")
    W("| `encrypt`          | 0.0 (none)    | Reversible encryption — data still usable |")
    W("")
    W("*Lower info loss = more utility preserved.*")
    W("")
    W("---")
    W(f"*Generated by medanon scoring engine — {scored_at}*")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interpretation helpers
# ---------------------------------------------------------------------------

def _interpret_fr(v: float | None) -> str:
    if v is None:
        return "indeterminate (no manifests)"
    if v >= 0.9:
        return "most fields retained"
    if v >= 0.7:
        return "moderate redaction"
    return "heavy redaction — consider generalize/cryptohash"


def _interpret_sp(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 0.9:
        return "clinical codes and references intact"
    if v >= 0.7:
        return "some codes/references may be degraded"
    return "many codes or references broken or missing"


def _interpret_tc(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 0.95:
        return "date ordering preserved"
    if v >= 0.7:
        return "some temporal inconsistencies"
    return "date perturbation broke period/ordering"


def _interpret_il(v: float | None) -> str:
    if v is None:
        return "indeterminate (no manifests)"
    if v >= 0.8:
        return "low information loss — utility well preserved"
    if v >= 0.5:
        return "moderate loss — mix of actions"
    return "high loss — redact-heavy config"


def _interpret_sr(v: float | None, error_count: int, total: int) -> str:
    if v is None or total == 0:
        return "n/a"
    if error_count == 0:
        return "all resources processed without errors ✓"
    pct = error_count / max(total, 1) * 100
    return f"{pct:.1f}% error rate — check logs for failed resources"


def _interpret_rc(v: float | None, missed: collections.Counter) -> str:
    if v is None:
        return "indeterminate (settings not loaded)"
    if len(missed) == 0:
        return "all applicable rules fired ✓"
    return f"{len(missed)} rule(s) never fired on applicable resources"


def _interpret_sv(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 0.95:
        return "schema valid ✓"
    return "some resources missing required fields (id, resourceType)"


def _interpret_ri(v: float | None) -> str:
    if v is None:
        return "n/a"
    if v >= 0.99:
        return "all references well-formed ✓"
    if v >= 0.90:
        return "minor dangling references"
    return "significant dangling refs — enable rewrite_references"


def _explain_failure_reason(reason: str, ts: _TypeStats, rtype: str) -> str:
    if reason == "identifier_not_covered":
        paths = ", ".join(f"`{p}`" for p, _ in ts.uncovered_hipaa.most_common(3))
        return f"uncovered paths: {paths}"
    if reason == "text_risk":
        return "PII found in text field"
    if reason == "no_transformations_detected":
        return "no manifest — privacy cannot be verified"
    return reason


def _suggest_action(path: str) -> tuple[str, str]:
    """Suggest a transformation action for an uncovered HIPAA path."""
    p = path.lower()
    if "id" in p:
        return "cryptohash", "IDs → pseudonymized, preserves linkage"
    if "name" in p:
        return "redact", "names must be fully removed"
    if "birth" in p or "date" in p:
        return "generalize", "dates → year-only reduces info loss"
    if "address" in p or "zip" in p:
        return "generalize", "partial address suppression"
    if "telecom" in p or "phone" in p or "email" in p:
        return "redact", "contact details must be removed"
    if "photo" in p:
        return "redact", "photos must be fully removed"
    return "redact", "sensitive field — remove entirely"


# ---------------------------------------------------------------------------
# Recommendations builder
# ---------------------------------------------------------------------------

def _build_recommendations(
    summary: dict,
    by_type: dict[str, _TypeStats],
    uncovered_hipaa: collections.Counter,
    text_pattern_counts: collections.Counter,
    missed_rules: collections.Counter,
    action_totals: collections.Counter,
    avg_utility: float,
    avg_quality: float,
    fail_count: int,
    total: int,
    dim_samples: dict[str, list[float]],
    ri: float | None,
    batch_privacy: dict,
) -> list[tuple[str, str, str]]:
    """Return list of (priority, title, body) tuples."""
    recs: list[tuple[str, str, str]] = []

    # ---- CRITICAL: uncovered HIPAA paths -----------------------------------
    if uncovered_hipaa:
        paths_yaml = "\n".join(
            f'- name: "{_suggest_action(p)[0]} {p.split(".")[0].lower()} {p.split(".")[-1]}"\n'
            f'  match: "{p}"\n  action: "{_suggest_action(p)[0]}"'
            for p, _ in uncovered_hipaa.most_common(6)
        )
        recs.append((
            "CRITICAL",
            f"Cover {len(uncovered_hipaa)} uncovered HIPAA-sensitive path(s)",
            f"The following paths contain PHI but are not transformed by any rule.\n"
            f"Resources with these paths will always FAIL the privacy gate (composite = 0).\n\n"
            f"**Add to your config:**\n```yaml\n{paths_yaml}\n```",
        ))

    # ---- CRITICAL: k-anonymity singletons -----------------------------------
    if batch_privacy:
        for ev in batch_privacy.get("evidence", []):
            if ev.get("check") == "attacker_model_batch":
                min_k = ev.get("details", {}).get("min_k", 99)
                singletons = ev.get("details", {}).get("singleton_groups", 0)
                if isinstance(min_k, int) and min_k < 3 and singletons > 0:
                    recs.append((
                        "CRITICAL",
                        f"k-anonymity too low (min_k={min_k}, {singletons} singleton group(s))",
                        "Some patients are uniquely re-identifiable from quasi-identifiers "
                        "(gender + birth_year + zip_prefix).\n\n"
                        "**Fix:** Generalize `Patient.birthDate` to year-only and/or apply "
                        "`config_hipaa_safe_harbor` profile which enforces 3-digit zip prefix.",
                    ))

    # ---- CRITICAL: text PII detections -------------------------------------
    if text_pattern_counts:
        patterns = ", ".join(f"`{p}`" for p in list(text_pattern_counts)[:5])
        recs.append((
            "CRITICAL",
            f"PII patterns detected in text fields ({patterns})",
            f"{sum(text_pattern_counts.values())} pattern matches across "
            f"{len(text_pattern_counts)} pattern type(s) found in narrative/text fields.\n\n"
            "**Add scrub_text rules:**\n```yaml\n"
            "- name: scrub narrative text\n"
            "  match: \"*.text.div\"\n  action: scrub_text\n\n"
            "- name: scrub note text\n"
            "  match: \"*.note.text\"\n  action: scrub_text\n```",
        ))

    # ---- HIGH: high information loss from redact ---------------------------
    il_samples = dim_samples.get("information_loss", [])
    il_avg = sum(il_samples) / len(il_samples) if il_samples else None
    if il_avg is not None and il_avg < 0.55 and action_totals.get("redact", 0) > 0:
        redact_pct = action_totals["redact"] / max(sum(action_totals.values()), 1) * 100
        recs.append((
            "HIGH",
            f"High information loss from `redact` ({redact_pct:.0f}% of actions) — utility {_pct(il_avg)}",
            "Most of your utility loss comes from `redact` actions.  "
            "Consider lower-loss alternatives for fields where structure must be preserved:\n\n"
            "| Replace `redact` on | With | Why |\n"
            "|---------------------|------|-----|\n"
            "| `*.id`, `Patient.identifier` | `cryptohash` or `gpas_pseudonymize` | "
            "Preserves linkage across datasets |\n"
            "| `Patient.birthDate` | `generalize` (year-only) or `perturb` | "
            "Age band sufficient for most analysis |\n"
            "| Address fields | `generalize` | Regional analysis preserved |\n"
            "| Practitioner names | `substitute` | Structure preserved, PHI removed |",
        ))

    # ---- HIGH: low field retention -----------------------------------------
    fr_samples = dim_samples.get("field_retention", [])
    fr_avg = sum(fr_samples) / len(fr_samples) if fr_samples else None
    if fr_avg is not None and fr_avg < 0.60:
        recs.append((
            "HIGH",
            f"Low field retention ({_pct(fr_avg)}) — many fields removed",
            "A large proportion of fields are being redacted entirely.  "
            "Fields with value=`[REDACTED]` or removed from the resource reduce "
            "utility for downstream analytics.\n\n"
            "Review which rules use `action: redact` and consider whether "
            "`generalize`, `cryptohash`, or `substitute` could replace them.",
        ))

    # ---- MEDIUM: missed rules ----------------------------------------------
    if missed_rules:
        top_missed = "\n".join(f"- `{r}`" for r, _ in missed_rules.most_common(10))
        recs.append((
            "MEDIUM",
            f"{len(missed_rules)} rule(s) never fired on applicable resources",
            "These rules are defined in your config but were not applied to any "
            "resources of the types they target.  This may mean:\n"
            "1. The target field is absent from your exported data\n"
            "2. The FHIRPath expression has a typo or wrong resource type prefix\n"
            "3. The rule condition (`where`) filtered out all matches\n\n"
            f"**Missed rules:**\n{top_missed}\n\n"
            "Check the FHIRPath expressions against your actual data "
            "using `GET /fhir/{ResourceType}/{id}`.",
        ))

    # ---- MEDIUM: reference integrity ---------------------------------------
    if ri is not None and ri < 0.95:
        recs.append((
            "MEDIUM",
            f"Reference integrity {_pct(ri)} — dangling references after de-identification",
            "When resource IDs are replaced by pseudonyms/hashes, `reference` fields "
            "pointing to those resources become invalid.\n\n"
            "**Fix:** Enable in your config:\n```yaml\nrewrite_references: true\n```\n\n"
            "This causes the pipeline to automatically rewrite all internal references "
            "to match new pseudonymized IDs.",
        ))

    # ---- LOW: schema validation ----------------------------------------------
    sv_samples = dim_samples.get("schema_validation", [])
    sv_avg = sum(sv_samples) / len(sv_samples) if sv_samples else None
    if sv_avg is not None and sv_avg < 0.95:
        recs.append((
            "LOW",
            f"Schema validation {_pct(sv_avg)} — some resources missing required fields",
            "Some de-identified resources are missing `id` or `resourceType` fields, "
            "or contain empty required arrays (e.g. `name: []`).\n\n"
            "Check that rules do not redact `*.id` without replacing it with a pseudonym, "
            "or use `cryptohash`/`gpas_pseudonymize` for ID fields instead of `redact`.",
        ))

    return recs
