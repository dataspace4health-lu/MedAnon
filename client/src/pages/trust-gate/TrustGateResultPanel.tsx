import { useState, useEffect } from "react";
import { toast } from "sonner";
import {
  ShieldCheck,
  ShieldAlert,
  ShieldX,
  AlertTriangle,
  CheckCircle2,
  XCircle,
  MinusCircle,
  ChevronDown,
  FileText,
  Braces,
  FileCheck2,
  ListChecks,
  Activity,
  BadgeCheck,
  Award,
  ScrollText,
  Fingerprint,
  Loader2,
} from "lucide-react";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MarkdownReport } from "@/components/shared/MarkdownReport";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  getAudit,
  getDatasetFindings,
  transitionFinding,
  FINDING_STATUSES,
  FINDING_ROOT_CAUSES,
} from "@/api/trustGate";
import type {
  Finding,
  FindingStatus,
  FindingRootCause,
  AuditRow,
  EhdsLabel,
} from "@/api/trustGate";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import type {
  QualityPassport,
  TrustCheck,
  CheckResultValue,
  DimensionScore,
  PhaseVerdict,
  SectorVerdict,
  Coverage,
} from "@/api/trustGate";
import {
  DECISION_STYLES,
  humanizeCheckId,
  rateColorClass,
  rateTextClass,
} from "./trustGateHelpers";
import { codeSystemLabel } from "@/lib/loincLabels";
import { locateProblem, whatItChecks } from "./humanize";
import { ClinicalDistributions } from "./ClinicalDistributions";

// ---------------------------------------------------------------------------
// Pillar metadata
// ---------------------------------------------------------------------------

const PILLAR_META = [
  {
    key: "conformance" as const,
    label: "Conformance",
    description: "Format validity, FHIR spec adherence, coding structure, and reference integrity.",
    Icon: FileCheck2,
  },
  {
    key: "completeness" as const,
    label: "Completeness",
    description: "Required elements present, observations carry a value or a declared reason for absence.",
    Icon: ListChecks,
  },
  {
    key: "plausibility" as const,
    label: "Plausibility",
    description: "Clinical value ranges, uniqueness, temporal ordering, and cross-field consistency.",
    Icon: Activity,
  },
] as const;

// ---------------------------------------------------------------------------
// Small shared components
// ---------------------------------------------------------------------------

const DECISION_ICON = {
  PASS: ShieldCheck,
  CONDITIONAL_PASS: ShieldAlert,
  BLOCK: ShieldX,
} as const;

