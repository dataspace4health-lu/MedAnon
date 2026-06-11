import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { toast } from 'sonner';
import { Search, Loader2, AlertCircle, Stethoscope, PackageOpen, X } from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { ConditionGroupCard } from '@/components/shared/ConditionGroupCard';
import type { ConditionGroup } from '@/components/shared/ConditionGroupCard';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { capabilityStatement, searchAllConditions } from '@/api/fhir';
import { submitCohortJob } from '@/api/medanon';
import { useConfig } from '@/context/ConfigContext';
import { useBulkExport } from '@/context/BulkExportContext';
import type { ConditionRow } from '@/api/types';

const CLINICAL_STATUSES = [
  { value: 'any', label: 'Any status' },
  { value: 'active', label: 'Active' },
  { value: 'resolved', label: 'Resolved' },
  { value: 'inactive', label: 'Inactive' },
];

// '__all__' is a sentinel that means "no category filter" — the empty string
// that FHIR uses cannot be used as a SelectItem value in shadcn/ui.
const CONDITION_CATEGORIES = [
  { value: 'encounter-diagnosis,problem-list-item', label: 'Clinical conditions' },
  { value: '__all__', label: 'All conditions' },
  { value: 'social-history', label: 'Social factors only' },
];

/** Convert the UI category value to the FHIR category param ('' = no filter). */
function fhirCategory(val: string): string {
  return val === '__all__' ? '' : val;
}

