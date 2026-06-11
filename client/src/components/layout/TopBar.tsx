import { useLocation, Link } from 'react-router-dom';
import { Menu, X, ChevronRight } from 'lucide-react';
import { HealthBadge } from '@/components/shared/HealthBadge';
import { ThemeToggle } from '@/components/shared/ThemeToggle';
import { useHealth } from '@/hooks/useHealth';
import { resolveCrumb } from '@/config/navigation';
import ds4hLogo from '@/assets/ds4h-logo.png';

interface TopBarProps {
  sidebarOpen: boolean;
  onToggleSidebar: () => void;
}

export function TopBar({ sidebarOpen, onToggleSidebar }: TopBarProps) {
  const { pathname } = useLocation();
  const crumb = resolveCrumb(pathname);
  const health = useHealth();

  return (
    <header className="flex h-16 shrink-0 items-center gap-4 border-b border-border bg-background/95 px-4 backdrop-blur-sm sm:px-6 z-40">
      {/* Hamburger — mobile only */}
      <button
        onClick={onToggleSidebar}
        className="flex size-9 shrink-0 items-center justify-center rounded-lg text-muted-foreground hover:bg-accent hover:text-foreground md:hidden"
        aria-label={sidebarOpen ? 'Close menu' : 'Open menu'}
      >
        {sidebarOpen ? <X className="size-5" /> : <Menu className="size-5" />}
      </button>

      {/* Brand — links home */}
      <Link to="/" className="flex shrink-0 items-center gap-2.5">
        <span className="flex size-8 items-center justify-center rounded-lg bg-primary text-primary-foreground font-bold text-sm">
          DP
        </span>
        <span className="hidden text-base font-semibold tracking-tight text-foreground sm:inline">
          Data&nbsp;Privacy&nbsp;Toolkit
        </span>
      </Link>

      <div className="hidden h-6 w-px bg-border sm:block" />

      {/* Breadcrumb */}
      <div className="flex min-w-0 flex-1 items-center gap-2">
        {crumb.section && (
          <>
            <span className="hidden truncate text-sm text-muted-foreground sm:inline">
              {crumb.section}
            </span>
            <ChevronRight className="hidden size-4 shrink-0 text-muted-foreground/40 sm:inline" />
          </>
        )}
        <span className="truncate text-sm font-semibold text-foreground">{crumb.label}</span>
      </div>

      {/* Right: health + theme + partner logo */}
      <div className="flex shrink-0 items-center gap-3">
        <HealthBadge ok={health.ok} version={health.version} loading={health.loading} />
        <ThemeToggle />
        <div className="hidden h-8 w-px bg-border md:block" />
        <img
          src={ds4hLogo}
          alt="Dataspace4Health"
          className="hidden h-9 w-auto max-w-[180px] object-contain md:block"
        />
      </div>
    </header>
  );
}