function ResultBadge({ result }: { result: CheckResultValue }) {
  if (result === "PASS")
    return (
      <Badge variant="outline" className="border-emerald-500/40 text-emerald-600 dark:text-emerald-400">
        <CheckCircle2 className="size-3" /> PASS
      </Badge>
    );
  if (result === "FAIL")
    return (
      <Badge variant="destructive">
        <XCircle className="size-3" /> FAIL
      </Badge>
    );
  return (
    <Badge variant="secondary">
      <MinusCircle className="size-3" /> NA
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// Pillar card, one per Kahn category
// ---------------------------------------------------------------------------

interface PillarCardProps {
  label: string;
  description: string;
  Icon: React.ElementType;
  pct: number | null;
  passed: number;
  failed: number;
  total: number;
}

function PillarCard({ label, description, Icon, pct, passed, failed, total }: PillarCardProps) {
  const assessed = pct !== null;
  const display  = assessed ? pct! : 0;

  const borderCls = !assessed
    ? "border-muted"
    : display >= 90
    ? "border-emerald-400/40"
    : display >= 70
    ? "border-amber-400/40"
    : "border-destructive/40";

  const textCls = !assessed ? "text-muted-foreground" : rateTextClass(display);
  const barCls  = rateColorClass(display);

  return (
    <Card className={borderCls}>
      <CardContent className="flex flex-col gap-3 pb-4 pt-5">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            <Icon className="size-4 shrink-0 text-muted-foreground" />
            <span className="text-sm font-semibold">{label}</span>
          </div>
          <span className={`text-3xl font-bold tabular-nums leading-none ${textCls}`}>
            {assessed ? `${display}%` : "—"}
          </span>
        </div>

        <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
          <div
            className={`h-full rounded-full transition-all ${barCls}`}
            style={{ width: `${display}%` }}
          />
        </div>

        <div className="flex items-center gap-3 text-xs tabular-nums">
          <span className="text-emerald-600 dark:text-emerald-400">{passed} passed</span>
          {failed > 0 && <span className="text-destructive">{failed} failed</span>}
          <span className="ml-auto text-muted-foreground">{total} assessed</span>
        </div>

        <p className="text-[11px] leading-snug text-muted-foreground">{description}</p>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Checks table
// ---------------------------------------------------------------------------

function ChecksTable({ checks }: { checks: TrustCheck[] }) {
  if (!checks.length) {
    return <p className="py-6 text-center text-sm text-muted-foreground">No checks in this view.</p>;
  }
  return (
    <div className="w-full max-w-full overflow-x-auto">
      <Table className="w-full table-fixed">
        <TableHeader>
          <TableRow>
            <TableHead className="w-[22%]">Check</TableHead>
            <TableHead className="w-[11%]">Category</TableHead>
            <TableHead className="w-[8%]">Result</TableHead>
            <TableHead className="w-[12%] text-right">Violations</TableHead>
            <TableHead className="w-[8%] text-right">Threshold</TableHead>
            <TableHead className="w-[18%]">What it verifies</TableHead>
            <TableHead className="w-[21%]">Recommendation</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {checks.map((c) => (
            <TableRow key={c.check_id} className={c.critical && c.result === "FAIL" ? "bg-destructive/5" : undefined}>
              <TableCell className="align-top font-mono text-xs">
                <div className="flex flex-wrap items-center gap-1.5">
                  <span className="break-words">{humanizeCheckId(c.check_id)}</span>
                  {c.critical && (
                    <Badge variant="destructive" className="h-4 px-1 text-[10px]">critical</Badge>
                  )}
                  {c.advisory && (
                    <Badge variant="outline" className="h-4 border-amber-500/40 px-1 text-[10px] text-amber-600 dark:text-amber-400">advisory</Badge>
                  )}
                </div>
                <div className="mt-0.5 break-all font-mono text-[10px] text-muted-foreground">{c.check_id}</div>
                {c.hdqt_category && (
                  <div className="mt-0.5 break-words text-[10px] uppercase tracking-wide text-muted-foreground">
                    {c.hdqt_category} · {c.hdqt_dimension}
                  </div>
                )}
              </TableCell>
              <TableCell className="align-top text-xs capitalize text-muted-foreground">
                <span className="break-words">{c.category}</span>
                <div className="break-words text-[10px]">{c.subcategory} · {c.context}</div>
              </TableCell>
              <TableCell className="align-top">
                {c.skipped ? (
                  <Badge variant="secondary" title={c.skip_reason}>skipped</Badge>
                ) : (
                  <ResultBadge result={c.result} />
                )}
              </TableCell>
              <TableCell className="align-top text-right text-xs tabular-nums">
                {c.applicable > 0
                  ? `${c.violations.toLocaleString()}/${c.applicable.toLocaleString()} (${Math.round(c.violation_fraction * 100)}%)`
                  : "—"}
              </TableCell>
              <TableCell className="align-top text-right text-xs tabular-nums text-muted-foreground">
                {Math.round(c.threshold * 100)}%
              </TableCell>
              <TableCell className="align-top whitespace-normal break-words text-xs text-muted-foreground">
                {c.skipped ? c.skip_reason : (c.description || "—")}
              </TableCell>
              <TableCell className="align-top whitespace-normal break-words text-xs text-muted-foreground">
                {c.skipped ? "—" : (c.recommendation || "—")}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Findings card
// ---------------------------------------------------------------------------

function FindingGroup({
  check,
  details,
}: {
  check: TrustCheck;
  details: Array<Record<string, unknown>>;
}) {
  const [open, setOpen] = useState(false);
  const failPct =
    (check.violation_fraction ?? (check.applicable > 0 ? check.violations / check.applicable : 0)) * 100;
  const sevText  = failPct >= 50 ? "text-destructive" : "text-amber-600 dark:text-amber-400";
  const sevBar   = failPct >= 50 ? "bg-destructive" : "bg-amber-500";
  const failLabel = failPct > 0 && failPct < 1 ? failPct.toFixed(1) : Math.round(failPct).toString();
  const shown = details.length;
  const total = check.violations;

  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium">{humanizeCheckId(check.check_id)}</span>
            <Badge variant="outline" className="text-[10px] uppercase tracking-wide capitalize">
              {check.category}
            </Badge>
          </div>
          <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{check.check_id}</div>
        </div>
        <div className="text-right">
          <div className={`text-lg font-semibold tabular-nums ${sevText}`}>{failLabel}%</div>
          <div className="text-xs text-muted-foreground">
            {total.toLocaleString()} of {check.applicable.toLocaleString()} affected
          </div>
        </div>
      </div>

      <div className="mt-3 h-1.5 w-full overflow-hidden rounded-full bg-muted">
        <div className={`h-full ${sevBar}`} style={{ width: `${Math.max(failPct, failPct > 0 ? 2 : 0)}%` }} />
      </div>

      <p className="mt-3 text-sm text-muted-foreground">
        <span className="font-medium text-foreground">What this checks: </span>
        {whatItChecks(check)}
      </p>
      {check.recommendation && (
        <p className="mt-1.5 text-sm text-muted-foreground">
          <span className="font-medium text-foreground">How to fix: </span>
          {check.recommendation}
        </p>
      )}
      {failPct >= 100 && (
        <p className="mt-2 flex items-start gap-1.5 text-xs text-amber-600 dark:text-amber-400">
          <AlertTriangle className="mt-0.5 size-3.5 shrink-0" />
          {check.check_id === "conformance.reference_integrity"
            ? "Every reference in this batch points outside the batch. This is expected when you submit a slice of a larger server, the referenced resources exist on the server but were not included in this scan. Run a full-server scan or Patient/$everything to assess reference integrity correctly."
            : "Every applicable resource is affected. A 100% rate typically indicates a systematic scope or encoding issue (e.g. a referenced resource type was excluded from the scan) rather than isolated bad records. Verify the check logic and input coverage before treating these as individual data errors."}
        </p>
      )}

      {shown > 0 && (
        <Collapsible open={open} onOpenChange={setOpen} className="mt-3">
          <CollapsibleTrigger className="flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground">
            <ChevronDown className={`size-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
            {open ? "Hide" : "Show"} affected resources
            {shown < total
              ? ` (first ${shown.toLocaleString()} of ${total.toLocaleString()})`
              : ` (${shown.toLocaleString()})`}
          </CollapsibleTrigger>
          <CollapsibleContent>
            <div className="mt-2 overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Record</TableHead>
                    <TableHead>Field</TableHead>
                    <TableHead>Value</TableHead>
                    <TableHead>What is wrong</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {details.map((d, i) => {
                    const loc = locateProblem(check.check_id, d);
                    return (
                      <TableRow key={i}>
                        <TableCell className="font-mono text-xs">{loc.resource}</TableCell>
                        <TableCell className="font-mono text-xs text-muted-foreground">{loc.field}</TableCell>
                        <TableCell className="font-mono text-xs">
                          {loc.value ?? <span className="text-muted-foreground/60" title="Withheld, a field value can be PHI">—</span>}
                        </TableCell>
                        <TableCell className="text-xs">{loc.problem}</TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
          </CollapsibleContent>
        </Collapsible>
      )}
    </div>
  );
}

function FindingsCard({ checks }: { checks: TrustCheck[] }) {
  const groups = checks
    .filter((c) => c.result === "FAIL" && (c.violation_details?.length ?? 0) > 0)
    .map((c) => ({ check: c, details: c.violation_details ?? [] }));

  if (!groups.length) {
    const anyFail = checks.some((c) => c.result === "FAIL");
    return (
      <Card>
        <CardHeader><CardTitle>Findings</CardTitle></CardHeader>
        <CardContent className="text-sm text-muted-foreground">
          {anyFail
            ? "Checks failed on aggregate counts but produced no per-resource detail in this view."
            : "No violations, every applicable check passed."}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Findings, what is wrong and where</CardTitle>
        <p className="text-sm text-muted-foreground">
          {groups.length} check{groups.length === 1 ? "" : "s"} failed with per-resource detail.
          Each shows the share of records affected and how to remediate.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {groups.map(({ check, details }) => (
          <FindingGroup key={check.check_id} check={check} details={details} />
        ))}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Dimension scorecard / phases / sectors
// ---------------------------------------------------------------------------

function KeyVals({ obj }: { obj: Record<string, unknown> }) {
  const entries = Object.entries(obj).filter(([, v]) => v !== undefined && v !== null);
  if (!entries.length) return null;
  return (
    <div className="space-y-1.5">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-start justify-between gap-4 text-sm">
          <span className="text-muted-foreground">{k.replace(/_/g, " ")}</span>
          <span className="text-right font-medium">
            {typeof v === "boolean" ? (v ? "yes" : "no") : Array.isArray(v) ? v.join(", ") : String(v)}
          </span>
        </div>
      ))}
    </div>
  );
}

const GRADE_CLASS: Record<string, string> = {
  A: "bg-emerald-500/15 text-emerald-600",
  B: "bg-emerald-500/15 text-emerald-600",
  C: "bg-amber-500/15 text-amber-600",
  D: "bg-amber-500/15 text-amber-600",
  F: "bg-destructive/15 text-destructive",
};

const DECISION_CHIP: Record<string, string> = {
  PASS: "text-emerald-600",
  CONDITIONAL_PASS: "text-amber-600",
  BLOCK: "text-destructive",
  NA: "text-muted-foreground",
};

function GradeBadge({ grade }: { grade: string | null }) {
  if (!grade) return <span className="text-xs text-muted-foreground">n/a</span>;
  return (
    <span
      className={`inline-flex size-6 items-center justify-center rounded-full text-xs font-bold ${GRADE_CLASS[grade] ?? "bg-muted"}`}
    >
      {grade}
    </span>
  );
}

function Scorecard({ scorecard }: { scorecard?: Record<string, DimensionScore> }) {
  const entries = Object.entries(scorecard ?? {});
  if (!entries.length) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Dimension scorecard, DAMA / ISO 25012</CardTitle></CardHeader>
      <CardContent className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {entries.map(([dim, s]) => (
          <div key={dim} className="flex items-center gap-2 rounded-lg border p-3">
            <GradeBadge grade={s.grade} />
            <div className="min-w-0">
              <div className="truncate text-sm font-medium capitalize">{dim}</div>
              <div className="text-xs tabular-nums text-muted-foreground">
                {s.score === null
                  ? "not assessed"
                  : `${Math.round(s.score)}% · ${s.checks_passed}/${s.checks_assessed}`}
              </div>
            </div>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function PhasesCard({ phases }: { phases?: Record<string, PhaseVerdict> }) {
  const entries = Object.entries(phases ?? {});
  if (entries.length < 2) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Audit phases</CardTitle></CardHeader>
      <CardContent className="space-y-2">
        {entries.map(([id, p]) => (
          <div key={id} className="flex items-center justify-between gap-3 text-sm">
            <span className="font-mono text-xs">{id}</span>
            <span className="flex items-center gap-3">
              <span className="tabular-nums text-muted-foreground">
                {p.score === null ? "NA" : `${Math.round(p.score)}%`} · {p.checks_passed}/{p.checks_assessed}
              </span>
              <span className={`font-medium ${DECISION_CHIP[p.decision] ?? ""}`}>{p.decision}</span>
            </span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

function SectorsCard({ targets }: { targets?: Record<string, SectorVerdict> }) {
  const entries = Object.entries(targets ?? {});
  if (!entries.length) return null;
  return (
    <Card>
      <CardHeader><CardTitle>Sectors, per-target verdicts</CardTitle></CardHeader>
      <CardContent className="space-y-2">
        {entries.map(([id, t]) => (
          <div key={id} className="flex items-center justify-between gap-3 text-sm">
            <span className="font-medium">{id}</span>
            <span className="flex items-center gap-3">
              <span className="tabular-nums text-muted-foreground">
                {t.resource_count} res · {t.score === null ? "NA" : `${Math.round(t.score)}%`}
              </span>
              <span className={`font-medium ${DECISION_CHIP[t.decision] ?? ""}`}>{t.decision}</span>
            </span>
          </div>
        ))}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// EHDS dataset label (quality + utility + maturity + FAIR)
// ---------------------------------------------------------------------------

function LabelTile({ cap, val, sub }: { cap: string; val: string; sub: string }) {
  return (
    <div className="rounded-lg border bg-muted/30 p-3">
      <div className="text-[10px] uppercase tracking-wide text-muted-foreground">{cap}</div>
      <div className="mt-1 text-lg font-bold capitalize">{val}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">{sub}</div>
    </div>
  );
}

function EhdsLabelCard({ label }: { label: EhdsLabel }) {
  const q = label.quality ?? {}, u = label.utility ?? {}, m = label.maturity ?? {};
  const fairKeys = Array.isArray(label.fair)
    ? label.fair
    : Object.keys(label.fair ?? {}).filter((k) => (label.fair as Record<string, boolean>)[k]);
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Award className="size-4" /> EHDS dataset label
          <span className="text-xs font-normal text-muted-foreground">{label.scheme}</span>
        </CardTitle>
      </CardHeader>
      <CardContent>
        <div className="grid gap-3 sm:grid-cols-3">
          <LabelTile cap="Quality" val={String(q.decision ?? q.grade ?? "—")}
            sub={q.score != null ? `${Math.round(q.score)}% checks passing` : "purpose-bound verdict"} />
          <LabelTile cap="Utility" val={String(u.tier ?? "—")}
            sub={u.score != null ? `${Math.round(u.score)}% fit for declared use` : "fitness for declared use"} />
          <LabelTile cap="Maturity" val={`Level ${m.level ?? "—"} / 5`}
            sub={Array.isArray(m.basis) ? m.basis.join(", ") : (m.name ?? "data-lifecycle maturity")} />
        </div>
        <div className="mt-3 flex flex-wrap gap-1.5">
          {fairKeys.length
            ? fairKeys.map((k) => <Badge key={k} variant="secondary">FAIR: {k}</Badge>)
            : <Badge variant="outline" className="text-muted-foreground">FAIR: not yet certified</Badge>}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Remediation findings, interactive triage (PDSA study/act loop)
// ---------------------------------------------------------------------------

const SEV_BADGE: Record<string, "destructive" | "secondary" | "outline"> = {
  critical: "destructive", major: "secondary", minor: "outline",
};

function TriageRow({
  finding,
  detailRows,
  onSaved,
}: {
  finding: Finding;
  detailRows: Array<Record<string, unknown>>;
  onSaved: (f: Finding) => void;
}) {
  const [status, setStatus] = useState<FindingStatus>(finding.status);
  const [rootCause, setRootCause] = useState<FindingRootCause>(finding.root_cause);
  const [note, setNote] = useState(finding.note ?? "");
  const [saving, setSaving] = useState(false);
  const [open, setOpen] = useState(false);

  const dirty = status !== finding.status || rootCause !== finding.root_cause || note !== (finding.note ?? "");

  const save = async () => {
    setSaving(true);
    try {
      const updated = await transitionFinding(finding.id, { status, root_cause: rootCause, note });
      onSaved(updated);
      toast.success(`Finding ${updated.status}`, { description: prettyOrId(finding.check_id) });
    } catch (e) {
      toast.error("Could not update finding", { description: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="rounded-lg border p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <span className="font-mono text-xs font-medium">{finding.check_id}</span>
          <Badge variant={SEV_BADGE[finding.severity] ?? "secondary"} className="text-[10px]">{finding.severity}</Badge>
          <Badge variant="outline" className="text-[10px]">{finding.status}</Badge>
        </div>
        {detailRows.length > 0 && (
          <button onClick={() => setOpen((v) => !v)} className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground">
            <ChevronDown className={`size-3.5 transition-transform ${open ? "rotate-180" : ""}`} />
            {open ? "Hide" : "Show"} affected records ({detailRows.length})
          </button>
        )}
      </div>

      <p className="mt-2 text-sm text-muted-foreground">{humanizeFinding(finding.check_id)}</p>

      {detailRows.length > 0 && (() => {
        const ex = locateProblem(finding.check_id, detailRows[0]);
        return (
          <p className="mt-1.5 text-xs">
            <span className="text-muted-foreground">Example: </span>
            <span className="font-mono">{ex.resource}</span>
            <span className="text-muted-foreground"> · {ex.field}</span>
            {ex.value != null && <> = <span className="font-mono font-medium text-foreground">{ex.value}</span></>}
          </p>
        );
      })()}

      {open && detailRows.length > 0 && (
        <div className="mt-3 overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Record</TableHead><TableHead>Field</TableHead><TableHead>Value</TableHead><TableHead>What is wrong</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {detailRows.map((d, i) => {
                const loc = locateProblem(finding.check_id, d);
                return (
                  <TableRow key={i}>
                    <TableCell className="font-mono text-xs">{loc.resource}</TableCell>
                    <TableCell className="font-mono text-xs text-muted-foreground">{loc.field}</TableCell>
                    <TableCell className="font-mono text-xs">{loc.value ?? <span className="text-muted-foreground/60" title="Withheld, a field value can be PHI">—</span>}</TableCell>
                    <TableCell className="text-xs">{loc.problem}</TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </div>
      )}

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

function RemediationTriage({
  findings,
  checks,
  onChange,
}: {
  findings: Finding[];
  checks: TrustCheck[];
  onChange: (f: Finding) => void;
}) {
  const detailByCheck = new Map<string, Array<Record<string, unknown>>>();
  for (const c of checks) if (c.violation_details?.length) detailByCheck.set(c.check_id, c.violation_details);
  const open = findings.filter((f) => f.status !== "resolved").length;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Remediation findings, triage (PDSA)</CardTitle>
        <p className="text-sm text-muted-foreground">
          {open} open of {findings.length}. Set status + root cause (source-error / ETL-error / genuine-biology),
          expand to see exactly which record and field failed, then save.
        </p>
      </CardHeader>
      <CardContent className="space-y-3">
        {findings.map((f) => (
          <TriageRow key={f.id} finding={f} detailRows={detailByCheck.get(f.check_id) ?? []} onSaved={onChange} />
        ))}
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Audit report (ALCOA++ / determinism provenance / trail)
// ---------------------------------------------------------------------------

function AuditKV({ obj }: { obj: Record<string, unknown> }) {
  const entries = Object.entries(obj).filter(([, v]) => v !== undefined && v !== null && v !== "");
  return (
    <div className="space-y-1.5">
      {entries.map(([k, v]) => (
        <div key={k} className="flex items-start justify-between gap-4 text-sm">
          <span className="text-muted-foreground">{k.replace(/_/g, " ")}</span>
          <span className="text-right font-medium">
            {typeof v === "boolean" ? (v ? "yes" : "no") : Array.isArray(v) ? v.length : String(v)}
          </span>
        </div>
      ))}
    </div>
  );
}

function AuditReport({ passport, audit }: { passport: QualityPassport; audit: AuditRow[] }) {
  const ev = passport.evaluation ?? {};
  const latest = audit[0] ?? {};
  const identity = {
    dataset_id: passport.dataset_id,
    provider_id: latest.provider_id ?? "—",
    source_model: (passport.source_types ?? []).join(", ") || "fhir",
    assessment_id: latest.assessment_id ?? "(not persisted)",
    generated_at: passport.generated_at ?? latest.generated_at ?? "—",
    recorded_at: latest.recorded_at ?? "—",
    records: passport.resource_count,
  };
  const determinism = {
    decision: passport.decision,
    decision_basis: ev.decision_basis ?? "deterministic checks only",
    sampler_seed: ev.sampler_seed,
    advisory_checks: Array.isArray(ev.advisory_check_ids) ? ev.advisory_check_ids.length : ev.advisory_check_ids ?? 0,
    validator_used: ev.validator_used,
    terminology_used: ev.terminology_used,
    lifecycle_stage: passport.lifecycle_stage ?? ev.lifecycle_stage,
    org_role: passport.org_role ?? ev.org_role,
  };
  return (
    <Card>
      <CardHeader>
        <CardTitle className="flex items-center gap-2"><ScrollText className="size-4" /> Quality Control Audit Report</CardTitle>
        <p className="text-sm text-muted-foreground">ALCOA++ / 21 CFR Part 11-aligned, attributable, contemporaneous, traceable.</p>
      </CardHeader>
      <CardContent className="space-y-6">
        <div>
          <h3 className="mb-2 flex items-center gap-1.5 text-sm font-semibold"><Fingerprint className="size-3.5" /> Assessment identity</h3>
          <AuditKV obj={identity} />
        </div>
        <div>
          <h3 className="mb-2 text-sm font-semibold">Determinism &amp; reproducibility</h3>
          <AuditKV obj={determinism} />
          <p className="mt-2 text-xs text-muted-foreground">
            The verdict derives solely from deterministic checks. Statistical advisory findings are reproducible
            under the recorded sampler seed and never alter the gate decision.
          </p>
        </div>
        {Object.keys(passport.framework_versions ?? {}).length > 0 && (
          <div>
            <h3 className="mb-2 text-sm font-semibold">Framework &amp; engine versions</h3>
            <div className="flex flex-wrap gap-1.5">
              {Object.entries(passport.framework_versions).map(([k, v]) => (
                <Badge key={k} variant="outline" className="font-mono text-[10px]">{k}: {v}</Badge>
              ))}
            </div>
          </div>
        )}
        <div>
          <h3 className="mb-2 text-sm font-semibold">Provenance &amp; auditability</h3>
          <AuditKV obj={passport.auditability} />
        </div>
        <div>
          <h3 className="mb-2 text-sm font-semibold">ALCOA++ audit trail</h3>
          {audit.length ? (
            <div className="space-y-2">
              {audit.map((r, i) => (
                <div key={i} className="flex items-center justify-between gap-3 rounded-md border px-3 py-2 text-sm">
                  <span className="font-medium">{r.decision}<span className="ml-2 font-mono text-xs text-muted-foreground">{r.assessment_id}</span></span>
                  <span className="text-xs text-muted-foreground">recorded {r.recorded_at ?? r.generated_at ?? "—"}</span>
                </div>
              ))}
            </div>
          ) : (
            <p className="text-sm text-muted-foreground">
              No persisted trail for this dataset. Configure <code>TRUST_GATE_STORE_DB</code> (or a Postgres
              <code>TRUST_GATE_STORE_DB_URL</code>) on the service to retain an append-only history across runs.
            </p>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

function prettyOrId(id: string): string {
  return humanizeCheckId(id);
}
function humanizeFinding(id: string): string {
  // re-use the humanizer (kept local to avoid a circular import surprise)
  return whatItChecks({ check_id: id, description: "" } as TrustCheck);
}

// ---------------------------------------------------------------------------
// Assessment-coverage honesty badge, a grade over a thin subset of checks must
// not read like a full assessment, so the validation depth + what did not run
// travel next to the verdict.
// ---------------------------------------------------------------------------

const DEPTH_LABEL: Record<string, string> = {
  comprehensive: "full coverage",
  partial: "partial coverage",
  structural_only: "structural-only coverage",
};

function CoverageBadge({ coverage }: { coverage: Coverage }) {
  const depth = coverage.validation_depth;
  const missing = (coverage.not_exercised ?? []).map((m) => m.replace(/_/g, " "));
  const full = depth === "comprehensive";
  const title = missing.length
    ? `Not exercised: ${missing.join(", ")}`
    : "All depth capabilities exercised";
  return (
    <Badge
      variant="outline"
      title={title}
      className={`mt-2 gap-1 ${full ? "border-emerald-500/40 text-emerald-700 dark:text-emerald-400" : "border-amber-500/40 text-amber-700 dark:text-amber-400"}`}
    >
      {!full && <AlertTriangle className="size-3" />}
      {DEPTH_LABEL[depth] ?? depth} · {coverage.checks_assessed}/{coverage.checks_total} checks assessed
      {missing.length > 0 && <span className="opacity-70">· not run: {missing.join(", ")}</span>}
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// Main export
// ---------------------------------------------------------------------------

export function TrustGateResultPanel({ passport }: { passport: QualityPassport }) {
  const [reportOpen, setReportOpen] = useState(false);
  const [jsonOpen, setJsonOpen]     = useState(false);
  const [audit, setAudit]       = useState<AuditRow[]>([]);
  const [findings, setFindings] = useState<Finding[]>([]);

  useEffect(() => {
    const ds = passport.dataset_id;
    if (!ds) return;
    getAudit(ds).then(setAudit).catch(() => setAudit([]));
    getDatasetFindings(ds).then(setFindings).catch(() => setFindings([]));
  }, [passport.dataset_id, passport.generated_at]);

  const style       = DECISION_STYLES[passport.decision];
  const DecisionIcon = DECISION_ICON[passport.decision];
  const failed       = passport.checks.filter((c) => c.result === "FAIL");
  const skipped      = passport.checks.filter((c) => c.skipped);
  // NA = not assessed (no applicable units / dependency off). Kept off the main
  // "Assessed" tab so PASS/FAIL aren't drowned out; skipped is its own bucket.
  const notAssessed  = passport.checks.filter((c) => c.result === "NA" && !c.skipped);
  const assessed     = passport.checks.filter((c) => c.result !== "NA" && !c.skipped);
  const prof         = passport.profile ?? {};
  const overallPct   = passport.overall_score != null ? Math.round(passport.overall_score) : null;

  const onFindingSaved = (f: Finding) =>
    setFindings((prev) => prev.map((x) => (x.id === f.id ? f : x)));

  return (
    <Tabs defaultValue="passport" className="space-y-6">
      <TabsList>
        <TabsTrigger value="passport">
          <BadgeCheck className="size-4" /> Quality Passport report
        </TabsTrigger>
        <TabsTrigger value="audit">
          <ScrollText className="size-4" /> Audit report
        </TabsTrigger>
      </TabsList>

      <TabsContent value="passport" className="space-y-6">
      {/* Decision banner */}
      <div className={`flex items-start gap-4 rounded-xl border p-5 ${style.banner}`}>
        <DecisionIcon className={`mt-0.5 size-8 shrink-0 ${style.accent}`} />
        <div className="flex-1">
          <div className="flex items-center gap-3">
            <span className={`text-xl font-bold tracking-tight ${style.accent}`}>{style.label}</span>
            {passport.degraded && (
              <Badge variant="outline" className="gap-1">
                <AlertTriangle className="size-3" /> advisory · gate degraded
              </Badge>
            )}
          </div>
          <p className="mt-1 text-sm text-muted-foreground">{style.blurb}</p>
          {passport.fitness?.statement && (
            <p className="mt-2 text-sm font-medium">{passport.fitness.statement}</p>
          )}
          {passport.coverage && <CoverageBadge coverage={passport.coverage} />}
        </div>
        <div className="text-right">
          {overallPct != null ? (
            <>
              <div className={`text-3xl font-bold tabular-nums ${rateTextClass(overallPct)}`}>{overallPct}%</div>
              <div className="text-xs text-muted-foreground">checks passing</div>
            </>
          ) : (
            <div className="text-sm text-muted-foreground">not assessed</div>
          )}
          {passport.overall_grade && (
            <div className="mt-1 text-2xl font-bold tabular-nums">grade {passport.overall_grade}</div>
          )}
        </div>
      </div>

      {/* EHDS dataset label */}
      {passport.label && <EhdsLabelCard label={passport.label} />}

      {/* KPI strip */}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        {[
          { label: "Resources",  value: passport.resource_count },
          { label: "Checks run", value: passport.checks.filter((c) => c.result !== "NA").length },
          { label: "Failed",     value: failed.length },
          { label: "Processing", value: passport.privacy_processing_allowed ? "allowed" : "blocked" },
        ].map((kpi) => (
          <Card key={kpi.label}>
            <CardContent className="py-4">
              <div className="text-2xl font-bold tabular-nums">{kpi.value}</div>
              <div className="text-xs text-muted-foreground">{kpi.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      {/* Quality pillars */}
      <div>
        <h2 className="mb-3 text-sm font-semibold uppercase tracking-wide text-muted-foreground">
          Data quality pillars
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {PILLAR_META.map(({ key, label, description, Icon }) => {
            const score       = passport.category_scores?.[key] ?? null;
            const pct         = score !== null ? Math.round(score) : null;
            const pillarChecks = passport.checks.filter((c) => c.category === key);
            const assessed     = pillarChecks.filter((c) => c.result !== "NA");
            const passedN      = assessed.filter((c) => c.result === "PASS").length;
            const failedN      = assessed.filter((c) => c.result === "FAIL").length;
            return (
              <PillarCard
                key={key}
                label={label}
                description={description}
                Icon={Icon}
                pct={pct}
                passed={passedN}
                failed={failedN}
                total={assessed.length}
              />
            );
          })}
        </div>
      </div>

      {/* Dimension scorecard + phases + sectors */}
      <Scorecard scorecard={passport.scorecard} />
      <PhasesCard phases={passport.phases} />
      <SectorsCard targets={passport.targets} />

      {/* Blockers */}
      {passport.blockers.length > 0 && (
        <Card className="border-destructive/40">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-destructive">
              <ShieldX className="size-4" /> Blockers
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {passport.blockers.map((b, i) => (
              <div key={i} className="rounded-md border border-destructive/30 bg-destructive/5 p-3 text-sm">
                {b}
              </div>
            ))}
          </CardContent>
        </Card>
      )}

      {/* Checks table */}
      <Card>
        <CardHeader><CardTitle>Checks</CardTitle></CardHeader>
        <CardContent>
          <Tabs defaultValue={failed.length ? "failed" : "assessed"}>
            <TabsList className="flex-wrap">
              <TabsTrigger value="assessed">Assessed ({assessed.length})</TabsTrigger>
              <TabsTrigger value="failed">Failed ({failed.length})</TabsTrigger>
              <TabsTrigger value="na">N/A ({notAssessed.length})</TabsTrigger>
              <TabsTrigger value="skipped">Skipped ({skipped.length})</TabsTrigger>
            </TabsList>
            <TabsContent value="assessed"><ChecksTable checks={assessed} /></TabsContent>
            <TabsContent value="failed"><ChecksTable checks={failed} /></TabsContent>
            <TabsContent value="na"><ChecksTable checks={notAssessed} /></TabsContent>
            <TabsContent value="skipped"><ChecksTable checks={skipped} /></TabsContent>
          </Tabs>
        </CardContent>
      </Card>

      {/* Findings, where/what (per-record) */}
      <FindingsCard checks={passport.checks} />

      {/* Remediation findings, interactive triage (server-derived) */}
      {findings.length > 0 ? (
        <RemediationTriage findings={findings} checks={passport.checks} onChange={onFindingSaved} />
      ) : passport.checks.some((c) => c.result === "FAIL") && (
        <p className="text-xs text-muted-foreground">
          Persistent finding triage (status, root-cause, notes) requires{" "}
          <code>TRUST_GATE_STORE_DB</code> or <code>TRUST_GATE_STORE_DB_URL</code> to be
          configured on the Trust Gate service. Configure it to retain an append-only audit trail.
        </p>
      )}

      {/* Clinical value distributions (histogram + age x sex stratification) */}
      {(prof.observation_value_stats?.length ?? 0) > 0 && (
        <ClinicalDistributions
          stats={prof.observation_value_stats!}
          stratified={prof.observation_value_stats_stratified}
        />
      )}

      {/* Auditability + fitness */}
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader><CardTitle>Auditability</CardTitle></CardHeader>
          <CardContent><KeyVals obj={passport.auditability} /></CardContent>
        </Card>
        <Card>
          <CardHeader><CardTitle>Fitness for use</CardTitle></CardHeader>
          <CardContent className="space-y-2 text-sm">
            {passport.approved_for.map((u) => (
              <div key={u} className="flex items-center gap-2 text-emerald-600 dark:text-emerald-400">
                <CheckCircle2 className="size-4 shrink-0" /> {u}
              </div>
            ))}
            {passport.not_approved_for.map((u) => (
              <div key={u} className="flex items-center gap-2 text-amber-600 dark:text-amber-400">
                <AlertTriangle className="size-4 shrink-0" /> not approved: {u}
              </div>
            ))}
            {!passport.approved_for.length && !passport.not_approved_for.length && (
              <p className="text-muted-foreground">No fitness determination.</p>
            )}
          </CardContent>
        </Card>
      </div>

      {/* Data profile (descriptive, value distributions live in their own card) */}
      {Object.keys(prof.resource_counts ?? {}).length > 0 && (
        <Card>
          <CardHeader>
            <CardTitle>
              Data profile{" "}
              <span className="text-sm font-normal text-muted-foreground">(descriptive, not scored)</span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-5">
            {/* Resource type bar chart */}
            {prof.resource_counts && Object.keys(prof.resource_counts).length > 0 && (
              <div className="space-y-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Resource types
                </p>
                <div className="space-y-1.5">
                  {Object.entries(prof.resource_counts)
                    .sort(([, a], [, b]) => b - a)
                    .map(([type, count]) => {
                      const total = Object.values(prof.resource_counts!).reduce((s, v) => s + v, 0);
                      const pct   = total > 0 ? (count / total) * 100 : 0;
                      return (
                        <div key={type}>
                          <div className="mb-0.5 flex items-center justify-between text-xs">
                            <span className="font-medium">{type}</span>
                            <span className="tabular-nums text-muted-foreground">
                              {count.toLocaleString()}
                            </span>
                          </div>
                          <div className="h-1.5 overflow-hidden rounded-full bg-muted">
                            <div
                              className="h-full rounded-full bg-primary/60"
                              style={{ width: `${pct}%` }}
                            />
                          </div>
                        </div>
                      );
                    })}
                </div>
              </div>
            )}

            {/* Code systems */}
            {prof.code_system_distribution && Object.keys(prof.code_system_distribution).length > 0 && (
              <div>
                <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Code systems
                </p>
                <div className="flex flex-wrap gap-1.5">
                  {Object.entries(prof.code_system_distribution).map(([uri, count]) => (
                    <Badge key={uri} variant="secondary" className="tabular-nums">
                      {codeSystemLabel(uri)} ({count.toLocaleString()})
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            {/* Gender distribution */}
            {prof.patient_gender_distribution &&
              Object.keys(prof.patient_gender_distribution).length > 0 && (
                <div>
                  <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
                    Patient gender
                  </p>
                  <div className="flex gap-4">
                    {Object.entries(prof.patient_gender_distribution).map(([g, n]) => (
                      <div key={g} className="text-sm">
                        <span className="capitalize font-medium">{g}</span>
                        <span className="ml-1.5 tabular-nums text-muted-foreground">{n}</span>
                      </div>
                    ))}
                  </div>
                </div>
              )}

            {/* Reference density */}
            {prof.reference_density !== undefined && (
              <div className="flex items-center justify-between text-sm">
                <span className="text-muted-foreground">Reference density</span>
                <span className="font-medium tabular-nums">{prof.reference_density}</span>
              </div>
            )}
            {/* Clinical value distributions are rendered by ClinicalDistributions
                above (histogram + age x sex stratification), not duplicated here. */}
          </CardContent>
        </Card>
      )}

      {/* Framework + raw views */}
      <Card>
        <CardContent className="space-y-3 py-4">
          <div className="flex flex-wrap items-center gap-1.5">
            {Object.entries(passport.framework_versions ?? {}).map(([k, v]) => (
              <Badge key={k} variant="outline" className="font-mono text-[10px]">
                {k}: {v}
              </Badge>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">{passport.framework}</p>

          {passport.report && (
            <Collapsible open={reportOpen} onOpenChange={setReportOpen}>
              <CollapsibleTrigger className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
                <FileText className="size-4" /> Full passport report
                <ChevronDown className={`size-4 transition-transform ${reportOpen ? "rotate-180" : ""}`} />
              </CollapsibleTrigger>
              <CollapsibleContent>
                <div className="mt-2 max-h-96 overflow-auto rounded-md border bg-muted/40 p-3">
                  <MarkdownReport markdown={passport.report} />
                </div>
              </CollapsibleContent>
            </Collapsible>
          )}

          <Collapsible open={jsonOpen} onOpenChange={setJsonOpen}>
            <CollapsibleTrigger className="flex items-center gap-2 text-sm text-muted-foreground hover:text-foreground">
              <Braces className="size-4" /> Raw passport JSON
              <ChevronDown className={`size-4 transition-transform ${jsonOpen ? "rotate-180" : ""}`} />
            </CollapsibleTrigger>
            <CollapsibleContent>
              <pre className="mt-2 max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed">
                {JSON.stringify(passport, null, 2)}
              </pre>
            </CollapsibleContent>
          </Collapsible>
        </CardContent>
      </Card>
      </TabsContent>

      <TabsContent value="audit">
        <AuditReport passport={passport} audit={audit} />
      </TabsContent>
    </Tabs>
  );
}
