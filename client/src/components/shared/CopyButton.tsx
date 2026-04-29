import { useCallback, useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface CopyButtonProps {
  /** Text to copy. Can be a function that's invoked lazily on click. */
  value: string | (() => string);
  /** Optional visible label. Omit for icon-only button. */
  label?: string;
  /** Tooltip / aria-label. Defaults to "Copy". */
  ariaLabel?: string;
  className?: string;
  size?: "sm" | "icon";
  variant?: "ghost" | "outline";
}

/**
 * Compact copy-to-clipboard button. Uses the async Clipboard API with a
 * legacy textarea fallback for non-secure contexts (http://). Shows a brief
 * "Copied" check icon for ~1.5s after a successful copy.
 */
export function CopyButton({
  value,
  label,
  ariaLabel = "Copy",
  className,
  size = "sm",
  variant = "ghost",
}: CopyButtonProps) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const t = window.setTimeout(() => setCopied(false), 1500);
    return () => window.clearTimeout(t);
  }, [copied]);

  const handleCopy = useCallback(async () => {
    const text = typeof value === "function" ? value() : value;
    if (!text) return;

    try {
      if (navigator.clipboard && window.isSecureContext) {
        await navigator.clipboard.writeText(text);
      } else {
        // Fallback for insecure contexts (e.g. http://192.168.x.x dev)
        const ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.opacity = "0";
        document.body.appendChild(ta);
        ta.select();
        document.execCommand("copy");
        document.body.removeChild(ta);
      }
      setCopied(true);
    } catch {
      setCopied(false);
    }
  }, [value]);

  const iconCls = label ? "mr-1.5 h-3.5 w-3.5" : "h-3.5 w-3.5";

  return (
    <Button
      type="button"
      variant={variant}
      size={size}
      onClick={handleCopy}
      aria-label={copied ? "Copied" : ariaLabel}
      title={copied ? "Copied!" : ariaLabel}
      className={cn("text-xs", className)}
    >
      {copied ? (
        <Check className={cn(iconCls, "text-emerald-600")} />
      ) : (
        <Copy className={iconCls} />
      )}
      {label && <span>{copied ? "Copied" : label}</span>}
    </Button>
  );
}
