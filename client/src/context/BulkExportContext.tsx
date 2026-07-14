import {
  createContext,
  useContext,
  useCallback,
  useRef,
  useState,
  useEffect,
} from "react";
import type { ReactNode } from "react";
import { getJobStatus, getJobResult, cancelJob as cancelJobApi, reprocessJob as reprocessJobApi, listJobs } from "@/api/medanon";
import type { JobResponse, JobScoreResponse, UploadErrorDetail } from "@/api/medanon";
import { useAuth } from "@/context/AuthContext";

export type ExportJobStatus = "submitting" | "pending" | "running" | "done" | "error" | "cancelled";
export type ExportJobPhase = "queued" | "fetching" | "processing" | "loading" | "uploading" | "done" | string;

export interface ExportJobMeta {
  source: "all" | "condition" | "patient" | "patients";
  conditionName?: string;
  patientName?: string;
  patientCount?: number;
  configProfile: string;
  type?: string;
}

export interface ExportJob {
  id: string;
  jobId: string | null;
  label: string;
  filename: string;
  type: string;
  status: ExportJobStatus;
  phase: ExportJobPhase;
  error: string | null;
  processed: number;
  stagedCount: number | null;
  startedAt: number;
  completedAt: number | null;
  source: "all" | "condition" | "patient" | "patients";
  conditionName?: string;
  patientName?: string;
  patientCount?: number;
  configProfile: string;
  backendScore: JobScoreResponse | null;
  uploadErrors?: number;
  uploadErrorDetails?: UploadErrorDetail[];
  summary?: JobResponse["summary"];
}

interface BulkExportContextValue {
  jobs: ExportJob[];
  submitExport: (
    label: string,
    filename: string,
    onSubmit: () => Promise<JobResponse>,
    meta: ExportJobMeta,
  ) => string;
  downloadResult: (id: string) => Promise<void>;
  cancelJob: (id: string) => Promise<void>;
  reprocessJob: (id: string, configProfile: string) => string;
  dismissJob: (id: string) => void;
  clearCompleted: () => void;
  setJobBackendScore: (id: string, score: JobScoreResponse) => void;
}

const BulkExportContext = createContext<BulkExportContextValue | null>(null);

export function useBulkExport() {
  const ctx = useContext(BulkExportContext);
  if (!ctx)
    throw new Error("useBulkExport must be used within BulkExportProvider");
  return ctx;
}

const DISMISSED_JOBS_KEY = "medanon_dismissed_jobs";

function loadDismissedIds(): Set<string> {
  try {
    const raw = localStorage.getItem(DISMISSED_JOBS_KEY);
    return raw ? new Set(JSON.parse(raw) as string[]) : new Set();
  } catch {
    return new Set();
  }
}

function saveDismissedIds(ids: Set<string>): void {
  try {
    localStorage.setItem(DISMISSED_JOBS_KEY, JSON.stringify([...ids]));
  } catch { /* quota exceeded, best effort */ }
}

const TYPE_LABELS: Record<string, string> = {
  "bulk-export": "Bulk Export",
  "cohort": "Cohort Export",
  "patient-export": "Patient Export",
  "batch-patient-export": "Batch Patient Export",
  "bulk-import": "Bulk Import",
  "reprocess": "Re-process",
};

function jobResponseToExportJob(jr: JobResponse): ExportJob {
  const label = `${TYPE_LABELS[jr.type] ?? jr.type} ${jr.job_id.slice(0, 8)}`;
  const sourceMap: Record<string, ExportJob["source"]> = {
    "cohort": "condition",
    "patient-export": "patient",
    "batch-patient-export": "patients",
  };
  const isTerminal = jr.status === "done" || jr.status === "error" || jr.status === "cancelled";
  return {
    id: `recovered-${jr.job_id}`,
    jobId: jr.job_id,
    label,
    filename: `job_${jr.job_id}.ndjson`,
    type: jr.type,
    status: jr.status as ExportJobStatus,
    phase: jr.phase as ExportJobPhase,
    error: jr.error,
    processed: jr.processed,
    stagedCount: jr.staged_count,
    startedAt: new Date(jr.created_at).getTime(),
    completedAt: isTerminal ? new Date(jr.updated_at).getTime() : null,
    source: sourceMap[jr.type] ?? "all",
    configProfile: jr.config_profile ?? jr.summary?.config_profile ?? "auto",
    backendScore: null,
    uploadErrors: jr.upload_errors,
    uploadErrorDetails: jr.upload_error_details,
    summary: jr.summary,
  };
}

