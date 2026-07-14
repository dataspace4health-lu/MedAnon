/**
 * SQL source de-identification API, saved connections, schema inspection, and
 * async table export.  Mirrors the backend endpoints under /v1:
 *
 *   GET    /v1/sql-connections
 *   POST   /v1/sql-connections
 *   POST   /v1/sql-connections/{id}/test
 *   DELETE /v1/sql-connections/{id}
 *   POST   /v1/process/sql/inspect
 *   POST   /v1/jobs/sql-export
 *
 * Connections are read-only and host allow-listed server-side; passwords are
 * encrypted at rest and never returned by the API.
 */

import { fetchApi } from "./client";

/** A table-scoped column rule authored in the SQL schema explorer. */
export interface SqlColumnRule {
  table: string;
  column: string;
  action: string;
  params?: Record<string, unknown>;
}

export interface SqlConnection {
  id: string;
  name: string;
  host: string;
  port: number;
  dbname: string;
  username: string;
  sslmode: string;
  created_at: string;
}

export interface SqlConnectionCreate {
  name: string;
  host: string;
  port: number;
  dbname: string;
  username: string;
  password: string;
  sslmode?: string;
}

/** A column within a reflected table, same explorer vocabulary as TabularColumn. */
export interface SqlColumn {
  name: string;
  data_type: string;
  samples: string[];
  recommended_action?: string;
}

export interface SqlTable {
  name: string;
  row_estimate: number;
  columns: SqlColumn[];
}

export interface SqlSchemaPreview {
  schema: string;
  tables: SqlTable[];
}

export async function listSqlConnections(): Promise<SqlConnection[]> {
  const { connections } = await fetchApi<{ connections: SqlConnection[] }>(
    "/v1/sql-connections",
  );
  return connections;
}

export async function createSqlConnection(
  body: SqlConnectionCreate,
): Promise<SqlConnection> {
  return fetchApi<SqlConnection>("/v1/sql-connections", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function testSqlConnection(
  id: string,
): Promise<{ ok: boolean; server: string }> {
  return fetchApi<{ ok: boolean; server: string }>(
    `/v1/sql-connections/${encodeURIComponent(id)}/test`,
    { method: "POST" },
  );
}

export async function deleteSqlConnection(id: string): Promise<void> {
  await fetchApi<void>(`/v1/sql-connections/${encodeURIComponent(id)}`, {
    method: "DELETE",
  });
}

export async function inspectSql(
  connectionId: string,
  schema = "public",
): Promise<SqlSchemaPreview> {
  return fetchApi<SqlSchemaPreview>("/v1/process/sql/inspect", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ connection_id: connectionId, schema }),
  });
}

export type SqlOutputFormat = "csv" | "ndjson" | "parquet";

export interface SqlExportJob {
  job_id: string;
  status: string;
}

/**
 * Submit an async sql-export job. ``rules`` are table-scoped column rules
 * (serialized as match: "table:<t>/column:<c>") so a single job can map many
 * tables unambiguously; when omitted the saved ``configProfile`` is used.
 * Returns the job id (track + download the ZIP on the Jobs page).
 */
export async function submitSqlExport(opts: {
  connectionId: string;
  tables: string[];
  schema?: string;
  outputFormat: SqlOutputFormat;
  rules?: SqlColumnRule[];
  configProfile?: string;
}): Promise<SqlExportJob> {
  const body: Record<string, unknown> = {
    connection_id: opts.connectionId,
    tables: opts.tables,
    schema: opts.schema ?? "public",
    output_format: opts.outputFormat,
  };
  if (opts.rules && opts.rules.length > 0) {
    body.rules = opts.rules.map((r) => ({
      match: `table:${r.table}/column:${r.column}`,
      action: r.action,
      ...(r.params ? { params: r.params } : {}),
    }));
  } else if (opts.configProfile) {
    body.config_profile = opts.configProfile;
  }
  return fetchApi<SqlExportJob>("/v1/jobs/sql-export", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}
