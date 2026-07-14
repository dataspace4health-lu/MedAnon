/**
 * ConnectorPanel, File upload and SQL database connectors for Trust Gate.
 * Renders as two sub-tabs inside the parent TrustGatePage Input card.
 */
import { useRef, useState } from "react";
import {
  Database,
  FileUp,
  Loader2,
  Play,
  RefreshCw,
  ShieldCheck,
  Table2,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Tabs, TabsList, TabsTrigger, TabsContent } from "@/components/ui/tabs";
import {
  assessFile,
  assessSql,
  fileFormatLabel,
  sqlListTables,
} from "@/api/trustConnectors";
import type { QualityPassport } from "@/api/trustGate";
import type { SqlDriver } from "@/api/trustConnectors";

interface Props {
  datasetId: string;
  busy: boolean;
  onAssess: (fn: () => Promise<QualityPassport>, label: string, source: "fhir" | "omop") => void;
}

export function ConnectorPanel({ datasetId, busy, onAssess }: Props) {
  return (
    <Tabs defaultValue="file" className="w-full">
      <TabsList>
        <TabsTrigger value="file" className="gap-1.5">
          <FileUp className="size-4" /> File upload
        </TabsTrigger>
        <TabsTrigger value="sql" className="gap-1.5">
          <Database className="size-4" /> SQL database
        </TabsTrigger>
      </TabsList>
      <TabsContent value="file">
        <FileConnector datasetId={datasetId} busy={busy} onAssess={onAssess} />
      </TabsContent>
      <TabsContent value="sql">
        <SqlConnector datasetId={datasetId} busy={busy} onAssess={onAssess} />
      </TabsContent>
    </Tabs>
  );
}

// ---------------------------------------------------------------------------
// File connector
// ---------------------------------------------------------------------------

