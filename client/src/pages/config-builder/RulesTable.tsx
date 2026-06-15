import { useMemo, useState } from 'react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  Table,
  TableHeader,
  TableBody,
  TableHead,
  TableRow,
  TableCell,
} from '@/components/ui/table';
import { Plus, Trash2, Lock, Shuffle, AlertTriangle, Wand2, Filter } from 'lucide-react';
import { cn } from '@/lib/utils';
import { toast } from 'sonner';
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/tooltip';
import {
  type Action,
  type LocalRule,
  VALID_ACTIONS,
  DETERMINISTIC_ACTIONS,
  newRule,
  validateParams,
  cleanupRules,
  resourceTypeOf,
  resourceTypesIn,
  actionLabel,
} from './configConstants';
import { ParamsEditor } from './ParamsEditor';

// Sentinel for the "show every resource type" option in the filter dropdown.
const ALL_TYPES = '__all__';

// ---------------------------------------------------------------------------
// RulesTable
// ---------------------------------------------------------------------------

export function RulesTable({
  rules,
  onChange,
}: {
  rules: LocalRule[];
  onChange: (rules: LocalRule[]) => void;
}) {
  // Resource-type filter for the rules list. Editing still operates on the full
  // `rules` array by `_id`, so filtering only changes which rows are rendered.
  const [filterType, setFilterType] = useState<string>(ALL_TYPES);

  const update = (id: string, patch: Partial<LocalRule>) =>
    onChange(rules.map((r) => (r._id === id ? { ...r, ...patch } : r)));

  const remove = (id: string) => onChange(rules.filter((r) => r._id !== id));

  const addRule = () => onChange([...rules, newRule()]);

  // Distinct resource types present, for the filter dropdown.
  const presentTypes = useMemo(() => resourceTypesIn(rules), [rules]);

  // When the active filter no longer matches any rule (e.g. the last Patient
  // rule was deleted), fall back to showing all so the list never looks empty
  // for a stale reason.
  const effectiveFilter =
    filterType !== ALL_TYPES && !presentTypes.includes(filterType)
      ? ALL_TYPES
      : filterType;

  const visibleRules = useMemo(
    () =>
      effectiveFilter === ALL_TYPES
        ? rules
        : rules.filter((r) => resourceTypeOf(r.match) === effectiveFilter),
    [rules, effectiveFilter],
  );

  // Per-rule param validation errors: ruleId → error messages.
  const paramErrors = useMemo(() => {
    const out = new Map<string, string[]>();
    for (const r of rules) {
      const errs = validateParams(r.action, r.params);
      if (errs.length > 0) out.set(r._id, errs.map((e) => e.message));
    }
    return out;
  }, [rules]);

  // Match expressions that appear more than once (trimmed, non-empty).
  const duplicateMatches = useMemo(() => {
    const counts = new Map<string, number>();
    for (const r of rules) {
      const m = r.match.trim();
      if (m) counts.set(m, (counts.get(m) ?? 0) + 1);
    }
    return new Set([...counts.entries()].filter(([, n]) => n > 1).map(([m]) => m));
  }, [rules]);

  // Match expressions that appear more than once with DIFFERENT actions (conflicts).
  const conflictMatches = useMemo(() => {
    const seen = new Map<string, string>(); // match → first action seen
    const conflicts = new Set<string>();
    for (const r of rules) {
      const m = r.match.trim();
      if (!m) continue;
      if (!seen.has(m)) {
        seen.set(m, r.action);
      } else if (seen.get(m) !== r.action) {
        conflicts.add(m);
      }
    }
    return conflicts;
  }, [rules]);

  const hasIssues = duplicateMatches.size > 0 || conflictMatches.size > 0;

  const handleCleanup = () => {
    const { rules: cleaned, removedExact, removedConflict } = cleanupRules(rules);
    onChange(cleaned);
    const parts: string[] = [];
    if (removedExact > 0) parts.push(`${removedExact} exact duplicate${removedExact !== 1 ? 's' : ''}`);
    if (removedConflict > 0) parts.push(`${removedConflict} conflicting rule${removedConflict !== 1 ? 's' : ''} (first rule kept)`);
    if (parts.length === 0) {
      toast.message('No duplicates or conflicts found.');
    } else {
      toast.success(`Removed ${parts.join(' and ')}.`);
    }
  };

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2.5 min-w-0">
          <span className="shrink-0 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            Rules (
            {effectiveFilter === ALL_TYPES
              ? rules.length
              : `${visibleRules.length} of ${rules.length}`}
            )
          </span>
          {presentTypes.length > 1 && (
            <Select
              value={effectiveFilter}
              onValueChange={(v) => setFilterType(v ?? ALL_TYPES)}
            >
              <SelectTrigger className="h-7 w-auto min-w-36 gap-1 text-xs">
                <Filter className="size-3 shrink-0 text-muted-foreground" />
                <SelectValue placeholder="All resource types" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={ALL_TYPES} className="text-xs">
                  All resource types ({rules.length})
                </SelectItem>
                {presentTypes.map((t) => {
                  const count = rules.filter(
                    (r) => resourceTypeOf(r.match) === t,
                  ).length;
                  return (
                    <SelectItem key={t} value={t} className="text-xs">
                      <span className="font-mono">{t}</span>{' '}
                      <span className="text-muted-foreground">({count})</span>
                    </SelectItem>
                  );
                })}
              </SelectContent>
            </Select>
          )}
        </div>
        <div className="flex shrink-0 items-center gap-2">
          {hasIssues && (
            <Button
              variant="outline"
              size="sm"
              onClick={handleCleanup}
              className="h-7 gap-1 text-xs border-amber-300 text-amber-800 hover:bg-amber-50 dark:border-amber-700 dark:text-amber-300 dark:hover:bg-amber-950/40"
            >
              <Wand2 className="size-3" />
              Fix duplicates &amp; conflicts
            </Button>
          )}
          <Button variant="outline" size="sm" onClick={addRule} className="h-7 text-xs">
            <Plus className="size-3" />
            Add Rule
          </Button>
        </div>
      </div>

      {conflictMatches.size > 0 && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-orange-300 bg-orange-50 px-3 py-2 text-xs text-orange-800 dark:border-orange-700 dark:bg-orange-950/40 dark:text-orange-300">
          <span className="flex items-center gap-2">
            <AlertTriangle className="size-3.5 shrink-0" />
            {conflictMatches.size === 1
              ? '1 match expression has conflicting actions — only the first rule fires.'
              : `${conflictMatches.size} match expressions have conflicting actions — only the first rule fires each.`}
          </span>
          <button onClick={handleCleanup} className="shrink-0 underline font-medium">
            Fix now
          </button>
        </div>
      )}
      {duplicateMatches.size > 0 && conflictMatches.size === 0 && (
        <div className="flex items-center justify-between gap-2 rounded-md border border-amber-300 bg-amber-50 px-3 py-2 text-xs text-amber-800 dark:border-amber-700 dark:bg-amber-950/40 dark:text-amber-300">
          <span className="flex items-center gap-2">
            <AlertTriangle className="size-3.5 shrink-0" />
            {duplicateMatches.size === 1
              ? '1 duplicate match expression — only the first matching rule fires at runtime.'
              : `${duplicateMatches.size} duplicate match expressions — only the first matching rule fires at runtime.`}
          </span>
          <button onClick={handleCleanup} className="shrink-0 underline font-medium">
            Remove duplicates
          </button>
        </div>
      )}
      {paramErrors.size > 0 && (
        <div className="flex items-center gap-2 rounded-md border border-red-300 bg-red-50 px-3 py-2 text-xs text-red-800 dark:border-red-700 dark:bg-red-950/40 dark:text-red-300">
          <AlertTriangle className="size-3.5 shrink-0" />
          {paramErrors.size === 1
            ? '1 rule has invalid params — hover the ⚠ icon in the Params column for details.'
            : `${paramErrors.size} rules have invalid params — hover the ⚠ icons for details.`}
        </div>
      )}

      {rules.length === 0 ? (
        <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-10 text-center text-muted-foreground">
          <Plus className="mb-2 size-8" />
          <p className="text-sm">No rules yet. Add one above.</p>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-md border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead className="w-[26%] text-xs">FHIRPath Match</TableHead>
                <TableHead className="w-[20%] text-xs">Action</TableHead>
                <TableHead className="text-xs">Params</TableHead>
                <TableHead className="w-[16%] text-xs">Name (optional)</TableHead>
                <TableHead className="w-8" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {visibleRules.length === 0 ? (
                <TableRow>
                  <TableCell
                    colSpan={5}
                    className="py-6 text-center text-xs text-muted-foreground"
                  >
                    No rules for{' '}
                    <span className="font-mono font-medium">{effectiveFilter}</span>.{' '}
                    <button
                      onClick={() => setFilterType(ALL_TYPES)}
                      className="underline underline-offset-2 hover:text-foreground"
                    >
                      Show all
                    </button>
                  </TableCell>
                </TableRow>
              ) : (
                visibleRules.map((rule) => {
                const m = rule.match.trim();
                const isDup = m !== '' && duplicateMatches.has(m);
                const isConflict = m !== '' && conflictMatches.has(m);
                return (
                  <TableRow
                    key={rule._id}
                    className={cn(
                      isConflict && 'bg-orange-50/70 dark:bg-orange-950/20',
                      !isConflict && isDup && 'bg-amber-50/60 dark:bg-amber-950/20',
                    )}
                  >
                    <TableCell className="py-1.5">
                      <div className="flex items-center gap-1">
                        <Input
                          value={rule.match}
                          onChange={(e) => update(rule._id, { match: e.target.value })}
                          placeholder="e.g. Patient.name"
                          className={cn(
                            'h-7 font-mono text-xs',
                            isConflict && 'border-orange-400 focus-visible:ring-orange-400',
                            !isConflict && isDup && 'border-amber-400 focus-visible:ring-amber-400',
                          )}
                        />
                        {isConflict && (
                          <span title="Conflicting actions for same match — only the first rule fires" className="shrink-0 inline-flex">
                            <AlertTriangle className="size-3.5 text-orange-500" />
                          </span>
                        )}
                        {!isConflict && isDup && (
                          <span title="Duplicate match — this rule may be shadowed" className="shrink-0 inline-flex">
                            <AlertTriangle className="size-3.5 text-amber-500" />
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="py-1.5">
                      <div className="flex items-center gap-1">
                        <Select
                          value={rule.action}
                          onValueChange={(v) =>
                            update(rule._id, { action: v as Action, params: {} })
                          }
                        >
                          <SelectTrigger className="h-7 min-w-0 flex-1 text-xs">
                            <SelectValue />
                          </SelectTrigger>
                          <SelectContent className="min-w-[20rem]">
                            <div className="px-2 py-1.5 text-[11px] text-muted-foreground border-b mb-1">
                              <Lock className="size-3 inline mr-1 text-emerald-500" />
                              deterministic &nbsp;·&nbsp;
                              <Shuffle className="size-3 inline mr-1 text-amber-500" />
                              non-deterministic
                            </div>
                            {VALID_ACTIONS.map((a) => (
                              <SelectItem key={a} value={a} className="text-sm">
                                <span className="flex w-full items-center gap-2">
                                  {DETERMINISTIC_ACTIONS.has(a) ? (
                                    <Lock className="size-3 shrink-0 text-emerald-500" />
                                  ) : (
                                    <Shuffle className="size-3 shrink-0 text-amber-500" />
                                  )}
                                  <span className="truncate">{actionLabel(a)}</span>
                                  <span className="ml-auto shrink-0 pl-3 font-mono text-[11px] text-muted-foreground/70">
                                    {a}
                                  </span>
                                </span>
                              </SelectItem>
                            ))}
                          </SelectContent>
                        </Select>
                        {!DETERMINISTIC_ACTIONS.has(rule.action) && (
                          <span
                            title={`"${rule.action}" produces different output each run — referential integrity across resources may be broken`}
                            className="shrink-0 flex items-center gap-0.5 rounded border border-amber-300 bg-amber-50 dark:bg-amber-900/20 dark:border-amber-700 px-1 py-px text-[10px] font-medium text-amber-700 dark:text-amber-400"
                          >
                            <Shuffle className="size-2.5" />
                            non-det
                          </span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="py-1.5">
                      <div className="flex items-center gap-1.5">
                        <ParamsEditor
                          action={rule.action}
                          params={rule.params}
                          onChange={(p) => update(rule._id, { params: p })}
                        />
                        {paramErrors.has(rule._id) && (
                          <TooltipProvider delay={100}>
                            <Tooltip>
                              <TooltipTrigger>
                                <span className="shrink-0 cursor-default">
                                  <AlertTriangle className="size-3.5 text-red-500" />
                                </span>
                              </TooltipTrigger>
                              <TooltipContent side="top" className="max-w-xs text-xs">
                                <ul className="space-y-0.5">
                                  {paramErrors.get(rule._id)!.map((msg, i) => (
                                    <li key={i}>• {msg}</li>
                                  ))}
                                </ul>
                              </TooltipContent>
                            </Tooltip>
                          </TooltipProvider>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="py-1.5">
                      <Input
                        value={rule.name}
                        onChange={(e) => update(rule._id, { name: e.target.value })}
                        placeholder="Label..."
                        className="h-7 text-xs"
                      />
                    </TableCell>
                    <TableCell className="py-1.5 text-center">
                      <button
                        onClick={() => remove(rule._id)}
                        className="text-muted-foreground transition-colors hover:text-destructive"
                        aria-label="Remove rule"
                      >
                        <Trash2 className="size-3.5" />
                      </button>
                    </TableCell>
                  </TableRow>
                );
                })
              )}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
