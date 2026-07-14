import { useCallback, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  Database,
  Loader2,
  Plus,
  Trash2,
  CheckCircle2,
  ServerCog,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
} from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useSettings, SAVED_SOURCE_PREFIX } from "@/context/SettingsContext";
import { useAuth } from "@/context/AuthContext";
import {
  type ServerRole,
  type SourceConnection,
  createSource,
  deleteSource,
  listSources,
  testSource,
} from "@/api/connectors";
import { updateSettings } from "@/api/instanceSettings";

const NONE = "none";

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

/**
 * Single place to manage FHIR server connections: add a server, say what it is
 * used for (source / target / both), and pick the active source and target that
 * drive browsing and jobs. Replaces the old split "FHIR servers" +
 * "Active source & target" + "FHIR servers (saved)" cards.
 */
export function FhirServersCard() {
  const {
    activeSourceId,
    activeTargetId,
    setActiveSource,
    setActiveTarget,
    builtinFhirEnabled,
  } = useSettings();
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [servers, setServers] = useState<SourceConnection[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [form, setForm] = useState<{
    name: string;
    server_url: string;
    token: string;
    role: ServerRole;
  }>({ name: "", server_url: "", token: "", role: "both" });

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      setServers(await listSources());
    } catch {
      // Non-admin / no app DB: saved servers unavailable, non-fatal.
      setServers([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const sourceOptions = servers.filter(
    (s) => s.role === "source" || s.role === "both",
  );
  const targetOptions = servers.filter(
    (s) => s.role === "target" || s.role === "both",
  );

  const onAdd = async () => {
    if (!form.name.trim() || !form.server_url.trim()) {
      toast.error("Name and base URL are required.");
      return;
    }
    if (!/^https?:\/\//.test(form.server_url.trim())) {
      toast.error("URL must start with http:// or https://");
      return;
    }
    setBusy("add");
    try {
      await createSource({
        name: form.name.trim(),
        server_url: form.server_url.trim(),
        token: form.token || undefined,
        role: form.role,
      });
      toast.success(`Server "${form.name}" added.`);
      setForm({ name: "", server_url: "", token: "", role: "both" });
      await refresh();
    } catch (e) {
      toast.error("Could not add server.", { description: errMsg(e) });
    } finally {
      setBusy(null);
    }
  };

  const onTest = async (id: string) => {
    setBusy(id);
    try {
      const res = await testSource(id);
      toast.success("Server reachable.", {
        description: `${res.resource_types} resource types available.`,
      });
    } catch (e) {
      toast.error("Server test failed.", { description: errMsg(e) });
    } finally {
      setBusy(null);
    }
  };

  const onDelete = async (s: SourceConnection) => {
    setBusy(s.id);
    try {
      await deleteSource(s.id);
      // If it was the active source/target, disconnect.
      const savedId = `${SAVED_SOURCE_PREFIX}${s.id}`;
      if (activeSourceId === savedId) setActiveSource(NONE);
      if (activeTargetId === savedId) setActiveTarget(NONE);
      toast.success(`Removed "${s.name}".`);
      await refresh();
    } catch (e) {
      toast.error("Could not remove server.", { description: errMsg(e) });
    } finally {
      setBusy(null);
    }
  };

  const saveDeploymentDefault = async () => {
    try {
      await updateSettings({
        active_source_id: activeSourceId,
        active_target_id: activeTargetId,
      });
      toast.success("Saved as deployment default (applies to all users).");
    } catch (e) {
      toast.error("Could not save deployment default.", {
        description: errMsg(e),
      });
    }
  };

  return (
    <Card className="mb-6">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <Database className="size-4" /> FHIR servers
        </CardTitle>
        <CardDescription>
          Add the FHIR servers this app connects to, say what each is used for,
          and choose the active source and target. The source feeds the Patient /
          Condition browsers and export jobs; the target receives de-identified
          uploads. Bearer tokens are encrypted at rest and resolved server-side.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-6">
        {/* Active selection ------------------------------------------------ */}
        <div className="grid gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Active source
            </label>
            <Select
              value={activeSourceId || NONE}
              onValueChange={(v) => setActiveSource(v ?? NONE)}
            >
              <SelectTrigger>
                <SelectValue placeholder="None (disconnected)" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>None (disconnected)</SelectItem>
                {builtinFhirEnabled && (
                  <SelectItem value="source">Source HAPI (built-in)</SelectItem>
                )}
                {sourceOptions.map((s) => (
                  <SelectItem key={s.id} value={`${SAVED_SOURCE_PREFIX}${s.id}`}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Active target
            </label>
            <Select
              value={activeTargetId || NONE}
              onValueChange={(v) => setActiveTarget(v ?? NONE)}
            >
              <SelectTrigger>
                <SelectValue placeholder="None (disconnected)" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NONE}>None (disconnected)</SelectItem>
                {builtinFhirEnabled && (
                  <SelectItem value="target">Target HAPI (built-in)</SelectItem>
                )}
                {targetOptions.map((s) => (
                  <SelectItem key={s.id} value={`${SAVED_SOURCE_PREFIX}${s.id}`}>
                    {s.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        {isAdmin && (
          <div className="flex items-center gap-3">
            <Button variant="outline" size="sm" onClick={saveDeploymentDefault}>
              <ServerCog className="mr-2 h-4 w-4" />
              Save as deployment default
            </Button>
            <span className="text-xs text-muted-foreground">
              Applies this source/target to every user of this deployment.
            </span>
          </div>
        )}

        {/* Configured servers --------------------------------------------- */}
        <div className="space-y-2 border-t pt-4">
          <p className="text-sm font-medium">Configured servers</p>
          {loading ? (
            <p className="text-sm text-muted-foreground">Loading…</p>
          ) : servers.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No servers yet.{" "}
              {isAdmin
                ? "Add one below."
                : "Ask an administrator to add a FHIR server."}
            </p>
          ) : (
            servers.map((s) => {
              const savedId = `${SAVED_SOURCE_PREFIX}${s.id}`;
              return (
                <div
                  key={s.id}
                  className="flex items-center justify-between rounded-md border p-3"
                >
                  <div className="min-w-0">
                    <p className="flex items-center gap-2 truncate font-medium">
                      {s.name}
                      <Badge variant="outline">{s.role}</Badge>
                      {activeSourceId === savedId && (
                        <Badge variant="secondary">active source</Badge>
                      )}
                      {activeTargetId === savedId && (
                        <Badge variant="secondary">active target</Badge>
                      )}
                    </p>
                    <p className="truncate text-xs text-muted-foreground">
                      {s.server_url}
                      {s.has_token ? " · token" : ""}
                    </p>
                  </div>
                  <div className="flex shrink-0 items-center gap-2">
                    <Button
                      size="sm"
                      variant="outline"
                      onClick={() => onTest(s.id)}
                      disabled={busy === s.id}
                    >
                      {busy === s.id ? (
                        <Loader2 className="h-4 w-4 animate-spin" />
                      ) : (
                        <CheckCircle2 className="h-4 w-4" />
                      )}
                      <span className="ml-1">Test</span>
                    </Button>
                    {isAdmin && (
                      <Button
                        size="sm"
                        variant="ghost"
                        onClick={() => onDelete(s)}
                        disabled={busy === s.id}
                      >
                        <Trash2 className="h-4 w-4" />
                      </Button>
                    )}
                  </div>
                </div>
              );
            })
          )}
        </div>

        {/* Add server (admin) --------------------------------------------- */}
        {isAdmin && (
          <div className="grid gap-3 rounded-lg border border-dashed p-4 sm:grid-cols-2">
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">Name</label>
              <Input
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Client EHR (prod)"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">
                FHIR base URL
              </label>
              <Input
                value={form.server_url}
                onChange={(e) => setForm({ ...form, server_url: e.target.value })}
                placeholder="https://fhir.client.org/fhir"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">
                Bearer token (optional)
              </label>
              <Input
                type="password"
                value={form.token}
                onChange={(e) => setForm({ ...form, token: e.target.value })}
                placeholder="••••••"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs text-muted-foreground">Used as</label>
              <Select
                value={form.role}
                onValueChange={(v) =>
                  setForm({ ...form, role: (v as ServerRole) ?? "both" })
                }
              >
                <SelectTrigger>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="source">Source</SelectItem>
                  <SelectItem value="target">Target</SelectItem>
                  <SelectItem value="both">Both</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <div className="sm:col-span-2 flex items-center gap-2">
              <Button onClick={onAdd} disabled={busy === "add"}>
                {busy === "add" ? (
                  <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                ) : (
                  <Plus className="mr-2 h-4 w-4" />
                )}
                Add server
              </Button>
              <span className="text-xs text-muted-foreground">
                Stored server-side (encrypted token), shared across users. Reached
                through the SSRF-guarded proxy.
              </span>
            </div>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
