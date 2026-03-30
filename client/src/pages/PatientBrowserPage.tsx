import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Search, Loader2, AlertCircle, Users, ChevronDown } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { PatientCard } from '@/components/shared/PatientCard';
import { BulkExportButton } from '@/components/shared/BulkExportButton';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import { capabilityStatement, searchPatients } from '@/api/fhir';
import { submitBulkExportJob } from '@/api/medanon';
import { useConfig } from '@/context/ConfigContext';
import type { PatientSummary } from '@/api/types';

const PAGE_SIZE = 20;

export default function PatientBrowserPage() {
  const navigate = useNavigate();
  const { configProfile } = useConfig();

  const [patients, setPatients] = useState<PatientSummary[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [fhirConnected, setFhirConnected] = useState<boolean | null>(null);
  const [searching, setSearching] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [searchName, setSearchName] = useState('');
  // Committed search term (only changes on Search click / Enter)
  const [committedName, setCommittedName] = useState('');

  // FHIR connectivity check on mount
  useEffect(() => {
    let cancelled = false;
    capabilityStatement()
      .then(() => { if (!cancelled) setFhirConnected(true); })
      .catch(() => { if (!cancelled) setFhirConnected(false); });
    return () => { cancelled = true; };
  }, []);

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
      <div className="mb-6 flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-sm sm:flex-row sm:items-end">
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
          {searching ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Search className="h-4 w-4" />
          )}
          {searching ? 'Searching…' : 'Search'}
        </Button>
        <div className="sm:self-end">
          <BulkExportButton
            label="Bulk Export All"
            onSubmit={() => submitBulkExportJob({ config_profile: configProfile })}
            filename="all-patients-deidentified.ndjson"
            disabled={!fhirConnected}
          />
        </div>
      </div>

      {/* Empty state */}
      {!searching && patients.length === 0 && fhirConnected && (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16 text-center text-muted-foreground">
          <Users className="mb-3 h-10 w-10" />
          <p className="text-sm">No patients to display. Use the search bar above to find patients.</p>
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

          {/* Load more */}
          {hasMore && (
            <div className="mt-6 flex justify-center">
              <Button
                variant="outline"
                onClick={handleLoadMore}
                disabled={loadingMore}
              >
                {loadingMore ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <ChevronDown className="h-4 w-4" />
                )}
                {loadingMore ? 'Loading…' : 'Load more'}
              </Button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
