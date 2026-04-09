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
import { Plus, Trash2 } from 'lucide-react';
import { type Action, type LocalRule, VALID_ACTIONS, newRule } from './configConstants';
import { ParamsEditor } from './ParamsEditor';

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
  const update = (id: string, patch: Partial<LocalRule>) =>
    onChange(rules.map((r) => (r._id === id ? { ...r, ...patch } : r)));

  const remove = (id: string) => onChange(rules.filter((r) => r._id !== id));

  const addRule = () => onChange([...rules, newRule()]);

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <span className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Rules ({rules.length})
        </span>
        <Button variant="outline" size="sm" onClick={addRule} className="h-7 text-xs">
          <Plus className="size-3" />
          Add Rule
        </Button>
      </div>

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
                <TableHead className="w-[30%] text-xs">FHIRPath Match</TableHead>
                <TableHead className="w-[15%] text-xs">Action</TableHead>
                <TableHead className="text-xs">Params</TableHead>
                <TableHead className="w-[20%] text-xs">Name (optional)</TableHead>
                <TableHead className="w-8" />
              </TableRow>
            </TableHeader>
            <TableBody>
              {rules.map((rule) => (
                <TableRow key={rule._id}>
                  <TableCell className="py-1.5">
                    <Input
                      value={rule.match}
                      onChange={(e) => update(rule._id, { match: e.target.value })}
                      placeholder="e.g. Patient.name"
                      className="h-7 font-mono text-xs"
                    />
                  </TableCell>
                  <TableCell className="py-1.5">
                    <Select
                      value={rule.action}
                      onValueChange={(v) =>
                        update(rule._id, { action: v as Action, params: {} })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        {VALID_ACTIONS.map((a) => (
                          <SelectItem key={a} value={a} className="text-xs">
                            {a}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </TableCell>
                  <TableCell className="py-1.5">
                    <ParamsEditor
                      action={rule.action}
                      params={rule.params}
                      onChange={(p) => update(rule._id, { params: p })}
                    />
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
              ))}
            </TableBody>
          </Table>
        </div>
      )}
    </div>
  );
}
