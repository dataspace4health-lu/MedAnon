import { NavLink, useLocation } from 'react-router-dom';
import { useState, useEffect } from 'react';
import { ChevronDown } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';
import { useConfig } from '@/context/ConfigContext';
import { listConfigs } from '@/api/medanon';
import type { ConfigMeta } from '@/api/medanon';
import { NAV_HOME, NAV_SECTIONS, type NavItem, type NavLeaf } from '@/config/navigation';
import {
  Select, SelectContent, SelectItem, SelectTrigger, SelectValue,
} from '@/components/ui/select';

// ---------------------------------------------------------------------------
// Leaf nav item
// ---------------------------------------------------------------------------

function NavRow({ item, onNavigate, indent }: { item: NavLeaf; onNavigate?: () => void; indent?: boolean }) {
  const Icon = item.icon;
  return (
    <li>
      <NavLink
        to={item.to}
        end={item.to === '/'}
        onClick={onNavigate}
        className={({ isActive }) =>
          [
            'group relative flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors',
            indent ? 'py-2' : 'py-2.5',
            isActive
              ? 'bg-sidebar-accent text-sidebar-accent-foreground'
              : 'text-sidebar-foreground/80 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground',
          ].join(' ')
        }
      >
        {({ isActive }) => (
          <>
            <span
              className={`absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-sidebar-primary transition-opacity ${
                isActive ? 'opacity-100' : 'opacity-0'
              }`}
            />
            <Icon className="size-4 shrink-0" />
            <span className="truncate">{item.label}</span>
          </>
        )}
      </NavLink>
    </li>
  );
}

// ---------------------------------------------------------------------------
// Collapsible parent item
// ---------------------------------------------------------------------------

function NavGroup({ item, onNavigate }: { item: NavItem; onNavigate?: () => void }) {
  const { hasRole } = useAuth();
  const location = useLocation();
  const children = (item.children ?? []).filter((c) => hasRole(c.minRole));

  const childActive = children.some((c) => location.pathname === c.to);
  const [open, setOpen] = useState(childActive);

  // Auto-expand when the user navigates to a child route
  useEffect(() => {
    if (childActive) setOpen(true);
  }, [childActive]);

  const Icon = item.icon;

  return (
    <li>
      <div className="flex items-center gap-1">
        <NavLink
          to={item.to}
          end
          onClick={onNavigate}
          className={({ isActive }) =>
            [
              'group relative flex flex-1 items-center gap-3 rounded-lg px-3 py-2.5 text-sm font-medium transition-colors',
              isActive || childActive
                ? 'bg-sidebar-accent text-sidebar-accent-foreground'
                : 'text-sidebar-foreground/80 hover:bg-sidebar-accent/50 hover:text-sidebar-foreground',
            ].join(' ')
          }
        >
          {({ isActive }) => (
            <>
              <span
                className={`absolute left-0 top-1.5 bottom-1.5 w-0.5 rounded-full bg-sidebar-primary transition-opacity ${
                  isActive || childActive ? 'opacity-100' : 'opacity-0'
                }`}
              />
              <Icon className="size-4.5 shrink-0" />
              <span className="truncate">{item.label}</span>
            </>
          )}
        </NavLink>

        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-label={open ? 'Collapse' : 'Expand'}
          className="mr-1 flex items-center justify-center rounded p-1 text-muted-foreground/50 hover:text-sidebar-foreground transition-colors"
        >
          <ChevronDown
            className={`size-3.5 transition-transform duration-200 ${open ? '' : '-rotate-90'}`}
          />
        </button>
      </div>

      {open && children.length > 0 && (
        <ul className="mt-0.5 ml-7 space-y-0.5 border-l border-sidebar-border/40 pl-2.5">
          {children.map((child) => (
            <NavRow key={child.to} item={child} onNavigate={onNavigate} indent />
          ))}
        </ul>
      )}
    </li>
  );
}

// ---------------------------------------------------------------------------
// Sidebar
// ---------------------------------------------------------------------------

export function Sidebar({ onNavigate }: { onNavigate?: () => void }) {
  const { hasRole } = useAuth();
  const { configProfile, setConfigProfile } = useConfig();
  const [profiles, setProfiles] = useState<ConfigMeta[]>([]);

  useEffect(() => {
    listConfigs().then(setProfiles).catch(() => {});
  }, []);

  return (
    <div className="flex h-full flex-col">
      {/* Navigation */}
      <nav className="flex-1 overflow-y-auto scrollbar-hidden px-3 py-4">
        {/* Dashboard (top-level) */}
        {hasRole(NAV_HOME.minRole) && (
          <ul className="mb-5">
            <NavRow item={NAV_HOME} onNavigate={onNavigate} />
          </ul>
        )}

        {/* Grouped sections */}
        {NAV_SECTIONS.map((section) => {
          const visible = section.items.filter((i) => hasRole(i.minRole));
          if (!visible.length) return null;
          return (
            <div key={section.label} className="mb-6">
              <p className="mb-2 px-3 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground/60">
                {section.label}
              </p>
              <ul className="space-y-1">
                {visible.map((item) =>
                  item.children?.length
                    ? <NavGroup key={item.to} item={item} onNavigate={onNavigate} />
                    : <NavRow key={item.to} item={item} onNavigate={onNavigate} />
                )}
              </ul>
            </div>
          );
        })}
      </nav>

      {/* Config profile selector, pinned to the bottom */}
      <div className="border-t border-sidebar-border px-4 py-4">
        <label className="mb-2 block text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground/60">
          Active Profile
        </label>
        <Select value={configProfile} onValueChange={(v) => setConfigProfile(v ?? 'auto')}>
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
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground/70">
          Applied to all de-identification across formats.
        </p>
      </div>
    </div>
  );
}
