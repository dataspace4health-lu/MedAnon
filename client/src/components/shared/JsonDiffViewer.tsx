import { useMemo } from "react";
import { diffLines, diffWords } from "diff";
import { highlightJsonHtml } from "./highlightJson";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

interface EqualRow  { kind: "equal";   left: string; right: string; ln: number; rn: number }
interface DelRow    { kind: "del";     left: string; right: null;   ln: number; rn: null  }
interface AddRow    { kind: "add";     left: null;   right: string; ln: null;   rn: number }
interface ReplaceRow{ kind: "replace"; left: string; right: string; ln: number; rn: number }
interface GapRow    { kind: "gap";     count: number }

type DiffRow = EqualRow | DelRow | AddRow | ReplaceRow | GapRow;

const DEFAULT_CONTEXT = 4; // unchanged lines shown around each change

// ---------------------------------------------------------------------------
// JSON key-order normalizer — sorts all object keys alphabetically so that
// backend key-reordering doesn't produce false-positive diff rows.
// ---------------------------------------------------------------------------

function sortKeys(obj: unknown): unknown {
  if (Array.isArray(obj)) return obj.map(sortKeys);
  if (obj !== null && typeof obj === "object") {
    return Object.fromEntries(
      Object.entries(obj as Record<string, unknown>)
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([k, v]) => [k, sortKeys(v)])
    );
  }
  return obj;
}

function normalizeJson(jsonStr: string): string {
  try {
    return JSON.stringify(sortKeys(JSON.parse(jsonStr)), null, 2);
  } catch {
    return jsonStr;
  }
}

// ---------------------------------------------------------------------------
// Build side-by-side rows from diff output
// ---------------------------------------------------------------------------

function buildRows(original: string, modified: string, context: number = DEFAULT_CONTEXT, disableGapCompression: boolean = false): DiffRow[] {
  const changes = diffLines(normalizeJson(original), normalizeJson(modified));

  // Flatten into per-line entries tagged del/add/equal
  const flat: Array<{ tag: "del" | "add" | "equal"; text: string }> = [];
  for (const c of changes) {
    const lines = c.value.replace(/\n$/, "").split("\n");
    const tag = c.removed ? "del" : c.added ? "add" : "equal";
    for (const l of lines) flat.push({ tag, text: l });
  }

  // Pair consecutive del+add blocks into "replace" rows for better display
  const paired: DiffRow[] = [];
  let ln = 1, rn = 1;
  let i = 0;
  while (i < flat.length) {
    const cur = flat[i];
    if (cur.tag === "equal") {
      paired.push({ kind: "equal", left: cur.text, right: cur.text, ln: ln++, rn: rn++ });
      i++;
    } else if (cur.tag === "del") {
      // Collect contiguous del block
      const dels: string[] = [];
      while (i < flat.length && flat[i].tag === "del") dels.push(flat[i++].text);
      // Collect immediately following add block
      const adds: string[] = [];
      while (i < flat.length && flat[i].tag === "add") adds.push(flat[i++].text);

      const maxLen = Math.max(dels.length, adds.length);
      for (let j = 0; j < maxLen; j++) {
        const l = dels[j] ?? null;
        const r = adds[j] ?? null;
        if (l !== null && r !== null) {
          // If the backend reordered keys without changing values, l === r.
          // Treat those as "equal" so they don't show false-positive highlights.
          if (l === r) {
            paired.push({ kind: "equal", left: l, right: r, ln: ln++, rn: rn++ });
          } else {
            paired.push({ kind: "replace", left: l, right: r, ln: ln++, rn: rn++ });
          }
        } else if (l !== null) {
          paired.push({ kind: "del", left: l, right: null, ln: ln++, rn: null });
        } else {
          paired.push({ kind: "add", left: null, right: r!, ln: null, rn: rn++ });
        }
      }
    } else {
      // standalone add
      paired.push({ kind: "add", left: null, right: cur.text, ln: null, rn: rn++ });
      i++;
    }
  }

  // Collapse long equal runs → gap placeholder (if enabled)
  const result: DiffRow[] = [];
  let j = 0;
  while (j < paired.length) {
    if (paired[j].kind !== "equal") { result.push(paired[j++]); continue; }
    // Find length of equal run
    let end = j;
    while (end < paired.length && paired[end].kind === "equal") end++;
    const runLen = end - j;
    if (disableGapCompression || runLen <= context * 2) {
      for (let k = j; k < end; k++) result.push(paired[k]);
    } else {
      for (let k = j; k < j + context; k++) result.push(paired[k]);
      result.push({ kind: "gap", count: runLen - context * 2 });
      for (let k = end - context; k < end; k++) result.push(paired[k]);
    }
    j = end;
  }
  return result;
}

