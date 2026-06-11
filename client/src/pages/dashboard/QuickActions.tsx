import { Link } from 'react-router-dom';
import { PackageOpen, Layers, Activity, TrendingUp, BarChart3, SlidersHorizontal, ArrowUpRight } from 'lucide-react';

const ACTIONS = [
  { to: '/bulk-deidentify', label: 'Bulk Job',  icon: PackageOpen,     bg: 'bg-blue-500/10',    icon_color: 'text-blue-500',    hover: 'hover:bg-blue-500/15' },
  { to: '/process',         label: 'Process',   icon: Layers,          bg: 'bg-orange-500/10',  icon_color: 'text-orange-500',  hover: 'hover:bg-orange-500/15' },
  { to: '/batch',           label: 'Batch',     icon: Activity,        bg: 'bg-sky-600/10',  icon_color: 'text-sky-600',  hover: 'hover:bg-sky-600/15' },
  { to: '/analytics',       label: 'Analytics', icon: TrendingUp,      bg: 'bg-emerald-500/10', icon_color: 'text-emerald-500', hover: 'hover:bg-emerald-500/15' },
  { to: '/jobs',            label: 'Jobs',      icon: BarChart3,       bg: 'bg-blue-600/10',  icon_color: 'text-blue-600',  hover: 'hover:bg-blue-600/15' },
  { to: '/configs',         label: 'Configs',   icon: SlidersHorizontal, bg: 'bg-slate-500/10', icon_color: 'text-slate-500', hover: 'hover:bg-slate-500/15' },
];

export function QuickActions() {
  return (
    <div className="rounded-2xl border bg-card shadow-sm overflow-hidden h-full">
      <div className="border-b px-5 py-3.5">
        <h3 className="text-sm font-bold text-foreground">Quick Actions</h3>
        <p className="text-xs text-muted-foreground mt-0.5">Jump to any tool</p>
      </div>
      <div className="p-3 grid grid-cols-2 gap-2">
        {ACTIONS.map(({ to, label, icon: Icon, bg, icon_color, hover }) => (
          <Link
            key={to}
            to={to}
            className={`group flex items-center gap-2.5 rounded-xl p-3 transition-all ${hover} border border-transparent hover:border-border/60`}
          >
            <div className={`flex size-8 shrink-0 items-center justify-center rounded-lg ${bg}`}>
              <Icon className={`size-4 ${icon_color}`} />
            </div>
            <span className="text-sm font-medium text-foreground truncate">{label}</span>
            <ArrowUpRight className="size-3 text-muted-foreground opacity-0 group-hover:opacity-100 transition-opacity ml-auto shrink-0" />
          </Link>
        ))}
      </div>
    </div>
  );
}
