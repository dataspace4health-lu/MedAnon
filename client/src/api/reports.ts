/**
 * Transformation-passport reports (D7.2 §5.5.1 / EHDS Art 79).
 *
 * Durable, anonymous documentation bundles a risk-driven export produced:
 * privacy model + achieved k/l/t, tools, privacy-risk, and disclosure. Backed
 * by Postgres when the app DB is configured; the list is empty otherwise (the
 * per-job passport is still visible in Jobs Monitor from the job status).
 */

import { fetchApi } from "./client";
import type { TransformationPassport } from "./jobs";

export interface ReportIndexRow {
  job_id: string;
  permit_id: string | null;
  decision: "approve" | "refer" | "refuse" | null;
  created_at: string | null;
}

/** GET /api/v1/reports, recent passport index rows. */
export async function listReports(): Promise<ReportIndexRow[]> {
  return fetchApi<ReportIndexRow[]>("/v1/reports", { cache: "no-store" });
}

/** GET /api/v1/reports/:jobId, the full Transformation Passport. */
export async function getReport(jobId: string): Promise<TransformationPassport> {
  return fetchApi<TransformationPassport>(
    `/v1/reports/${encodeURIComponent(jobId)}`,
    { cache: "no-store" },
  );
}
