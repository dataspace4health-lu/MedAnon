"""Page 5 — Condition Browser: search HAPI FHIR patients by illness / diagnosis."""
import json
import os
import re as _re
from collections import Counter

import streamlit as st

from utils.api import process_everything
from utils.fhir import capability_statement, search_conditions
from utils.sidebar import render_sidebar

FHIR_URL = os.environ.get("FHIR_URL", "http://localhost:8081/fhir").rstrip("/")
CARD_COLS = 3

st.set_page_config(page_title="Condition Browser — MedAnon", layout="wide")
render_sidebar()

st.title("Condition Browser")
st.caption(
    "Search HAPI FHIR patients by illness or diagnosis code, "
    "then de-identify all linked resources via `$everything`."
)

# ── Connectivity probe ────────────────────────────────────────────────────────
fhir_ok, _ = capability_statement()
if not fhir_ok:
    st.error("Cannot reach HAPI FHIR server. Check the **Status Dashboard** page for details.")
    st.stop()

# ── Search bar ────────────────────────────────────────────────────────────────
col_q, col_status, col_count = st.columns([4, 2, 1])
with col_q:
    query = st.text_input(
        "Illness name or SNOMED code",
        placeholder="e.g. diabetes  or  44054006",
        help="Free text matches CodeableConcept display/text. A plain integer is treated as a SNOMED-CT code.",
    )
with col_status:
    clinical_status = st.selectbox(
        "Clinical status",
        ["any", "active", "resolved", "inactive"],
        help="Filter by Condition.clinicalStatus",
    )
with col_count:
    result_count = st.number_input("Max", min_value=1, max_value=500, value=50)

st.caption("Example: 'diabetes', 'hypertension', or SNOMED code '44054006' (Type 2 diabetes).")

if st.button("Search", type="primary"):
    with st.spinner("Searching…"):
        results = search_conditions(
            query=query,
            clinical_status=clinical_status,
            count=result_count,
        )
    st.session_state["conditions"] = results
    st.session_state["cond_search_done"] = True
    st.session_state.pop("cond_deid_result", None)
    st.session_state.pop("cond_selected", None)

conditions: list[dict] | None = st.session_state.get("conditions")
search_done: bool = st.session_state.get("cond_search_done", False)

if not search_done:
    st.info("Enter an illness name or SNOMED code and press **Search**.")
    st.stop()

if not conditions:
    st.warning("No conditions found matching your query.")
    st.stop()

unique_patients = len({c["patient_id"] for c in conditions if c["patient_id"]})
st.caption(
    f"{len(conditions)} condition(s) found across {unique_patients} unique patient(s)"
    " — expand a card to de-identify that patient"
)

# ── Condition cards (expandable) ──────────────────────────────────────────────
rows = [conditions[i:i + CARD_COLS] for i in range(0, len(conditions), CARD_COLS)]
for row in rows:
    cols = st.columns(min(len(row), CARD_COLS))
    for col, cond in zip(cols, row):
        with col:
            status = cond.get("clinical_status", "")
            diagnosis = cond.get("display") or cond.get("code") or "Unknown diagnosis"
            label = f"{diagnosis}  ·  {cond['patient_name']} [{status or 'unknown'}]"
            with st.expander(label):
                st.markdown(f"**Diagnosis:** {diagnosis}")
                st.markdown(
                    f"**Status:** {status or '—'}"
                    + (f"  \n**SNOMED code:** `{cond['code']}`" if cond.get("code") else "")
                )
                st.divider()
                st.markdown(
                    f"**Patient:** {cond['patient_name']}  \n"
                    f"**Born:** {cond.get('patient_birth_date') or '—'}  \n"
                    f"**Gender:** {cond.get('patient_gender') or '—'}  \n"
                    f"**Patient ID:** `{cond['patient_id']}`"
                )
                if st.button(
                    "De-identify patient",
                    key=f"cond_{cond['condition_id']}",
                    use_container_width=True,
                    type="primary",
                ):
                    st.session_state["cond_selected"] = cond
                    st.session_state.pop("cond_deid_result", None)
                    st.rerun()

# ── De-identify panel ─────────────────────────────────────────────────────────
selected = st.session_state.get("cond_selected")
if not selected:
    st.stop()

patient_id = selected["patient_id"]
patient_name = selected["patient_name"]

st.divider()
st.subheader(f"De-identify: {patient_name} (`{patient_id}`)")

col_info, col_btn = st.columns([3, 1])
with col_info:
    st.markdown(
        f"**Patient ID:** `{patient_id}`  \n"
        f"**Diagnosis:** {selected.get('display') or selected.get('code', '—')}  \n"
        f"**Status:** {selected.get('clinical_status', '—')}  \n"
        f"**Born:** {selected.get('patient_birth_date', '—')}  \n"
        f"**Gender:** {selected.get('patient_gender', '—')}"
    )
with col_btn:
    run_deid = st.button("Run $everything", type="primary", use_container_width=True)

if run_deid:
    lines: list[str] = []
    errors: list[str] = []
    counts: Counter = Counter()
    received = 0
    progress = st.progress(0, text="Streaming resources…")

    for ok, line in process_everything(
        server_url=FHIR_URL,
        resource_type="Patient",
        resource_id=patient_id,
        config_profile=st.session_state.get("config_profile", "auto"),
    ):
        received += 1
        if ok:
            lines.append(line)
            try:
                rtype = json.loads(line).get("resourceType", "Unknown")
                counts[rtype] += 1
            except Exception:
                pass
            progress.progress(
                min(received / (received + 10), 0.95),
                text=f"Received {received} resource(s)…",
            )
        else:
            errors.append(line)

    progress.progress(1.0, text=f"Done — {len(lines)} resource(s)")
    st.session_state["cond_deid_result"] = {
        "lines": lines,
        "errors": errors,
        "counts": counts,
        "patient_id": patient_id,
    }

# ── Show result ───────────────────────────────────────────────────────────────
result = st.session_state.get("cond_deid_result")
if result:
    lines = result["lines"]
    errors = result["errors"]
    counts = result["counts"]
    pid = result["patient_id"]

    for e in errors:
        st.error(e)

    if lines:
        st.markdown("**Resource type summary**")
        st.table([{"Resource type": rt, "Count": cnt} for rt, cnt in sorted(counts.items())])

        with st.expander(f"Show all {len(lines)} de-identified resources (JSON)"):
            for ln in lines:
                try:
                    st.json(json.loads(ln))
                except Exception:
                    st.code(ln)

        safe_pid = _re.sub(r"[^a-zA-Z0-9_\-]", "_", pid)
        st.download_button(
            label="Download NDJSON",
            data=("\n".join(lines)).encode(),
            file_name=f"patient_{safe_pid}_deid.ndjson",
            mime="application/x-ndjson",
        )
