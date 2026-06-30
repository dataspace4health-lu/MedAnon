import { useQuery } from '@tanstack/react-query';
import { listProcessingRuns, getProcessingRunStats } from '@/api/processingRuns';

export function useProcessingRuns(params?: {
  endpoint?: string;
  config_profile?: string;
  limit?: number;
}) {
  return useQuery({
    queryKey: ['processing-runs', params],
    queryFn: () => listProcessingRuns({ limit: 200, ...params }),
    staleTime: 30_000,
  });
}

export function useProcessingRunStats(params?: { config_profile?: string }) {
  return useQuery({
    queryKey: ['processing-runs', 'stats', params],
    queryFn: () => getProcessingRunStats(params),
    refetchInterval: 15_000,
  });
}
