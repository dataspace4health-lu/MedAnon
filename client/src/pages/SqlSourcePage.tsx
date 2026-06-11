import { useState, useCallback, useEffect } from 'react';
import { Link } from 'react-router-dom';
import { toast } from 'sonner';
import {
  Loader2,
  Database,
  Plug,
  Trash2,
  Play,
  ShieldCheck,
  Table2,
  ChevronRight,
  ChevronDown,
} from 'lucide-react';
import { PageHeader } from '@/components/layout/PageHeader';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from '@/components/ui/card';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useAuth } from '@/context/AuthContext';
import {
  listSqlConnections,
  createSqlConnection,
  testSqlConnection,
  deleteSqlConnection,
  inspectSql,
  submitSqlExport,
  type SqlConnection,
  type SqlSchemaPreview,
  type SqlColumnRule,
  type SqlOutputFormat,
} from '@/api/sqlSource';

// Per-column action vocabulary — mirrors the tabular column-mapper so the SQL
// schema explorer offers the same de-identification choices (incl. gPAS).
const COLUMN_ACTIONS: {
  value: string;
  label: string;
  toRule: (table: string, col: string) => SqlColumnRule | null;
}[] = [
  { value: 'none', label: 'Keep', toRule: () => null },
  { value: 'redact', label: 'Redact', toRule: (t, c) => ({ table: t, column: c, action: 'redact' }) },
  {
    value: 'generalize_year',
    label: 'Generalize → year',
    toRule: (t, c) => ({ table: t, column: c, action: 'generalize', params: { strategy: 'date_year' } }),
  },
  { value: 'pseudonymize_gpas', label: 'Pseudonymize (gPAS)', toRule: (t, c) => ({ table: t, column: c, action: 'gpas_pseudonymize' }) },
  { value: 'tokenize', label: 'Tokenize (consistent)', toRule: (t, c) => ({ table: t, column: c, action: 'tokenize' }) },
  { value: 'cryptohash', label: 'Hash (pseudonym)', toRule: (t, c) => ({ table: t, column: c, action: 'cryptohash' }) },
  { value: 'nlp_scrub', label: 'NLP scrub (free text)', toRule: (t, c) => ({ table: t, column: c, action: 'nlp_scrub' }) },
];

const ACTION_LABEL: Record<string, string> = Object.fromEntries(
  COLUMN_ACTIONS.map((a) => [a.value, a.label]),
);

const OUTPUT_FORMATS: { value: SqlOutputFormat; label: string }[] = [
  { value: 'csv', label: 'CSV' },
  { value: 'ndjson', label: 'NDJSON' },
  { value: 'parquet', label: 'Parquet' },
];

const EMPTY_FORM = {
  name: '', host: '', port: 5432, dbname: '', username: '', password: '', sslmode: 'prefer',
};

