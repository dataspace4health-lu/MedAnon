import { useState, useEffect, useCallback, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { PageHeader } from '@/components/layout/PageHeader';
import { listConfigs, getConfigYaml, deleteConfig } from '@/api/medanon';
import type { ConfigMeta } from '@/api/medanon';
import { useAuth } from '@/context/AuthContext';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Badge } from '@/components/ui/badge';
import {
  Card,
  CardHeader,
  CardTitle,
  CardDescription,
  CardContent,
  CardFooter,
} from '@/components/ui/card';
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from '@/components/ui/collapsible';
import {
  Plus,
  Search,
  Shield,
  SlidersHorizontal,
  ChevronDown,
  Pencil,
  Copy,
  Trash2,
  Loader2,
  AlertCircle,
} from 'lucide-react';
import { toast } from 'sonner';
import { cn } from '@/lib/utils';

// ---------------------------------------------------------------------------
// ConfigCard
// ---------------------------------------------------------------------------

function ConfigCard({
  config,
  onEdit,
  onDuplicate,
  onDelete,
}: {
  config: ConfigMeta;
  onEdit: (name: string) => void;
  onDuplicate: (name: string) => void;
  onDelete: (name: string) => void;
}) {
  const { hasRole } = useAuth();
  const [yamlOpen, setYamlOpen] = useState(false);
  const [yaml, setYaml] = useState<string | null>(null);
  const [yamlLoading, setYamlLoading] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const handlePreview = useCallback(async () => {
    if (!yamlOpen && yaml === null) {
      setYamlLoading(true);
      try {
        const text = await getConfigYaml(config.name);
        setYaml(text);
      } catch (err) {
        toast.error('Failed to load YAML', { description: String(err) });
      } finally {
        setYamlLoading(false);
      }
    }
    setYamlOpen((v) => !v);
  }, [yamlOpen, yaml, config.name]);

  return (
    <Card className="flex flex-col">
      <CardHeader className="pb-2">
        <div className="flex items-start justify-between gap-2">
          <div className="flex items-center gap-2">
            {config.is_system ? (
              <Shield className="size-4 shrink-0 text-muted-foreground" />
            ) : (
              <SlidersHorizontal className="size-4 shrink-0 text-primary" />
            )}
            <CardTitle className="text-base leading-tight">{config.name}</CardTitle>
          </div>
          <Badge variant={config.is_system ? 'secondary' : 'outline'} className="shrink-0 text-xs">
            {config.is_system ? 'System' : 'Custom'}
          </Badge>
        </div>
        {config.description && (
          <CardDescription className="mt-1 text-xs leading-relaxed">
            {config.description}
          </CardDescription>
        )}
      </CardHeader>

      <CardContent className="flex-1 pb-2">
        <Collapsible open={yamlOpen} onOpenChange={() => {}}>
          <CollapsibleTrigger
            onClick={handlePreview}
            className="flex items-center gap-1 text-xs font-medium text-muted-foreground transition-colors hover:text-foreground"
          >
            {yamlLoading ? (
              <Loader2 className="size-3 animate-spin" />
            ) : (
              <ChevronDown
                className={cn('size-3 transition-transform', yamlOpen && 'rotate-180')}
              />
            )}
            {yamlOpen ? 'Hide YAML' : 'Preview YAML'}
          </CollapsibleTrigger>
          <CollapsibleContent>
            {yaml && (
              <pre className="mt-2 max-h-64 overflow-y-auto rounded-md border bg-muted/50 p-3 font-mono text-xs leading-relaxed">
                {yaml}
              </pre>
            )}
          </CollapsibleContent>
        </Collapsible>
      </CardContent>

      <CardFooter className="flex flex-wrap gap-2 pt-2">
        {/* Preview already handled above — actions below */}
        {hasRole('admin') && !config.is_system && (
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            onClick={() => onEdit(config.name)}
          >
            <Pencil className="size-3" />
            Edit
          </Button>
        )}
        {hasRole('admin') && (
          <Button
            variant="outline"
            size="sm"
            className="h-7 text-xs"
            onClick={() => onDuplicate(config.name)}
          >
            <Copy className="size-3" />
            Duplicate
          </Button>
        )}
        {hasRole('admin') && !config.is_system && (
          <>
            {confirmDelete ? (
              <div className="flex items-center gap-1">
                <span className="text-xs text-destructive">Delete?</span>
                <Button
                  variant="destructive"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => {
                    setConfirmDelete(false);
                    onDelete(config.name);
                  }}
                >
                  Yes
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  className="h-7 text-xs"
                  onClick={() => setConfirmDelete(false)}
                >
                  No
                </Button>
              </div>
            ) : (
              <Button
                variant="ghost"
                size="sm"
                className="h-7 text-xs text-destructive hover:bg-destructive/10 hover:text-destructive"
                onClick={() => setConfirmDelete(true)}
              >
                <Trash2 className="size-3" />
                Delete
              </Button>
            )}
          </>
        )}
      </CardFooter>
    </Card>
  );
}

