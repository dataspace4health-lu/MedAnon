import { PackageOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import { useBulkExport } from "@/context/BulkExportContext";
import type { JobResponse } from "@/api/medanon";

interface BulkExportButtonProps {
  label: string;
  onSubmit: () => Promise<JobResponse>;
  filename: string;
  disabled?: boolean;
  variant?: "default" | "outline" | "secondary";
}

export function BulkExportButton({
  label,
  onSubmit,
  filename,
  disabled,
  variant = "outline",
}: BulkExportButtonProps) {
  const { submitExport } = useBulkExport();

  return (
    <Button
      variant={variant}
      size="sm"
      onClick={() => submitExport(label, filename, onSubmit)}
      disabled={disabled}
    >
      <PackageOpen className="h-4 w-4" />
      {label}
    </Button>
  );
}
