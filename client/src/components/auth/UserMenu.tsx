import { useState, useRef, useEffect } from 'react';
import { User, LogOut, ChevronDown, ShieldCheck, Key } from 'lucide-react';
import { useAuth } from '@/context/AuthContext';

const ROLE_LABEL: Record<string, { label: string; color: string }> = {
  admin:   { label: 'Admin',   color: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' },
  analyst: { label: 'Analyst', color: 'bg-blue-100 text-blue-700 dark:bg-blue-900/40 dark:text-blue-300' },
  viewer:  { label: 'Viewer',  color: 'bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300' },
};

export function UserMenu() {
  const { mode, isAuthenticated, user, logout } = useAuth();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    function handle(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('mousedown', handle);
    return () => document.removeEventListener('mousedown', handle);
  }, []);

  // Don't render anything in open/none mode — no auth concept to surface
  if (mode === 'none' || mode === 'auto' || !isAuthenticated) return null;

  const topRole = user.roles[0] ?? 'viewer';
  const roleStyle = ROLE_LABEL[topRole] ?? ROLE_LABEL.viewer;
  const displayName = user.name || (mode === 'apikey' ? 'API Key' : 'User');

  return (
    <div className="relative" ref={ref}>
      <button
        onClick={() => setOpen((v) => !v)}
        className="flex items-center gap-2 rounded-lg px-2.5 py-1.5 text-sm text-foreground hover:bg-accent transition-colors"
      >
        <span className="flex size-7 items-center justify-center rounded-full bg-primary/10 text-primary">
          {mode === 'apikey' ? (
            <Key className="size-3.5" />
          ) : (
            <User className="size-3.5" />
          )}
        </span>
        <span className="hidden max-w-[120px] truncate font-medium sm:inline">
          {displayName}
        </span>
        <span className={`hidden rounded-full px-2 py-0.5 text-xs font-medium sm:inline ${roleStyle.color}`}>
          {roleStyle.label}
        </span>
        <ChevronDown className={`size-3.5 text-muted-foreground transition-transform ${open ? 'rotate-180' : ''}`} />
      </button>

      {open && (
        <div className="absolute right-0 top-full z-50 mt-1 w-56 rounded-xl border border-border bg-background shadow-lg ring-1 ring-black/5 dark:ring-white/5">
          {/* User info header */}
          <div className="border-b border-border px-4 py-3">
            <div className="flex items-center gap-2.5">
              <span className="flex size-8 items-center justify-center rounded-full bg-primary/10 text-primary">
                {mode === 'apikey' ? <Key className="size-4" /> : <User className="size-4" />}
              </span>
              <div className="min-w-0">
                <p className="truncate text-sm font-semibold text-foreground">{displayName}</p>
                <div className="flex items-center gap-1 mt-0.5">
                  <ShieldCheck className="size-3 text-muted-foreground" />
                  <p className="text-xs text-muted-foreground capitalize">{topRole}</p>
                </div>
              </div>
            </div>
          </div>

          {/* Role badges (if multiple) */}
          {user.roles.length > 1 && (
            <div className="border-b border-border px-4 py-2">
              <p className="mb-1.5 text-xs text-muted-foreground">Roles</p>
              <div className="flex flex-wrap gap-1">
                {user.roles.map((r) => {
                  const s = ROLE_LABEL[r] ?? ROLE_LABEL.viewer;
                  return (
                    <span key={r} className={`rounded-full px-2 py-0.5 text-xs font-medium ${s.color}`}>
                      {s.label}
                    </span>
                  );
                })}
              </div>
            </div>
          )}

          {/* Actions */}
          <div className="p-1">
            <button
              onClick={() => { setOpen(false); void logout(); }}
              className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-destructive hover:bg-destructive/10 transition-colors"
            >
              <LogOut className="size-4" />
              {mode === 'apikey' ? 'Clear API key' : 'Sign out'}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
