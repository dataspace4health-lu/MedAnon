import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHeader } from '@/components/layout/PageHeader';
import {
  listConfigs,
  getConfigYaml,
  createConfig,
  updateConfig,
} from '@/api/medanon';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from '@/components/ui/collapsible';
import {
  ChevronDown,
  Loader2,
  AlertCircle,
  ArrowLeft,
  Info,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
} from '@/components/ui/tooltip';

import {
  type LocalRule,
  VALID_ACTIONS,
  ACTION_DESCRIPTIONS,
  parseYamlIntoRules,
  extractGeneralBlock,
  buildYamlPreview,
  toApiRules,
} from './config-builder/configConstants';
import { RulesTable } from './config-builder/RulesTable';
import { ImportPanel } from './config-builder/ImportPanel';
import { AiAssistantPanel } from './config-builder/AiAssistantPanel';
import { ResourceExplorerPanel } from './config-builder/ResourceExplorerPanel';

// ---------------------------------------------------------------------------
// ConfigBuilderPage
// ---------------------------------------------------------------------------

export default function ConfigBuilderPage() {
  const navigate = useNavigate();
  const { name: editName } = useParams<{ name?: string }>();
  const [searchParams] = useSearchParams();
  const fromName = searchParams.get('from');   // duplicate source

  const isEdit = Boolean(editName);
  const title = isEdit ? `Edit: ${editName}` : 'New Configuration';
  const description = isEdit
    ? 'Modify the rules for this configuration profile.'
    : 'Build a custom de-identification rule set.';

  // Form state
  const [configName, setConfigName] = useState(editName ?? '');
  const [configDescription, setConfigDescription] = useState('');
  const [rules, setRules] = useState<LocalRule[]>([]);
  // Referential integrity — rewrite cross-resource references so a patient's
  // resources stay linked after IDs are pseudonymized/hashed. Default ON: this
  // is almost always what you want, and omitting it silently breaks references.
  const [rewriteReferences, setRewriteReferences] = useState(true);
  // The source profile's `general:` block (domain_map etc.) carried through a
  // load/duplicate so the builder doesn't strip it on save. The toggle controls
  // rewrite_references/_text_ids; everything else here is preserved verbatim.
  const [preservedGeneral, setPreservedGeneral] = useState<Record<string, unknown>>({});
  const [yamlPreviewOpen, setYamlPreviewOpen] = useState(false);
  // PHI-free field-path summary from uploaded example resources (Resource
  // Explorer) → grounds the AI assistant when no live server tree is loaded.
  // TODO: wire ResourceExplorerPanel to populate this; empty until then.
  const fieldContext = '';

  // UI state
  const [loading, setLoading] = useState(isEdit || Boolean(fromName));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Load existing config for edit or duplicate
  useEffect(() => {
    const sourceName = editName ?? fromName;
    if (!sourceName) return;

    let cancelled = false;
    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        // Get description from list
        const configs = await listConfigs();
        const meta = configs.find((c) => c.name === sourceName);
        if (meta && !isEdit) {
          // Duplicating — pre-fill description but clear name
          setConfigDescription(meta.description);
          setConfigName('');
        } else if (meta && isEdit) {
          setConfigDescription(meta.description);
        }

        // Get YAML and parse rules using the shared parser
        const yaml = await getConfigYaml(sourceName);
        const { rules: parsed, error: parseErr } = parseYamlIntoRules(yaml);

        if (!cancelled) {
          if (parseErr) setError(parseErr);
          setRules(parsed);
          // Reflect + preserve the source profile's general block so editing or
          // duplicating it doesn't strip domain_map / referential integrity.
          const general = extractGeneralBlock(yaml);
          setPreservedGeneral(general);
          setRewriteReferences(general.rewrite_references === true);
        }
      } catch (err) {
        if (!cancelled) setError(String(err));
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    load();
    return () => { cancelled = true; };
  }, [editName, fromName, isEdit]);

  const yamlPreview = useMemo(
    () =>
      buildYamlPreview(configName, configDescription, rules, {
        rewriteReferences,
        general: preservedGeneral,
      }),
    [configName, configDescription, rules, rewriteReferences, preservedGeneral],
  );

  const handleSave = useCallback(async () => {
    setError(null);

    if (!configName.trim()) {
      setError('Config name is required.');
      return;
    }
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(configName)) {
      setError('Name must be 1\u201364 characters: letters, digits, hyphens, underscores only.');
      return;
    }
    const validRules = rules.filter((r) => r.match.trim());
    if (validRules.length === 0) {
      setError('At least one rule with a FHIRPath match is required.');
      return;
    }

    setSaving(true);
    try {
      // Merge the preserved source general block (domain_map, …) with the
      // referential-integrity flags the toggle controls, so editing/duplicating
      // a profile keeps its gPAS domain routing intact.
      const general: Record<string, unknown> = {
        ...preservedGeneral,
        rewrite_references: rewriteReferences,
        rewrite_text_ids: rewriteReferences,
      };
      if (isEdit && editName) {
        await updateConfig(editName, {
          description: configDescription,
          rules: toApiRules(validRules),
          general,
        });
        toast.success(`Config "${editName}" updated.`);
      } else {
        await createConfig({
          name: configName.trim(),
          description: configDescription,
          rules: toApiRules(validRules),
          general,
        });
        toast.success(`Config "${configName}" created.`);
      }
      navigate('/configs');
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }, [configName, configDescription, rules, rewriteReferences, preservedGeneral, isEdit, editName, navigate]);

  if (loading) {
    return (
      <div>
        <PageHeader title={title} description={description} />
        <div className="flex items-center gap-2.5 rounded-lg border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading config...
        </div>
      </div>
    );
  }

  return (
    <div>
      <div className="flex items-start gap-2">
        <PageHeader title={title} description={description} />
        <TooltipProvider>
          <Tooltip>
            <TooltipTrigger className="mt-1.5 shrink-0 text-muted-foreground transition-colors hover:text-foreground">
              <Info className="size-4" />
            </TooltipTrigger>
            <TooltipContent side="bottom" align="start" className="max-w-sm p-0">
              <div className="space-y-1 p-2.5 text-xs leading-relaxed">
                <p className="font-semibold">Available Actions</p>
                {VALID_ACTIONS.map((a) => (
                  <div key={a}>
                    <span className="font-mono font-medium">{a}</span>
                    <span className="ml-1 text-background/70">{ACTION_DESCRIPTIONS[a]}</span>
                  </div>
                ))}
              </div>
            </TooltipContent>
          </Tooltip>
        </TooltipProvider>
      </div>

      {/* Back link */}
      <button
        onClick={() => navigate('/configs')}
        className="mb-6 flex items-center gap-1.5 text-sm text-muted-foreground transition-colors hover:text-foreground"
      >
        <ArrowLeft className="size-4" />
        Back to configs
      </button>

      {/* Error banner */}
      {error && (
        <div className="mb-4 flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          {error}
        </div>
      )}

      {/* -- Section 1: General Settings -- */}
      <div className="mb-6 space-y-4">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          General Settings
        </p>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Config Name
            </label>
            <Input
              value={configName}
              onChange={(e) => setConfigName(e.target.value)}
              placeholder="e.g. my-hipaa-profile"
              disabled={isEdit}
              className={cn('font-mono', isEdit && 'opacity-60')}
            />
            {isEdit && (
              <p className="mt-1 text-xs text-muted-foreground">
                Name cannot be changed after creation.
              </p>
            )}
          </div>
          <div>
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Description
            </label>
            <Input
              value={configDescription}
              onChange={(e) => setConfigDescription(e.target.value)}
              placeholder="Short description of this profile..."
            />
          </div>
        </div>

        {/* Referential integrity toggle */}
        <label className="mt-4 flex cursor-pointer items-start gap-2.5 rounded-md border bg-muted/20 p-3">
          <input
            type="checkbox"
            checked={rewriteReferences}
            onChange={(e) => setRewriteReferences(e.target.checked)}
            className="mt-0.5 size-4 shrink-0 accent-[#0072bc]"
          />
          <span className="text-sm">
            <span className="font-medium text-foreground">
              Keep resources linked (rewrite references)
            </span>
            <span className="mt-0.5 block text-xs text-muted-foreground">
              After IDs are pseudonymized/hashed, rewrite every cross-resource
              reference (<span className="font-mono">subject</span>,{' '}
              <span className="font-mono">patient</span>, …) so the same patient's
              resources still point at each other. The engine rewrites the id
              after the <span className="font-mono">ResourceType/</span> prefix with
              the same deterministic mapping as the <span className="font-mono">.id</span> rule —
              don't add separate rules on <span className="font-mono">*.reference</span>.
              Leave on unless you specifically want references untouched.
            </span>
          </span>
        </label>
      </div>

      <Separator className="mb-6" />

      {/* -- Section 2: Rules -- */}
      <div className="mb-6 space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Rules
          </p>
          <ImportPanel
            onImport={(imported) => setRules((prev) => [...prev, ...imported])}
            existingRules={rules}
          />
          <AiAssistantPanel
            configYaml={yamlPreview}
            fieldContext={fieldContext}
            existingRules={rules}
            onImport={(imported) => setRules((prev) => [...prev, ...imported])}
          />
          <ResourceExplorerPanel
            onAddRule={(rule) => setRules((prev) => [...prev, rule])}
            rules={rules}
          />
        </div>
        <RulesTable rules={rules} onChange={setRules} />
      </div>

      <Separator className="mb-6" />

      {/* -- Section 3: YAML Preview -- */}
      <div className="mb-8">
        <Collapsible open={yamlPreviewOpen} onOpenChange={setYamlPreviewOpen}>
          <CollapsibleTrigger className="flex items-center gap-1.5 text-sm font-medium text-muted-foreground transition-colors hover:text-foreground">
            <ChevronDown
              className={cn('size-4 transition-transform', yamlPreviewOpen && 'rotate-180')}
            />
            YAML Preview
            {rules.filter((r) => r.match.trim()).length > 0 && (
              <Badge variant="secondary" className="ml-1 text-xs">
                {rules.filter((r) => r.match.trim()).length} rules
              </Badge>
            )}
          </CollapsibleTrigger>
          <CollapsibleContent className="mt-2">
            <pre className="max-h-72 overflow-y-auto rounded-md border bg-muted/50 p-4 font-mono text-xs leading-relaxed">
              {yamlPreview || '# Add rules above to see a preview'}
            </pre>
          </CollapsibleContent>
        </Collapsible>
      </div>

      {/* -- Footer actions -- */}
      <div className="sticky bottom-0 z-10 -mx-4 flex gap-3 border-t bg-background px-4 py-4 sm:-mx-6 sm:px-6 lg:-mx-8 lg:px-8">
        <Button
          onClick={handleSave}
          disabled={saving}
          className="min-w-[120px]"
        >
          {saving && <Loader2 className="size-4 animate-spin" />}
          {saving ? 'Saving...' : isEdit ? 'Save Changes' : 'Create Config'}
        </Button>
        <Button
          variant="outline"
          onClick={() => navigate('/configs')}
          disabled={saving}
        >
          Cancel
        </Button>
      </div>
    </div>
  );
}