// ---------------------------------------------------------------------------
// ConfigsPage
// ---------------------------------------------------------------------------

export default function ConfigsPage() {
  const navigate = useNavigate();
  const { hasRole } = useAuth();

  const [configs, setConfigs] = useState<ConfigMeta[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState('');

  const loadConfigs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await listConfigs();
      setConfigs(data);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadConfigs();
  }, [loadConfigs]);

  const filtered = useMemo(
    () =>
      configs.filter(
        (c) =>
          c.name.toLowerCase().includes(search.toLowerCase()) ||
          c.description.toLowerCase().includes(search.toLowerCase()),
      ),
    [configs, search],
  );

  const handleEdit = useCallback(
    (name: string) => navigate(`/configs/${name}/edit`),
    [navigate],
  );

  const handleDuplicate = useCallback(
    (name: string) => navigate(`/configs/new?from=${encodeURIComponent(name)}`),
    [navigate],
  );

  const handleDelete = useCallback(
    async (name: string) => {
      try {
        await deleteConfig(name);
        toast.success(`Config "${name}" deleted.`);
        setConfigs((prev) => prev.filter((c) => c.name !== name));
      } catch (err) {
        toast.error('Delete failed', { description: String(err) });
      }
    },
    [],
  );

  return (
    <div>
      <PageHeader
        title="Configuration Profiles"
        description="Manage de-identification rule sets. System profiles are read-only; create custom profiles for your specific use case."
      />

      {/* Toolbar */}
      <div className="mb-6 flex flex-col gap-3 rounded-xl border bg-card p-4 shadow-sm sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            placeholder="Search configs..."
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            className="pl-9"
          />
        </div>
        {hasRole('admin') && (
          <Button onClick={() => navigate('/configs/new')} className="shrink-0">
            <Plus className="size-4" />
            New Config
          </Button>
        )}
      </div>

      {/* Error */}
      {error && (
        <div className="mb-6 flex items-start gap-2.5 rounded-lg border border-destructive/40 bg-destructive/5 px-4 py-3 text-sm text-destructive">
          <AlertCircle className="mt-0.5 size-4 shrink-0" />
          {error}
        </div>
      )}

      {/* Loading */}
      {loading && (
        <div className="flex items-center gap-2.5 rounded-lg border bg-muted/40 px-4 py-3 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" />
          Loading configs...
        </div>
      )}

      {/* Grid */}
      {!loading && !error && (
        <>
          {filtered.length === 0 ? (
            <div className="flex flex-col items-center justify-center rounded-lg border border-dashed py-16 text-center text-muted-foreground">
              <SlidersHorizontal className="mb-3 size-10" />
              <p className="text-sm">
                {search ? 'No configs match your search.' : 'No configs found.'}
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
              {filtered.map((config) => (
                <ConfigCard
                  key={config.name}
                  config={config}
                  onEdit={handleEdit}
                  onDuplicate={handleDuplicate}
                  onDelete={handleDelete}
                />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
