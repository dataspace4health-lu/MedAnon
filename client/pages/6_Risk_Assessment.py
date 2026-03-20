"""Page 6 — Risk Assessment: compute re-identification risk on de-identified FHIR resources."""
import json
import os

import pandas as pd
import streamlit as st

from utils.api import analyse_risk
from utils.sidebar import render_sidebar

# ── Page config ──────────────────────────────────────────────────────────────
st.set_page_config(page_title="Risk Assessment — MedAnon", layout="wide")
render_sidebar()

st.title("Risk Assessment")
st.caption(
    "Upload de-identified FHIR resources (NDJSON, JSON Bundle, or XML) — optionally including "
    "Condition resources — to compute k-anonymity and re-identification risk scores."
)

with st.expander("How this works", expanded=False):
    st.markdown("""
**Why risk assessment matters after de-identification**

gPAS pseudonymization protects against direct lookup attacks on patient IDs.
However, combining quasi-identifiers (birth year, gender, zip code) with
clinical data (diagnoses) can still re-identify individuals through linkage
with external datasets — even without any direct identifiers.

**Accepted input formats**
- **NDJSON** — one FHIR JSON object per line (most common batch output)
- **JSON** — single resource or FHIR Bundle (`entry[].resource` is unwrapped automatically)
- **XML** — FHIR XML document (single resource or Bundle)

**Metrics computed:**
| Metric | Formula | Interpretation |
|---|---|---|
| **k-anonymity (min k)** | smallest equivalence class | Each patient shares QIs with >= k others |
| **Prosecutor risk** | 1 / min_k | Probability of re-identifying a *targeted* individual |
| **Journalist risk** | max(1/k_i) | Easiest patient to re-identify |
| **Marketer risk** | groups / patients | Expected risk for a randomly drawn record |
| **l-diversity** | distinct Condition codes per group | Prevents attribute inference within groups |

**Risk levels:** Low (k>=5) · Medium (k>=3) · High (k>=2) · Critical (k=1)

**Tip:** Include both Patient and Condition resources for l-diversity analysis.
Run this *after* de-identification to measure residual risk.
""")

# ── Format → Content-Type map ─────────────────────────────────────────────────
_EXT_TO_CT = {
    ".ndjson": "application/x-ndjson",
    ".json":   "application/json",
    ".xml":    "application/fhir+xml",
}

_FORMAT_LABELS = ["NDJSON", "JSON / Bundle", "XML"]
_FORMAT_CT = {
    "NDJSON":        "application/x-ndjson",
    "JSON / Bundle": "application/json",
    "XML":           "application/fhir+xml",
}

# ── Input ─────────────────────────────────────────────────────────────────────
MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB

tab_upload, tab_paste = st.tabs(["Upload file", "Paste / type"])

with tab_upload:
    uploaded = st.file_uploader(
        "Upload FHIR file (NDJSON, JSON Bundle, or XML — Patient resources, optionally with Condition resources)",
        type=["ndjson", "json", "xml"],
        help="Max 10 MB. NDJSON: one FHIR JSON object per line. JSON: single resource or Bundle. XML: FHIR XML.",
    )

    upload_bytes: bytes | None = None
    upload_ct: str = "application/x-ndjson"

    if uploaded:
        if uploaded.size > MAX_UPLOAD_BYTES:
            st.error(f"File too large ({uploaded.size / 1024 / 1024:.1f} MB). Maximum is 10 MB.")
        else:
            upload_bytes = uploaded.read()
            ext = os.path.splitext(uploaded.name)[1].lower()
            upload_ct = _EXT_TO_CT.get(ext, "application/x-ndjson")

            # Preview
            raw_text = upload_bytes.decode("utf-8", errors="replace")
            if upload_ct == "application/x-ndjson":
                lines = [l for l in raw_text.splitlines() if l.strip() and not l.strip().startswith("//")]
                st.caption(f"{len(lines)} non-empty lines loaded from **{uploaded.name}**")
                with st.expander("Preview first 3 lines"):
                    for line in lines[:3]:
                        try:
                            st.json(json.loads(line))
                        except Exception:
                            st.code(line)
            elif upload_ct == "application/fhir+xml":
                st.caption(f"{len(upload_bytes):,} bytes loaded from **{uploaded.name}** (XML)")
                with st.expander("Preview"):
                    st.code(raw_text[:800], language="xml")
            else:
                try:
                    parsed = json.loads(raw_text)
                    if isinstance(parsed, dict) and parsed.get("resourceType") == "Bundle":
                        entry_count = len(parsed.get("entry", []))
                        st.caption(f"Bundle with {entry_count} entries loaded from **{uploaded.name}**")
                    else:
                        st.caption(f"JSON resource loaded from **{uploaded.name}**")
                    with st.expander("Preview"):
                        st.json(parsed if not isinstance(parsed, dict) or parsed.get("resourceType") != "Bundle"
                                else {"resourceType": "Bundle", "entry_count": len(parsed.get("entry", []))})
                except Exception:
                    st.caption(f"{len(upload_bytes):,} bytes loaded from **{uploaded.name}**")

    if st.button("Analyse Risk", type="primary", key="btn_upload", disabled=not upload_bytes):
        with st.spinner("Computing risk metrics…"):
            ok, report = analyse_risk(upload_bytes, content_type=upload_ct)
        if not ok:
            st.error(f"Error from server: {report.get('error', report)}")
            st.stop()
        st.session_state["risk_report"] = report

