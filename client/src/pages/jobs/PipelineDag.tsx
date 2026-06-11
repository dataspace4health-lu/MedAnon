import { CheckCircle2, Circle, Loader2 } from 'lucide-react';
import type { JobResponse } from '@/api/jobs';

const STAGES = ['Fetch', 'NLP', 'gPAS', 'Process', 'Score', 'Upload'] as const;

function completedStages(phase: string, status: string): Set<string> {
  const done = new Set<string>();
  const p = phase?.toLowerCase() ?? '';
  if (status === 'done' || p === 'done') { STAGES.forEach((s) => done.add(s)); return done; }
  if (p === 'uploading' || p === 'loading') { ['Fetch','NLP','gPAS','Process','Score'].forEach((s) => done.add(s)); }
  else if (p === 'processing') { ['Fetch','NLP','gPAS'].forEach((s) => done.add(s)); }
  else if (p === 'fetching') { done.add('Fetch'); }
  return done;
}

function activeStage(phase: string): string | null {
  const p = phase?.toLowerCase() ?? '';
  if (p === 'fetching')              return 'Fetch';
  if (p === 'processing')            return 'NLP';
  if (p === 'loading' || p === 'uploading') return 'Upload';
  return null;
}

export function PipelineDag({ job }: { job: JobResponse }) {
  const done    = completedStages(job.phase, job.status);
  const active  = job.status === 'running' ? activeStage(job.phase) : null;

  return (
    <div className="flex items-center gap-0.5 flex-wrap">
      {STAGES.map((stage, i) => {
        const isDone   = done.has(stage);
        const isActive = stage === active;
        void (!isDone && !isActive); // pending state used only for fallback styling
        return (
          <div key={stage} className="flex items-center gap-0.5">
            <div className={`flex items-center gap-1 rounded-md px-2 py-0.5 text-[10px] font-semibold border transition-all ${
              isDone
                ? 'bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400'
                : isActive
                ? 'bg-blue-500/15 border-blue-500/30 text-blue-600 dark:text-blue-400 shadow-sm shadow-blue-500/10'
                : 'bg-transparent border-border/40 text-muted-foreground/50'
            }`}>
              {isDone   ? <CheckCircle2 className="size-2.5 shrink-0" />
               : isActive ? <Loader2 className="size-2.5 shrink-0 animate-spin" />
               : <Circle className="size-2.5 shrink-0" />}
              {stage}
            </div>
            {i < STAGES.length - 1 && (
              <div className={`w-2 h-px ${isDone ? 'bg-emerald-400/60' : 'bg-border/50'}`} />
            )}
          </div>
        );
      })}
    </div>
  );
}
