import type { ConfigRule } from '@/api/medanon';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const VALID_ACTIONS = [
  'redact',
  'cryptohash',
  'generalize',
  'mask',
  'date_shift',
  'tokenize',
  'perturb',
  'substitute',
  'scrub_text',
  'nlp_scrub',
  'nlp_detect_act',
  'encrypt',
  'decrypt',
  'gpas_pseudonymize',
] as const;

export type Action = (typeof VALID_ACTIONS)[number];

// Keep in sync with backend GeneralizeParams strategy enum.
export const GENERALIZE_STRATEGIES = [
  'date_year',
  'date_year_month',
  'date_year_instant',
  'date_decade',
  'age_bracket',
  'number_round',
  'zip_prefix',
  'category',
  'redact_if_rare',
] as const;

// Keep in sync with backend MaskParams strategy enum.
export const MASK_STRATEGIES = [
  'keep_prefix',
  'keep_suffix',
  'keep_domain',
  'keep_country_code',
  'full',
] as const;

export const SCRUB_MODES = ['text', 'html_tokenize'] as const;
export const SCRUB_PATTERNS = ['all', 'phone', 'email', 'date'] as const;
export const NLP_MODES = ['tokenize', 'redact'] as const;

// Actions that always produce the same output for the same input.
// Non-deterministic actions (perturb, substitute, encrypt) break referential
// integrity across FHIR resources - the same Patient.id would hash differently
// each run, making cross-resource de-identification inconsistent.
export const DETERMINISTIC_ACTIONS = new Set<Action>([
  'redact',
  'cryptohash',
  'gpas_pseudonymize',
  'generalize',
  'mask',
  'date_shift',
  'tokenize',
  'scrub_text',
  'nlp_scrub',
  'nlp_detect_act',
]);

// Human-friendly action labels for dropdowns and summary badges. The YAML
// action key (the Record key) is unchanged - only the display string differs -
// so saved configs and the backend registry are untouched.
export const ACTION_LABELS: Record<Action, string> = {
  redact: 'Redact value',
  cryptohash: 'Hash (irreversible)',
  generalize: 'Generalize (date / zip)',
  mask: 'Mask (partial reveal)',
  date_shift: 'Shift date (keep age)',
  tokenize: 'Tokenize (format-preserving)',
  perturb: 'Perturb (random offset)',
  substitute: 'Substitute (synthetic)',
  scrub_text: 'Scrub text (regex)',
  nlp_scrub: 'NLP scrub (free-text PHI)',
  nlp_detect_act: 'NLP detect & act (per-entity)',
  encrypt: 'Encrypt (reversible)',
  decrypt: 'Decrypt',
  gpas_pseudonymize: 'Pseudonymize (gPAS)',
};

/** Friendly label for any action string, falling back to the raw key (covers
 * the neutral "modified" marker and legacy/unknown actions). */
export function actionLabel(action: string): string {
  return (ACTION_LABELS as Record<string, string>)[action] ?? action;
}

export const ACTION_DESCRIPTIONS: Record<Action, string> = {
  redact: 'Replace the matched value with a fixed placeholder (e.g. [REDACTED]).',
  cryptohash: 'One-way HMAC-SHA3-256 hash. Irreversible, but the same value always hashes the same way for linkage.',
  generalize: 'Reduce precision, e.g. date to year, ZIP to its first 3 digits.',
  mask: 'Partially obscure a value, keeping a configurable prefix, suffix, or domain.',
  date_shift: 'Shift a date by a fixed per-subject offset. Keeps the age bracket.',
  tokenize: 'Replace with a format-preserving token, optionally namespace-scoped.',
  perturb: 'Shift a number or date by a random offset within a set range.',
  substitute: 'Replace the value with a synthetic but structurally valid one.',
  scrub_text: 'Regex text scrubbing for phones, emails, dates, and similar patterns.',
  nlp_scrub: 'NLP scrubbing with Presidio. Replaces names, locations, and other entities with tokens.',
  nlp_detect_act: 'Detects PHI entities and applies a targeted action per entity type.',
  encrypt: 'RSA-encrypt the value; reversible with the private key.',
  decrypt: 'RSA-decrypt a previously encrypted value.',
  gpas_pseudonymize: 'Replace the value with a gPAS-generated pseudonym (requires gPAS server).',
};

// ---------------------------------------------------------------------------
// Local rule type (adds a stable React key)
// ---------------------------------------------------------------------------

