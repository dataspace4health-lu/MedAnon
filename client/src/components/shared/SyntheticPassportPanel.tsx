import { useState } from "react";
import { toast } from "sonner";
import { ShieldCheck, Loader2, BadgeCheck, AlertTriangle, XCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  synthesizePassport,
  parseResourceArray,
  type SyntheticPassport,
} from "@/api/analytics";

const VERDICT: Record<
  SyntheticPassport["verdict"]["overall"],
  { label: string; icon: typeof BadgeCheck; className: string }
> = {
  release: {
    label: "Release",
    icon: BadgeCheck,
    className: "bg-emerald-600 text-white hover:bg-emerald-600",
  },
  review: {
    label: "Review",
    icon: AlertTriangle,
    className: "bg-amber-500 text-white hover:bg-amber-500",
  },
  reject: {
    label: "Reject",
    icon: XCircle,
    className: "bg-destructive text-white hover:bg-destructive",
  },
};

/**
 * On-demand fidelity + privacy passport for a generated synthetic dataset
 * (D7.2 §5.4/§5.5). Compares the synthetic output against the real source:
 * privacy is a hard gate (any exact real/synthetic duplicate → reject),
 * fidelity is graded A–F on per-column marginal similarity.
 */
export function SyntheticPassportPanel({
  realText,
  syntheticText,
}: {
  realText: string;
  syntheticText: string;
}) {
  const [loading, setLoading] = useState(false);
  const [passport, setPassport] = useState<SyntheticPassport | null>(null);

  async function assess() {
    setLoading(true);
    setPassport(null);
    try {
      const real = parseResourceArray(realText);
      const synthetic = parseResourceArray(syntheticText);
      if (real.length === 0 || synthetic.length === 0) {
        toast.error("Need both real and synthetic resources to assess.");
        return;
      }
      setPassport(await synthesizePassport(real, synthetic));
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Passport assessment failed");
    } finally {
      setLoading(false);
    }
  }

  const dist = passport?.privacy.distance;
  const attr = passport?.privacy.attribute_inference;

  return (
    <Card className="mt-4">
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-base">
          <ShieldCheck className="h-4 w-4 text-muted-foreground" />
          Privacy + Fidelity Passport
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-4">
        {!passport && (
          <div className="flex items-center justify-between gap-4">
            <p className="text-sm text-muted-foreground">
              Assess whether this synthetic set is safe to release: fidelity
              grade + a privacy verdict (record-level distance, exact-duplicate
              leak, attribute inference) against the real source.
            </p>
            <Button onClick={assess} disabled={loading}>
              {loading ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <ShieldCheck className="mr-2 h-4 w-4" />
              )}
              Assess
            </Button>
          </div>
        )}

        {passport && (
          <div className="space-y-4">
            {/* Verdict */}
            <div className="flex flex-wrap items-center gap-3">
              {(() => {
                const v = VERDICT[passport.verdict.overall];
                const Icon = v.icon;
                return (
                  <Badge className={v.className}>
                    <Icon className="mr-1.5 h-3.5 w-3.5" />
                    {v.label}
                  </Badge>
                );
              })()}
              <span className="text-sm text-muted-foreground">
                {passport.record_counts.real} real vs{" "}
                {passport.record_counts.synthetic} synthetic
              </span>
            </div>

            <div className="grid gap-3 sm:grid-cols-3">
              <Metric
                label="Fidelity"
                value={`${passport.verdict.fidelity_grade} · ${(
                  passport.fidelity.mean_fidelity * 100
                ).toFixed(0)}%`}
                hint="per-column marginal similarity"
              />
              <Metric
                label="Privacy gate"
                value={passport.verdict.privacy_pass ? "Pass" : "Fail"}
                tone={passport.verdict.privacy_pass ? "ok" : "bad"}
                hint={
                  dist?.computed
                    ? `${dist.exact_duplicates ?? 0} exact duplicate(s)`
                    : (dist?.reason ?? "")
                }
              />
              <Metric
                label="Closest-record distance"
                value={
                  dist?.computed && dist.dcr_mean !== undefined
                    ? dist.dcr_mean.toFixed(3)
                    : "n/a"
                }
                hint={
                  dist?.computed
                    ? `NNDR ${dist.nndr_mean?.toFixed(3) ?? "?"} · τ-share ${(
                        (dist.tau_dcr_share ?? 0) * 100
                      ).toFixed(0)}%`
                    : "no comparison"
                }
              />
            </div>

            {attr?.computed && attr.attribute_inference && (
              <div className="text-sm">
                <div className="mb-1 font-medium">Attribute-inference protection</div>
                <div className="flex flex-wrap gap-2">
                  {Object.entries(attr.attribute_inference).map(([k, v]) => (
                    <Badge key={k} variant="outline" className="font-mono text-xs">
                      {k}: {typeof v === "number" ? v.toFixed(3) : "n/a"}
                    </Badge>
                  ))}
                </div>
              </div>
            )}

            <Button variant="ghost" size="sm" onClick={assess} disabled={loading}>
              {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Re-assess
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function Metric({
  label,
  value,
  hint,
  tone,
}: {
  label: string;
  value: string;
  hint?: string;
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
      {hint && <div className="mt-0.5 text-xs text-muted-foreground">{hint}</div>}
    </div>
  );
}