export function BulkExportProvider({ children }: { children: ReactNode }) {
  const { loading: authLoading, isAuthenticated } = useAuth();
  const [jobs, setJobs] = useState<ExportJob[]>([]);
  const nextIdRef = useRef(1);
  // Stores timeout handles (not intervals) so we can cancel scheduled polls.
  const pollRefs = useRef<Map<string, ReturnType<typeof setTimeout>>>(
    new Map(),
  );
  const dismissedIds = useRef<Set<string>>(loadDismissedIds());
  // Tracks how long (ms) each job has been polling so we can apply backoff.
  const pollElapsedRef = useRef<Map<string, number>>(new Map());

  const stopPolling = useCallback((id: string) => {
    const timer = pollRefs.current.get(id);
    if (timer) {
      clearTimeout(timer);
      pollRefs.current.delete(id);
    }
    pollElapsedRef.current.delete(id);
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    const refs = pollRefs.current;
    const elapsed = pollElapsedRef.current;
    return () => {
      for (const [, timer] of refs) clearTimeout(timer);
      refs.clear();
      elapsed.clear();
    };
  }, []);

  const updateJob = useCallback(
    (id: string, patch: Partial<ExportJob>) =>
      setJobs((prev) =>
        prev.map((j) => (j.id === id ? { ...j, ...patch } : j)),
      ),
    [],
  );

  const pollFailures = useRef<Map<string, number>>(new Map());

  // Poll interval grows with elapsed time to reduce unnecessary requests for
  // long-running jobs: 3s → 5s (after 30s) → 10s (after 2min) → 30s (after 5min).
  function _nextPollDelay(elapsedMs: number): number {
    if (elapsedMs < 30_000) return 3_000;
    if (elapsedMs < 120_000) return 5_000;
    if (elapsedMs < 300_000) return 10_000;
    return 30_000;
  }

  const startPolling = useCallback(
    (id: string, jobId: string) => {
      pollFailures.current.set(id, 0);
      pollElapsedRef.current.set(id, 0);

      const scheduleNext = (delayMs: number) => {
        const handle = setTimeout(async () => {
          const elapsed = (pollElapsedRef.current.get(id) ?? 0) + delayMs;
          pollElapsedRef.current.set(id, elapsed);

          try {
            const job = await getJobStatus(jobId);
            pollFailures.current.set(id, 0);
            if (job.status === "done") {
              stopPolling(id);
              updateJob(id, { status: "done", phase: "done", processed: job.processed, stagedCount: job.staged_count, completedAt: new Date(job.updated_at).getTime(), uploadErrors: job.upload_errors, uploadErrorDetails: job.upload_error_details });
            } else if (job.status === "error") {
              stopPolling(id);
              updateJob(id, {
                status: "error",
                error: job.error ?? "Export failed",
                processed: job.processed,
                phase: job.phase,
                stagedCount: job.staged_count,
                completedAt: new Date(job.updated_at).getTime(),
              });
            } else if (job.status === "cancelled") {
              stopPolling(id);
              updateJob(id, { status: "cancelled", phase: job.phase, processed: job.processed, stagedCount: job.staged_count, completedAt: new Date(job.updated_at).getTime() });
            } else {
              updateJob(id, {
                status: job.status as ExportJobStatus,
                processed: job.processed,
                phase: job.phase as ExportJobPhase,
                stagedCount: job.staged_count,
              });
              // Still running, schedule next poll with backoff
              if (pollRefs.current.has(id)) {
                scheduleNext(_nextPollDelay(elapsed));
              }
            }
          } catch (err) {
            const failures = (pollFailures.current.get(id) ?? 0) + 1;
            pollFailures.current.set(id, failures);
            if (failures >= 3) {
              stopPolling(id);
              updateJob(id, {
                status: "error",
                error: err instanceof Error ? err.message : "Failed to check job status",
                completedAt: Date.now(),
              });
            } else if (pollRefs.current.has(id)) {
              scheduleNext(_nextPollDelay(elapsed));
            }
          }
        }, delayMs);
        pollRefs.current.set(id, handle);
      };

      scheduleNext(_nextPollDelay(0));
    },
    [stopPolling, updateJob],
  );

  // Recover jobs from backend once auth has settled. BulkExportProvider mounts
  // as a child of AuthProvider, which renders children immediately without
  // waiting for its async bootstrap (fetch auth config -> restore OIDC session
  // from localStorage -> publish the access token). Firing this on a bare `[]`
  // mount effect races that bootstrap: the request goes out with no bearer
  // token yet and the backend correctly 401s. Gating on `authLoading` avoids
  // the race, and re-running when `isAuthenticated` flips true also recovers
  // jobs right after an OIDC login.
  useEffect(() => {
    if (authLoading || !isAuthenticated) return;
    let cancelled = false;
    (async () => {
      try {
        const backendJobs = await listJobs({ limit: 50 });
        if (cancelled) return;

        // backendJobs arrives DESC (newest first). Reverse to ASC so recovered
        // jobs match the normal append-based state order: the grid does
        // [...jobs].reverse() for display, and auto-select uses
        // jobs[jobs.length-1], both expect oldest-first state.
        const sortedAsc = [...backendJobs].reverse();

        // Build candidates outside the updater to avoid StrictMode double-run.
        const EXPORT_JOB_TYPES = new Set([
          "bulk-export", "cohort", "patient-export", "batch-patient-export", "reprocess", "bulk-import",
        ]);
        const dismissed = dismissedIds.current;
        const candidates: ExportJob[] = sortedAsc
          .filter((jr) => !dismissed.has(jr.job_id) && EXPORT_JOB_TYPES.has(jr.type))
          .map(jobResponseToExportJob);
        const activeIds: { localId: string; serverId: string }[] = sortedAsc
          .filter((jr) => !dismissed.has(jr.job_id) && EXPORT_JOB_TYPES.has(jr.type) && (jr.status === "pending" || jr.status === "running"))
          .map((jr) => ({ localId: `recovered-${jr.job_id}`, serverId: jr.job_id }));

        // Merge into state, skipping jobs already tracked (dedup by jobId).
        setJobs((prev) => {
          const known = new Set(prev.map((j) => j.jobId));
          const fresh = candidates.filter((c) => !known.has(c.jobId));
          return fresh.length === 0 ? prev : [...prev, ...fresh];
        });

        // Resume polling for active recovered jobs (guarded against double-start).
        for (const { localId, serverId } of activeIds) {
          if (!pollRefs.current.has(localId)) {
            startPolling(localId, serverId);
          }
        }
      } catch (err) {
        console.warn("Failed to recover jobs from backend:", err);
      }
    })();
    return () => { cancelled = true; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [authLoading, isAuthenticated]);

  const submitExport = useCallback(
    (
      label: string,
      filename: string,
      onSubmit: () => Promise<JobResponse>,
      meta: ExportJobMeta,
    ): string => {
      const id = `export-${nextIdRef.current++}`;
      const newJob: ExportJob = {
        id,
        jobId: null,
        label,
        filename,
        type: meta.type ?? "bulk-export",
        status: "submitting",
        phase: "queued",
        error: null,
        processed: 0,
        stagedCount: null,
        startedAt: Date.now(),
        completedAt: null,
        source: meta.source,
        conditionName: meta.conditionName,
        patientName: meta.patientName,
        patientCount: meta.patientCount,
        configProfile: meta.configProfile,
        backendScore: null,
      };
      setJobs((prev) => [...prev, newJob]);

      onSubmit()
        .then((resp) => {
          updateJob(id, { jobId: resp.job_id, status: "pending" });
          startPolling(id, resp.job_id);
        })
        .catch((err) => {
          updateJob(id, {
            status: "error",
            completedAt: Date.now(),
            error:
              err instanceof Error
                ? err.message
                : "Failed to submit export",
          });
        });

      return id;
    },
    [updateJob, startPolling],
  );

  const downloadResult = useCallback(
    async (id: string) => {
      const job = jobs.find((j) => j.id === id);
      if (!job?.jobId) return;
      try {
        const blob = await getJobResult(job.jobId);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.download = job.filename;
        document.body.appendChild(a);
        a.click();
        document.body.removeChild(a);
        URL.revokeObjectURL(url);
      } catch (err) {
        updateJob(id, {
          error: err instanceof Error ? err.message : "Failed to download result",
        });
      }
    },
    [jobs, updateJob],
  );

  const cancelJob = useCallback(
    async (id: string) => {
      const job = jobs.find((j) => j.id === id);
      if (!job?.jobId) return;
      try {
        await cancelJobApi(job.jobId);
        stopPolling(id);
        updateJob(id, { status: "cancelled", completedAt: Date.now() });
      } catch (err) {
        updateJob(id, {
          status: "error",
          error: err instanceof Error ? err.message : "Failed to cancel job",
          completedAt: Date.now(),
        });
      }
    },
    [jobs, stopPolling, updateJob],
  );

  const reprocessJob = useCallback(
    (id: string, configProfile: string): string => {
      const job = jobs.find((j) => j.id === id);
      if (!job?.jobId) return "";
      const sourceJobId = job.jobId;
      const newId = `export-${nextIdRef.current++}`;
      const newJob: ExportJob = {
        id: newId,
        jobId: null,
        label: `Re-process: ${job.label}`,
        filename: job.filename.replace(/\.ndjson$/, `.reprocess.ndjson`),
        type: "reprocess",
        status: "submitting",
        phase: "queued",
        error: null,
        processed: 0,
        stagedCount: null,
        startedAt: Date.now(),
        completedAt: null,
        source: job.source,
        conditionName: job.conditionName,
        patientName: job.patientName,
        configProfile,
        backendScore: null,
      };
      setJobs((prev) => [...prev, newJob]);

      reprocessJobApi(sourceJobId, configProfile)
        .then((resp) => {
          updateJob(newId, { jobId: resp.job_id, status: "pending" });
          startPolling(newId, resp.job_id);
        })
        .catch((err) => {
          updateJob(newId, {
            status: "error",
            completedAt: Date.now(),
            error: err instanceof Error ? err.message : "Failed to submit reprocess",
          });
        });

      return newId;
    },
    [jobs, updateJob, startPolling],
  );

  const dismissJob = useCallback(
    (id: string) => {
      stopPolling(id);
      // Find the backend jobId before removing from state
      setJobs((prev) => {
        const job = prev.find((j) => j.id === id);
        if (job?.jobId) {
          dismissedIds.current.add(job.jobId);
          saveDismissedIds(dismissedIds.current);
        }
        return prev.filter((j) => j.id !== id);
      });
    },
    [stopPolling],
  );

  const clearCompleted = useCallback(() => {
    setJobs((prev) => {
      for (const j of prev) {
        if (j.status === "done" && j.jobId) {
          dismissedIds.current.add(j.jobId);
        }
      }
      saveDismissedIds(dismissedIds.current);
      return prev.filter((j) => j.status !== "done");
    });
  }, []);

  const setJobBackendScore = useCallback(
    (id: string, score: JobScoreResponse) => updateJob(id, { backendScore: score }),
    [updateJob],
  );

  return (
    <BulkExportContext.Provider
      value={{ jobs, submitExport, downloadResult, cancelJob, reprocessJob, dismissJob, clearCompleted, setJobBackendScore }}
    >
      {children}
    </BulkExportContext.Provider>
  );
}