function FileConnector({ datasetId, busy, onAssess }: Props) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [sheet, setSheet] = useState("");
  const [tableName, setTableName] = useState("");
  const [mappingText, setMappingText] = useState("");

  const accept = ".json,.ndjson,.jsonl,.csv,.tsv,.xlsx,.xls";

  const pickFile = (f: File) => setFile(f);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) pickFile(f);
  };

  const isFhirFormat = (f: File): boolean => {
    const name = f.name.toLowerCase();
    return name.endsWith(".json") || name.endsWith(".ndjson") || name.endsWith(".jsonl");
  };

  const handleAssess = () => {
    if (!file) return;
    const sourceModel: "fhir" | "omop" = isFhirFormat(file) ? "fhir" : "omop";
    onAssess(
      async () => {
        let mapping: Record<string, Record<string, string>> | undefined;
        if (mappingText.trim()) {
          try {
            mapping = JSON.parse(mappingText.trim());
          } catch {
            throw new Error("Column mapping is not valid JSON.");
          }
        }
        return assessFile({
          file,
          datasetId: datasetId || undefined,
          sheet: sheet.trim() || undefined,
          tableName: tableName.trim() || undefined,
          mapping,
        });
      },
      `file:${file.name}`,
      sourceModel,
    );
  };

  const fmt = file ? fileFormatLabel(file) : null;
  const isExcel = fmt === "Excel";

  return (
    <div className="flex flex-col gap-4 pt-4">
      <p className="text-xs text-muted-foreground">
        Upload a file to assess its data quality. Supported formats:{" "}
        <strong>JSON, NDJSON/JSONL, CSV, TSV, Excel (.xlsx)</strong>. FHIR
        resources are assessed directly; tabular data is mapped through OMOP CDM
        and assessed with OHDSI-DQD checks.
      </p>

      {/* Drop zone */}
      <div
        className={[
          "relative flex min-h-[140px] cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed p-6 text-center transition-colors",
          dragging
            ? "border-primary bg-primary/5"
            : "border-border hover:border-muted-foreground/60 hover:bg-muted/30",
        ].join(" ")}
        onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={handleDrop}
        onClick={() => inputRef.current?.click()}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => e.key === "Enter" && inputRef.current?.click()}
      >
        <input
          ref={inputRef}
          type="file"
          accept={accept}
          className="sr-only"
          onChange={(e) => { const f = e.target.files?.[0]; if (f) pickFile(f); }}
        />
        {file ? (
          <div className="flex flex-col items-center gap-2">
            <div className="flex items-center gap-2">
              <FileUp className="size-5 text-primary" />
              <span className="font-medium text-foreground">{file.name}</span>
              <Badge variant="secondary">{fmt}</Badge>
            </div>
            <span className="text-xs text-muted-foreground">
              {(file.size / 1024).toFixed(1)} KB, click to change
            </span>
          </div>
        ) : (
          <div className="flex flex-col items-center gap-2 text-muted-foreground">
            <FileUp className="size-8" />
            <p className="text-sm font-medium">Drop a file here or click to browse</p>
            <p className="text-xs">{accept}</p>
          </div>
        )}
      </div>

      {/* Options */}
      {file && (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {isExcel && (
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                Sheet (blank = all sheets)
              </label>
              <Input
                placeholder="e.g. Sheet1"
                value={sheet}
                onChange={(e) => setSheet(e.target.value)}
              />
            </div>
          )}
          {!isExcel && (
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">
                Table name override
              </label>
              <Input
                placeholder="defaults to filename stem"
                value={tableName}
                onChange={(e) => setTableName(e.target.value)}
              />
            </div>
          )}
        </div>
      )}

      {file && (
        <div>
          <label className="mb-1 block text-xs font-medium text-muted-foreground">
            Column mapping (optional), JSON:
            {" "}<code className="text-[10px]">&#123;"table": &#123;"omop_col": "source_col"&#125;&#125;</code>
          </label>
          <Textarea
            className="h-24 resize-none font-mono text-xs"
            placeholder={'{"person": {"gender_concept_id": "sex_code"}}'}
            value={mappingText}
            onChange={(e) => setMappingText(e.target.value)}
            spellCheck={false}
          />
        </div>
      )}

      <div>
        <Button
          onClick={handleAssess}
          disabled={busy || !file}
        >
          {busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
          Assess quality
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// SQL connector
// ---------------------------------------------------------------------------

const DEFAULT_PORTS: Record<SqlDriver, number> = {
  postgresql: 5432,
  mysql: 3306,
  sqlite: 0,
};

function SqlConnector({ datasetId, busy, onAssess }: Props) {
  const [driver, setDriver] = useState<SqlDriver>("postgresql");
  const [host, setHost] = useState("");
  const [port, setPort] = useState("");
  const [database, setDatabase] = useState("");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [query, setQuery] = useState("SELECT * FROM ");
  const [tableName, setTableName] = useState("");
  const [mappingText, setMappingText] = useState("");

  const [tables, setTables] = useState<string[] | null>(null);
  const [testingConn, setTestingConn] = useState(false);
  const [connError, setConnError] = useState<string | null>(null);

  const isSqlite = driver === "sqlite";

  const handleDriverChange = (d: string | null) => {
    if (!d) return;
    const drv = d as SqlDriver;
    setDriver(drv);
    if (!isSqlite && !port) setPort(String(DEFAULT_PORTS[drv]));
    setTables(null);
    setConnError(null);
  };

  const handleTestConnection = async () => {
    setTestingConn(true);
    setConnError(null);
    setTables(null);
    try {
      const list = await sqlListTables({
        driver,
        host: isSqlite ? "" : host,
        port: port ? parseInt(port, 10) : undefined,
        database,
        username,
        password,
      });
      setTables(list);
    } catch (e) {
      setConnError((e as Error).message);
    } finally {
      setTestingConn(false);
    }
  };

  const handleAssess = () => {
    onAssess(
      async () => {
        let mapping: Record<string, Record<string, string>> | undefined;
        if (mappingText.trim()) {
          try {
            mapping = JSON.parse(mappingText.trim());
          } catch {
            throw new Error("Column mapping is not valid JSON.");
          }
        }
        return assessSql({
          driver,
          host: isSqlite ? "" : host,
          port: port ? parseInt(port, 10) : undefined,
          database,
          username,
          password,
          query,
          tableName: tableName.trim() || undefined,
          mapping,
          datasetId: datasetId || undefined,
        });
      },
      `sql:${driver}:${database}`,
      "omop",
    );
  };

  const canAssess = query.trim().length > 6 && database.trim().length > 0 && (isSqlite || host.trim().length > 0);
  const canTest = database.trim().length > 0 && (isSqlite || host.trim().length > 0);

  const insertTable = (t: string) =>
    setQuery((q) => (q.trimEnd().endsWith("FROM") ? `${q} ${t}` : q));

  return (
    <div className="flex flex-col gap-4 pt-4">
      <p className="text-xs text-muted-foreground">
        Connect to a <strong>PostgreSQL</strong>, <strong>MySQL</strong>, or{" "}
        <strong>SQLite</strong> database and run a SELECT query. The result rows
        are mapped through OMOP CDM and assessed with OHDSI-DQD checks. Only
        hosts listed in <code className="text-[10px]">TRUST_GATE_SQL_ALLOWED_HOSTS</code> are permitted.
      </p>

      {/* Connection fields */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
        <div>
          <label className="mb-1 block text-xs font-medium text-muted-foreground">Driver</label>
          <Select value={driver} onValueChange={handleDriverChange}>
            <SelectTrigger>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="postgresql">PostgreSQL</SelectItem>
              <SelectItem value="mysql">MySQL</SelectItem>
              <SelectItem value="sqlite">SQLite (local file)</SelectItem>
            </SelectContent>
          </Select>
        </div>

        {!isSqlite && (
          <>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">Host</label>
              <Input
                placeholder="db.example.internal"
                value={host}
                onChange={(e) => setHost(e.target.value)}
                autoComplete="off"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">Port</label>
              <Input
                type="number"
                placeholder={String(DEFAULT_PORTS[driver])}
                value={port}
                onChange={(e) => setPort(e.target.value)}
              />
            </div>
          </>
        )}

        <div className={isSqlite ? "sm:col-span-2" : ""}>
          <label className="mb-1 block text-xs font-medium text-muted-foreground">
            {isSqlite ? "File path" : "Database"}
          </label>
          <Input
            placeholder={isSqlite ? "/data/cohort.db" : "my_database"}
            value={database}
            onChange={(e) => setDatabase(e.target.value)}
            autoComplete="off"
          />
        </div>

        {!isSqlite && (
          <>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">Username</label>
              <Input
                placeholder="readonly_user"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="username"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium text-muted-foreground">Password</label>
              <Input
                type="password"
                placeholder=""
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                autoComplete="current-password"
              />
            </div>
          </>
        )}
      </div>

      {/* Test connection */}
      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={testingConn || !canTest}
          onClick={handleTestConnection}
        >
          {testingConn
            ? <Loader2 className="size-4 animate-spin" />
            : <RefreshCw className="size-4" />}
          Test connection
        </Button>
        {tables !== null && !connError && (
          <span className="flex items-center gap-1 text-xs text-emerald-600">
            <ShieldCheck className="size-3.5" />
            Connected, {tables.length} table{tables.length !== 1 ? "s" : ""} visible
          </span>
        )}
        {connError && (
          <span className="text-xs text-destructive">{connError}</span>
        )}
      </div>

      {/* Table browser */}
      {tables && tables.length > 0 && (
        <div>
          <p className="mb-1.5 text-xs font-medium text-muted-foreground">
            <Table2 className="mr-1 inline size-3.5" />
            Available tables, click to insert into query
          </p>
          <div className="flex flex-wrap gap-1.5">
            {tables.map((t) => (
              <button
                key={t}
                type="button"
                onClick={() => insertTable(t)}
                className="rounded border border-border px-2 py-0.5 font-mono text-xs text-muted-foreground transition-colors hover:border-primary hover:text-primary"
              >
                {t}
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Query */}
      <div>
        <label className="mb-1 block text-xs font-medium text-muted-foreground">
          SELECT query (DDL / DML rejected)
        </label>
        <Textarea
          className="h-32 resize-none font-mono text-xs"
          placeholder="SELECT * FROM person LIMIT 5000"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          spellCheck={false}
        />
      </div>

      {/* Options */}
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <div>
          <label className="mb-1 block text-xs font-medium text-muted-foreground">
            Result table name (defaults to "query_result")
          </label>
          <Input
            placeholder="person"
            value={tableName}
            onChange={(e) => setTableName(e.target.value)}
          />
        </div>
      </div>

      <div>
        <label className="mb-1 block text-xs font-medium text-muted-foreground">
          Column mapping (optional), JSON:
          {" "}<code className="text-[10px]">&#123;"query_result": &#123;"omop_col": "source_col"&#125;&#125;</code>
        </label>
        <Textarea
          className="h-20 resize-none font-mono text-xs"
          placeholder={'{"query_result": {"gender_concept_id": "sex_code", "year_of_birth": "birth_year"}}'}
          value={mappingText}
          onChange={(e) => setMappingText(e.target.value)}
          spellCheck={false}
        />
      </div>

      <div>
        <Button onClick={handleAssess} disabled={busy || !canAssess}>
          {busy ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
          Assess quality
        </Button>
      </div>
    </div>
  );
}
