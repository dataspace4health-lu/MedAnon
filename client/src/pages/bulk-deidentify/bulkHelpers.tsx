import { extractFieldsDeep } from "@/lib/fhirFields";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export const PHASE_LABELS: Record<string, string> = {
  queued: "Queued",
  fetching: "Fetching from FHIR server",
  processing: "De-identifying resources",
  loading: "Loading NDJSON",
  uploading: "Uploading to target FHIR server",
  done: "Complete",
};

export interface NdjsonSummary {
  counts: Record<string, number>;
  allFieldCounts: Record<string, Record<string, number>>;
  piiSample: Record<string, unknown>[];
  totalResources: number;
  errorCount: number;
}

export async function parseNdjsonBlob(blob: Blob): Promise<NdjsonSummary> {
  const counts: Record<string, number> = {};
  const allFieldCounts: Record<string, Record<string, number>> = {};
  const piiSample: Record<string, unknown>[] = [];
  // Per-type PII sample cap — ensures uniform coverage across all resource types
  const PII_SAMPLE_PER_TYPE = 50;
  const piiSampleCount: Record<string, number> = {};
  // Cap expensive deep-field walk per type — schema is consistent after ~200 resources
  const FIELD_SAMPLE_LIMIT = 200;
  const fieldSampleCount: Record<string, number> = {};
  let totalResources = 0;
  let errorCount = 0;

  // Stream the blob in chunks — never materialise the full text
  const reader = blob.stream().getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    // Keep the last (potentially incomplete) chunk in the buffer
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      try {
        const resource = JSON.parse(trimmed) as Record<string, unknown>;
        const type = (resource.resourceType as string) ?? "Unknown";
        counts[type] = (counts[type] ?? 0) + 1;
        totalResources++;

        // Count error markers from failed processing
        if ("error" in resource) {
          errorCount++;
          continue;
        }

        // Per-type PII sample — keep originals (with manifest tags)
        piiSampleCount[type] = (piiSampleCount[type] ?? 0) + 1;
        if (piiSampleCount[type] <= PII_SAMPLE_PER_TYPE) piiSample.push(resource);

        // Only extract fields for the first FIELD_SAMPLE_LIMIT resources per type
        fieldSampleCount[type] = (fieldSampleCount[type] ?? 0) + 1;
        if (fieldSampleCount[type] <= FIELD_SAMPLE_LIMIT) {
          if (!allFieldCounts[type]) allFieldCounts[type] = {};
          const fc = allFieldCounts[type];
          for (const { field } of extractFieldsDeep(resource)) {
            if (field !== "resourceType") fc[field] = (fc[field] ?? 0) + 1;
          }
        }
      } catch {
        // skip malformed lines
      }
    }
  }

  // Flush remaining buffer
  if (buffer.trim()) {
    try {
      const resource = JSON.parse(buffer.trim()) as Record<string, unknown>;
      const type = (resource.resourceType as string) ?? "Unknown";
      counts[type] = (counts[type] ?? 0) + 1;
      totalResources++;
      if ("error" in resource) {
        errorCount++;
      } else {
        piiSampleCount[type] = (piiSampleCount[type] ?? 0) + 1;
        if (piiSampleCount[type] <= PII_SAMPLE_PER_TYPE) piiSample.push(resource);
        fieldSampleCount[type] = (fieldSampleCount[type] ?? 0) + 1;
        if (fieldSampleCount[type] <= FIELD_SAMPLE_LIMIT) {
          if (!allFieldCounts[type]) allFieldCounts[type] = {};
          const fc = allFieldCounts[type];
          for (const { field } of extractFieldsDeep(resource)) {
            if (field !== "resourceType") fc[field] = (fc[field] ?? 0) + 1;
          }
        }
      }
    } catch {
      // skip malformed
    }
  }

  return { counts, allFieldCounts, piiSample, totalResources, errorCount };
}

// ---------------------------------------------------------------------------
// Export format helpers
// ---------------------------------------------------------------------------

export type BulkFormat = "ndjson" | "bundle" | "json-array" | "patient-bundles";

export const FORMAT_OPTIONS: { value: BulkFormat; label: string; description: string }[] = [
  { value: "ndjson",          label: "NDJSON",          description: "One resource per line" },
  { value: "bundle",          label: "FHIR Bundle",      description: "All resources in one Bundle" },
  { value: "json-array",      label: "JSON Array",       description: "JSON array of resources" },
  { value: "patient-bundles", label: "Patient Bundles",  description: "One Bundle per patient (NDJSON)" },
];

export function getPatientRef(r: Record<string, unknown>): string | null {
  for (const field of ["subject", "patient", "beneficiary", "member"]) {
    const ref = (r[field] as Record<string, unknown> | undefined)?.reference;
    if (typeof ref === "string" && ref.startsWith("Patient/")) return ref;
  }
  return null;
}

export function buildPatientBundlesNdjson(resources: Record<string, unknown>[]): string {
  const patientMap = new Map<string, Record<string, unknown>[]>();
  const unlinked: Record<string, unknown>[] = [];

  for (const r of resources) {
    if (r.resourceType === "Patient") {
      const key = `Patient/${String(r.id ?? "unknown")}`;
      if (!patientMap.has(key)) patientMap.set(key, []);
      patientMap.get(key)!.unshift(r); // patient first in its own bundle
    } else {
      const ref = getPatientRef(r);
      if (ref) {
        if (!patientMap.has(ref)) patientMap.set(ref, []);
        patientMap.get(ref)!.push(r);
      } else {
        unlinked.push(r);
      }
    }
  }

  const lines: string[] = [];
  for (const entries of patientMap.values()) {
    lines.push(JSON.stringify({
      resourceType: "Bundle", id: crypto.randomUUID(), type: "collection",
      timestamp: new Date().toISOString(), total: entries.length,
      entry: entries.map((resource) => ({ resource })),
    }));
  }
  if (unlinked.length > 0) {
    lines.push(JSON.stringify({
      resourceType: "Bundle", id: crypto.randomUUID(), type: "collection",
      timestamp: new Date().toISOString(), total: unlinked.length,
      entry: unlinked.map((resource) => ({ resource })),
    }));
  }
  return lines.join("\n");
}

export function triggerDownload(content: string, filename: string, mime: string) {
  const blob = new Blob([content], { type: mime });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

export function formatElapsed(ms: number): string {
  const sec = Math.floor(ms / 1000);
  if (sec < 60) return `${sec}s`;
  const min = Math.floor(sec / 60);
  const remSec = sec % 60;
  return `${min}m ${remSec}s`;
}

