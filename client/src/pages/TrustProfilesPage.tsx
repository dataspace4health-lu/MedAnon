import { useState, useEffect, useCallback } from "react";
import { toast } from "sonner";
import { Plus, Pencil, Trash2, ShieldCheck, BadgeCheck, Loader2 } from "lucide-react";
import { PageHeader } from "@/components/layout/PageHeader";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { useAuth } from "@/context/AuthContext";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { Textarea } from "@/components/ui/textarea";
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
  DialogFooter,
} from "@/components/ui/dialog";
import {
  listTrustProfiles,
  listPhases,
  createTrustProfile,
  updateTrustProfile,
  deleteTrustProfile,
} from "@/api/trustProfiles";
import type { TrustProfileMeta, SectorTarget } from "@/api/trustProfiles";

/** A sector-target row in the editor, array fields held as CSV for easy input. */
interface TargetRow {
  id: string;
  resourceTypes: string;
  codeSystems: string;
  fhirpath: string;
}

/** A per-check threshold override row. */
interface ThresholdRow {
  checkId: string;
  value: string;
}

interface EditorState {
  open: boolean;
  editing: TrustProfileMeta | null;
  name: string;
  description: string;
  intendedUse: string;
  phases: Set<string>;
  targets: TargetRow[];
  thresholds: ThresholdRow[];
}

const EMPTY_EDITOR: EditorState = {
  open: false,
  editing: null,
  name: "",
  description: "",
  intendedUse: "",
  phases: new Set(),
  targets: [],
  thresholds: [],
};

const csvToList = (s: string): string[] =>
  s.split(",").map((x) => x.trim()).filter(Boolean);

