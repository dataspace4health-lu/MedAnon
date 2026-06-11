import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Collapsible,
  CollapsibleTrigger,
  CollapsibleContent,
} from "@/components/ui/collapsible";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { MessageSquare, ChevronDown, Loader2, Send, AlertCircle } from "lucide-react";
import { cn } from "@/lib/utils";
import { streamChat, type ChatTurn } from "@/api/agents";
import { AiStatusBadge } from "@/components/shared/AiStatusBadge";

// Model switcher options — both served by the local Ollama.
const MODELS = [
  { value: "ollama/gemma3:1b", label: "gemma3:1b (fast)" },
  {
    value: "ollama/hf.co/unsloth/medgemma-4b-it-GGUF:Q4_K_M",
    label: "MedGemma 4B (medical)",
  },
  { value: "ollama/gemma3:4b", label: "gemma3:4b" },
];

interface ChatEntry {
  role: "user" | "assistant";
  content: string;
}

/**
 * Conversational panel: ask questions about the config being built and watch
 * the local model stream an answer. Sends the current YAML preview as context.
 */
export function AiChatPanel({ configYaml }: { configYaml: string }) {
  const [open, setOpen] = useState(false);
  const [model, setModel] = useState(MODELS[0].value);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatEntry[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  const scrollToBottom = () => {
    requestAnimationFrame(() => {
      scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
    });
  };

  const handleSend = async () => {
    const question = input.trim();
    if (!question || streaming) return;
    setErr(null);
    setInput("");

    const history: ChatTurn[] = messages.map((m) => ({
      role: m.role,
      content: m.content,
    }));

    setMessages((prev) => [
      ...prev,
      { role: "user", content: question },
      { role: "assistant", content: "" },
    ]);
    scrollToBottom();
    setStreaming(true);

    try {
      for await (const chunk of streamChat({
        question,
        config_yaml: configYaml,
        history,
        model,
      })) {
        setMessages((prev) => {
          const next = [...prev];
          next[next.length - 1] = {
            role: "assistant",
            content: next[next.length - 1].content + chunk,
          };
          return next;
        });
        scrollToBottom();
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      // Drop the empty assistant placeholder on failure.
      setMessages((prev) =>
        prev[prev.length - 1]?.content === "" ? prev.slice(0, -1) : prev,
      );
    } finally {
      setStreaming(false);
    }
  };

  return (
    <Collapsible open={open} onOpenChange={setOpen}>
      <CollapsibleTrigger className="inline-flex h-7 items-center gap-1.5 rounded-md border bg-background px-2.5 text-xs font-medium shadow-sm transition-colors hover:bg-accent hover:text-accent-foreground">
        <MessageSquare className="size-3" />
        Ask AI
        <ChevronDown className={cn("size-3 transition-transform", open && "rotate-180")} />
      </CollapsibleTrigger>
      <CollapsibleContent className="mt-2">
        <div className="rounded-lg border bg-muted/30 p-3 space-y-2">
          <div className="flex items-center justify-between gap-2">
            <p className="text-xs text-muted-foreground">
              Ask about your config — actions, FHIRPaths, or compliance. The
              model sees your current rules as context.
            </p>
            <AiStatusBadge />
          </div>

          <div className="flex items-center gap-2">
            <span className="text-[11px] text-muted-foreground">Model</span>
            <Select value={model} onValueChange={(v) => setModel(v ?? MODELS[0].value)}>
              <SelectTrigger className="h-7 w-56 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {MODELS.map((m) => (
                  <SelectItem key={m.value} value={m.value} className="text-xs">
                    {m.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>

          {/* Conversation */}
          {messages.length > 0 && (
            <div
              ref={scrollRef}
              className="max-h-80 space-y-2 overflow-y-auto rounded-md border bg-background p-2"
            >
              {messages.map((m, i) => (
                <div
                  key={i}
                  className={cn(
                    "rounded-md px-2.5 py-1.5 text-xs leading-relaxed whitespace-pre-wrap",
                    m.role === "user"
                      ? "bg-[#0072bc]/10 text-foreground"
                      : "bg-muted/60 text-foreground",
                  )}
                >
                  <span className="mb-0.5 block text-[10px] font-semibold uppercase tracking-wide opacity-50">
                    {m.role === "user" ? "You" : "AI"}
                  </span>
                  {m.content || (
                    <Loader2 className="size-3 animate-spin opacity-60" />
                  )}
                </div>
              ))}
            </div>
          )}

          {err && (
            <p className="flex items-center gap-1 text-xs text-destructive">
              <AlertCircle className="size-3 shrink-0" /> {err}
            </p>
          )}

          <div className="flex gap-2">
            <Input
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey) {
                  e.preventDefault();
                  handleSend();
                }
              }}
              placeholder="e.g. What action should I use for an SSN field?"
              className="h-8 text-xs"
              disabled={streaming}
            />
            <Button
              size="sm"
              className="h-8 text-xs"
              onClick={handleSend}
              disabled={streaming || !input.trim()}
            >
              {streaming ? (
                <Loader2 className="size-3 animate-spin" />
              ) : (
                <Send className="size-3" />
              )}
            </Button>
          </div>
        </div>
      </CollapsibleContent>
    </Collapsible>
  );
}
