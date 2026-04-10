/* eslint-disable react-refresh/only-export-components */
import { Badge } from "@/components/ui/badge";
import { Loader2, AlertCircle, CheckCircle2, XCircle } from "lucide-react";
import type { ExportJobStatus } from "@/context/BulkExportContext";

export function StatusIcon({ status }: { status: ExportJobStatus }) {
  switch (status) {
    case "submitting":
    case "pending":
    case "running":
      return <Loader2 className="size-4 animate-spin text-primary shrink-0" />;
    case "done":
      return <CheckCircle2 className="size-4 text-green-600 shrink-0" />;
    case "cancelled":
      return <XCircle className="size-4 text-muted-foreground shrink-0" />;
    case "error":
      return <AlertCircle className="size-4 text-destructive shrink-0" />;
  }
}

export function statusBadge(status: string) {
  switch (status) {
    case "submitting":
    case "pending":
      return <Badge variant="secondary">Pending</Badge>;
    case "running":
      return <Badge className="bg-blue-100 text-blue-800 hover:bg-blue-100">Running</Badge>;
    case "done":
      return <Badge className="bg-green-100 text-green-800 hover:bg-green-100">Done</Badge>;
    case "cancelled":
      return <Badge variant="secondary">Cancelled</Badge>;
    case "error":
      return <Badge variant="destructive">Error</Badge>;
    default:
      return <Badge variant="secondary">{status}</Badge>;
  }
}
