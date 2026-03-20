"""Page 3 — Batch: upload a FHIR file (NDJSON, JSON Bundle, or XML), process it, download the result."""
import json
import os
import re as _re
from collections import Counter

import streamlit as st

from utils.api import process_batch
from utils.sidebar import render_sidebar

st.set_page_config(page_title="Batch Processing — MedAnon", layout="wide")
render_sidebar()

st.title("Batch Processing")
st.caption("Upload a FHIR file (NDJSON, JSON Bundle, or XML), de-identify every resource, and download the result.")

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024   # 10 MB — must match MEDANON_MAX_BODY_BYTES
_ALLOWED_MIME = {"application/json", "application/x-ndjson", "application/xml",
                 "application/fhir+json", "application/fhir+xml", "text/xml", "text/plain", ""}

_EXT_TO_CT = {
    ".ndjson": "application/x-ndjson",
    ".json":   "application/json",
    ".xml":    "application/fhir+xml",
}

uploaded = st.file_uploader("FHIR file", type=["ndjson", "json", "xml"])

if uploaded is None:
    st.info("Upload a FHIR file (NDJSON, JSON Bundle, or XML) to get started.")
    with st.expander("Expected input format"):
        st.code(
            '{"resourceType":"Patient","id":"p1","gender":"male","birthDate":"1980-01-01"}\n'
            '{"resourceType":"Condition","id":"c1","subject":{"reference":"Patient/p1"}}',
            language="json",
        )
        st.caption("NDJSON: one FHIR JSON object per line. Also accepts JSON Bundle or FHIR XML.")
    st.stop()

if uploaded.size > _MAX_UPLOAD_BYTES:
    st.error(f"File is too large ({uploaded.size / 1024 / 1024:.1f} MB). Maximum allowed size is 10 MB.")
    st.stop()

if uploaded.type not in _ALLOWED_MIME:
    st.error(f"Unexpected file type '{uploaded.type}'. Please upload a FHIR NDJSON, JSON, or XML file.")
    st.stop()

try:
    raw_bytes = uploaded.read()
    raw_text = raw_bytes.decode("utf-8", errors="strict")
except UnicodeDecodeError:
    st.error("File is not valid UTF-8. Please upload a properly encoded file.")
    st.stop()
except Exception as exc:
    st.error(f"Could not read file: {exc}")
    st.stop()

# Detect format from file extension
ext = os.path.splitext(uploaded.name)[1].lower()
content_type = _EXT_TO_CT.get(ext, "application/x-ndjson")

# Count resources for display
if content_type == "application/x-ndjson":
    raw_lines = [ln for ln in raw_text.splitlines() if ln.strip() and not ln.strip().startswith("//")]
    resource_count_label = f"{len(raw_lines)} resource(s) (NDJSON)"
elif content_type == "application/fhir+xml":
    resource_count_label = f"{len(raw_bytes):,} bytes (XML)"
else:
    # JSON — try to count entries if Bundle
    try:
        parsed = json.loads(raw_text)
        if isinstance(parsed, dict) and parsed.get("resourceType") == "Bundle":
            entry_count = len(parsed.get("entry", []))
            resource_count_label = f"{entry_count} resource(s) in Bundle (JSON)"
        elif isinstance(parsed, list):
            resource_count_label = f"{len(parsed)} resource(s) (JSON array)"
        else:
            resource_count_label = "1 resource (JSON)"
    except Exception:
        resource_count_label = f"{len(raw_bytes):,} bytes (JSON)"

st.caption(f"{resource_count_label} — {len(raw_bytes):,} bytes — format: `{ext or 'unknown'}`")

with st.expander("Preview (first 3 lines / top of file)"):
    if content_type == "application/x-ndjson":
        for ln in raw_text.splitlines()[:3]:
            if ln.strip():
                try:
                    st.json(json.loads(ln))
                except Exception:
                    st.code(ln)
    elif content_type == "application/fhir+xml":
        st.code(raw_text[:800], language="xml")
    else:
        try:
            parsed = json.loads(raw_text)
            if isinstance(parsed, dict) and parsed.get("resourceType") == "Bundle":
                entries = parsed.get("entry", [])[:3]
                for e in entries:
                    st.json(e.get("resource", e))
            else:
                st.json(parsed if not isinstance(parsed, list) else parsed[:3])
        except Exception:
            st.code(raw_text[:800])

run = st.button("Process", type="primary")

if not run:
    st.stop()

# ── Stream processing ─────────────────────────────────────────────────────────
out_lines: list[str] = []
errors: list[str] = []
counts: Counter = Counter()

_MAX_ERRORS_SHOWN = 10
progress = st.progress(0, text="Starting…")
error_container = st.container()
received = 0

for ok, line in process_batch(raw_bytes, content_type=content_type):
    received += 1
    if ok:
        out_lines.append(line)
        try:
            rtype = json.loads(line).get("resourceType", "Unknown")
            counts[rtype] += 1
        except Exception:
            pass
        progress.progress(
            min(received / (received + 10), 0.95),
            text=f"Processed {len(out_lines)} resource(s)…",
        )
    else:
        errors.append(line)
        if len(errors) <= _MAX_ERRORS_SHOWN:
            with error_container:
                st.error(f"Error on resource {received}: {line}")

if len(errors) > _MAX_ERRORS_SHOWN:
    with error_container:
        st.warning(f"… and {len(errors) - _MAX_ERRORS_SHOWN} more error(s) not shown.")

progress.progress(1.0, text=f"Done — {len(out_lines)} resource(s) processed, {len(errors)} error(s)")

# ── Results ───────────────────────────────────────────────────────────────────
if errors:
    st.warning(f"{len(errors)} error(s) occurred. Successfully processed resources are still available for download.")

if out_lines:
    st.success(f"De-identified {len(out_lines)} resource(s).")

    st.markdown("**Resource type summary**")
    st.table([{"Resource type": rt, "Count": cnt} for rt, cnt in sorted(counts.items())])

    ndjson_out = "\n".join(out_lines).encode()
    base_name = uploaded.name
    for suffix in (".ndjson", ".json", ".xml"):
        if base_name.endswith(suffix):
            base_name = base_name[: -len(suffix)]
            break
    safe_base = _re.sub(r"[^a-zA-Z0-9_\-]", "_", base_name)
    st.download_button(
        label="Download de-identified NDJSON",
        data=ndjson_out,
        file_name=f"{safe_base}_deid.ndjson",
        mime="application/x-ndjson",
    )
else:
    st.warning("No output resources were returned.")
