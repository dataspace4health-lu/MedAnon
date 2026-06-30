/**
 * Trust Gate connector API: file upload and SQL database assessment.
 * All endpoints proxy through nginx /trust/ → trust-gate:8400.
 */
import type { QualityPassport } from "./trustGate";

const BASE = "/trust";

export type SqlDriver = "postgresql" | "mysql" | "sqlite";

export interface SqlConnectParams {
  driver: SqlDriver;
  host?: string;
  port?: number;
  database: string;
  username?: string;
  password?: string;
}

export interface SqlAssessParams extends SqlConnectParams {
  query: string;
  tableName?: string;
  mapping?: Record<string, Record<string, string>>;
  datasetId?: string;
  providerId?: string;
  configProfile?: string;
  intendedUse?: string;
  useCase?: string;
}

export interface FileAssessParams {
  file: File;
  datasetId?: string;
  providerId?: string;
  tableName?: string;
  sheet?: string;
  mapping?: Record<string, Record<string, string>>;
  configProfile?: string;
  intendedUse?: string;
  useCase?: string;
}

// ---------------------------------------------------------------------------
// File connector
// ---------------------------------------------------------------------------

export async function assessFile(params: FileAssessParams): Promise<QualityPassport> {
  const fd = new FormData();
  fd.append("file", params.file, params.file.name);
  if (params.datasetId) fd.append("dataset_id", params.datasetId);
  if (params.providerId) fd.append("provider_id", params.providerId);
  if (params.tableName) fd.append("table_name", params.tableName);
  if (params.sheet) fd.append("sheet", params.sheet);
  if (params.mapping) fd.append("mapping", JSON.stringify(params.mapping));
  if (params.configProfile) fd.append("config_profile", params.configProfile);
  if (params.intendedUse) fd.append("intended_use", params.intendedUse);
  if (params.useCase) fd.append("use_case", params.useCase);

  const res = await fetch(`${BASE}/v1/trust/connectors/file`, {
    method: "POST",
    body: fd,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? res.statusText);
  }
  return res.json() as Promise<QualityPassport>;
}

// ---------------------------------------------------------------------------
// SQL connector
// ---------------------------------------------------------------------------

export async function assessSql(params: SqlAssessParams): Promise<QualityPassport> {
  const body = {
    driver: params.driver,
    host: params.host ?? "",
    port: params.port ?? null,
    database: params.database,
    username: params.username ?? "",
    password: params.password ?? "",
    query: params.query,
    table_name: params.tableName ?? null,
    mapping: params.mapping ?? null,
    dataset_id: params.datasetId ?? null,
    provider_id: params.providerId ?? null,
    config_profile: params.configProfile ?? null,
    intended_use: params.intendedUse ?? null,
    use_case: params.useCase ?? null,
  };
  const res = await fetch(`${BASE}/v1/trust/connectors/sql`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? res.statusText);
  }
  return res.json() as Promise<QualityPassport>;
}

export async function sqlListTables(params: SqlConnectParams): Promise<string[]> {
  const body = {
    driver: params.driver,
    host: params.host ?? "",
    port: params.port ?? null,
    database: params.database,
    username: params.username ?? "",
    password: params.password ?? "",
  };
  const res = await fetch(`${BASE}/v1/trust/connectors/sql/tables`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(detail.detail ?? res.statusText);
  }
  const data = await res.json();
  return (data.tables ?? []) as string[];
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Detect a format label from a File object for display purposes only. */
export function fileFormatLabel(file: File): string {
  const name = file.name.toLowerCase();
  if (name.endsWith(".xlsx") || name.endsWith(".xls")) return "Excel";
  if (name.endsWith(".csv")) return "CSV";
  if (name.endsWith(".tsv")) return "TSV";
  if (name.endsWith(".ndjson") || name.endsWith(".jsonl")) return "NDJSON";
  if (name.endsWith(".json")) return "JSON";
  return "Auto-detect";
}
