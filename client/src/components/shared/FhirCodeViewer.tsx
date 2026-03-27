import { Prism as SyntaxHighlighter } from "react-syntax-highlighter";
import { oneLight } from "react-syntax-highlighter/dist/esm/styles/prism";

// ---------------------------------------------------------------------------
// Fast inline JSON highlighter
// Uses a single regex pass + dangerouslySetInnerHTML — no React element
// overhead, works at any size without freezing.
// ---------------------------------------------------------------------------

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function highlightJsonHtml(raw: string): string {
  const escaped = escapeHtml(raw);
  return escaped.replace(
    /(\"(?:\\.|[^"\\])*\"(?=\s*:))|(\"(?:\\.|[^"\\])*\")|\b(true|false)\b|\b(null)\b|(-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (_m, key, str, bool_, null_, num) => {
      if (key)   return `<span style="color:#7c3aed;font-weight:500">${key}</span>`;
      if (str)   return `<span style="color:#16a34a">${str}</span>`;
      if (bool_) return `<span style="color:#2563eb">${bool_}</span>`;
      if (null_) return `<span style="color:#9ca3af">${null_}</span>`;
      if (num)   return `<span style="color:#ea580c">${num}</span>`;
      return _m;
    }
  );
}

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
