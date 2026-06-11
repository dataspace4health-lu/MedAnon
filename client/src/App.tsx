import { lazy, Suspense } from 'react';
import { Routes, Route } from 'react-router-dom';
import { AppLayout } from '@/components/layout/AppLayout';

const HomePage = lazy(() => import('@/pages/HomePage'));
const PatientBrowserPage = lazy(() => import('@/pages/PatientBrowserPage'));
const ConditionBrowserPage = lazy(() => import('@/pages/ConditionBrowserPage'));
const DeidentifyPage = lazy(() => import('@/pages/DeidentifyPage'));
const ProcessResourcePage = lazy(() => import('@/pages/ProcessResourcePage'));
const FormatProcessPage = lazy(() => import('@/pages/FormatProcessPage'));
const SqlSourcePage = lazy(() => import('@/pages/SqlSourcePage'));
const BatchPage = lazy(() => import('@/pages/BatchPage'));
const RiskAssessmentPage = lazy(() => import('@/pages/RiskAssessmentPage'));
const SyntheticDataPage = lazy(() => import('@/pages/SyntheticDataPage'));
const StatusPage = lazy(() => import('@/pages/StatusPage'));
const ConfigsPage = lazy(() => import('@/pages/ConfigsPage'));
const ConfigBuilderPage = lazy(() => import('@/pages/ConfigBuilderPage'));
const BulkDeidentifyPage = lazy(() => import('@/pages/BulkDeidentifyPage'));
const TargetBrowserPage = lazy(() => import('@/pages/TargetBrowserPage'));
const ProcessingHistoryPage = lazy(() => import('@/pages/ProcessingHistoryPage'));
const MonitoringPage = lazy(() => import('@/pages/MonitoringPage'));
const JobsPage = lazy(() => import('@/pages/JobsPage'));
const AnalyticsPage = lazy(() => import('@/pages/AnalyticsPage'));
const AuditPage = lazy(() => import('@/pages/AuditPage'));

function PageLoader() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
  );
}

export default function App() {
  return (
    <Suspense fallback={<PageLoader />}>
      <Routes>
        <Route element={<AppLayout />}>
          <Route index element={<HomePage />} />
          <Route path="patients" element={<PatientBrowserPage />} />
          <Route path="conditions" element={<ConditionBrowserPage />} />
          <Route path="deidentify/:patientId" element={<DeidentifyPage />} />
          <Route path="process" element={<ProcessResourcePage />} />
          <Route path="formats" element={<FormatProcessPage />} />
          <Route path="sql-source" element={<SqlSourcePage />} />
          <Route path="batch" element={<BatchPage />} />
          <Route path="risk" element={<RiskAssessmentPage />} />
          <Route path="synthetic" element={<SyntheticDataPage />} />
          <Route path="status" element={<StatusPage />} />
          <Route path="configs" element={<ConfigsPage />} />
          <Route path="configs/new" element={<ConfigBuilderPage />} />
          <Route path="configs/:name/edit" element={<ConfigBuilderPage />} />
          <Route path="bulk-deidentify" element={<BulkDeidentifyPage />} />
          <Route path="target-browser" element={<TargetBrowserPage />} />
          <Route path="history" element={<ProcessingHistoryPage />} />
          <Route path="monitoring" element={<MonitoringPage />} />
          <Route path="jobs" element={<JobsPage />} />
          <Route path="analytics" element={<AnalyticsPage />} />
          <Route path="audit" element={<AuditPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