// ---------------------------------------------------------------------------
// Word-level diff highlight for replace rows
// ---------------------------------------------------------------------------

function renderWordDiff(left: string, right: string, side: "left" | "right"): string {
  // Always diff left→right so p.removed = deleted from original, p.added = new in de-identified
  const parts = diffWords(left, right);
  let html = "";
  for (const p of parts) {
    const escaped = p.value
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
    if (side === "left" && p.removed) {
      html += `<mark style="background:#fecaca;border-radius:2px;padding:0 1px">${escaped}</mark>`;
    } else if (side === "right" && p.added) {
      html += `<mark style="background:#bbf7d0;border-radius:2px;padding:0 1px">${escaped}</mark>`;
    } else if (!p.removed && !p.added) {
      html += highlightJsonHtml(p.value);
    }
    // skip: p.removed on right side (was in original, not shown in de-identified col)
    //       p.added on left side (new in de-identified, not shown in original col)
  }
  return html;
}

// ---------------------------------------------------------------------------
// Rendering helpers
// ---------------------------------------------------------------------------

const CELL_BASE: React.CSSProperties = {
  display: "table-cell",
  padding: "0 0.75rem",
  whiteSpace: "pre",
  fontSize: "0.75rem",
  lineHeight: "1.6",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  verticalAlign: "top",
  width: "50%",
  overflowX: "hidden",
};

const NUM_CELL: React.CSSProperties = {
  display: "table-cell",
  padding: "0 0.4rem",
  width: "2.5rem",
  minWidth: "2.5rem",
  textAlign: "right",
  fontSize: "0.7rem",
  lineHeight: "1.6",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  color: "#9ca3af",
  userSelect: "none",
  verticalAlign: "top",
};

const GUTTER: React.CSSProperties = {
  display: "table-cell",
  width: "1.5rem",
  minWidth: "1.5rem",
  textAlign: "center",
  fontSize: "0.7rem",
  lineHeight: "1.6",
  fontFamily: "ui-monospace, SFMono-Regular, Menlo, monospace",
  verticalAlign: "top",
};

function RowComp({ row }: { row: DiffRow }) {
  if (row.kind === "gap") {
    return (
      <div
        style={{ display: "table-row", background: "#f3f4f6" }}
      >
        <div style={{ ...NUM_CELL, color: "#6b7280" }}></div>
        <div style={{ ...GUTTER, color: "#6b7280" }}></div>
        <div style={{ ...CELL_BASE, color: "#6b7280", fontStyle: "italic" }}>
          ··· {row.count} unchanged lines ··· (click to expand in full view)
        </div>
        <div style={{ ...NUM_CELL, color: "#6b7280" }}></div>
        <div style={{ ...GUTTER, color: "#6b7280" }}></div>
        <div style={{ ...CELL_BASE, color: "#6b7280", fontStyle: "italic" }}></div>
      </div>
    );
  }

  const isChange = row.kind !== "equal";

  const leftBg  = row.kind === "del" ? "#fee2e2"
                : row.kind === "replace" ? "#fff3cd"
                : "transparent";
  const rightBg = row.kind === "add" ? "#dcfce7"
                : row.kind === "replace" ? "#d1fae5"
                : "transparent";

  const leftGutter  = row.kind === "del" || row.kind === "replace" ? "−" : " ";
  const rightGutter = row.kind === "add" || row.kind === "replace" ? "+" : " ";
  const leftGutterColor  = row.kind === "del" || row.kind === "replace" ? "#dc2626" : "#9ca3af";
  const rightGutterColor = row.kind === "add" || row.kind === "replace" ? "#16a34a" : "#9ca3af";

  const leftHtml = row.left !== null
    ? (row.kind === "replace"
        ? renderWordDiff(row.left, row.right, "left")
        : highlightJsonHtml(row.left))
    : "";
  const rightHtml = row.right !== null
    ? (row.kind === "replace"
        ? renderWordDiff(row.left, row.right, "right")
        : highlightJsonHtml(row.right))
    : "";

  return (
    <div style={{ display: "table-row" }}>
      {/* Left line number */}
      <div style={NUM_CELL}>{row.ln ?? ""}</div>
      {/* Left gutter */}
      <div style={{ ...GUTTER, color: leftGutterColor, background: leftBg }}>
        {isChange ? leftGutter : " "}
      </div>
      {/* Left content */}
      <div
        style={{ ...CELL_BASE, background: leftBg }}
        dangerouslySetInnerHTML={{ __html: leftHtml }}
      />
      {/* Right line number */}
      <div style={NUM_CELL}>{row.rn ?? ""}</div>
      {/* Right gutter */}
      <div style={{ ...GUTTER, color: rightGutterColor, background: rightBg }}>
        {isChange ? rightGutter : " "}
      </div>
      {/* Right content */}
      <div
        style={{ ...CELL_BASE, background: rightBg }}
        dangerouslySetInnerHTML={{ __html: rightHtml }}
      />
    </div>
  );
}

