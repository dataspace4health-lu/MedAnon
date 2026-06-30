import { useState } from "react";
import { BarChart, Bar, XAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Activity, ChevronDown } from "lucide-react";
import type { ObservationValueStat } from "@/api/trustGate";
import { loincLabel, hasLoincLabel } from "@/lib/loincLabels";

// ---------------------------------------------------------------------------
// Clinical value distributions presented as Kaggle-style "data cards": one
// compact card per (concept, unit) with a histogram, the key summary stats, and
// an optional age-band x sex breakdown. Each card is unambiguous (label + LOINC
// code + unit) so two metrics that share a label are still distinguishable.
// ---------------------------------------------------------------------------

const fmt = (v: number) =>
  Math.abs(v) >= 1000 ? v.toLocaleString(undefined, { maximumFractionDigits: 0 })
    : v.toLocaleString(undefined, { maximumFractionDigits: 2 });

function Histogram({ stat }: { stat: ObservationValueStat }) {
  const bins = stat.histogram ?? [];
  if (!bins.length) return null;
  const data = bins.map((b) => ({ n: b.n, range: `${fmt(b.x0)}–${fmt(b.x1)}` }));
  const meanBin = bins.findIndex((b) => stat.mean >= b.x0 && stat.mean <= b.x1);
  return (
    <ResponsiveContainer width="100%" height={84}>
      <BarChart data={data} margin={{ top: 4, right: 0, bottom: 0, left: 0 }} barCategoryGap={1}>
        <XAxis dataKey="range" hide />
        <Tooltip
          cursor={{ fill: "hsl(var(--muted) / 0.5)" }}
          contentStyle={{ fontSize: 11, borderRadius: 8, padding: "4px 8px" }}
          formatter={(v) => [`${Number(v)} value(s)`, "count"]}
          labelFormatter={(_l, p) => (p && p[0] ? p[0].payload.range : "")}
        />
        <Bar dataKey="n" radius={[2, 2, 0, 0]} isAnimationActive={false}>
          {data.map((_d, i) => <Cell key={i} fill={i === meanBin ? "#0072bc" : "#9bd0ec"} />)}
        </Bar>
      </BarChart>
    </ResponsiveContainer>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wide text-muted-foreground">{label}</span>
      <span className="text-xs font-semibold tabular-nums">{value}</span>
    </div>
  );
}

function DataCard({ stat, strata }: { stat: ObservationValueStat; strata: ObservationValueStat[] }) {
  const [open, setOpen] = useState(false);
  // Prefer the label the source FHIR already carried; fall back to the static
  // LOINC map only when the data didn't name the code.
  const label = stat.display ?? (hasLoincLabel(stat.code) ? loincLabel(stat.code) : null);
  const known = label !== null;
  const maxStratN = Math.max(1, ...strata.map((s) => s.count));
  return (
    <div className="flex flex-col rounded-xl border bg-card p-3.5 shadow-sm">
      {/* header */}
      <div className="flex items-start justify-between gap-2">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold" title={known ? label! : stat.code}>
            {known ? label : <span className="font-mono">{stat.code}</span>}
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1">
            <Badge variant="outline" className="font-mono text-[9px]" title="LOINC code">{stat.code}</Badge>
            {stat.unit && <Badge variant="secondary" className="text-[9px]">{stat.unit}</Badge>}
            {!known && <span className="text-[9px] text-muted-foreground">unlabelled</span>}
          </div>
        </div>
        <div className="shrink-0 text-right">
          <div className="text-base font-bold leading-none tabular-nums">{stat.count.toLocaleString()}</div>
          <div className="text-[10px] text-muted-foreground">values</div>
        </div>
      </div>

      {/* histogram */}
      <div className="mt-2">
        <Histogram stat={stat} />
        <div className="flex justify-between text-[9px] tabular-nums text-muted-foreground">
          <span>{fmt(stat.min)}</span>
          <span>{fmt(stat.max)}</span>
        </div>
      </div>

      {/* stats */}
      <div className="mt-2 grid grid-cols-4 gap-2 border-t pt-2">
        <Stat label="min" value={fmt(stat.min)} />
        <Stat label="median" value={stat.median != null ? fmt(stat.median) : "—"} />
        <Stat label="mean" value={fmt(stat.mean)} />
        <Stat label="max" value={fmt(stat.max)} />
      </div>
      <div className="mt-1 text-[10px] text-muted-foreground">± {fmt(stat.stddev)} std dev</div>

      {/* demographic strata */}
      {strata.length > 0 && (
        <div className="mt-2 border-t pt-2">
          <button onClick={() => setOpen((v) => !v)} className="flex w-full items-center gap-1 text-[11px] text-muted-foreground hover:text-foreground">
            <ChevronDown className={`size-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
            By sex × age ({strata.length})
          </button>
          {open && (
            <div className="mt-1.5 space-y-1">
              {strata.map((s) => (
                <div key={s.stratum} className="grid grid-cols-[64px_1fr_auto] items-center gap-2 text-[10px] tabular-nums">
                  <span className="font-medium">{s.stratum}</span>
                  <span className="h-1.5 overflow-hidden rounded-full bg-muted">
                    <span className="block h-full rounded-full bg-primary/60" style={{ width: `${(s.count / maxStratN) * 100}%` }} />
                  </span>
                  <span className="text-muted-foreground">n {s.count} · med {s.median != null ? fmt(s.median) : "—"}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function ClinicalDistributions({
  stats,
  stratified,
}: {
  stats: ObservationValueStat[];
  stratified?: ObservationValueStat[];
}) {
  if (!stats?.length) return null;
  const stratByCode = new Map<string, ObservationValueStat[]>();
  for (const s of stratified ?? []) {
    const key = `${s.code}|${s.unit}`;
    const list = stratByCode.get(key) ?? [];
    list.push(s);
    stratByCode.set(key, list);
  }

  // Show a reasonable first screen of cards (sorted by frequency upstream), with
  // the long tail of rarer concepts behind a toggle so the whole clinical panel
  // is reachable without flooding the page on a full-server scan.
  const [expanded, setExpanded] = useState(false);
  const INITIAL = 24;
  const visible = expanded ? stats : stats.slice(0, INITIAL);
  const hidden = stats.length - visible.length;

  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><Activity className="size-4" /> Clinical value distributions</CardTitle>
        <p className="text-sm text-muted-foreground">
          One card per measured concept ({stats.length}) — distribution histogram, summary statistics, and an
          age-band × sex breakdown. Each card is keyed by its LOINC code + unit, so distinct metrics that share a
          label stay separate.
        </p>
      </CardHeader>
      <CardContent>
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
          {visible.map((stat) => (
            <DataCard
              key={`${stat.code}-${stat.unit}`}
              stat={stat}
              strata={(stratByCode.get(`${stat.code}|${stat.unit}`) ?? []).slice().sort((a, b) => b.count - a.count)}
            />
          ))}
        </div>
        {stats.length > INITIAL && (
          <button
            onClick={() => setExpanded((v) => !v)}
            className="mt-3 flex w-full items-center justify-center gap-1 rounded-lg border py-2 text-xs font-medium text-muted-foreground hover:bg-muted hover:text-foreground"
          >
            <ChevronDown className={`size-3.5 transition-transform ${expanded ? "rotate-180" : ""}`} />
            {expanded ? "Show fewer" : `Show ${hidden} more concept${hidden === 1 ? "" : "s"}`}
          </button>
        )}
      </CardContent>
    </Card>
  );
}
