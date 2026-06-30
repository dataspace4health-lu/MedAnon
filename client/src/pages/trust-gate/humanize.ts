/**
 * Human-readable translation of Trust Gate findings.
 *
 * The Quality Passport is PHI-safe by design: a violation detail carries the
 * resource (type + id), the offending field/path, and a short technical reason,
 * but never the raw field value (a value can itself be PHI — a name, a date).
 * This module turns those technical signals into plain language and pinpoints
 * *where* each error is, without surfacing any raw value.
 */

import type { TrustCheck } from "@/api/trustGate";

export interface LocatedProblem {
  /** "Patient/p1" or "Observation[3]" — the affected record. */
  resource: string;
  /** The exact field/path the problem is at. */
  field: string;
  /** Plain-language description of what is wrong at that point. */
  problem: string;
  /** A non-PHI value captured by the backend (code/format token/number), if any. */
  value?: string;
}

// Plain-language "what is wrong" by check id (leaf or full). Keeps the clinical
// data-quality framing without the jargon.
const CHECK_PLAIN: Record<string, string> = {
  "conformance.structural": "The FHIR validator reported a structural error in this record.",
  "conformance.value_format": "A field is not in the format FHIR requires (for example a malformed id or date).",
  "conformance.reference_integrity": "A reference points to a record that is not present in the dataset.",
  "conformance.status_not_entered_in_error": "This record is marked entered-in-error and must be removed before processing.",
  "conformance.id_present": "The record has no id.",
  "conformance.terminology": "A code is not recognized in its declared code system.",
  "terminology.validity": "A code is not recognized in its declared code system.",
  "completeness.required_elements": "A required field is missing on this record.",
  "completeness.value_or_absent": "This Observation has neither a result value nor a stated reason the value is absent.",
  "completeness.recommended_elements": "A recommended field is missing (not blocking, but lowers richness).",
  "plausibility.value_outlier": "This measurement is an extreme outlier versus comparable measurements.",
  "plausibility.value_outlier_stratified": "This measurement is an outlier for the patient's age/sex group.",
  "plausibility.distribution_drift": "The value distribution shifted sharply from the established baseline.",
  "clinical.measurement_after_birth": "A measurement is dated before the patient was born.",
  "temporal.ordering": "Two dates are in an impossible order (for example an end before its start).",
  "temporal.plausibility": "A date falls outside a plausible window.",
  "identity.uniqueness": "A business identifier is duplicated across records.",
  "identity.integrity": "An identifier is missing or malformed.",
  "provenance.present": "The record carries no provenance (source/extraction) metadata.",
};

/** Friendly leaf label: "conformance.reference_integrity" → "Reference integrity". */
export function prettyCheck(checkId: string): string {
  const plain = CHECK_PLAIN[checkId];
  if (plain) return plain;
  const leaf = (checkId.split(".").pop() ?? checkId).replace(/_/g, " ");
  return leaf.charAt(0).toUpperCase() + leaf.slice(1);
}

/** One-line "what this check verifies" — prefers the backend description. */
export function whatItChecks(c: TrustCheck): string {
  return c.description || CHECK_PLAIN[c.check_id] || prettyCheck(c.check_id);
}

/** Turn a backend detail string into a plain sentence (fallback humanizer). */
export function humanizeDetail(checkId: string, detail?: string): string {
  if (CHECK_PLAIN[checkId]) return CHECK_PLAIN[checkId];
  if (!detail) return prettyCheck(checkId);
  // Lightly de-jargon common phrasings.
  return detail
    .replace(/^id absent or empty$/i, "The record has no id.")
    .replace(/dataAbsentReason/g, "reason-for-absence")
    .replace(/value\[x\]/g, "result value")
    .replace(/entered-in-error/g, "entered-in-error (retracted)");
}

/** Build a located problem from one PHI-safe violation_detail row. */
export function locateProblem(checkId: string, d: Record<string, unknown>): LocatedProblem {
  const rtype = d.resource_type ? String(d.resource_type) : "";
  const rid = d.resource_id != null && String(d.resource_id) !== "" ? String(d.resource_id)
    : d.resource_index != null ? `[${String(d.resource_index)}]` : "";
  const resource = rtype ? `${rtype}${rid && !rid.startsWith("[") ? "/" : ""}${rid}` : (rid || "—");
  const field = String(d.path ?? d.attribute ?? "—");
  // Only ever a non-PHI token the backend explicitly chose to expose.
  const rawVal = d.found ?? d.value ?? d.expected;
  const value = rawVal != null && rawVal !== "" ? String(rawVal) : undefined;
  return { resource, field, problem: humanizeDetail(checkId, d.detail as string | undefined), value };
}

/** Severity from a check's violation share (for findings ranking/labels). */
export function severityOf(c: TrustCheck): "critical" | "major" | "minor" {
  if (c.critical) return "critical";
  return (c.violation_fraction ?? 0) >= 0.5 ? "major" : "minor";
}
