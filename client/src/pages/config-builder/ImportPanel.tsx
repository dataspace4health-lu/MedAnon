import { useCallback, useState } from 'react';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from '@/components/ui/dialog';
import { FileUploader } from '@/components/shared/FileUploader';
import { Upload, AlertCircle, FileCode2, X } from 'lucide-react';
import { toast } from 'sonner';
import { type LocalRule, parseYamlIntoRules, deduplicateIncoming } from './configConstants';

// ---------------------------------------------------------------------------
// ImportPanel -- paste YAML or upload a .yaml/.yml file to populate rule table
// ---------------------------------------------------------------------------

// A config YAML is text; 2 MB is far above any realistic profile (the largest
// bundled profile is ~30 KB) while still bounding what we read into memory.
const MAX_YAML_BYTES = 2 * 1024 * 1024;
const ACCEPTED_EXTENSIONS = ['.yaml', '.yml'];

export function ImportPanel({
  onImport,
  existingRules = [],
}: {
  onImport: (rules: LocalRule[]) => void;
  existingRules?: LocalRule[];
}) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState('');
  const [fileName, setFileName] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const reset = useCallback(() => {
    setText('');
    setFileName(null);
    setErr(null);
  }, []);

  const handleFile = useCallback((file: File, content: Uint8Array) => {
    setErr(null);
    // Config YAML is always UTF-8; `fatal` surfaces a binary file picked by
    // extension as a readable error instead of silently importing mojibake.
    try {
      setText(new TextDecoder('utf-8', { fatal: true }).decode(content));
      setFileName(file.name);
    } catch {
      setErr(`"${file.name}" is not valid UTF-8 text.`);
    }
  }, []);

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
        setErr('All rules duplicate existing match expressions. Nothing imported.');
        return;
      }
      onImport(added);
      reset();
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
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) reset();
      }}
    >
      <DialogTrigger className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground">
        <Upload className="size-3" />
        Import from YAML
      </DialogTrigger>

      <DialogContent className="w-[92vw]! max-w-2xl! sm:max-w-2xl! max-h-[88vh] grid-rows-[auto_1fr_auto] p-0 gap-0">
        <DialogHeader className="border-b p-4">
          <DialogTitle className="flex items-center gap-2 text-base">
            <FileCode2 className="size-4" />
            Import from YAML
          </DialogTitle>
          <DialogDescription className="text-xs">
            Upload a config file or paste YAML directly. Rules are added to the
            builder table; params require manual review.
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 space-y-3 overflow-y-auto p-4">
          <FileUploader
            accept={ACCEPTED_EXTENSIONS}
            maxSize={MAX_YAML_BYTES}
            onFile={handleFile}
            onError={setErr}
          />

          <div className="flex items-center gap-3">
            <span className="h-px flex-1 bg-border" />
            <span className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              or paste
            </span>
            <span className="h-px flex-1 bg-border" />
          </div>

          <div className="space-y-1.5">
            {fileName && (
              <div className="flex items-center gap-1.5 text-xs text-muted-foreground">
                <FileCode2 className="size-3 shrink-0" />
                <span className="truncate font-mono">{fileName}</span>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  className="size-5 shrink-0"
                  onClick={reset}
                  aria-label={`Clear ${fileName}`}
                >
                  <X className="size-3" />
                </Button>
              </div>
            )}
            <label htmlFor="import-yaml-text" className="sr-only">
              Config YAML
            </label>
            <Textarea
              id="import-yaml-text"
              value={text}
              onChange={(e) => {
                setText(e.target.value);
                // The buffer no longer reflects the uploaded file once edited.
                if (fileName) setFileName(null);
              }}
              placeholder="rules:&#10;  - name: redact patient name&#10;    match: Patient.name&#10;    action: redact"
              className="h-48 resize-none font-mono text-xs"
            />
          </div>

          {err && (
            <p className="flex items-start gap-1.5 text-xs text-destructive" role="alert">
              <AlertCircle className="mt-px size-3 shrink-0" />
              {err}
            </p>
          )}
        </div>

        <DialogFooter className="mx-0! mb-0! flex-row! items-center! justify-end! gap-2 rounded-b-xl border-t p-4">
          <Button variant="ghost" size="sm" className="h-7 text-xs" onClick={() => setOpen(false)}>
            Cancel
          </Button>
          <Button size="sm" className="h-7 text-xs" onClick={handleImport} disabled={!text.trim()}>
            Import Rules
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
