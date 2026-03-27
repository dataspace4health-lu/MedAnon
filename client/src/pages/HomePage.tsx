import { Link } from 'react-router-dom';
import { PageHeader } from '@/components/layout/PageHeader';
import { Badge } from '@/components/ui/badge';
import {
  Users,
  FileText,
  Layers,
  Shield,
  FlaskConical,
  BarChart3,
  Activity,
  ArrowRight,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

// ---------------------------------------------------------------------------
// Tool definitions
// ---------------------------------------------------------------------------

interface ToolCard {
  title: string;
  description: string;
  to: string;
  icon: LucideIcon;
  category: string;
  color: string;
}

const TOOLS: ToolCard[] = [
  {
    title: 'Patient Browser',
    description: 'Search patients in the FHIR server and run $everything de-identification with one click.',
    to: '/patients',
    icon: Users,
    category: 'Browse',
    color: 'bg-blue-500/10 text-blue-600 dark:text-blue-400',
  },
  {
    title: 'Condition Browser',
    description: 'Find clinical conditions with patient context, then de-identify the linked patient record.',
    to: '/conditions',
    icon: FileText,
    category: 'Browse',
    color: 'bg-purple-500/10 text-purple-600 dark:text-purple-400',
  },
  {
    title: 'Process Resource',
    description: 'Paste or type any FHIR resource (JSON, NDJSON, XML) and get de-identified output instantly.',
    to: '/process',
    icon: Layers,
    category: 'Process',
    color: 'bg-orange-500/10 text-orange-600 dark:text-orange-400',
  },
  {
    title: 'Batch Processing',
    description: 'Upload a FHIR bundle or NDJSON file and stream-process all resources in bulk.',
    to: '/batch',
    icon: Activity,
    category: 'Process',
    color: 'bg-orange-500/10 text-orange-600 dark:text-orange-400',
  },
  {
    title: 'Risk Assessment',
    description: 'Measure re-identification risk of de-identified data using k-anonymity and l-diversity.',
    to: '/risk',
    icon: Shield,
    category: 'Analyse',
    color: 'bg-red-500/10 text-red-600 dark:text-red-400',
  },
  {
    title: 'Synthetic Data',
    description: 'Generate realistic synthetic FHIR patients from real distributions for safe sharing.',
    to: '/synthetic',
    icon: FlaskConical,
    category: 'Analyse',
    color: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  },
  {
    title: 'Status Dashboard',
    description: 'Monitor live health, readiness, and connectivity of MedAnon, HAPI FHIR, and gPAS.',
    to: '/status',
    icon: BarChart3,
    category: 'Monitor',
    color: 'bg-slate-500/10 text-slate-600 dark:text-slate-400',
  },
];

// Category display order and accent colors
const CATEGORY_ORDER = ['Browse', 'Process', 'Analyse', 'Monitor'];
const CATEGORY_COLORS: Record<string, string> = {
  Browse:  'bg-blue-500/10 text-blue-700 dark:text-blue-300',
  Process: 'bg-orange-500/10 text-orange-700 dark:text-orange-300',
  Analyse: 'bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  Monitor: 'bg-slate-500/10 text-slate-700 dark:text-slate-300',
};

// ---------------------------------------------------------------------------
// ToolCard component
// ---------------------------------------------------------------------------

function ToolCardItem({ tool }: { tool: ToolCard }) {
  const Icon = tool.icon;

  return (
    <Link
      to={tool.to}
      className="group flex flex-col gap-3 rounded-xl border bg-card p-5 shadow-sm transition-all hover:shadow-md hover:border-primary/30 hover:-translate-y-0.5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      <div className="flex items-start justify-between">
        <div className={`flex size-10 shrink-0 items-center justify-center rounded-lg ${tool.color}`}>
          <Icon className="size-5" />
        </div>
        <Badge
          variant="secondary"
          className={`text-xs font-medium ${CATEGORY_COLORS[tool.category]}`}
        >
          {tool.category}
        </Badge>
      </div>

      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-1.5">
          <span className="font-semibold text-card-foreground">{tool.title}</span>
          <ArrowRight className="size-3.5 text-muted-foreground opacity-0 transition-opacity group-hover:opacity-100" />
        </div>
        <p className="text-sm text-muted-foreground leading-relaxed">
          {tool.description}
        </p>
      </div>
    </Link>
  );
}

// ---------------------------------------------------------------------------
// HomePage
// ---------------------------------------------------------------------------

export default function HomePage() {
  const grouped = CATEGORY_ORDER.map((cat) => ({
    category: cat,
    tools: TOOLS.filter((t) => t.category === cat),
  }));

  return (
    <div>
      <PageHeader
        title="MedAnon"
        description="Rule-driven FHIR de-identification and pseudonymization engine. Select a tool to get started."
      />

      <div className="space-y-8">
        {grouped.map(({ category, tools }) => (
          <section key={category}>
            <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              {category}
            </h2>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {tools.map((tool) => (
                <ToolCardItem key={tool.to} tool={tool} />
              ))}
            </div>
          </section>
        ))}
      </div>
    </div>
  );
}
