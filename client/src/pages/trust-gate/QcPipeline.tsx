import { useEffect, useRef, useState } from "react";
import { Download, Gauge, BadgeCheck, Check, X, AlertTriangle, Loader2 } from "lucide-react";
import type { QualityPassport, TrustCheck } from "@/api/trustGate";

// ---------------------------------------------------------------------------
// Horizontal, real-time QC pipeline. The steps are the ACTUAL audit phases that
// ran (from passport.phases / the checks' `phase`), not generic buckets. While a
// run is in flight the phases light up left-to-right; when the passport arrives
// each phase is finalized in sequence with its real score + verdict.
// ---------------------------------------------------------------------------

const PHASE_ORDER = [
  "structural_conformance",
  "terminology_validity",
  "referential_integrity",
  "completeness_core",
  "completeness_richness",
  "value_plausibility",
  "temporal_plausibility",
  "identity_integrity",
  "provenance_auditability",
  "timeliness",
  "source_accuracy",
];
const PHASE_LABEL: Record<string, string> = {
  structural_conformance: "Structural conformance",
  terminology_validity: "Terminology validity",
  referential_integrity: "Referential integrity",
  completeness_core: "Core completeness",
  completeness_richness: "Completeness richness",
  value_plausibility: "Value plausibility",
  temporal_plausibility: "Temporal plausibility",
  identity_integrity: "Identity integrity",
  provenance_auditability: "Provenance & auditability",
  timeliness: "Timeliness",
  source_accuracy: "Source accuracy",
};
const labelFor = (id: string) =>
  PHASE_LABEL[id] ?? id.replace(/_/g, " ").replace(/\b\w/g, (m) => m.toUpperCase());

type SStatus = "pending" | "running" | "done" | "warn" | "fail";
interface Step { id: string; name: string; status: SStatus; meta: string; kind: "intake" | "phase" | "decision" | "label" }

const pct = (n: unknown) => n != null ? `${Math.round(Number(n))}%` : "n/a";

function phasesOf(p: QualityPassport): string[] {
  const fromVerdicts = Object.keys(p.phases ?? {});
  const ran = fromVerdicts.length
    ? fromVerdicts
    : Array.from(new Set((p.checks ?? []).map((c: TrustCheck) => c.phase).filter(Boolean) as string[]));
  const ordered = PHASE_ORDER.filter((id) => ran.includes(id));
  const extra = ran.filter((id) => !PHASE_ORDER.includes(id));
  return [...ordered, ...extra];
}

function phaseStatus(p: QualityPassport, id: string): { status: SStatus; meta: string } {
  const v = (p.phases ?? {})[id];
  if (v) {
    const status: SStatus = v.decision === "PASS" ? "done" : v.decision === "BLOCK" ? "fail" : "warn";
    const sc = v.score == null ? "" : `${pct(v.score)}% · `;
    return { status, meta: `${sc}${v.checks_passed}/${v.checks_assessed} passing` };
  }
  // Derive from checks when no per-phase verdict.
  const ch = (p.checks ?? []).filter((c) => c.phase === id && c.result !== "NA");
  if (!ch.length) return { status: "done", meta: "not assessed" };
  const passed = ch.filter((c) => c.result === "PASS").length;
  const failedDet = ch.filter((c) => c.result === "FAIL" && !c.advisory).length;
  return { status: failedDet ? "fail" : passed === ch.length ? "done" : "warn", meta: `${passed}/${ch.length} passing` };
}

function finalSteps(p: QualityPassport, sourceModel: string): Step[] {
  const steps: Step[] = [
    { id: "intake", name: "Intake & parse", kind: "intake", status: "done", meta: `${p.resource_count ?? 0} records (${sourceModel})` },
  ];
  for (const id of phasesOf(p)) {
    const { status, meta } = phaseStatus(p, id);
    steps.push({ id, name: labelFor(id), kind: "phase", status, meta });
  }
  const d = p.decision;
  steps.push({
    id: "decision", name: "Decision", kind: "decision",
    status: d === "PASS" ? "done" : d === "BLOCK" ? "fail" : "warn",
    meta: `${pct(p.overall_score)} · ${d.replace(/_/g, " ")}`,
  });
  const l = p.label;
  steps.push({
    id: "label", name: "EHDS label", kind: "label", status: "done",
    meta: l ? `${l.quality?.decision ?? l.quality?.grade ?? ""} · ${l.utility?.tier ?? ""} · L${l.maturity?.level ?? ""}` : "issued",
  });
  return steps;
}

