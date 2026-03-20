"""Page 4 — Status: health dashboard for all backend services."""
import os
import time

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from utils.api import health, ready
from utils.fhir import capability_statement, patient_count
from utils.sidebar import render_sidebar

st.set_page_config(page_title="Status Dashboard — MedAnon", layout="wide")
render_sidebar()

st.title("Service Status")
st.caption("Health dashboard for all backend services.")

MEDANON_URL = os.environ.get("MEDANON_URL", "http://localhost:8000")
FHIR_URL = os.environ.get("FHIR_URL", "http://localhost:8081/fhir")
GPAS_DOMAIN = os.environ.get("GPAS_DOMAIN", "")

# ── Auto-refresh toggle (non-blocking JS timer) ───────────────────────────────
col_refresh, col_last = st.columns([2, 3])
with col_refresh:
    auto_refresh = st.toggle("Auto-refresh every 30 s", value=False)
with col_last:
    if "last_checked" in st.session_state:
        st.caption(f"Last checked: {st.session_state['last_checked']}")

if auto_refresh:
    st_autorefresh(interval=30_000, key="status_refresh")


def _badge(ok: bool, label: str) -> str:
    return ("[online] " if ok else "[offline] ") + label


# ── Check all services ────────────────────────────────────────────────────────
with st.spinner("Checking services…"):
    anon_ok, anon_detail = health()
    anon_ready, anon_checks = ready()
    fhir_ok, fhir_meta = capability_statement()
    pat_count = patient_count() if fhir_ok else None

st.session_state["last_checked"] = time.strftime("%H:%M:%S")

# ── Service cards ─────────────────────────────────────────────────────────────
st.subheader("Services")

col1, col2, col3 = st.columns(3)

with col1:
    if anon_ok:
        version = anon_detail.get("version", "")
        sub = f"v{version}" if version else "online"
        st.success(f"{_badge(True, 'MedAnon')}  \n{sub}  \n`{MEDANON_URL}`")
    else:
        err = anon_detail.get("error", "unreachable")
        st.error(f"{_badge(False, 'MedAnon')}  \n{err}")

with col2:
    if anon_ready:
        st.success(_badge(True, "MedAnon /ready"))
    else:
        st.warning(_badge(False, "MedAnon /ready"))

    if GPAS_DOMAIN:
        st.caption(f"Pseudonym domain: `{GPAS_DOMAIN}`")

    if isinstance(anon_checks, dict) and anon_checks:
        for svc, result in anon_checks.items():
            if isinstance(result, dict):
                svc_ok = result.get("status") in ("ok", "healthy", True)
                svc_msg = result.get("message", "")
            else:
                svc_ok = bool(result)
                svc_msg = str(result)
            status_text = "ok" if svc_ok else "error"
            st.caption(f"{status_text} — {svc}: {svc_msg}")

with col3:
    if fhir_ok:
        fhir_ver = fhir_meta.get("fhirVersion", "")
        sw = fhir_meta.get("software", {})
        sw_str = " ".join(filter(None, [sw.get("name", ""), sw.get("version", "")]))
        sub = f"{sw_str} (FHIR {fhir_ver})" if fhir_ver else sw_str
        st.success(f"{_badge(True, 'HAPI FHIR')}  \n{sub}  \n`{FHIR_URL}`")
    else:
        err = fhir_meta.get("error", "unreachable")
        st.error(f"{_badge(False, 'HAPI FHIR')}  \nerror: {err}")

# ── HAPI stats ────────────────────────────────────────────────────────────────
st.divider()
st.subheader("HAPI FHIR Statistics")

if fhir_ok and pat_count is not None:
    st.metric("Patient count", pat_count)
else:
    st.info("HAPI FHIR is not reachable — statistics unavailable.")

# ── Dependency check table ────────────────────────────────────────────────────
if isinstance(anon_checks, dict) and anon_checks:
    st.divider()
    st.subheader("MedAnon Dependency Checks")
    rows = []
    for svc, result in anon_checks.items():
        if isinstance(result, dict):
            svc_ok = result.get("status") in ("ok", "healthy", True)
            svc_msg = result.get("message", "")
        else:
            svc_ok = bool(result)
            svc_msg = str(result)
        rows.append({"Service": svc, "Status": "ok" if svc_ok else "error", "Detail": svc_msg})
    st.table(rows)

# ── Raw detail expanders ──────────────────────────────────────────────────────
with st.expander("MedAnon /health raw response"):
    st.json(anon_detail)

with st.expander("HAPI FHIR CapabilityStatement (summary)"):
    if fhir_ok:
        st.json({
            "fhirVersion": fhir_meta.get("fhirVersion"),
            "software": fhir_meta.get("software"),
            "implementation": fhir_meta.get("implementation"),
            "format": fhir_meta.get("format"),
        })
    else:
        st.json(fhir_meta)

if st.button("Refresh now"):
    st.rerun()
