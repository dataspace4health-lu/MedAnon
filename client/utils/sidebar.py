"""Shared sidebar component for all MedAnon pages."""
import os

import streamlit as st

from utils.api import health
from utils.auth import require_login, has_role

_ALL_PAGES = [
    ("pages/1_Patient_Browser.py",  "Patient Browser",   "viewer"),
    ("pages/5_Condition_Browser.py", "Condition Browser", "viewer"),
    ("pages/2_Process_Resource.py", "Process Resource",   "analyst"),
    ("pages/6_Risk_Assessment.py",  "Risk Assessment",    "analyst"),
    ("pages/7_Synthetic_Data.py",   "Synthetic Data",     "analyst"),
    ("pages/3_Batch.py",            "Batch Processing",   "analyst"),
    ("pages/4_Status.py",           "Status Dashboard",   "viewer"),
]


def render_sidebar() -> None:
    """Render the shared MedAnon sidebar on any page."""
    user = require_login()
    st.session_state["kc_user"] = user

    with st.sidebar:
        st.markdown("## MedAnon")

        # User info
        name = user.get("name", "anonymous")
        roles = ", ".join(user.get("roles", []))
        st.caption(f"Logged in as **{name}** ({roles})")

        ok, detail = health()
        if ok:
            version = detail.get("version", "")
            label = f"v{version}" if version else "online"
            st.success(f"● Anonymizer {label}")
        else:
            st.error("● Anonymizer offline")

        st.divider()

        for path, label, min_role in _ALL_PAGES:
            if has_role(user, min_role):
                st.page_link(path, label=label)

        st.divider()

        st.selectbox(
            "Config profile",
            ["auto", "minimal", "gpas", "gdpr", "hipaa", "research", "structural"],
            key="config_profile",
            help=(
                "**auto** — gpas if GPAS_URL is set, else minimal  \n"
                "**minimal** — crypto hash + regex scrubbing  \n"
                "**gpas** — gPAS pseudonymization + generalization  \n"
                "**gdpr** — GDPR Art. 4(5) HMAC pseudonymization  \n"
                "**hipaa** — HIPAA Safe Harbor (45 CFR §164.514)  \n"
                "**research** — IRB-grade research profile  \n"
                "**structural** — structure-preserving de-identification"
            ),
        )

        with st.expander("Connection"):
            medanon_url = os.environ.get("MEDANON_URL", "http://localhost:8000")
            fhir_url = os.environ.get("FHIR_URL", "http://localhost:8081/fhir")
            st.caption(f"**API:** `{medanon_url}`")
            st.caption(f"**FHIR:** `{fhir_url}`")

        st.caption("FHIR de-identification toolkit")
