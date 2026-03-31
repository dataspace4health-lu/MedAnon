import { lazy, Suspense } from 'react';
import { Routes, Route } from 'react-router-dom';
import { AppLayout } from '@/components/layout/AppLayout';

const HomePage = lazy(() => import('@/pages/HomePage'));
const PatientBrowserPage = lazy(() => import('@/pages/PatientBrowserPage'));
const ConditionBrowserPage = lazy(() => import('@/pages/ConditionBrowserPage'));
const DeidentifyPage = lazy(() => import('@/pages/DeidentifyPage'));
const ProcessResourcePage = lazy(() => import('@/pages/ProcessResourcePage'));
const BatchPage = lazy(() => import('@/pages/BatchPage'));
const RiskAssessmentPage = lazy(() => import('@/pages/RiskAssessmentPage'));
const SyntheticDataPage = lazy(() => import('@/pages/SyntheticDataPage'));
const StatusPage = lazy(() => import('@/pages/StatusPage'));

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
          <Route path="batch" element={<BatchPage />} />
          <Route path="risk" element={<RiskAssessmentPage />} />
          <Route path="synthetic" element={<SyntheticDataPage />} />
          <Route path="status" element={<StatusPage />} />
        </Route>
      </Routes>
    </Suspense>
  );
}
