import { useState } from 'react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { Input } from '@/components/ui/input';
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from '@/components/ui/collapsible';
import { Sparkles, ChevronDown, AlertCircle, Loader2 } from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';
import { type LocalRule, parseYamlIntoRules } from './configConstants';
import { generateConfig } from '@/api/agents';

// ---------------------------------------------------------------------------
// AiGeneratePanel -- describe what you need, AI generates rules
// ---------------------------------------------------------------------------

export function AiGeneratePanel({
  onImport,
}: {
  onImport: (rules: LocalRule[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [prompt, setPrompt] = useState('');
  const [regulation, setRegulation] = useState('');
  const [generating, setGenerating] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const handleGenerate = async () => {
    setErr(null);
    if (prompt.trim().length < 10) {
      setErr('Please describe your requirements in at least 10 characters.');
      return;
    }
    setGenerating(true);
    try {
      const res = await generateConfig({ prompt: prompt.trim(), regulation: regulation.trim() || undefined });

      if (!res.yaml) {
        setErr('AI returned an empty configuration. Try rephrasing your prompt.');
        return;
      }

      const { rules: parsed, error: parseErr } = parseYamlIntoRules(res.yaml);
      if (parseErr) {
        setErr(parseErr);
        return;
      }

      onImport(parsed);
      setPrompt('');
      setRegulation('');
      setOpen(false);

      const sourceLabel = res.source === 'ai' ? 'AI' : 'template';
      toast.success(
        `Generated ${parsed.length} rule${parsed.length !== 1 ? 's' : ''} from ${sourceLabel}.${!res.valid ? ' Note: validation warnings detected.' : ''}`,
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setGenerating(false);
    }
  };

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground">
        <Sparkles className="size-3" />
        Generate with AI
        <ChevronDown className={cn('size-3 transition-transform', open && 'rotate-180')} />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2">
        <div className="rounded-lg border bg-muted/30 p-3 space-y-2">
          <p className="text-xs text-muted-foreground">
            Describe your de-identification requirements in plain language. The AI agent will generate
            matching rules for you.
          </p>
          <Textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder="e.g. I need HIPAA Safe Harbor compliant de-identification that redacts patient names, hashes IDs for linkage, and generalizes dates to year only..."
            className="h-28 resize-none text-xs"
            disabled={generating}
          />
          <div>
            <label className="mb-1 block text-xs text-muted-foreground">
              Target regulation (optional)
            </label>
            <Input
              value={regulation}
              onChange={(e) => setRegulation(e.target.value)}
              placeholder="e.g. HIPAA, GDPR, HIPAA Safe Harbor"
              className="text-xs"
              disabled={generating}
            />
          </div>
          {err && (
            <p className="flex items-center gap-1 text-xs text-destructive">
              <AlertCircle className="size-3 shrink-0" /> {err}
            </p>
          )}
          <div className="flex gap-2">
            <Button
              size="sm"
              className="h-7 text-xs"
              onClick={handleGenerate}
              disabled={generating || !prompt.trim()}
            >
              {generating && <Loader2 className="size-3 animate-spin" />}
              {generating ? 'Generating...' : 'Generate Rules'}
            </Button>
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-xs"
              onClick={() => setOpen(false)}
              disabled={generating}
            >
              Cancel
            </Button>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