with tab_paste:
    fmt_label = st.selectbox(
        "Input format",
        _FORMAT_LABELS,
        help="Select the format of the content you will paste below.",
    )
    paste_ct = _FORMAT_CT[fmt_label]

    _PLACEHOLDERS = {
        "NDJSON":        '{"resourceType":"Patient","id":"p1","birthDate":"1980","gender":"male"}\n{"resourceType":"Condition","subject":{"reference":"Patient/p1"},...}',
        "JSON / Bundle": '{\n  "resourceType": "Bundle",\n  "type": "collection",\n  "entry": [\n    {"resource": {"resourceType": "Patient", "id": "p1", "birthDate": "1980", "gender": "male"}}\n  ]\n}',
        "XML":           '<Bundle xmlns="http://hl7.org/fhir">...</Bundle>',
    }

    _SAMPLE_NDJSON = "\n".join([
        '{"resourceType":"Patient","id":"p1","gender":"male","birthDate":"1982-01-01","address":[{"postalCode":"10115"}]}',
        '{"resourceType":"Patient","id":"p2","gender":"female","birthDate":"1975-01-01","address":[{"postalCode":"10115"}]}',
        '{"resourceType":"Patient","id":"p3","gender":"male","birthDate":"1982-01-01","address":[{"postalCode":"10115"}]}',
        '{"resourceType":"Patient","id":"p4","gender":"female","birthDate":"1990-01-01","address":[{"postalCode":"20148"}]}',
        '{"resourceType":"Patient","id":"p5","gender":"male","birthDate":"1990-01-01","address":[{"postalCode":"20148"}]}',
        '{"resourceType":"Patient","id":"p6","gender":"female","birthDate":"1975-01-01","address":[{"postalCode":"80331"}]}',
    ])

    if st.button("Load example (6 de-identified patients)"):
        st.session_state["risk_paste_area"] = _SAMPLE_NDJSON

    pasted = st.text_area(
        "Paste FHIR content here",
        height=250,
        placeholder=_PLACEHOLDERS[fmt_label],
        key="risk_paste_area",
    )
    if st.button("Analyse Risk", type="primary", key="btn_paste", disabled=not pasted.strip()):
        with st.spinner("Computing risk metrics…"):
            ok, report = analyse_risk(pasted.encode("utf-8"), content_type=paste_ct)
        if not ok:
            st.error(f"Error from server: {report.get('error', report)}")
            st.stop()
        st.session_state["risk_report"] = report

# ── Results ───────────────────────────────────────────────────────────────────
_LEVEL_FN = {
    "low": st.success,
    "medium": st.warning,
    "high": st.error,
    "critical": st.error,
}
_LEVEL_LABEL = {
    "low": "LOW RISK — k >= 5. Dataset meets basic k-anonymity standards.",
    "medium": "MEDIUM RISK — k = 3 or 4. Consider further generalization.",
    "high": "HIGH RISK — k = 2. Significant re-identification risk remains.",
    "critical": "CRITICAL — k = 1. One or more records are unique. Immediate action required.",
}


def _recommendations(report: dict) -> list[str]:
    recs = []
    s = report["summary"]
    if s["min_k"] < 5:
        recs.append("Apply broader date generalization (e.g., year-only birth date) to increase minimum group size.")
    if s["min_k"] < 3:
        recs.append("Suppress or redact postal codes for records in singleton or pair groups.")
    if s.get("singleton_groups", 0) > 0:
        recs.append(
            f"{s['singleton_groups']} group(s) contain only 1 record — "
            "these individuals are uniquely identifiable and should be suppressed or further generalized."
        )
    if s.get("records_with_missing_qi", 0) > 0:
        recs.append(
            f"{s['records_with_missing_qi']} record(s) have missing quasi-identifier fields — "
            "verify de-identification rules cover gender, birthDate, and address.postalCode."
        )
    ld = report.get("l_diversity", {})
    if ld.get("computed") and ld.get("violations", 0) > 0:
        recs.append(
            f"{ld['violations']} group(s) violate 2-diversity — "
            "patients in these groups share the same Condition codes and may be vulnerable to attribute inference."
        )
    if not recs:
        recs.append("Dataset meets all configured thresholds. No immediate action required.")
    return recs