export interface LocalRule {
  _id: string;       // local-only React key, stripped before saving
  match: string;
  action: Action;
  params: Record<string, unknown>;
  name: string;
  // UI-only: a disabled rule is kept in the editor but excluded from the saved
  // config (and from validation/duplicate warnings). Undefined == enabled.
  enabled?: boolean;
}

/** Whether a rule is active (default true - undefined counts as enabled). */
export function isRuleEnabled(r: LocalRule): boolean {
  return r.enabled !== false;
}

// crypto.randomUUID() requires HTTPS or localhost - unavailable over plain HTTP.
export function uid(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

// Default placeholder for the `substitute` action. Required by the schema, so a
// sensible default keeps the rule valid out of the box and saves typing it on
// every rule. Single source of truth - used by the UI seed, the export
// fallback, and the explorer.
export const SUBSTITUTE_DEFAULT = '[SUBSTITUTED]';

// Params auto-seeded when a rule's action is (re)selected, so common
// required/recommended params don't have to be typed each time. Extend this map
// to give other actions their own sensible defaults.
export const ACTION_PARAM_DEFAULTS: Partial<Record<Action, Record<string, unknown>>> = {
  substitute: { substitute_with: SUBSTITUTE_DEFAULT },
  // date_shift.max_days is required - seed a reasonable window so the rule is
  // valid out of the box; the user can tune it.
  date_shift: { max_days: 30, direction: 'both' },
  // mask needs a strategy + how many chars to keep; keep_prefix/4 suits most IDs.
  mask: { strategy: 'keep_prefix', keep_chars: 4 },
};

/** A fresh copy of the default params for an action (empty when none). */
export function defaultParamsForAction(action: Action): Record<string, unknown> {
  const d = ACTION_PARAM_DEFAULTS[action];
  return d ? { ...d } : {};
}

export function newRule(): LocalRule {
  return { _id: uid(), match: '', action: 'redact', params: {}, name: '' };
}

export function toApiRules(rules: LocalRule[]): ConfigRule[] {
  return rules.filter(isRuleEnabled).map(({ match, action, params, name }) => {
    // Drop empty-string/null values - they add no information and can confuse
    // the backend validator. Keep explicit false / 0 / arrays.
    const resolvedParams = Object.fromEntries(
      Object.entries(params).filter(([, v]) => v !== '' && v !== null && v !== undefined),
    );
    if (action === 'substitute' && !resolvedParams.substitute_with) {
      resolvedParams.substitute_with = SUBSTITUTE_DEFAULT;
    }
    const r: ConfigRule = { match, action };
    if (Object.keys(resolvedParams).length > 0) r.params = resolvedParams;
    if (name.trim()) r.name = name.trim();
    return r;
  });
}

// ---------------------------------------------------------------------------
// YAML → LocalRule parser
//
// Handles both indentation styles used in this project:
//   style A (config.yaml):           style B (config_gpas/structural):
//     rules:                            rules:
//       - match: "*.id"                 - name: pseudonymize ids
//         action: cryptohash              match: "*.id"
//                                         action: gpas_pseudonymize
//
// Also handles inline-list params (entities: [PERSON, DATE]), quoted strings,
// and YAML anchors (&name / *name) are stripped so parse doesn't choke.
// ---------------------------------------------------------------------------

/**
 * Normalise a raw YAML scalar value: strip a trailing inline `# comment`
 * (only when the value is unquoted), then strip matching surrounding quotes,
 * then trim. A `#` inside quotes is preserved.
 */
function unquote(raw: string): string {
  let v = raw.trim();
  // Quoted value: take the quoted span verbatim (a # inside is part of the value).
  const quoted = v.match(/^(['"])(.*?)\1\s*(?:#.*)?$/);
  if (quoted) return quoted[2].trim();
  // Unquoted: a ` #` begins an inline comment.
  const hash = v.indexOf(' #');
  if (hash !== -1) v = v.slice(0, hash);
  if (v === '#' || v.startsWith('# ')) return '';
  return v.trim();
}

/** Coerce a YAML scalar string to bool / number / unquoted string. */
function coerceScalar(raw: string): unknown {
  const v = raw.replace(/\s+#.*$/, '').trim().replace(/^["']|["']$/g, '');
  if (v === 'true' || v === 'false') return v === 'true';
  if (v !== '' && !isNaN(Number(v))) return Number(v);
  return v;
}

/**
 * Extract the `general:` block (appname, rewrite_references, domain_map, …) from
 * a config YAML so it survives a builder round-trip. The config builder only
 * parses `rules:`, so without this the `general:` block - crucially `domain_map`
 * and `rewrite_references` - is silently dropped when a profile is duplicated or
 * edited, breaking cross-resource reference rewriting.
 *
 * Handles scalar keys and ONE level of nesting (e.g. `domain_map:` → Type:domain).
 */
export function extractGeneralBlock(yaml: string): Record<string, unknown> {
  const lines = yaml.split('\n');
  const start = lines.findIndex((l) => /^general\s*:/.test(l));
  if (start === -1) return {};

  const out: Record<string, unknown> = {};
  let nested: Record<string, unknown> | null = null;
  for (let i = start + 1; i < lines.length; i++) {
    const line = lines[i];
    if (line.trim() === '' || line.trimStart().startsWith('#')) continue;
    const indent = line.length - line.trimStart().length;
    if (indent === 0) break; // reached the next top-level section (rules:)
    const m = line.match(/^(\s+)([\w.-]+)\s*:\s*(.*)$/);
    if (!m) continue;
    const [, ind, key, rawVal] = m;
    if (ind.length <= 2) {
      if (rawVal.trim() === '' || rawVal.trim().startsWith('#')) {
        nested = {}; // a nested block opener (e.g. domain_map:)
        out[key] = nested;
      } else {
        nested = null;
        out[key] = coerceScalar(rawVal);
      }
    } else if (nested) {
      nested[key] = coerceScalar(rawVal);
    }
  }
  return out;
}

export function parseYamlIntoRules(yaml: string): { rules: LocalRule[]; error: string | null } {
  const rulesStart = yaml.search(/^rules\s*:/m);
  if (rulesStart === -1) {
    return { rules: [], error: 'Could not find a "rules:" block in the YAML.' };
  }
  const afterRules = yaml.slice(rulesStart + yaml.slice(rulesStart).indexOf('\n') + 1);

  // Split on list-item openers - handles both 0-indent and 2-indent prefixes.
  const blocks = afterRules.split(/\n(?=\s*- )/).filter((b) => b.trim());

  const rules: LocalRule[] = blocks.map((block) => {
    // Strip YAML anchor definitions (&name) and alias references (*name).
    const clean = block.replace(/\s+&\w+/g, '').replace(/:\s+\*\w+/g, ': ~');

    // A key may sit directly after the list dash (`- match:`) or on its own
    // indented line (`    match:`), so allow an optional `- ` after the indent.
    // The value may be quoted, unquoted, and trailed by an inline `# comment`.
    const matchVal = unquote(
      clean.match(/^\s*(?:-\s+)?match:\s*(.+?)\s*$/m)?.[1] ?? '',
    );
    const actionVal = unquote(
      clean.match(/^\s*(?:-\s+)?action:\s*(.+?)\s*$/m)?.[1] ?? '',
    ).replace(/[&*]\w+/, '').trim() as Action;
    const nameVal = unquote(
      clean.match(/^\s*(?:-\s+)?name:\s*(.+?)\s*$/m)?.[1] ?? '',
    );

    // Parse params block - supports scalar, quoted, and inline-list values.
    const params: Record<string, unknown> = {};
    const paramsSection = clean.match(/^\s*params:\s*\n((?:[ \t]+\S[^\n]*\n?)*)/m)?.[1] ?? '';
    for (const line of paramsSection.split('\n')) {
      const listKv = line.match(/^\s+(\w+):\s*\[([^\]]*)\]/);
      if (listKv) {
        params[listKv[1]] = listKv[2].split(',').map((s) => unquote(s)).filter(Boolean);
        continue;
      }
      const kv = line.match(/^\s+(\w+):\s*(.+?)\s*$/);
      if (kv) {
        const raw = unquote(kv[2]);
        if (raw === '') continue; // bare `key:` (nested block) - skip, not a scalar
        // Coerce YAML scalars to their JS types: booleans (so base64_encoded /
        // preserve_length round-trip as real booleans, not the string "true"),
        // then numbers, else keep the string.
        if (raw === 'true' || raw === 'false') params[kv[1]] = raw === 'true';
        else params[kv[1]] = isNaN(Number(raw)) ? raw : Number(raw);
      }
    }

    return { _id: uid(), match: matchVal, action: actionVal || 'redact', params, name: nameVal };
  }).filter((r) => r.match && VALID_ACTIONS.includes(r.action));

  if (rules.length === 0) {
    return { rules: [], error: 'No valid rules found. Make sure each rule has a "match" and "action" field.' };
  }
  return { rules, error: null };
}

// ---------------------------------------------------------------------------
// Params validation - mirrors backend pipeline/config/rule_schema.py
// ---------------------------------------------------------------------------

interface ParamSpec {
  known: Set<string>;
  required?: string[];
  enums?: Record<string, readonly string[]>;
  positiveInts?: string[];
  zeroToOne?: string[];
}

const PARAMS_SPEC: Partial<Record<string, ParamSpec>> = {
  redact: { known: new Set(['replacement']) },
  generalize: {
    known: new Set(['strategy', 'bracket_size', 'precision', 'prefix_length', 'mapping', 'unmapped']),
    enums: { strategy: GENERALIZE_STRATEGIES },
    positiveInts: ['bracket_size', 'precision', 'prefix_length'],
  },
  mask: {
    known: new Set(['strategy', 'mask_char', 'keep_chars', 'preserve_length']),
    enums: { strategy: MASK_STRATEGIES },
  },
  perturb: { known: new Set(['min', 'max', 'min_offset', 'max_offset']) },
  date_shift: {
    known: new Set(['max_days', 'direction', 'preserve_age_bracket', 'anchor_path']),
    required: ['max_days'],
    enums: { direction: ['past', 'future', 'both'] as const },
    positiveInts: ['max_days'],
  },
  tokenize: { known: new Set(['format', 'namespace', 'preserve_length']) },
  substitute: { known: new Set(['substitute_with']), required: ['substitute_with'] },
  cryptohash: { known: new Set(['hash_type', 'secret_key', 'secret_key_env']) },
  encrypt: { known: new Set(['algorithm', 'key_path']) },
  decrypt: { known: new Set(['algorithm', 'key_path']) },
  nlp_scrub: {
    known: new Set([
      'entities',
      'threshold',
      'language',
      'entity_actions',
      'default_action',
      'mode',
      'html',
      'base64_encoded',
      'entity_priorities',
      'fail_mode',
      'mapping_scope',
    ]),
    zeroToOne: ['threshold'],
  },
  nlp_detect_act: {
    known: new Set([
      'entities',
      'threshold',
      'language',
      'entity_actions',
      'default_action',
      'mode',
      'html',
      'base64_encoded',
      'entity_priorities',
      'fail_mode',
      'mapping_scope',
    ]),
    zeroToOne: ['threshold'],
  },
  scrub_text: {
    known: new Set(['mode', 'patterns']),
    enums: { mode: SCRUB_MODES, patterns: SCRUB_PATTERNS },
  },
  gpas_pseudonymize: { known: new Set(['gpas_domain', 'gpas_operation']) },
  gpas_depseudonymize: { known: new Set(['gpas_domain', 'gpas_operation']) },
};

export interface ParamError {
  key: string;
  message: string;
}

export function validateParams(action: string, params: Record<string, unknown>): ParamError[] {
  const spec = PARAMS_SPEC[action];
  if (!spec) return [];
  const errors: ParamError[] = [];

  for (const key of Object.keys(params)) {
    if (!spec.known.has(key)) {
      errors.push({ key, message: `unknown param "${key}" for action "${action}"` });
    }
  }
  for (const req of spec.required ?? []) {
    const v = params[req];
    if (v === undefined || v === '' || v === null) {
      errors.push({ key: req, message: `"${req}" is required for action "${action}"` });
    }
  }
  for (const [enumKey, allowed] of Object.entries(spec.enums ?? {})) {
    const v = params[enumKey];
    if (v !== undefined && !(allowed as readonly unknown[]).includes(v)) {
      errors.push({ key: enumKey, message: `"${enumKey}" must be one of: ${[...allowed].join(', ')}` });
    }
  }
  for (const k of spec.positiveInts ?? []) {
    const v = params[k];
    if (v !== undefined && (typeof v !== 'number' || v <= 0)) {
      errors.push({ key: k, message: `"${k}" must be a positive number` });
    }
  }
  for (const k of spec.zeroToOne ?? []) {
    const v = params[k];
    if (v !== undefined && (typeof v !== 'number' || v < 0 || v > 1)) {
      errors.push({ key: k, message: `"${k}" must be between 0 and 1` });
    }
  }
  return errors;
}

// ---------------------------------------------------------------------------
// Deduplication helper - used by all rule-import paths (AI approve, explorer
// Add, YAML import) to prevent the same match expression from being added twice.
// ---------------------------------------------------------------------------

export interface DeduplicateResult {
  added: LocalRule[];
  skipped: LocalRule[];
}

export function deduplicateIncoming(
  incoming: LocalRule[],
  existing: LocalRule[],
): DeduplicateResult {
  const existingMatches = new Set(existing.map((r) => r.match.trim()).filter(Boolean));
  const added: LocalRule[] = [];
  const skipped: LocalRule[] = [];
  const seenInBatch = new Set<string>();
  for (const rule of incoming) {
    const m = rule.match.trim();
    if (!m || existingMatches.has(m) || seenInBatch.has(m)) {
      skipped.push(rule);
    } else {
      added.push(rule);
      seenInBatch.add(m);
    }
  }
  return { added, skipped };
}

// ---------------------------------------------------------------------------
// Resource-type derivation (for grouping / filtering the rules list)
// ---------------------------------------------------------------------------

/** Sentinel bucket labels for matches that don't name a concrete resource type. */
export const RESOURCE_TYPE_WILDCARD = '*';
export const RESOURCE_TYPE_UNSET = '(unset)';

/** Derive the FHIR resource type a rule's `match` targets.
 *
 * The resource type is the leading FHIRPath segment before the first dot:
 *   "Patient.name.family" → "Patient"
 *   "Observation"         → "Observation"   (whole-resource match)
 *   "*.id" / "*"          → "*"             (wildcard - applies to all types)
 *   ""                    → "(unset)"       (incomplete rule)
 */
export function resourceTypeOf(match: string): string {
  const m = match.trim();
  if (!m) return RESOURCE_TYPE_UNSET;
  if (m === '*' || m.startsWith('*.') || m.startsWith('*')) {
    return RESOURCE_TYPE_WILDCARD;
  }
  const dot = m.indexOf('.');
  const head = (dot === -1 ? m : m.slice(0, dot)).trim();
  return head || RESOURCE_TYPE_UNSET;
}

/** Distinct resource types present in a rule set, sorted with concrete types
 * first (alphabetical), then the wildcard and unset buckets last. */
export function resourceTypesIn(rules: LocalRule[]): string[] {
  const set = new Set(rules.map((r) => resourceTypeOf(r.match)));
  const special = [RESOURCE_TYPE_WILDCARD, RESOURCE_TYPE_UNSET];
  const concrete = [...set]
    .filter((t) => !special.includes(t))
    .sort((a, b) => a.localeCompare(b));
  return [...concrete, ...special.filter((t) => set.has(t))];
}

// ---------------------------------------------------------------------------
// Values-only granularity enforcement
//
// Small local models (e.g. Gemma 3 4B) don't reliably obey the "emit leaves,
// not the parent" instruction - they propose BOTH a parent rule (Patient.name)
// AND its leaf rules (Patient.name.family). In values-only mode the parent is
// redundant and defeats the point (it removes the whole element, including the
// structure we wanted to keep). This deterministic filter drops any rule whose
// `match` is a strict ANCESTOR of another proposed rule's `match`, so only the
// leaves survive. A parent with no proposed leaves is kept untouched - nothing
// is left untreated.
// ---------------------------------------------------------------------------

/** True when `ancestor` is a strict dot-path prefix of `descendant`
 * (Patient.name is an ancestor of Patient.name.family, but not of
 * Patient.namespace - the boundary must fall on a `.`). */
export function isAncestorPath(ancestor: string, descendant: string): boolean {
  if (!ancestor || !descendant || ancestor === descendant) return false;
  return descendant.startsWith(ancestor + '.');
}

export function dropParentRulesWithLeaves(rules: LocalRule[]): {
  kept: LocalRule[];
  dropped: LocalRule[];
} {
  const matches = rules.map((r) => r.match.trim()).filter(Boolean);
  const kept: LocalRule[] = [];
  const dropped: LocalRule[] = [];
  for (const rule of rules) {
    const m = rule.match.trim();
    // Drop this rule if some OTHER proposed rule targets a descendant of it.
    const hasLeaf = m && matches.some((other) => isAncestorPath(m, other));
    if (hasLeaf) dropped.push(rule);
    else kept.push(rule);
  }
  return { kept, dropped };
}

// ---------------------------------------------------------------------------
// Conflict / duplicate resolution
// ---------------------------------------------------------------------------

export type ConflictKind =
  | "exact"      // same match + same action → pure duplicate, safe to drop
  | "conflict";  // same match, different action → ambiguous, keep first (lower index)

export interface CleanupResult {
  rules: LocalRule[];
  removedExact: number;    // identical duplicates dropped
  removedConflict: number; // conflicting rules (same match, diff action) dropped
  kept: LocalRule[];       // full list of survivors
}

/**
 * Deduplicate and resolve conflicts in-place.
 *
 * Strategy: first occurrence of each `match` expression wins.
 * - Same match + same action → exact duplicate → drop the later one.
 * - Same match + different action → conflict → drop the later one, mark as conflict.
 */
export function cleanupRules(rules: LocalRule[]): CleanupResult {
  const seen = new Map<string, { action: string; index: number }>();
  const kept: LocalRule[] = [];
  let removedExact = 0;
  let removedConflict = 0;

  for (const rule of rules) {
    const m = rule.match.trim();
    if (!m) {
      kept.push(rule);
      continue;
    }
    const prior = seen.get(m);
    if (!prior) {
      seen.set(m, { action: rule.action, index: kept.length });
      kept.push(rule);
    } else if (prior.action === rule.action) {
      removedExact++;
    } else {
      removedConflict++;
    }
  }

  return { rules: kept, removedExact, removedConflict, kept };
}

// ---------------------------------------------------------------------------
// YAML serialiser (client-side preview - mirrors backend format)
// ---------------------------------------------------------------------------

export function buildYamlPreview(
  name: string,
  description: string,
  rules: LocalRule[],
  opts?: { rewriteReferences?: boolean; general?: Record<string, unknown> },
): string {
  if (!name && rules.length === 0) return '';

  const lines: string[] = [
    `# Config: ${name || '(unnamed)'}`,
    `# ${description}`,
    '',
    'general:',
    '  appname: SPE-FHIR-BlackBox',
  ];
  // Referential integrity: rewrite cross-resource references (and bare IDs in
  // free text) so a patient's resources stay linked after IDs are pseudonymized.
  if (opts?.rewriteReferences) {
    lines.push('  rewrite_references: true');
    lines.push('  rewrite_text_ids: true');
  }
  // Preserve any other general settings (e.g. domain_map) carried over from the
  // source profile so a builder round-trip doesn't drop them.
  for (const [k, v] of Object.entries(opts?.general ?? {})) {
    if (['appname', 'rewrite_references', 'rewrite_text_ids'].includes(k)) continue;
    if (v && typeof v === 'object' && !Array.isArray(v)) {
      lines.push(`  ${k}:`);
      for (const [nk, nv] of Object.entries(v as Record<string, unknown>)) {
        lines.push(`    ${nk}: ${nv}`);
      }
    } else {
      lines.push(`  ${k}: ${v}`);
    }
  }
  lines.push('', 'rules:');

  for (const r of rules) {
    if (!r.match.trim() || !isRuleEnabled(r)) continue;
    // Always emit `- match:` (or `- name:`) as the first key so the block is
    // unambiguous YAML - never a bare `  -`.
    if (r.name.trim()) {
      lines.push(`  - name: "${r.name.trim()}"`);
      lines.push(`    match: "${r.match}"`);
    } else {
      lines.push(`  - match: "${r.match}"`);
    }
    lines.push(`    action: ${r.action}`);
    // substitute requires substitute_with - fall back to the default so an
    // imported/AI rule that omitted it still produces a valid config.
    const effectiveParams =
      r.action === 'substitute' && !r.params.substitute_with
        ? { ...r.params, substitute_with: SUBSTITUTE_DEFAULT }
        : r.params;
    const filteredParams = Object.entries(effectiveParams).filter(
      ([, v]) => v !== '' && v !== null && v !== undefined,
    );
    if (filteredParams.length > 0) {
      lines.push('    params:');
      for (const [k, v] of filteredParams) {
        if (Array.isArray(v)) {
          lines.push(`      ${k}: [${(v as unknown[]).join(', ')}]`);
        } else {
          lines.push(`      ${k}: ${v}`);
        }
      }
    }
  }

  return lines.join('\n');
}
