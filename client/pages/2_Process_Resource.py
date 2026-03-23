"""Page 2 — Process Resource: paste FHIR JSON / NDJSON / XML and de-identify."""
import json

import streamlit as st

from utils.api import process_raw
from utils.sidebar import render_sidebar

st.set_page_config(page_title="Process Resource — MedAnon", layout="wide")
render_sidebar()

st.title("Process Resource")
st.caption("Paste a FHIR resource (JSON, NDJSON, or XML) and run de-identification.")

SAMPLE = json.dumps(
    {
        "resourceType": "Patient",
        "id": "p-001",
        "name": [{"family": "Mustermann", "given": ["Max"]}],
        "birthDate": "1980-03-15",
        "gender": "male",
        "telecom": [{"system": "phone", "value": "+49-30-12345678"}],
    },
    indent=2,
)

if st.button("Load example"):
    st.session_state["content_area"] = SAMPLE

# ── Two-column comparison layout ──────────────────────────────────────────────
col_left, col_right = st.columns(2)

with col_left:
    st.subheader("Input")
    content = st.text_area(
        "input",
        height=560,
        placeholder=SAMPLE,
        label_visibility="collapsed",
        key="content_area",
    )
    col_fmt, col_btn = st.columns([2, 1])
    with col_fmt:
        output_format = st.selectbox(
            "Output format",
            ["json", "ndjson", "xml"],
            help=(
                "**json** — single resource or Bundle  \n"
                "**ndjson** — one resource per line  \n"
                "**xml** — FHIR XML"
            ),
        )
    with col_btn:
        st.write("")
        run = st.button(
            "De-identify",
            type="primary",
            disabled=not content.strip(),
            use_container_width=True,
        )

# ── Run de-identification every time the button is pressed ────────────────────
if run and content.strip():
    st.session_state.pop("proc_result", None)   # clear stale result first
    with st.spinner("Processing…"):
        ok, result = process_raw(content.strip(), output_format=output_format, config_profile=st.session_state.get("config_profile", "auto"))

    if ok:
        if output_format == "json":
            try:
                result = json.dumps(json.loads(result), indent=2, ensure_ascii=False)
            except Exception as e:
                st.warning(f"Could not format output as JSON: {e}")
        elif output_format == "ndjson":
            pretty_lines = []
            for ln in result.splitlines():
                if ln.strip():
                    try:
                        pretty_lines.append(json.dumps(json.loads(ln), indent=2, ensure_ascii=False))
                    except Exception as e:
                        st.warning(f"Could not format line: {e}")
                        pretty_lines.append(ln)
            result = "\n\n".join(pretty_lines)
        st.toast("De-identification complete.")
    else:
        st.toast("De-identification failed.")

    st.session_state["proc_result"] = (ok, result, output_format)

# ── Right column — copyable output ────────────────────────────────────────────
with col_right:
    st.subheader("Output")
    stored = st.session_state.get("proc_result")

    if stored is None:
        st.code("", language="json")
    else:
        ok, result, fmt = stored
        if ok and result:
            lang = "xml" if fmt == "xml" else "json"
            st.code(result, language=lang)
            st.download_button(
                label=f"Download .{fmt}",
                data=result.encode(),
                file_name=f"deid_result.{fmt}",
                mime="application/xml" if fmt == "xml" else "application/json",
            )
        elif not ok:
            st.error(result)
            st.code("", language="json")
