"""MedAnon Streamlit UI — entrypoint / home page."""
import streamlit as st

from utils.sidebar import render_sidebar

st.set_page_config(
    page_title="MedAnon",
    page_icon=None,
    layout="wide",
    initial_sidebar_state="expanded",
)

render_sidebar()

# ── Home ──────────────────────────────────────────────────────────────────────
st.title("MedAnon")
st.caption(
    "FHIR de-identification and pseudonymization — select a page from the sidebar."
)

st.divider()

col1, col2 = st.columns(2)

with col1:
    st.page_link("pages/1_Patient_Browser.py", label="Patient Browser", use_container_width=True)
    st.caption("Search patients by name and de-identify all linked resources via `$everything`.")

    st.page_link("pages/5_Condition_Browser.py", label="Condition Browser", use_container_width=True)
    st.caption("Find patients by illness name or SNOMED code, then de-identify.")

    st.page_link("pages/2_Process_Resource.py", label="Process Resource", use_container_width=True)
    st.caption("Paste a FHIR resource (JSON, NDJSON, or XML) and de-identify it inline.")

    st.page_link("pages/6_Risk_Assessment.py", label="Risk Assessment", use_container_width=True)
    st.caption("Compute k-anonymity and re-identification risk on de-identified data.")

with col2:
    st.page_link("pages/7_Synthetic_Data.py", label="Synthetic Data", use_container_width=True)
    st.caption("Generate synthetic FHIR patients that preserve statistical distributions.")

    st.page_link("pages/3_Batch.py", label="Batch Processing", use_container_width=True)
    st.caption("Upload an NDJSON, JSON Bundle, or XML file and download the de-identified result.")

    st.page_link("pages/4_Status.py", label="Status Dashboard", use_container_width=True)
    st.caption("Live health check for all backend services (anonymizer, FHIR, gPAS).")
