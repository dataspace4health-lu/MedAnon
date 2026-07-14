"""Markdown rendering for a :class:`passport.QualityPassport`.

Extracted from the domain model so that ``passport.py`` carries data only and the
presentation layer depends on the model (never the reverse). ``QualityPassport.to_markdown``
delegates here.
"""

from __future__ import annotations

from passport import CATEGORIES, RESULT_FAIL, RESULT_NA, RESULT_PASS


def render_markdown(p) -> str:
    """Render *p* (a QualityPassport) as a Markdown quality report."""
    lines: list[str] = [
        f"# Quality Passport  {p.dataset_id}",
        "",
        f"- **Decision:** {p.decision}",
        "- **Overall score:** "
        + (
            f"{round(p.overall_score, 1)}% checks passing"
            if p.has_assessed
            else "not assessed"
        )
        + (f" (grade {p.overall_grade})" if p.overall_grade else ""),
        *(
            [f"- **Fitness:** {p.fitness.get('statement', '')}"]
            if p.fitness and p.fitness.get("statement")
            else []
        ),
        *(
            [
                "- **Assessment coverage:** "
                f"{p.coverage.get('checks_assessed')}/"
                f"{p.coverage.get('checks_total')} checks assessed"
                f" ({p.coverage.get('validation_depth')})"
                + (
                    "; not exercised: " + ", ".join(p.coverage.get("not_exercised", []))
                    if p.coverage.get("not_exercised")
                    else ""
                )
            ]
            if p.coverage and p.coverage.get("checks_total")
            else []
        ),
        f"- **Privacy processing allowed:** "
        f"{'yes' if p.privacy_processing_allowed else 'no'}",
        f"- **Resources assessed:** {p.resource_count}",
        f"- **Source types:** {', '.join(p.source_types) or 'unknown'}",
        f"- **Framework:** {p.framework}",
        f"- **Generated at:** {p.generated_at}",
        *(
            [
                "- **Reporting context:** "
                f"{p.evaluation.get('lifecycle_stage', 'operation')} stage, "
                f"{p.evaluation.get('org_role', 'data-receiving')} org"
            ]
            if p.evaluation
            else []
        ),
        "",
        "## Data-quality categories (Kahn 2016)",
        "",
        "| Category | Checks passed | Pass rate |",
        "|---|---|---|",
    ]
    for name in CATEGORIES:
        score = p.category_scores.get(name)
        shown = f"{round(score, 1)}%" if score is not None else "not assessed"
        assessed = sum(
            1 for c in p.checks if c.category == name and c.result != RESULT_NA
        )
        passed = sum(
            1 for c in p.checks if c.category == name and c.result == RESULT_PASS
        )
        lines.append(f"| {name} | {passed}/{assessed} | {shown} |")
    lines.append("")

    if p.scorecard:
        lines.append("## Dimension scorecard (DAMA / ISO 25012)")
        lines.append("")
        lines.append("| Dimension | Grade | Checks passed | Score |")
        lines.append("|---|---|---|---|")
        for dim, rep in p.scorecard.items():
            score = rep.get("score")
            shown = f"{score}%" if score is not None else "not assessed"
            lines.append(
                f"| {dim} | {rep.get('grade') or '-'} | "
                f"{rep.get('checks_passed', 0)}/{rep.get('checks_assessed', 0)} | {shown} |"
            )
        lines.append("")

    if p.phases:
        lines.append("## Audit phases (selected)")
        lines.append("")
        lines.append("| Phase | Decision | Checks passed | Score |")
        lines.append("|---|---|---|---|")
        for ph, rep in p.phases.items():
            score = rep.get("score")
            shown = f"{score}%" if score is not None else "not assessed"
            lines.append(
                f"| {ph} | {rep.get('decision', '')} | "
                f"{rep.get('checks_passed', 0)}/{rep.get('checks_assessed', 0)} | {shown} |"
            )
        lines.append("")

    if p.targets:
        lines.append("## Sectors (per-target verdicts)")
        lines.append("")
        lines.append("| Sector | Decision | Resources | Checks passed | Score |")
        lines.append("|---|---|---|---|---|")
        for tid, rep in p.targets.items():
            score = rep.get("score")
            shown = f"{score}%" if score is not None else "not assessed"
            lines.append(
                f"| {tid} | {rep.get('decision', '')} | "
                f"{rep.get('resource_count', 0)} | "
                f"{rep.get('checks_passed', 0)}/{rep.get('checks_assessed', 0)} | {shown} |"
            )
        lines.append("")

    if p.blockers:
        lines.append("## Blockers")
        lines.append("")
        lines.extend(f"- {b}" for b in p.blockers)
        lines.append("")

    failed = [c for c in p.checks if c.result == RESULT_FAIL]
    if failed:
        lines.append("## Failed checks")
        lines.append("")
        lines.append(
            "| Check | Category | Subcat | Context | Violations | Threshold | Recommendation |"
        )
        lines.append("|---|---|---|---|---|---|---|")
        for c in failed:
            lines.append(
                f"| {c.check_id} | {c.category} | {c.subcategory} | {c.context} | "
                f"{c.violations}/{c.applicable} ({round(c.violation_fraction * 100, 1)}%) | "
                f"{c.threshold} | {c.recommendation} |"
            )
        lines.append("")

    aud = p.auditability
    if aud:
        lines.append("## Auditability (fitness-for-use policy)")
        lines.append("")
        for k, v in aud.items():
            lines.append(f"- {k}: {v}")
        lines.append("")

    if p.approved_for or p.not_approved_for:
        lines.append("## Fitness for use")
        lines.append("")
        lines.extend(f"- approved: {u}" for u in p.approved_for)
        lines.extend(f"- not approved: {u}" for u in p.not_approved_for)
        lines.append("")

    prof = p.profile or {}
    if prof:
        lines.append("## Data profile (descriptive  not scored)")
        lines.append("")
        counts = prof.get("resource_counts") or {}
        if counts:
            lines.append(
                "- Resource types: " + ", ".join(f"{k}×{v}" for k, v in counts.items())
            )
        systems = prof.get("code_system_distribution") or {}
        if systems:
            lines.append(
                "- Code systems: " + ", ".join(f"{k} ({v})" for k, v in systems.items())
            )
        if prof.get("patient_gender_distribution"):
            lines.append(f"- Gender: {prof['patient_gender_distribution']}")
        lines.append(f"- Reference density: {prof.get('reference_density', 0)}")
        for s in prof.get("observation_value_stats") or []:
            lines.append(
                f"- Obs {s['code']} ({s['unit']}): n={s['count']}, "
                f"min={s['min']}, max={s['max']}, mean={s['mean']}, sd={s['stddev']}"
            )
        lines.append("")

    limitations = p._limitations()
    if limitations:
        lines.append("## Limitations")
        lines.append("")
        lines.extend(f"- {note}" for note in limitations)
        lines.append("")

    return "\n".join(lines)
