import type { RiskReport } from '@/api/types';

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const ACCEPTED_EXTENSIONS = ['.ndjson', '.json', '.xml'];

export const FORMAT_CT: Record<string, string> = {
  NDJSON: 'application/x-ndjson',
  'JSON / Bundle': 'application/json',
  XML: 'application/fhir+xml',
};

export const FORMAT_OPTIONS = Object.keys(FORMAT_CT);

export const EXAMPLE_NDJSON = [
  '{"resourceType":"Patient","id":"p1","gender":"male","birthDate":"1982-01-01","address":[{"postalCode":"10115"}]}',
  '{"resourceType":"Patient","id":"p2","gender":"female","birthDate":"1975-01-01","address":[{"postalCode":"10115"}]}',
  '{"resourceType":"Patient","id":"p3","gender":"male","birthDate":"1982-01-01","address":[{"postalCode":"10115"}]}',
  '{"resourceType":"Patient","id":"p4","gender":"female","birthDate":"1990-01-01","address":[{"postalCode":"20148"}]}',
  '{"resourceType":"Patient","id":"p5","gender":"male","birthDate":"1990-01-01","address":[{"postalCode":"20148"}]}',
  '{"resourceType":"Patient","id":"p6","gender":"female","birthDate":"1975-01-01","address":[{"postalCode":"80331"}]}',
].join('\n');

export const EXT_CT: Record<string, string> = {
  '.ndjson': 'application/x-ndjson',
  '.json': 'application/json',
  '.xml': 'application/fhir+xml',
};

export const MAX_TABLE_ROWS = 100;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

export function formatBytes(bytes: number): string {
  if (bytes === 0) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / Math.pow(1024, i);
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function pct(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

export function riskVariant(
  level: RiskReport['summary']['risk_level'],
): 'success' | 'warning' | 'destructive' {
  switch (level) {
    case 'low':
      return 'success';
    case 'medium':
      return 'warning';
    case 'high':
    case 'critical':
      return 'destructive';
  }
}

export function riskBannerClasses(level: RiskReport['summary']['risk_level']): string {
  switch (level) {
    case 'low':
      return 'border-emerald-500/50 bg-emerald-500/10 text-emerald-700 dark:text-emerald-400';
    case 'medium':
      return 'border-amber-500/50 bg-amber-500/10 text-amber-700 dark:text-amber-400';
    case 'high':
    case 'critical':
      return 'border-destructive/50 bg-destructive/10 text-destructive';
  }
}

export function riskBannerText(level: RiskReport['summary']['risk_level']): string {
  switch (level) {
    case 'low':
      return 'Low risk -- minimum group size is 5 or greater. The dataset provides strong protection against re-identification attacks.';
    case 'medium':
      return 'Medium risk -- minimum group size is between 3 and 4. Some groups may be vulnerable to targeted re-identification. Consider additional generalization.';
    case 'high':
      return 'High risk -- minimum group size is 2. Several groups contain only pairs of records, making them vulnerable to re-identification. Broader suppression or generalization is recommended.';
    case 'critical':
      return 'Critical risk -- singleton groups exist (k=1). Individuals in these groups are uniquely identifiable and must be suppressed or further generalized before release.';
  }
}

export function getRecommendations(report: RiskReport): string[] {
  const recs: string[] = [];
  const s = report.summary;
  if (s.min_k < 5)
    recs.push(
      'Apply broader date generalization (e.g., year-only birth date) to increase minimum group size.',
    );
  if (s.min_k < 3)
    recs.push(
      'Suppress or redact postal codes for records in singleton or pair groups.',
    );
  if (s.singleton_groups > 0)
    recs.push(
      `${s.singleton_groups} group(s) contain only 1 record \u2014 these individuals are uniquely identifiable and should be suppressed or further generalized.`,
    );
  if (s.records_with_missing_qi > 0)
    recs.push(
      `${s.records_with_missing_qi} record(s) have missing quasi-identifier fields \u2014 verify de-identification rules cover gender, birthDate, and address.postalCode.`,
    );
  const ld = report.l_diversity;
  if (ld.computed && (ld.violations ?? 0) > 0)
    recs.push(
      `${ld.violations} group(s) violate 2-diversity \u2014 patients in these groups share the same Condition codes and may be vulnerable to attribute inference.`,
    );
  if (recs.length === 0)
    recs.push(
      'Dataset meets all configured thresholds. No immediate action required.',
    );
  return recs;
}
