import { useState, useCallback, useRef } from 'react';

interface StreamState {
  lines: Record<string, unknown>[];
  errors: string[];
  resourceCounts: Record<string, number>;
  isStreaming: boolean;
  progress: number;
}

const initialState: StreamState = {
  lines: [],
  errors: [],
  resourceCounts: {},
  isStreaming: false,
  progress: 0,
};

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

      try {
        for await (const item of generator) {
          if (signal.aborted) break;

          processed += 1;

          if (item.error) {
            errors.push(String(item.error));
          } else {
            collected.push(item);
            const resourceType = String(item.resourceType ?? 'Unknown');
            counts[resourceType] = (counts[resourceType] ?? 0) + 1;
          }

          setState({
            lines: [...collected],
            errors: [...errors],
            resourceCounts: { ...counts },
            isStreaming: true,
            progress: processed,
          });
        }
      } catch (err) {
        if (!signal.aborted) {
          errors.push(err instanceof Error ? err.message : String(err));
        }
      } finally {
        setState({
          lines: [...collected],
          errors: [...errors],
          resourceCounts: { ...counts },
          isStreaming: false,
          progress: processed,
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
