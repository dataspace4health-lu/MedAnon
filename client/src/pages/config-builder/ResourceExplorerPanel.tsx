import { useState, useEffect, useCallback } from 'react';
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
  ChevronDown,
  ChevronRight,
  Layers,
  Plus,
  AlertCircle,
  Loader2,
  CheckCircle2,
  Lock,
  Shuffle,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { type Action, type LocalRule, uid, DETERMINISTIC_ACTIONS } from './configConstants';
import { FHIR_EXAMPLES } from './fhirExamples';
import { fetchFhir } from '@/api/client';
import { listResourceTypes } from '@/api/fhir';

// ---------------------------------------------------------------------------
// Action suggestion heuristic
// ---------------------------------------------------------------------------

function suggestAction(fhirPath: string): Action {
  const p = fhirPath.toLowerCase();

  // IDs and identifier values → pseudonymize (matches config_gpas.yaml: *.id, *.identifier.value)
  if (/\.id$/.test(p)) return 'gpas_pseudonymize';
  if (p.endsWith('.identifier.value') || p.includes('identifier.value')) return 'gpas_pseudonymize';

  // Structured name fields → redact
  if (
    p.includes('.name') ||
    p.includes('.family') ||
    p.includes('.given') ||
    p.includes('.prefix') ||
    p.includes('.suffix')
  )
    return 'redact';

  // Dates → generalize (year-only or year-month)
  if (
    p.includes('birthdate') ||
    p.includes('deceaseddate') ||
    (p.includes('date') && !p.includes('update') && !p.includes('candidate')) ||
    p.includes('datetime') ||
    p.endsWith('.period') ||
    p.includes('.issued') ||
    p.includes('.recorded') ||
    p.includes('authoredon')
  )
    return 'generalize';

  // Contact / telecom → redact
  if (p.includes('telecom') || p.includes('.phone') || p.includes('.email')) return 'redact';

  // Address components → redact
  if (
    p.includes('.address') ||
    p.includes('.city') ||
    p.includes('.postalcode') ||
    p.includes('.line') ||
    p.includes('.country') ||
    p.includes('.district')
  )
    return 'redact';

  // Binary / attachment data → redact
  if (p.includes('.photo') || p.includes('.attachment') || p.endsWith('.data')) return 'redact';

  // Free-text display labels → nlp_detect_act (matches config.yaml: *.display)
  if (p.endsWith('.display')) return 'nlp_detect_act';

  // Free-text / narrative fields → nlp_detect_act
  if (
    p.includes('.note') ||
    p.includes('.valuestring') ||
    p.includes('.dosage.text') ||
    p.includes('.patientinstruction') ||
    p.includes('.reaction.description') ||
    p.includes('.comment') ||
    p.includes('.conclusion') ||
    p.includes('.description') ||
    p.endsWith('.div')
  )
    return 'nlp_detect_act';

  // Structured narrative text → nlp_scrub
  if (p.endsWith('.text') || p.includes('.text.')) return 'nlp_scrub';

  if (p.includes('.contact')) return 'redact';
  if (p.includes('.reference')) return 'redact';

  return 'redact';
}

const ACTION_PILL: Record<string, string> = {
  redact:            'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-400',
  cryptohash:        'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-400',
  generalize:        'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-400',
  nlp_scrub:         'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-400',
  nlp_detect_act:    'bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-400',
  gpas_pseudonymize: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-400',
  encrypt:           'bg-sky-100 text-sky-700 dark:bg-sky-900/30 dark:text-sky-400',
  perturb:           'bg-orange-100 text-orange-700 dark:bg-orange-900/30 dark:text-orange-400',
  substitute:        'bg-teal-100 text-teal-700 dark:bg-teal-900/30 dark:text-teal-400',
  scrub_text:        'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-400',
};

// ---------------------------------------------------------------------------
// StructureDefinition → complete field schema
// ---------------------------------------------------------------------------

interface ElementDef {
  path: string;
  max?: string;
  type?: Array<{ code: string }>;
}