export default function SqlSourcePage() {
  const { hasRole } = useAuth();
  const isAdmin = hasRole('admin');

  const [connections, setConnections] = useState<SqlConnection[]>([]);
  const [selectedConnId, setSelectedConnId] = useState<string>('');
  const [schemaName, setSchemaName] = useState<string>('public');
  const [form, setForm] = useState({ ...EMPTY_FORM });
  const [showForm, setShowForm] = useState(false);

  const [preview, setPreview] = useState<SqlSchemaPreview | null>(null);
  // table → column → action value
  const [actions, setActions] = useState<Record<string, Record<string, string>>>({});
  const [includedTables, setIncludedTables] = useState<Set<string>>(new Set());
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [outputFormat, setOutputFormat] = useState<SqlOutputFormat>('csv');

  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [submittedJobId, setSubmittedJobId] = useState<string | null>(null);

  const loadConnections = useCallback(async () => {
    try {
      setConnections(await listSqlConnections());
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
    }
  }, []);

  useEffect(() => {
    loadConnections();
  }, [loadConnections]);

  // -- connection management ------------------------------------------------

  const handleCreate = useCallback(async () => {
    if (!form.name || !form.host || !form.dbname || !form.username) {
      toast.error('Name, host, database, and username are required.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const conn = await createSqlConnection(form);
      toast.success(`Connection "${conn.name}" saved.`);
      setForm({ ...EMPTY_FORM });
      setShowForm(false);
      await loadConnections();
      setSelectedConnId(conn.id);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      toast.error('Could not save connection.', { description: msg });
    } finally {
      setBusy(false);
    }
  }, [form, loadConnections]);

  const handleTest = useCallback(async (id: string) => {
    setBusy(true);
    try {
      const res = await testSqlConnection(id);
      toast.success('Connection OK.', { description: res.server });
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error('Connection failed.', { description: msg });
    } finally {
      setBusy(false);
    }
  }, []);

  const handleDelete = useCallback(async (id: string) => {
    if (!window.confirm('Delete this connection?')) return;
    try {
      await deleteSqlConnection(id);
      toast.success('Connection deleted.');
      if (selectedConnId === id) {
        setSelectedConnId('');
        setPreview(null);
      }
      await loadConnections();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      toast.error('Could not delete.', { description: msg });
    }
  }, [selectedConnId, loadConnections]);

  // -- schema inspection ----------------------------------------------------

  const handleInspect = useCallback(async () => {
    if (!selectedConnId) {
      toast.error('Choose a connection first.');
      return;
    }
    setBusy(true);
    setError(null);
    setSubmittedJobId(null);
    try {
      const schema = await inspectSql(selectedConnId, (schemaName || 'public').trim());
      setPreview(schema);
      // Pre-fill each column with its recommended action; pre-include any table
      // that has at least one recommended (non-keep) action.
      const next: Record<string, Record<string, string>> = {};
      const include = new Set<string>();
      for (const tbl of schema.tables) {
        next[tbl.name] = {};
        let anyReco = false;
        for (const col of tbl.columns) {
          const reco = col.recommended_action ?? 'none';
          next[tbl.name][col.name] = reco;
          if (reco !== 'none') anyReco = true;
        }
        if (anyReco) include.add(tbl.name);
      }
      setActions(next);
      setIncludedTables(include);
      setExpanded(new Set(include));
      toast.success(`Found ${schema.tables.length} tables in "${schema.schema}".`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      toast.error('Could not inspect schema.', { description: msg });
    } finally {
      setBusy(false);
    }
  }, [selectedConnId, schemaName]);

  const toggleTable = useCallback((name: string) => {
    setIncludedTables((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const toggleExpand = useCallback((name: string) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  }, []);

  const setColumnAction = useCallback((table: string, column: string, value: string) => {
    setActions((prev) => ({
      ...prev,
      [table]: { ...prev[table], [column]: value ?? 'none' },
    }));
  }, []);

  // -- export ---------------------------------------------------------------

  const buildRules = useCallback((): SqlColumnRule[] => {
    const rules: SqlColumnRule[] = [];
    for (const table of includedTables) {
      const cols = actions[table] ?? {};
      for (const [col, choice] of Object.entries(cols)) {
        const rule = COLUMN_ACTIONS.find((a) => a.value === choice)?.toRule(table, col);
        if (rule) rules.push(rule);
      }
    }
    return rules;
  }, [includedTables, actions]);

  const handleExport = useCallback(async () => {
    const tables = Array.from(includedTables);
    if (tables.length === 0) {
      toast.error('Select at least one table to export.');
      return;
    }
    const rules = buildRules();
    if (rules.length === 0) {
      toast.error('Map at least one column to an action.');
      return;
    }
    setBusy(true);
    setError(null);
    setSubmittedJobId(null);
    try {
      const { job_id } = await submitSqlExport({
        connectionId: selectedConnId,
        tables,
        schema: (schemaName || 'public').trim(),
        outputFormat,
        rules,
      });
      setSubmittedJobId(job_id);
      toast.success(`Export job submitted (${tables.length} tables). Track it on Jobs.`);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setError(msg);
      toast.error('Export submit failed.', { description: msg });
    } finally {
      setBusy(false);
    }
  }, [includedTables, buildRules, selectedConnId, outputFormat, schemaName]);

  // -- render ---------------------------------------------------------------

  return (
    <div>
      <PageHeader
        title="SQL Database De-identification"
        description="Connect to a PostgreSQL database (read-only), explore its tables and columns with recommended actions, then export de-identified files. gPAS pseudonyms stay consistent across tables, so joins survive."
      />

      <div className="mb-5 flex items-start gap-2 rounded-lg border border-primary/30 bg-primary/5 px-4 py-3">
        <ShieldCheck className="mt-0.5 size-4 shrink-0 text-primary" />
        <p className="text-sm text-muted-foreground">
          Connections are <strong>read-only</strong> and restricted to allow-listed hosts.
          Output is returned as downloadable files — never written back to the source or
          uploaded to the FHIR target server.
        </p>
      </div>

      {/* 1. Connections */}
      <Card className="mb-5">
        <CardHeader className="flex-row items-center justify-between pb-3">
          <CardTitle className="text-base">1. Connection</CardTitle>
          {isAdmin && (
            <Button variant="outline" size="sm" onClick={() => setShowForm((v) => !v)}>
              <Database className="mr-1.5 h-3.5 w-3.5" />
              {showForm ? 'Cancel' : 'Add connection'}
            </Button>
          )}
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {connections.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              No saved connections yet.{isAdmin ? ' Add one to get started.' : ' Ask an admin to add one.'}
            </p>
          ) : (
            <div className="flex flex-col gap-2">
              {connections.map((c) => (
                <div
                  key={c.id}
                  className={`flex items-center justify-between rounded-lg border px-3 py-2 ${
                    selectedConnId === c.id ? 'border-primary bg-primary/5' : ''
                  }`}
                >
                  <button
                    type="button"
                    className="flex flex-1 items-center gap-2 text-left"
                    onClick={() => { setSelectedConnId(c.id); setPreview(null); }}
                  >
                    <Database className="size-4 text-muted-foreground" />
                    <span className="text-sm font-medium">{c.name}</span>
                    <span className="text-xs text-muted-foreground">
                      {c.username}@{c.host}:{c.port}/{c.dbname}
                    </span>
                  </button>
                  <div className="flex items-center gap-1">
                    <Button variant="ghost" size="sm" onClick={() => handleTest(c.id)} disabled={busy}>
                      <Plug className="h-3.5 w-3.5" />
                    </Button>
                    {isAdmin && (
                      <Button variant="ghost" size="sm" onClick={() => handleDelete(c.id)}>
                        <Trash2 className="h-3.5 w-3.5 text-destructive" />
                      </Button>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}

          {showForm && isAdmin && (
            <div className="grid grid-cols-1 gap-3 rounded-lg border bg-muted/30 p-4 sm:grid-cols-2">
              <Input placeholder="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              <Input placeholder="Host" value={form.host} onChange={(e) => setForm({ ...form, host: e.target.value })} />
              <Input type="number" placeholder="Port" value={form.port} onChange={(e) => setForm({ ...form, port: Number(e.target.value) || 5432 })} />
              <Input placeholder="Database" value={form.dbname} onChange={(e) => setForm({ ...form, dbname: e.target.value })} />
              <Input placeholder="Username" value={form.username} onChange={(e) => setForm({ ...form, username: e.target.value })} />
              <Input type="password" placeholder="Password" value={form.password} onChange={(e) => setForm({ ...form, password: e.target.value })} />
              <Select value={form.sslmode} onValueChange={(v) => setForm({ ...form, sslmode: v ?? 'prefer' })}>
                <SelectTrigger><SelectValue /></SelectTrigger>
                <SelectContent>
                  {['disable', 'allow', 'prefer', 'require', 'verify-ca', 'verify-full'].map((m) => (
                    <SelectItem key={m} value={m}>{m}</SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <div className="sm:col-span-2">
                <Button onClick={handleCreate} disabled={busy} className="w-full">
                  {busy ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <Database className="mr-1.5 h-4 w-4" />}
                  Save connection
                </Button>
              </div>
            </div>
          )}

          <div className="space-y-1">
            <label htmlFor="sql-schema" className="text-xs font-medium text-muted-foreground">
              Schema
            </label>
            <input
              id="sql-schema"
              type="text"
              value={schemaName}
              onChange={(e) => setSchemaName(e.target.value)}
              placeholder="public"
              spellCheck={false}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm"
            />
            <p className="text-[11px] text-muted-foreground">
              The PostgreSQL schema to explore (e.g. <code>public</code>, <code>clinic</code>).
            </p>
          </div>

          <Button onClick={handleInspect} disabled={busy || !selectedConnId} className="w-full">
            {busy && !preview ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <Table2 className="mr-1.5 h-4 w-4" />}
            {preview ? 'Re-read schema' : 'Inspect schema'}
          </Button>

          {error && (
            <div className="rounded-lg border border-destructive/50 bg-destructive/10 px-4 py-3">
              <p className="text-sm font-medium text-destructive">Error</p>
              <p className="mt-0.5 text-sm text-destructive/80">{error}</p>
            </div>
          )}
        </CardContent>
      </Card>

      {/* 2. Schema explorer */}
      {preview && (
        <Card className="mb-5">
          <CardHeader className="flex-row items-center justify-between pb-3">
            <CardTitle className="text-base">2. Map columns to actions</CardTitle>
            <span className="text-xs text-muted-foreground">
              schema <code className="font-mono">{preview.schema}</code> · {includedTables.size} of {preview.tables.length} tables selected
            </span>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            {preview.tables.length === 0 && (
              <div className="rounded-lg border border-amber-300 bg-amber-50 px-4 py-3 text-sm text-amber-800 dark:border-amber-900/50 dark:bg-amber-950/20 dark:text-amber-300">
                No tables found in schema <code className="font-mono">{preview.schema}</code>.
                Check the <strong>Schema</strong> field above — your data may live in a
                different schema (e.g. <code className="font-mono">clinic</code>), not{' '}
                <code className="font-mono">public</code>.
              </div>
            )}
            {preview.tables.map((tbl) => {
              const isOpen = expanded.has(tbl.name);
              const included = includedTables.has(tbl.name);
              return (
                <div key={tbl.name} className="overflow-hidden rounded-lg border">
                  <div className="flex items-center gap-2 bg-muted/40 px-3 py-2">
                    <Checkbox checked={included} onCheckedChange={() => toggleTable(tbl.name)} />
                    <button type="button" className="flex flex-1 items-center gap-1.5 text-left" onClick={() => toggleExpand(tbl.name)}>
                      {isOpen ? <ChevronDown className="size-4" /> : <ChevronRight className="size-4" />}
                      <span className="text-sm font-medium">{tbl.name}</span>
                      <span className="text-xs text-muted-foreground">
                        ~{tbl.row_estimate.toLocaleString()} rows · {tbl.columns.length} cols
                      </span>
                    </button>
                  </div>
                  {isOpen && (
                    <table className="w-full text-sm">
                      <thead className="bg-muted/20 text-xs uppercase tracking-wide text-muted-foreground">
                        <tr>
                          <th className="px-4 py-2 text-left font-medium">Column</th>
                          <th className="px-4 py-2 text-left font-medium">Type · samples</th>
                          <th className="px-4 py-2 text-left font-medium">Action</th>
                        </tr>
                      </thead>
                      <tbody className="divide-y">
                        {tbl.columns.map((col) => (
                          <tr key={col.name} className="align-top">
                            <td className="px-4 py-2.5 font-medium">{col.name}</td>
                            <td className="px-4 py-2.5">
                              <div className="flex flex-wrap items-center gap-1">
                                <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground">
                                  {col.data_type}
                                </span>
                                {col.samples.slice(0, 2).map((s, i) => (
                                  <span key={i} className="truncate rounded bg-muted px-1.5 py-0.5 font-mono text-[11px] text-muted-foreground" style={{ maxWidth: '12rem' }}>
                                    {s}
                                  </span>
                                ))}
                              </div>
                            </td>
                            <td className="px-4 py-2 w-52">
                              <Select
                                value={actions[tbl.name]?.[col.name] ?? 'none'}
                                onValueChange={(v) => setColumnAction(tbl.name, col.name, v ?? 'none')}
                              >
                                <SelectTrigger className="h-8 w-full text-xs"><SelectValue /></SelectTrigger>
                                <SelectContent>
                                  {COLUMN_ACTIONS.map((a) => (
                                    <SelectItem key={a.value} value={a.value}>{a.label}</SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                              {col.recommended_action && col.recommended_action !== 'none' && (
                                actions[tbl.name]?.[col.name] === col.recommended_action ? (
                                  <span className="mt-1 inline-flex items-center gap-1 text-[11px] font-medium text-primary">
                                    <ShieldCheck className="size-3" /> Recommended
                                  </span>
                                ) : (
                                  <button
                                    type="button"
                                    onClick={() => setColumnAction(tbl.name, col.name, col.recommended_action as string)}
                                    className="mt-1 text-[11px] text-muted-foreground underline-offset-2 hover:text-primary hover:underline"
                                  >
                                    Suggested: {ACTION_LABEL[col.recommended_action] ?? col.recommended_action}
                                  </button>
                                )
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                </div>
              );
            })}
          </CardContent>
        </Card>
      )}

      {/* 3. Export */}
      {preview && (
        <Card>
          <CardHeader className="pb-3">
            <CardTitle className="text-base">3. Export de-identified files</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="flex items-center gap-3">
              <span className="text-sm text-muted-foreground">Output format</span>
              <div className="inline-flex items-center rounded-lg border bg-background p-1">
                {OUTPUT_FORMATS.map((f) => (
                  <button
                    key={f.value}
                    type="button"
                    onClick={() => setOutputFormat(f.value)}
                    className={`rounded-md px-3 py-1 text-xs transition ${
                      outputFormat === f.value ? 'bg-primary text-primary-foreground' : 'text-muted-foreground hover:text-foreground'
                    }`}
                  >
                    {f.label}
                  </button>
                ))}
              </div>
            </div>

            <Button onClick={handleExport} disabled={busy || includedTables.size === 0} className="w-full">
              {busy ? <Loader2 className="mr-1.5 h-4 w-4 animate-spin" /> : <Play className="mr-1.5 h-4 w-4" />}
              {busy ? 'Submitting…' : `De-identify ${includedTables.size} table(s) → ${outputFormat.toUpperCase()} ZIP`}
            </Button>
            <p className="text-xs text-muted-foreground">
              Columns mapped to <strong>Pseudonymize (gPAS)</strong> stay consistent across
              tables, so foreign-key joins survive in the export.
            </p>

            {submittedJobId && (
              <div className="flex items-center justify-between rounded-lg border border-primary/30 bg-primary/5 px-4 py-3">
                <div className="flex items-center gap-2">
                  <ShieldCheck className="size-4 text-primary" />
                  <p className="text-sm">
                    Job <span className="font-mono">{submittedJobId.slice(0, 8)}</span> submitted —
                    download the result ZIP on the Jobs page.
                  </p>
                </div>
                <Link to="/jobs">
                  <Button size="sm" variant="outline">Open Jobs</Button>
                </Link>
              </div>
            )}
          </CardContent>
        </Card>
      )}
    </div>
  );
}
