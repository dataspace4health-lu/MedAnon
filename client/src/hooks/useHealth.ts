import { useState, useEffect, useCallback, useRef } from 'react';

interface HealthState {
  ok: boolean;
  version: string;
  loading: boolean;
}

export function useHealth(intervalMs = 30000) {
  const [state, setState] = useState<HealthState>({
    ok: false,
    version: '',
    loading: true,
  });

  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const refresh = useCallback(async () => {
    try {
      const res = await fetch('/api/health');
      if (!res.ok) {
        setState((prev) => ({ ...prev, ok: false, loading: false }));
        return;
      }
      const data = await res.json();
      setState({
        ok: true,
        version: data.version ?? '',
        loading: false,
      });
    } catch {
      setState((prev) => ({ ...prev, ok: false, loading: false }));
    }
  }, []);

  useEffect(() => {
    refresh();

    timerRef.current = setInterval(refresh, intervalMs);

    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
      }
    };
  }, [refresh, intervalMs]);

  return { ...state, refresh };
}
