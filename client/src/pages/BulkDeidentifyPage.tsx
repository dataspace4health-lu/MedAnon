import { useState, useEffect, useRef } from "react";
import { useNavigate, useLocation } from "react-router-dom";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Inbox } from "lucide-react";
import { useBulkExport } from "@/context/BulkExportContext";
import { JobCard } from "./bulk-deidentify/JobCard.tsx";
import { JobDetailPanel } from "./bulk-deidentify/JobDetailPanel.tsx";

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function BulkDeidentifyPage() {
  const navigate = useNavigate();
  const location = useLocation();
  const autoSelectId = (location.state as { autoSelectId?: string } | null)?.autoSelectId;
  const { jobs } = useBulkExport();

  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Auto-select on arrival or when a new job appears.
  // didAutoSelect guards against autoSelectId (from location.state) overriding
  // the user's explicit card click on every subsequent poll cycle.
  const prevJobCount = useRef(jobs.length);
  const didAutoSelect = useRef(false);
  useEffect(() => {
    if (!didAutoSelect.current && autoSelectId && jobs.some((j) => j.id === autoSelectId)) {
      setSelectedId(autoSelectId);
      didAutoSelect.current = true;
    } else if (jobs.length > prevJobCount.current) {
      setSelectedId(jobs[jobs.length - 1].id);
    } else if (selectedId === null && jobs.length > 0) {
      setSelectedId(jobs[jobs.length - 1].id);
    }
    prevJobCount.current = jobs.length;
  }, [autoSelectId, jobs, selectedId]);

  // If selected job was dismissed, clear selection
  const selectedJob = jobs.find((j) => j.id === selectedId) ?? null;
  useEffect(() => {
    if (selectedId && !selectedJob && jobs.length > 0) {
      setSelectedId(jobs[jobs.length - 1].id);
    }
  }, [selectedId, selectedJob, jobs]);

  return (
    <div>
      <PageHeader
        title="Bulk De-identification"
        description="View and manage all bulk de-identification jobs"
      />

      {jobs.length === 0 ? (
        /* Empty state */
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16 text-center text-muted-foreground">
          <Inbox className="mb-3 size-10" />
          <p className="text-sm font-medium">No bulk de-identification jobs</p>
          <p className="mt-1 max-w-sm text-xs">
            Start a bulk export from the Patient Browser or Condition Browser to see jobs here.
          </p>
          <div className="mt-4 flex gap-2">
            <Button variant="outline" size="sm" onClick={() => navigate("/patients")}>
              Patient Browser
            </Button>
            <Button variant="outline" size="sm" onClick={() => navigate("/conditions")}>
              Condition Browser
            </Button>
          </div>
        </div>
      ) : (
        <>
          {/* Job card grid */}
          <div className="mb-6 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {[...jobs].reverse().map((job) => (
              <JobCard
                key={job.id}
                job={job}
                isSelected={job.id === selectedId}
                onSelect={() => setSelectedId(job.id)}
              />
            ))}
          </div>

          {/* Detail panel */}
          {selectedJob && (
            <div className="space-y-4">
              <h2 className="text-sm font-semibold uppercase tracking-wide text-muted-foreground">
                Job Details
              </h2>
              <JobDetailPanel key={selectedJob.id} job={selectedJob} />
            </div>
          )}
        </>
      )}
    </div>
  );
}
