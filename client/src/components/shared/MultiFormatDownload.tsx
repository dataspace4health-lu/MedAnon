import { useState, useCallback, useMemo } from "react";
import { Download } from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  SelectLabel,
  SelectGroup,
} from "@/components/ui/select";

export type DownloadFormat = "json" | "bundle" | "ndjson" | "xml";

interface DownloadOption {
  format: DownloadFormat;
  label: string;
  description: string;
  extension: string;
  mime: string;
}

const DOWNLOAD_OPTIONS: DownloadOption[] = [
  {
    format: "ndjson",
    label: "NDJSON",
    description: "Newline-delimited JSON (one resource per line)",
    extension: "ndjson",
    mime: "application/x-ndjson",
  },
  {
    format: "json",
    label: "JSON Array",
    description: "JSON array of resources",
    extension: "json",
    mime: "application/json",
  },
  {
    format: "bundle",
    label: "FHIR Bundle",
    description: "FHIR Bundle resource containing all resources",
    extension: "json",
    mime: "application/fhir+json",
  },
  {
    format: "xml",
    label: "XML (via API)",
    description: "FHIR XML format (requires API conversion)",
    extension: "xml",
    mime: "application/fhir+xml",
  },
];

interface MultiFormatDownloadProps {
  resources: Record<string, unknown>[];
  baseFilename: string;
  defaultFormat?: DownloadFormat;
  onXmlDownload?: () => void; // Callback for XML download via API
}

export function MultiFormatDownload({
  resources,
  baseFilename,
  defaultFormat = "ndjson",
  onXmlDownload,
}: MultiFormatDownloadProps) {
  const [selectedFormat, setSelectedFormat] = useState<DownloadFormat>(defaultFormat);

  // Generate data in different formats
  const formatData = useMemo(() => {
    const data: Record<DownloadFormat, string> = {
      ndjson: resources.map(r => JSON.stringify(r)).join('\n'),
      json: JSON.stringify(resources, null, 2),
      bundle: JSON.stringify(createBundle(resources), null, 2),
      xml: "", // XML handled via API
    };
    return data;
  }, [resources]);

  // Create FHIR Bundle from resources
  function createBundle(resources: Record<string, unknown>[]): Record<string, unknown> {
    return {
      resourceType: "Bundle",
      id: `deidentified-${Date.now()}`,
      type: "collection",
      timestamp: new Date().toISOString(),
      total: resources.length,
      entry: resources.map((resource) => ({
        fullUrl: `urn:uuid:${generateUuid()}`,
        resource: resource,
      })),
    };
  }

  // Simple UUID v4 generator
  function generateUuid(): string {
    return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function(c) {
      const r = Math.random() * 16 | 0;
      const v = c === 'x' ? r : (r & 0x3 | 0x8);
      return v.toString(16);
    });
  }

  const handleDownload = useCallback((format: DownloadFormat) => {
    const option = DOWNLOAD_OPTIONS.find(opt => opt.format === format);
    if (!option) return;

    // Special handling for XML
    if (format === "xml") {
      if (onXmlDownload) {
        onXmlDownload();
      } else {
        alert("XML download requires backend API support. Please use the API endpoint for XML conversion.");
      }
      return;
    }

    const data = formatData[format];
    const filename = `${baseFilename}.${option.extension}`;

    const blob = new Blob([data], { type: option.mime });
    const url = URL.createObjectURL(blob);

    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();

    document.body.removeChild(anchor);
    URL.revokeObjectURL(url);
  }, [formatData, baseFilename, onXmlDownload]);

  return (
    <div className="flex gap-2 items-center">
      {/* Format selector */}
      <Select value={selectedFormat} onValueChange={(value) => setSelectedFormat(value as DownloadFormat)}>
        <SelectTrigger className="w-[200px]">
          <SelectValue placeholder="Select format" />
        </SelectTrigger>
        <SelectContent>
          <SelectGroup>
            <SelectLabel>Download Format</SelectLabel>
            {DOWNLOAD_OPTIONS.map((option) => (
              <SelectItem key={option.format} value={option.format}>
                <div className="flex flex-col">
                  <span className="font-medium">{option.label}</span>
                  <span className="text-xs text-muted-foreground">{option.description}</span>
                </div>
              </SelectItem>
            ))}
          </SelectGroup>
        </SelectContent>
      </Select>

      {/* Download button */}
      <Button variant="outline" onClick={() => handleDownload(selectedFormat)}>
        <Download className="h-4 w-4 mr-2" />
        Download
      </Button>
    </div>
  );
}

// Export types for external use
export type { DownloadOption };
export { DOWNLOAD_OPTIONS };