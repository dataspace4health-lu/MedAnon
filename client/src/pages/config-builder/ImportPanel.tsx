import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from '@/components/ui/collapsible';
import { Upload, ChevronDown, AlertCircle } from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { type LocalRule, parseYamlIntoRules, deduplicateIncoming } from './configConstants';

// ---------------------------------------------------------------------------
// ImportPanel -- paste YAML to populate rule table
// ---------------------------------------------------------------------------

export function ImportPanel({
  onImport,
  existingRules = [],
}: {
  onImport: (rules: LocalRule[]) => void;
  existingRules?: LocalRule[];
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [err, setErr] = useState<string | null>(null);

  const handleImport = () => {
    setErr(null);
    try {
      const { rules: parsed, error: parseErr } = parseYamlIntoRules(text);
      if (parseErr) {
        setErr(parseErr);
        return;
      }
      const { added, skipped } = deduplicateIncoming(parsed, existingRules);
      if (added.length === 0) {
        setErr('All pasted rules duplicate existing match expressions — nothing imported.');
        return;
      }
      onImport(added);
      setText('');
      setOpen(false);
      toast.success(
        skipped.length > 0
          ? `Imported ${added.length} rule${added.length !== 1 ? 's' : ''}; skipped ${skipped.length} duplicate${skipped.length !== 1 ? 's' : ''}.`
          : `Imported ${added.length} rule${added.length !== 1 ? 's' : ''}.`,
      );
    } catch (e) {
      setErr(String(e));
    }
  };

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground">
        <Upload className="size-3" />
        Import from YAML
        <ChevronDown className={cn('size-3 transition-transform', open && 'rotate-180')} />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2">
        <div className="rounded-lg border bg-muted/30 p-3 space-y-2">
          <p className="text-xs text-muted-foreground">
            Paste an existing config YAML. The rules will be imported into the table — params require manual review.
          </p>
          <Textarea
            value={text}
            onChange={(e) => setText(e.target.value)}
            placeholder="Paste YAML here..."
            className="h-40 resize-none font-mono text-xs"
          />
          {err && (
            <p className="flex items-center gap-1 text-xs text-destructive">
              <AlertCircle className="size-3" /> {err}
            </p>
          )}
          <div className="flex gap-2">
            <Button size="sm" className="h-7 text-xs" onClick={handleImport} disabled={!text.trim()}>
              Import Rules
            </Button>
            <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