// ---------------------------------------------------------------------------
// Public component
// ---------------------------------------------------------------------------

interface JsonDiffViewerProps {
  original: string;
  modified: string;
  maxHeight?: string;
  context?: number;
  disableGapCompression?: boolean;
  fullHeight?: boolean;
}

export function JsonDiffViewer({
  original,
  modified,
  maxHeight = "480px",
  context = DEFAULT_CONTEXT,
  disableGapCompression = false,
  fullHeight = false
}: JsonDiffViewerProps) {
  const rows = useMemo(() => buildRows(original, modified, context, disableGapCompression), [original, modified, context, disableGapCompression]);

  const changes = rows.filter((r) => r.kind !== "equal" && r.kind !== "gap").length;

  return (
    <div className="rounded-lg border overflow-hidden">
      {/* Header */}
      <div className="flex items-center justify-between border-b bg-muted/40 px-3 py-2">
        <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
          Diff — Original vs De-identified
        </span>
        <div className="flex items-center gap-3 text-xs">
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-red-300" />
            <span className="text-muted-foreground">removed</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-green-300" />
            <span className="text-muted-foreground">added</span>
          </span>
          <span className="flex items-center gap-1">
            <span className="inline-block h-2 w-2 rounded-sm bg-yellow-200" />
            <span className="text-muted-foreground">changed</span>
          </span>
          {changes > 0 && (
            <span className="ml-1 font-medium text-foreground">{changes} change{changes !== 1 ? "s" : ""}</span>
          )}
        </div>
      </div>

      {/* Column headers */}
      <div
        style={{ display: "table", width: "100%", tableLayout: "fixed", borderCollapse: "collapse" }}
        className="border-b bg-muted/20"
      >
        <div style={{ display: "table-row" }}>
          <div style={NUM_CELL} />
          <div style={GUTTER} />
          <div style={{ ...CELL_BASE, padding: "0.25rem 0.75rem", color: "#6b7280", fontWeight: 600, fontSize: "0.7rem" }}>
            ORIGINAL
          </div>
          <div style={NUM_CELL} />
          <div style={GUTTER} />
          <div style={{ ...CELL_BASE, padding: "0.25rem 0.75rem", color: "#6b7280", fontWeight: 600, fontSize: "0.7rem" }}>
            DE-IDENTIFIED
          </div>
        </div>
      </div>

      {/* Rows */}
      <div style={{
        overflowX: "auto",
        overflowY: fullHeight ? "visible" : "auto",
        maxHeight: fullHeight ? "none" : maxHeight
      }}>
        {changes === 0 ? (
          <div className="flex flex-col items-center justify-center gap-2 py-10 text-center">
            <span className="text-2xl">⚠️</span>
            <p className="text-sm font-medium text-foreground">No fields were de-identified</p>
            <p className="max-w-xs text-xs text-muted-foreground">
              The output is identical to the input. The active config profile may not have
              rules matching this patient's fields, or the gPAS pseudonymization service
              may not be reachable. Try switching to a different config profile in the sidebar.
            </p>
          </div>
        ) : (
          <div style={{ display: "table", width: "100%", tableLayout: "fixed", borderCollapse: "collapse" }}>
            {rows.map((row, i) => (
              <RowComp key={i} row={row} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
