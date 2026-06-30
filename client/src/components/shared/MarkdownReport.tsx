/**
 * MarkdownReport — renders a Markdown report (Trust Gate Quality Passport,
 * scoring / audit report) as a styled document that fits the app UI, instead of
 * dumping the raw `#`/`|` source in a <pre>. GFM tables, lists, and code blocks
 * are mapped to themed elements.
 */
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { cn } from "@/lib/utils";

export function MarkdownReport({
  markdown,
  className,
}: {
  markdown: string;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2.5 text-sm", className)}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        components={{
          h1: ({ children }) => (
            <h1 className="mt-4 mb-2 text-base font-semibold tracking-tight first:mt-0">{children}</h1>
          ),
          h2: ({ children }) => (
            <h2 className="mt-4 mb-2 border-b pb-1 text-sm font-semibold first:mt-0">{children}</h2>
          ),
          h3: ({ children }) => <h3 className="mt-3 mb-1 text-sm font-semibold">{children}</h3>,
          p: ({ children }) => <p className="leading-relaxed text-muted-foreground">{children}</p>,
          ul: ({ children }) => (
            <ul className="ml-5 list-disc space-y-1 text-muted-foreground">{children}</ul>
          ),
          ol: ({ children }) => (
            <ol className="ml-5 list-decimal space-y-1 text-muted-foreground">{children}</ol>
          ),
          li: ({ children }) => <li className="leading-relaxed">{children}</li>,
          strong: ({ children }) => <strong className="font-semibold text-foreground">{children}</strong>,
          em: ({ children }) => <em className="italic">{children}</em>,
          a: ({ children, href }) => (
            <a
              href={href}
              target="_blank"
              rel="noreferrer"
              className="text-[#0072bc] underline-offset-2 hover:underline"
            >
              {children}
            </a>
          ),
          hr: () => <hr className="my-3 border-border" />,
          blockquote: ({ children }) => (
            <blockquote className="border-l-2 border-border pl-3 text-muted-foreground">{children}</blockquote>
          ),
          code: ({ children, className: codeClass }) =>
            codeClass ? (
              <code className={cn("font-mono text-xs", codeClass)}>{children}</code>
            ) : (
              <code className="rounded bg-muted px-1 py-px font-mono text-xs">{children}</code>
            ),
          pre: ({ children }) => (
            <pre className="overflow-x-auto rounded-md border bg-muted/50 p-3 text-xs leading-relaxed">
              {children}
            </pre>
          ),
          table: ({ children }) => (
            <div className="overflow-x-auto rounded-md border">
              <table className="w-full border-collapse text-xs">{children}</table>
            </div>
          ),
          thead: ({ children }) => <thead className="bg-muted/50">{children}</thead>,
          th: ({ children }) => (
            <th className="border-b px-2.5 py-1.5 text-left font-semibold">{children}</th>
          ),
          td: ({ children }) => <td className="border-b px-2.5 py-1.5 align-top">{children}</td>,
        }}
      >
        {markdown}
      </ReactMarkdown>
    </div>
  );
}
