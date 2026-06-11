/**
 * Non-FHIR format de-identification — HL7 v2, CDA, and DICOM.
 *
 * Mirrors the backend format endpoints:
 *   POST /api/v1/process/hl7v2        text/plain  → text/plain
 *   POST /api/v1/process/cda          application/xml → application/xml
 *   POST /api/v1/process/dicom        application/dicom (bytes) → bytes
 *
 * When a ``config_profile`` is supplied the request is routed through the full
 * rule engine (the same one FHIR uses) via the format adapter; without it,
 * HL7 v2 / DICOM fall back to the legacy fixed-field scrubber.  CDA always uses
 * the engine path.  Per the FHIR-only-to-target invariant, these outputs are
 * returned to the caller — never uploaded to the FHIR target server.
 */

import { getAuthHeaders } from "./client";

export interface FormatTextResult {
  text: string;
}

function _profileQuery(configProfile?: string): string {
  return configProfile ? `?config_profile=${encodeURIComponent(configProfile)}` : "";
}

async function _readError(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (typeof body?.detail === "string") return body.detail;
    if (body?.detail?.message) return body.detail.message as string;
    return JSON.stringify(body?.detail ?? body);
  } catch {
    return response.statusText;
  }
}

/**
 * POST /api/v1/process/hl7v2 — de-identify a single HL7 v2 message.
 *
 * With *configProfile* the message runs through the full rule engine
 * (Hl7v2Adapter); without it, the legacy fixed-field scrubber is used.
 */
export async function processHl7v2(
  message: string,
  configProfile?: string,
): Promise<FormatTextResult> {
  const response = await fetch(`/api/v1/process/hl7v2${_profileQuery(configProfile)}`, {
    method: "POST",
    headers: { "Content-Type": "text/plain; charset=utf-8", ...getAuthHeaders() },
    body: message,
  });
  if (!response.ok) {
    throw new Error(
      `HL7 v2 de-identification failed (${response.status}): ${await _readError(response)}`,
    );
  }
  return { text: await response.text() };
}

/**
 * POST /api/v1/process/cda — de-identify a single CDA / CCDA document through
 * the rule engine (CdaAdapter).  *configProfile* defaults to ``auto`` server-side.
 */
export async function processCda(
  xml: string,
  configProfile?: string,
): Promise<FormatTextResult> {
  const response = await fetch(`/api/v1/process/cda${_profileQuery(configProfile)}`, {
    method: "POST",
    headers: { "Content-Type": "application/xml; charset=utf-8", ...getAuthHeaders() },
    body: xml,
  });
  if (!response.ok) {
    throw new Error(
      `CDA de-identification failed (${response.status}): ${await _readError(response)}`,
    );
  }
  return { text: await response.text() };
}

export interface DicomResult {
  /** De-identified DICOM bytes, ready to download. */
  blob: Blob;
  filename: string;
}

/**
 * POST /api/v1/process/dicom — de-identify a single DICOM file.
 *
 * DICOM is binary, so the result is returned as a Blob for download rather
 * than rendered inline.  The de-identification markers (PS3.15 §E.3.1) are
 * stamped by the backend.
 */
export async function processDicom(
  file: File,
  configProfile?: string,
): Promise<DicomResult> {
  const buf = await file.arrayBuffer();
  const response = await fetch(`/api/v1/process/dicom${_profileQuery(configProfile)}`, {
    method: "POST",
    headers: { "Content-Type": "application/dicom", ...getAuthHeaders() },
    body: buf,
  });

  if (!response.ok) {
    throw new Error(
      `DICOM de-identification failed (${response.status}): ${await _readError(response)}`,
    );
  }

  const blob = await response.blob();
  const base = file.name.replace(/\.dcm$/i, "");
  return { blob, filename: `${base}_deidentified.dcm` };
}

export type TabularFormat = "csv" | "xlsx" | "parquet";

export interface TabularResult {
  blob: Blob;
  filename: string;
}

/** A single column → action mapping authored in the UI column-mapper. */
export interface ColumnRule {
  column: string;
  action: string;
  params?: Record<string, unknown>;
}

