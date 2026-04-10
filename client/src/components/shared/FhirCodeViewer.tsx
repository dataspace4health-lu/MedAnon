import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";
import { highlightJsonHtml } from "./highlightJson";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface FhirCodeViewerProps {
  code: string;
  language?: "json" | "xml";
  maxHeight?: string;
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
}: FhirCodeViewerProps) {
  const containerClass = "overflow-auto rounded-lg border bg-muted/30";
  const containerStyle = { maxHeight };

  // Fast path: custom inline highlighter for JSON (any size, no freeze)
  if (language === "json") {
    return (
      <div className={containerClass} style={containerStyle}>
        <pre
          style={BASE_PRE}
          dangerouslySetInnerHTML={{ __html: highlightJsonHtml(code) }}
        />
      </div>
    );
  }

  // XML — keep Prism (less common, usually smaller payloads)
  return (
    <div className={containerClass} style={containerStyle}>
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
