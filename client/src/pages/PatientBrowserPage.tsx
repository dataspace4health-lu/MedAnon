import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import {
  Search, Loader2, AlertCircle, Users, ChevronDown, PackageOpen,
  ChevronRight, Check,
} from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PatientCard } from '@/components/shared/PatientCard';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { capabilityStatement, searchPatients, fetchResourceTypeCounts } from '@/api/fhir';
import type { ResourceTypeCount } from '@/api/fhir';
import { submitBulkExportJob } from '@/api/medanon';
import { useConfig } from '@/context/ConfigContext';
import { useBulkExport } from '@/context/BulkExportContext';
import type { PatientSummary } from '@/api/types';

const PAGE_SIZE = 20;

export default function PatientBrowserPage() {
  const navigate = useNavigate();
  const { configProfile } = useConfig();
  const { submitExport } = useBulkExport();

  const [patients, setPatients] = useState<PatientSummary[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [fhirConnected, setFhirConnected] = useState<boolean | null>(null);
  const [searching, setSearching] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [searchName, setSearchName] = useState('');
  const [committedName, setCommittedName] = useState('');

  // Resource type picker state
  const [typeCounts, setTypeCounts] = useState<ResourceTypeCount[]>([]);
  const [typeCountsLoading, setTypeCountsLoading] = useState(false);
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  const [typePickerOpen, setTypePickerOpen] = useState(false);
  const [exporting, setExporting] = useState(false);

  // FHIR connectivity check + initial data load
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        await capabilityStatement();
        if (cancelled) return;
        setFhirConnected(true);
        setSearching(true);
        const result = await searchPatients(undefined, PAGE_SIZE, 0);
        if (cancelled) return;
        setPatients(result.items);
        setTotal(result.total);
        setHasMore(result.hasMore);
        setOffset(PAGE_SIZE);
      } catch {
        if (!cancelled) setFhirConnected(false);
      } finally {
        if (!cancelled) setSearching(false);
      }
    };
    run();
    return () => { cancelled = true; };
  }, []);

  // Fetch resource type counts when FHIR is connected
  useEffect(() => {
    if (!fhirConnected) return;
    let cancelled = false;
    setTypeCountsLoading(true);
    fetchResourceTypeCounts()
      .then((counts) => {
        if (cancelled) return;
        setTypeCounts(counts);
        setSelectedTypes(new Set(counts.map((c) => c.type)));
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setTypeCountsLoading(false); });
    return () => { cancelled = true; };
  }, [fhirConnected]);

  const handleSearch = async () => {
    setSearching(true);
    setCommittedName(searchName);
    try {
      const result = await searchPatients(searchName || undefined, PAGE_SIZE, 0);
      setPatients(result.items);
      setTotal(result.total);
      setHasMore(result.hasMore);
      setOffset(PAGE_SIZE);
    } catch {
      toast.error('Failed to search patients');
    } finally {
      setSearching(false);
    }
  };

  const handleLoadMore = async () => {
    setLoadingMore(true);
    try {
      const result = await searchPatients(committedName || undefined, PAGE_SIZE, offset);
      setPatients((prev) => [...prev, ...result.items]);
      setHasMore(result.hasMore);
      setOffset((prev) => prev + PAGE_SIZE);
    } catch {
      toast.error('Failed to load more patients');
    } finally {
      setLoadingMore(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && fhirConnected && !searching) {
      handleSearch();
    }
  };

  const handleSelectPatient = (patient: PatientSummary) => {
    navigate(`/deidentify/${patient.id}`, { state: { name: patient.name, from: '/patients' } });
  };

  // Toggle a single resource type
  const toggleType = (type: string) => {
    setSelectedTypes((prev) => {
      const next = new Set(prev);
      if (next.has(type)) next.delete(type);
      else next.add(type);
      return next;
    });
  };

  // Select all / deselect all
  const allSelected = typeCounts.length > 0 && selectedTypes.size === typeCounts.length;
  const toggleAll = () => {
    if (allSelected) {
      setSelectedTypes(new Set());
    } else {
      setSelectedTypes(new Set(typeCounts.map((c) => c.type)));
    }
  };

  // Export selected types: one job per type → each gets its own file
  const handleExportByType = () => {
    if (selectedTypes.size === 0) return;
    setExporting(true);
    const types = Array.from(selectedTypes);
    let lastExportId: string | undefined;

    for (const type of types) {
      lastExportId = submitExport(
        `Export ${type}`,
        `${type.toLowerCase()}-deidentified.ndjson`,
        () => submitBulkExportJob({
          level: 'system',
          resource_type: type,
          config_profile: configProfile,
        }),
        { source: 'all', configProfile },
      );
    }

    setExporting(false);
    if (lastExportId) {
      navigate('/bulk-deidentify', { state: { autoSelectId: lastExportId } });
    }
  };

  const totalServerResources = typeCounts.reduce((sum, c) => sum + c.count, 0);

  return (
    <div>
      <PageHeader
        title="Patient Browser"
        description="Search and browse HAPI FHIR Patient resources, then de-identify with a single click"
      />

      {/* FHIR connectivity banners */}
      {fhirConnected === null && (
        <div className="mb-6 flex items-center gap-2.5 rounded-lg border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
          Checking FHIR server connectivity…
        </div>
      )}
      {fhirConnected === false && (
        <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
          <span>
            <span className="font-medium">FHIR server unreachable.</span>{' '}
            Verify that HAPI FHIR is running and accessible.
          </span>
        </div>
      )}

      {/* Search toolbar */}
      <div className="mb-4 flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-sm sm:flex-row sm:items-end">
        <div className="flex-1">
          <label htmlFor="patient-name" className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Patient name
          </label>
          <Input
            id="patient-name"
            placeholder="Search by name…"
            value={searchName}
            onChange={(e) => setSearchName(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={!fhirConnected || searching}
          />
        </div>
        <Button
          onClick={handleSearch}
          disabled={!fhirConnected || searching}
          className="sm:mb-0 sm:self-end"
        >
          {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
          {searching ? 'Searching…' : 'Search'}
        </Button>
        <div className="sm:self-end">
          <Button
            variant="outline"
            size="sm"
            disabled={!fhirConnected}
            onClick={() => {
              const exportId = submitExport(
                'Bulk Export All',
                'all-deidentified.ndjson',
                () => submitBulkExportJob({ config_profile: configProfile }),
                { source: 'all', configProfile },
              );
              navigate('/bulk-deidentify', { state: { autoSelectId: exportId } });
            }}
          >
            <PackageOpen className="h-4 w-4" />
            Bulk Export All
          </Button>
        </div>
      </div>

      {/* Export by Resource Type picker */}
      {fhirConnected && (
        <div className="mb-6 rounded-xl border bg-card shadow-sm overflow-hidden">
          {/* Collapsible header */}
          <button
            className="flex w-full items-center gap-2.5 px-4 py-3 text-left hover:bg-muted/20 transition-colors"
            onClick={() => setTypePickerOpen((v) => !v)}
          >
            <ChevronRight
              className={cn(
                "size-4 text-muted-foreground transition-transform",
                typePickerOpen && "rotate-90",
              )}
            />
            <PackageOpen className="size-4 text-muted-foreground" />
            <span className="flex-1 text-sm font-medium">Export by Resource Type</span>
            {typeCountsLoading && <Loader2 className="size-3.5 animate-spin text-muted-foreground" />}
            {!typeCountsLoading && typeCounts.length > 0 && (
              <span className="text-xs text-muted-foreground">
                {typeCounts.length} type{typeCounts.length !== 1 ? 's' : ''} · {totalServerResources.toLocaleString()} resources
              </span>
            )}
          </button>

          {/* Expanded content */}
          {typePickerOpen && (
            <div className="border-t px-4 py-3">
              {typeCountsLoading ? (
                <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  Discovering resource types on FHIR server…
                </div>
              ) : typeCounts.length === 0 ? (
                <p className="py-4 text-sm text-muted-foreground">
                  No resources found on the FHIR server.
                </p>
              ) : (
                <>
                  {/* Select all toggle + export button */}
                  <div className="mb-3 flex items-center justify-between">
                    <button
                      className="flex items-center gap-2 text-xs font-medium text-primary hover:underline"
                      onClick={toggleAll}
                    >
                      {allSelected ? 'Deselect All' : 'Select All'}
                    </button>
                    <div className="flex items-center gap-3">
                      <span className="text-xs text-muted-foreground">
                        {selectedTypes.size} selected
                      </span>
                      <Button
                        size="sm"
                        disabled={selectedTypes.size === 0 || exporting}
                        onClick={handleExportByType}
                      >
                        {exporting ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <PackageOpen className="h-3.5 w-3.5" />}
                        Export Selected ({selectedTypes.size})
                      </Button>
                    </div>
                  </div>

                  {/* Type grid */}
                  <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2 lg:grid-cols-3">
                    {typeCounts.map(({ type, count }) => {
                      const isSelected = selectedTypes.has(type);
                      return (
                        <button
                          key={type}
                          className={cn(
                            "flex items-center gap-2.5 rounded-lg border px-3 py-2 text-left transition-colors",
                            isSelected
                              ? "border-primary/40 bg-primary/5"
                              : "border-transparent bg-muted/30 hover:bg-muted/50",
                          )}
                          onClick={() => toggleType(type)}
                        >
                          <div
                            className={cn(
                              "flex size-4 shrink-0 items-center justify-center rounded border transition-colors",
                              isSelected
                                ? "border-primary bg-primary text-primary-foreground"
                                : "border-muted-foreground/30",
                            )}
                          >
                            {isSelected && <Check className="size-3" />}
                          </div>
                          <span className="flex-1 truncate text-sm font-medium">{type}</span>
                          <span className="text-xs tabular-nums text-muted-foreground">
                            {count.toLocaleString()}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                </>
              )}
            </div>
          )}
        </div>
      )}

      {/* Empty state */}
      {!searching && patients.length === 0 && fhirConnected && (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16 text-center text-muted-foreground">
          <Users className="mb-3 h-10 w-10" />
          <p className="text-sm">No patients found. Try adjusting the search term above.</p>
        </div>
      )}

      {/* Loading state */}
      {searching && (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          <span className="text-sm">Searching patients…</span>
        </div>
      )}

      {/* Results */}
      {!searching && patients.length > 0 && (
        <>
          <p className="mb-4 text-sm text-muted-foreground">
            {total !== null ? (
              <>{total} patient{total !== 1 ? 's' : ''} total &middot; showing {patients.length}</>
            ) : (
              <>{patients.length} patient{patients.length !== 1 ? 's' : ''} found</>
            )}
          </p>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {patients.map((patient) => (
              <PatientCard
                key={patient.id}
                patient={patient}
                selected={false}
                onSelect={() => handleSelectPatient(patient)}
              />
            ))}
          </div>

          {hasMore && (
            <div className="mt-6 flex justify-center">
              <Button variant="outline" onClick={handleLoadMore} disabled={loadingMore}>
                {loadingMore ? <Loader2 className="h-4 w-4 animate-spin" /> : <ChevronDown className="h-4 w-4" />}
                {loadingMore ? 'Loading…' : 'Load more'}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
