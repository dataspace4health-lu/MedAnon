/**
 * Single source of truth for the app's navigation.
 *
 * Previously the nav structure was duplicated across three files, Sidebar
 * (NAV_SECTIONS), TopBar (EXACT/PREFIX crumb maps), and HomePage (SECTIONS).
 * They drifted (e.g. "Multi-Format" was added to the sidebar but not the
 * breadcrumb map).  This module is now the one place that defines every
 * navigable destination: its route, label, icon, required role, the section it
 * belongs to, and a one-line description used on the dashboard tool cards.
 *
 * Sidebar, TopBar, and HomePage all derive their content from here.
 */

import {
  Home, Users, FileText, Layers, Activity, Shield,
  FlaskConical, BarChart3, SlidersHorizontal, PackageOpen,
  ShieldCheck, History, MonitorDot, ServerCog, TrendingUp,
  FileBarChart, FileStack, Database, BadgeCheck, Settings,
  ScrollText,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import type { Role } from '@/config/constants';

/** A leaf nav destination (no further nesting). */
export interface NavLeaf {
  to: string;
  label: string;
  description: string;
  icon: LucideIcon;
  minRole: Role;
}

export interface NavItem extends NavLeaf {
  /** Optional child items, renders as a collapsible group in the sidebar. */
  children?: NavLeaf[];
}

export interface NavSection {
  /** Section heading (sidebar group + dashboard group + breadcrumb context). */
  label: string;
  items: NavItem[];
}

/** The Home/Dashboard entry, rendered above the grouped sections. */
export const NAV_HOME: NavItem = {
  to: '/',
  label: 'Dashboard',
  description: 'Overview of activity, service health, and quick actions.',
  icon: Home,
  minRole: 'viewer',
};

export const NAV_SECTIONS: NavSection[] = [
  {
    label: 'Browse',
    items: [
      { to: '/patients',      label: 'Patient Browser',     description: 'Search patients and run $everything de-identification.', icon: Users,      minRole: 'viewer' },
      { to: '/conditions',    label: 'Condition Browser',   description: 'Find conditions and de-identify linked patient records.', icon: FileText,   minRole: 'viewer' },
      { to: '/target-browser',label: 'Target FHIR Browser', description: 'Browse de-identified data in the target FHIR server.',    icon: ShieldCheck, minRole: 'viewer' },
    ],
  },
  {
    label: 'Process',
    items: [
      { to: '/process',    label: 'Process Resource', description: 'Paste FHIR JSON/NDJSON/XML and get de-identified output instantly.',            icon: Layers,    minRole: 'analyst' },
      { to: '/formats',    label: 'Multi-Format',     description: 'De-identify HL7 v2, CDA, and DICOM through the same engine.',                   icon: FileStack, minRole: 'analyst' },
      { to: '/sql-source', label: 'SQL Database',     description: 'Connect a PostgreSQL DB (read-only), explore tables, and export de-identified files.', icon: Database, minRole: 'analyst' },
      {
        to: '/bulk-deidentify',
        label: 'Async Jobs',
        description: 'Batch and bulk async processing, submit, monitor, and manage server-side jobs.',
        icon: PackageOpen,
        minRole: 'analyst',
        children: [
          { to: '/batch',           label: 'Batch Processing', description: 'Upload a FHIR bundle or NDJSON file and stream-process in bulk.',        icon: Activity,    minRole: 'analyst' },
          { to: '/bulk-deidentify', label: 'Bulk Jobs',        description: 'Submit and monitor async bulk export/import jobs.',                       icon: PackageOpen, minRole: 'analyst' },
          { to: '/jobs',            label: 'Jobs Monitor',     description: 'Live fleet view of all server-side jobs with dead-letter management.',    icon: ServerCog,   minRole: 'analyst' },
        ],
      },
    ],
  },
  {
    label: 'Analyse',
    items: [
      { to: '/risk',      label: 'Risk Assessment',    description: 'Measure re-identification risk using k-anonymity and l-diversity.',             icon: Shield,      minRole: 'analyst' },
      { to: '/synthetic', label: 'Synthetic Data',     description: 'Generate realistic synthetic FHIR patients from real distributions.',           icon: FlaskConical, minRole: 'analyst' },
      { to: '/analytics', label: 'Analytics Dashboard',description: 'Privacy / utility / quality scoring trends and run analytics.',                 icon: TrendingUp,  minRole: 'analyst' },
      {
        to: '/trust-gate',
        label: 'Trust Gate',
        description: 'Assess incoming FHIR data quality and get a Quality Passport before privacy processing.',
        icon: BadgeCheck,
        minRole: 'analyst',
        children: [
          { to: '/trust-gate',     label: 'Assess Data',    description: 'Run a phased data-quality assessment and produce a Quality Passport.',       icon: BadgeCheck,      minRole: 'analyst' },
          { to: '/trust-history',  label: 'QC History',     description: 'Track a dataset\'s quality across runs and triage remediation findings.',     icon: History,         minRole: 'analyst' },
          { to: '/trust-profiles', label: 'Trust Profiles', description: 'Build and manage reusable audit profiles (phases, sectors, thresholds).',   icon: SlidersHorizontal, minRole: 'analyst' },
        ],
      },
    ],
  },
  {
    label: 'Govern',
    items: [
      { to: '/permits', label: 'Data Permits', description: 'Create and approve data permits (EHDS/D7.2) that scope pseudonymisation to an authorised use.', icon: ScrollText, minRole: 'admin' },
      { to: '/reports', label: 'Passports', description: 'Durable transformation passports (privacy model, risk, disclosure) for every risk-driven export.', icon: FileBarChart, minRole: 'analyst' },
    ],
  },
  {
    label: 'Configure',
    items: [
      { to: '/configs',  label: 'Rule Configs', description: 'Create, edit, and manage de-identification rule profiles.', icon: SlidersHorizontal, minRole: 'viewer' },
      { to: '/settings', label: 'Settings',     description: 'Define FHIR servers, processing defaults, appearance, and view backend configuration.', icon: Settings, minRole: 'viewer' },
    ],
  },
  {
    label: 'Monitor',
    items: [
      { to: '/status',     label: 'Status Dashboard',    description: 'Monitor live health and connectivity of all services.',              icon: BarChart3,  minRole: 'viewer'  },
      { to: '/history',    label: 'Processing History',  description: 'Browse and filter historical de-identification runs.',               icon: History,    minRole: 'analyst' },
      { to: '/audit',      label: 'Audit Report',        description: 'Per-run privacy gate, score breakdown, and downloadable audit trail.', icon: FileBarChart, minRole: 'analyst' },
      { to: '/monitoring', label: 'Monitoring',          description: 'Prometheus metrics and Grafana observability.',                      icon: MonitorDot, minRole: 'viewer'  },
    ],
  },
];

// ---------------------------------------------------------------------------
// Lookup helpers (used by TopBar for breadcrumbs, etc.)
// ---------------------------------------------------------------------------

interface ResolvedCrumb {
  section?: string;
  label: string;
}

/** Exact route → {section, label} index, derived from NAV_SECTIONS + Home. */
const _EXACT: Record<string, ResolvedCrumb> = (() => {
  const map: Record<string, ResolvedCrumb> = { '/': { label: NAV_HOME.label } };
  for (const section of NAV_SECTIONS) {
    for (const item of section.items) {
      map[item.to] = { section: section.label, label: item.label };
      for (const child of item.children ?? []) {
        map[child.to] = { section: section.label, label: child.label };
      }
    }
  }
  return map;
})();

/** Prefix routes that don't have an exact nav entry (detail / edit pages). */
const _PREFIX: { prefix: string; crumb: ResolvedCrumb }[] = [
  { prefix: '/configs/', crumb: { section: 'Configure', label: 'Edit Config' } },
  { prefix: '/deidentify/', crumb: { section: 'Browse', label: 'De-identify Patient' } },
];

/** Resolve a pathname to a breadcrumb (section + label). */
export function resolveCrumb(pathname: string): ResolvedCrumb {
  if (_EXACT[pathname]) return _EXACT[pathname];
  for (const { prefix, crumb } of _PREFIX) {
    if (pathname.startsWith(prefix)) return crumb;
  }
  return { label: 'Data Privacy Toolkit' };
}
