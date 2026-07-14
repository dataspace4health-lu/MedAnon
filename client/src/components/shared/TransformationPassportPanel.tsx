import { FileText, Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import type { TransformationPassport } from "@/api/medanon";

/**
 * Renders the Transformation Passport (D7.2 §5.5.1) a risk-driven export
 * produced: the anonymous, machine-readable documentation of *what was done* to
 * the treated data, permit binding, privacy model + achieved k/l/t, tools, the
 * privacy-risk assessment, and the disclosure decision. Downloadable as JSON.
 */
export function TransformationPassportPanel({
  passport,
}: {
  passport: TransformationPassport;
}) {
  const gen = passport.processing.generalization;
  const pm = passport.processing.privacy_model as
    | { target_k?: number; target_l?: number; target_t?: number }
    | null;
  const reid =
    (passport.privacy_risk_assessment?.re_identification as
      | { summary?: { min_k?: number; risk_level?: string } }
      | undefined) ?? undefined;
  const orig = passport.original_dataset as
    | { total_resources?: number; released?: number; optout_excluded?: number }
    | null;

  function download() {
    const blob = new Blob([JSON.stringify(passport, null, 2)], {
      type: "application/json",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `transformation-passport-${passport.identification.job_id ?? "job"}.json`;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Card>
      <CardHeader className="pb-3">
        <div className="flex items-start justify-between gap-2">
          <CardTitle className="flex items-center gap-2 text-base">
            <FileText className="h-4 w-4 text-muted-foreground" />
            Transformation Passport
          </CardTitle>
          <Button variant="outline" size="sm" onClick={download}>
            <Download className="mr-1.5 h-3.5 w-3.5" />
            JSON
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {/* Identification */}
        <Section title="Authorisation">
          <Row label="Permit" value={passport.identification.permit_id ?? "unbound"} />
          <Row label="Created by" value={passport.identification.data_creator} />
          {passport.identification.hash_key_id && (
            <Row label="Key generation" value={passport.identification.hash_key_id} />
          )}
          <Row label="Generated" value={passport.generated_at.slice(0, 19).replace("T", " ")} />
        </Section>

        {/* Dataset */}
        {orig && (
          <Section title="Dataset">
            {orig.total_resources != null && (
              <Row label="Input resources" value={String(orig.total_resources)} />
            )}
            {orig.released != null && (
              <Row label="Released" value={String(orig.released)} />
            )}
            {orig.optout_excluded != null && (
              <Row label="Opt-out excluded" value={String(orig.optout_excluded)} />
            )}
          </Section>
        )}

        {/* Privacy model: intent vs achieved */}
        <Section title="Privacy model">
          <div className="grid grid-cols-2 gap-x-6 gap-y-1">
            <Row label="Target k" value={pm?.target_k != null ? String(pm.target_k) : ""} />
            <Row label="Achieved k" value={gen?.achieved_k != null ? String(gen.achieved_k) : ""} />
            {(pm?.target_l != null || gen?.achieved_l != null) && (
              <>
                <Row label="Target l" value={pm?.target_l != null ? String(pm.target_l) : ""} />
                <Row label="Achieved l" value={gen?.achieved_l != null ? String(gen.achieved_l) : ""} />
              </>
            )}
            {gen?.suppressed_count != null && (
              <Row label="Suppressed" value={String(gen.suppressed_count)} />
            )}
            {gen?.feasible != null && (
              <Row label="Feasible" value={gen.feasible ? "yes" : "no"} />
            )}
          </div>
        </Section>

        {/* Privacy-risk assessment */}
        {reid?.summary && (
          <Section title="Privacy-risk assessment">
            <Row label="Min k (output)" value={String(reid.summary.min_k ?? "")} />
            {reid.summary.risk_level && (
              <Row
                label="Re-identification risk"
                value={reid.summary.risk_level}
              />
            )}
          </Section>
        )}

        {/* Disclosure decision */}
        {passport.disclosure && (
          <Section title="Disclosure decision">
            <Badge
              className={
                passport.disclosure.decision === "approve"
                  ? "bg-emerald-600 text-white hover:bg-emerald-600"
                  : passport.disclosure.decision === "refer"
                    ? "bg-amber-500 text-white hover:bg-amber-500"
                    : "bg-destructive text-white hover:bg-destructive"
              }
            >
              {passport.disclosure.decision}
            </Badge>
            {passport.disclosure.checks.length > 0 && (
              <ul className="mt-1.5 space-y-0.5 text-xs text-muted-foreground">
                {passport.disclosure.checks.map((c, i) => (
                  <li key={i}>
                    <span className="font-mono">{c.rule}</span> → {c.outcome}
                  </li>
                ))}
              </ul>
            )}
          </Section>
        )}

        {/* Tools (provenance + approval, §5.5.8) */}
        {passport.processing.tools.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
            <span>
              Produced by{" "}
              {passport.processing.tools
                .map((t) => `${t.name} ${t.version}`)
                .join(", ")}
              {passport.processing.config_profile
                ? ` · profile ${passport.processing.config_profile}`
                : ""}
            </span>
            {passport.processing.tool_assessment && (
              <Badge
                variant="outline"
                className={
                  passport.processing.tool_assessment.overall_status === "approved"
                    ? "border-emerald-600 text-emerald-700 dark:text-emerald-400"
                    : "border-amber-500 text-amber-700 dark:text-amber-400"
                }
              >
                tools: {passport.processing.tool_assessment.overall_status}
              </Badge>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="mb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
      </div>
      {children}
    </div>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-muted-foreground">{label}</span>
      <span className="truncate text-right font-medium">{value}</span>
    </div>
  );
}