async function fetchStructureDefinition(type: string): Promise<ElementDef[]> {
  const sd = await fetchFhir<{ snapshot?: { element: ElementDef[] } }>(
    `/StructureDefinition/${type}`,
    {},
  );
  return sd.snapshot?.element ?? [];
}

// Write a value at the nested path in obj.
// Leaf nodes get a placeholder string like "<string>" or "<date>".
// isArray applies ONLY at the leaf — never used for intermediate node creation.
// StructureDefinition elements are parent-before-child, so intermediate nodes
// are always created by their own element pass before children reach them.
function setAtPath(
  obj: Record<string, unknown>,
  segments: string[],
  isArray: boolean,
  typeCode: string,
): void {
  if (segments.length === 0) return;
  const [head, ...tail] = segments;

  if (tail.length === 0) {
    if (!(head in obj)) {
      const ph = typeCode ? `<${typeCode}>` : '<value>';
      obj[head] = isArray ? [ph] : ph;
    }
    return;
  }

  // Intermediate: always plain object when not already present.
  if (!(head in obj)) obj[head] = {};

  const child = obj[head];
  if (Array.isArray(child)) {
    // Upgrade placeholder array (e.g. ['<HumanName>']) to object array on first child visit.
    if (!child[0] || typeof child[0] !== 'object') (obj[head] as unknown[]) = [{}];
    // Pass isArray unchanged — it must reach the leaf intact.
    setAtPath((obj[head] as Record<string, unknown>[])[0], tail, isArray, typeCode);
  } else if (typeof child === 'object' && child !== null) {
    setAtPath(child as Record<string, unknown>, tail, isArray, typeCode);
  } else {
    // Intermediate was a scalar placeholder (e.g. '<CodeableConcept>') — upgrade to object
    // so sub-paths can be added beneath it.
    obj[head] = {};
    setAtPath(obj[head] as Record<string, unknown>, tail, isArray, typeCode);
  }
}

// Convert StructureDefinition snapshot elements into a nested schema object
// where every field that the spec defines exists as a node.
function buildSchemaFromElements(
  elements: ElementDef[],
  rootType: string,
): Record<string, unknown> {
  const schema: Record<string, unknown> = {};
  for (const el of elements) {
    if (!el.path.includes('.')) continue; // skip root element
    if (el.path.includes(':')) continue;  // skip named slices
    if (el.max === '0') continue;         // prohibited in this profile
    const relative = el.path.slice(rootType.length + 1);
    if (!relative) continue;
    // Normalize polymorphic [x] choice elements to just the base name
    const normalized = relative.replace('[x]', '');
    const segments = normalized.split('.');
    const isArray = el.max === '*';
    const typeCode = el.type?.[0]?.code ?? '';
    setAtPath(schema, segments, isArray, typeCode);
  }
  return schema;
}

// ---------------------------------------------------------------------------
// Live-data union-schema helpers
// ---------------------------------------------------------------------------

function mergeValues(a: unknown, b: unknown): unknown {
  if (a === null || a === undefined) return b;
  if (b === null || b === undefined) return a;
  if (Array.isArray(a) && Array.isArray(b)) {
    const all = [...a, ...b];
    const objs = all.filter(
      (x): x is Record<string, unknown> =>
        typeof x === 'object' && x !== null && !Array.isArray(x),
    );
    const prims = all.filter((x) => typeof x !== 'object' || x === null);
    if (objs.length > 0) return [mergeObjects(objs)];
    return prims.length > 0 ? [prims[0]] : [];
  }
  if (Array.isArray(a)) return a;
  if (Array.isArray(b)) return b;
  if (typeof a === 'object' && typeof b === 'object')
    return mergeObjects([a as Record<string, unknown>, b as Record<string, unknown>]);
  return a;
}

function mergeObjects(objects: Record<string, unknown>[]): Record<string, unknown> {
  const result: Record<string, unknown> = {};
  for (const obj of objects)
    for (const [k, v] of Object.entries(obj))
      result[k] = k in result ? mergeValues(result[k], v) : v;
  return result;
}

function buildUnionSchema(resources: Record<string, unknown>[]): Record<string, unknown> {
  if (resources.length === 0) return {};
  if (resources.length === 1) return resources[0];
  return mergeObjects(resources);
}

