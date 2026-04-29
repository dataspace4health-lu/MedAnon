import { Monitor, Moon, Sun } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useTheme, type Theme } from "@/context/ThemeContext";
import { cn } from "@/lib/utils";

const OPTIONS: { value: Theme; label: string; Icon: typeof Sun }[] = [
  { value: "light", label: "Light", Icon: Sun },
  { value: "system", label: "System", Icon: Monitor },
  { value: "dark", label: "Dark", Icon: Moon },
];

interface ThemeToggleProps {
  /** "compact" = icon-only single toggle button; "segmented" = 3-state pill. */
  variant?: "compact" | "segmented";
  className?: string;
}

/**
 * Theme switcher. Two visual variants:
 *  - compact: a single icon button that flips light <-> dark
 *  - segmented: a 3-button pill (Light / System / Dark)
 */
export function ThemeToggle({
  variant = "compact",
  className,
}: ThemeToggleProps) {
  const { theme, resolved, setTheme, toggle } = useTheme();

  if (variant === "compact") {
    const Icon = resolved === "dark" ? Sun : Moon;
    const next = resolved === "dark" ? "light" : "dark";
    return (
      <Button
        variant="ghost"
        size="icon"
        onClick={toggle}
        aria-label={`Switch to ${next} theme`}
        title={`Switch to ${next} theme`}
        className={cn("h-8 w-8", className)}
      >
        <Icon className="h-4 w-4" />
      </Button>
    );
  }

  return (
    <div
      role="radiogroup"
      aria-label="Theme"
      className={cn(
        "inline-flex items-center rounded-md border bg-background p-0.5",
        className,
      )}
    >
      {OPTIONS.map(({ value, label, Icon }) => {
        const active = theme === value;
        return (
          <button
            key={value}
            type="button"
            role="radio"
            aria-checked={active}
            aria-label={label}
            title={label}
            onClick={() => setTheme(value)}
            className={cn(
              "inline-flex h-7 w-7 items-center justify-center rounded transition-colors",
              active
                ? "bg-primary text-primary-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
          >
            <Icon className="h-3.5 w-3.5" />
          </button>
        );
      })}
    </div>
  );
}
