import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import { FileText, Loader2, ChevronRight } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { TransformationPassportPanel } from "@/components/shared/TransformationPassportPanel";
import {
  listReports,
  getReport,
  type ReportIndexRow,
} from "@/api/reports";
import type { TransformationPassport } from "@/api/medanon";

const DECISION_STYLE: Record<string, string> = {
  approve: "bg-emerald-600 text-white hover:bg-emerald-600",
  refer: "bg-amber-500 text-white hover:bg-amber-500",
  refuse: "bg-destructive text-white hover:bg-destructive",
};

export default function ReportsPage() {
  const [rows, setRows] = useState<ReportIndexRow[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [passport, setPassport] = useState<TransformationPassport | null>(null);
  const [loadingPassport, setLoadingPassport] = useState(false);

  const load = useCallback(async () => {
    try {
      setRows(await listReports());
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to load reports");
      setRows([]);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function open(jobId: string) {
    if (selected === jobId) {
      setSelected(null);
      setPassport(null);
      return;
    }
    setSelected(jobId);
    setPassport(null);
    setLoadingPassport(true);
    try {
      setPassport(await getReport(jobId));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to load passport");
    } finally {
      setLoadingPassport(false);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Transformation Passports"
        description="Durable, anonymous documentation (TEHDAS2 D7.2 §5.5.1 / EHDS Art 79) of every risk-driven export, privacy model, achieved k/l/t, privacy-risk assessment, and the disclosure decision. Kept independently of the job, for audit."
      />

      {rows === null ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          Loading reports…
        </div>
      ) : rows.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            <FileText className="mx-auto mb-3 h-8 w-8 opacity-40" />
            No stored reports yet. Run a risk-driven export (Async Jobs → Batch →
            Risk-Driven); its passport is persisted here when the app database is
            configured.
          </CardContent>
        </Card>
      ) : (
        <div className="space-y-2">
          {rows.map((r) => (
            <div key={r.job_id}>
              <button
                onClick={() => open(r.job_id)}
                className="flex w-full items-center justify-between gap-3 rounded-lg border px-4 py-3 text-left transition-colors hover:bg-muted/50"
              >
                <div className="flex items-center gap-3">
                  <ChevronRight
                    className={`h-4 w-4 text-muted-foreground transition-transform ${
                      selected === r.job_id ? "rotate-90" : ""
                    }`}
                  />
                  <div>
                    <div className="font-mono text-sm">{r.job_id}</div>
                    <div className="text-xs text-muted-foreground">
                      {r.permit_id ? `permit ${r.permit_id} · ` : ""}
                      {r.created_at?.slice(0, 19).replace("T", " ")}
                    </div>
                  </div>
                </div>
                {r.decision && (
                  <Badge className={DECISION_STYLE[r.decision]}>{r.decision}</Badge>
                )}
              </button>
              {selected === r.job_id && (
                <div className="mt-2 pl-4">
                  {loadingPassport ? (
                    <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                      <Loader2 className="h-4 w-4 animate-spin" />
                      Loading passport…
                    </div>
                  ) : passport ? (
                    <TransformationPassportPanel passport={passport} />
                  ) : null}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
