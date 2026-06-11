import { useEffect, useState } from "react";
import { Sparkles, WifiOff, AlertTriangle, Loader2 } from "lucide-react";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { cn } from "@/lib/utils";
import { getAgentStatus, type AgentStatus } from "@/api/agents";

type State = "loading" | "up" | "down" | "degraded" | "disabled";

function deriveState(s: AgentStatus | null, errored: boolean): State {
  if (errored) return "down";
  if (!s) return "loading";
  if (!s.enabled) return "disabled";
  const cbState = String(s.circuit_breaker?.state ?? "").toLowerCase();
  if (cbState === "open") return "degraded";
  return "up";
}

const META: Record<
  State,
  { label: string; cls: string; icon: React.ReactNode }
> = {
  loading: {
    label: "Checking AI…",
    cls: "border-muted bg-muted/40 text-muted-foreground",
    icon: <Loader2 className="size-3 animate-spin" />,
  },
  up: {
    label: "AI Online",
    cls: "border-emerald-200 bg-emerald-50 text-emerald-700 dark:border-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-300",
    icon: <Sparkles className="size-3" />,
  },
  degraded: {
    label: "AI Degraded",
    cls: "border-amber-200 bg-amber-50 text-amber-700 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300",
    icon: <AlertTriangle className="size-3" />,
  },
  down: {
    label: "AI Offline",
    cls: "border-red-200 bg-red-50 text-red-700 dark:border-red-900 dark:bg-red-950/40 dark:text-red-400",
    icon: <WifiOff className="size-3" />,
  },
  disabled: {
    label: "AI Disabled",
    cls: "border-muted bg-muted/40 text-muted-foreground",
    icon: <WifiOff className="size-3" />,
  },
};

/**
 * Live AI provider status pill. Polls /v1/ai/status on an interval so the
 * user always knows whether the model is reachable before using AI features.
 */
export function AiStatusBadge({
  pollMs = 15000,
  className,
}: {
  pollMs?: number;
  className?: string;
}) {
  const [status, setStatus] = useState<AgentStatus | null>(null);
  const [errored, setErrored] = useState(false);

  useEffect(() => {
    let alive = true;
    const tick = async () => {
      try {
        const s = await getAgentStatus();
        if (alive) {
          setStatus(s);
          setErrored(false);
        }
      } catch {
        if (alive) setErrored(true);
      }
    };
    tick();
    const id = setInterval(tick, pollMs);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [pollMs]);

  const state = deriveState(status, errored);
  const meta = META[state];

  const detail =
    state === "up" || state === "degraded"
      ? `Model: ${status?.model || "unknown"} · ${status?.api_base || ""}`
      : state === "disabled"
        ? "Set MEDANON_AI_ENABLED=true to enable AI features."
        : state === "down"
          ? "The AI model is unreachable. Check Ollama and MEDANON_AI_API_BASE."
          : "Querying the AI provider…";

  return (
    <TooltipProvider>
      <Tooltip>
        <TooltipTrigger
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium",
            meta.cls,
            className,
          )}
        >
          {meta.icon}
          {meta.label}
        </TooltipTrigger>
        <TooltipContent>{detail}</TooltipContent>
      </Tooltip>
    </TooltipProvider>
  );
}
