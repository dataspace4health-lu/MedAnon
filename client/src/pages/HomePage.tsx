import { Link } from 'react-router-dom';
import { ArrowRight } from 'lucide-react';
import { DashboardMetrics } from './dashboard/DashboardMetrics';
import { PipelineActivityFeed } from './dashboard/PipelineActivityFeed';
import { QuickActions } from './dashboard/QuickActions';
import { ServiceHealthStrip } from './dashboard/ServiceHealthStrip';
import { useAuth } from '@/context/AuthContext';
import { NAV_SECTIONS, type NavItem } from '@/config/navigation';

// ---------------------------------------------------------------------------
// Tool card, palette-token styling only (no ad-hoc Tailwind colors)
// ---------------------------------------------------------------------------

function ToolCard({ item }: { item: NavItem }) {
  const Icon = item.icon;
  return (
    <Link
      to={item.to}
      className="group relative flex gap-4 rounded-xl border border-border bg-card p-5 transition-all duration-200 hover:-translate-y-0.5 hover:border-primary/40 hover:shadow-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="flex size-11 shrink-0 items-center justify-center rounded-xl bg-accent text-primary">
        <Icon className="size-5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="flex items-center gap-1.5">
          <span className="truncate text-sm font-semibold text-card-foreground">{item.label}</span>
          <ArrowRight className="size-4 shrink-0 text-primary opacity-0 transition-all group-hover:translate-x-0.5 group-hover:opacity-100" />
        </div>
        <p className="mt-1 line-clamp-2 text-xs leading-relaxed text-muted-foreground">
          {item.description}
        </p>
      </div>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// Home / Dashboard
// ---------------------------------------------------------------------------

export default function HomePage() {
  const { hasRole } = useAuth();

  return (
    <div className="space-y-10">
      {/* Hero / welcome */}
      <header className="space-y-1.5">
        <h1 className="text-2xl font-semibold tracking-tight text-foreground sm:text-3xl">
          Data Privacy Toolkit
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground sm:text-base">
          De-identify and pseudonymize healthcare data across FHIR, HL7&nbsp;v2, CDA, and DICOM,
          with measurable privacy, utility, and quality scoring.
        </p>
      </header>

      {/* Live KPIs */}
      <DashboardMetrics />

      {/* Service health */}
      <ServiceHealthStrip />

      {/* Operational split: activity feed + quick actions */}
      <div className="grid grid-cols-1 gap-6 lg:grid-cols-5">
        <div className="lg:col-span-3"><PipelineActivityFeed /></div>
        <div className="lg:col-span-2"><QuickActions /></div>
      </div>

      {/* All tools, grouped by section, role-gated, driven by the nav config */}
      <section className="space-y-8">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold uppercase tracking-[0.12em] text-muted-foreground">
            All Tools
          </h2>
          <div className="h-px flex-1 bg-border" />
        </div>

        {NAV_SECTIONS.map((section) => {
          const visible = section.items.filter((i) => hasRole(i.minRole));
          if (!visible.length) return null;
          return (
            <div key={section.label} className="space-y-3">
              <h3 className="text-xs font-semibold uppercase tracking-[0.14em] text-primary/80">
                {section.label}
              </h3>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
                {visible.map((item) => (
                  <ToolCard key={item.to} item={item} />
                ))}
              </div>
            </div>
          );
        })}
      </section>
    </div>
  );
}
