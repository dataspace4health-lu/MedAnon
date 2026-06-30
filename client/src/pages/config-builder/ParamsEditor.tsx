import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
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
  MASK_STRATEGIES,
  SCRUB_MODES,
  SCRUB_PATTERNS,
  NLP_MODES,
  SUBSTITUTE_DEFAULT,
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

  if (action === 'mask') {
    return (
      <div className="flex items-center gap-1.5">
        <Select
          value={String(params.strategy ?? 'keep_prefix')}
          onValueChange={(v) => set('strategy', v)}
        >
          <SelectTrigger className="h-7 w-36 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {MASK_STRATEGIES.map((s) => (
              <SelectItem key={s} value={s} className="text-xs">
                {s}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
        <Input
          type="number"
          placeholder="keep"
          title="keep_chars"
          className="h-7 w-16 text-xs"
          value={String(params.keep_chars ?? '')}
          onChange={(e) =>
            e.target.value ? set('keep_chars', Number(e.target.value)) : unset('keep_chars')
          }
        />
      </div>
    );
  }

  if (action === 'date_shift') {
    return (
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-muted-foreground">±days</span>
        <Input
          type="number"
          placeholder="max_days"
          className="h-7 w-20 text-xs"
          value={String(params.max_days ?? '')}
          onChange={(e) =>
            e.target.value ? set('max_days', Number(e.target.value)) : unset('max_days')
          }
        />
        <Select
          value={String(params.direction ?? 'both')}
          onValueChange={(v) => set('direction', v)}
        >
          <SelectTrigger className="h-7 w-20 text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {['both', 'past', 'future'].map((d) => (
              <SelectItem key={d} value={d} className="text-xs">
                {d}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    );
  }

  if (action === 'tokenize') {
    return (
      <div className="flex items-center gap-1.5">
        <Input
          placeholder="namespace"
          className="h-7 w-24 text-xs"
          value={String(params.namespace ?? '')}
          onChange={(e) =>
            e.target.value ? set('namespace', e.target.value) : unset('namespace')
          }
        />
        <Input
          placeholder="format (opt)"
          className="h-7 w-28 text-xs font-mono"
          title="Format pattern — e.g. PAT-######"
          value={String(params.format ?? '')}
          onChange={(e) =>
            e.target.value ? set('format', e.target.value) : unset('format')
          }
        />
      </div>
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

  if (action === 'nlp_scrub' || action === 'nlp_detect_act') {
    const base64On = params.base64_encoded === true;
    return (
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1.5">
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
        <label
          className="flex cursor-pointer items-center gap-1.5 text-xs text-muted-foreground"
          title="Decode a Base64-encoded attachment payload (e.g. Attachment.data) before scrubbing, then re-encode on write-back. Enable for content.attachment.data / *Base64Binary fields."
        >
          <Checkbox
            checked={base64On}
            onCheckedChange={(c) => (c ? set('base64_encoded', true) : unset('base64_encoded'))}
            className="size-3.5"
          />
          Base64
        </label>
      </div>
    );
  }

  if (action === 'perturb') {
    return (
      <div className="flex items-center gap-1.5">
        <span className="text-xs text-muted-foreground">±</span>
        <Input
          type="number"
          placeholder="min"
          className="h-7 w-16 text-xs"
          value={String(params.min ?? '')}
          onChange={(e) =>
            e.target.value ? set('min', Number(e.target.value)) : unset('min')
          }
        />
        <Input
          type="number"
          placeholder="max"
          className="h-7 w-16 text-xs"
          value={String(params.max ?? '')}
          onChange={(e) =>
            e.target.value ? set('max', Number(e.target.value)) : unset('max')
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
        placeholder={`${SUBSTITUTE_DEFAULT} (default)`}
        className="h-7 text-xs"
        value={String(params.substitute_with ?? '')}
        onChange={(e) =>
          e.target.value ? set('substitute_with', e.target.value) : unset('substitute_with')
        }
      />
    );
  }

  // cryptohash, gpas_pseudonymize — no UI-configurable params
  return <span className="text-xs text-muted-foreground">—</span>;
}
