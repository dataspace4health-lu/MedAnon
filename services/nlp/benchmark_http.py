"""
Presidio NLP accuracy benchmark — HTTP API edition.

Calls POST /v1/detect on the running NLP service instead of loading the model
in-process, so it works even when the container has no memory headroom.

Ground truth is derived from structured FHIR data (Patient, Practitioner),
not from regex patterns that mirror the detector — prevents overfitting.

Usage:
  python3 benchmark_http.py
  python3 benchmark_http.py --url http://localhost:8200
  python3 benchmark_http.py --dataset /path/to/TestBase --limit 200
  python3 benchmark_http.py --threshold 0.3 --entity PERSON --entity DATE_TIME
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Reuse ground-truth helpers from benchmark_accuracy.py
# ---------------------------------------------------------------------------

_AGE_PATTERN = re.compile(r"\b(\d{1,3}\s*-?\s*year(?:s)?\s*-?\s*old)\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")


def _extract_patient_pii(patient: dict) -> dict[str, list[str]]:
    pii: dict[str, list[str]] = defaultdict(list)
    for name_obj in patient.get("name", []):
        for given in name_obj.get("given", []):
            if len(given) > 2:
                pii["PERSON"].append(given)
        family = name_obj.get("family", "")
        if len(family) > 2:
            pii["PERSON"].append(family)
    for ext in patient.get("extension", []):
        if "mothersMaidenName" in ext.get("url", ""):
            for part in ext.get("valueString", "").split():
                if len(part) > 2:
                    pii["PERSON"].append(part)
    for addr in patient.get("address", []):
        for line in addr.get("line", []):
            if len(line) > 3:
                pii["LOCATION"].append(line)
        for field in ("city", "state", "postalCode"):
            val = addr.get(field, "")
            if len(val) > 1:
                pii["LOCATION"].append(val)
    for ext in patient.get("extension", []):
        if "birthPlace" in ext.get("url", ""):
            addr = ext.get("valueAddress", {})
            for field in ("city", "state", "country"):
                val = addr.get(field, "")
                if len(val) > 1:
                    pii["LOCATION"].append(val)
    for telecom in patient.get("telecom", []):
        val = telecom.get("value", "")
        if telecom.get("system") == "phone" and len(val) > 6:
            pii["PHONE_NUMBER"].append(val)
        elif telecom.get("system") == "email":
            pii["EMAIL_ADDRESS"].append(val)
    dob = patient.get("birthDate", "")
    if dob:
        pii["DATE_TIME"].append(dob)
    for ident in patient.get("identifier", []):
        text = ident.get("type", {}).get("text", "")
        val = ident.get("value", "")
        if not val or len(val) < 4:
            continue
        if "Social Security" in text or "SSN" in text:
            pii["US_SSN"].append(val)
        elif "Passport" in text:
            pii["US_PASSPORT"].append(val)
        elif "Driver" in text or "License" in text:
            pii["US_DRIVER_LICENSE"].append(val)
    return dict(pii)


def _extract_practitioner_names(practitioner: dict) -> list[str]:
    names = []
    for name_obj in practitioner.get("name", []):
        for given in name_obj.get("given", []):
            if len(given) > 2:
                names.append(given)
        family = name_obj.get("family", "")
        if len(family) > 2:
            names.append(family)
    return names


def build_pii_registry(dataset_dir: str) -> tuple[dict, set[str]]:
    patient_pii: dict[str, dict[str, list[str]]] = {}
    practitioner_names: set[str] = set()
    dataset = Path(dataset_dir)
    patient_file = dataset / "Patient.000.ndjson"
    if patient_file.exists():
        with open(patient_file) as f:
            for line in f:
                try:
                    p = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                pid = p.get("id", "")
                if pid:
                    patient_pii[pid] = _extract_patient_pii(p)
    pract_file = dataset / "Practitioner.000.ndjson"
    if pract_file.exists():
        with open(pract_file) as f:
            for line in f:
                try:
                    p = json.loads(line.strip())
                except json.JSONDecodeError:
                    continue
                practitioner_names.update(_extract_practitioner_names(p))
    return patient_pii, practitioner_names


def extract_ground_truth(
    text: str,
    patient_pii: dict[str, list[str]] | None,
    practitioner_names: set[str] | None,
) -> list[tuple[int, int, str]]:
    spans: list[tuple[int, int, str]] = []
    if patient_pii:
        for entity_type, values in patient_pii.items():
            for val in values:
                for m in re.compile(re.escape(val)).finditer(text):
                    spans.append((m.start(), m.end(), entity_type))
    if practitioner_names:
        for name in practitioner_names:
            for m in re.compile(re.escape(name)).finditer(text):
                spans.append((m.start(), m.end(), "PERSON"))
    for m in _AGE_PATTERN.finditer(text):
        spans.append((m.start(), m.end(), "AGE"))
    for m in _ISO_DATE.finditer(text):
        date_str = m.group(1)
        if patient_pii and date_str in patient_pii.get("DATE_TIME", []):
            continue
        spans.append((m.start(), m.end(), "DATE_TIME"))
    for m in _US_DATE.finditer(text):
        spans.append((m.start(), m.end(), "DATE_TIME"))
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    deduped: list[tuple[int, int, str]] = []
    last_end = -1
    for s, e, t in spans:
        if s >= last_end:
            deduped.append((s, e, t))
            last_end = e
    return deduped


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def match_spans(
    predicted: list[tuple[int, int, str]],
    gold: list[tuple[int, int, str]],
    entity_filter: set[str] | None,
) -> tuple[list, list, list]:
    if entity_filter:
        predicted = [p for p in predicted if p[2] in entity_filter]
        gold = [g for g in gold if g[2] in entity_filter]
    matched_gold: set[int] = set()
    tp_pairs: list = []
    fp_preds: list = []
    for pred in predicted:
        ps, pe, pt = pred
        found = False
        for gi, (gs, ge, gt) in enumerate(gold):
            if gi in matched_gold:
                continue
            if gt == pt and _overlaps(ps, pe, gs, ge):
                matched_gold.add(gi)
                tp_pairs.append((pred, gold[gi]))
                found = True
                break
        if not found:
            fp_preds.append(pred)
    fn_golds = [g for gi, g in enumerate(gold) if gi not in matched_gold]
    return tp_pairs, fp_preds, fn_golds


# ---------------------------------------------------------------------------
# HTTP detection
# ---------------------------------------------------------------------------


def detect_via_http(
    url: str,
    text: str,
    entities: list[str],
    threshold: float,
) -> list[tuple[int, int, str]]:
    """Call POST /v1/detect and return (start, end, entity_type) spans."""
    payload = json.dumps(
        {
            "text": text,
            "entities": entities,
            "language": "en",
            "score_threshold": threshold,
        }
    ).encode()
    req = urllib.request.Request(
        f"{url}/v1/detect",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read())

    # Response is scrubbed_text + token_state — extract spans from token_state map
    # token_state.map: {"ENTITY_TYPE,original_value": "[[ENTITY_TYPE_N]]"}
    spans: list[tuple[int, int, str]] = []
    token_map = body.get("token_state", {}).get("map", {})
    for key, placeholder in token_map.items():
        parts = key.split(",", 1)
        if len(parts) != 2:
            continue
        entity_type, original = parts
        for m in re.finditer(re.escape(original), text):
            spans.append((m.start(), m.end(), entity_type))
    return spans


# ---------------------------------------------------------------------------
# Main benchmark
# ---------------------------------------------------------------------------


def run_benchmark(
    url: str,
    dataset_dir: str,
    limit: int,
    threshold: float,
    entity_filter: list[str] | None,
) -> None:
    dataset = Path(dataset_dir)
    docref_path = dataset / "DocumentReference.000.ndjson"
    if not docref_path.exists():
        print(f"ERROR: {docref_path} not found")
        sys.exit(1)

    # Health check
    try:
        with urllib.request.urlopen(f"{url}/health", timeout=5) as r:
            health = json.loads(r.read())
        print(f"NLP service: {health}")
    except Exception as exc:
        print(f"ERROR: NLP service not reachable at {url} — {exc}")
        sys.exit(1)

    print(f"\nMedAnon NLP Accuracy Benchmark (HTTP)")
    print(f"  Service  : {url}")
    print(f"  Dataset  : {dataset_dir}")
    print(f"  Limit    : {limit} documents")
    print(f"  Threshold: {threshold}")
    print(f"  Entities : {entity_filter or 'all'}")

    patient_pii, practitioner_names = build_pii_registry(dataset_dir)
    print(f"  Patients : {len(patient_pii)} loaded")
    print(f"  Practitioners: {len(practitioner_names)} name parts")
    print(f"  Ground truth : FHIR-structured (model-independent)")
    print()

    entities_to_use = entity_filter or [
        "PERSON",
        "LOCATION",
        "DATE_TIME",
        "PHONE_NUMBER",
        "EMAIL_ADDRESS",
        "US_SSN",
        "US_PASSPORT",
        "US_DRIVER_LICENSE",
        "AGE",
        "MEDICAL_LICENSE",
    ]
    filter_set = set(entity_filter) if entity_filter else None

    tp_count: dict[str, int] = defaultdict(int)
    fp_count: dict[str, int] = defaultdict(int)
    fn_count: dict[str, int] = defaultdict(int)

    docs_processed = 0
    total_chars = 0
    errors = 0
    total_latency = 0.0

    with open(docref_path) as f:
        for line in f:
            if docs_processed >= limit:
                break
            try:
                resource = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            subject_ref = resource.get("subject", {}).get("reference", "")
            pat_id = subject_ref.split("/")[-1] if "/" in subject_ref else ""
            pat_pii = patient_pii.get(pat_id)

            for c in resource.get("content", []):
                if docs_processed >= limit:
                    break
                raw = c.get("attachment", {}).get("data", "")
                if not raw:
                    continue
                try:
                    text = base64.b64decode(raw).decode("utf-8", errors="replace")
                except Exception:
                    continue
                if len(text) < 50:
                    continue

                gold = extract_ground_truth(text, pat_pii, practitioner_names)
                if not gold:
                    continue

                docs_processed += 1
                total_chars += len(text)

                try:
                    t0 = time.monotonic()
                    predicted = detect_via_http(url, text, entities_to_use, threshold)
                    total_latency += time.monotonic() - t0
                except Exception as exc:
                    errors += 1
                    print(
                        f"  [ERROR] {resource.get('id', '?')}: {exc}", file=sys.stderr
                    )
                    continue

                tp_pairs, fp_preds, fn_golds = match_spans(predicted, gold, filter_set)
                for _, gs in tp_pairs:
                    tp_count[gs[2]] += 1
                for p in fp_preds:
                    fp_count[p[2]] += 1
                for g in fn_golds:
                    fn_count[g[2]] += 1

                if docs_processed % 25 == 0:
                    print(f"  ... {docs_processed} docs processed", flush=True)

    if docs_processed == 0:
        print("No documents with ground truth found.")
        return

    # --- Results table ---
    all_types = sorted(set(list(tp_count) + list(fp_count) + list(fn_count)))
    col_w = 22
    print(
        f"\n{'Entity':<{col_w}} {'TP':>6} {'FP':>6} {'FN':>6}  {'Prec':>8} {'Recall':>8} {'F1':>8} {'Acc':>8}"
    )
    print("-" * (col_w + 62))

    total_tp = total_fp = total_fn = 0
    for et in all_types:
        tp, fp, fn = tp_count[et], fp_count[et], fn_count[et]
        total_tp += tp
        total_fp += fp
        total_fn += fn
        prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1 = 2 * prec * recall / (prec + recall) if (prec + recall) > 0 else 0.0
        acc = tp / (tp + fp + fn) if (tp + fp + fn) > 0 else 0.0
        bar = "█" * int(f1 * 20)
        print(
            f"{et:<{col_w}} {tp:>6} {fp:>6} {fn:>6}  {prec:>7.1%} {recall:>7.1%} {f1:>7.1%} {acc:>7.1%}  {bar}"
        )

    print("-" * (col_w + 62))
    prec_o = total_tp / (total_tp + total_fp) if (total_tp + total_fp) > 0 else 0.0
    recall_o = total_tp / (total_tp + total_fn) if (total_tp + total_fn) > 0 else 0.0
    f1_o = (
        2 * prec_o * recall_o / (prec_o + recall_o) if (prec_o + recall_o) > 0 else 0.0
    )
    acc_o = (
        total_tp / (total_tp + total_fp + total_fn)
        if (total_tp + total_fp + total_fn) > 0
        else 0.0
    )

    print(
        f"{'OVERALL':<{col_w}} {total_tp:>6} {total_fp:>6} {total_fn:>6}  "
        f"{prec_o:>7.1%} {recall_o:>7.1%} {f1_o:>7.1%} {acc_o:>7.1%}"
    )

    avg_latency_ms = (total_latency / docs_processed) * 1000 if docs_processed else 0
    print(f"\nSummary")
    print(f"  Documents processed : {docs_processed}")
    print(f"  Total characters    : {total_chars:,}")
    print(f"  Errors              : {errors}")
    print(f"  Avg latency/doc     : {avg_latency_ms:.0f} ms")
    print(f"  Overall Precision   : {prec_o:.1%}")
    print(f"  Overall Recall      : {recall_o:.1%}")
    print(f"  Overall F1          : {f1_o:.1%}")
    print(f"  Overall Accuracy    : {acc_o:.1%}")


if __name__ == "__main__":
    default_dataset = str(
        Path(__file__).parent.parent / "anonymizer/tests/data/TestBase"
    )
    parser = argparse.ArgumentParser(
        description="Benchmark Presidio NLP accuracy via HTTP API"
    )
    parser.add_argument(
        "--url", default="http://localhost:8200", help="NLP service base URL"
    )
    parser.add_argument(
        "--dataset", default=default_dataset, help="Path to TestBase directory"
    )
    parser.add_argument(
        "--limit", type=int, default=500, help="Max documents to evaluate"
    )
    parser.add_argument(
        "--threshold", type=float, default=0.4, help="Presidio confidence threshold"
    )
    parser.add_argument(
        "--entity", action="append", help="Entity types to evaluate (repeatable)"
    )
    args = parser.parse_args()

    run_benchmark(
        url=args.url,
        dataset_dir=args.dataset,
        limit=args.limit,
        threshold=args.threshold,
        entity_filter=args.entity,
    )
