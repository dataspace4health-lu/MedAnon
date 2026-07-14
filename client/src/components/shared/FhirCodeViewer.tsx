import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import { highlightJsonHtml } from "./highlightJson";
import { CopyButton } from "./CopyButton";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface FhirCodeViewerProps {
  code: string;
  language?: "json" | "xml";
  maxHeight?: string;
  /** Set false to hide the floating copy button. Defaults to true. */
  showCopy?: boolean;
}

const BASE_PRE: React.CSSProperties = {
  margin: 0,
  padding: "1rem",
  fontSize: "0.8125rem",
  lineHeight: "1.5",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  background: "transparent",
};

export function FhirCodeViewer({
  code,
  language = "json",
  maxHeight = "400px",
  showCopy = true,
}: FhirCodeViewerProps) {
  const containerClass = "relative overflow-auto rounded-lg border bg-muted/30";
  const containerStyle = { maxHeight };

  // Floating copy button (top-right, hidden when there's no content)
  const copyOverlay = showCopy && code ? (
    <div className="absolute right-2 top-2 z-10">
      <CopyButton
        value={code}
        ariaLabel={`Copy ${language.toUpperCase()}`}
        variant="outline"
        className="bg-background/80 backdrop-blur-sm shadow-sm"
      />
    </div>
  ) : null;

  // Fast path: custom inline highlighter for JSON (any size, no freeze)
  if (language === "json") {
    return (
      <div className={containerClass} style={containerStyle}>
        {copyOverlay}
        <pre
          style={BASE_PRE}
          dangerouslySetInnerHTML={{ __html: highlightJsonHtml(code) }}
        />
      </div>
    );
  }

  // XML, keep Prism (less common, usually smaller payloads)
  return (
    <div className={containerClass} style={containerStyle}>
      {copyOverlay}
      <SyntaxHighlighter
        language={language}
        style={oneLight}
        customStyle={{ ...BASE_PRE, background: "transparent" }}
      >
        {code}
      </SyntaxHighlighter>
    </div>
  );
}
