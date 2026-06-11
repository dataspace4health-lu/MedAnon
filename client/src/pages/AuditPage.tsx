import { PageHeader } from '@/components/layout/PageHeader';
import { AuditReport } from './analytics/AuditReport';

export default function AuditPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Audit Report"
        description="Per-run de-identification audit trail with privacy gate status and score breakdown."
      />
      <AuditReport />
    </div>
  );
}
