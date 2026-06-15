import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  ChevronDown,
  ChevronRight,
  Layers,
  AlertCircle,
  Loader2,
  CheckCircle2,
  Search,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import {
  type Action,
  type LocalRule,
  uid,
  VALID_ACTIONS,
  deduplicateIncoming,
  actionLabel,
} from './configConstants';
import { FHIR_EXAMPLES } from './fhirExamples';
import { fetchFhir } from '@/api/client';
import { listResourceTypes } from '@/api/fhir';

// ---------------------------------------------------------------------------
// Action suggestion heuristic
// ---------------------------------------------------------------------------

function suggestAction(fhirPath: string): Action {
  const p = fhirPath.toLowerCase();

  if (/\.id$/.test(p)) return 'gpas_pseudonymize';
  if (p.endsWith('.identifier.value') || p.includes('identifier.value')) return 'gpas_pseudonymize';

  if (
    p.includes('.name') || p.includes('.family') || p.includes('.given') ||
    p.includes('.prefix') || p.includes('.suffix')
  ) return 'redact';

  if (
    p.includes('birthdate') || p.includes('deceaseddate') ||
    (p.includes('date') && !p.includes('update') && !p.includes('candidate')) ||
    p.includes('datetime') || p.endsWith('.period') || p.includes('.issued') ||
    p.includes('.recorded') || p.includes('authoredon')
  ) return 'generalize';

  if (p.includes('telecom') || p.includes('.phone') || p.includes('.email')) return 'redact';

  if (
    p.includes('.address') || p.includes('.city') || p.includes('.postalcode') ||
    p.includes('.line') || p.includes('.country') || p.includes('.district')
  ) return 'redact';

  if (p.includes('.photo') || p.includes('.attachment') || p.endsWith('.data')) return 'redact';
  if (p.endsWith('.display')) return 'nlp_detect_act';

  if (
    p.includes('.note') || p.includes('.valuestring') || p.includes('.dosage.text') ||
    p.includes('.patientinstruction') || p.includes('.reaction.description') ||
    p.includes('.comment') || p.includes('.conclusion') || p.includes('.description') ||
    p.endsWith('.div')
  ) return 'nlp_detect_act';

  if (p.endsWith('.text') || p.includes('.text.')) return 'nlp_scrub';
  if (p.includes('.contact')) return 'redact';
  if (p.includes('.reference')) return 'redact';

  return 'redact';
}

// ---------------------------------------------------------------------------
// StructureDefinition → complete field schema (unchanged logic)
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
  if (!(head in obj)) obj[head] = {};
  const child = obj[head];
  if (Array.isArray(child)) {
    if (!child[0] || typeof child[0] !== 'object') (obj[head] as unknown[]) = [{}];
    setAtPath((obj[head] as Record<string, unknown>[])[0], tail, isArray, typeCode);
  } else if (typeof child === 'object' && child !== null) {
    setAtPath(child as Record<string, unknown>, tail, isArray, typeCode);
  } else {
    obj[head] = {};
    setAtPath(obj[head] as Record<string, unknown>, tail, isArray, typeCode);
  }
}

function buildSchemaFromElements(
  elements: ElementDef[],
  rootType: string,
): Record<string, unknown> {
  const schema: Record<string, unknown> = {};
  for (const el of elements) {
    if (!el.path.includes('.')) continue;
    if (el.path.includes(':')) continue;
    if (el.max === '0') continue;
    const relative = el.path.slice(rootType.length + 1);
    if (!relative) continue;
    const normalized = relative.replace('[x]', '');
    const segments = normalized.split('.');
    const isArray = el.max === '*';
    const typeCode = el.type?.[0]?.code ?? '';
    setAtPath(schema, segments, isArray, typeCode);
  }
  return schema;
}

// ---------------------------------------------------------------------------
// Live-data union-schema helpers (unchanged logic)
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

