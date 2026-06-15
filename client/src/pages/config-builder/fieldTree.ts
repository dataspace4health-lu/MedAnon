// ---------------------------------------------------------------------------
// Field-path extraction from FHIR resources (uploaded examples or server samples).
//
// PHI BOUNDARY: extractFieldPaths returns ONLY structural information —
// FHIRPath-style paths and their JSON value *type* (<string>, <number>,
// <boolean>, <object>, <array>). No patient values ever leave this module, so
// the result is safe to send to the LLM as grounding context (mirrors the
// backend source-context PHI rule: paths/counts only, never resource bodies).
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

// Walk one resource, collecting `Type.path.to.field : <jsontype>` into `acc`.
// Array indices are collapsed (Patient.name.family, not name[0].family) to
// match FHIRPath semantics. VALUES ARE NEVER RECORDED — only the type label.
function walk(
  node: unknown,
  pathSegments: string[],
  depth: number,
  acc: Map<string, string>,
): void {
  if (depth > MAX_DEPTH) return;

  if (Array.isArray(node)) {
    if (node.length > 0) walk(node[0], pathSegments, depth, acc);
    else acc.set(pathSegments.join('.'), '<array>');
    return;
  }

  if (typeof node === 'object' && node !== null) {
    for (const [k, v] of Object.entries(node as Record<string, unknown>)) {
      if (depth === 1 && SKIP_KEYS.has(k)) continue;
      const next = [...pathSegments, k];
      const path = next.join('.');
      if (v !== null && typeof v === 'object') {
        if (!acc.has(path)) acc.set(path, typeLabel(v));
        walk(v, next, depth + 1, acc);
      } else {
        acc.set(path, typeLabel(v));
      }
    }
  }
}

export interface FieldContextResult {
  summary: string;        // prompt-ready `path : <type>` lines
  pathCount: number;
  resourceTypes: string[];
}

/** Extract a PHI-free field-path summary from parsed resources.
 *
 * The returned `summary` contains ONLY paths and JSON value types — safe to
 * send to the AI. Paths are deduped across every resource so multiple examples
 * of the same type collapse into one union tree. */
export function extractFieldPaths(
  resources: Record<string, unknown>[],
): FieldContextResult {
  const acc = new Map<string, string>();
  const types = new Set<string>();

  for (const res of resources) {
    const rtype = String(res.resourceType ?? 'Resource');
    types.add(rtype);
    walk(res, [rtype], 1, acc);
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
    .map(([path, type]) =>
      containers.has(path)
        ? `${path} : ${type} (container)`
        : `${path} : ${type}`,
    );

  return {
    summary: lines.join('\n'),
    pathCount: acc.size,
    resourceTypes: [...types].sort(),
  };
}
