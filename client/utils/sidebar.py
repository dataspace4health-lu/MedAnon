"""Shared sidebar component for all MedAnon pages."""
import os

import streamlit as st

from utils.api import health

_PAGES = [
    ("pages/1_Patient_Browser.py",  "Patient Browser"),
    ("pages/5_Condition_Browser.py","Condition Browser"),
    ("pages/2_Process_Resource.py", "Process Resource"),
    ("pages/6_Risk_Assessment.py",  "Risk Assessment"),
    ("pages/7_Synthetic_Data.py",   "Synthetic Data"),
    ("pages/3_Batch.py",            "Batch Processing"),
    ("pages/4_Status.py",           "Status Dashboard"),
]


def render_sidebar() -> None:
    """Render the shared MedAnon sidebar on any page."""
    with st.sidebar:
        st.markdown("## MedAnon")

        ok, detail = health()
        if ok:
            version = detail.get("version", "")
            label = f"v{version}" if version else "online"
            st.success(f"● Anonymizer {label}")
        else:
            st.error("● Anonymizer offline")

        st.divider()

        for path, label in _PAGES:
            st.page_link(path, label=label)

        st.divider()

        with st.expander("Connection"):
            medanon_url = os.environ.get("MEDANON_URL", "http://localhost:8000")
            fhir_url = os.environ.get("FHIR_URL", "http://localhost:8081/fhir")
            st.caption(f"**API:** `{medanon_url}`")
            st.caption(f"**FHIR:** `{fhir_url}`")

        st.caption("FHIR de-identification toolkit")