function overlayLiveValues(
  schema: Record<string, unknown>,
  live: Record<string, unknown>,
): Record<string, unknown> {
  const result = { ...schema };
  for (const [k, v] of Object.entries(live)) {
    if (v === null || v === undefined) continue;
    const sv = result[k];
    if (
      Array.isArray(sv) && sv.length > 0 &&
      typeof sv[0] === 'object' && sv[0] !== null && !Array.isArray(sv[0]) &&
      Array.isArray(v) && v.length > 0
    ) {
      const template = sv[0] as Record<string, unknown>;
      result[k] = (v as unknown[]).map((item) =>
        typeof item === 'object' && item !== null && !Array.isArray(item)
          ? overlayLiveValues(template, item as Record<string, unknown>)
          : item,
      );
    } else if (
      typeof v === 'object' && !Array.isArray(v) &&
      typeof sv === 'object' && !Array.isArray(sv) && sv !== null
    ) {
      result[k] = overlayLiveValues(sv as Record<string, unknown>, v as Record<string, unknown>);
    } else {
      result[k] = v;
    }
  }
  return result;
}

// ---------------------------------------------------------------------------
// Leaf-path collection — for "sub-field values only" treatment
// ---------------------------------------------------------------------------

const MAX_DEPTH = 8;
const PLACEHOLDER_RE = /^<[a-zA-Z]+>$/;

/** Collect every scalar leaf FHIRPath under a node (recursing into objects /
 * arrays). Used when the user picks "sub-field values only": one redact rule
 * per identifying leaf, keeping the parent structure intact. */
function collectLeafPaths(
  node: unknown,
  pathSegments: string[],
  depth: number,
  acc: string[],
): void {
  if (depth > MAX_DEPTH) return;
  if (Array.isArray(node)) {
    if (node.length > 0) collectLeafPaths(node[0], pathSegments, depth, acc);
    else acc.push(pathSegments.join('.'));
    return;
  }
  if (typeof node === 'object' && node !== null) {
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
      if (k === 'resourceType') continue;
      collectLeafPaths(v, [...pathSegments, k], depth + 1, acc);
    }
    return;
  }
  acc.push(pathSegments.join('.'));
}

function isLeaf(value: unknown): boolean {
  if (value === null || value === undefined || typeof value !== 'object') return true;
  if (Array.isArray(value) && (value.length === 0 || typeof value[0] !== 'object')) return true;
  return false;
}

// ---------------------------------------------------------------------------
// Field-nature flags — hint the user (and the rule they add) about a field's
// content type. Base64 attachment data needs base64_encoded; date fields are
// usually generalize/date_shift candidates.
// ---------------------------------------------------------------------------

type FieldFlag = 'base64' | 'date' | 'freetext' | 'geo' | 'sensitive';

// Each flag carries a recommended action (and params) so a single click applies
// the right treatment for that field's content type.
const FLAG_META: Record<
  FieldFlag,
  { label: string; title: string; cls: string; recommend: { action: Action; params?: Record<string, unknown>; label: string } }
> = {
  base64: {
    label: 'base64',
    title: 'Base64-encoded attachment payload. Recommended: NLP scrub with base64_encoded so the payload is decoded before scrubbing, then re-encoded.',
    cls: 'bg-violet-100 text-violet-700 dark:bg-violet-900/30 dark:text-violet-300',
    recommend: { action: 'nlp_scrub', params: { base64_encoded: true }, label: 'NLP scrub (base64)' },
  },
  date: {
    label: 'date',
    title: 'Date / timestamp field. Recommended: generalize to year / year-month (or date_shift) to preserve temporal utility.',
    cls: 'bg-amber-100 text-amber-700 dark:bg-amber-900/30 dark:text-amber-300',
    recommend: { action: 'generalize', params: { strategy: 'date_year' }, label: 'Generalize (year)' },
  },
  freetext: {
    label: 'free-text',
    title: 'Free-text / narrative field. Recommended: NLP scrub to remove embedded names / dates / locations while keeping clinical content.',
    cls: 'bg-pink-100 text-pink-700 dark:bg-pink-900/30 dark:text-pink-300',
    recommend: { action: 'nlp_scrub', label: 'NLP scrub' },
  },
  geo: {
    label: 'geo',
    title: 'Precise geolocation (latitude / longitude) — a HIPAA Safe Harbor identifier. Recommended: redact these coordinates.',
    cls: 'bg-cyan-100 text-cyan-700 dark:bg-cyan-900/30 dark:text-cyan-300',
    recommend: { action: 'redact', label: 'Redact' },
  },
  sensitive: {
    label: 'sensitive',
    title: 'Sensitive attribute (race / ethnicity / religion). Recommended: redact if your de-identification policy requires it.',
    cls: 'bg-rose-100 text-rose-700 dark:bg-rose-900/30 dark:text-rose-300',
    recommend: { action: 'redact', label: 'Redact' },
  },
};