export default function TrustProfilesPage() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole("admin");

  const [profiles, setProfiles] = useState<TrustProfileMeta[]>([]);
  const [phaseCatalog, setPhaseCatalog] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [editor, setEditor] = useState<EditorState>(EMPTY_EDITOR);
  const [deleteTarget, setDeleteTarget] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [profs, phases] = await Promise.all([listTrustProfiles(), listPhases()]);
      setProfiles(profs);
      setPhaseCatalog(phases);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Failed to load trust profiles");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  function openNew() {
    setEditor({ ...EMPTY_EDITOR, open: true, phases: new Set(phaseCatalog) });
  }

  function openEdit(p: TrustProfileMeta) {
    setEditor({
      open: true,
      editing: p,
      name: p.name,
      description: p.description,
      intendedUse: p.intended_use,
      phases: new Set(p.phases),
      targets: p.targets.map((t) => ({
        id: t.id ?? "",
        resourceTypes: (t.resource_types ?? []).join(", "),
        codeSystems: (t.code_systems ?? []).join(", "),
        fhirpath: t.fhirpath ?? "",
      })),
      thresholds: Object.entries(p.thresholds ?? {}).map(([checkId, value]) => ({
        checkId,
        value: String(value),
      })),
    });
  }

  function updateTargetRow(i: number, patch: Partial<TargetRow>) {
    setEditor((e) => ({
      ...e,
      targets: e.targets.map((r, j) => (j === i ? { ...r, ...patch } : r)),
    }));
  }

  function updateThresholdRow(i: number, patch: Partial<ThresholdRow>) {
    setEditor((e) => ({
      ...e,
      thresholds: e.thresholds.map((r, j) => (j === i ? { ...r, ...patch } : r)),
    }));
  }

  function togglePhase(phase: string) {
    setEditor((e) => {
      const next = new Set(e.phases);
      if (next.has(phase)) next.delete(phase);
      else next.add(phase);
      return { ...e, phases: next };
    });
  }

  async function save() {
    if (editor.phases.size === 0) {
      toast.error("Select at least one phase.");
      return;
    }
    setSaving(true);
    try {
      const phases = phaseCatalog.filter((p) => editor.phases.has(p));
      const targets: SectorTarget[] = editor.targets
        .map((r) => {
          const t: SectorTarget = {};
          if (r.id.trim()) t.id = r.id.trim();
          const rt = csvToList(r.resourceTypes);
          if (rt.length) t.resource_types = rt;
          const cs = csvToList(r.codeSystems);
          if (cs.length) t.code_systems = cs;
          if (r.fhirpath.trim()) t.fhirpath = r.fhirpath.trim();
          return t;
        })
        .filter((t) => t.resource_types || t.code_systems || t.fhirpath);
      const thresholds: Record<string, number> = {};
      for (const r of editor.thresholds) {
        const k = r.checkId.trim();
        const v = parseFloat(r.value);
        if (k && !Number.isNaN(v)) thresholds[k] = v;
      }
      if (editor.editing) {
        await updateTrustProfile(editor.editing.name, {
          description: editor.description,
          intended_use: editor.intendedUse,
          phases,
          targets,
          thresholds,
        });
        toast.success(`Updated "${editor.editing.name}"`);
      } else {
        await createTrustProfile({
          name: editor.name,
          description: editor.description,
          intended_use: editor.intendedUse,
          phases,
          targets,
          thresholds,
        });
        toast.success(`Created "${editor.name}"`);
      }
      setEditor(EMPTY_EDITOR);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }

  async function confirmDelete() {
    if (!deleteTarget) return;
    try {
      await deleteTrustProfile(deleteTarget);
      toast.success(`Deleted "${deleteTarget}"`);
      await load();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : "Delete failed");
    } finally {
      setDeleteTarget(null);
    }
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Trust Profiles"
        description="Reusable audit profiles that tune which data-quality phases the Trust Gate runs and for what declared use."
        actions={
          isAdmin ? (
            <Button onClick={openNew} disabled={loading}>
              <Plus className="size-4" /> New profile
            </Button>
          ) : undefined
        }
      />

      {loading ? (
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading…
        </div>
      ) : (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {profiles.map((p) => (
            <Card key={p.name}>
              <CardHeader>
                <CardTitle className="flex items-center gap-2">
                  {p.is_system ? (
                    <ShieldCheck className="size-4 text-muted-foreground" />
                  ) : (
                    <BadgeCheck className="size-4 text-emerald-600" />
                  )}
                  {p.name}
                  {p.is_system && (
                    <Badge variant="outline" className="ml-auto text-xs">
                      system
                    </Badge>
                  )}
                </CardTitle>
                <CardDescription>{p.description || "—"}</CardDescription>
              </CardHeader>
              <CardContent className="space-y-3">
                {p.intended_use && (
                  <div className="text-xs">
                    <span className="text-muted-foreground">Intended use: </span>
                    <span className="font-medium">{p.intended_use}</span>
                  </div>
                )}
                <div className="flex flex-wrap gap-1">
                  {p.phases.map((ph) => (
                    <Badge key={ph} variant="secondary" className="font-mono text-[10px]">
                      {ph}
                    </Badge>
                  ))}
                </div>
                {(p.targets.length > 0 || Object.keys(p.thresholds).length > 0) && (
                  <div className="text-xs text-muted-foreground">
                    {[
                      p.targets.length > 0 && `${p.targets.length} sector${p.targets.length > 1 ? "s" : ""}`,
                      Object.keys(p.thresholds).length > 0 &&
                        `${Object.keys(p.thresholds).length} threshold override${Object.keys(p.thresholds).length > 1 ? "s" : ""}`,
                    ]
                      .filter(Boolean)
                      .join(" · ")}
                  </div>
                )}
              </CardContent>
              {isAdmin && !p.is_system && (
                <CardFooter className="gap-2">
                  <Button variant="outline" size="sm" onClick={() => openEdit(p)}>
                    <Pencil className="size-3.5" /> Edit
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className="text-destructive"
                    onClick={() => setDeleteTarget(p.name)}
                  >
                    <Trash2 className="size-3.5" /> Delete
                  </Button>
                </CardFooter>
              )}
            </Card>
          ))}
        </div>
      )}

      {/* Editor dialog ----------------------------------------------------- */}
      <Dialog open={editor.open} onOpenChange={(o) => !o && setEditor(EMPTY_EDITOR)}>
        <DialogContent className="max-h-[85vh] overflow-y-auto sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>
              {editor.editing ? `Edit "${editor.editing.name}"` : "New trust profile"}
            </DialogTitle>
          </DialogHeader>
          <div className="space-y-4">
            {!editor.editing && (
              <div className="space-y-1.5">
                <label className="text-sm font-medium">Name</label>
                <Input
                  value={editor.name}
                  onChange={(e) => setEditor((s) => ({ ...s, name: e.target.value }))}
                  placeholder="e.g. lab-research"
                />
                <p className="text-xs text-muted-foreground">
                  1–64 chars: letters, digits, hyphens, underscores.
                </p>
              </div>
            )}
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Description</label>
              <Textarea
                value={editor.description}
                rows={2}
                onChange={(e) => setEditor((s) => ({ ...s, description: e.target.value }))}
              />
            </div>
            <div className="space-y-1.5">
              <label className="text-sm font-medium">Intended use</label>
              <Input
                value={editor.intendedUse}
                onChange={(e) => setEditor((s) => ({ ...s, intendedUse: e.target.value }))}
                placeholder="e.g. research cohort discovery"
              />
              <p className="text-xs text-muted-foreground">
                Names the use the fitness verdict is bound to.
              </p>
            </div>
            <div className="space-y-2">
              <label className="text-sm font-medium">
                Audit phases ({editor.phases.size}/{phaseCatalog.length})
              </label>
              <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
                {phaseCatalog.map((ph) => (
                  <label
                    key={ph}
                    className="flex items-center gap-2 rounded-md border p-2 text-xs"
                  >
                    <Checkbox
                      checked={editor.phases.has(ph)}
                      onCheckedChange={() => togglePhase(ph)}
                    />
                    <span className="font-mono">{ph}</span>
                  </label>
                ))}
              </div>
            </div>

            {/* Sector targets ------------------------------------------------ */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-sm font-medium">Sector targets (optional)</label>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    setEditor((s) => ({
                      ...s,
                      targets: [
                        ...s.targets,
                        { id: "", resourceTypes: "", codeSystems: "", fhirpath: "" },
                      ],
                    }))
                  }
                >
                  <Plus className="size-3.5" /> Add sector
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Each sector gets its own verdict. Leave empty to assess the whole batch only.
              </p>
              {editor.targets.map((t, i) => (
                <div key={i} className="space-y-2 rounded-md border p-2">
                  <div className="grid grid-cols-1 gap-2 sm:grid-cols-[1fr_1fr_1fr_auto]">
                    <Input
                      value={t.id}
                      placeholder="id (e.g. labs)"
                      onChange={(e) => updateTargetRow(i, { id: e.target.value })}
                    />
                    <Input
                      value={t.resourceTypes}
                      placeholder="resource types, CSV"
                      onChange={(e) => updateTargetRow(i, { resourceTypes: e.target.value })}
                    />
                    <Input
                      value={t.codeSystems}
                      placeholder="code systems, CSV"
                      onChange={(e) => updateTargetRow(i, { codeSystems: e.target.value })}
                    />
                    <Button
                      variant="ghost"
                      size="icon"
                      className="text-destructive"
                      onClick={() =>
                        setEditor((s) => ({ ...s, targets: s.targets.filter((_, j) => j !== i) }))
                      }
                    >
                      <Trash2 className="size-4" />
                    </Button>
                  </div>
                  <Input
                    value={t.fhirpath}
                    placeholder="FHIRPath cohort, e.g. Patient.gender = 'female'"
                    onChange={(e) => updateTargetRow(i, { fhirpath: e.target.value })}
                  />
                </div>
              ))}
            </div>

            {/* Threshold overrides ------------------------------------------- */}
            <div className="space-y-2">
              <div className="flex items-center justify-between">
                <label className="text-sm font-medium">Threshold overrides (optional)</label>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() =>
                    setEditor((s) => ({
                      ...s,
                      thresholds: [...s.thresholds, { checkId: "", value: "" }],
                    }))
                  }
                >
                  <Plus className="size-3.5" /> Add threshold
                </Button>
              </div>
              <p className="text-xs text-muted-foreground">
                Per-check max violation fraction (0–1), e.g. <code>completeness.element_density</code> = 0.5.
              </p>
              {editor.thresholds.map((t, i) => (
                <div key={i} className="grid grid-cols-[1fr_120px_auto] gap-2 rounded-md border p-2">
                  <Input
                    value={t.checkId}
                    placeholder="check_id"
                    onChange={(e) => updateThresholdRow(i, { checkId: e.target.value })}
                  />
                  <Input
                    value={t.value}
                    type="number"
                    step="0.01"
                    min="0"
                    max="1"
                    placeholder="0.0"
                    onChange={(e) => updateThresholdRow(i, { value: e.target.value })}
                  />
                  <Button
                    variant="ghost"
                    size="icon"
                    className="text-destructive"
                    onClick={() =>
                      setEditor((s) => ({
                        ...s,
                        thresholds: s.thresholds.filter((_, j) => j !== i),
                      }))
                    }
                  >
                    <Trash2 className="size-4" />
                  </Button>
                </div>
              ))}
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditor(EMPTY_EDITOR)}>
              Cancel
            </Button>
            <Button onClick={save} disabled={saving || (!editor.editing && !editor.name)}>
              {saving && <Loader2 className="size-4 animate-spin" />}
              {editor.editing ? "Save changes" : "Create"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <ConfirmDialog
        open={deleteTarget !== null}
        onOpenChange={(o) => !o && setDeleteTarget(null)}
        title={`Delete trust profile "${deleteTarget}"?`}
        description="This cannot be undone."
        confirmLabel="Delete"
        variant="destructive"
        onConfirm={confirmDelete}
      />
    </div>
  );
}
