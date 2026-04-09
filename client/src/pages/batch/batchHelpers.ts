// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------

export const MAX_SIZE = 10 * 1024 * 1024; // 10 MB
export const ACCEPTED_EXTENSIONS = ['.ndjson', '.json', '.xml'];
export const MAX_VISIBLE_ERRORS = 10;

export const CT_MAP: Record<string, string> = {
  '.ndjson': 'application/x-ndjson',
  '.json': 'application/json',
  '.xml': 'application/fhir+xml',
};

// Poll interval for job status (ms)
export const JOB_POLL_INTERVAL = 3000;

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface FileInfo {
  name: string;
  size: number;
  ext: string;
  content: string;
}

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

export function sanitizeFilename(name: string): string {
  const sanitized = name.replace(/[^a-zA-Z0-9\-_.]/g, '_');
  return `deid_${sanitized}`;
}

export function detectFormat(ext: string): string {
  switch (ext) {
    case '.ndjson':
      return 'NDJSON';
    case '.json':
      return 'JSON';
    case '.xml':
      return 'XML';
    default:
      return 'Unknown';
  }
}

export function countResources(content: string, ext: string): string {
  switch (ext) {
    case '.ndjson': {
      const lines = content.split('\n').filter((l) => l.trim().length > 0);
      return `${lines.length} resource(s)`;
    }
    case '.json': {
      try {
        const parsed = JSON.parse(content);
        if (parsed.resourceType === 'Bundle' && Array.isArray(parsed.entry)) {
          return `${parsed.entry.length} resource(s) in Bundle`;
        }
        return '1 resource';
      } catch {
        return 'Invalid JSON';
      }
    }
    case '.xml':
      return formatBytes(new TextEncoder().encode(content).byteLength);
    default:
      return 'Unknown';
  }
}

export function getPreview(content: string, ext: string): string {
  switch (ext) {
    case '.ndjson': {
      const lines = content.split('\n').filter((l) => l.trim().length > 0);
      return lines
        .slice(0, 3)
        .map((line) => {
          try {
            return JSON.stringify(JSON.parse(line), null, 2);
          } catch {
            return line;
          }
        })
        .join('\n---\n');
    }
    case '.json': {
      try {
        const parsed = JSON.parse(content);
        return JSON.stringify(parsed, null, 2).slice(0, 2000);
      } catch {
        return content.slice(0, 2000);
      }
    }
    case '.xml':
      return content.slice(0, 800);
    default:
      return content.slice(0, 800);
  }
}
