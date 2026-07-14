import { useEffect, useState } from "react";
import { Database, HardDriveUpload } from "lucide-react";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  listDestinations,
  listSources,
  type OutputDestination,
  type SourceConnection,
} from "@/api/connectors";

const NONE = "__none__";

/**
 * Selects a saved input source for an export job. When chosen, the backend uses
 * the source's server URL and resolves its stored bearer token server-side. A
 * non-admin (403 on list) simply sees no picker. ``null`` = use the request's
 * own server_url / defaults.
 */
export function SourcePicker({
  value,
  onChange,
  disabled,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  disabled?: boolean;
}) {
  const [items, setItems] = useState<SourceConnection[] | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const all = await listSources("source");
        if (!cancelled) setItems(all);
      } catch {
        if (!cancelled) setForbidden(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (forbidden) return null;
  const sources = items ?? [];

  return (
    <div>
      <label className="mb-1 flex items-center gap-1.5 text-sm font-medium">
        <Database className="h-3.5 w-3.5 text-muted-foreground" />
        Input Source{" "}
        <span className="font-normal text-muted-foreground">
          (optional, overrides the URL above with a saved source)
        </span>
      </label>
      <Select
        value={value ?? NONE}
        onValueChange={(v) => onChange(v === NONE ? null : v)}
        disabled={disabled}
      >
        <SelectTrigger>
          <SelectValue placeholder="Use URL above / default" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>Use URL above / default</SelectItem>
          {sources.map((s) => (
            <SelectItem key={s.id} value={s.id}>
              {s.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

/**
 * Selects a saved S3 output destination. The de-identified file is delivered
 * there. ``null`` = the deployment default (MEDANON_DEFAULT_DESTINATION_ID); if
 * none resolves and MEDANON_REQUIRE_S3_DELIVERY=true the job fails closed.
 */
export function DestinationPicker({
  value,
  onChange,
  disabled,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  disabled?: boolean;
}) {
  const [items, setItems] = useState<OutputDestination[] | null>(null);
  const [forbidden, setForbidden] = useState(false);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const all = await listDestinations();
        if (!cancelled) setItems(all);
      } catch {
        if (!cancelled) setForbidden(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (forbidden) return null;
  const dests = items ?? [];
  const chosen = dests.find((d) => d.id === value) ?? null;

  return (
    <div>
      <label className="mb-1 flex items-center gap-1.5 text-sm font-medium">
        <HardDriveUpload className="h-3.5 w-3.5 text-muted-foreground" />
        S3 Output Destination{" "}
        <span className="font-normal text-muted-foreground">
          (the de-identified file is delivered here)
        </span>
      </label>
      <Select
        value={value ?? NONE}
        onValueChange={(v) => onChange(v === NONE ? null : v)}
        disabled={disabled}
      >
        <SelectTrigger>
          <SelectValue placeholder="Deployment default" />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={NONE}>Deployment default</SelectItem>
          {dests.map((d) => (
            <SelectItem key={d.id} value={d.id}>
              {d.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
      {chosen && (
        <p className="mt-1 truncate text-xs text-muted-foreground">
          {`s3://${chosen.bucket}/${chosen.key_prefix}`}
        </p>
      )}
    </div>
  );
}
