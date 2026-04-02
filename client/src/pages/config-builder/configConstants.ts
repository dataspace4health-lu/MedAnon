import type { ConfigRule } from '@/api/medanon';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const VALID_ACTIONS = [
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

export type Action = (typeof VALID_ACTIONS)[number];

export const GENERALIZE_STRATEGIES = [
  'date_year',
  'date_year_month',
  'zip_3digit',
  'age_group',
] as const;

export const SCRUB_MODES = ['text', 'html_tokenize'] as const;
export const SCRUB_PATTERNS = ['all', 'phone', 'email', 'date'] as const;
export const NLP_MODES = ['tokenize', 'redact'] as const;

export const ACTION_DESCRIPTIONS: Record<Action, string> = {
  redact: 'Replace the matched value with a fixed placeholder (e.g. [REDACTED]).',
  cryptohash: 'One-way HMAC-SHA3-256 hash \u2014 irreversible but deterministic for linkage.',
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

export interface LocalRule {
  _id: string;       // local-only React key, stripped before saving
  match: string;
  action: Action;
  params: Record<string, unknown>;
  name: string;
}

// crypto.randomUUID() requires HTTPS or localhost -- unavailable over plain HTTP.
export function uid(): string {
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16);
  });
}

export function newRule(): LocalRule {
  return {
    _id: uid(),
    match: '',
    action: 'redact',
    params: {},
    name: '',
  };
}

export function toApiRules(rules: LocalRule[]): ConfigRule[] {
  return rules.map(({ match, action, params, name }) => {
    const r: ConfigRule = { match, action };
    if (Object.keys(params).length > 0) r.params = params;
    if (name.trim()) r.name = name.trim();
    return r;
  });
}

// ---------------------------------------------------------------------------
// Shared YAML -> LocalRule parser
//
// Handles both indentation styles used in this project:
//   style A (config.yaml):           style B (config_gpas/structural):
//     rules:                            rules:
//       - match: "*.id"                 - name: pseudonymize ids
//         action: cryptohash              match: "*.id"
//                                         action: gpas_pseudonymize
//
// YAML anchors (&gpas, *gpas) are stripped -- params that reference anchors
// cannot be represented in the simplified params UI and are left empty.
// ---------------------------------------------------------------------------
export function parseYamlIntoRules(yaml: string): { rules: LocalRule[]; error: string | null } {
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

// ---------------------------------------------------------------------------
// YAML serialiser (client-side preview -- mirrors backend format)
// ---------------------------------------------------------------------------

export function buildYamlPreview(
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
