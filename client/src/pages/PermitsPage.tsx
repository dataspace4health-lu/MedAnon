import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import {
  Plus,
  ScrollText,
  Loader2,
  Send,
  CheckCircle2,
  XCircle,
  Ban,
  ShieldCheck,
} from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  listPermits,
  createPermit,
  submitPermit,
  approvePermit,
  rejectPermit,
  revokePermit,
  isPermitActive,
  type Permit,
  type PermitStatus,
} from "@/api/permits";

const EMPTY_FORM = {
  purpose: "",
  legal_basis: "",
  controller: "",
  recipient: "",
  allowed_paths: "",
  restrictions: "",
  valid_from: "",
  valid_until: "",
};

type FormState = typeof EMPTY_FORM;

/** Status → Badge styling. Approved gets an emerald tint; terminal states muted. */
const STATUS_STYLE: Record<
  PermitStatus,
  { variant: "default" | "secondary" | "destructive" | "outline"; className?: string }
> = {
  draft: { variant: "outline" },
  submitted: { variant: "secondary" },
  approved: {
    variant: "default",
    className: "bg-emerald-600 hover:bg-emerald-600 text-white",
  },
  rejected: { variant: "destructive" },
  revoked: { variant: "destructive", className: "opacity-70" },
};

function StatusBadge({ status }: { status: PermitStatus }) {
  const s = STATUS_STYLE[status];
  return (
    <Badge variant={s.variant} className={s.className}>
      {status}
    </Badge>
  );
}

function toLines(v: string): string[] {
  return v
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
}

