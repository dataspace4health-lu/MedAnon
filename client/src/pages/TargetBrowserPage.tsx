import { useState, useEffect, useCallback } from 'react';
import { Loader2, AlertCircle, ShieldCheck, ChevronDown, ChevronRight, RefreshCw } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/button';
import {
  capabilityStatementTarget,
  fetchResourceTypeCountsTarget,
  fetchResourcesTarget,
  type ResourceTypeCount,
  type ResourcePage,
} from '@/api/fhirTarget';

// ---------------------------------------------------------------------------
// Resource type color legend (matches ResourceTypeSummary)
// ---------------------------------------------------------------------------

const TYPE_COLORS: Record<string, string> = {
  Patient:            'bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-200',
  Observation:        'bg-emerald-100 text-emerald-800 border-emerald-200 hover:bg-emerald-200',
  Condition:          'bg-amber-100 text-amber-800 border-amber-200 hover:bg-amber-200',
  MedicationRequest:  'bg-teal-100 text-teal-800 border-teal-200 hover:bg-teal-200',
  Encounter:          'bg-orange-100 text-orange-800 border-orange-200 hover:bg-orange-200',
  Procedure:          'bg-sky-100 text-sky-800 border-sky-200 hover:bg-sky-200',
  DiagnosticReport:   'bg-cyan-100 text-cyan-800 border-cyan-200 hover:bg-cyan-200',
  AllergyIntolerance: 'bg-red-100 text-red-800 border-red-200 hover:bg-red-200',
  Immunization:       'bg-emerald-100 text-emerald-800 border-emerald-200 hover:bg-emerald-200',
};

const FALLBACK_COLORS = [
  'bg-slate-100 text-slate-800 border-slate-200 hover:bg-slate-200',
  'bg-blue-100 text-blue-800 border-blue-200 hover:bg-blue-200',
  'bg-sky-100 text-sky-800 border-sky-200 hover:bg-sky-200',
  'bg-cyan-100 text-cyan-800 border-cyan-200 hover:bg-cyan-200',
  'bg-teal-100 text-teal-800 border-teal-200 hover:bg-teal-200',
];

function typeColor(type: string, index: number): string {
  return TYPE_COLORS[type] ?? FALLBACK_COLORS[index % FALLBACK_COLORS.length];
}

function typeColorSelected(type: string, index: number): string {
  const base = typeColor(type, index);
  // Replace hover class with a ring to mark selection
  return base.replace(/hover:\S+/, '') + ' ring-2 ring-offset-1 ring-current';
}

// ---------------------------------------------------------------------------
// JSON preview helpers
// ---------------------------------------------------------------------------

function JsonPreview({ resource }: { resource: Record<string, unknown> }) {
  return (
    <pre className="overflow-auto rounded-lg border bg-muted/40 p-4 text-xs font-mono text-foreground leading-relaxed max-h-[600px]">
      {JSON.stringify(resource, null, 2)}
    </pre>
  );
}

// ---------------------------------------------------------------------------
// Resource row with expandable JSON
// ---------------------------------------------------------------------------