if "risk_report" in st.session_state:
    report = st.session_state["risk_report"]
    s = report["summary"]
    meta = report.get("meta", {})
    ld = report.get("l_diversity", {})

    st.markdown("---")
    st.subheader("Risk Summary")

    level = s.get("risk_level", "low")
    _LEVEL_FN.get(level, st.info)(_LEVEL_LABEL.get(level, level.upper()))

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Min k (k-anonymity)", s.get("min_k", "—"))
    col2.metric("Prosecutor Risk", f"{s.get('prosecutor_risk', 0):.1%}")
    col3.metric("Journalist Risk", f"{s.get('journalist_risk', 0):.1%}")
    col4.metric("Marketer Risk", f"{s.get('marketer_risk', 0):.1%}")

    col5, col6, col7, col8 = st.columns(4)
    col5.metric("Total Patients", s.get("total_records", 0))
    col6.metric("Equivalence Classes", s.get("total_groups", 0))
    col7.metric("Singleton Groups", s.get("singleton_groups", 0))
    col8.metric("Missing QI Records", s.get("records_with_missing_qi", 0))

    st.markdown("---")
    st.subheader("l-Diversity (Condition code diversity per group)")
    if ld.get("computed"):
        ld_col1, ld_col2, ld_col3 = st.columns(3)
        ld_col1.metric("Min l", ld.get("min_l", "—"))
        ld_col2.metric("Max l", ld.get("max_l", "—"))
        ld_col3.metric("Groups violating 2-diversity", ld.get("violations", 0))
        if ld.get("details"):
            with st.expander(f"Groups violating 2-diversity ({len(ld['details'])} shown)"):
                viol_df = pd.DataFrame([
                    {
                        "Gender": d["group"].get("gender") or "(redacted)",
                        "Birth Year": d["group"].get("birth_year") or "(missing)",
                        "Zip Prefix": d["group"].get("zip_prefix") or "(redacted)",
                        "Distinct Codes (l)": d["l_value"],
                    }
                    for d in ld["details"]
                ])
                st.dataframe(viol_df, use_container_width=True, hide_index=True)
    else:
        st.info(
            ld.get("reason", "l-diversity not computed.")
            + "  \nTo enable l-diversity: include Condition resources alongside Patient resources in the input."
        )

    groups = report.get("groups", [])
    if groups:
        st.markdown("---")
        st.subheader("Equivalence Class Size Distribution")
        st.caption("Each bar shows how many groups have that exact size (k). Smaller groups = higher risk.")

        from collections import Counter
        size_counts = Counter(g["k"] for g in groups)
        chart_df = pd.DataFrame(
            sorted(size_counts.items()), columns=["Group size (k)", "Number of groups"]
        ).set_index("Group size (k)")
        st.bar_chart(chart_df)

    if groups:
        _MAX_GROUPS = 100
        shown = groups[:_MAX_GROUPS]
        label = f"All equivalence classes — {len(groups)} groups (sorted by k ascending)"
        if len(groups) > _MAX_GROUPS:
            label = f"Equivalence classes — showing first {_MAX_GROUPS} of {len(groups)} groups (sorted by k ascending)"
        with st.expander(label):
            if len(groups) > _MAX_GROUPS:
                st.caption(f"Showing first {_MAX_GROUPS} of {len(groups)} groups. Download the JSON report for the full list.")
            group_df = pd.DataFrame([
                {
                    "Gender": g["qi"].get("gender") or "(redacted)",
                    "Birth Year": g["qi"].get("birth_year") or "(missing)",
                    "Zip Prefix": g["qi"].get("zip_prefix") or "(redacted)",
                    "Size (k)": g["k"],
                    "Risk (1/k)": f"{g['risk_1_over_k']:.3f}",
                    "Weight": f"{g['weight']:.3f}",
                }
                for g in shown
            ])
            st.dataframe(group_df, use_container_width=True, hide_index=True)

    warnings = report.get("warnings", [])
    if warnings:
        st.markdown("---")
        for w in warnings:
            st.warning(w)

    st.markdown("---")
    st.subheader("Recommendations")
    for rec in _recommendations(report):
        st.markdown(f"- {rec}")

    st.markdown("---")
    with st.expander("Analysis metadata"):
        st.markdown(f"""
| Field | Value |
|---|---|
| Computed at | `{meta.get('computed_at', '—')}` |
| Input resources | {meta.get('input_lines', '—')} |
| Patient resources | {meta.get('patient_lines', '—')} |
| Condition resources | {meta.get('condition_lines', '—')} |
| Quasi-identifiers | `gender`, `birth_year`, `zip_prefix_3` |
""")

    st.download_button(
        label="Download JSON report",
        data=json.dumps(report, indent=2),
        file_name="risk_report.json",
        mime="application/json",
    )
