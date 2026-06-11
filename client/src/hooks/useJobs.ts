import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { listJobs, getJobStatus, cancelJob, reprocessJob } from '@/api/jobs';
import { listDeadJobs, requeueJob } from '@/api/jobsExtra';

export function useJobs(params?: { status?: string; type?: string; limit?: number }) {
  return useQuery({
    queryKey: ['jobs', params],
    queryFn: () => listJobs({ limit: 100, ...params }),
    refetchInterval: 3_000,
  });
}

export function useJobDetail(jobId: string | null) {
  return useQuery({
    queryKey: ['jobs', jobId],
    queryFn: () => getJobStatus(jobId!),
    enabled: !!jobId,
    refetchInterval: 3_000,
  });
}

export function useDeadJobs() {
  return useQuery({
    queryKey: ['jobs', 'dead'],
    queryFn: listDeadJobs,
    refetchInterval: 10_000,
  });
}

export function useJobMutations() {
  const qc = useQueryClient();
  const invalidate = () => qc.invalidateQueries({ queryKey: ['jobs'] });

  const cancel = useMutation({ mutationFn: cancelJob, onSuccess: invalidate });
  const reprocess = useMutation({
    mutationFn: ({ id, profile }: { id: string; profile?: string }) =>
      reprocessJob(id, profile),
    onSuccess: invalidate,
  });
  const requeue = useMutation({ mutationFn: requeueJob, onSuccess: invalidate });

  return { cancel, reprocess, requeue };
}