// Overlay real values from live data on top of the spec placeholder schema.
// Spec fields absent from live data keep their <type> placeholder.
// For array fields, each live item is merged with the spec template so spec-only
// sub-fields (e.g. name.prefix, name.suffix) are never lost.
function overlayLiveValues(
  schema: Record<string, unknown>,
  live: Record<string, unknown>,
): Record<string, unknown> {
  const result = { ...schema };
  for (const [k, v] of Object.entries(live)) {
    if (v === null || v === undefined) continue;
    const sv = result[k];

    if (
      Array.isArray(sv) &&
      sv.length > 0 &&
      typeof sv[0] === 'object' && sv[0] !== null && !Array.isArray(sv[0]) &&
      Array.isArray(v) && v.length > 0
    ) {
      // Array of objects: merge spec template into each live item so spec-only
      // sub-fields remain visible even when absent from any live resource.
      const template = sv[0] as Record<string, unknown>;
      result[k] = (v as unknown[]).map((item) =>
        typeof item === 'object' && item !== null && !Array.isArray(item)
          ? overlayLiveValues(template, item as Record<string, unknown>)
          : item,
      );
    } else if (
      typeof v === 'object' &&
      !Array.isArray(v) &&
      typeof sv === 'object' &&
      !Array.isArray(sv) &&
      sv !== null
    ) {
      result[k] = overlayLiveValues(sv as Record<string, unknown>, v as Record<string, unknown>);
    } else {
      result[k] = v;
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// Recursive tree node
// ---------------------------------------------------------------------------

const SKIP_KEYS = new Set(['resourceType']);
const MAX_DEPTH = 8;
const PLACEHOLDER_RE = /^<[a-zA-Z]+>$/;

// Badge shown on a node that already has a configured rule
function ConfiguredBadge({ action }: { action: string }) {
  return (
    <span
      className={cn(
        'shrink-0 flex items-center gap-0.5 rounded px-1.5 py-px text-[10px] font-semibold ring-1',
        ACTION_PILL[action] ?? ACTION_PILL.redact,
        'ring-current/30',
      )}
    >
      <CheckCircle2 className="size-2.5" />
      {action}
    </span>
  );
}

function FhirTreeNode({
  nodeKey,
  value,
  pathSegments,
  depth,
  onAddRule,
  configuredPaths,
}: {
  nodeKey: string;
  value: unknown;
  pathSegments: string[];
  depth: number;
  onAddRule: (rule: LocalRule) => void;
  configuredPaths: Map<string, string>;
}) {
  const [open, setOpen] = useState(depth < 2);
  const fhirPath = pathSegments.join('.');

  if (SKIP_KEYS.has(nodeKey) || depth > MAX_DEPTH) return null;

  const isPlaceholder =
    typeof value === 'string' && PLACEHOLDER_RE.test(value);

  // Primitive leaf (real value or spec placeholder)
  if (value === null || value === undefined || typeof value !== 'object') {
    const configuredAction = configuredPaths.get(fhirPath);
    const isConfigured = configuredAction !== undefined;
    const suggestedAction = suggestAction(fhirPath);
    const preview = isPlaceholder
      ? (value as string)
      : String(value ?? '').slice(0, 60);
    return (
      <div
        className={cn(
          'flex items-center gap-2 py-[3px] group/node rounded-sm',
          isConfigured && 'bg-emerald-50/60 dark:bg-emerald-950/20',
        )}
      >
        <div
          style={{ paddingLeft: `${depth * 14}px` }}
          className="flex flex-1 items-center gap-1.5 min-w-0"
        >
          {isConfigured ? (
            <CheckCircle2 className="shrink-0 size-3 text-emerald-500" />
          ) : (
            <span className="shrink-0 size-1.5 rounded-full bg-muted-foreground/30" />
          )}
          <span className={cn('text-[11px] font-mono shrink-0', isConfigured ? 'text-foreground/90 font-semibold' : 'text-foreground/70')}>
            {nodeKey}
          </span>
          <span className={cn('text-[11px] truncate', isPlaceholder ? 'text-foreground/25 italic' : 'text-foreground/40')}>
            : {preview}
          </span>
        </div>
        {isConfigured ? (
          <ConfiguredBadge action={configuredAction} />
        ) : (
          <span className={cn('shrink-0 flex items-center gap-0.5 rounded px-1.5 py-px text-[10px] font-medium', ACTION_PILL[suggestedAction] ?? ACTION_PILL.redact)}>
            {DETERMINISTIC_ACTIONS.has(suggestedAction)
              ? <Lock className="size-2 shrink-0 opacity-70" />
              : <Shuffle className="size-2 shrink-0 opacity-70" />}
            {suggestedAction}
          </span>
        )}
        <button
          onClick={() =>
            onAddRule({ _id: uid(), match: fhirPath, action: suggestedAction, params: {}, name: nodeKey })
          }
          className={cn(
            'shrink-0 transition-opacity flex items-center gap-0.5 rounded border bg-background px-1.5 py-px text-[10px] font-medium',
            isConfigured
              ? 'opacity-0 group-hover/node:opacity-60 hover:bg-accent hover:border-foreground/30'
              : 'opacity-0 group-hover/node:opacity-100 hover:bg-accent hover:border-foreground/30',
          )}
        >
          <Plus className="size-2.5" />
          {isConfigured ? 'Re-add' : 'Add'}
        </button>
      </div>
    );
  }

  // Array of primitives / placeholder arrays  ── leaf-level, no expansion needed
  if (Array.isArray(value) && (value.length === 0 || typeof value[0] !== 'object')) {
    const configuredAction = configuredPaths.get(fhirPath);
    const isConfigured = configuredAction !== undefined;
    const suggestedAction = suggestAction(fhirPath);
    const first = value[0];
    const isPhArr = typeof first === 'string' && PLACEHOLDER_RE.test(first);
    const preview = isPhArr
      ? (first as string)
      : value.slice(0, 3).map(String).join(', ');
    return (
      <div
        className={cn(
          'flex items-center gap-2 py-[3px] group/node rounded-sm',
          isConfigured && 'bg-emerald-50/60 dark:bg-emerald-950/20',
        )}
      >
        <div
          style={{ paddingLeft: `${depth * 14}px` }}
          className="flex flex-1 items-center gap-1.5 min-w-0"
        >
          {isConfigured ? (
            <CheckCircle2 className="shrink-0 size-3 text-emerald-500" />
          ) : (
            <span className="shrink-0 size-1.5 rounded-full bg-muted-foreground/30" />
          )}
          <span className={cn('text-[11px] font-mono shrink-0', isConfigured ? 'text-foreground/90 font-semibold' : 'text-foreground/70')}>
            {nodeKey}
          </span>
          <span className={cn('text-[11px] truncate', isPhArr ? 'text-foreground/25 italic' : 'text-foreground/40')}>
            : [{preview}]
          </span>
        </div>
        {isConfigured ? (
          <ConfiguredBadge action={configuredAction} />
        ) : (
          <span className={cn('shrink-0 flex items-center gap-0.5 rounded px-1.5 py-px text-[10px] font-medium', ACTION_PILL[suggestedAction] ?? ACTION_PILL.redact)}>
            {DETERMINISTIC_ACTIONS.has(suggestedAction)
              ? <Lock className="size-2 shrink-0 opacity-70" />
              : <Shuffle className="size-2 shrink-0 opacity-70" />}
            {suggestedAction}
          </span>
        )}
        <button
          onClick={() =>
            onAddRule({ _id: uid(), match: fhirPath, action: suggestedAction, params: {}, name: nodeKey })
          }
          className={cn(
            'shrink-0 transition-opacity flex items-center gap-0.5 rounded border bg-background px-1.5 py-px text-[10px] font-medium',
            isConfigured
              ? 'opacity-0 group-hover/node:opacity-60 hover:bg-accent hover:border-foreground/30'
              : 'opacity-0 group-hover/node:opacity-100 hover:bg-accent hover:border-foreground/30',
          )}
        >
          <Plus className="size-2.5" />
          {isConfigured ? 'Re-add' : 'Add'}
        </button>
      </div>
    );
  }

  // Array of objects
  if (Array.isArray(value)) {
    const first = value[0] as Record<string, unknown>;
    const entries = Object.entries(first ?? {});
    return (
      <div>
        <button
          onClick={() => setOpen((o) => !o)}
          style={{ paddingLeft: `${depth * 14}px` }}
          className="flex w-full items-center gap-1 py-[3px] text-[11px] font-mono text-muted-foreground hover:text-foreground transition-colors"
        >
          {open ? (
            <ChevronDown className="size-3 shrink-0" />
          ) : (
            <ChevronRight className="size-3 shrink-0" />
          )}
          <span>{nodeKey}</span>
          <span className="ml-1 text-[10px] font-normal text-foreground/30">[array]</span>
        </button>
        {open && (
          <div>
            {entries.map(([k, v]) => (
              <FhirTreeNode
                key={k}
                nodeKey={k}
                value={v}
                pathSegments={[...pathSegments, k]}
                depth={depth + 1}
                onAddRule={onAddRule}
                configuredPaths={configuredPaths}
              />
            ))}
          </div>
        )}
      </div>
    );
  }

  // Plain object
  const entries = Object.entries(value as Record<string, unknown>);
  return (
    <div>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{ paddingLeft: `${depth * 14}px` }}
        className="flex w-full items-center gap-1 py-[3px] text-[11px] font-mono text-muted-foreground hover:text-foreground transition-colors"
      >
        {open ? (
          <ChevronDown className="size-3 shrink-0" />
        ) : (
          <ChevronRight className="size-3 shrink-0" />
        )}
        <span>{nodeKey}</span>
        <span className="ml-1 text-[10px] font-normal text-foreground/30">
          {`{${entries.length}}`}
        </span>
      </button>
      {open && (
        <div>
          {entries.map(([k, v]) => (
            <FhirTreeNode
              key={k}
              nodeKey={k}
              value={v}
              pathSegments={[...pathSegments, k]}
              depth={depth + 1}
              onAddRule={onAddRule}
              configuredPaths={configuredPaths}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Discovery helpers
// ---------------------------------------------------------------------------

interface FhirBundle {
  resourceType: 'Bundle';
  total?: number;
  entry?: Array<{ resource?: Record<string, unknown> }>;
}

const TYPE_PRIORITY = [
  'Patient', 'Practitioner', 'Observation', 'Condition', 'Encounter',
  'MedicationRequest', 'Procedure', 'AllergyIntolerance', 'DiagnosticReport',
  'ImagingStudy', 'Immunization', 'CarePlan', 'Organization', 'Location',
];

function sortTypes(types: string[]): string[] {
  return [...types].sort((a, b) => {
    const pa = TYPE_PRIORITY.indexOf(a);
    const pb = TYPE_PRIORITY.indexOf(b);
    if (pa !== -1 && pb !== -1) return pa - pb;
    if (pa !== -1) return -1;
    if (pb !== -1) return 1;
    return a.localeCompare(b);
  });
}

function fetchFhirTimed<T>(
  path: string,
  params: Record<string, string>,
  timeoutMs: number,
): Promise<T> {
  return Promise.race([
    fetchFhir<T>(path, params),
    new Promise<never>((_, reject) =>
      setTimeout(() => reject(new Error('timeout')), timeoutMs),
    ),
  ]);
}

function getFallbackResource(type: string): Record<string, unknown> | null {
  return FHIR_EXAMPLES.find((e) => e.resourceType === type)?.resource ?? null;
}

// ---------------------------------------------------------------------------
// Panel component
// ---------------------------------------------------------------------------

type DiscoveryState = 'idle' | 'discovering' | 'done';
type TypeLoadState = 'loading' | 'done' | 'error';

export function ResourceExplorerPanel({
  onAddRule,
  rules = [],
}: {
  onAddRule: (rule: LocalRule) => void;
  rules?: LocalRule[];
}) {
  const [open, setOpen] = useState(false);
  const [discoveryState, setDiscoveryState] = useState<DiscoveryState>('idle');
  const [availableTypes, setAvailableTypes] = useState<string[]>([]);
  const [selectedType, setSelectedType] = useState<string | null>(null);

  // Per-type: final merged schema (spec + live overlay) and metadata
  const [typeSchemas, setTypeSchemas] = useState<Record<string, Record<string, unknown>>>({});
  const [typeLoadStates, setTypeLoadStates] = useState<Record<string, TypeLoadState>>({});
  const [typeLiveCounts, setTypeLiveCounts] = useState<Record<string, number>>({});

  // Discovery: probe each type with _count=1 (fast) just to see if it has data.
  const runDiscovery = useCallback(async () => {
    setDiscoveryState('discovering');
    const allTypes = await listResourceTypes().catch(() =>
      FHIR_EXAMPLES.map((e) => e.resourceType),
    );

    const found: string[] = [];
    await Promise.allSettled(
      allTypes.map(async (type) => {
        try {
          const bundle = await fetchFhirTimed<FhirBundle>(
            `/${type}`,
            { _count: '1' },
            6_000,
          );
          if ((bundle.entry?.length ?? 0) > 0 || (bundle.total ?? 0) > 0) {
            found.push(type);
          }
        } catch { /* type unreachable or empty — skip */ }
      }),
    );

    const types =
      found.length > 0
        ? sortTypes(found)
        : sortTypes(FHIR_EXAMPLES.map((e) => e.resourceType));

    setAvailableTypes(types);
    setSelectedType(types[0] ?? null);
    setDiscoveryState('done');
  }, []);

  // Schema loading: fetch StructureDefinition (complete spec) + 20 live resources,
  // then overlay live values on the spec schema so every field is always visible.
  const loadTypeSchema = useCallback(async (type: string) => {
    setTypeLoadStates((prev) => ({ ...prev, [type]: 'loading' }));
    try {
      const [sdResult, bundleResult] = await Promise.allSettled([
        fetchStructureDefinition(type),
        fetchFhirTimed<FhirBundle>(`/${type}`, { _count: '20' }, 10_000),
      ]);

      const elements = sdResult.status === 'fulfilled' ? sdResult.value : [];
      const resources =
        bundleResult.status === 'fulfilled'
          ? (bundleResult.value.entry ?? [])
              .map((e) => e.resource)
              .filter((r): r is Record<string, unknown> => Boolean(r))
          : [];

      let schema: Record<string, unknown>;

      if (elements.length > 0) {
        // Primary path: build complete spec schema, then overlay real example values
        const specSchema = buildSchemaFromElements(elements, type);
        const liveUnion = resources.length > 0 ? buildUnionSchema(resources) : {};
        schema = overlayLiveValues(specSchema, liveUnion);
      } else {
        // StructureDefinition unavailable — fall back to union of live data
        schema =
          resources.length > 0
            ? buildUnionSchema(resources)
            : (getFallbackResource(type) ?? {});
      }

      setTypeSchemas((prev) => ({ ...prev, [type]: schema }));
      setTypeLiveCounts((prev) => ({ ...prev, [type]: resources.length }));
      setTypeLoadStates((prev) => ({ ...prev, [type]: 'done' }));
    } catch {
      const fallback = getFallbackResource(type);
      setTypeSchemas((prev) => ({ ...prev, [type]: fallback ?? {} }));
      setTypeLoadStates((prev) => ({ ...prev, [type]: 'error' }));
    }
  }, []);

  useEffect(() => {
    if (open && discoveryState === 'idle') runDiscovery();
  }, [open, discoveryState, runDiscovery]);

  // Load schema lazily the first time a type is selected
  useEffect(() => {
    if (selectedType && !typeLoadStates[selectedType] && discoveryState === 'done') {
      loadTypeSchema(selectedType);
    }
  }, [selectedType, typeLoadStates, discoveryState, loadTypeSchema]);

  const handleAddRule = (rule: LocalRule) => {
    onAddRule(rule);
    toast.success(`Rule added: ${rule.match}`, { duration: 1500 });
  };

  const schema = selectedType ? typeSchemas[selectedType] : null;
  const loadState = selectedType ? typeLoadStates[selectedType] : undefined;
  const liveCount = selectedType ? (typeLiveCounts[selectedType] ?? 0) : 0;
  const topEntries = schema
    ? Object.entries(schema).filter(([k]) => k !== 'resourceType')
    : [];

  // Build a path→action lookup from all current rules for O(1) highlighting
  const configuredPaths = new Map<string, string>(
    rules.filter((r) => r.match.trim()).map((r) => [r.match.trim(), r.action]),
  );

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground">
        <Layers className="size-3" />
        Resource Explorer
        <ChevronDown className={cn('size-3 transition-transform', open && 'rotate-180')} />
      </CollapsibleTrigger>

      <CollapsibleContent className="mt-2">
        <div className="rounded-lg border bg-muted/30 p-3 space-y-3">

          {/* Header row */}
          <div className="flex items-start justify-between gap-3">
            <p className="text-xs text-muted-foreground leading-relaxed">
              {discoveryState === 'discovering' ? (
                'Querying your FHIR server…'
              ) : discoveryState === 'done' ? (
                <>
                  All FHIR R4 fields from the spec, with real values from your server.
                  {availableTypes.length > 0 && (
                    <span className="ml-1 text-foreground/50">
                      ({availableTypes.length} types found)
                    </span>
                  )}
                  {' '}Hover a field and click{' '}
                  <span className="font-medium text-foreground">Add</span> to create a rule.
                </>
              ) : null}
            </p>

            {discoveryState === 'done' && availableTypes.length > 0 && (
              <Select
                value={selectedType ?? ''}
                onValueChange={(v) => v && setSelectedType(v)}
              >
                <SelectTrigger className="h-7 w-52 text-xs shrink-0">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {availableTypes.map((t) => (
                    <SelectItem key={t} value={t} className="text-xs font-mono">
                      {t}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>

          {/* Tree area */}
          <div
            className="rounded-md border bg-background overflow-y-auto"
            style={{ maxHeight: '620px' }}
          >
            {discoveryState === 'discovering' ? (
              <div className="flex flex-col items-center justify-center gap-2 py-12 text-xs text-muted-foreground">
                <Loader2 className="size-5 animate-spin" />
                <span>Discovering resource types…</span>
                <span className="text-[10px] text-foreground/30">
                  Probing each type — runs once per session
                </span>
              </div>
            ) : availableTypes.length === 0 ? (
              <div className="flex items-center gap-2 py-6 px-3 text-xs text-muted-foreground">
                <AlertCircle className="size-4 shrink-0" />
                No resources found on the FHIR server.
              </div>
            ) : !loadState || loadState === 'loading' ? (
              <div className="flex flex-col items-center justify-center gap-2 py-12 text-xs text-muted-foreground">
                <Loader2 className="size-5 animate-spin" />
                <span>Loading {selectedType} schema…</span>
              </div>
            ) : schema && topEntries.length > 0 ? (
              <div className="p-2">
                <div className="flex items-center justify-between pb-1.5 mb-1 border-b">
                  <span className="text-[11px] font-mono font-semibold">
                    {selectedType}
                  </span>
                  <span className="text-[10px] text-muted-foreground">
                    {liveCount > 0
                      ? `spec + ${liveCount} live resource${liveCount !== 1 ? 's' : ''}`
                      : 'spec only — no live data'}
                  </span>
                </div>
                {topEntries.map(([k, v]) => (
                  <FhirTreeNode
                    key={k}
                    nodeKey={k}
                    value={v}
                    pathSegments={[selectedType!, k]}
                    depth={1}
                    onAddRule={handleAddRule}
                    configuredPaths={configuredPaths}
                  />
                ))}
              </div>
            ) : (
              <div className="flex items-center gap-2 py-6 px-3 text-xs text-muted-foreground">
                <AlertCircle className="size-4 shrink-0" />
                Could not load schema for {selectedType}.
              </div>
            )}
          </div>

          <p className="text-[10px] text-muted-foreground">
            Dimmed italic fields (<span className="italic text-foreground/30">&lt;type&gt;</span>)
            are spec-defined but absent from your data. Suggested actions are inferred
            from field names — review before saving.
          </p>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
