"""Page 1 — Patient Browser: search HAPI FHIR patients, de-identify via $everything."""
import json
import os
from collections import Counter

import streamlit as st

from utils.api import process_everything
from utils.fhir import capability_statement, search_patients
from utils.sidebar import render_sidebar

FHIR_URL = os.environ.get("FHIR_URL", "http://localhost:8081/fhir").rstrip("/")
CARD_COLS = 3


# --- Page config and sidebar ---
st.set_page_config(page_title="Patient Browser — MedAnon", layout="wide")
render_sidebar()

st.title("Patient Browser")
st.caption("Search HAPI FHIR patients and de-identify all linked resources via `$everything`.")

# ── Connectivity probe ────────────────────────────────────────────────────────
fhir_ok, _ = capability_statement()
if not fhir_ok:
    st.error("Cannot reach HAPI FHIR server. Check the **Status Dashboard** page for details.")
    st.stop()

# ── Search bar ────────────────────────────────────────────────────────────────

with st.container():
    col_search, col_count = st.columns([4, 1])
    with col_search:
        name_query = st.text_input("Search by name", placeholder="Leave empty to list all")
    with col_count:
        result_count = st.number_input("Max results", min_value=1, max_value=200, value=20)

st.caption("Example: search for 'Smith' or 'Johnson'. Leave empty to list all patients.")

if st.button("Search", type="primary"):
    with st.spinner("Searching…"):
        patients = search_patients(name=name_query, count=result_count)
    st.session_state["patients"] = patients
    st.session_state["search_done"] = True
    st.session_state.pop("deid_result", None)
    st.session_state.pop("selected_patient", None)

patients: list[dict] | None = st.session_state.get("patients")
search_done: bool = st.session_state.get("search_done", False)

if not search_done:
    st.info("Enter a search query and press **Search** to browse patients.")
    st.stop()

if not patients:
    st.warning("No patients found.")
    st.stop()


st.caption(f"{len(patients)} patient(s) found — click De-identify on a card to process")

# ── Patient cards (expandable) ────────────────────────────────────────────────

rows = [patients[i:i + CARD_COLS] for i in range(0, len(patients), CARD_COLS)]
for row in rows:
    cols = st.columns(min(len(row), CARD_COLS))
    for col, patient in zip(cols, row):
        with col:
            name = patient.get("name") or "(unnamed)"
            st.markdown(f"### {name}")
            st.markdown(f"**Gender:** {patient.get('gender', '—')}")
            st.markdown(f"**Born:** {patient.get('birthDate') or '—'}")
            st.markdown(f"**ID:** `{patient['id']}`")
            if st.button(
                "De-identify",
                key=f"select_{patient['id']}",
                use_container_width=True,
                type="primary",
            ):
                st.session_state["selected_patient"] = patient
                st.session_state.pop("deid_result", None)
                st.rerun()

# ── De-identify panel ─────────────────────────────────────────────────────────
selected = st.session_state.get("selected_patient")
if not selected:
    st.stop()

patient_id = selected["id"]
patient_name = selected.get("name") or "(unnamed)"

st.divider()
st.subheader(f"De-identify: {patient_name} (`{patient_id}`)")

col_info, col_btn = st.columns([3, 1])
with col_info:
    st.markdown(
        f"**Patient ID:** `{patient_id}`  \n"
        f"**Born:** {selected.get('birthDate') or '—'}  \n"
        f"**Gender:** {selected.get('gender') or '—'}"
    )
with col_btn:
    run_deid = st.button("Run $everything", type="primary", use_container_width=True)

if run_deid:
    lines: list[str] = []
    errors: list[str] = []
    counts: Counter = Counter()
    progress = st.progress(0, text="Streaming resources…")

    for ok, line in process_everything(
        server_url=FHIR_URL,
        resource_type="Patient",
        resource_id=patient_id,
    ):
        if ok:
            lines.append(line)
            try:
                rtype = json.loads(line).get("resourceType", "Unknown")
                counts[rtype] += 1
            except Exception:
                pass
            progress.progress(
                min(len(lines) / max(len(lines) + 1, 1), 0.99),
                text=f"Received {len(lines)} resource(s)…",
            )
        else:
            errors.append(line)

    progress.progress(1.0, text=f"Done — {len(lines)} resource(s)")
    st.session_state["deid_result"] = {
        "lines": lines,
        "errors": errors,
        "counts": counts,
        "patient_id": patient_id,
    }

# ── Show result ───────────────────────────────────────────────────────────────
result = st.session_state.get("deid_result")
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

        import re as _re
        safe_pid = _re.sub(r"[^a-zA-Z0-9_\-]", "_", pid)
        st.download_button(
            label="Download NDJSON",
            data=("\n".join(lines)).encode(),
            file_name=f"patient_{safe_pid}_deid.ndjson",
            mime="application/x-ndjson",
        )
