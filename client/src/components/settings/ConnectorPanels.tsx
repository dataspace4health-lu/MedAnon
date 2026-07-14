import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  HardDriveUpload,
  Loader2,
  Plus,
  Trash2,
  CheckCircle2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Checkbox } from "@/components/ui/checkbox";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import {
  type OutputDestination,
  createDestination,
  deleteDestination,
  listDestinations,
  testDestination,
} from "@/api/connectors";

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/** Small labelled field wrapper matching the app's form density. */
function Field({
  label,
  children,
  hint,
}: {
  label: string;
  children: React.ReactNode;
  hint?: string;
}) {
  return (
    <label className="block space-y-1 text-sm">
      <span className="font-medium">{label}</span>
      {children}
      {hint && <span className="block text-xs text-muted-foreground">{hint}</span>}
    </label>
  );
}

// ── S3 output destinations ───────────────────────────────────────────────────

const EMPTY_DEST = {
  name: "",
  endpoint: "",
  bucket: "",
  access_key: "",
  secret_key: "",
  region: "",
  key_prefix: "",
  secure: true,
  path_style: false,
};

export function DestinationsPanel() {
  const [items, setItems] = useState<OutputDestination[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [form, setForm] = useState({ ...EMPTY_DEST });

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setItems(await listDestinations());
    } catch (e) {
      toast.error("Could not load destinations.", { description: errMsg(e) });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onCreate = async () => {
    if (
      !form.name.trim() ||
      !form.endpoint.trim() ||
      !form.bucket.trim() ||
      !form.access_key.trim() ||
      !form.secret_key.trim()
    ) {
      toast.error("Name, endpoint, bucket, access key, and secret key are required.");
      return;
    }
    setBusy("create");
    try {
      await createDestination({
        name: form.name.trim(),
        endpoint: form.endpoint.trim(),
        bucket: form.bucket.trim(),
        access_key: form.access_key.trim(),
        secret_key: form.secret_key,
        region: form.region.trim() || null,
        key_prefix: form.key_prefix.trim(),
        secure: form.secure,
        path_style: form.path_style,
      });
      toast.success(`S3 destination "${form.name}" saved.`);
      setForm({ ...EMPTY_DEST });
      await refresh();
    } catch (e) {
      toast.error("Could not save destination.", { description: errMsg(e) });
    } finally {
      setBusy(null);
    }
  };

  const onTest = async (id: string) => {
    setBusy(id);
    try {
      const res = await testDestination(id);
      toast.success("Destination reachable.", {
        description: res.created
          ? `Bucket "${res.bucket}" created.`
          : `Bucket "${res.bucket}" exists.`,
      });
    } catch (e) {
      toast.error("Destination test failed.", { description: errMsg(e) });
    } finally {
      setBusy(null);
    }
  };

  const onDelete = async (id: string, name: string) => {
    setBusy(id);
    try {
      await deleteDestination(id);
      toast.success(`Removed "${name}".`);
      await refresh();
    } catch (e) {
      toast.error("Could not remove destination.", { description: errMsg(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <HardDriveUpload className="size-4" /> S3 output destinations
        </CardTitle>
        <CardDescription>
          The de-identified export is always delivered to S3. The secret key is
          encrypted at rest and never returned by the API.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Name">
            <Input
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
              placeholder="Dataspace bucket (prod)"
            />
          </Field>
          <Field label="Endpoint" hint="host:port (no scheme)">
            <Input
              value={form.endpoint}
              onChange={(e) => setForm({ ...form, endpoint: e.target.value })}
              placeholder="s3.eu-central-1.amazonaws.com"
            />
          </Field>
          <Field label="Bucket">
            <Input
              value={form.bucket}
              onChange={(e) => setForm({ ...form, bucket: e.target.value })}
              placeholder="dataspace-deidentified"
            />
          </Field>
          <Field label="Region (optional)">
            <Input
              value={form.region}
              onChange={(e) => setForm({ ...form, region: e.target.value })}
              placeholder="eu-central-1"
            />
          </Field>
          <Field label="Access key">
            <Input
              value={form.access_key}
              onChange={(e) => setForm({ ...form, access_key: e.target.value })}
            />
          </Field>
          <Field
            label="Secret key"
            hint="Write-only. Never returned by the API."
          >
            <Input
              type="password"
              value={form.secret_key}
              onChange={(e) => setForm({ ...form, secret_key: e.target.value })}
            />
          </Field>
          <Field
            label="Key prefix / template (optional)"
            hint="Tokens: {job_id} {permit_id} {ts} {resource_type}. Trailing / appends {job_id}.ndjson."
          >
            <Input
              value={form.key_prefix}
              onChange={(e) => setForm({ ...form, key_prefix: e.target.value })}
              placeholder="{permit_id}/{ts}/"
            />
          </Field>
        </div>
        <div className="flex flex-wrap gap-6">
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={form.secure}
              onCheckedChange={(v) => setForm({ ...form, secure: v === true })}
            />
            Use TLS (https)
          </label>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox
              checked={form.path_style}
              onCheckedChange={(v) => setForm({ ...form, path_style: v === true })}
            />
            Path-style addressing
          </label>
        </div>
        <Button onClick={onCreate} disabled={busy === "create"}>
          {busy === "create" ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Plus className="mr-2 h-4 w-4" />
          )}
          Save destination
        </Button>

        <div className="space-y-2">
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : items.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No S3 destinations yet.
            </p>
          ) : (
            items.map((d) => (
              <div
                key={d.id}
                className="flex items-center justify-between rounded-md border p-3"
              >
                <div className="min-w-0">
                  <p className="truncate font-medium">{d.name}</p>
                  <p className="truncate text-xs text-muted-foreground">
                    {`s3://${d.bucket}/${d.key_prefix}`} @ {d.endpoint}
                  </p>
                </div>
                <div className="flex items-center gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    onClick={() => onTest(d.id)}
                    disabled={busy === d.id}
                  >
                    {busy === d.id ? (
                      <Loader2 className="h-4 w-4 animate-spin" />
                    ) : (
                      <CheckCircle2 className="h-4 w-4" />
                    )}
                    <span className="ml-1">Test</span>
                  </Button>
                  <Button
                    size="sm"
                    variant="ghost"
                    onClick={() => onDelete(d.id, d.name)}
                    disabled={busy === d.id}
                  >
                    <Trash2 className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))
          )}
        </div>
      </CardContent>
    </Card>
  );
}
