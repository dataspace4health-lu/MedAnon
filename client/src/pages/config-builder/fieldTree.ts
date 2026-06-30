// ---------------------------------------------------------------------------
// Field-path extraction from FHIR resources (uploaded examples or server samples).
//
// PHI BOUNDARY (default): extractFieldPaths returns ONLY structural information
// — FHIRPath-style paths and their JSON value *type* (<string>, <number>,
// <boolean>, <object>, <array>). In this default mode no patient values leave
// the module, so the result is safe to send to ANY LLM as grounding context
// (mirrors the backend source-context PHI rule: paths/counts only).
//
// OPT-IN VALUES MODE (`includeValues: true`): a truncated SAMPLE value is
// appended to each leaf (`path : <type> = value`) so a *local* model can judge
// PII more accurately (a "code" field holding a free-text name, a numeric field
// that is actually an MRN, etc.). Because the summary then carries PHI, the
// backend treats it as a PHI payload and the AI local-guard HARD-REFUSES any
// non-local endpoint — values only ever reach a self-hosted model. Callers must
// flag the request `include_values: true` so that enforcement engages.
// ---------------------------------------------------------------------------

/** Parse uploaded file text (JSON object, JSON array, Bundle, or NDJSON) into
 * a flat list of FHIR resources. Best-effort: malformed lines are skipped. */
export function parseResources(text: string): Record<string, unknown>[] {
  const trimmed = text.trim();
  if (!trimmed) return [];

  try {
    const doc = JSON.parse(trimmed);
    return collectFromJson(doc);
  } catch {
    /* fall through to NDJSON */
  }

  const out: Record<string, unknown>[] = [];
  for (const line of trimmed.split('\n')) {
    const l = line.trim();
    if (!l) continue;
    try {
      out.push(...collectFromJson(JSON.parse(l)));
    } catch {
      /* skip malformed NDJSON line */
    }
  }
  return out;
}

function collectFromJson(doc: unknown): Record<string, unknown>[] {
  if (Array.isArray(doc)) {
    return doc.filter(isResource);
  }
  if (isResource(doc)) {
    if (doc.resourceType === 'Bundle' && Array.isArray(doc.entry)) {
      return doc.entry
        .map((e) => (e as { resource?: unknown })?.resource)
        .filter(isResource);
    }
    return [doc];
  }
  return [];
}

function isResource(x: unknown): x is Record<string, unknown> {
  return (
    typeof x === 'object' &&
    x !== null &&
    !Array.isArray(x) &&
    typeof (x as Record<string, unknown>).resourceType === 'string'
  );
}

const MAX_DEPTH = 8;
// FHIR bookkeeping keys not worth proposing rules against. NOTE: `text`
// (Narrative.text.div) is intentionally NOT skipped — it is human-readable
// rendered content that carries PHI, so the AI/Explorer must see the leaf to
// recommend scrubbing it. `meta` stays skipped (provenance/versioning only).
const SKIP_KEYS = new Set(['resourceType', 'meta']);

function typeLabel(v: unknown): string {
  if (v === null || v === undefined) return '<null>';
  if (Array.isArray(v)) return '<array>';
  switch (typeof v) {
    case 'string': return '<string>';
    case 'number': return '<number>';
    case 'boolean': return '<boolean>';
    case 'object': return '<object>';
    default: return '<value>';
  }
}

// Max length of a single captured sample value (values mode only). Keeps the
// prompt small and prevents a giant narrative <div> from blowing the budget.
const MAX_VALUE_LEN = 80;

/** A compact, single-line sample of a leaf value for the values-mode summary. */
function sampleValue(v: unknown): string {
  const s = String(v).replace(/\s+/g, ' ').trim();
  return s.length > MAX_VALUE_LEN ? `${s.slice(0, MAX_VALUE_LEN)}…` : s;
}

// Walk one resource, collecting `Type.path.to.field : <jsontype>` into `acc`.
// Array indices are collapsed (Patient.name.family, not name[0].family) to
// match FHIRPath semantics. When `values` is provided (opt-in values mode), a
// truncated SAMPLE of each leaf value is recorded into it — first value seen
// per path wins; otherwise only the type label is recorded.
function walk(
  node: unknown,
  pathSegments: string[],
  depth: number,
  acc: Map<string, string>,
  values?: Map<string, string>,
): void {
  if (depth > MAX_DEPTH) return;

  if (Array.isArray(node)) {
    if (node.length === 0) {
      acc.set(pathSegments.join('.'), '<array>');
      return;
    }
    // Walk EVERY element, not just node[0], so heterogeneous siblings all
    // contribute their leaf paths to the AI's field context — FHIR extension
    // arrays are keyed by `url` (mothersMaidenName, birthsex, birthPlace,
    // race vs ethnicity each carry a different value[x]), and identifier/
    // telecom arrays differ element-to-element. Indices stay collapsed
    // (FHIRPath semantics); the Map dedups, first value seen per path wins.
    for (const item of node) walk(item, pathSegments, depth, acc, values);
    return;
  }

  if (typeof node === 'object' && node !== null) {
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
      if (depth === 1 && SKIP_KEYS.has(k)) continue;
      const next = [...pathSegments, k];
      const path = next.join('.');
      if (v !== null && typeof v === 'object') {
        if (!acc.has(path)) acc.set(path, typeLabel(v));
        walk(v, next, depth + 1, acc, values);
      } else {
        acc.set(path, typeLabel(v));
        if (values && v !== null && v !== undefined && !values.has(path)) {
          values.set(path, sampleValue(v));
        }
      }
    }
  }
}

export interface FieldContextResult {
  summary: string;        // prompt-ready `path : <type>` lines
  pathCount: number;
  resourceTypes: string[];
}

/** Extract a field-path summary from parsed resources.
 *
 * By default the `summary` contains ONLY paths and JSON value types — safe to
 * send to ANY model. With `{ includeValues: true }` a truncated sample value is
 * appended per leaf (`path : <type> = value`); the result then carries PHI and
 * must only be sent to a local model (the backend enforces this via the AI
 * local-guard when the request is flagged `include_values`). Paths are deduped
 * across every resource so multiple examples of the same type collapse into one
 * union tree. */
export function extractFieldPaths(
  resources: Record<string, unknown>[],
  opts?: { includeValues?: boolean },
): FieldContextResult {
  const includeValues = opts?.includeValues ?? false;
  const acc = new Map<string, string>();
  const values = includeValues ? new Map<string, string>() : undefined;
  const types = new Set<string>();

  for (const res of resources) {
    const rtype = String(res.resourceType ?? 'Resource');
    types.add(rtype);
    walk(res, [rtype], 1, acc, values);
  }

  // A path is a CONTAINER if some other recorded path is its strict child
  // (Patient.name is a container because Patient.name.family exists). Marking
  // these lets the AI distinguish a structural parent from a leaf value, so in
  // values-only mode it can target leaves and skip the container.
  const paths = [...acc.keys()];
  const containers = new Set<string>();
  for (const p of paths) {
    for (const other of paths) {
      if (other.length > p.length && other.startsWith(p + '.')) {
        containers.add(p);
        break;
      }
    }
  }

  const lines = [...acc.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([path, type]) => {
      if (containers.has(path)) return `${path} : ${type} (container)`;
      const sample = values?.get(path);
      // Only leaves carry a sample; an empty string is still informative.
      return sample !== undefined
        ? `${path} : ${type} = ${sample}`
        : `${path} : ${type}`;
    });

  return {
    summary: lines.join('\n'),
    pathCount: acc.size,
    resourceTypes: [...types].sort(),
  };
}
