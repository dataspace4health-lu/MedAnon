import { useState, useCallback, useRef } from 'react';
import type { PiiLeakInfo } from '@/api/processing';

interface StreamState {
  lines: Record<string, unknown>[];
  errors: string[];
  resourceCounts: Record<string, number>;
  isStreaming: boolean;
  progress: number;
  piiLeak: PiiLeakInfo | null;
}

const initialState: StreamState = {
  lines: [],
  errors: [],
  resourceCounts: {},
  isStreaming: false,
  progress: 0,
  piiLeak: null,
};

// Throttle UI updates during streaming to avoid excessive re-renders
const UPDATE_INTERVAL_MS = 250;

export function useStreamingProcess() {
  const [state, setState] = useState<StreamState>(initialState);
  const abortRef = useRef<AbortController | null>(null);

  const start = useCallback(
    async (generator: AsyncGenerator<Record<string, unknown>>) => {
      abortRef.current = new AbortController();
      const signal = abortRef.current.signal;

      setState({ ...initialState, isStreaming: true });

      const collected: Record<string, unknown>[] = [];
      const errors: string[] = [];
      const counts: Record<string, number> = {};
      let processed = 0;
      let lastUpdate = 0;
      let piiLeakDetected: PiiLeakInfo | null = null;

      const flush = () => {
        lastUpdate = Date.now();
        setState({
          lines: collected.slice(),
          errors: errors.slice(),
          resourceCounts: { ...counts },
          isStreaming: true,
          progress: processed,
          piiLeak: null,
        });
      };

      try {
        for await (const item of generator) {
          if (signal.aborted) break;

          // Detect the stream completion trailer — extract pii_leak, skip the
          // trailer itself so it doesn't appear as a resource in the output.
          if (item.__stream_complete) {
            const trailerLeak = item.pii_leak as PiiLeakInfo | undefined;
            if (trailerLeak?.leaked) {
              piiLeakDetected = trailerLeak;
            }
            continue;
          }

          processed += 1;

          if (item.error) {
            errors.push(String(item.error));
          } else {
            collected.push(item);
            const resourceType = String(item.resourceType ?? 'Unknown');
            counts[resourceType] = (counts[resourceType] ?? 0) + 1;
          }

          // Throttle state updates — flush at most every UPDATE_INTERVAL_MS
          const now = Date.now();
          if (now - lastUpdate >= UPDATE_INTERVAL_MS) {
            flush();
          }
        }
      } catch (err) {
        if (!signal.aborted) {
          errors.push(err instanceof Error ? err.message : String(err));
        }
      } finally {
        setState({
          lines: collected.slice(),
          errors: errors.slice(),
          resourceCounts: { ...counts },
          isStreaming: false,
          progress: processed,
          piiLeak: piiLeakDetected,
        });
        abortRef.current = null;
      }
    },
    []
  );

  const abort = useCallback(() => {
    if (abortRef.current) {
      abortRef.current.abort();
    }
  }, []);

  return { ...state, start, abort };
}
