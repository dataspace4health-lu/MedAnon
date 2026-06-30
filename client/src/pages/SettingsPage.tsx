import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import {
  Database,
  Plus,
  Trash2,
  RotateCcw,
  CheckCircle2,
  SlidersHorizontal,
  Palette,
  Pencil,
  Lock,
} from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useSettings } from "@/context/SettingsContext";
import { useConfig } from "@/context/ConfigContext";
import { useTheme, type Theme } from "@/context/ThemeContext";
import { listConfigs, type ConfigMeta } from "@/api/configs";
import type { FhirConnection } from "@/api/fhirScan";

// ---------------------------------------------------------------------------
// Connection editor row
// ---------------------------------------------------------------------------

function ConnectionRow({
  conn,
  active,
  onActivate,
  onSave,
  onRemove,
  onReset,
}: {
  conn: FhirConnection;
  active: boolean;
  onActivate: () => void;
  onSave: (next: FhirConnection) => void;
  onRemove: () => void;
  onReset: () => void;
}) {
  const builtin = conn.kind !== "custom";
  const [editing, setEditing] = useState(false);
  const [label, setLabel] = useState(conn.label);
  const [baseUrl, setBaseUrl] = useState(conn.baseUrl ?? "");
  const [token, setToken] = useState(conn.token ?? "");

  const save = () => {
    if (baseUrl && !/^https?:\/\//.test(baseUrl)) {
      toast.error("URL must start with http:// or https://");
      return;
    }
    onSave({
      ...conn,
      label: label.trim() || conn.label,
      baseUrl: baseUrl.trim() || undefined,
      token: token.trim() || undefined,
    });
    setEditing(false);
    toast.success(`Saved "${label.trim() || conn.label}"`);
  };

  return (
    <div className={`rounded-lg border p-4 ${active ? "border-primary bg-primary/5" : ""}`}>
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium">{conn.label}</span>
            {builtin && (
              <Badge variant="secondary" className="gap-1">
                <Lock className="size-3" /> built-in
              </Badge>
            )}
            {active && (
              <Badge variant="outline" className="gap-1 border-primary/40 text-primary">
                <CheckCircle2 className="size-3" /> active
              </Badge>
            )}
          </div>
          <p className="mt-0.5 truncate text-xs text-muted-foreground">
            {conn.baseUrl
              ? conn.baseUrl
              : conn.kind === "source"
                ? "nginx /fhir proxy (server env)"
                : conn.kind === "target"
                  ? "nginx /fhir-target proxy (server env)"
                  : "—"}
            {conn.token ? " · token set" : ""}
          </p>
        </div>
        <div className="flex shrink-0 items-center gap-1.5">
          {!active && (
            <Button size="sm" variant="outline" onClick={onActivate}>
              Use
            </Button>
          )}
          <Button size="sm" variant="ghost" onClick={() => setEditing((e) => !e)}>
            <Pencil className="size-3.5" />
          </Button>
          {builtin
            ? conn.baseUrl && (
                <Button size="sm" variant="ghost" onClick={onReset} title="Revert to server proxy">
                  <RotateCcw className="size-3.5" />
                </Button>
              )
            : (
              <Button size="sm" variant="ghost" onClick={onRemove}>
                <Trash2 className="size-3.5" />
              </Button>
            )}
        </div>
      </div>

      {editing && (
        <div className="mt-3 grid gap-3 border-t pt-3 sm:grid-cols-3">
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Label</label>
            <Input value={label} onChange={(e) => setLabel(e.target.value)} disabled={builtin} />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">
              {builtin ? "Override base URL (optional)" : "FHIR base URL"}
            </label>
            <Input
              placeholder={builtin ? "leave blank to use the server proxy" : "https://fhir.example.org/r4"}
              value={baseUrl}
              onChange={(e) => setBaseUrl(e.target.value)}
            />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">Bearer token (optional)</label>
            <Input type="password" placeholder="••••••" value={token} onChange={(e) => setToken(e.target.value)} />
          </div>
          <div className="sm:col-span-3">
            <Button size="sm" onClick={save}>
              Save
            </Button>
            {builtin && (
              <span className="ml-3 text-xs text-muted-foreground">
                Set an override URL to point this connection at a different FHIR server
                (routed through the SSRF-guarded backend proxy).
              </span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SettingsPage() {
  const {
    connections,
    activeConnectionId,
    setActiveConnection,
    upsertConnection,
    removeConnection,
    resetBuiltin,
    preferences,
    setPreference,
  } = useSettings();
  const { configProfile, setConfigProfile } = useConfig();
  const { theme, setTheme } = useTheme();

  const [profiles, setProfiles] = useState<ConfigMeta[]>([]);

  const [showAdd, setShowAdd] = useState(false);
  const [newLabel, setNewLabel] = useState("");
  const [newUrl, setNewUrl] = useState("");
  const [newToken, setNewToken] = useState("");

  useEffect(() => {
    listConfigs().then(setProfiles).catch(() => {});
  }, []);

  const addCustom = useCallback(() => {
    const url = newUrl.trim();
    if (!url) return toast.error("Enter a FHIR base URL.");
    if (!/^https?:\/\//.test(url)) return toast.error("URL must start with http:// or https://");
    const conn: FhirConnection = {
      id: `custom-${Date.now()}`,
      label: newLabel.trim() || new URL(url).host,
      kind: "custom",
      baseUrl: url,
      token: newToken.trim() || undefined,
    };
    upsertConnection(conn);
    setActiveConnection(conn.id);
    setNewLabel("");
    setNewUrl("");
    setNewToken("");
    setShowAdd(false);
    toast.success(`Added "${conn.label}"`);
  }, [newLabel, newUrl, newToken, upsertConnection, setActiveConnection]);

  return (
    <div>
      <PageHeader
        title="Settings"
        description="Configure the FHIR servers the app talks to, de-identification and assessment defaults, and appearance. Everything here is applied by the app and saved in this browser."
      />

      {/* FHIR connections --------------------------------------------------- */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Database className="size-4" /> FHIR servers
          </CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <p className="text-sm text-muted-foreground">
            The <strong>active</strong> connection is used by data pages (e.g. Trust Gate). Source
            and Target are built in; add custom servers or give a built-in an override URL to point
            it elsewhere.
          </p>
          {connections.map((c) => (
            <ConnectionRow
              key={c.id}
              conn={c}
              active={c.id === activeConnectionId}
              onActivate={() => setActiveConnection(c.id)}
              onSave={upsertConnection}
              onRemove={() => removeConnection(c.id)}
              onReset={() => resetBuiltin(c.id)}
            />
          ))}

          {showAdd ? (
            <div className="grid gap-3 rounded-lg border border-dashed p-4 sm:grid-cols-3">
              <div>
                <label className="mb-1 block text-xs text-muted-foreground">Label</label>
                <Input placeholder="Partner site A" value={newLabel} onChange={(e) => setNewLabel(e.target.value)} />
              </div>
              <div>
                <label className="mb-1 block text-xs text-muted-foreground">FHIR base URL</label>
                <Input placeholder="https://fhir.example.org/r4" value={newUrl} onChange={(e) => setNewUrl(e.target.value)} />
              </div>
              <div>
                <label className="mb-1 block text-xs text-muted-foreground">Bearer token (optional)</label>
                <Input type="password" placeholder="••••••" value={newToken} onChange={(e) => setNewToken(e.target.value)} />
              </div>
              <div className="sm:col-span-3 flex items-center gap-2">
                <Button onClick={addCustom}>
                  <Plus className="size-4" /> Save connection
                </Button>
                <Button variant="ghost" onClick={() => setShowAdd(false)}>
                  Cancel
                </Button>
                <span className="text-xs text-muted-foreground">
                  Reached through the backend SSRF-guarded proxy (private/loopback rejected).
                </span>
              </div>
            </div>
          ) : (
            <Button variant="outline" onClick={() => setShowAdd(true)}>
              <Plus className="size-4" /> Add connection
            </Button>
          )}
        </CardContent>
      </Card>

      {/* Defaults ----------------------------------------------------------- */}
      <Card className="mb-6">
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <SlidersHorizontal className="size-4" /> Defaults
          </CardTitle>
        </CardHeader>
        <CardContent className="grid gap-5 sm:grid-cols-2">
          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              De-identification rule profile
            </label>
            <Select value={configProfile} onValueChange={(v) => setConfigProfile(v ?? "auto")}>
              <SelectTrigger className="w-full">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">Auto (default)</SelectItem>
                {profiles.map((p) => (
                  <SelectItem key={p.name} value={p.name}>
                    {p.name}
                    {!p.is_system && " *"}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            <p className="mt-1.5 text-xs text-muted-foreground">Applied to all de-identification requests.</p>
          </div>

          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Default FHIR page size
            </label>
            <Input
              type="number"
              min={50}
              max={1000}
              value={preferences.fhirPageSize}
              onChange={(e) =>
                setPreference("fhirPageSize", Math.min(1000, Math.max(50, Number(e.target.value) || 50)))
              }
            />
            <p className="mt-1.5 text-xs text-muted-foreground">Resources fetched per request when paging.</p>
          </div>

          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Default dataset ID (Trust Gate)
            </label>
            <Input value={preferences.datasetId} onChange={(e) => setPreference("datasetId", e.target.value)} />
          </div>

          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Default provenance source system
            </label>
            <Input
              value={preferences.sourceSystem}
              onChange={(e) => setPreference("sourceSystem", e.target.value)}
            />
          </div>

          <div>
            <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
              Default full-server scan cap
            </label>
            <Input
              type="number"
              min={100}
              step={1000}
              value={preferences.scanMaxResources}
              onChange={(e) =>
                setPreference("scanMaxResources", Math.max(100, Number(e.target.value) || 100))
              }
            />
            <p className="mt-1.5 text-xs text-muted-foreground">Max resources a Trust Gate scan pulls.</p>
          </div>
        </CardContent>
      </Card>

      {/* Appearance --------------------------------------------------------- */}
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Palette className="size-4" /> Appearance
          </CardTitle>
        </CardHeader>
        <CardContent>
          <label className="mb-1.5 block text-xs font-medium text-muted-foreground">Theme</label>
          <Select value={theme} onValueChange={(v) => setTheme((v as Theme) ?? "system")}>
            <SelectTrigger className="w-48">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="system">System (default)</SelectItem>
              <SelectItem value="light">Light</SelectItem>
              <SelectItem value="dark">Dark</SelectItem>
            </SelectContent>
          </Select>
        </CardContent>
      </Card>
    </div>
  );
}