const RUNNING_STEPS: Step[] = [
  { id: "intake", name: "Intake & parse", kind: "intake", status: "pending", meta: "" },
  ...PHASE_ORDER.slice(0, 9).map((id) => ({ id, name: labelFor(id), kind: "phase" as const, status: "pending" as SStatus, meta: "" })),
  { id: "decision", name: "Decision", kind: "decision", status: "pending", meta: "" },
  { id: "label", name: "EHDS label", kind: "label", status: "pending", meta: "" },
];

const ICON = { intake: Download, decision: Gauge, label: BadgeCheck } as Record<string, React.ElementType>;
const NODE: Record<SStatus, string> = {
  pending: "border-border bg-card text-muted-foreground",
  running: "border-primary bg-primary/5 text-primary animate-pulse",
  done: "border-emerald-500/50 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400",
  warn: "border-amber-500/50 bg-amber-500/10 text-amber-600 dark:text-amber-400",
  fail: "border-destructive/50 bg-destructive/10 text-destructive",
};
export function QcPipeline({ running, passport, sourceModel }: { running: boolean; passport: QualityPassport | null; sourceModel: string }) {
  const [steps, setSteps] = useState<Step[]>(RUNNING_STEPS);
  const timers = useRef<number[]>([]);
  const clear = () => { timers.current.forEach((t) => clearTimeout(t)); timers.current = []; };

  // Left-to-right sweep while in flight.
  useEffect(() => {
    if (!running) return;
    clear();
    const base = RUNNING_STEPS.map((s) => ({ ...s, status: "pending" as SStatus }));
    setSteps(base);
    let i = 0;
    const tick = () => {
      setSteps((prev) => prev.map((s, idx) => (idx < i ? { ...s, status: "done" } : idx === i ? { ...s, status: "running" } : s)));
      if (i < base.length - 1) { i += 1; timers.current.push(window.setTimeout(tick, 260)); }
    };
    tick();
    return clear;
  }, [running]);

  // Finalize each real phase in sequence when the passport arrives.
  useEffect(() => {
    if (!passport) return;
    clear();
    const final = finalSteps(passport, sourceModel);
    setSteps(final.map((s) => ({ ...s, status: "pending", meta: "" })));
    final.forEach((s, idx) => {
      timers.current.push(window.setTimeout(() => {
        setSteps((prev) => { const next = prev.slice(); next[idx] = s; return next; });
      }, idx * 220));
    });
    return clear;
  }, [passport, sourceModel]);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => () => clear(), []);

  return (
    <ol className="grid grid-cols-2 gap-x-3 gap-y-5 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-6">
      {steps.map((s, idx) => {
        const StepIcon = ICON[s.kind] ?? null;
        return (
          <li key={s.id} className="flex flex-col items-center text-center">
            <div className="flex items-center gap-1">
              <span className="text-[10px] font-semibold tabular-nums text-muted-foreground">{idx + 1}</span>
              <div className={`grid size-10 place-items-center rounded-full border-2 transition-colors ${NODE[s.status]}`}>
                {s.status === "running" ? <Loader2 className="size-4 animate-spin" />
                  : s.status === "done" ? <Check className="size-4" />
                  : s.status === "warn" ? <AlertTriangle className="size-4" />
                  : s.status === "fail" ? <X className="size-4" />
                  : StepIcon ? <StepIcon className="size-4" /> : <span className="text-xs font-bold">{idx + 1}</span>}
              </div>
            </div>
            <div className={`mt-2 text-xs font-medium leading-tight ${s.status === "pending" ? "text-muted-foreground" : ""}`}>{s.name}</div>
            <div className="mt-0.5 text-[10px] tabular-nums text-muted-foreground">{s.meta}</div>
          </li>
        );
      })}
    </ol>
  );
}
