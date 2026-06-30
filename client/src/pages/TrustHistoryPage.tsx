import { useState, useCallback } from "react";
import { toast } from "sonner";
import { Loader2, Search, History as HistoryIcon, ClipboardList } from "lucide-react";
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";
import { PageHeader } from "@/components/layout/PageHeader";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from "@/components/ui/select";
import {
  getHistory, getTrend, getDatasetFindings, transitionFinding,
  FINDING_STATUSES, FINDING_ROOT_CAUSES,
} from "@/api/trustGate";
import type {
  HistoryRow, TrendPoint, Finding, FindingStatus, FindingRootCause, Decision,
} from "@/api/trustGate";
import { ApiError } from "@/api/types";
import { rateTextClass } from "./trust-gate/trustGateHelpers";
import { humanizeDetail } from "./trust-gate/humanize";

const DECISION_FILL: Record<Decision, string> = {
  PASS: "#16a34a",
  CONDITIONAL_PASS: "#d97706",
  BLOCK: "#dc2626",
};
const DECISION_TEXT: Record<Decision, string> = {
  PASS: "text-emerald-600 dark:text-emerald-400",
  CONDITIONAL_PASS: "text-amber-600 dark:text-amber-400",
  BLOCK: "text-destructive",
};

function FindingTriage({ finding, onSaved }: { finding: Finding; onSaved: (f: Finding) => void }) {
  const [status, setStatus] = useState<FindingStatus>(finding.status);
  const [rootCause, setRootCause] = useState<FindingRootCause>(finding.root_cause);
  const [note, setNote] = useState(finding.note ?? "");
  const [saving, setSaving] = useState(false);
  const dirty = status !== finding.status || rootCause !== finding.root_cause || note !== (finding.note ?? "");

  const save = async () => {
    setSaving(true);
    try {
      const updated = await transitionFinding(finding.id, { status, root_cause: rootCause, note });
      onSaved(updated);
      toast.success(`Finding ${updated.status}`, { description: finding.check_id });
    } catch (e) {
      toast.error("Could not update finding", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-mono text-xs font-medium">{finding.check_id}</span>
        <Badge variant={finding.severity === "critical" ? "destructive" : "secondary"} className="text-[10px]">{finding.severity}</Badge>
        <Badge variant="outline" className="text-[10px]">{finding.status}</Badge>
      </div>
      <p className="mt-2 text-sm text-muted-foreground">{humanizeDetail(finding.check_id, finding.note)}</p>
      <div className="mt-3 flex flex-wrap items-end gap-2">
        <div>
          <label className="mb-1 block text-[10px] uppercase tracking-wide text-muted-foreground">Status</label>
          <Select value={status} onValueChange={(v) => setStatus(v as FindingStatus)}>
            <SelectTrigger className="h-8 w-32"><SelectValue /></SelectTrigger>
            <SelectContent>{FINDING_STATUSES.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <div>
          <label className="mb-1 block text-[10px] uppercase tracking-wide text-muted-foreground">Root cause</label>
          <Select value={rootCause} onValueChange={(v) => setRootCause(v as FindingRootCause)}>
            <SelectTrigger className="h-8 w-40"><SelectValue /></SelectTrigger>
            <SelectContent>{FINDING_ROOT_CAUSES.map((s) => <SelectItem key={s} value={s}>{s.replace(/_/g, " ")}</SelectItem>)}</SelectContent>
          </Select>
        </div>
        <Input className="h-8 flex-1" placeholder="note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
        <Button size="sm" className="h-8" disabled={!dirty || saving} onClick={save}>
          {saving ? <Loader2 className="size-3.5 animate-spin" /> : "Save"}
        </Button>
      </div>
    </div>
  );
}

export default function TrustHistoryPage() {
  const [datasetId, setDatasetId] = useState("provider-dataset");
  const [loading, setLoading] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [history, setHistory] = useState<HistoryRow[]>([]);
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);

  const load = useCallback(async () => {
    const ds = datasetId.trim();
    if (!ds) return;
    setLoading(true);
    try {
      const [h, t, f] = await Promise.all([getHistory(ds), getTrend(ds), getDatasetFindings(ds)]);
      setHistory(h); setTrend(t); setFindings(f); setLoaded(true);
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.status}: ${e.message}` : String(e);
      toast.error("Could not load history", { description: msg });
      setHistory([]); setTrend([]); setFindings([]); setLoaded(true);
    } finally {
      setLoading(false);
    }
  }, [datasetId]);

  const onSaved = (f: Finding) => setFindings((prev) => prev.map((x) => (x.id === f.id ? f : x)));

  const latest = history[0];
  const open = findings.filter((f) => f.status !== "resolved").length;
  // recharts wants oldest → newest
  const chartData = trend.slice().reverse().map((p, i) => ({
    i: i + 1,
    score: p.overall_score ?? 0,
    decision: p.decision,
    when: p.generated_at?.slice(0, 19) ?? "",
  }));

  return (
    <div>
      <PageHeader
        title="QC History & Findings"
        description="Track a dataset's quality across runs and triage its open remediation findings (the PDSA study/act loop). Backed by the Trust Gate's stateful store."
      />

      <Card className="mb-6">
        <CardHeader><CardTitle className="flex items-center gap-2"><HistoryIcon className="size-4" /> Dataset</CardTitle></CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-3">
            <div className="min-w-64 flex-1">
              <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Dataset ID</label>
              <Input
                value={datasetId}
                onChange={(e) => setDatasetId(e.target.value)}
                onKeyDown={(e) => { if (e.key === "Enter") load(); }}
                placeholder="provider-dataset"
              />
            </div>
            <Button onClick={load} disabled={loading || !datasetId.trim()}>
              {loading ? <Loader2 className="size-4 animate-spin" /> : <Search className="size-4" />}
              Load history
            </Button>
          </div>
        </CardContent>
      </Card>

      {loaded && (
        <>
          {/* KPI strip */}
          <div className="mb-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
            {[
              { k: "Runs recorded", v: history.length },
              { k: "Latest verdict", v: latest ? latest.decision.replace("_", " ") : "—" },
              { k: "Latest score", v: latest && latest.overall_score != null ? `${Math.round(latest.overall_score)}%` : "—" },
              { k: "Open findings", v: open },
            ].map((kpi) => (
              <Card key={kpi.k}><CardContent className="py-4"><div className="text-2xl font-bold tabular-nums">{kpi.v}</div><div className="text-xs text-muted-foreground">{kpi.k}</div></CardContent></Card>
            ))}
          </div>

          {/* Trend */}
          {chartData.length > 0 && (
            <Card className="mb-6">
              <CardHeader><CardTitle>Overall score trend</CardTitle><p className="text-sm text-muted-foreground">Oldest → newest, {chartData.length} run{chartData.length === 1 ? "" : "s"}; bar colour = decision.</p></CardHeader>
              <CardContent>
                <ResponsiveContainer width="100%" height={200}>
                  <BarChart data={chartData} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
                    <CartesianGrid strokeDasharray="3 3" stroke="hsl(var(--border))" vertical={false} />
                    <XAxis dataKey="i" tick={{ fontSize: 11 }} stroke="hsl(var(--muted-foreground))" />
                    <YAxis domain={[0, 100]} tick={{ fontSize: 11 }} stroke="hsl(var(--muted-foreground))" />
                    <Tooltip
                      cursor={{ fill: "hsl(var(--muted) / 0.4)" }}
                      contentStyle={{ fontSize: 12, borderRadius: 8 }}
                      formatter={(v) => [`${Math.round(Number(v))}%`, "score"]}
                      labelFormatter={(_l, p) => (p && p[0] ? `${p[0].payload.decision} · ${p[0].payload.when}` : "")}
                    />
                    <Bar dataKey="score" radius={[3, 3, 0, 0]}>
                      {chartData.map((d, i) => <Cell key={i} fill={DECISION_FILL[d.decision]} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </CardContent>
            </Card>
          )}

          {/* Runs list */}
          <Card className="mb-6">
            <CardHeader><CardTitle>Assessment runs</CardTitle></CardHeader>
            <CardContent>
              {history.length ? (
                <div className="space-y-2">
                  {history.map((r, i) => (
                    <div key={r.id ?? i} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm">
                      <span className="flex items-center gap-2">
                        <Badge variant="outline" className={DECISION_TEXT[r.decision]}>{r.decision.replace("_", " ")}</Badge>
                        <span className="text-xs text-muted-foreground">{r.generated_at ?? r.recorded_at ?? ""}</span>
                      </span>
                      <span className={`font-medium tabular-nums ${rateTextClass(Math.round(r.overall_score ?? 0))}`}>
                        {r.overall_score != null ? `${Math.round(r.overall_score)}%` : "—"}{r.grade ? ` · ${r.grade}` : ""}
                      </span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="text-sm text-muted-foreground">
                  No persisted runs for this dataset. Run a QC assessment for it first (the service must have a store configured).
                </p>
              )}
            </CardContent>
          </Card>

          {/* Findings triage */}
          <Card>
            <CardHeader>
              <CardTitle className="flex items-center gap-2"><ClipboardList className="size-4" /> Findings — triage ({findings.length})</CardTitle>
              <p className="text-sm text-muted-foreground">Set status + root cause (source-error / ETL-error / genuine-biology) and save. PDSA study/act loop.</p>
            </CardHeader>
            <CardContent className="space-y-3">
              {findings.length
                ? findings.map((f) => <FindingTriage key={f.id} finding={f} onSaved={onSaved} />)
                : <p className="text-sm text-muted-foreground">No open findings for this dataset.</p>}
            </CardContent>
          </Card>
        </>
      )}
    </div>
  );
}