/** Infer content-nature flags for a leaf field from its path. Purely advisory —
 * drives the badge hints, not the rule action. */
function fieldFlags(fhirPath: string): FieldFlag[] {
  const p = fhirPath.toLowerCase();
  const flags: FieldFlag[] = [];
  // Base64 attachment data: FHIR Attachment.data / inline data URIs.
  if (p.endsWith('.data') || p.includes('attachment.data') || p.endsWith('.presentedform.data')) {
    flags.push('base64');
  }
  // Date / timestamp fields.
  if (
    p.endsWith('date') || p.endsWith('datetime') || p.includes('.period') ||
    p.endsWith('.start') || p.endsWith('.end') || p.endsWith('.issued') ||
    p.endsWith('.recorded') || p.endsWith('.recordeddate') || p.endsWith('authoredon') ||
    p.endsWith('.occurrencedatetime') || p.endsWith('.effectivedatetime')
  ) {
    flags.push('date');
  }
  // Free-text / narrative.
  if (
    p.endsWith('.text') || p.includes('.text.') || p.endsWith('.note') ||
    p.endsWith('.description') || p.endsWith('.comment') || p.endsWith('.conclusion') ||
    p.endsWith('.div') || p.endsWith('.patientinstruction')
  ) {
    flags.push('freetext');
  }
  // Precise geolocation (lat/long) — HIPAA Safe Harbor identifier. Catches the
  // FHIR geolocation extension's latitude/longitude leaves and position fields.
  if (
    p.endsWith('.latitude') || p.endsWith('.longitude') ||
    p.endsWith('.position.latitude') || p.endsWith('.position.longitude') ||
    p.includes('geolocation')
  ) {
    flags.push('geo');
  }
  // Sensitive attributes (race / ethnicity / religion). These commonly live in
  // US Core extensions (Patient.extension…ombCategory / .text) and Patient
  // fields. We flag by keyword so the user can decide their policy.
  if (
    p.includes('race') || p.includes('ethnic') || p.includes('religion') ||
    p.endsWith('.maritalstatus.text')
  ) {
    flags.push('sensitive');
  }
  return flags;
}

// ---------------------------------------------------------------------------
// Treatment modes
// ---------------------------------------------------------------------------

type Treatment = 'value' | 'subfields' | 'everything';

const TREATMENT_LABELS: Record<Treatment, string> = {
  value: 'Value only (keep key)',
  subfields: 'Sub-field values only (keep structure)',
  everything: 'Everything (key + value)',
};

const TREATMENT_HELP: Record<Treatment, string> = {
  value: 'Blank the value at this exact path; the key stays. Best for leaf fields.',
  subfields: 'Add one rule per identifying leaf under this object; all keys & nesting stay.',
  everything: 'Redact the whole node at this path — removes its entire content.',
};

// Actions that scrub text and therefore honour the base64_encoded param.
const NLP_ACTIONS = new Set<Action>(['nlp_scrub', 'nlp_detect_act']);

/** Default params for a (path, action) pair — auto-sets base64_encoded on an
 * NLP rule targeting a Base64 attachment field so the payload is decoded before
 * scrubbing. */
function defaultParamsFor(fhirPath: string, action: Action): Record<string, unknown> {
  if (NLP_ACTIONS.has(action) && fieldFlags(fhirPath).includes('base64')) {
    return { base64_encoded: true };
  }
  return {};
}

