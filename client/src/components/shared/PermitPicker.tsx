import { useEffect, useState } from "react";
import { ScrollText, AlertTriangle } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { listPermits, isPermitActive, type Permit } from "@/api/permits";

const NONE = "__none__";

/**
 * Selects an ACTIVE data permit to bind an export job to (D7.2 §4.4).
 *
 * Only APPROVED permits currently inside their validity window are offered.
 * Binding to an inactive permit would be refused by the backend. Admin-only
 * data, so a non-admin (403 on list) simply sees no picker. The chosen id is
 * surfaced via ``onChange`` (``null`` = unbound / no permit scoping).
 */
export function PermitPicker({
  value,
  onChange,
  disabled,
}: {
  value: string | null;
  onChange: (permitId: string | null) => void;
  disabled?: boolean;
}) {
  const [permits, setPermits] = useState<Permit[] | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const all = await listPermits();
        if (!cancelled) setPermits(all.filter((p) => isPermitActive(p)));
      } catch {
        // 403 for non-admins, or the store is unavailable, hide the picker.
        if (!cancelled) setForbidden(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Non-admin / unavailable → render nothing (opt-in feature, never blocks the form).
  if (forbidden) return null;

  const active = permits ?? [];

  return (
    <div>
      <label className="mb-1 flex items-center gap-1.5 text-sm font-medium">
        <ScrollText className="h-3.5 w-3.5 text-muted-foreground" />
        Data Permit{" "}
        <span className="font-normal text-muted-foreground">
          (optional, scopes pseudonymisation to an authorised use, D7.2 §4.4)
        </span>
      </label>
      <Select
        value={value ?? NONE}
        onValueChange={(v) => onChange(v === NONE ? null : v)}
        disabled={disabled}
      >
        <SelectTrigger>
          <SelectValue placeholder="No permit (unbound)" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>No permit (unbound)</SelectItem>
          {active.map((p) => (
            <SelectItem key={p.id} value={p.id}>
              {(p.purpose || p.id)}
              {p.recipient ? ` → ${p.recipient}` : ""}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {permits !== null && active.length === 0 && (
        <p className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
          <AlertTriangle className="h-3 w-3" />
          No active permits. Create and approve one under Govern → Data Permits.
        </p>
      )}
    </div>
  );
}
