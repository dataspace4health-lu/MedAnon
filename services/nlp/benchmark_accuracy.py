"""
Presidio NLP accuracy benchmark against Synthea FHIR clinical notes.

Ground truth is derived from **structured FHIR data** (Patient, Practitioner),
NOT from regex patterns that mirror the detector.  This prevents overfitting:

  1. Load Patient.000.ndjson + Practitioner.000.ndjson → PII registry
     (names, addresses, cities, states, phones, SSNs, DOBs, maiden names)
  2. For each DocumentReference, link via subject.reference → Patient
  3. Search for the patient's known PII strings in the clinical note text
  4. Also extract structural patterns: ISO dates, "X year-old"
  5. Compare Presidio detections against this model-independent ground truth

Metrics per entity type and overall:
  - TP / FP / FN / Precision / Recall / F1 / Accuracy

Usage:
  python3 benchmark_accuracy.py
  python3 benchmark_accuracy.py --dataset /path/to/TestBase/
  python3 benchmark_accuracy.py --limit 200 --threshold 0.3
  python3 benchmark_accuracy.py --entity PERSON --entity DATE_TIME
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# PII Registry — built from structured FHIR resources
# ---------------------------------------------------------------------------

_AGE_PATTERN = re.compile(r"\b(\d{1,3}\s*-?\s*year(?:s)?\s*-?\s*old)\b", re.I)
_ISO_DATE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")
_US_DATE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")


def _extract_patient_pii(patient: dict) -> dict[str, list[str]]:
    """Extract known PII strings from a structured Patient resource."""
    pii: dict[str, list[str]] = defaultdict(list)

    # Names (given + family + maiden)
    for name_obj in patient.get("name", []):
        for given in name_obj.get("given", []):
            if len(given) > 2:
                pii["PERSON"].append(given)
        family = name_obj.get("family", "")
        if len(family) > 2:
            pii["PERSON"].append(family)

    # Mother's maiden name (extension)
    for ext in patient.get("extension", []):
        if "mothersMaidenName" in ext.get("url", ""):
            val = ext.get("valueString", "")
            for part in val.split():
                if len(part) > 2:
                    pii["PERSON"].append(part)

    # Address components
    for addr in patient.get("address", []):
        for line in addr.get("line", []):
            if len(line) > 3:
                pii["LOCATION"].append(line)
        city = addr.get("city", "")
        if len(city) > 2:
            pii["LOCATION"].append(city)
        state = addr.get("state", "")
        if len(state) > 1:
            pii["LOCATION"].append(state)
        postal = addr.get("postalCode", "")
        if postal:
            pii["LOCATION"].append(postal)

    # Birth place (extension)
    for ext in patient.get("extension", []):
        if "birthPlace" in ext.get("url", ""):
            addr = ext.get("valueAddress", {})
            for field in ("city", "state", "country"):
                val = addr.get(field, "")
                if len(val) > 1:
                    pii["LOCATION"].append(val)

    # Phone / email
    for telecom in patient.get("telecom", []):
        val = telecom.get("value", "")
        if telecom.get("system") == "phone" and len(val) > 6:
            pii["PHONE_NUMBER"].append(val)
        elif telecom.get("system") == "email":
            pii["EMAIL_ADDRESS"].append(val)

    # Birth date
    dob = patient.get("birthDate", "")
    if dob:
        pii["DATE_TIME"].append(dob)

    # Identifiers (SSN, passport, DL, MRN)
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
    """Extract name parts from a Practitioner resource."""
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
    """Build PII lookup from Patient + Practitioner NDJSON files.

    Returns:
        (patient_pii, practitioner_names)
        patient_pii: {patient_id: {entity_type: [values]}}
        practitioner_names: set of all practitioner name parts
    """
    patient_pii: dict[str, dict[str, list[str]]] = {}
    practitioner_names: set[str] = set()

    dataset = Path(dataset_dir)

    # Load patients
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

    # Load practitioners
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


# ---------------------------------------------------------------------------
# Ground-truth extraction — FHIR-based (model-independent)
# ---------------------------------------------------------------------------


def extract_ground_truth(
    text: str,
    patient_pii: dict[str, list[str]] | None,
    practitioner_names: set[str] | None,
) -> list[tuple[int, int, str]]:
    """Find known PII from structured FHIR data in the note text.

    Searches for exact string matches of patient PII values, practitioner
    names, and structural patterns (dates, ages).  This is model-independent:
    ground truth comes from the FHIR data, not from regex-mirroring.
    """
    spans: list[tuple[int, int, str]] = []

    # --- String-match PII from the patient's structured fields ---
    if patient_pii:
        for entity_type, values in patient_pii.items():
            for val in values:
                # Escape regex specials in the value, search case-sensitive
                pattern = re.compile(re.escape(val))
                for m in pattern.finditer(text):
                    spans.append((m.start(), m.end(), entity_type))

    # --- Practitioner names (appear in notes as treating physician) ---
    if practitioner_names:
        for name in practitioner_names:
            pattern = re.compile(re.escape(name))
            for m in pattern.finditer(text):
                spans.append((m.start(), m.end(), "PERSON"))

    # --- Structural patterns not in FHIR fields ---
    # Age expressions (PHI under HIPAA when >89)
    for m in _AGE_PATTERN.finditer(text):
        spans.append((m.start(), m.end(), "AGE"))

    # ISO dates (encounter dates in note headers, not from Patient.birthDate)
    for m in _ISO_DATE.finditer(text):
        # Skip if already covered by patient DOB
        date_str = m.group(1)
        if patient_pii and date_str in patient_pii.get("DATE_TIME", []):
            continue  # already counted from structured data
        spans.append((m.start(), m.end(), "DATE_TIME"))

    # US dates in note body
    for m in _US_DATE.finditer(text):
        spans.append((m.start(), m.end(), "DATE_TIME"))

    # --- Deduplicate overlapping spans (keep longest) ---
    spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
    deduped: list[tuple[int, int, str]] = []
    last_end = -1
    for s, e, t in spans:
        if s >= last_end:
            deduped.append((s, e, t))
            last_end = e
    return deduped


# ---------------------------------------------------------------------------
# Overlap matching
# ---------------------------------------------------------------------------


def _overlaps(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start < b_end and b_start < a_end


def match_spans(
    predicted: list[tuple[int, int, str]],
    gold: list[tuple[int, int, str]],
    entity_filter: set[str] | None,
) -> tuple[list, list, list]:
    """Match predicted vs gold spans.

    Returns (tp_pairs, fp_preds, fn_golds).
    A predicted span is TP if it overlaps a gold span of the same entity type.
    Each gold span may only be matched once.
    """
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
# Main benchmark
# ---------------------------------------------------------------------------


def run_benchmark(
    dataset_dir: str,
    limit: int,
    threshold: float,
    entity_filter: list[str] | None,
) -> None:
    from detector import HEALTHCARE_ENTITIES, _detect_entities_cached

    entities_to_use = entity_filter if entity_filter else list(HEALTHCARE_ENTITIES)
    filter_set = set(entity_filter) if entity_filter else None

    dataset = Path(dataset_dir)
    docref_path = dataset / "DocumentReference.000.ndjson"
    if not docref_path.exists():
        print(f"ERROR: {docref_path} not found")
        return

    # Build PII registry from structured FHIR data
    print(f"\nMedAnon NLP Accuracy Benchmark")
    print(f"  Dataset  : {dataset_dir}")
    print(f"  Limit    : {limit} documents")
    print(f"  Threshold: {threshold}")
    print(f"  Entities : {entity_filter or 'all'}")

    patient_pii, practitioner_names = build_pii_registry(dataset_dir)
    print(f"  Patients : {len(patient_pii)} loaded")
    print(f"  Practitioners: {len(practitioner_names)} name parts")
    print(f"  Ground truth : FHIR-structured (model-independent)")
    print()

    tp_count: dict[str, int] = defaultdict(int)
    fp_count: dict[str, int] = defaultdict(int)
    fn_count: dict[str, int] = defaultdict(int)

    docs_processed = 0
    docs_with_gt = 0
    total_chars = 0
    errors = 0

    with open(docref_path) as f:
        for line in f:
            if docs_processed >= limit:
                break
            try:
                resource = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            # Resolve patient ID from subject reference
            subject_ref = resource.get("subject", {}).get("reference", "")
            pat_id = subject_ref.split("/")[-1] if "/" in subject_ref else ""
            pat_pii = patient_pii.get(pat_id)

            for c in resource.get("content", []):
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
                docs_with_gt += 1
                total_chars += len(text)

                try:
                    raw_hits = _detect_entities_cached(
                        text, tuple(entities_to_use), threshold, "en"
                    )
                    predicted = list(raw_hits)
                except Exception as exc:
                    errors += 1
                    print(
                        f"  [ERROR] doc {resource.get('id', '?')}: {exc}",
                        file=sys.stderr,
                    )
                    continue

                tp_pairs, fp_preds, fn_golds = match_spans(predicted, gold, filter_set)

                for _, gold_span in tp_pairs:
                    tp_count[gold_span[2]] += 1
                for pred in fp_preds:
                    fp_count[pred[2]] += 1
                for gold_span in fn_golds:
                    fn_count[gold_span[2]] += 1

                if docs_processed % 50 == 0:
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
        tp = tp_count[et]
        fp = fp_count[et]
        fn = fn_count[et]
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

    print(f"\nSummary")
    print(f"  Documents processed : {docs_processed}")
    print(f"  Documents with GT   : {docs_with_gt}")
    print(f"  Total characters    : {total_chars:,}")
    print(f"  Errors              : {errors}")
    print(f"  Overall Accuracy    : {acc_o:.1%}")
    print(f"  Overall Precision   : {prec_o:.1%}")
    print(f"  Overall Recall      : {recall_o:.1%}")
    print(f"  Overall F1          : {f1_o:.1%}")
    print()

    # --- Example FP / FN ---
    print("False-positive examples (detected but not in FHIR ground truth):")
    _print_examples(
        docref_path,
        patient_pii,
        practitioner_names,
        limit,
        threshold,
        entities_to_use,
        filter_set,
        show_fp=True,
    )
    print()
    print("False-negative examples (in FHIR ground truth but not detected):")
    _print_examples(
        docref_path,
        patient_pii,
        practitioner_names,
        limit,
        threshold,
        entities_to_use,
        filter_set,
        show_fp=False,
    )


def _print_examples(
    docref_path: Path,
    patient_pii: dict,
    practitioner_names: set[str],
    limit: int,
    threshold: float,
    entities_to_use: list[str],
    filter_set: set[str] | None,
    show_fp: bool,
    max_examples: int = 10,
) -> None:
    from detector import _detect_entities_cached

    count = 0
    with open(docref_path) as f:
        for line in f:
            if count >= max_examples:
                break
            try:
                resource = json.loads(line.strip())
            except json.JSONDecodeError:
                continue

            subject_ref = resource.get("subject", {}).get("reference", "")
            pat_id = subject_ref.split("/")[-1] if "/" in subject_ref else ""
            pat_pii = patient_pii.get(pat_id)

            for c in resource.get("content", []):
                if count >= max_examples:
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

                try:
                    raw_hits = _detect_entities_cached(
                        text, tuple(entities_to_use), threshold, "en"
                    )
                    predicted = list(raw_hits)
                except Exception:
                    continue

                tp_pairs, fp_preds, fn_golds = match_spans(predicted, gold, filter_set)
                targets = fp_preds if show_fp else fn_golds

                for span in targets[:2]:
                    s, e, et = span
                    snippet = text[max(0, s - 20) : e + 20].replace("\n", " ")
                    marker = text[s:e]
                    label = "FP" if show_fp else "FN"
                    print(f"  [{label}] {et:20s}  «{marker}»  context: …{snippet}…")
                    count += 1
                    if count >= max_examples:
                        break


if __name__ == "__main__":
    default_dataset = str(
        Path(__file__).parent.parent / "anonymizer/tests/data/TestBase"
    )

    parser = argparse.ArgumentParser(
        description="Benchmark Presidio NLP accuracy against FHIR ground truth"
    )
    parser.add_argument(
        "--dataset",
        default=default_dataset,
        help="Path to TestBase directory containing NDJSON files",
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
        dataset_dir=args.dataset,
        limit=args.limit,
        threshold=args.threshold,
        entity_filter=args.entity,
    )
