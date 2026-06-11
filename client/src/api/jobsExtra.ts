import { getAuthHeaders } from './client';
import type { JobResponse } from './jobs';

export interface StagedStats {
  job_id: string;
  total: number;
  pending: number;
  processing: number;
  done: number;
  error: number;
}

async function apiCall<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, { headers: getAuthHeaders(), ...init });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(`${init?.method ?? 'GET'} ${url} failed (${res.status}): ${body?.detail ?? res.statusText}`);
  }
  return res.json() as Promise<T>;
}

export function listDeadJobs(): Promise<JobResponse[]> {
  return apiCall('/api/v1/jobs/dead');
}

export function requeueJob(jobId: string): Promise<JobResponse> {
  return apiCall(`/api/v1/jobs/${encodeURIComponent(jobId)}/requeue`, { method: 'POST' });
}

export function getStagedStats(jobId: string): Promise<StagedStats> {
  return apiCall(`/api/v1/jobs/${encodeURIComponent(jobId)}/staged-stats`);
}

/** @deprecated use getRunScoreReport — this calls the jobs endpoint which requires a job-store ID */
export function getJobScoreReport(jobId: string): Promise<{ report: string }> {
  return apiCall(`/api/v1/jobs/${encodeURIComponent(jobId)}/score/report`);
}

/** Fetch the Markdown audit report for a processing run from the DB. */
export async function getRunScoreReport(runId: string): Promise<string> {
  const res = await fetch(`/api/v1/processing-runs/${encodeURIComponent(runId)}/score/report`, {
    headers: getAuthHeaders(),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(`Score report unavailable (${res.status}): ${body?.detail ?? res.statusText}`);
  }
  return res.text();
}