// Expand one selected (path, treatment) into the concrete rules it implies.
function expandSelection(
  fhirPath: string,
  value: unknown,
  treatment: Treatment,
  action: Action,
): LocalRule[] {
  const name = fhirPath.split('.').slice(1).join('.') || fhirPath;

  if (treatment === 'everything') {
    return [{ _id: uid(), match: fhirPath, action, params: defaultParamsFor(fhirPath, action), name }];
  }
  if (treatment === 'value' || isLeaf(value)) {
    // For a leaf, "value" and "subfields" both mean: rule on this path.
    return [{ _id: uid(), match: fhirPath, action, params: defaultParamsFor(fhirPath, action), name }];
  }
  // subfields on a non-leaf: one rule per leaf descendant, action per-leaf suggested.
  const leaves: string[] = [];
  collectLeafPaths(value, fhirPath.split('.'), fhirPath.split('.').length, leaves);
  const unique = [...new Set(leaves)];
  return unique.map((lp) => {
    const leafAction = suggestAction(lp);
    return {
      _id: uid(),
      match: lp,
      action: leafAction,
      params: defaultParamsFor(lp, leafAction),
      name: lp.split('.').slice(1).join('.'),
    };
  });
}

// ---------------------------------------------------------------------------
// Tree node — checkbox-selectable
// ---------------------------------------------------------------------------

const SKIP_KEYS = new Set(['resourceType']);