export default function PermitsPage() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [permits, setPermits] = useState<Permit[]>([]);
  const [loading, setLoading] = useState(true);
  const [creating, setCreating] = useState(false);
  const [showCreate, setShowCreate] = useState(false);
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [busyId, setBusyId] = useState<string | null>(null);
  // Reason-gated transitions (reject / revoke) run through the confirm dialog.
  const [reasonAction, setReasonAction] = useState<{
    id: string;
    action: "reject" | "revoke";
  } | null>(null);
  const [reason, setReason] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setPermits(await listPermits());
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to load permits");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isAdmin) load();
    else setLoading(false);
  }, [isAdmin, load]);

  const setField = (k: keyof FormState, v: string) =>
    setForm((f) => ({ ...f, [k]: v }));

  async function handleCreate() {
    if (!form.purpose.trim()) {
      toast.error("Purpose is required.");
      return;
    }
    setCreating(true);
    try {
      await createPermit({
        purpose: form.purpose.trim(),
        legal_basis: form.legal_basis.trim(),
        controller: form.controller.trim(),
        recipient: form.recipient.trim(),
        allowed_paths: toLines(form.allowed_paths),
        restrictions: toLines(form.restrictions),
        valid_from: form.valid_from || null,
        valid_until: form.valid_until || null,
      });
      toast.success("Permit created (draft).");
      setShowCreate(false);
      setForm(EMPTY_FORM);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to create permit");
    } finally {
      setCreating(false);
    }
  }

  async function runTransition(
    id: string,
    fn: () => Promise<Permit>,
    okMsg: string,
  ) {
    setBusyId(id);
    try {
      await fn();
      toast.success(okMsg);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Transition failed");
    } finally {
      setBusyId(null);
    }
  }

  async function confirmReason() {
    if (!reasonAction) return;
    const { id, action } = reasonAction;
    const r = reason.trim();
    setReasonAction(null);
    setReason("");
    await runTransition(
      id,
      () => (action === "reject" ? rejectPermit(id, r) : revokePermit(id, r)),
      action === "reject" ? "Permit rejected." : "Permit revoked.",
    );
  }

  if (!isAdmin) {
    return (
      <div className="space-y-6">
        <PageHeader
          title="Data Permits"
          description="Governance authorisations for scoped, permit-bound de-identification."
        />
        <Card>
          <CardContent className="py-10 text-center text-sm text-muted-foreground">
            <ShieldCheck className="mx-auto mb-3 h-8 w-8 opacity-40" />
            Data-permit governance is restricted to administrators.
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Data Permits"
        description="Create and approve data permits (TEHDAS2 D7.2 §2 / EHDS). An APPROVED permit can be attached to an export job so pseudonymisation is scoped to it, the same subject cannot be linked across permits."
        actions={
          <Button onClick={() => setShowCreate(true)}>
            <Plus className="mr-2 h-4 w-4" />
            New Permit
          </Button>
        }
      />

      {loading ? (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          Loading permits…
        </div>
      ) : permits.length === 0 ? (
        <Card>
          <CardContent className="py-12 text-center text-sm text-muted-foreground">
            <ScrollText className="mx-auto mb-3 h-8 w-8 opacity-40" />
            No permits yet. Create one to authorise a scoped, permit-bound export.
          </CardContent>
        </Card>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {permits.map((p) => {
            const busy = busyId === p.id;
            return (
              <Card key={p.id} className="flex flex-col">
                <CardHeader className="pb-3">
                  <div className="flex items-start justify-between gap-2">
                    <CardTitle className="text-base leading-snug">
                      {p.purpose || "(no purpose)"}
                    </CardTitle>
                    <StatusBadge status={p.status} />
                  </div>
                  <CardDescription className="font-mono text-xs">
                    {p.id}
                  </CardDescription>
                </CardHeader>
                <CardContent className="flex-1 space-y-1.5 text-sm">
                  {p.recipient && (
                    <Row label="Recipient" value={p.recipient} />
                  )}
                  {p.controller && (
                    <Row label="Controller" value={p.controller} />
                  )}
                  {p.legal_basis && (
                    <Row label="Legal basis" value={p.legal_basis} />
                  )}
                  {(p.valid_from || p.valid_until) && (
                    <Row
                      label="Validity"
                      value={`${p.valid_from?.slice(0, 10) ?? "…"} → ${
                        p.valid_until?.slice(0, 10) ?? "…"
                      }`}
                    />
                  )}
                  {p.allowed_paths.length > 0 && (
                    <Row
                      label="Scope"
                      value={`${p.allowed_paths.length} path(s)`}
                    />
                  )}
                  {p.status === "approved" && (
                    <div className="pt-1">
                      <Badge
                        variant="outline"
                        className={
                          isPermitActive(p)
                            ? "border-emerald-600 text-emerald-700 dark:text-emerald-400"
                            : "border-amber-500 text-amber-700 dark:text-amber-400"
                        }
                      >
                        {isPermitActive(p) ? "active now" : "outside validity window"}
                      </Badge>
                    </div>
                  )}
                  {p.decision_reason && (
                    <p className="pt-1 text-xs italic text-muted-foreground">
                      "{p.decision_reason}"
                    </p>
                  )}
                </CardContent>
                <CardFooter className="flex flex-wrap gap-2 pt-3">
                  {p.status === "draft" && (
                    <Button
                      size="sm"
                      variant="secondary"
                      disabled={busy}
                      onClick={() =>
                        runTransition(p.id, () => submitPermit(p.id), "Permit submitted.")
                      }
                    >
                      {busy ? (
                        <Loader2 className="mr-1.5 h-3.5 w-3.5 animate-spin" />
                      ) : (
                        <Send className="mr-1.5 h-3.5 w-3.5" />
                      )}
                      Submit
                    </Button>
                  )}
                  {p.status === "submitted" && (
                    <>
                      <Button
                        size="sm"
                        disabled={busy}
                        className="bg-emerald-600 hover:bg-emerald-700"
                        onClick={() =>
                          runTransition(
                            p.id,
                            () => approvePermit(p.id),
                            "Permit approved.",
                          )
                        }
                      >
                        <CheckCircle2 className="mr-1.5 h-3.5 w-3.5" />
                        Approve
                      </Button>
                      <Button
                        size="sm"
                        variant="destructive"
                        disabled={busy}
                        onClick={() => {
                          setReason("");
                          setReasonAction({ id: p.id, action: "reject" });
                        }}
                      >
                        <XCircle className="mr-1.5 h-3.5 w-3.5" />
                        Reject
                      </Button>
                    </>
                  )}
                  {p.status === "approved" && (
                    <Button
                      size="sm"
                      variant="destructive"
                      disabled={busy}
                      onClick={() => {
                        setReason("");
                        setReasonAction({ id: p.id, action: "revoke" });
                      }}
                    >
                      <Ban className="mr-1.5 h-3.5 w-3.5" />
                      Revoke
                    </Button>
                  )}
                  {(p.status === "rejected" || p.status === "revoked") && (
                    <span className="text-xs text-muted-foreground">
                      Terminal, no further transitions.
                    </span>
                  )}
                </CardFooter>
              </Card>
            );
          })}
        </div>
      )}

      {/* Create dialog */}
      <Dialog open={showCreate} onOpenChange={setShowCreate}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>New data permit</DialogTitle>
            <DialogDescription>
              Created in <span className="font-medium">draft</span>. Submit then
              approve it before it can authorise an export.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-4">
            <Field label="Purpose" required>
              <Input
                value={form.purpose}
                onChange={(e) => setField("purpose", e.target.value)}
                placeholder="e.g. Retrospective diabetes outcomes study"
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Legal basis">
                <Input
                  value={form.legal_basis}
                  onChange={(e) => setField("legal_basis", e.target.value)}
                  placeholder="GDPR Art 6(1)(e)"
                />
              </Field>
              <Field label="Controller">
                <Input
                  value={form.controller}
                  onChange={(e) => setField("controller", e.target.value)}
                  placeholder="HDAB-X"
                />
              </Field>
            </div>
            <Field label="Recipient (approved data user)">
              <Input
                value={form.recipient}
                onChange={(e) => setField("recipient", e.target.value)}
                placeholder="research-team-42"
              />
            </Field>
            <div className="grid grid-cols-2 gap-3">
              <Field label="Valid from">
                <Input
                  type="date"
                  value={form.valid_from}
                  onChange={(e) => setField("valid_from", e.target.value)}
                />
              </Field>
              <Field label="Valid until">
                <Input
                  type="date"
                  value={form.valid_until}
                  onChange={(e) => setField("valid_until", e.target.value)}
                />
              </Field>
            </div>
            <Field label="Allowed paths (one FHIRPath per line; empty = unrestricted)">
              <Textarea
                rows={3}
                value={form.allowed_paths}
                onChange={(e) => setField("allowed_paths", e.target.value)}
                placeholder={"Patient.birthDate\nPatient.gender\nCondition.code"}
                className="font-mono text-xs"
              />
            </Field>
            <Field label="Restrictions (one per line, optional)">
              <Textarea
                rows={2}
                value={form.restrictions}
                onChange={(e) => setField("restrictions", e.target.value)}
                placeholder="no-reversal"
                className="text-xs"
              />
            </Field>
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setShowCreate(false)}>
              Cancel
            </Button>
            <Button onClick={handleCreate} disabled={creating}>
              {creating && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
              Create permit
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* Reason-gated reject / revoke */}
      <Dialog
        open={reasonAction !== null}
        onOpenChange={(o) => {
          if (!o) setReasonAction(null);
        }}
      >
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>
              {reasonAction?.action === "reject"
                ? "Reject permit"
                : "Revoke permit"}
            </DialogTitle>
            <DialogDescription>
              {reasonAction?.action === "reject"
                ? "The applicant will need to resubmit."
                : "Revoking immediately deactivates the permit for any bound export."}
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <label className="text-sm font-medium">Reason (optional)</label>
            <Textarea
              rows={3}
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="Recorded on the permit's decision trail."
            />
          </div>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setReasonAction(null)}>
              Cancel
            </Button>
            <Button variant="destructive" onClick={confirmReason}>
              {reasonAction?.action === "reject" ? "Reject" : "Revoke"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
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

function Field({
  label,
  required,
  children,
}: {
  label: string;
  required?: boolean;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-1.5">
      <label className="text-sm font-medium">
        {label}
        {required && <span className="ml-0.5 text-destructive">*</span>}
      </label>
      {children}
    </div>
  );
}