export interface TabularColumn {
  name: string;
  samples: string[];
  /**
   * Heuristic action suggestion from the backend column explorer (one of the
   * UI action values: none | redact | generalize_year | pseudonymize_gpas |
   * tokenize | nlp_scrub).  The mapper pre-selects this; the user can override.
   */
  recommended_action?: string;
}

export interface TabularPreview {
  format: string;
  row_count: number;
  columns: TabularColumn[];
}

const _TABULAR_CONTENT_TYPE: Record<TabularFormat, string> = {
  csv: "text/csv",
  xlsx: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  parquet: "application/vnd.apache.parquet",
};

/**
 * POST /api/v1/process/tabular/inspect — preview a file's columns + samples.
 *
 * Read-only; no de-identification.  Powers the UI column-mapper so the user can
 * see each column and a few example values before assigning actions.
 */
export async function inspectTabular(
  file: File,
  format: TabularFormat,
  delimiter?: string,
): Promise<TabularPreview> {
  const params = new URLSearchParams({ format });
  if (delimiter) params.set("delimiter", delimiter);

  const buf = await file.arrayBuffer();
  const response = await fetch(`/api/v1/process/tabular/inspect?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": _TABULAR_CONTENT_TYPE[format], ...getAuthHeaders() },
    body: buf,
  });

  if (!response.ok) {
    throw new Error(
      `Tabular inspect failed (${response.status}): ${await _readError(response)}`,
    );
  }
  return (await response.json()) as TabularPreview;
}

/**
 * POST /api/v1/process/tabular — de-identify a CSV / Excel / Parquet file.
 *
 * Tabular de-identification is by *column rule*.  Pass either *columnRules*
 * (authored in the UI column-mapper) OR a saved *configProfile* that contains
 * ``column:`` rules.  Returns the de-identified file as a Blob for download.
 */
export async function processTabular(
  file: File,
  format: TabularFormat,
  opts: { configProfile?: string; columnRules?: ColumnRule[]; delimiter?: string } = {},
): Promise<TabularResult> {
  const params = new URLSearchParams({ format });
  if (opts.columnRules && opts.columnRules.length > 0) {
    params.set("rules", JSON.stringify(opts.columnRules));
  } else if (opts.configProfile) {
    params.set("config_profile", opts.configProfile);
  }
  if (opts.delimiter) params.set("delimiter", opts.delimiter);

  const buf = await file.arrayBuffer();
  const response = await fetch(`/api/v1/process/tabular?${params.toString()}`, {
    method: "POST",
    headers: { "Content-Type": _TABULAR_CONTENT_TYPE[format], ...getAuthHeaders() },
    body: buf,
  });

  if (!response.ok) {
    throw new Error(
      `Tabular de-identification failed (${response.status}): ${await _readError(response)}`,
    );
  }

  const blob = await response.blob();
  const dot = file.name.lastIndexOf(".");
  const base = dot > 0 ? file.name.slice(0, dot) : file.name;
  const ext = format === "xlsx" ? "xlsx" : format === "parquet" ? "parquet" : "csv";
  return { blob, filename: `${base}_deidentified.${ext}` };
}

export interface TabularBatchJob {
  job_id: string;
  status: string;
}

/**
 * POST /api/v1/jobs/tabular-batch — queue an async job that de-identifies many
 * tabular files of one format with a single saved profile.  Returns the job id
 * (track progress + download the result ZIP on the Jobs page).
 */
export async function submitTabularBatch(
  files: File[],
  format: TabularFormat,
  configProfile: string,
): Promise<TabularBatchJob> {
  const form = new FormData();
  form.set("format", format);
  form.set("config_profile", configProfile);
  for (const f of files) form.append("files", f, f.name);

  // Note: do NOT set Content-Type — the browser sets the multipart boundary.
  const response = await fetch(`/api/v1/jobs/tabular-batch`, {
    method: "POST",
    headers: { ...getAuthHeaders() },
    body: form,
  });

  if (!response.ok) {
    throw new Error(
      `Tabular batch submit failed (${response.status}): ${await _readError(response)}`,
    );
  }
  return (await response.json()) as TabularBatchJob;
}