function TreeNode({
  nodeKey,
  value,
  pathSegments,
  depth,
  selected,
  configuredPaths,
  onToggle,
  filter,
}: {
  nodeKey: string;
  value: unknown;
  pathSegments: string[];
  depth: number;
  selected: Set<string>;
  configuredPaths: Map<string, string>;
  onToggle: (path: string, value: unknown) => void;
  filter: string;
}) {
  const [open, setOpen] = useState(depth < 2);
  const fhirPath = pathSegments.join('.');

  if (SKIP_KEYS.has(nodeKey) || depth > MAX_DEPTH) return null;

  // Filter: when a query is present, show a node if it (or any descendant) matches.
  const matchesFilter =
    !filter || fhirPath.toLowerCase().includes(filter.toLowerCase());

  const leaf = isLeaf(value);
  const isPlaceholder = typeof value === 'string' && PLACEHOLDER_RE.test(value);
  const configuredAction = configuredPaths.get(fhirPath);
  const isChecked = selected.has(fhirPath);

  if (leaf) {
    if (!matchesFilter) return null;
    const preview = Array.isArray(value)
      ? `[${value.slice(0, 2).map(String).join(', ')}]`
      : isPlaceholder
        ? (value as string)
        : String(value ?? '').slice(0, 50);
    return (
      <label
        style={{ paddingLeft: `${depth * 14}px` }}
        className={cn(
          'flex items-center gap-2 py-[3px] rounded-sm cursor-pointer hover:bg-accent/50',
          isChecked && 'bg-[#0072bc]/10',
        )}
      >
        <Checkbox
          checked={isChecked}
          onCheckedChange={() => onToggle(fhirPath, value)}
          className="size-3.5 shrink-0"
        />
        <span className="text-[11px] font-mono shrink-0 text-foreground/80">{nodeKey}</span>
        <span className={cn('text-[11px] truncate flex-1', isPlaceholder ? 'text-foreground/25 italic' : 'text-foreground/40')}>
          : {preview}
        </span>
        {fieldFlags(fhirPath).map((f) => (
          <span
            key={f}
            title={FLAG_META[f].title}
            className={cn(
              'shrink-0 rounded px-1 py-px text-[9px] font-medium',
              FLAG_META[f].cls,
            )}
          >
            {FLAG_META[f].label}
          </span>
        ))}
        {configuredAction && (
          <span className="shrink-0 inline-flex items-center gap-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 className="size-2.5" />
            {configuredAction}
          </span>
        )}
      </label>
    );
  }

  // Container node (object or array of objects)
  const entries = Array.isArray(value)
    ? Object.entries((value[0] as Record<string, unknown>) ?? {})
    : Object.entries(value as Record<string, unknown>);

  return (
    <div>
      <div
        style={{ paddingLeft: `${depth * 14}px` }}
        className="flex items-center gap-2 py-[3px] rounded-sm hover:bg-accent/40"
      >
        <Checkbox
          checked={isChecked}
          onCheckedChange={() => onToggle(fhirPath, value)}
          className="size-3.5 shrink-0"
          title="Select this whole object"
        />
        <button
          onClick={() => setOpen((o) => !o)}
          className="flex items-center gap-1 text-[11px] font-mono text-muted-foreground hover:text-foreground transition-colors min-w-0"
        >
          {open ? <ChevronDown className="size-3 shrink-0" /> : <ChevronRight className="size-3 shrink-0" />}
          <span>{nodeKey}</span>
          <span className="ml-1 text-[10px] font-normal text-foreground/30">
            {Array.isArray(value) ? '[array]' : `{${entries.length}}`}
          </span>
        </button>
        {configuredAction && (
          <span className="shrink-0 inline-flex items-center gap-0.5 text-[10px] text-emerald-600 dark:text-emerald-400">
            <CheckCircle2 className="size-2.5" />
            {configuredAction}
          </span>
        )}
      </div>
      {open && (
        <div>
          {entries.map(([k, v]) => (
            <TreeNode
              key={k}
              nodeKey={k}
              value={v}
              pathSegments={[...pathSegments, k]}
              depth={depth + 1}
              selected={selected}
              configuredPaths={configuredPaths}
              onToggle={onToggle}
              filter={filter}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Discovery helpers (unchanged)
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
// Modal component
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

  const [typeSchemas, setTypeSchemas] = useState<Record<string, Record<string, unknown>>>({});
  const [typeLoadStates, setTypeLoadStates] = useState<Record<string, TypeLoadState>>({});
  const [typeLiveCounts, setTypeLiveCounts] = useState<Record<string, number>>({});

  // Selection: path → captured value snapshot (needed for sub-field expansion).
  const [selected, setSelected] = useState<Map<string, unknown>>(new Map());
  const [treatment, setTreatment] = useState<Treatment>('value');
  const [bulkAction, setBulkAction] = useState<Action>('redact');
  const [filter, setFilter] = useState('');

  const runDiscovery = useCallback(async () => {
    setDiscoveryState('discovering');
    const allTypes = await listResourceTypes().catch(() =>
      FHIR_EXAMPLES.map((e) => e.resourceType),
    );
    const found: string[] = [];
    await Promise.allSettled(
      allTypes.map(async (type) => {
        try {
          const bundle = await fetchFhirTimed<FhirBundle>(`/${type}`, { _count: '1' }, 6_000);
          if ((bundle.entry?.length ?? 0) > 0 || (bundle.total ?? 0) > 0) found.push(type);
        } catch { /* skip */ }
      }),
    );
    const types = found.length > 0
      ? sortTypes(found)
      : sortTypes(FHIR_EXAMPLES.map((e) => e.resourceType));
    setAvailableTypes(types);
    setSelectedType(types[0] ?? null);
    setDiscoveryState('done');
  }, []);

  const loadTypeSchema = useCallback(async (type: string) => {
    setTypeLoadStates((prev) => ({ ...prev, [type]: 'loading' }));
    try {
      const [sdResult, bundleResult] = await Promise.allSettled([
        fetchStructureDefinition(type),
        fetchFhirTimed<FhirBundle>(`/${type}`, { _count: '20' }, 10_000),
      ]);
      const elements = sdResult.status === 'fulfilled' ? sdResult.value : [];
      const resources = bundleResult.status === 'fulfilled'
        ? (bundleResult.value.entry ?? []).map((e) => e.resource).filter((r): r is Record<string, unknown> => Boolean(r))
        : [];
      let schema: Record<string, unknown>;
      if (elements.length > 0) {
        const specSchema = buildSchemaFromElements(elements, type);
        const liveUnion = resources.length > 0 ? buildUnionSchema(resources) : {};
        schema = overlayLiveValues(specSchema, liveUnion);
      } else {
        schema = resources.length > 0 ? buildUnionSchema(resources) : (getFallbackResource(type) ?? {});
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

  useEffect(() => {
    if (selectedType && !typeLoadStates[selectedType] && discoveryState === 'done') {
      loadTypeSchema(selectedType);
    }
  }, [selectedType, typeLoadStates, discoveryState, loadTypeSchema]);

  // Clear selection when switching resource type (paths are type-scoped).
  useEffect(() => {
    setSelected(new Map());
    setFilter('');
  }, [selectedType]);

  const toggleSelect = (path: string, value: unknown) => {
    setSelected((prev) => {
      const next = new Map(prev);
      if (next.has(path)) next.delete(path);
      else next.set(path, value);
      return next;
    });
  };

  const schema = selectedType ? typeSchemas[selectedType] : null;
  const loadState = selectedType ? typeLoadStates[selectedType] : undefined;
  const liveCount = selectedType ? (typeLiveCounts[selectedType] ?? 0) : 0;
  const topEntries = schema ? Object.entries(schema).filter(([k]) => k !== 'resourceType') : [];

  const configuredPaths = useMemo(
    () => new Map<string, string>(rules.filter((r) => r.match.trim()).map((r) => [r.match.trim(), r.action])),
    [rules],
  );

  // Expand the current selection into concrete rules under the chosen treatment.
  const pendingRules = useMemo(() => {
    const all: LocalRule[] = [];
    for (const [path, value] of selected.entries()) {
      all.push(...expandSelection(path, value, treatment, bulkAction));
    }
    return all;
  }, [selected, treatment, bulkAction]);

  const handleApply = () => {
    if (pendingRules.length === 0) return;
    const { added, skipped } = deduplicateIncoming(pendingRules, rules);
    for (const r of added) onAddRule(r);
    toast.success(
      skipped.length > 0
        ? `Added ${added.length} rule${added.length !== 1 ? 's' : ''}; skipped ${skipped.length} duplicate${skipped.length !== 1 ? 's' : ''}.`
        : `Added ${added.length} rule${added.length !== 1 ? 's' : ''}.`,
    );
    setSelected(new Map());
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <button className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground" />
        }
      >
        <Layers className="size-3" />
        Resource Explorer
      </DialogTrigger>

      <DialogContent className="max-w-5xl! w-full p-0 gap-0 sm:max-w-5xl!">
        <DialogHeader className="border-b p-4">
          <DialogTitle className="flex items-center gap-2 text-base">
            <Layers className="size-4" />
            Resource Field Explorer
          </DialogTitle>
          <DialogDescription className="text-xs">
            Pick a resource type, check the fields to de-identify, choose how to treat
            them, then add the rules. Fields shown are the full FHIR R4 spec overlaid
            with real values from your server.
          </DialogDescription>
        </DialogHeader>

        <div className="flex h-[60vh] min-h-[420px] px-4 py-3">
          {/* Left: resource type list */}
          <div className="w-52 shrink-0 border-r overflow-y-auto bg-muted/20 rounded-l-md border-y border-l">
            {discoveryState === 'discovering' ? (
              <div className="flex flex-col items-center gap-2 py-10 text-xs text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                Discovering types…
              </div>
            ) : availableTypes.length === 0 ? (
              <div className="flex items-center gap-2 p-3 text-xs text-muted-foreground">
                <AlertCircle className="size-4 shrink-0" />
                No resources found.
              </div>
            ) : (
              <ul className="py-1">
                {availableTypes.map((t) => {
                  const ruleCount = rules.filter((r) => r.match.trim().startsWith(`${t}.`) || r.match.trim() === t).length;
                  return (
                    <li key={t}>
                      <button
                        onClick={() => setSelectedType(t)}
                        className={cn(
                          'flex w-full items-center justify-between gap-2 px-3 py-1.5 text-left text-xs font-mono transition-colors',
                          selectedType === t
                            ? 'bg-[#0072bc]/10 text-foreground font-semibold'
                            : 'text-muted-foreground hover:bg-accent/50 hover:text-foreground',
                        )}
                      >
                        <span className="truncate">{t}</span>
                        {ruleCount > 0 && (
                          <span className="shrink-0 rounded-full bg-emerald-100 px-1.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-900/30 dark:text-emerald-400">
                            {ruleCount}
                          </span>
                        )}
                      </button>
                    </li>
                  );
                })}
              </ul>
            )}
          </div>

          {/* Right: field tree */}
          <div className="flex flex-1 flex-col min-w-0 rounded-r-md border-y border-r">
            {/* Filter bar */}
            <div className="flex items-center gap-2 border-b px-3 py-2">
              <Search className="size-3.5 text-muted-foreground shrink-0" />
              <input
                value={filter}
                onChange={(e) => setFilter(e.target.value)}
                placeholder={`Filter ${selectedType ?? ''} fields…`}
                className="flex-1 bg-transparent text-xs outline-none placeholder:text-muted-foreground"
              />
              {selectedType && (
                <span className="text-[10px] text-muted-foreground shrink-0">
                  {liveCount > 0 ? `spec + ${liveCount} live` : 'spec only'}
                </span>
              )}
            </div>

            <div className="flex-1 overflow-y-auto p-2">
              {!loadState || loadState === 'loading' ? (
                <div className="flex flex-col items-center justify-center gap-2 py-12 text-xs text-muted-foreground">
                  <Loader2 className="size-5 animate-spin" />
                  Loading {selectedType} schema…
                </div>
              ) : schema && topEntries.length > 0 ? (
                topEntries.map(([k, v]) => (
                  <TreeNode
                    key={k}
                    nodeKey={k}
                    value={v}
                    pathSegments={[selectedType!, k]}
                    depth={1}
                    selected={new Set(selected.keys())}
                    configuredPaths={configuredPaths}
                    onToggle={toggleSelect}
                    filter={filter}
                  />
                ))
              ) : (
                <div className="flex items-center gap-2 py-6 px-3 text-xs text-muted-foreground">
                  <AlertCircle className="size-4 shrink-0" />
                  Could not load schema for {selectedType}.
                </div>
              )}
            </div>
          </div>
        </div>

        {/* Footer: treatment + action + apply */}
        <DialogFooter className="mx-0! mb-0! flex-col! items-stretch! gap-3 rounded-b-xl sm:flex-col!">
          <div className="flex flex-wrap items-end gap-3">
            <div className="flex flex-col gap-1">
              <label className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                Treatment
              </label>
              <Select value={treatment} onValueChange={(v) => setTreatment(v as Treatment)}>
                <SelectTrigger className="h-8 w-64 text-xs">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(TREATMENT_LABELS) as Treatment[]).map((t) => (
                    <SelectItem key={t} value={t} className="text-xs">
                      {TREATMENT_LABELS[t]}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <div className="flex flex-col gap-1">
              <label className="text-[10px] font-semibold uppercase tracking-wide text-muted-foreground">
                Action
              </label>
              <Select value={bulkAction} onValueChange={(v) => setBulkAction(v as Action)}>
                <SelectTrigger className="h-8 w-56 text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent className="min-w-[20rem]">
                  {VALID_ACTIONS.map((a) => (
                    <SelectItem key={a} value={a} className="text-sm">
                      <span className="flex w-full items-center gap-2">
                        <span className="truncate">{actionLabel(a)}</span>
                        <span className="ml-auto shrink-0 pl-3 font-mono text-[11px] text-muted-foreground/70">
                          {a}
                        </span>
                      </span>
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
            <p className="flex-1 min-w-[180px] text-[11px] text-muted-foreground leading-snug">
              {TREATMENT_HELP[treatment]}
              {treatment === 'subfields' && (
                <span className="block text-foreground/50">
                  Per-leaf actions are auto-suggested; the Action picker applies to value / everything modes.
                </span>
              )}
            </p>
          </div>

          <div className="flex items-center justify-between gap-3 border-t pt-3">
            <span className="text-xs text-muted-foreground">
              {selected.size} field{selected.size !== 1 ? 's' : ''} selected
              {pendingRules.length > 0 && (
                <span className="ml-1 text-foreground/60">
                  → {pendingRules.length} rule{pendingRules.length !== 1 ? 's' : ''}
                </span>
              )}
            </span>
            <div className="flex gap-2">
              <Button variant="outline" size="sm" className="h-8 text-xs" onClick={() => setSelected(new Map())} disabled={selected.size === 0}>
                Clear
              </Button>
              <Button size="sm" className="h-8 text-xs" onClick={handleApply} disabled={pendingRules.length === 0}>
                Add {pendingRules.length > 0 ? pendingRules.length : ''} rule{pendingRules.length !== 1 ? 's' : ''}
              </Button>
            </div>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
