import { useEffect, useMemo, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogTrigger,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  Sparkles,
  Loader2,
  Send,
  AlertCircle,
  Check,
  X,
  FileCode2,
  Database,
  RefreshCw,
  MessageSquarePlus,
  ChevronRight,
  ChevronDown,
  Zap,
  Brain,
  Cpu,
  Search,
  ListFilter,
  Layers,
  ListTree,
} from "lucide-react";
import { toast } from "sonner";
import { cn } from "@/lib/utils";
import {
  streamChat,
  buildFieldSketch,
  type ChatTurn,
  type FieldGranularity,
} from "@/api/agents";
import {
  buildServerFieldTree,
  clearServerFieldTreeCache,
  listResourceTypes,
} from "@/api/fhir";
import { AiStatusBadge } from "@/components/shared/AiStatusBadge";
import {
  type LocalRule,
  parseYamlIntoRules,
  deduplicateIncoming,
  dropParentRulesWithLeaves,
} from "./configConstants";
import { extractFieldPaths } from "./fieldTree";

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

interface ModelDef {
  value: string;
  label: string;
  tag: string;
  icon: React.ReactNode;
  description: string;
}

const MODELS: ModelDef[] = [
  {
    value: "ollama/gemma3:1b",
    label: "Gemma 3 · 1B",
    tag: "Fast",
    icon: <Zap className="size-3.5" />,
    description: "Fastest, good for simple rules",
  },
  {
    value: "ollama/gemma3:4b",
    label: "Gemma 3 · 4B",
    tag: "Balanced",
    icon: <Cpu className="size-3.5" />,
    description: "Better reasoning, moderate speed",
  },
  {
    value: "ollama/hf.co/unsloth/medgemma-4b-it-GGUF:Q4_K_M",
    label: "MedGemma · 4B",
    tag: "Medical",
    icon: <Brain className="size-3.5" />,
    description: "Healthcare-tuned, best for PHI/HIPAA",
  },
];

// ---------------------------------------------------------------------------
// Quick prompts ({type} is replaced with the selected scope at send time)
// ---------------------------------------------------------------------------

const SUGGESTED_PROMPTS = [
  "List every PII field in {type} and propose rules",
  "Build a HIPAA Safe Harbor config for {type}",
  "Which {type} fields are PII? Suggest an action for each",
  "Build a GDPR pseudonymization config for {type}",
];

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface ChatEntry {
  role: "user" | "assistant";
  content: string;
}

