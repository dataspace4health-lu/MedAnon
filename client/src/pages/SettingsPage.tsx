import { useState, useEffect } from "react";
import { SlidersHorizontal, Palette } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
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
import { useAuth } from "@/context/AuthContext";
import { DestinationsPanel } from "@/components/settings/ConnectorPanels";
import { FhirServersCard } from "@/components/settings/FhirServersCard";
import { listConfigs, type ConfigMeta } from "@/api/configs";

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export default function SettingsPage() {
  const { preferences, setPreference } = useSettings();
  const { configProfile, setConfigProfile } = useConfig();
  const { theme, setTheme } = useTheme();
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [profiles, setProfiles] = useState<ConfigMeta[]>([]);

  useEffect(() => {
    listConfigs().then(setProfiles).catch(() => {});
  }, []);

  return (
    <div>
      <PageHeader
        title="Settings"
        description="Configure the FHIR servers the app talks to, de-identification and assessment defaults, and appearance. Everything here is applied by the app and saved in this browser."
      />

      {/* FHIR servers, one place: add servers, set roles, pick active. */}
      <FhirServersCard />

      {/* S3 output destinations (backend-persisted, admin-only) ----------- */}
      {isAdmin && <DestinationsPanel />}

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
