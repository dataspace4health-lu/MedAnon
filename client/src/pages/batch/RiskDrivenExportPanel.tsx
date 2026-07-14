import { useState, useEffect, useCallback, useRef } from "react";
import { toast } from "sonner";
import {
  Play,
  Loader2,
  ShieldCheck,
  Download,
  BadgeCheck,
  AlertTriangle,
  XCircle,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { PermitPicker } from "@/components/shared/PermitPicker";
import { TransformationPassportPanel } from "@/components/shared/TransformationPassportPanel";
import { useSettings } from "@/context/SettingsContext";
import { activeSourceJobParams } from "@/api/fhirRoute";
import {
  submitRiskDrivenExportJob,
  getJobStatus,
  getJobResult,
  type JobResponse,
} from "@/api/medanon";
import { JOB_POLL_INTERVAL } from "./batchHelpers.ts";

const DECISION: Record<
  string,
  { label: string; icon: typeof BadgeCheck; className: string }
> = {
  approve: {
    label: "Approved for release",
    icon: BadgeCheck,
    className: "bg-emerald-600 text-white hover:bg-emerald-600",
  },
  refer: {
    label: "Referred to output checker",
    icon: AlertTriangle,
    className: "bg-amber-500 text-white hover:bg-amber-500",
  },
  refuse: {
    label: "Refused",
    icon: XCircle,
    className: "bg-destructive text-white hover:bg-destructive",
  },
};

function toList(v: string): string[] {
  return v
    .split(/[\n,]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

export function RiskDrivenExportPanel({
  configProfile,
}: {
  configProfile: string;
}) {
  const [serverUrl, setServerUrl] = useState("");
  const [profile, setProfile] = useState(configProfile || "config_k_anonymity");
  const [permitId, setPermitId] = useState<string | null>(null);
  const [recipient, setRecipient] = useState("");
  const [declaredPaths, setDeclaredPaths] = useState("");
  const [optoutIds, setOptoutIds] = useState("");
  const [job, setJob] = useState<JobResponse | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [sourceId, setSourceId] = useState<string | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Prefill from the app-wide Active Source (Settings).
  const { activeSourceId, connections } = useSettings();
  useEffect(() => {
    const src = activeSourceJobParams(activeSourceId, connections);
    if (src.source_id) setSourceId(src.source_id);
    else if (src.server_url) setServerUrl(src.server_url);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeSourceId]);

  const stopPolling = useCallback(() => {
    if (pollRef.current !== null) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);
  useEffect(() => () => stopPolling(), [stopPolling]);

  const startPolling = useCallback(
    (jobId: string) => {
      stopPolling();
      pollRef.current = setInterval(async () => {
        try {
          const updated = await getJobStatus(jobId);
          setJob(updated);
          if (updated.status === "done" || updated.status === "error") {
            stopPolling();
            if (updated.status === "done") toast.success("Risk-driven export complete.");
            else toast.error("Export failed.", { description: updated.error ?? undefined });
          }
        } catch {
          stopPolling();
        }
      }, JOB_POLL_INTERVAL);
    },
    [stopPolling],
  );

  const running = submitting || job?.status === "running" || job?.status === "pending";

  const handleSubmit = useCallback(async () => {
    setSubmitting(true);
    try {
      const submitted = await submitRiskDrivenExportJob({
        server_url: serverUrl.trim() || undefined,
        source_id: sourceId ?? undefined,
        config_profile: profile.trim() || undefined,
        permit_id: permitId,
        recipient: recipient.trim() || null,
        declared_paths: declaredPaths.trim() ? toList(declaredPaths) : null,
        optout_ids: optoutIds.trim() ? toList(optoutIds) : null,
      });
      setJob(submitted);
      toast.success("Job queued.", { description: `ID: ${submitted.job_id}` });
      startPolling(submitted.job_id);
    } catch (err) {
      toast.error("Failed to submit job.", {
        description: err instanceof Error ? err.message : String(err),
      });
    } finally {
      setSubmitting(false);
    }
  }, [serverUrl, sourceId, profile, permitId, recipient, declaredPaths, optoutIds, startPolling]);

  const handleDownload = useCallback(async () => {
    if (!job) return;
    try {
      const blob = await getJobResult(job.job_id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = `${job.job_id}.ndjson`;
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Download failed");
    }
  }, [job]);

  const ap = job?.achieved_privacy;
  const disc = job?.disclosure;

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="pb-3">
          <CardTitle className="flex items-center gap-2 text-base">
            <ShieldCheck className="h-4 w-4 text-muted-foreground" />
            Risk-Driven (k-anonymity) Export
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            Fetches a cohort, drops opted-out subjects (Art 71), solves a
            generalisation lattice to the profile's target k/l/t, pseudonymises
            under the selected permit (§4.4), and runs a disclosure decision on
            the output before release (D7.2 §5.4).
          </p>

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-sm font-medium">
                FHIR Server URL{" "}
                <span className="font-normal text-muted-foreground">(optional)</span>
              </label>
              <Input
                placeholder="Leave blank for FHIR_SOURCE_URL"
                value={serverUrl}
                onChange={(e) => setServerUrl(e.target.value)}
                disabled={running}
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">
                Config profile{" "}
                <span className="font-normal text-muted-foreground">
                  (must carry a privacy_model)
                </span>
              </label>
              <Input
                placeholder="config_k_anonymity"
                value={profile}
                onChange={(e) => setProfile(e.target.value)}
                disabled={running}
              />
            </div>
          </div>

          <PermitPicker value={permitId} onChange={setPermitId} disabled={running} />

          <div className="grid gap-3 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-sm font-medium">
                Recipient{" "}
                <span className="font-normal text-muted-foreground">(optional)</span>
              </label>
              <Input
                placeholder="Checked against the permit"
                value={recipient}
                onChange={(e) => setRecipient(e.target.value)}
                disabled={running}
              />
            </div>
            <div>
              <label className="mb-1 block text-sm font-medium">
                Opt-out subject ids{" "}
                <span className="font-normal text-muted-foreground">
                  (Art 71, comma/line-separated)
                </span>
              </label>
              <Textarea
                rows={2}
                placeholder="NHS / MRN / patient ids"
                value={optoutIds}
                onChange={(e) => setOptoutIds(e.target.value)}
                disabled={running}
                className="text-xs"
              />
            </div>
          </div>

          <div>
            <label className="mb-1 block text-sm font-medium">
              Declared paths{" "}
              <span className="font-normal text-muted-foreground">
                (purpose limitation, optional)
              </span>
            </label>
            <Textarea
              rows={2}
              placeholder={"Patient.birthDate\nCondition.code"}
              value={declaredPaths}
              onChange={(e) => setDeclaredPaths(e.target.value)}
              disabled={running}
              className="font-mono text-xs"
            />
          </div>

          <Button onClick={handleSubmit} disabled={running}>
            {submitting ? (
              <Loader2 className="mr-2 h-4 w-4 animate-spin" />
            ) : (
              <Play className="mr-2 h-4 w-4" />
            )}
            {running ? "Running…" : "Start Risk-Driven Export"}
          </Button>
        </CardContent>
      </Card>

      {job && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">
              Job {job.job_id.slice(0, 8)}:{" "}
              <span className="text-muted-foreground">{job.phase}</span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {(job.status === "running" || job.status === "pending") && (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                {job.processed} processed…
              </div>
            )}

            {ap && (
              <div className="grid gap-3 sm:grid-cols-3">
                <Metric label="Achieved k" value={ap.achieved_k ?? ""} />
                <Metric label="Suppressed" value={ap.suppressed_count ?? 0} />
                <Metric
                  label="Feasible"
                  value={ap.feasible ? "yes" : "no"}
                  tone={ap.feasible ? "ok" : "bad"}
                />
              </div>
            )}

            {/* Full passport once the job produced it; otherwise the bare
                disclosure block (e.g. mid-run or older jobs). */}
            {job.transformation_passport ? (
              <TransformationPassportPanel passport={job.transformation_passport} />
            ) : null}

            {!job.transformation_passport && disc && (
              <div className="space-y-2">
                {(() => {
                  const d = DECISION[disc.decision] ?? DECISION.refer;
                  const Icon = d.icon;
                  return (
                    <Badge className={d.className}>
                      <Icon className="mr-1.5 h-3.5 w-3.5" />
                      {d.label}
                    </Badge>
                  );
                })()}
                {disc.checks.length > 0 && (
                  <ul className="ml-1 space-y-0.5 text-xs text-muted-foreground">
                    {disc.checks.map((c, i) => (
                      <li key={i}>
                        <span className="font-mono">{c.rule}</span> → {c.outcome}: {c.detail}
                      </li>
                    ))}
                  </ul>
                )}
                {disc.permit_id && (
                  <p className="text-xs text-muted-foreground">
                    Bound to permit <span className="font-mono">{disc.permit_id}</span>
                  </p>
                )}
              </div>
            )}

            {job.status === "done" && job.result_path && (
              <Button onClick={handleDownload} variant="outline" size="sm">
                <Download className="mr-2 h-4 w-4" />
                Download NDJSON
              </Button>
            )}
            {job.status === "error" && (
              <p className="text-sm text-destructive">{job.error}</p>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}

function Metric({
  label,
  value,
  tone,
}: {
  label: string;
  value: string | number;
  tone?: "ok" | "bad";
}) {
  return (
    <div className="rounded-lg border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div
        className={
          "mt-0.5 text-lg font-semibold " +
          (tone === "ok"
            ? "text-emerald-600 dark:text-emerald-400"
            : tone === "bad"
              ? "text-destructive"
              : "")
        }
      >
        {value}
      </div>
    </div>
  );
}
