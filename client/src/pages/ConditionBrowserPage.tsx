import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Search, Loader2, AlertCircle, Stethoscope, ChevronDown } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { ConditionCard } from '@/components/shared/ConditionCard';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { capabilityStatement, searchConditions } from '@/api/fhir';
import type { ConditionRow } from '@/api/types';

const PAGE_SIZE = 20;

const CLINICAL_STATUSES = [
  { value: 'any', label: 'Any status' },
  { value: 'active', label: 'Active' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'inactive', label: 'Inactive' },
];

export default function ConditionBrowserPage() {
  const navigate = useNavigate();

  const [conditions, setConditions] = useState<ConditionRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [hasMore, setHasMore] = useState(false);
  const [offset, setOffset] = useState(0);
  const [fhirConnected, setFhirConnected] = useState<boolean | null>(null);
  const [searching, setSearching] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [query, setQuery] = useState('');
  const [clinicalStatus, setClinicalStatus] = useState('any');
  // Committed filters (only applied on Search)
  const [committedQuery, setCommittedQuery] = useState('');
  const [committedStatus, setCommittedStatus] = useState('any');

  const uniquePatients = new Set(conditions.map((c) => c.patient_id)).size;

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
    setCommittedQuery(query);
    setCommittedStatus(clinicalStatus);
    try {
      const statusParam = clinicalStatus === 'any' ? undefined : clinicalStatus;
      const result = await searchConditions(query || undefined, statusParam, PAGE_SIZE, 0);
      setConditions(result.items);
      setTotal(result.total);
      setHasMore(result.hasMore);
      setOffset(PAGE_SIZE);
    } catch {
      toast.error('Failed to search conditions');
    } finally {
      setSearching(false);
    }
  };

  const handleLoadMore = async () => {
    setLoadingMore(true);
    try {
      const statusParam = committedStatus === 'any' ? undefined : committedStatus;
      const result = await searchConditions(committedQuery || undefined, statusParam, PAGE_SIZE, offset);
      setConditions((prev) => [...prev, ...result.items]);
      setHasMore(result.hasMore);
      setOffset((prev) => prev + PAGE_SIZE);
    } catch {
      toast.error('Failed to load more conditions');
    } finally {
      setLoadingMore(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && fhirConnected && !searching) {
      handleSearch();
    }
  };

  const handleDeidentify = (condition: ConditionRow) => {
    navigate(`/deidentify/${condition.patient_id}`, { state: { name: condition.patient_name, from: '/conditions' } });
  };

  return (
    <div>
      <PageHeader
        title="Condition Browser"
        description="Search FHIR Condition resources by illness name or SNOMED code, then de-identify the linked patient"
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
          <label htmlFor="condition-query" className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Illness name or SNOMED code
          </label>
          <Input
            id="condition-query"
            placeholder="e.g. Diabetes, 73211009…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={handleKeyDown}
            disabled={!fhirConnected || searching}
          />
        </div>
        <div className="w-full sm:w-44">
          <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Clinical status
          </label>
          <Select
            value={clinicalStatus}
            onValueChange={(val) => setClinicalStatus(val ?? 'any')}
            disabled={!fhirConnected || searching}
          >
            <SelectTrigger className="w-full">
              <SelectValue placeholder="Any status" />
            </SelectTrigger>
            <SelectContent>
              {CLINICAL_STATUSES.map((status) => (
                <SelectItem key={status.value} value={status.value}>
                  {status.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        </div>
        <Button
          onClick={handleSearch}
          disabled={!fhirConnected || searching}
          className="sm:self-end"
        >
          {searching ? (
            <Loader2 className="h-4 w-4 animate-spin" />
          ) : (
            <Search className="h-4 w-4" />
          )}
          {searching ? 'Searching…' : 'Search'}
        </Button>
      </div>

      {/* Empty state */}
      {!searching && conditions.length === 0 && fhirConnected && (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16 text-center text-muted-foreground">
          <Stethoscope className="mb-3 h-10 w-10" />
          <p className="text-sm">No conditions to display. Use the search bar above to find conditions.</p>
        </div>
      )}

      {/* Loading state */}
      {searching && (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          <span className="text-sm">Searching conditions…</span>
        </div>
      )}

      {/* Results */}
      {!searching && conditions.length > 0 && (
        <>
          <p className="mb-4 text-sm text-muted-foreground">
            {total !== null ? (
              <>{total} condition{total !== 1 ? 's' : ''} total &middot; showing {conditions.length}</>
            ) : (
              <>{conditions.length} condition{conditions.length !== 1 ? 's' : ''} found</>
            )}
            {' '}across {uniquePatients} unique patient{uniquePatients !== 1 ? 's' : ''}
          </p>

          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            {conditions.map((condition) => (
              <ConditionCard
                key={condition.condition_id}
                condition={condition}
                selected={false}
                onDeidentify={() => handleDeidentify(condition)}
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
