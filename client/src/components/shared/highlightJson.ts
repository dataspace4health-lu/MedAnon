function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function highlightJsonHtml(raw: string): string {
  const escaped = escapeHtml(raw);
  return escaped.replace(
    /("(?:\\.|[^"\\])*"(?=\s*:))|("(?:\\.|[^"\\])*")|\b(true|false)\b|\b(null)\b|(-?\d+(?:\.\d*)?(?:[eE][+-]?\d+)?)/g,
    (_m, key, str, bool_, null_, num) => {
      if (key)   return `<span style="color:#7c3aed;font-weight:500">${key}</span>`;
      if (str)   return `<span style="color:#16a34a">${str}</span>`;
      if (bool_) return `<span style="color:#2563eb">${bool_}</span>`;
      if (null_) return `<span style="color:#9ca3af">${null_}</span>`;
      if (num)   return `<span style="color:#ea580c">${num}</span>`;
      return _m;
    },
  );
}
