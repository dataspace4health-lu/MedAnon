import React, { lazy, Suspense } from 'react';
import { Routes, Route } from 'react-router-dom';
import { AppLayout } from '@/components/layout/AppLayout';
import { AuthGate } from '@/components/auth/AuthGate';
import { RoleGate } from '@/components/auth/RoleGate';
import type { Role } from '@/config/constants';

// ── Auth routes ────────────────────────────────────────────────────────────
const LoginPage       = lazy(() => import('@/pages/LoginPage'));
const CallbackPage    = lazy(() => import('@/pages/auth/CallbackPage'));

// ── App pages ──────────────────────────────────────────────────────────────
const HomePage             = lazy(() => import('@/pages/HomePage'));
const PatientBrowserPage   = lazy(() => import('@/pages/PatientBrowserPage'));
const ConditionBrowserPage = lazy(() => import('@/pages/ConditionBrowserPage'));
const DeidentifyPage       = lazy(() => import('@/pages/DeidentifyPage'));
const ProcessResourcePage  = lazy(() => import('@/pages/ProcessResourcePage'));
const FormatProcessPage    = lazy(() => import('@/pages/FormatProcessPage'));
const SqlSourcePage        = lazy(() => import('@/pages/SqlSourcePage'));
const BatchPage            = lazy(() => import('@/pages/BatchPage'));
const RiskAssessmentPage   = lazy(() => import('@/pages/RiskAssessmentPage'));
const SyntheticDataPage    = lazy(() => import('@/pages/SyntheticDataPage'));
const StatusPage           = lazy(() => import('@/pages/StatusPage'));
const ConfigsPage          = lazy(() => import('@/pages/ConfigsPage'));
const ConfigBuilderPage    = lazy(() => import('@/pages/ConfigBuilderPage'));
const BulkDeidentifyPage   = lazy(() => import('@/pages/BulkDeidentifyPage'));
const TargetBrowserPage    = lazy(() => import('@/pages/TargetBrowserPage'));
const ProcessingHistoryPage= lazy(() => import('@/pages/ProcessingHistoryPage'));
const MonitoringPage       = lazy(() => import('@/pages/MonitoringPage'));
const JobsPage             = lazy(() => import('@/pages/JobsPage'));
const AnalyticsPage        = lazy(() => import('@/pages/AnalyticsPage'));
const AuditPage            = lazy(() => import('@/pages/AuditPage'));
const TrustGatePage        = lazy(() => import('@/pages/TrustGatePage'));
const TrustProfilesPage    = lazy(() => import('@/pages/TrustProfilesPage'));
const TrustHistoryPage     = lazy(() => import('@/pages/TrustHistoryPage'));
const SettingsPage         = lazy(() => import('@/pages/SettingsPage'));

function PageLoader() {
  return (
    <div className="flex items-center justify-center h-64">
      <div className="h-8 w-8 animate-spin rounded-full border-4 border-primary border-t-transparent" />
    </div>
  );
}

function Guarded({ role, children }: { role: Role; children: React.ReactNode }) {
  return <RoleGate minRole={role}>{children}</RoleGate>;
}

export default function App() {
  return (
    <Suspense fallback={<PageLoader />}>
      <Routes>
        {/*
         * Public / auth routes — rendered outside AuthGate and outside AppLayout.
         * /login      — branded sign-in page (OIDC redirect button or API-key form)
         * /auth/callback — PKCE code exchange after Keycloak redirects back
         */}
        <Route path="login" element={<LoginPage />} />
        <Route path="auth/callback" element={<CallbackPage />} />

        {/* All other routes require authentication. */}
        <Route element={<AuthGate />}>
          <Route element={<AppLayout />}>
            {/* viewer */}
            <Route index element={<Guarded role="viewer"><HomePage /></Guarded>} />
            <Route path="patients"      element={<Guarded role="viewer"><PatientBrowserPage /></Guarded>} />
            <Route path="conditions"    element={<Guarded role="viewer"><ConditionBrowserPage /></Guarded>} />
            <Route path="target-browser" element={<Guarded role="viewer"><TargetBrowserPage /></Guarded>} />
            <Route path="configs"       element={<Guarded role="viewer"><ConfigsPage /></Guarded>} />
            <Route path="status"        element={<Guarded role="viewer"><StatusPage /></Guarded>} />
            <Route path="settings"      element={<Guarded role="viewer"><SettingsPage /></Guarded>} />
            <Route path="monitoring"    element={<Guarded role="viewer"><MonitoringPage /></Guarded>} />

            {/* analyst */}
            <Route path="deidentify/:patientId" element={<Guarded role="analyst"><DeidentifyPage /></Guarded>} />
            <Route path="process"       element={<Guarded role="analyst"><ProcessResourcePage /></Guarded>} />
            <Route path="formats"       element={<Guarded role="analyst"><FormatProcessPage /></Guarded>} />
            <Route path="sql-source"    element={<Guarded role="analyst"><SqlSourcePage /></Guarded>} />
            <Route path="batch"         element={<Guarded role="analyst"><BatchPage /></Guarded>} />
            <Route path="bulk-deidentify" element={<Guarded role="analyst"><BulkDeidentifyPage /></Guarded>} />
            <Route path="jobs"          element={<Guarded role="analyst"><JobsPage /></Guarded>} />
            <Route path="risk"          element={<Guarded role="analyst"><RiskAssessmentPage /></Guarded>} />
            <Route path="synthetic"     element={<Guarded role="analyst"><SyntheticDataPage /></Guarded>} />
            <Route path="analytics"     element={<Guarded role="analyst"><AnalyticsPage /></Guarded>} />
            <Route path="trust-gate"    element={<Guarded role="analyst"><TrustGatePage /></Guarded>} />
            <Route path="trust-profiles" element={<Guarded role="analyst"><TrustProfilesPage /></Guarded>} />
            <Route path="trust-history" element={<Guarded role="analyst"><TrustHistoryPage /></Guarded>} />
            <Route path="history"       element={<Guarded role="analyst"><ProcessingHistoryPage /></Guarded>} />
            <Route path="audit"         element={<Guarded role="analyst"><AuditPage /></Guarded>} />

            {/* admin */}
            <Route path="configs/new"        element={<Guarded role="admin"><ConfigBuilderPage /></Guarded>} />
            <Route path="configs/:name/edit" element={<Guarded role="admin"><ConfigBuilderPage /></Guarded>} />
          </Route>
        </Route>
      </Routes>
    </Suspense>
  );
}
