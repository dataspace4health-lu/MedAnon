import { useState, useEffect, useCallback } from 'react';
import { PageHeader } from '@/components/layout/PageHeader';
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs';
import { Card, CardContent } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import {
  Loader2,
  AlertTriangle,
  RefreshCw,
  ExternalLink,
  BarChart3,
  LayoutDashboard,
  Terminal,
  CheckCircle2,
  XCircle,
} from 'lucide-react';
import { cn } from '@/lib/utils';
import {
  BarChart,
  Bar,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ResponsiveContainer,
  Cell,
} from 'recharts';

// ---------------------------------------------------------------------------
// Prometheus HTTP API helpers
// ---------------------------------------------------------------------------

interface PromResult {
  metric: Record<string, string>;
  value: [number, string]; // [timestamp, value]
}

interface PromResponse {
  status: 'success' | 'error';
  data?: { resultType: string; result: PromResult[] };
  error?: string;
}

async function promQuery(query: string): Promise<PromResult[]> {
  const res = await fetch(
    `/prometheus/api/v1/query?${new URLSearchParams({ query })}`,
    { signal: AbortSignal.timeout(8_000) },
  );
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const json: PromResponse = await res.json();
  if (json.status !== 'success') throw new Error(json.error ?? 'Prometheus error');
  return json.data?.result ?? [];
}

// ---------------------------------------------------------------------------
// Metric value formatting
// ---------------------------------------------------------------------------

function fmtBytes(n: number): string {
  if (n >= 1_073_741_824) return `${(n / 1_073_741_824).toFixed(1)} GB`;
  if (n >= 1_048_576)     return `${(n / 1_048_576).toFixed(0)} MB`;
  if (n >= 1_024)         return `${(n / 1_024).toFixed(0)} KB`;
  return `${n} B`;
}

function fmtCpu(n: number): string {
  return `${(n * 100).toFixed(1)}%`;
}

function shortName(name: string): string {
  return name.replace(/^medanon[-_]?/, '').replace(/_/g, '-') || name;
}

// ---------------------------------------------------------------------------
// Service uptime table
// ---------------------------------------------------------------------------

