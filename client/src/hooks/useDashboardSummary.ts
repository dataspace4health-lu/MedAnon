import { useQuery } from '@tanstack/react-query';
import { getDashboardSummary } from '@/api/dashboard';

export function useDashboardSummary(limit = 10) {
  return useQuery({
    queryKey: ['dashboard', limit],
    queryFn: ({ signal }) => getDashboardSummary(limit, signal),
    refetchInterval: 5_000,
  });
}