export default function ConditionBrowserPage() {
  const navigate = useNavigate();
  const { configProfile } = useConfig();
  const { submitExport } = useBulkExport();

  const [conditions, setConditions] = useState<ConditionRow[]>([]);
  const [total, setTotal] = useState<number | null>(null);
  const [fhirConnected, setFhirConnected] = useState<boolean | null>(null);
  const [searching, setSearching] = useState(false);

  // Search form state (editable)
  const [query, setQuery] = useState('');
  const [clinicalStatus, setClinicalStatus] = useState('any');
  const [category, setCategory] = useState('encounter-diagnosis,problem-list-item');

  // Committed filters — only change when Search is clicked
  const [committedQuery, setCommittedQuery] = useState('');
  const [committedStatus, setCommittedStatus] = useState('any');
  const [committedCategory, setCommittedCategory] = useState('encounter-diagnosis,problem-list-item');

  // Selected condition codes for scoped export (by SNOMED code).
  const [selectedCodes, setSelectedCodes] = useState<Set<string>>(new Set());

  const uniquePatients = new Set(conditions.map((c) => c.patient_id)).size;

  /** Group flat condition rows by code (or display when no code), deduplicating
   *  patients within each group. Preserves insertion order so earlier pages
   *  remain at the top. */
  const conditionGroups = useMemo<ConditionGroup[]>(() => {
    const groupMap = new Map<string, ConditionGroup>();
    for (const row of conditions) {
      const groupKey = row.code || row.display || row.condition_id;
      if (!groupMap.has(groupKey)) {
        groupMap.set(groupKey, {
          key: groupKey,
          code: row.code,
          display: row.display,
          patients: [],
        });
      }
      const group = groupMap.get(groupKey)!;
      // Deduplicate patients within the group by patient_id + clinical_status
      const patientKey = `${row.patient_id}::${row.clinical_status}`;
      if (!group.patients.some((p) => `${p.patient_id}::${p.clinical_status}` === patientKey)) {
        group.patients.push({
          patient_id: row.patient_id,
          patient_name: row.patient_name,
          patient_birth_date: row.patient_birth_date,
          patient_gender: row.patient_gender,
          clinical_status: row.clinical_status,
        });
      }
    }
    return Array.from(groupMap.values());
  }, [conditions]);

  // FHIR connectivity check + initial data load on mount (default filters)
  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      try {
        await capabilityStatement();
        if (cancelled) return;
        setFhirConnected(true);
        setSearching(true);
        const { items, total: t } = await searchAllConditions(
          undefined,
          undefined,
          'encounter-diagnosis,problem-list-item',
        );
        if (cancelled) return;
        setConditions(items);
        setTotal(t);
      } catch {
        if (!cancelled) setFhirConnected(false);
      } finally {
        if (!cancelled) setSearching(false);
      }
    };
    run();
    return () => { cancelled = true; };
  }, []);

  const handleSearch = async () => {
    setSearching(true);
    setCommittedQuery(query);
    setCommittedStatus(clinicalStatus);
    setCommittedCategory(category);
    setSelectedCodes(new Set());
    try {
      const statusParam = clinicalStatus === 'any' ? undefined : clinicalStatus;
      const result = await searchAllConditions(
        query || undefined,
        statusParam,
        fhirCategory(category),
      );
      setConditions(result.items);
      setTotal(result.total);
    } catch {
      toast.error('Failed to search conditions');
    } finally {
      setSearching(false);
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && fhirConnected && !searching) {
      handleSearch();
    }
  };

  const handleDeidentify = (patientId: string, patientName: string) => {
    navigate(`/deidentify/${patientId}`, {
      state: { name: patientName, from: '/conditions' },
    });
  };

  const toggleCodeSelection = (code: string) => {
    if (!code) return;
    setSelectedCodes((prev) => {
      const next = new Set(prev);
      if (next.has(code)) next.delete(code);
      else next.add(code);
      return next;
    });
  };

  const clearSelection = () => setSelectedCodes(new Set());

  // Build the FHIR search params for the cohort export.
  // Selected codes take priority; otherwise fall back to the active search filters.
  const buildExportSearchParams = (): Record<string, string> => {
    if (selectedCodes.size > 0) {
      return { code: [...selectedCodes].join(',') };
    }
    const params: Record<string, string> = {};
    if (committedQuery) {
      const isCode = /^\d[\d.-]*$/.test(committedQuery.trim());
      params[isCode ? 'code' : 'code:text'] = committedQuery;
    }
    if (committedStatus !== 'any') params['clinical-status'] = committedStatus;
    const fhirCat = fhirCategory(committedCategory);
    if (fhirCat) params['category'] = fhirCat;
    return params;
  };

  // Derive a label for the bulk de-identify page title.
  const deriveConditionLabel = (): string => {
    if (selectedCodes.size === 1) {
      const code = [...selectedCodes][0];
      const group = conditionGroups.find((g) => g.code === code);
      return group?.display || code;
    }
    if (selectedCodes.size > 1) {
      return `${selectedCodes.size} selected conditions`;
    }
    return committedQuery || 'matching conditions';
  };

  const handleBulkExport = () => {
    const label = deriveConditionLabel();
    const exportId = submitExport(
      `Export: ${label}`,
      'conditions-deidentified.ndjson',
      () =>
        submitCohortJob({
          search_type: 'Condition',
          search_params: buildExportSearchParams(),
          config_profile: configProfile,
        }),
      { source: 'condition', conditionName: label, configProfile },
    );
    navigate('/bulk-deidentify', {
      state: { autoSelectId: exportId },
    });
  };

  const exportLabel =
    selectedCodes.size > 0
      ? `Export ${selectedCodes.size} selected`
      : 'Bulk Export by Condition';

  return (
    <div>
      <PageHeader
        title="Condition Browser"
        description="Search clinical FHIR Condition resources by illness name or SNOMED code, then de-identify the linked patient"
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
      <div className="mb-6 flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-sm">
        {/* Row 1: text inputs + search button */}
        <div className="flex flex-col gap-3 sm:flex-row sm:items-end">
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
                {CLINICAL_STATUSES.map((s) => (
                  <SelectItem key={s.value} value={s.value}>{s.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <div className="w-full sm:w-52">
            <label className="mb-1.5 block text-xs font-semibold uppercase tracking-wide text-muted-foreground">
              Condition type
            </label>
            <Select
              value={category}
              onValueChange={(val) => setCategory(val ?? 'encounter-diagnosis,problem-list-item')}
              disabled={!fhirConnected || searching}
            >
              <SelectTrigger className="w-full">
                <SelectValue placeholder="Clinical conditions" />
              </SelectTrigger>
              <SelectContent>
                {CONDITION_CATEGORIES.map((c) => (
                  <SelectItem key={c.value || '__all__'} value={c.value}>{c.label}</SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>
        {/* Row 2: action buttons */}
        <div className="flex flex-wrap gap-2">
          <Button
            onClick={handleSearch}
            disabled={!fhirConnected || searching}
          >
            {searching ? <Loader2 className="h-4 w-4 animate-spin" /> : <Search className="h-4 w-4" />}
            {searching ? 'Searching…' : 'Search'}
          </Button>
          <Button
            variant="outline"
            className="gap-1.5"
            disabled={!fhirConnected || selectedCodes.size === 0}
            onClick={handleBulkExport}
          >
            <PackageOpen className="h-4 w-4" />
            {exportLabel}
          </Button>
        </div>
      </div>

      {/* Selection status bar */}
      {selectedCodes.size > 0 && (
        <div className="mb-4 flex items-center justify-between rounded-lg border border-primary/30 bg-primary/5 px-4 py-2.5 text-sm">
          <span className="font-medium text-primary">
            {selectedCodes.size} condition code{selectedCodes.size !== 1 ? 's' : ''} selected for export
          </span>
          <button
            type="button"
            onClick={clearSelection}
            className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground"
          >
            <X className="size-3" />
            Clear selection
          </button>
        </div>
      )}

      {/* Empty state */}
      {!searching && conditions.length === 0 && fhirConnected && (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16 text-center text-muted-foreground">
          <Stethoscope className="mb-3 h-10 w-10" />
          <p className="text-sm">No conditions found. Try adjusting the filters above.</p>
        </div>
      )}

      {/* Loading state */}
      {searching && (
        <div className="flex items-center justify-center py-16 text-muted-foreground">
          <Loader2 className="mr-2 h-5 w-5 animate-spin" />
          <span className="text-sm">Loading all conditions…</span>
        </div>
      )}

      {/* Results */}
      {!searching && conditions.length > 0 && (
        <>
          <p className="mb-4 text-sm text-muted-foreground">
            {total !== null ? (
              <>{total} condition record{total !== 1 ? 's' : ''} total &middot; </>
            ) : null}
            {conditionGroups.length} condition group{conditionGroups.length !== 1 ? 's' : ''}
            {' '}across {uniquePatients} unique patient{uniquePatients !== 1 ? 's' : ''}
            {selectedCodes.size > 0 && (
              <span className="ml-2 font-medium text-primary">· {selectedCodes.size} selected</span>
            )}
          </p>

          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {conditionGroups.map((group) => (
              <ConditionGroupCard
                key={group.key}
                group={group}
                selected={!!group.code && selectedCodes.has(group.code)}
                onSelect={group.code ? () => toggleCodeSelection(group.code) : undefined}
                onDeidentify={handleDeidentify}
              />
            ))}
          </div>
        </>
      )}
    </div>
  );
}
