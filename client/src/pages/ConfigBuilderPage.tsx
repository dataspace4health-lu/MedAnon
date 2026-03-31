import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { PageHeader } from '@/components/layout/PageHeader';
import {
  listConfigs,
  getConfigYaml,
  createConfig,
  updateConfig,
} from '@/api/medanon';
import type { ConfigRule } from '@/api/medanon';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from '@/components/ui/collapsible';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/table';
import {
  Plus,
  Trash2,
  ChevronDown,
  Loader2,
  AlertCircle,
  ArrowLeft,
  Upload,
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

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

const VALID_ACTIONS = [
  'redact',
  'cryptohash',
  'encrypt',
  'decrypt',
  'perturb',
  'substitute',
  'generalize',
  'scrub_text',
  'nlp_detect',
  'gpas_pseudonymize',
] as const;

type Action = (typeof VALID_ACTIONS)[number];

const GENERALIZE_STRATEGIES = [
  'date_year',
  'date_year_month',
  'zip_3digit',
  'age_group',
] as const;

const SCRUB_MODES = ['text', 'html_tokenize'] as const;
const SCRUB_PATTERNS = ['all', 'phone', 'email', 'date'] as const;
const NLP_MODES = ['tokenize', 'redact'] as const;

const ACTION_DESCRIPTIONS: Record<Action, string> = {
  redact: 'Replace the matched value with a fixed placeholder (e.g. [REDACTED]).',
  cryptohash: 'One-way HMAC-SHA3-256 hash — irreversible but deterministic for linkage.',
  encrypt: 'RSA-encrypt the value; reversible with the private key.',
  decrypt: 'RSA-decrypt a previously encrypted value.',
  perturb: 'Shift numeric/date values by a random offset within a configurable range.',
  substitute: 'Replace the value with a synthetic but structurally valid substitute.',
  generalize: 'Reduce precision (e.g. date to year-only, zip to 3-digit prefix).',
  scrub_text: 'Regex-based text scrubbing for phones, emails, dates, and other patterns.',
  nlp_detect: 'NLP-based named-entity detection (Presidio) for names, locations, etc.',
  gpas_pseudonymize: 'Replace the value with a gPAS-generated pseudonym (requires gPAS server).',
};

// ---------------------------------------------------------------------------
// Local rule type (adds a stable React key)
// ---------------------------------------------------------------------------

interface LocalRule {
  _id: string;       // local-only React key, stripped before saving
  match: string;
  action: Action;
  params: Record<string, unknown>;
  name: string;
}

// crypto.randomUUID() requires HTTPS or localhost — unavailable over plain HTTP.
function uid(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

// ---------------------------------------------------------------------------
// Shared YAML → LocalRule parser
//
// Handles both indentation styles used in this project:
//   style A (config.yaml):           style B (config_gpas/structural):
//     rules:                            rules:
//       - match: "*.id"                 - name: pseudonymize ids
//         action: cryptohash              match: "*.id"
//                                         action: gpas_pseudonymize
//
// YAML anchors (&gpas, *gpas) are stripped — params that reference anchors
// cannot be represented in the simplified params UI and are left empty.
// ---------------------------------------------------------------------------
function parseYamlIntoRules(yaml: string): { rules: LocalRule[]; error: string | null } {
  // Find the rules: block and grab everything after it
  const rulesStart = yaml.search(/^rules\s*:/m);
  if (rulesStart === -1) {
    return { rules: [], error: 'Could not find a "rules:" block in the YAML.' };
  }
  const afterRules = yaml.slice(rulesStart + yaml.slice(rulesStart).indexOf('\n') + 1);

  // Split on any line that starts a new list item: optional whitespace + "- "
  // This matches both "  - match:" (2-space) and "- name:" (0-space)
  const blocks = afterRules.split(/\n(?=\s*- )/).filter((b) => b.trim());

  const rules: LocalRule[] = blocks.map((block) => {
    // Strip YAML anchor definitions (&name) and references (*name) from values
    const clean = block.replace(/\s+&\w+/g, '').replace(/:\s+\*\w+/g, ': ~');
    const matchVal = clean.match(/match:\s*["']?([^"'\n]+?)["']?\s*$/m)?.[1]?.trim() ?? '';
    // action: strip any trailing anchor/alias artifacts
    const actionRaw = clean.match(/action:\s*(\S+)/)?.[1]?.trim() ?? '';
    const actionVal = actionRaw.replace(/[&*]\w+/, '').trim() as Action;
    const nameVal = clean.match(/name:\s*["']?([^"'\n#]+?)["']?\s*$/m)?.[1]?.trim() ?? '';
    return { _id: uid(), match: matchVal, action: actionVal || 'redact', params: {}, name: nameVal };
  }).filter((r) => r.match && VALID_ACTIONS.includes(r.action));

  if (rules.length === 0) {
    return { rules: [], error: 'No valid rules found. Make sure each rule has a "match" and "action" field.' };
  }
  return { rules, error: null };
}

function newRule(): LocalRule {
  return {
    _id: uid(),
    match: '',
    action: 'redact',
    params: {},
    name: '',
  };
}

function toApiRules(rules: LocalRule[]): ConfigRule[] {
  return rules.map(({ match, action, params, name }) => {
    const r: ConfigRule = { match, action };
    if (Object.keys(params).length > 0) r.params = params;
    if (name.trim()) r.name = name.trim();
    return r;
  });
}

// ---------------------------------------------------------------------------
// YAML serialiser (client-side preview — mirrors backend format)
// ---------------------------------------------------------------------------

function buildYamlPreview(
  name: string,
  description: string,
  rules: LocalRule[],
): string {
  if (!name && rules.length === 0) return '';

  const lines: string[] = [
    `# Config: ${name || '(unnamed)'}`,
    `# ${description}`,
    '',
    'general:',
    '  appname: SPE-FHIR-BlackBox',
    '',
    'rules:',
  ];

  for (const r of rules) {
    if (!r.match.trim()) continue;
    if (r.name.trim()) lines.push(`  - name: "${r.name.trim()}"`);
    else lines.push('  -');
    lines.push(`    match: "${r.match}"`);
    lines.push(`    action: ${r.action}`);
    if (Object.keys(r.params).length > 0) {
      lines.push('    params:');
      for (const [k, v] of Object.entries(r.params)) {
        lines.push(`      ${k}: ${v}`);
      }
    }
  }

  return lines.join('\n');
}

// ---------------------------------------------------------------------------
// ParamsEditor — contextual params UI per action
// ---------------------------------------------------------------------------

function ParamsEditor({
  action,
  params,
  onChange,
}: {
  action: Action;
  params: Record<string, unknown>;
  onChange: (p: Record<string, unknown>) => void;
}) {
  const set = (key: string, value: unknown) =>
    onChange({ ...params, [key]: value });
  const unset = (key: string) => {
    const next = { ...params };
    delete next[key];
    onChange(next);
  };

  if (action === 'redact') {
    return (
      <Input
        placeholder="Replacement (optional)"
        className="h-7 text-xs"
        value={String(params.replacement ?? '')}
        onChange={(e) =>
          e.target.value ? set('replacement', e.target.value) : unset('replacement')
        }
      />
    );
  }

  if (action === 'generalize') {
    return (
      <Select
        value={String(params.strategy ?? 'date_year')}
        onValueChange={(v) => set('strategy', v)}
      >
        <SelectTrigger className="h-7 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {GENERALIZE_STRATEGIES.map((s) => (
            <SelectItem key={s} value={s} className="text-xs">
              {s}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  if (action === 'scrub_text') {
    return (
      <div className="flex gap-1.5">
        <Select
          value={String(params.mode ?? 'text')}
          onValueChange={(v) => set('mode', v)}
        >
          <SelectTrigger className="h-7 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SCRUB_MODES.map((m) => (
              <SelectItem key={m} value={m} className="text-xs">
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={String(params.patterns ?? 'all')}
          onValueChange={(v) => set('patterns', v)}
        >
          <SelectTrigger className="h-7 w-24 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SCRUB_PATTERNS.map((p) => (
              <SelectItem key={p} value={p} className="text-xs">
                {p}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    );
  }

  if (action === 'nlp_detect') {
    return (
      <div className="flex items-center gap-1.5">
        <Select
          value={String(params.mode ?? 'tokenize')}
          onValueChange={(v) => set('mode', v)}
        >
          <SelectTrigger className="h-7 w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {NLP_MODES.map((m) => (
              <SelectItem key={m} value={m} className="text-xs">
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">threshold</span>
        <Input
          type="number"
          min={0.1}
          max={0.9}
          step={0.1}
          className="h-7 w-16 text-xs"
          value={String(params.threshold ?? 0.4)}
          onChange={(e) => set('threshold', parseFloat(e.target.value))}
        />
      </div>
    );
  }

  if (action === 'perturb') {
    return (
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-muted-foreground">±</span>
        <Input
          type="number"
          placeholder="min"
          className="h-7 w-16 text-xs"
          value={String(params.min_offset ?? '')}
          onChange={(e) =>
            e.target.value ? set('min_offset', Number(e.target.value)) : unset('min_offset')
          }
        />
        <Input
          type="number"
          placeholder="max"
          className="h-7 w-16 text-xs"
          value={String(params.max_offset ?? '')}
          onChange={(e) =>
            e.target.value ? set('max_offset', Number(e.target.value)) : unset('max_offset')
          }
        />
      </div>
    );
  }

  if (action === 'encrypt' || action === 'decrypt') {
    return (
      <Input
        placeholder="Key file path"
        className="h-7 text-xs font-mono"
        value={String(params.key_path ?? '')}
        onChange={(e) =>
          e.target.value ? set('key_path', e.target.value) : unset('key_path')
        }
      />
    );
  }

  // cryptohash, substitute, gpas_pseudonymize — no params needed
  return <span className="text-xs text-muted-foreground">—</span>;
}

// ---------------------------------------------------------------------------
// RulesTable
// ---------------------------------------------------------------------------

function RulesTable({
  rules,
  onChange,
}: {
  rules: LocalRule[];
  onChange: (rules: LocalRule[]) => void;
}) {
  const update = (id: string, patch: Partial<LocalRule>) =>
    onChange(rules.map((r) => (r._id === id ? { ...r, ...patch } : r)));

  const remove = (id: string) => onChange(rules.filter((r) => r._id !== id));

  const addRule = () => onChange([...rules, newRule()]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Rules ({rules.length})
        </span>
        <Button variant="outline" size="sm" onClick={addRule} className="h-7 text-xs">
          <Plus className="size-3" />
          Add Rule
        </Button>
      </div>

      {rules.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-10 text-center text-muted-foreground">
          <Plus className="mb-2 size-8" />
          <p className="text-sm">No rules yet. Add one above.</p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[30%] text-xs">FHIRPath Match</TableHead>
                <TableHead className="w-[15%] text-xs">Action</TableHead>
                <TableHead className="text-xs">Params</TableHead>
                <TableHead className="w-[20%] text-xs">Name (optional)</TableHead>
                <TableHead className="w-8" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rules.map((rule) => (
                <TableRow key={rule._id}>
                  <TableCell className="py-1.5">
                    <Input
                      value={rule.match}
                      onChange={(e) => update(rule._id, { match: e.target.value })}
                      placeholder="e.g. Patient.name"
                      className="h-7 font-mono text-xs"
                    />
                  </TableCell>
                  <TableCell className="py-1.5">
                    <Select
                      value={rule.action}
                      onValueChange={(v) =>
                        update(rule._id, { action: v as Action, params: {} })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {VALID_ACTIONS.map((a) => (
                          <SelectItem key={a} value={a} className="text-xs">
                            {a}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell className="py-1.5">
                    <ParamsEditor
                      action={rule.action}
                      params={rule.params}
                      onChange={(p) => update(rule._id, { params: p })}
                    />
                  </TableCell>
                  <TableCell className="py-1.5">
                    <Input
                      value={rule.name}
                      onChange={(e) => update(rule._id, { name: e.target.value })}
                      placeholder="Label..."
                      className="h-7 text-xs"
                    />
                  </TableCell>
                  <TableCell className="py-1.5 text-center">
                    <button
                      onClick={() => remove(rule._id)}
                      className="text-muted-foreground transition-colors hover:text-destructive"
                      aria-label="Remove rule"
                    >
                      <Trash2 className="size-3.5" />
                    </button>
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// ImportPanel — paste YAML to populate rule table
// ---------------------------------------------------------------------------

function ImportPanel({ onImport }: { onImport: (rules: LocalRule[]) => void }) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const handleImport = () => {
    setErr(null);
    try {
      const { rules: parsed, error: parseErr } = parseYamlIntoRules(text);
      if (parseErr) {
        setErr(parseErr);
        return;
      }
      onImport(parsed);
      setText('');
      setOpen(false);
      toast.success(`Imported ${parsed.length} rule${parsed.length !== 1 ? 's' : ''}.`);
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground">
        <Upload className="size-3" />
        Import from YAML
        <ChevronDown className={cn('size-3 transition-transform', open && 'rotate-180')} />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2">
        <div className="rounded-lg border bg-muted/30 p-3 space-y-2">
          <p className="text-xs text-muted-foreground">
            Paste an existing config YAML. The rules will be imported into the table — params require manual review.
          </p>
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste YAML here..."
            className="h-40 resize-none font-mono text-xs"
          />
          {err && (
            <p className="flex items-center gap-1 text-xs text-destructive">
              <AlertCircle className="size-3" /> {err}
            </p>
          )}
          <div className="flex gap-2">
            <Button size="sm" className="h-7 text-xs" onClick={handleImport} disabled={!text.trim()}>
              Import Rules
            </Button>
            <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}

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
  const [yamlPreviewOpen, setYamlPreviewOpen] = useState(false);

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
    () => buildYamlPreview(configName, configDescription, rules),
    [configName, configDescription, rules],
  );

  const handleSave = useCallback(async () => {
    setError(null);

    if (!configName.trim()) {
      setError('Config name is required.');
      return;
    }
    if (!/^[a-zA-Z0-9_-]{1,64}$/.test(configName)) {
      setError('Name must be 1–64 characters: letters, digits, hyphens, underscores only.');
      return;
    }
    const validRules = rules.filter((r) => r.match.trim());
    if (validRules.length === 0) {
      setError('At least one rule with a FHIRPath match is required.');
      return;
    }

    setSaving(true);
    try {
      if (isEdit && editName) {
        await updateConfig(editName, {
          description: configDescription,
          rules: toApiRules(validRules),
        });
        toast.success(`Config "${editName}" updated.`);
      } else {
        await createConfig({
          name: configName.trim(),
          description: configDescription,
          rules: toApiRules(validRules),
        });
        toast.success(`Config "${configName}" created.`);
      }
      navigate('/configs');
    } catch (err) {
      setError(String(err));
    } finally {
      setSaving(false);
    }
  }, [configName, configDescription, rules, isEdit, editName, navigate]);

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

      {/* ── Section 1: General Settings ── */}
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
      </div>

      <Separator className="mb-6" />

      {/* ── Section 2: Rules ── */}
      <div className="mb-6 space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Rules
          </p>
          <ImportPanel onImport={(imported) => setRules((prev) => [...prev, ...imported])} />
        </div>
        <RulesTable rules={rules} onChange={setRules} />
      </div>

      <Separator className="mb-6" />

      {/* ── Section 3: YAML Preview ── */}
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

      {/* ── Footer actions ── */}
      <div className="flex gap-3">
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
