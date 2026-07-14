import { useState } from 'react';
import { Outlet, useLocation } from 'react-router-dom';
import { Sidebar } from './Sidebar';
import { TopBar } from './TopBar';
import { AppFooter } from './AppFooter';
import { BulkExportTracker } from '@/components/shared/BulkExportTracker';

export function AppLayout() {
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const location = useLocation();
  const showTracker = !location.pathname.startsWith('/bulk-deidentify');

  return (
    <div className="flex flex-col h-screen overflow-hidden">
      {/* Fixed top bar, full width, always visible */}
      <TopBar
        sidebarOpen={sidebarOpen}
        onToggleSidebar={() => setSidebarOpen((v) => !v)}
      />

      <div className="flex flex-1 overflow-hidden">
        {/* Mobile sidebar overlay */}
        {sidebarOpen && (
          <div
            className="fixed inset-0 z-30 bg-black/40 backdrop-blur-[1px] md:hidden"
            onClick={() => setSidebarOpen(false)}
          />
        )}

        {/* Sidebar */}
        <aside
          className={`
            fixed top-16 bottom-0 left-0 z-30 w-64 transform border-r border-sidebar-border bg-sidebar-background transition-transform duration-200
            md:relative md:top-auto md:translate-x-0 md:shrink-0
            ${sidebarOpen ? 'translate-x-0' : '-translate-x-full'}
          `}
        >
          <Sidebar onNavigate={() => setSidebarOpen(false)} />
        </aside>

        {/* Main content */}
        <main className="flex-1 overflow-y-auto flex flex-col" id="main-scroll">
          <div className="mx-auto max-w-[1600px] w-full px-4 py-6 sm:px-6 lg:px-8 xl:px-10 flex-1">
            <Outlet />
          </div>
          <AppFooter />
        </main>
      </div>

      {showTracker && <BulkExportTracker />}
    </div>
  );
}
