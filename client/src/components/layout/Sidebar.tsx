import { NavLink } from 'react-router-dom';
import { useState, useEffect } from 'react';
import {
  Home,
  Users,
  FileText,
  Layers,
  Activity,
  Shield,
  FlaskConical,
  BarChart3,
  SlidersHorizontal,
  PackageOpen,
  ShieldCheck,
  History,
} from 'lucide-react';
import { HealthBadge } from '@/components/shared/HealthBadge';
import { ThemeToggle } from '@/components/shared/ThemeToggle';
import { useHealth } from '@/hooks/useHealth';
import { useAuth } from '@/context/AuthContext';
import { useConfig } from '@/context/ConfigContext';
import { listConfigs } from '@/api/medanon';
import type { ConfigMeta } from '@/api/medanon';
import type { Role } from '@/config/constants';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

interface NavEntry {
  to: string;
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  minRole: Role;
}

interface NavSection {
  label: string;
  items: NavEntry[];
}

const NAV_TOP: NavEntry[] = [
  { to: '/', label: 'Home', icon: Home, minRole: 'viewer' },
];

const NAV_SECTIONS: NavSection[] = [
  {
    label: 'Browse',
    items: [
      { to: '/patients', label: 'Patient Browser', icon: Users, minRole: 'viewer' },
      { to: '/conditions', label: 'Condition Browser', icon: FileText, minRole: 'viewer' },
      { to: '/target-browser', label: 'Target FHIR Browser', icon: ShieldCheck, minRole: 'viewer' },
    ],
  },
  {
    label: 'Process',
    items: [
      { to: '/process', label: 'Process Resource', icon: Layers, minRole: 'analyst' },
      { to: '/batch', label: 'Batch Processing', icon: Activity, minRole: 'analyst' },
      { to: '/bulk-deidentify', label: 'Bulk Jobs', icon: PackageOpen, minRole: 'analyst' },
    ],
  },
  {
    label: 'Analyse',
    items: [
      { to: '/risk', label: 'Risk Assessment', icon: Shield, minRole: 'analyst' },
      { to: '/synthetic', label: 'Synthetic Data', icon: FlaskConical, minRole: 'analyst' },
    ],
  },
  {
    label: 'Configure',
    items: [
      { to: '/configs', label: 'Rule Configs', icon: SlidersHorizontal, minRole: 'viewer' },
    ],
  },
  {
    label: 'Monitor',
    items: [
      { to: '/status', label: 'Status Dashboard', icon: BarChart3, minRole: 'viewer' },
      { to: '/history', label: 'Processing History', icon: History, minRole: 'analyst' },
    ],
  },
];

interface SidebarProps {
  onNavigate?: () => void;
}

function NavItem({
  entry,
  onNavigate,
}: {
  entry: NavEntry;
  onNavigate?: () => void;
}) {
  const Icon = entry.icon;
  return (
    <li>
      <NavLink
        to={entry.to}
        end={entry.to === '/'}
        onClick={onNavigate}
        className={({ isActive }) =>
          `flex items-center gap-2.5 rounded-md px-3 py-2 text-sm font-medium transition-colors ${
            isActive
              ? 'bg-sidebar-accent text-sidebar-accent-foreground'
              : 'text-sidebar-foreground hover:bg-sidebar-accent/50'
          }`
        }
      >
        <Icon className="size-4 shrink-0" />
        {entry.label}
      </NavLink>
    </li>
  );
}

export function Sidebar({ onNavigate }: SidebarProps) {
  const { hasRole } = useAuth();
  const { configProfile, setConfigProfile } = useConfig();
  const health = useHealth();
  const [profiles, setProfiles] = useState<ConfigMeta[]>([]);

  useEffect(() => {
    listConfigs()
      .then(setProfiles)
      .catch(() => {/* fallback: dropdown shows only "Auto" */});
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* Header */}
      <div className="flex items-center gap-2.5 border-b px-4 py-3.5">
        <div className="flex flex-col leading-tight">
          <span className="text-base font-bold tracking-tight">MedAnon</span>
          <span className="text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
            FHIR Privacy Toolkit
          </span>
        </div>
        <HealthBadge ok={health.ok} version={health.version} loading={health.loading} />
        <ThemeToggle className="ml-auto" />
      </div>

      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto px-2 py-3">
        {/* Top-level items (Home) */}
        <ul className="mb-3 space-y-0.5">
          {NAV_TOP.filter((item) => hasRole(item.minRole)).map((entry) => (
            <NavItem key={entry.to} entry={entry} onNavigate={onNavigate} />
          ))}
        </ul>

        {/* Grouped sections */}
        {NAV_SECTIONS.map((section) => {
          const visible = section.items.filter((item) => hasRole(item.minRole));
          if (visible.length === 0) return null;
          return (
            <div key={section.label} className="mb-4">
              <p className="mb-1 px-3 text-[10px] font-semibold uppercase tracking-widest text-muted-foreground/70">
                {section.label}
              </p>
              <ul className="space-y-0.5">
                {visible.map((entry) => (
                  <NavItem key={entry.to} entry={entry} onNavigate={onNavigate} />
                ))}
              </ul>
            </div>
          );
        })}
      </nav>

      {/* Config profile selector */}
      <div className="border-t px-4 py-3">
        <label className="mb-1.5 block text-xs font-medium text-muted-foreground">
          Config Profile
        </label>
        <Select
          value={configProfile}
          onValueChange={(val) => setConfigProfile(val ?? 'auto')}
        >
          <SelectTrigger className="w-full">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="auto">Auto</SelectItem>
            {profiles.map((p) => (
              <SelectItem key={p.name} value={p.name}>
                {p.name}{!p.is_system && ' *'}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