function ResourceRow({
  resource,
  isExpanded,
  onToggle,
}: {
  resource: Record<string, unknown>;
  isExpanded: boolean;
  onToggle: () => void;
}) {
  const id = String(resource.id ?? '–');
  const resourceType = String(resource.resourceType ?? '');
  const lastUpdated = (resource.meta as Record<string, unknown> | undefined)
    ?.lastUpdated;
  const lastUpdatedStr = lastUpdated
    ? new Date(String(lastUpdated)).toLocaleString()
    : '–';

  return (
    <>
      <tr
        className="cursor-pointer border-b transition-colors hover:bg-muted/30"
        onClick={onToggle}
      >
        <td className="px-3 py-2.5">
          {isExpanded ? (
            <ChevronDown className="size-3.5 text-muted-foreground" />
          ) : (
            <ChevronRight className="size-3.5 text-muted-foreground" />
          )}
        </td>
        <td className="px-3 py-2.5 font-mono text-xs text-foreground">{id}</td>
        <td className="px-3 py-2.5 text-sm text-muted-foreground">{resourceType}</td>
        <td className="px-3 py-2.5 text-xs text-muted-foreground tabular-nums">
          {lastUpdatedStr}
        </td>
      </tr>
      {isExpanded && (
        <tr className="border-b bg-muted/10">
          <td colSpan={4} className="px-4 py-3">
            <JsonPreview resource={resource} />
          </td>
        </tr>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
// Main page
// ---------------------------------------------------------------------------

export default function TargetBrowserPage() {
  const [connected, setConnected] = useState<boolean | null>(null);
  const [loadingCounts, setLoadingCounts] = useState(false);
  const [typeCounts, setTypeCounts] = useState<ResourceTypeCount[]>([]);

  const [selectedType, setSelectedType] = useState<string | null>(null);
  const [page, setPage] = useState<ResourcePage | null>(null);
  const [loadingPage, setLoadingPage] = useState(false);
  const [offset, setOffset] = useState(0);

  const [expandedIds, setExpandedIds] = useState<Set<string>>(new Set());

  // ---------------------------------------------------------------------------
  // Load resource counts on mount / on refresh
  // ---------------------------------------------------------------------------

  const loadCounts = useCallback(async () => {
    setConnected(null);
    setLoadingCounts(true);
    setTypeCounts([]);
    setSelectedType(null);
    setPage(null);
    try {
      await capabilityStatementTarget();
      setConnected(true);
      const counts = await fetchResourceTypeCountsTarget();
      setTypeCounts(counts);
    } catch {
      setConnected(false);
    } finally {
      setLoadingCounts(false);
    }
  }, []);

  useEffect(() => {
    loadCounts();
  }, [loadCounts]);

  // ---------------------------------------------------------------------------
  // Load resource list when a type is selected
  // ---------------------------------------------------------------------------

  const loadPage = useCallback(async (type: string, newOffset: number) => {
    setLoadingPage(true);
    setExpandedIds(new Set());
    try {
      const result = await fetchResourcesTarget(type, 20, newOffset);
      setPage(result);
      setOffset(newOffset);
    } catch {
      setPage(null);
    } finally {
      setLoadingPage(false);
    }
  }, []);

  const handleSelectType = (type: string) => {
    setSelectedType(type);
    loadPage(type, 0);
  };

  const handlePrev = () => {
    if (selectedType && offset >= 20) loadPage(selectedType, offset - 20);
  };

  const handleNext = () => {
    if (selectedType && page?.hasMore) loadPage(selectedType, offset + 20);
  };

  const toggleExpand = (id: string) => {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) { next.delete(id); } else { next.add(id); }
      return next;
    });
  };

  const totalInServer = typeCounts.reduce((s, t) => s + t.count, 0);

  return (
    <div>
      <PageHeader
        title="Target FHIR Browser"
        description="Browse de-identified resources stored on the target HAPI FHIR server (HAPI 2) to verify that de-identification ran correctly."
      />

      {/* ── Connectivity banners ── */}
      {connected === null && (
        <div className="mb-6 flex items-center gap-2.5 rounded-lg border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
          Checking target FHIR server connectivity…
        </div>
      )}
      {connected === false && (
        <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            <span className="font-medium">Target FHIR server unreachable.</span>{' '}
            Verify that the target HAPI FHIR service is running and accessible at{' '}
            <span className="font-mono">/fhir-target</span>.
          </span>
        </div>
      )}
      {connected === true && (
        <div className="mb-6 flex items-center gap-2.5 rounded-lg border border-emerald-200 bg-emerald-50 px-4 py-3 text-sm text-emerald-800">
          <ShieldCheck className="h-4 w-4 shrink-0" />
          <span>
            Connected to target FHIR server ·{' '}
            <span className="font-semibold">{totalInServer.toLocaleString()}</span>{' '}
            de-identified resource{totalInServer !== 1 ? 's' : ''} stored
          </span>
          <button
            type="button"
            onClick={loadCounts}
            className="ml-auto flex items-center gap-1 text-xs text-emerald-700 hover:text-emerald-900"
          >
            <RefreshCw className="size-3" />
            Refresh
          </button>
        </div>
      )}

      {/* ── Resource type selector ── */}
      {connected === true && (
        <>
          {loadingCounts ? (
            <div className="mb-6 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="h-4 w-4 animate-spin" />
              Counting resource types…
            </div>
          ) : typeCounts.length === 0 ? (
            <div className="mb-6 rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
              No de-identified resources found on target server yet.
              <br />
              Run a bulk export or round-trip job to populate it.
            </div>
          ) : (
            <div className="mb-6">
              <p className="mb-2 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
                Resource types — click to browse
              </p>
              <div className="flex flex-wrap gap-2">
                {typeCounts.map((tc, idx) => (
                  <button
                    key={tc.type}
                    type="button"
                    onClick={() => handleSelectType(tc.type)}
                    className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-sm font-medium transition-all ${
                      selectedType === tc.type
                        ? typeColorSelected(tc.type, idx)
                        : typeColor(tc.type, idx)
                    }`}
                  >
                    {tc.type}
                    <span className="rounded-full bg-white/60 px-1.5 py-0.5 text-xs tabular-nums">
                      {tc.count.toLocaleString()}
                    </span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </>
      )}

      {/* ── Resource list for selected type ── */}
      {selectedType && (
        <div>
          <div className="mb-3 flex items-center justify-between">
            <p className="text-sm font-semibold text-foreground">
              {selectedType}
              {page?.total != null && (
                <span className="ml-2 font-normal text-muted-foreground">
                  · {page.total.toLocaleString()} total
                </span>
              )}
            </p>
            <div className="flex items-center gap-2 text-xs text-muted-foreground">
              <span>Showing {offset + 1}–{offset + (page?.items.length ?? 0)}</span>
              <Button
                variant="outline"
                size="sm"
                onClick={handlePrev}
                disabled={offset === 0 || loadingPage}
              >
                Previous
              </Button>
              <Button
                variant="outline"
                size="sm"
                onClick={handleNext}
                disabled={!page?.hasMore || loadingPage}
              >
                Next
              </Button>
            </div>
          </div>

          {loadingPage ? (
            <div className="flex items-center justify-center py-12 text-muted-foreground">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" />
              <span className="text-sm">Loading {selectedType} resources…</span>
            </div>
          ) : page && page.items.length === 0 ? (
            <div className="rounded-lg border border-dashed py-10 text-center text-sm text-muted-foreground">
              No resources found.
            </div>
          ) : page ? (
            <div className="overflow-hidden rounded-lg border">
              <table className="w-full">
                <thead className="bg-muted/50">
                  <tr>
                    <th className="w-8 px-3 py-2" />
                    <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                      ID
                    </th>
                    <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                      Resource Type
                    </th>
                    <th className="px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                      Last Updated
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {page.items.map((resource, idx) => {
                    const id = String(resource.id ?? `row-${idx}`);
                    const rowKey = `${selectedType}-${id}-${idx}`;
                    return (
                      <ResourceRow
                        key={rowKey}
                        resource={resource}
                        isExpanded={expandedIds.has(rowKey)}
                        onToggle={() => toggleExpand(rowKey)}
                      />
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}
