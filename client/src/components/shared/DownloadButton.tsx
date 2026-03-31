import { useCallback } from "react";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";

interface DownloadButtonProps {
  data: string;
  filename: string;
  mime?: string;
  label?: string;
}

export function DownloadButton({
  data,
  filename,
  mime = "application/octet-stream",
  label = "Download",
}: DownloadButtonProps) {
  const handleDownload = useCallback(() => {
    const blob = new Blob([data], { type: mime });

    const url = URL.createObjectURL(blob);

    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();

    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  }, [data, filename, mime]);

  return (
    <Button variant="outline" onClick={handleDownload}>
      <Download data-icon="inline-start" className="h-4 w-4" />
      {label}
    </Button>
  );
}