function UptimeTable({ results }: { results: PromResult[] }) {
  if (results.length === 0)
    return <p className="text-sm text-muted-foreground">No scrape targets found.</p>;

  return (
    <div className="rounded-xl border overflow-hidden">
      <div className="divide-y">
        {results.map((r) => {
          const up = r.value[1] === '1';
          const job = r.metric.job ?? r.metric.instance ?? '';
          const instance = r.metric.instance ?? '';
          return (
            <div key={`${job}-${instance}`} className="flex items-center gap-3 px-4 py-2.5">
              {up ? (
                <CheckCircle2 className="size-4 shrink-0 text-emerald-500" />
              ) : (
                <XCircle className="size-4 shrink-0 text-red-500" />
              )}
              <span className="flex-1 text-sm font-medium">{job}</span>
              <span className="text-xs text-muted-foreground font-mono">{instance}</span>
              <span className={cn('text-xs font-semibold', up ? 'text-emerald-600' : 'text-red-500')}>
                {up ? 'UP' : 'DOWN'}
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Generic horizontal bar chart for resource metrics
// ---------------------------------------------------------------------------

function ResourceChart({
  title,
  data,
  format,
  color,
}: {
  title: string;
  data: { name: string; value: number }[];
  format: (n: number) => string;
  color: string;
}) {
  if (data.length === 0) return null;

  return (
    <div className="space-y-2">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</p>
      <ResponsiveContainer width="100%" height={Math.max(data.length * 36 + 20, 80)}>
        <BarChart
          data={data}
          layout="vertical"
          margin={{ top: 0, right: 60, left: 0, bottom: 0 }}
        >
          <CartesianGrid strokeDasharray="3 3" horizontal={false} />
          <XAxis type="number" tick={{ fontSize: 10 }} tickFormatter={format} />
          <YAxis
            type="category"
            dataKey="name"
            width={120}
            tick={{ fontSize: 11 }}
            tickFormatter={shortName}
          />
          <Tooltip
            formatter={(v) => [format(typeof v === 'number' ? v : 0), title]}
            contentStyle={{ fontSize: 11 }}
          />
          <Bar dataKey="value" radius={[0, 3, 3, 0]}>
            {data.map((_, i) => (
              <Cell key={i} fill={color} fillOpacity={0.8 - i * 0.04} />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Native metrics panel (queries Prometheus HTTP API)
// ---------------------------------------------------------------------------

interface MetricsState {
  loading: boolean;
  error: string | null;
  uptime: PromResult[];
  memory: { name: string; value: number }[];
  cpu:    { name: string; value: number }[];
  requests: number | null;
}

const EMPTY_METRICS: MetricsState = {
  loading: true,
  error: null,
  uptime: [],
  memory: [],
  cpu: [],
  requests: null,
};

function MetricsPanel() {
  const [m, setM] = useState<MetricsState>(EMPTY_METRICS);

  const fetch = useCallback(async () => {
    setM((prev) => ({ ...prev, loading: true, error: null }));
    try {
      const [uptime, memRaw, cpuRaw, reqRaw] = await Promise.allSettled([
        promQuery('up'),
        promQuery('container_memory_usage_bytes{container!="",container!="POD"}'),
        promQuery('rate(container_cpu_usage_seconds_total{container!="",container!="POD"}[2m])'),
        promQuery('sum(medanon_requests_total)'),
      ]);

      const memory = uptime.status === 'fulfilled'
        ? (memRaw.status === 'fulfilled' ? memRaw.value : [])
            .map((r) => ({
              name: r.metric.name ?? r.metric.container ?? r.metric.instance ?? '?',
              value: parseFloat(r.value[1]),
            }))
            .filter((d) => d.name.includes('medanon') || d.name.includes('hapi') || d.name.includes('gpas') || d.name.includes('redis'))
            .sort((a, b) => b.value - a.value)
            .slice(0, 12)
        : [];

      const cpu = (cpuRaw.status === 'fulfilled' ? cpuRaw.value : [])
        .map((r) => ({
          name: r.metric.name ?? r.metric.container ?? r.metric.instance ?? '?',
          value: parseFloat(r.value[1]),
        }))
        .filter((d) => d.name.includes('medanon') || d.name.includes('hapi') || d.name.includes('gpas') || d.name.includes('redis'))
        .sort((a, b) => b.value - a.value)
        .slice(0, 12);

      const requests = reqRaw.status === 'fulfilled' && reqRaw.value.length > 0
        ? parseFloat(reqRaw.value[0].value[1])
        : null;

      setM({
        loading: false,
        error: null,
        uptime: uptime.status === 'fulfilled' ? uptime.value : [],
        memory,
        cpu,
        requests,
      });
    } catch (e) {
      setM((prev) => ({ ...prev, loading: false, error: String(e) }));
    }
  }, []);

  useEffect(() => { fetch(); }, [fetch]);

  if (m.loading) {
    return (
      <div className="flex items-center justify-center gap-2 py-16 text-muted-foreground text-sm">
        <Loader2 className="size-5 animate-spin" />
        Querying Prometheus…
      </div>
    );
  }

  if (m.error) {
    return (
      <div className="flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/5 p-4 text-sm text-destructive">
        <AlertTriangle className="size-4 shrink-0" />
        {m.error}
      </div>
    );
  }

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Live snapshot, instant queries
        </p>
        <Button variant="outline" size="sm" onClick={fetch} className="h-7 text-xs gap-1">
          <RefreshCw className="size-3" />
          Refresh
        </Button>
      </div>

      {/* Summary stat */}
      {m.requests !== null && (
        <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
          <Card>
            <CardContent className="pt-4 pb-3">
              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Total Requests Processed
              </p>
              <p className="mt-1 text-2xl font-bold tabular-nums">
                {m.requests.toLocaleString()}
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4 pb-3">
              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Targets Up
              </p>
              <p className="mt-1 text-2xl font-bold tabular-nums text-emerald-600">
                {m.uptime.filter((r) => r.value[1] === '1').length}
                <span className="ml-1 text-sm font-normal text-muted-foreground">
                  / {m.uptime.length}
                </span>
              </p>
            </CardContent>
          </Card>
          <Card>
            <CardContent className="pt-4 pb-3">
              <p className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground">
                Containers Tracked
              </p>
              <p className="mt-1 text-2xl font-bold tabular-nums">
                {m.memory.length}
              </p>
            </CardContent>
          </Card>
        </div>
      )}

      {/* Service uptime */}
      <div className="space-y-2">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          Scrape Targets
        </p>
        <UptimeTable results={m.uptime} />
      </div>

      {/* Memory */}
      <ResourceChart
        title="Container Memory Usage"
        data={m.memory}
        format={fmtBytes}
        color="#0072bc"
      />

      {/* CPU */}
      <ResourceChart
        title="Container CPU Usage (2 min rate)"
        data={m.cpu}
        format={fmtCpu}
        color="#0099d8"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Grafana iframe panel
// ---------------------------------------------------------------------------

type GrafanaStatus = 'checking' | 'ok' | 'error';

function GrafanaPanel() {
  const [grafana, setGrafana] = useState<GrafanaStatus>('checking');
  const [iframeLoaded, setIframeLoaded] = useState(false);

  useEffect(() => {
    fetch('/grafana/api/health', { signal: AbortSignal.timeout(5_000) })
      .then((r) => setGrafana(r.ok ? 'ok' : 'error'))
      .catch(() => setGrafana('error'));
  }, []);

  const openLink = (
    <a
      href="/grafana/"
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm hover:bg-accent transition-colors"
    >
      <ExternalLink className="size-3" />
      Open in new tab
    </a>
  );

  if (grafana === 'checking') {
    return (
      <div className="flex items-center justify-center gap-2 rounded-lg border bg-muted/30 py-12 text-sm text-muted-foreground">
        <Loader2 className="size-5 animate-spin" />
        Connecting to Grafana…
      </div>
    );
  }

  if (grafana === 'error') {
    return (
      <div className="space-y-4">
        <div className="rounded-xl border border-amber-200/60 bg-amber-50/40 dark:border-amber-800/30 dark:bg-amber-900/10 p-5">
          <div className="flex items-start gap-3">
            <AlertTriangle className="size-5 shrink-0 mt-0.5 text-amber-600" />
            <div className="flex-1">
              <p className="font-semibold text-sm">Grafana is not reachable</p>
              <p className="text-xs text-muted-foreground mt-1">
                Grafana is served at <code className="font-mono bg-muted px-1 rounded">/grafana/</code>.
                If you started with the monitoring profile it may still be warming up.
              </p>
            </div>
          </div>
          <div className="mt-3 flex items-center gap-3">
            <Button variant="outline" size="sm" className="gap-1.5"
              onClick={() => { setGrafana('checking'); fetch('/grafana/api/health', { signal: AbortSignal.timeout(5_000) }).then((r) => setGrafana(r.ok ? 'ok' : 'error')).catch(() => setGrafana('error')); }}
            >
              <RefreshCw className="size-3.5" /> Retry
            </Button>
            {openLink}
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted-foreground">
          Grafana dashboards, log in with admin credentials to create and save views.
        </p>
        {openLink}
      </div>

      {!iframeLoaded && (
        <div className="flex items-center justify-center gap-2 rounded-lg border bg-muted/30 py-12 text-sm text-muted-foreground">
          <Loader2 className="size-5 animate-spin" />
          Loading Grafana…
        </div>
      )}

      <iframe
        src="/grafana/"
        title="Grafana"
        className={cn('w-full rounded-xl border bg-background transition-opacity', iframeLoaded ? 'opacity-100' : 'opacity-0 absolute')}
        style={{ height: 'calc(100vh - 260px)', minHeight: '600px' }}
        onLoad={() => setIframeLoaded(true)}
        sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-popups-to-escape-sandbox"
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Setup instructions, shown when monitoring is not enabled
// ---------------------------------------------------------------------------

function SetupCard() {
  return (
    <Card className="border-amber-200/60 bg-amber-50/40 dark:border-amber-800/30 dark:bg-amber-900/10">
      <CardContent className="pt-5 pb-4 space-y-4">
        <div className="flex items-start gap-3">
          <AlertTriangle className="size-5 shrink-0 mt-0.5 text-amber-600" />
          <div>
            <p className="font-semibold text-sm">Monitoring profile is not running</p>
            <p className="text-xs text-muted-foreground mt-0.5">
              Prometheus, Grafana and cAdvisor are available as an opt-in profile. Start them
              with the command below, then reload this page.
            </p>
          </div>
        </div>

        <div className="flex items-center gap-2 rounded-md bg-muted px-3 py-2 font-mono text-xs">
          <Terminal className="size-3.5 shrink-0 text-muted-foreground" />
          docker compose --profile monitoring up -d
        </div>

        <div className="grid grid-cols-1 gap-3 sm:grid-cols-3 text-xs">
          {[
            { name: 'Prometheus', port: 9090, desc: 'Metrics collection & alerting' },
            { name: 'Grafana',    port: 3000, desc: 'Dashboard builder (proxied at /grafana/)' },
            { name: 'cAdvisor',   port: 8888, desc: 'Container CPU / memory / network' },
          ].map((s) => (
            <div key={s.name} className="rounded-lg border bg-background p-3">
              <p className="font-semibold">{s.name}</p>
              <p className="text-muted-foreground mt-0.5">{s.desc}</p>
              <p className="mt-1 font-mono text-foreground/60">:{s.port}</p>
            </div>
          ))}
        </div>
      </CardContent>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

type MonitoringStatus = 'checking' | 'available' | 'unavailable';

export default function MonitoringPage() {
  const [status, setStatus] = useState<MonitoringStatus>('checking');

  useEffect(() => {
    const check = async () => {
      try {
        const res = await fetch('/prometheus/api/v1/query?query=up', {
          signal: AbortSignal.timeout(5_000),
        });
        setStatus(res.ok ? 'available' : 'unavailable');
      } catch {
        setStatus('unavailable');
      }
    };
    check();
  }, []);

  return (
    <div className="flex flex-col gap-6">
      <PageHeader
        title="Monitoring"
        description="Resource consumption, service uptime and observability dashboards"
      />

      {status === 'checking' && (
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Checking monitoring stack…
        </div>
      )}

      {status === 'unavailable' && <SetupCard />}

      {status === 'available' && (
        <Tabs defaultValue="grafana">
          <TabsList className="mb-4">
            <TabsTrigger value="grafana" className="gap-1.5">
              <LayoutDashboard className="size-3.5" />
              Grafana Dashboards
            </TabsTrigger>
            <TabsTrigger value="metrics" className="gap-1.5">
              <BarChart3 className="size-3.5" />
              Live Metrics
            </TabsTrigger>
          </TabsList>

          <TabsContent value="grafana">
            <GrafanaPanel />
          </TabsContent>

          <TabsContent value="metrics">
            <MetricsPanel />
          </TabsContent>
        </Tabs>
      )}
    </div>
  );
}
