import { Badge } from "@/components/ui/badge";
import {
  Loader2, AlertCircle, CheckCircle2, XCircle,
} from "lucide-react";
import { extractFieldsDeep } from "@/lib/fhirFields";
import { stripManifestTag } from "@/lib/piiDetection";
import type { ExportJobStatus } from "@/context/BulkExportContext";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export const PHASE_LABELS: Record<string, string> = {
  queued: "Queued",
  fetching: "Fetching from FHIR server",
  processing: "De-identifying resources",
  done: "Complete",
};

export async function parseNdjsonBlob(blob: Blob): Promise<{
  counts: Record<string, number>;
  allFieldCounts: Record<string, Record<string, number>>;
  piiSample: Record<string, unknown>[];
  allResources: Record<string, unknown>[];
  rawNdjson: string;
}> {
  const text = await blob.text();
  const counts: Record<string, number> = {};
  const allFieldCounts: Record<string, Record<string, number>> = {};
  const allResources: Record<string, unknown>[] = [];
  const rawLines: string[] = [];
  const piiSample: Record<string, unknown>[] = [];
  // PII sample cap — keep originals (with manifest) for accurate action detection
  const PII_SAMPLE_LIMIT = 200;
  // Cap expensive deep-field walk per type — schema is consistent after ~200 resources
  const FIELD_SAMPLE_LIMIT = 200;
  const fieldSampleCount: Record<string, number> = {};

  for (const line of text.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed) continue;
    try {
      const resource = JSON.parse(trimmed) as Record<string, unknown>;
      const type = (resource.resourceType as string) ?? "Unknown";
      counts[type] = (counts[type] ?? 0) + 1;

      // Keep original (with manifest tags) for PII detection
      if (piiSample.length < PII_SAMPLE_LIMIT) piiSample.push(resource);

      // Strip manifest and pre-serialize for download
      const cleaned = stripManifestTag(resource);
      allResources.push(cleaned);
      rawLines.push(JSON.stringify(cleaned));

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
  return { counts, allFieldCounts, piiSample, allResources, rawNdjson: rawLines.join("\n") };
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

// ---------------------------------------------------------------------------
// Small UI helpers
// ---------------------------------------------------------------------------

export function StatusIcon({ status }: { status: ExportJobStatus }) {
  switch (status) {
    case "submitting":
    case "pending":
    case "running":
      return <Loader2 className="size-4 animate-spin text-primary shrink-0" />;
    case "done":
      return <CheckCircle2 className="size-4 text-green-600 shrink-0" />;
    case "cancelled":
      return <XCircle className="size-4 text-muted-foreground shrink-0" />;
    case "error":
      return <AlertCircle className="size-4 text-destructive shrink-0" />;
  }
}

export function statusBadge(status: string) {
  switch (status) {
    case "submitting":
    case "pending":
      return <Badge variant="secondary">Pending</Badge>;
    case "running":
      return <Badge className="bg-blue-100 text-blue-800 hover:bg-blue-100">Running</Badge>;
    case "done":
      return <Badge className="bg-green-100 text-green-800 hover:bg-green-100">Done</Badge>;
    case "cancelled":
      return <Badge variant="secondary">Cancelled</Badge>;
    case "error":
      return <Badge variant="destructive">Error</Badge>;
    default:
      return <Badge variant="secondary">{status}</Badge>;
  }
}
