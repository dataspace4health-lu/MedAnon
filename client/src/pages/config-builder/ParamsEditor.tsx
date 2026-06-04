import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  type Action,
  GENERALIZE_STRATEGIES,
  SCRUB_MODES,
  SCRUB_PATTERNS,
  NLP_MODES,
} from './configConstants';

// ---------------------------------------------------------------------------
// ParamsEditor -- contextual params UI per action
// ---------------------------------------------------------------------------

export function ParamsEditor({
  action,
  params,
  onChange,
}: {
  action: Action;
  params: Record<string, unknown>;
  onChange: (p: Record<string, unknown>) => void;
}) {
  const set = (key: string, value: unknown) =>
    onChange({ ...params, [key]: value });
  const unset = (key: string) => {
    const next = { ...params };
    delete next[key];
    onChange(next);
  };

  if (action === 'redact') {
    return (
      <Input
        placeholder="Replacement (optional)"
        className="h-7 text-xs"
        value={String(params.replacement ?? '')}
        onChange={(e) =>
          e.target.value ? set('replacement', e.target.value) : unset('replacement')
        }
      />
    );
  }

  if (action === 'generalize') {
    return (
      <Select
        value={String(params.strategy ?? 'date_year')}
        onValueChange={(v) => set('strategy', v)}
      >
        <SelectTrigger className="h-7 text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {GENERALIZE_STRATEGIES.map((s) => (
            <SelectItem key={s} value={s} className="text-xs">
              {s}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    );
  }

  if (action === 'scrub_text') {
    return (
      <div className="flex gap-1.5">
        <Select
          value={String(params.mode ?? 'text')}
          onValueChange={(v) => set('mode', v)}
        >
          <SelectTrigger className="h-7 w-32 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SCRUB_MODES.map((m) => (
              <SelectItem key={m} value={m} className="text-xs">
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Select
          value={String(params.patterns ?? 'all')}
          onValueChange={(v) => set('patterns', v)}
        >
          <SelectTrigger className="h-7 w-24 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {SCRUB_PATTERNS.map((p) => (
              <SelectItem key={p} value={p} className="text-xs">
                {p}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    );
  }

  if (action === 'nlp_scrub') {
    return (
      <div className="flex items-center gap-1.5">
        <Select
          value={String(params.mode ?? 'tokenize')}
          onValueChange={(v) => set('mode', v)}
        >
          <SelectTrigger className="h-7 w-28 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {NLP_MODES.map((m) => (
              <SelectItem key={m} value={m} className="text-xs">
                {m}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <span className="text-xs text-muted-foreground">threshold</span>
        <Input
          type="number"
          min={0.1}
          max={0.9}
          step={0.1}
          className="h-7 w-16 text-xs"
          value={String(params.threshold ?? 0.4)}
          onChange={(e) => set('threshold', parseFloat(e.target.value))}
        />
      </div>
    );
  }

  if (action === 'perturb') {
    return (
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-muted-foreground">\u00b1</span>
        <Input
          type="number"
          placeholder="min"
          className="h-7 w-16 text-xs"
          value={String(params.min_offset ?? '')}
          onChange={(e) =>
            e.target.value ? set('min_offset', Number(e.target.value)) : unset('min_offset')
          }
        />
        <Input
          type="number"
          placeholder="max"
          className="h-7 w-16 text-xs"
          value={String(params.max_offset ?? '')}
          onChange={(e) =>
            e.target.value ? set('max_offset', Number(e.target.value)) : unset('max_offset')
          }
        />
      </div>
    );
  }

  if (action === 'encrypt' || action === 'decrypt') {
    return (
      <Input
        placeholder="Key file path"
        className="h-7 text-xs font-mono"
        value={String(params.key_path ?? '')}
        onChange={(e) =>
          e.target.value ? set('key_path', e.target.value) : unset('key_path')
        }
      />
    );
  }

  if (action === 'substitute') {
    return (
      <Input
        placeholder="substitute_with (required)"
        className="h-7 text-xs"
        value={String(params.substitute_with ?? '')}
        onChange={(e) =>
          e.target.value ? set('substitute_with', e.target.value) : unset('substitute_with')
        }
      />
    );
  }

  // cryptohash, gpas_pseudonymize -- no params needed
  return <span className="text-xs text-muted-foreground">\u2014</span>;
}
