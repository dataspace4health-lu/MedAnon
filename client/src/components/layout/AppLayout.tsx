import { useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { BulkExportTracker } from '@/components/shared/BulkExportTracker';
import { Menu, X, ShieldCheck } from 'lucide-react';

export function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();
  const showTracker = !location.pathname.startsWith('/bulk-deidentify');

  return (
    <div className="flex h-screen overflow-hidden">
      {/* Mobile top bar */}
      <div className="fixed top-0 left-0 right-0 z-40 flex h-14 items-center justify-between border-b bg-background/95 backdrop-blur-sm px-4 md:hidden">
        <div className="flex items-center gap-2.5">
          <div className="flex size-7 items-center justify-center rounded-lg bg-primary text-primary-foreground">
            <ShieldCheck className="size-4" />
          </div>
          <div className="leading-tight">
            <span className="text-sm font-bold">MedAnon</span>
            <span className="ml-1.5 text-[10px] font-medium uppercase tracking-widest text-muted-foreground">
              Privacy Toolkit
            </span>
          </div>
        </div>
        <button
          onClick={() => setSidebarOpen((prev) => !prev)}
          className="flex size-8 items-center justify-center rounded-md hover:bg-accent"
          aria-label={sidebarOpen ? 'Close sidebar' : 'Open sidebar'}
        >
          {sidebarOpen ? <X className="size-5" /> : <Menu className="size-5" />}
        </button>
      </div>

      {/* Sidebar overlay on mobile */}
      {sidebarOpen && (
        <div
          className="fixed inset-0 z-30 bg-black/40 backdrop-blur-[1px] md:hidden"
          onClick={() => setSidebarOpen(false)}
        />
      )}

      {/* Sidebar */}
      <aside
        className={`
          fixed inset-y-0 left-0 z-30 w-64 transform border-r bg-sidebar-background transition-transform duration-200
          md:relative md:translate-x-0 md:shrink-0
          ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
        `}
      >
        <Sidebar onNavigate={() => setSidebarOpen(false)} />
      </aside>

      {/* Main content */}
      <main className="flex-1 overflow-y-auto pt-14 md:pt-0" id="main-scroll">
        <div className="mx-auto max-w-6xl px-4 py-8 sm:px-6 lg:px-8">
          <Outlet />
        </div>
      </main>

      {/* Global bulk export progress tracker */}
      {showTracker && <BulkExportTracker />}
    </div>
  );
}