interface FieldTreeState {
  status: "idle" | "loading" | "done" | "error";
  summary: string;
  resourceTypes: string[];
  pathCount: number;
  /** paths grouped by resource type */
  grouped: Record<string, string[]>;
  error?: string;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Matches a fenced code block (``` or ~~~), with an optional language tag, and
// optional whitespace/newline after the opener. The body is captured. We scan
// ALL fenced blocks (g flag) and pick the first that contains a `rules:` key,
// so a model that emits a non-YAML block first (e.g. an example) doesn't hide
// the real proposal. Tolerant of small-model formatting drift: a missing
// language tag, `~~~` fences, or no newline immediately after the opener.
const FENCE_RE = /(?:```|~~~)[ \t]*(?:ya?ml)?[ \t]*\r?\n?([\s\S]*?)(?:```|~~~)/gi;
const RULES_KEY_RE = /(^|\n)\s*rules\s*:/;

function extractYamlBlock(text: string): string | null {
  for (const m of text.matchAll(FENCE_RE)) {
    const body = m[1];
    if (RULES_KEY_RE.test(body)) return body.trim();
  }
  // Fallback: an unfenced `rules:` block (model forgot the code fence). Grab
  // from the `rules:` line to the end so the proposal still surfaces.
  const bare = text.match(/(^|\n)(rules\s*:[\s\S]*)$/);
  if (bare) return bare[2].trim();
  return null;
}

function stripYamlBlock(text: string): string {
  let stripped = text;
  for (const m of text.matchAll(FENCE_RE)) {
    if (RULES_KEY_RE.test(m[1])) stripped = stripped.replace(m[0], "");
  }
  return stripped.trim();
}

// The server rejects a `field_context` over 20000 chars (string_too_long). Cap
// below that with headroom for the JSON envelope. Truncation is on whole `path :
// type` lines so the model never sees a half-path; the backend truncates again.
const FIELD_CONTEXT_MAX_CHARS = 19000;

/** Keep only the lines whose `Type.` prefix is in `types`. Each line looks like
 * `Patient.name.family : <string>`, so the resource type is the segment before
 * the first dot. */
function filterTreeByTypes(summary: string, types: Set<string>): string {
  return summary
    .split("\n")
    .filter((line) => {
      const dot = line.indexOf(".");
      if (dot === -1) return false;
      return types.has(line.slice(0, dot));
    })
    .join("\n");
}

/** Hard-cap the tree at the server limit, dropping whole trailing lines and
 * appending a marker so the model knows the list was clipped. */
function capFieldContext(summary: string): string {
  if (summary.length <= FIELD_CONTEXT_MAX_CHARS) return summary;
  const marker =
    "\n… (field list truncated, select fewer resource types for full coverage)";
  const budget = FIELD_CONTEXT_MAX_CHARS - marker.length;
  const kept: string[] = [];
  let used = 0;
  for (const line of summary.split("\n")) {
    if (used + line.length + 1 > budget) break;
    kept.push(line);
    used += line.length + 1;
  }
  return kept.join("\n") + marker;
}

/** Group a flat `Type.path : <type>` summary by resource type prefix. */
function groupPaths(summary: string): Record<string, string[]> {
  const groups: Record<string, string[]> = {};
  for (const line of summary.split("\n")) {
    const dot = line.indexOf(".");
    if (dot === -1) continue;
    const rtype = line.slice(0, dot);
    (groups[rtype] ??= []).push(line);
  }
  return groups;
}

// ---------------------------------------------------------------------------
// Sub-components
// ---------------------------------------------------------------------------

function SidebarLabel({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[10px] font-semibold uppercase tracking-wider text-muted-foreground">
      {children}
    </p>
  );
}

function ModelCard({
  model,
  selected,
  onSelect,
}: {
  model: ModelDef;
  selected: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      className={cn(
        "group w-full rounded-lg border px-3 py-2.5 text-left transition-all",
        selected
          ? "border-[#0072bc] bg-[#0072bc]/[0.07] shadow-sm ring-1 ring-[#0072bc]/20"
          : "border-border bg-background hover:border-[#0072bc]/40 hover:bg-accent/40",
      )}
    >
      <div className="flex items-center justify-between gap-1.5">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold text-foreground">
          <span
            className={cn(
              "transition-colors",
              selected
                ? "text-[#0072bc]"
                : "text-muted-foreground group-hover:text-[#0072bc]",
            )}
          >
            {model.icon}
          </span>
          {model.label}
        </span>
        <Badge
          variant="secondary"
          className={cn(
            "h-4 px-1.5 text-[9px] font-semibold",
            selected && "border-[#0072bc]/30 bg-[#0072bc]/10 text-[#0072bc]",
          )}
        >
          {model.tag}
        </Badge>
      </div>
      <p className="mt-1 text-[10px] leading-snug text-muted-foreground">
        {model.description}
      </p>
    </button>
  );
}

/** Selectable card for the field-granularity toggle (values-only vs whole). */
function GranularityCard({
  active,
  onSelect,
  icon,
  label,
  tag,
  description,
  example,
}: {
  active: boolean;
  onSelect: () => void;
  icon: React.ReactNode;
  label: string;
  tag: string;
  description: string;
  example: string;
}) {
  return (
    <button
      type="button"
      onClick={onSelect}
      aria-pressed={active}
      className={cn(
        "group w-full rounded-lg border px-3 py-2.5 text-left transition-all",
        active
          ? "border-[#0072bc] bg-[#0072bc]/[0.07] shadow-sm ring-1 ring-[#0072bc]/20"
          : "border-border bg-background hover:border-[#0072bc]/40 hover:bg-accent/40",
      )}
    >
      <div className="flex items-center justify-between gap-1.5">
        <span className="flex items-center gap-1.5 text-[11px] font-semibold text-foreground">
          <span
            className={cn(
              "transition-colors",
              active
                ? "text-[#0072bc]"
                : "text-muted-foreground group-hover:text-[#0072bc]",
            )}
          >
            {icon}
          </span>
          {label}
        </span>
        <Badge
          variant="secondary"
          className={cn(
            "h-4 px-1.5 text-[9px] font-semibold",
            active && "border-[#0072bc]/30 bg-[#0072bc]/10 text-[#0072bc]",
          )}
        >
          {tag}
        </Badge>
      </div>
      <p className="mt-1 text-[10px] leading-snug text-muted-foreground">
        {description}
      </p>
      <p className="mt-1 truncate font-mono text-[9px] text-muted-foreground/70">
        {example}
      </p>
    </button>
  );
}

/** Collapsible path list for one resource type. */
function ResourceTypeTree({
  resourceType,
  paths,
  defaultOpen = false,
}: {
  resourceType: string;
  paths: string[];
  defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="flex w-full items-center gap-1 rounded px-1 py-1 text-[11px] font-semibold text-foreground transition-colors hover:bg-accent/60">
        {open ? (
          <ChevronDown className="size-3 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronRight className="size-3 shrink-0 text-muted-foreground" />
        )}
        <span className="font-mono">{resourceType}</span>
        <Badge
          variant="secondary"
          className="ml-auto h-4 px-1.5 text-[9px] font-normal tabular-nums"
        >
          {paths.length}
        </Badge>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <ul className="mb-1 ml-2.5 mt-0.5 space-y-px border-l border-border/60 pl-2">
          {paths.map((line) => {
            const [path, type = ""] = line.split(" : ");
            const leaf = (path ?? line).split(".").slice(1).join(".");
            return (
              <li
                key={line}
                className="flex items-baseline justify-between gap-1.5 rounded px-1.5 py-px text-[10px] text-muted-foreground hover:bg-accent/40"
                title={path}
              >
                <span className="truncate font-mono">{leaf}</span>
                <span className="shrink-0 text-[9px] opacity-50">{type}</span>
              </li>
            );
          })}
        </ul>
      </CollapsibleContent>
    </Collapsible>
  );
}

// ---------------------------------------------------------------------------
// Main component
// ---------------------------------------------------------------------------

export function AiAssistantPanel({
  configYaml,
  fieldContext: uploadedFieldContext = "",
  existingRules = [],
  onImport,
}: {
  configYaml: string;
  fieldContext?: string;
  existingRules?: LocalRule[];
  onImport: (rules: LocalRule[]) => void;
}) {
  const [open, setOpen] = useState(false);
  // Default to the 4B "Balanced" model: the 1B model is too weak for structured
  // tasks like "list every PII field and propose YAML rules" and tends to reply
  // with a filler token. Users can still pick 1B for speed on simple questions.
  const [model, setModel] = useState(MODELS[1].value);
  // Field granularity, toggled BEFORE the AI runs so it knows how to shape
  // rules. "values" (default): one rule per identifying leaf sub-field
  // (Patient.name.family), keeps the FHIR skeleton. "whole": one rule on the
  // parent path (Patient.name), removes the entire element.
  const [granularity, setGranularity] = useState<FieldGranularity>("values");
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const [proposal, setProposal] = useState<{
    msgIndex: number;
    yaml: string;
  } | null>(null);

  /** All data-bearing resource types on the server (for the picker). */
  const [allResourceTypes, setAllResourceTypes] = useState<string[]>([]);
  /** Resource types the user selected to focus the question on. */
  const [selectedTypes, setSelectedTypes] = useState<Set<string>>(new Set());
  /** Filter text for the resource-type picker. */
  const [typeFilter, setTypeFilter] = useState("");
  /** Which sidebar tab is visible. */
  const [sideTab, setSideTab] = useState<"settings" | "tree">("settings");
  /** When true the field tree sent to the AI includes real sample values so a
   * LOCAL model can judge PII more accurately. The values never leave a local
   * model, the backend marks the request as a PHI payload and refuses any
   * non-local AI endpoint (fail-closed). */
  const [includeValues, setIncludeValues] = useState(false);
  /** Opt-in: send the compact, PHI-safe SCHEMA SKETCH (one line per distinct
   * leaf: type, presence frequency, cardinality, shape/enum digest) instead of
   * the flat first-value field tree. Built server-side (/v1/ai/field-sketch),
   * budget-ranked so identifiers survive truncation. Default off until it is
   * validated in real use; the flat tree remains the default path. */
  const [useSketch, setUseSketch] = useState(false);
  const [sketch, setSketch] = useState<{
    status: "idle" | "loading" | "done" | "error";
    text: string;
    truncated: boolean;
    error?: string;
  }>({ status: "idle", text: "", truncated: false });

  const [fieldTree, setFieldTree] = useState<FieldTreeState>({
    status: "idle",
    summary: "",
    resourceTypes: [],
    pathCount: 0,
    grouped: {},
  });
  const [loadProgress, setLoadProgress] = useState<{
    loaded: number;
    total: number;
  } | null>(null);

  const loadingRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-load field tree on first open.
  useEffect(() => {
    if (!open) return;
    if (fieldTree.status !== "idle") return;
    if (loadingRef.current) return;
    loadTree();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Loads the FULL field tree for every data-bearing type (used to populate the
  // scope picker and the Field Tree tab). The tree actually SENT to the AI is
  // narrowed to the user's selected types in `activeFieldContext`, both to
  // focus the model and to stay under the server's field_context size cap.
  const loadTree = async (force = false) => {
    if (loadingRef.current) return;
    loadingRef.current = true;
    setFieldTree((s) => ({ ...s, status: "loading", error: undefined }));
    setLoadProgress({ loaded: 0, total: 1 });
    try {
      if (force) clearServerFieldTreeCache();

      // Type list first so the picker is complete even before sampling finishes.
      const allTypes = await listResourceTypes();
      setAllResourceTypes(allTypes);

      const result = await buildServerFieldTree(
        (r) => extractFieldPaths(r, { includeValues }),
        {
          force,
          includeValues,
          samplePerType: 10,
          onlyTypes: allTypes.length > 0 ? allTypes : undefined,
          onProgress: (loaded, total) => setLoadProgress({ loaded, total }),
        },
      );
      setFieldTree({
        status: "done",
        summary: result.summary,
        resourceTypes: result.resourceTypes,
        pathCount: result.pathCount,
        grouped: groupPaths(result.summary),
      });
    } catch (e) {
      setFieldTree({
        status: "error",
        summary: "",
        resourceTypes: [],
        pathCount: 0,
        grouped: {},
        error: e instanceof Error ? e.message : String(e),
      });
    } finally {
      loadingRef.current = false;
      setLoadProgress(null);
    }
  };

  // Rebuild the tree when the values toggle flips (the cached paths-only and
  // values trees are not interchangeable). Skip the initial mount, the
  // open-effect already performs the first load.
  const didMountValues = useRef(false);
  useEffect(() => {
    if (!didMountValues.current) {
      didMountValues.current = true;
      return;
    }
    if (open && fieldTree.status !== "idle") loadTree(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [includeValues]);

  // Fetch the compact sketch when the user opts in. Auto-picks the source:
  // uploaded example resources when the panel has them, else samples the source
  // server by type (the server does the sampling + PHI-safe compaction). Refires
  // when the type scope or the values toggle changes.
  const selectedTypesKey = useMemo(
    () => [...selectedTypes].sort().join(","),
    [selectedTypes],
  );
  useEffect(() => {
    if (!open || !useSketch) return;
    const types = selectedTypes.size
      ? [...selectedTypes]
      : allResourceTypes.length
        ? allResourceTypes
        : fieldTree.resourceTypes;
    if (types.length === 0) return;
    let cancelled = false;
    setSketch((s) => ({ ...s, status: "loading", error: undefined }));
    buildFieldSketch({ resourceTypes: types, includeValues })
      .then((res) => {
        if (cancelled) return;
        if (res.source === "error" || !res.sketch) {
          setSketch({
            status: "error",
            text: "",
            truncated: false,
            error: res.detail || "sketch unavailable",
          });
          return;
        }
        setSketch({ status: "done", text: res.sketch, truncated: res.truncated });
      })
      .catch((e) => {
        if (cancelled) return;
        setSketch({
          status: "error",
          text: "",
          truncated: false,
          error: e instanceof Error ? e.message : String(e),
        });
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    open,
    useSketch,
    selectedTypesKey,
    includeValues,
    allResourceTypes.length,
    fieldTree.resourceTypes.length,
  ]);

  const scrollToBottom = () => {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    });
  };

  // The grounding field tree sent to the AI. When the user has selected
  // resource types in the scope picker, narrow the tree to ONLY those types.
  // This focuses the model on what the user cares about AND keeps the payload
  // under the server's 20000-char `field_context` cap (the full multi-resource
  // tree easily exceeds it). With no selection, send the whole tree. A final
  // whole-line truncation guarantees the request can never be rejected with
  // `string_too_long`; the backend truncates again at its own limit.
  const activeFieldContext = useMemo(() => {
    // Opt-in compact sketch: already server-scoped to the selected types and
    // budget-ranked, so it is used verbatim (the flat-tree filter/cap assume the
    // `Type.path : type` line format and would strip the sketch's headers). A
    // sketch error falls through to the flat tree so grounding never silently
    // disappears.
    if (useSketch && sketch.status === "done" && sketch.text) {
      return sketch.text;
    }
    const base =
      fieldTree.status === "done" && fieldTree.summary
        ? fieldTree.summary
        : uploadedFieldContext;
    if (!base) return base;
    const scoped =
      selectedTypes.size > 0 ? filterTreeByTypes(base, selectedTypes) : base;
    return capFieldContext(scoped);
  }, [
    useSketch,
    sketch.status,
    sketch.text,
    fieldTree.status,
    fieldTree.summary,
    uploadedFieldContext,
    selectedTypes,
  ]);

  // Number of field lines actually sent to the AI (excludes the truncation
  // marker line). Reflects the scope filter so the footer is honest.
  const activeFieldCount = useMemo(
    () =>
      activeFieldContext
        ? activeFieldContext
            .split("\n")
            .filter((l) => l.trim() && !l.startsWith("…") && !l.startsWith("#"))
            .length
        : 0,
    [activeFieldContext],
  );

  const toggleType = (t: string) =>
    setSelectedTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });

  const pickerTypes = allResourceTypes.length
    ? allResourceTypes
    : fieldTree.resourceTypes;

  const filteredTypes = useMemo(() => {
    const q = typeFilter.trim().toLowerCase();
    return q ? pickerTypes.filter((t) => t.toLowerCase().includes(q)) : pickerTypes;
  }, [pickerTypes, typeFilter]);

  const scopeText = [...selectedTypes].join(", ");

  /** Build the question, injecting the selected resource-type scope. */
  const buildQuestion = (raw: string) => {
    if (selectedTypes.size === 0) return raw.replace("{type}", "my resources");
    if (raw.includes("{type}")) return raw.replaceAll("{type}", scopeText);
    return `${raw}\n\nScope your answer to these FHIR resource types only: ${scopeText}.`;
  };

  const send = async (rawQuestion: string) => {
    const question = buildQuestion(rawQuestion.trim());
    if (!question || streaming) return;
    setErr(null);
    setInput("");
    setProposal(null);

    const history: ChatTurn[] = messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    const placeholderIndex = messages.length + 1;
    setMessages((prev) => [
      ...prev,
      { role: "user", content: question },
      { role: "assistant", content: "" },
    ]);
    scrollToBottom();
    setStreaming(true);

    let full = "";
    try {
      for await (const chunk of streamChat({
        question,
        config_yaml: configYaml,
        history,
        model,
        field_context: activeFieldContext || undefined,
        include_values: includeValues,
        granularity,
      })) {
        full += chunk;
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = { role: "assistant", content: full };
          return next;
        });
        scrollToBottom();
      }

      const yaml = extractYamlBlock(full);
      if (yaml) setProposal({ msgIndex: placeholderIndex, yaml });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      setMessages((prev) =>
        prev[prev.length - 1]?.content === "" ? prev.slice(0, -1) : prev,
      );
    } finally {
      setStreaming(false);
    }
  };

  const handleApprove = () => {
    if (!proposal) return;
    const { rules, error } = parseYamlIntoRules(proposal.yaml);
    if (error) {
      setErr(error);
      return;
    }
    // Values-only contract: even when the model leaks a parent rule alongside
    // its leaves (small models do this), keep only the leaves so the FHIR
    // structure is preserved. A parent with no proposed leaves is kept.
    let incoming = rules;
    let prunedParents = 0;
    if (granularity === "values") {
      const { kept, dropped } = dropParentRulesWithLeaves(rules);
      incoming = kept;
      prunedParents = dropped.length;
    }
    const { added, skipped } = deduplicateIncoming(incoming, existingRules);
    if (added.length === 0) {
      toast.warning("All proposed rules already exist, nothing added.");
      setProposal(null);
      return;
    }
    onImport(added);
    const parts = [`Added ${added.length} rule${added.length !== 1 ? "s" : ""}`];
    if (prunedParents > 0)
      parts.push(
        `dropped ${prunedParents} parent rule${prunedParents !== 1 ? "s" : ""} (values-only)`,
      );
    if (skipped.length > 0)
      parts.push(
        `skipped ${skipped.length} duplicate${skipped.length !== 1 ? "s" : ""}`,
      );
    toast.success(parts.join("; ") + ".");
    setProposal(null);
  };

  const handleDeny = () => {
    setProposal(null);
    toast.message("Proposal dismissed.");
  };

  const resetChat = () => {
    setMessages([]);
    setProposal(null);
    setErr(null);
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger
        render={
          <button className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground" />
        }
      >
        <Sparkles className="size-3" />
        AI Assistant
      </DialogTrigger>

      <DialogContent className="w-[96vw]! max-w-[1600px]! sm:max-w-[1600px]! h-[88vh] grid-rows-[auto_1fr_auto] overflow-hidden p-0 gap-0">
        {/* ── Header ─────────────────────────────────────────────── */}
        <DialogHeader className="border-b bg-gradient-to-r from-[#0072bc]/[0.06] to-transparent px-6 py-4">
          <div className="flex items-center justify-between gap-2">
            <DialogTitle className="flex items-center gap-2.5 text-sm font-semibold">
              <span className="flex size-8 items-center justify-center rounded-lg bg-[#0072bc]/10">
                <Sparkles className="size-4 text-[#0072bc]" />
              </span>
              AI Config Assistant
            </DialogTitle>
            <AiStatusBadge />
          </div>
          <DialogDescription className="mt-0.5 text-xs text-muted-foreground">
            Pick resource types to focus on, then ask a question or describe the
            config you want, approve the proposed YAML into your builder.
          </DialogDescription>
        </DialogHeader>

        <div className="flex min-h-0 min-w-0 px-6 py-3">
          {/* ── Left sidebar ───────────────────────────────────────── */}
          <aside className="flex w-64 shrink-0 flex-col overflow-hidden rounded-l-md border-y border-l border-r bg-muted/20">
            {/* Tab switcher */}
            <div className="flex shrink-0 border-b">
              {(
                [
                  { id: "settings", label: "Setup", icon: ListFilter },
                  { id: "tree", label: "Field Tree", icon: Database },
                ] as const
              ).map((tab) => (
                <button
                  key={tab.id}
                  onClick={() => setSideTab(tab.id)}
                  className={cn(
                    "flex flex-1 items-center justify-center gap-1.5 py-2.5 text-[11px] font-semibold transition-colors",
                    sideTab === tab.id
                      ? "border-b-2 border-[#0072bc] text-[#0072bc]"
                      : "border-b-2 border-transparent text-muted-foreground hover:text-foreground",
                  )}
                >
                  <tab.icon className="size-3.5" />
                  {tab.label}
                </button>
              ))}
            </div>

            <div className="flex-1 overflow-y-auto p-4">
              {/* ── Setup tab ────────────────────────────────────── */}
              {sideTab === "settings" && (
                <div className="space-y-6">
                  {/* Model picker */}
                  <div className="space-y-2.5">
                    <SidebarLabel>Model</SidebarLabel>
                    <div className="space-y-2">
                      {MODELS.map((m) => (
                        <ModelCard
                          key={m.value}
                          model={m}
                          selected={model === m.value}
                          onSelect={() => setModel(m.value)}
                        />
                      ))}
                    </div>
                  </div>

                  {/* Field granularity */}
                  <div className="space-y-2.5">
                    <SidebarLabel>Field treatment</SidebarLabel>
                    <div className="space-y-2">
                      <GranularityCard
                        active={granularity === "values"}
                        onSelect={() => setGranularity("values")}
                        icon={<ListTree className="size-3.5" />}
                        label="Values only"
                        tag="Leaves"
                        description="One rule per leaf sub-field (name.family, name.given). Keeps FHIR structure, blanks values."
                        example="Patient.name.family · Patient.name.given"
                      />
                      <GranularityCard
                        active={granularity === "whole"}
                        onSelect={() => setGranularity("whole")}
                        icon={<Layers className="size-3.5" />}
                        label="Whole field"
                        tag="Parent"
                        description="One rule on the parent path. Removes the entire element."
                        example="Patient.name"
                      />
                    </div>
                  </div>

                  {/* Resource type scope */}
                  <div className="space-y-2.5">
                    <div className="flex items-center justify-between">
                      <SidebarLabel>Resource scope</SidebarLabel>
                      {selectedTypes.size > 0 && (
                        <Badge
                          variant="secondary"
                          className="h-4 bg-[#0072bc]/10 px-1.5 text-[9px] font-semibold text-[#0072bc]"
                        >
                          {selectedTypes.size}
                        </Badge>
                      )}
                    </div>

                    {fieldTree.status === "loading" &&
                      pickerTypes.length === 0 && (
                        <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                          <Loader2 className="size-3 animate-spin" />
                          {loadProgress
                            ? `Probing types… ${loadProgress.loaded}/${loadProgress.total}`
                            : "Loading types…"}
                        </div>
                      )}

                    {pickerTypes.length > 0 && (
                      <>
                        <div className="relative">
                          <Search className="pointer-events-none absolute left-2 top-1/2 size-3 -translate-y-1/2 text-muted-foreground" />
                          <Input
                            value={typeFilter}
                            onChange={(e) => setTypeFilter(e.target.value)}
                            placeholder="Filter types…"
                            className="h-7 pl-7 text-[11px]"
                          />
                        </div>
                        <div className="max-h-44 space-y-px overflow-y-auto rounded-md border bg-background/60 p-1">
                          {filteredTypes.length === 0 ? (
                            <p className="px-1.5 py-1 text-[10px] text-muted-foreground">
                              No match.
                            </p>
                          ) : (
                            filteredTypes.map((t) => {
                              const checked = selectedTypes.has(t);
                              return (
                                <label
                                  key={t}
                                  className={cn(
                                    "flex cursor-pointer items-center gap-2 rounded px-1.5 py-1 text-[11px] transition-colors",
                                    checked
                                      ? "bg-[#0072bc]/[0.07] text-foreground"
                                      : "hover:bg-accent/60",
                                  )}
                                >
                                  <input
                                    type="checkbox"
                                    checked={checked}
                                    onChange={() => toggleType(t)}
                                    className="size-3 accent-[#0072bc]"
                                  />
                                  <span className="font-mono font-medium">
                                    {t}
                                  </span>
                                </label>
                              );
                            })
                          )}
                        </div>
                        <p className="text-[10px] leading-snug text-muted-foreground">
                          {selectedTypes.size === 0
                            ? "No filter, the AI considers every resource type."
                            : "The AI focuses on the selected types."}
                        </p>
                      </>
                    )}

                    {fieldTree.status === "error" &&
                      pickerTypes.length === 0 && (
                        <div className="flex items-center justify-between gap-1.5 text-[11px] text-red-600">
                          <span className="flex items-center gap-1">
                            <AlertCircle className="size-3" /> Unavailable
                          </span>
                          <button
                            onClick={() => loadTree(true)}
                            className="underline"
                          >
                            retry
                          </button>
                        </div>
                      )}
                  </div>

                  {/* Quick prompts */}
                  <div className="space-y-2.5">
                    <SidebarLabel>Quick prompts</SidebarLabel>
                    <div className="space-y-1.5">
                      {SUGGESTED_PROMPTS.map((p) => {
                        const label = p.replace(
                          "{type}",
                          selectedTypes.size > 0 ? scopeText : "my resources",
                        );
                        return (
                          <button
                            key={p}
                            onClick={() => send(p)}
                            disabled={streaming}
                            className="block w-full rounded-md border bg-background px-2.5 py-2 text-left text-[11px] leading-snug text-foreground/80 transition-colors hover:border-[#0072bc]/40 hover:bg-accent hover:text-foreground disabled:opacity-50"
                          >
                            {label}
                          </button>
                        );
                      })}
                    </div>
                  </div>
                </div>
              )}

              {/* ── Field Tree tab ───────────────────────────────── */}
              {sideTab === "tree" && (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <SidebarLabel>
                      {fieldTree.status === "done"
                        ? `${fieldTree.pathCount} fields · ${fieldTree.resourceTypes.length} types`
                        : "FHIR field tree"}
                    </SidebarLabel>
                    {fieldTree.status === "done" && (
                      <button
                        onClick={() => loadTree(true)}
                        title="Refresh from server"
                        className="text-muted-foreground transition-colors hover:text-foreground"
                      >
                        <RefreshCw className="size-3" />
                      </button>
                    )}
                  </div>

                  {/* Local-AI values toggle. Including real sample values lets a
                      local model judge PII far more accurately; the backend
                      marks the call PHI and refuses any non-local endpoint, so
                      values never leave a self-hosted model. */}
                  <label
                    className="flex cursor-pointer items-start gap-2 rounded-md border bg-muted/20 p-2"
                    title="Send a truncated sample value per field. Only ever reaches a local model, the server refuses non-local AI endpoints for value-bearing requests."
                  >
                    <input
                      type="checkbox"
                      checked={includeValues}
                      onChange={(e) => setIncludeValues(e.target.checked)}
                      className="mt-0.5 size-3.5 shrink-0 accent-[#0072bc]"
                    />
                    <span className="text-[11px] leading-snug">
                      <span className="font-medium text-foreground">
                        Let local AI read sample values
                      </span>
                      <span className="block text-muted-foreground">
                        More accurate PII calls. Values reach a local model only
                       , non-local endpoints are refused.
                      </span>
                    </span>
                  </label>

                  {/* Opt-in compact schema sketch. Collapses many instances into
                      one line per distinct leaf (type, frequency, cardinality,
                      shape/enum digest) so ALL fields of the selected types fit
                      the context with example shapes. Built + PHI-masked
                      server-side; identifiers survive budget truncation. */}
                  <label
                    className="flex cursor-pointer items-start gap-2 rounded-md border bg-muted/20 p-2"
                    title="Send a compact per-type schema (all fields + example shapes) instead of the flat field list. Smaller context, full field coverage."
                  >
                    <input
                      type="checkbox"
                      checked={useSketch}
                      onChange={(e) => setUseSketch(e.target.checked)}
                      className="mt-0.5 size-3.5 shrink-0 accent-[#0072bc]"
                    />
                    <span className="text-[11px] leading-snug">
                      <span className="font-medium text-foreground">
                        Compact schema context
                      </span>
                      <span className="block text-muted-foreground">
                        All fields of the selected types with example shapes, in a
                        smaller context. Falls back to the field list on error.
                      </span>
                    </span>
                  </label>

                  {useSketch && sketch.status === "loading" && (
                    <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                      <Loader2 className="size-3 animate-spin" />
                      Building schema sketch…
                    </div>
                  )}
                  {useSketch && sketch.status === "done" && (
                    <p className="text-[11px] text-muted-foreground">
                      Schema sketch active · {activeFieldCount} fields
                      {sketch.truncated
                        ? " · scope trimmed to fit, select fewer types for full coverage"
                        : ""}
                    </p>
                  )}
                  {useSketch && sketch.status === "error" && (
                    <p className="text-[11px] text-amber-600 dark:text-amber-500">
                      Sketch unavailable ({sketch.error}); using the field list.
                    </p>
                  )}

                  {fieldTree.status === "loading" && (
                    <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
                      <Loader2 className="size-3 animate-spin" />
                      {loadProgress
                        ? `Sampling ${loadProgress.loaded}/${loadProgress.total}…`
                        : "Loading…"}
                    </div>
                  )}

                  {fieldTree.status === "error" && (
                    <div className="space-y-1.5 rounded-md border border-red-200 bg-red-50 p-2 text-[11px] text-red-700 dark:border-red-900/40 dark:bg-red-950/30 dark:text-red-400">
                      <div className="flex items-start gap-1">
                        <AlertCircle className="mt-px size-3 shrink-0" />
                        {fieldTree.error ?? "Failed to load field tree"}
                      </div>
                      <button
                        onClick={() => loadTree(true)}
                        className="text-[10px] underline"
                      >
                        Retry
                      </button>
                    </div>
                  )}

                  {fieldTree.status === "idle" && (
                    <p className="text-[11px] text-muted-foreground">
                      Field tree loads automatically when the assistant opens.
                    </p>
                  )}

                  {fieldTree.status === "done" && (
                    <div className="space-y-0.5">
                      {Object.entries(fieldTree.grouped)
                        .sort(([a], [b]) => a.localeCompare(b))
                        .map(([rtype, paths], idx) => (
                          <ResourceTypeTree
                            key={rtype}
                            resourceType={rtype}
                            paths={paths}
                            defaultOpen={idx === 0}
                          />
                        ))}
                    </div>
                  )}
                </div>
              )}
            </div>

            {/* New conversation footer */}
            {messages.length > 0 && (
              <div className="shrink-0 border-t p-4">
                <button
                  onClick={resetChat}
                  className="flex w-full items-center justify-center gap-1.5 rounded-md border bg-background py-2 text-[11px] font-medium text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
                >
                  <MessageSquarePlus className="size-3.5" />
                  New conversation
                </button>
              </div>
            )}
          </aside>

          {/* ── Main conversation area ─────────────────────────────── */}
          <div className="flex flex-1 flex-col min-w-0 overflow-hidden rounded-r-md border-y border-r">
            {/* Selected types pill strip */}
            {selectedTypes.size > 0 && (
              <div className="flex flex-wrap items-center gap-1.5 border-b bg-muted/20 px-6 py-2.5">
                <span className="text-[10px] font-medium text-muted-foreground">
                  Focus:
                </span>
                {[...selectedTypes].map((t) => (
                  <Badge
                    key={t}
                    variant="secondary"
                    className="h-5 gap-1 border-[#0072bc]/20 bg-[#0072bc]/10 px-1.5 text-[10px] font-mono font-semibold text-[#0072bc]"
                  >
                    {t}
                    <button
                      onClick={() => toggleType(t)}
                      className="opacity-60 transition-opacity hover:opacity-100"
                      aria-label={`Remove ${t} from scope`}
                    >
                      <X className="size-2.5" />
                    </button>
                  </Badge>
                ))}
                <button
                  onClick={() => setSelectedTypes(new Set())}
                  className="ml-auto text-[10px] text-muted-foreground underline-offset-2 hover:underline"
                >
                  clear all
                </button>
              </div>
            )}

            {/* Messages */}
            <div ref={scrollRef} className="flex-1 overflow-y-auto px-6 py-5 space-y-4">
              {messages.length === 0 ? (
                <div className="flex h-full flex-col items-center justify-center gap-3 text-center text-muted-foreground">
                  <span className="flex size-14 items-center justify-center rounded-2xl bg-[#0072bc]/[0.07]">
                    <Sparkles className="size-7 text-[#0072bc]/50" />
                  </span>
                  <div className="space-y-1">
                    <p className="text-sm font-semibold text-foreground">
                      Start a conversation
                    </p>
                    <p className="max-w-xs text-xs">
                      Pick resource types on the left to focus your request, then
                      ask a question or click a quick prompt.
                    </p>
                  </div>
                </div>
              ) : (
                messages.map((m, i) => {
                  const isProposalMsg = proposal?.msgIndex === i;
                  const prose =
                    m.role === "assistant" && extractYamlBlock(m.content)
                      ? stripYamlBlock(m.content)
                      : m.content;
                  return (
                    <div key={i} className="space-y-2">
                      <div
                        className={cn(
                          "rounded-2xl px-4 py-3 text-xs leading-relaxed",
                          m.role === "user"
                            ? "ml-12 whitespace-pre-wrap bg-[#0072bc] text-white"
                            : "mr-8 border bg-background text-foreground shadow-sm",
                        )}
                      >
                        <span
                          className={cn(
                            "mb-1.5 block text-[10px] font-bold uppercase tracking-widest",
                            m.role === "user"
                              ? "text-white/60"
                              : "text-[#0072bc]/70",
                          )}
                        >
                          {m.role === "user" ? "You" : "AI"}
                        </span>
                        {m.role === "assistant" ? (
                          prose ? (
                            <ReactMarkdown
                              remarkPlugins={[remarkGfm]}
                              components={{
                                p: ({ children }) => <p className="mb-2 last:mb-0">{children}</p>,
                                ul: ({ children }) => <ul className="mb-2 ml-4 list-disc space-y-0.5 last:mb-0">{children}</ul>,
                                ol: ({ children }) => <ol className="mb-2 ml-4 list-decimal space-y-0.5 last:mb-0">{children}</ol>,
                                li: ({ children }) => <li className="leading-relaxed">{children}</li>,
                                strong: ({ children }) => <strong className="font-semibold text-foreground">{children}</strong>,
                                em: ({ children }) => <em className="italic">{children}</em>,
                                h1: ({ children }) => <p className="mb-1.5 font-semibold text-foreground">{children}</p>,
                                h2: ({ children }) => <p className="mb-1.5 font-semibold text-foreground">{children}</p>,
                                h3: ({ children }) => <p className="mb-1 font-semibold text-foreground">{children}</p>,
                                code: ({ children, className }) =>
                                  className ? null : (
                                    <code className="rounded bg-muted px-1 py-px font-mono text-[10px]">{children}</code>
                                  ),
                                pre: () => null,
                                hr: () => <hr className="my-2 border-border" />,
                                blockquote: ({ children }) => (
                                  <blockquote className="mb-2 border-l-2 border-[#0072bc]/40 pl-3 text-muted-foreground">{children}</blockquote>
                                ),
                                table: ({ children }) => (
                                  <div className="mb-2 overflow-x-auto">
                                    <table className="w-full border-collapse text-[11px]">{children}</table>
                                  </div>
                                ),
                                th: ({ children }) => <th className="border border-border bg-muted/40 px-2 py-1 text-left font-semibold">{children}</th>,
                                td: ({ children }) => <td className="border border-border px-2 py-1">{children}</td>,
                              }}
                            >
                              {prose}
                            </ReactMarkdown>
                          ) : (
                            <span className="flex items-center gap-1.5 text-muted-foreground">
                              <Loader2 className="size-3.5 animate-spin" />
                              Thinking…
                            </span>
                          )
                        ) : (
                          prose
                        )}
                      </div>

                      {isProposalMsg && proposal && (
                        <div className="mr-8 overflow-hidden rounded-xl border border-[#0072bc]/30 bg-[#0072bc]/[0.03] shadow-sm">
                          <div className="flex items-center gap-1.5 border-b border-[#0072bc]/15 bg-[#0072bc]/[0.06] px-3 py-2 text-[11px] font-semibold text-[#0072bc]">
                            <FileCode2 className="size-3.5" />
                            Proposed rules, review &amp; edit before approving
                          </div>
                          <div className="space-y-3 p-3">
                            <Textarea
                              value={proposal.yaml}
                              onChange={(e) =>
                                setProposal({
                                  ...proposal,
                                  yaml: e.target.value,
                                })
                              }
                              className="min-h-48 resize-y bg-background font-mono text-[11px] leading-relaxed"
                              spellCheck={false}
                            />
                            <div className="flex gap-2">
                              <Button
                                size="sm"
                                className="h-8 gap-1.5 bg-[#0072bc] text-xs hover:bg-[#005fa3]"
                                onClick={handleApprove}
                              >
                                <Check className="size-3.5" />
                                Approve &amp; add rules
                              </Button>
                              <Button
                                variant="ghost"
                                size="sm"
                                className="h-8 gap-1.5 text-xs"
                                onClick={handleDeny}
                              >
                                <X className="size-3.5" />
                                Deny
                              </Button>
                            </div>
                          </div>
                        </div>
                      )}
                    </div>
                  );
                })
              )}
            </div>

            {err && (
              <p className="flex items-center gap-1.5 border-t bg-destructive/5 px-6 py-3 text-xs text-destructive">
                <AlertCircle className="size-3.5 shrink-0" /> {err}
              </p>
            )}
          </div>
        </div>

        {/* ── Footer (composer) ──────────────────────────────────── */}
        <DialogFooter className="mx-0! mb-0! flex-row! items-center! gap-2.5 rounded-b-xl border-t px-6 py-4 sm:justify-stretch!">
          {fieldTree.status === "done" && (
            <span
              className="flex shrink-0 items-center gap-1 text-[10px] text-muted-foreground"
              title={
                selectedTypes.size > 0
                  ? "PHI-free field tree grounding the assistant (scoped to selected types)"
                  : "PHI-free field tree grounding the assistant"
              }
            >
              <Database className="size-3" />
              {activeFieldCount} fields
              {selectedTypes.size > 0 && fieldTree.pathCount > activeFieldCount
                ? ` of ${fieldTree.pathCount}`
                : ""}
            </span>
          )}
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                send(input.trim());
              }
            }}
            placeholder={
              fieldTree.status === "loading"
                ? "Field tree loading, you can type now…"
                : selectedTypes.size > 0
                  ? `Ask about ${scopeText}…`
                  : "Ask a question or describe the config you want…"
            }
            className="h-9 flex-1 text-xs"
            disabled={streaming}
          />
          <Button
            size="sm"
            className="h-9 shrink-0 gap-1.5 bg-[#0072bc] text-xs hover:bg-[#005fa3]"
            onClick={() => send(input.trim())}
            disabled={streaming || !input.trim()}
          >
            {streaming ? (
              <Loader2 className="size-3.5 animate-spin" />
            ) : (
              <Send className="size-3.5" />
            )}
            Send
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
