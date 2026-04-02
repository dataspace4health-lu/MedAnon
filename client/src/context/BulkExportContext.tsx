import {
  createContext,
  useContext,
  useCallback,
  useRef,
  useState,
  useEffect,
} from "react";
import type { ReactNode } from "react";
import { getJobStatus, getJobResult, cancelJob as cancelJobApi, reprocessJob as reprocessJobApi } from "@/api/medanon";
import type { JobResponse } from "@/api/medanon";

export type ExportJobStatus = "submitting" | "pending" | "running" | "done" | "error" | "cancelled";
export type ExportJobPhase = "queued" | "fetching" | "processing" | "done";

export interface ExportJobMeta {
  source: "all" | "condition" | "patient";
  conditionName?: string;
  patientName?: string;
  configProfile: string;
}

export interface ExportJob {
  id: string;
  jobId: string | null;
  label: string;
  filename: string;
  status: ExportJobStatus;
  phase: ExportJobPhase;
  error: string | null;
  processed: number;
  stagedCount: number | null;
  startedAt: number;
  source: "all" | "condition" | "patient";
  conditionName?: string;
  patientName?: string;
  configProfile: string;
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
}

const BulkExportContext = createContext<BulkExportContextValue | null>(null);

export function useBulkExport() {
  const ctx = useContext(BulkExportContext);
  if (!ctx)
    throw new Error("useBulkExport must be used within BulkExportProvider");
  return ctx;
}

let nextId = 1;

export function BulkExportProvider({ children }: { children: ReactNode }) {
  const [jobs, setJobs] = useState<ExportJob[]>([]);
  const pollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(
    new Map(),
  );

  const stopPolling = useCallback((id: string) => {
    const timer = pollRefs.current.get(id);
    if (timer) {
      clearInterval(timer);
      pollRefs.current.delete(id);
    }
  }, []);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      for (const [, timer] of pollRefs.current) clearInterval(timer);
      pollRefs.current.clear();
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

  const startPolling = useCallback(
    (id: string, jobId: string) => {
      pollFailures.current.set(id, 0);
      const timer = setInterval(async () => {
        try {
          const job = await getJobStatus(jobId);
          // Reset failure count on success
          pollFailures.current.set(id, 0);
          if (job.status === "done") {
            stopPolling(id);
            pollFailures.current.delete(id);
            updateJob(id, { status: "done", phase: "done", processed: job.processed, stagedCount: job.staged_count });
          } else if (job.status === "error") {
            stopPolling(id);
            pollFailures.current.delete(id);
            updateJob(id, {
              status: "error",
              error: job.error ?? "Export failed",
              processed: job.processed,
              phase: job.phase,
              stagedCount: job.staged_count,
            });
          } else if (job.status === "cancelled") {
            stopPolling(id);
            pollFailures.current.delete(id);
            updateJob(id, { status: "cancelled", phase: job.phase, processed: job.processed, stagedCount: job.staged_count });
          } else {
            updateJob(id, {
              status: job.status as ExportJobStatus,
              processed: job.processed,
              phase: job.phase as ExportJobPhase,
              stagedCount: job.staged_count,
            });
          }
        } catch (err) {
          // Tolerate up to 3 consecutive poll failures before marking as error
          const failures = (pollFailures.current.get(id) ?? 0) + 1;
          pollFailures.current.set(id, failures);
          if (failures >= 3) {
            stopPolling(id);
            pollFailures.current.delete(id);
            updateJob(id, {
              status: "error",
              error:
                err instanceof Error ? err.message : "Failed to check job status",
            });
          }
        }
      }, 3000);
      pollRefs.current.set(id, timer);
    },
    [stopPolling, updateJob],
  );

  const submitExport = useCallback(
    (
      label: string,
      filename: string,
      onSubmit: () => Promise<JobResponse>,
      meta: ExportJobMeta,
    ): string => {
      const id = `export-${nextId++}`;
      const newJob: ExportJob = {
        id,
        jobId: null,
        label,
        filename,
        status: "submitting",
        phase: "queued",
        error: null,
        processed: 0,
        stagedCount: null,
        startedAt: Date.now(),
        source: meta.source,
        conditionName: meta.conditionName,
        patientName: meta.patientName,
        configProfile: meta.configProfile,
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
        updateJob(id, { status: "cancelled" });
      } catch (err) {
        updateJob(id, {
          status: "error",
          error: err instanceof Error ? err.message : "Failed to cancel job",
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
      const newId = `export-${nextId++}`;
      const newJob: ExportJob = {
        id: newId,
        jobId: null,
        label: `Re-process: ${job.label}`,
        filename: job.filename.replace(/\.ndjson$/, `.reprocess.ndjson`),
        status: "submitting",
        phase: "queued",
        error: null,
        processed: 0,
        stagedCount: null,
        startedAt: Date.now(),
        source: job.source,
        conditionName: job.conditionName,
        patientName: job.patientName,
        configProfile,
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
      setJobs((prev) => prev.filter((j) => j.id !== id));
    },
    [stopPolling],
  );

  const clearCompleted = useCallback(() => {
    setJobs((prev) => prev.filter((j) => j.status !== "done"));
  }, []);

  return (
    <BulkExportContext.Provider
      value={{ jobs, submitExport, downloadResult, cancelJob, reprocessJob, dismissJob, clearCompleted }}
    >
      {children}
    </BulkExportContext.Provider>
  );
}
