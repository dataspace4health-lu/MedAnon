import { useCallback, useRef, useState } from "react";
import { UploadCloud } from "lucide-react";
import { cn } from "@/lib/utils";

interface FileUploaderProps {
  accept: string[];
  maxSize: number;
  onFile: (file: File, content: Uint8Array) => void;
  onError: (message: string) => void;
}

function formatBytes(bytes: number): string {
  if (bytes === 0) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  const i = Math.floor(Math.log(bytes) / Math.log(1024));
  const value = bytes / Math.pow(1024, i);
  return `${value.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

export function FileUploader({
  accept,
  maxSize,
  onFile,
  onError,
}: FileUploaderProps) {
  const [isDragging, setIsDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const validateAndRead = useCallback(
    (file: File) => {
      const ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
      if (!accept.includes(ext)) {
        onError(
          `Unsupported file type "${ext}". Accepted: ${accept.join(", ")}`
        );
        return;
      }
      if (file.size > maxSize) {
        onError(
          `File too large (${formatBytes(file.size)}). Maximum: ${formatBytes(maxSize)}`
        );
        return;
      }
      const reader = new FileReader();
      reader.onload = () => {
        if (reader.result instanceof ArrayBuffer) {
          onFile(file, new Uint8Array(reader.result));
        }
      };
      reader.onerror = () => onError("Failed to read file.");
      reader.readAsArrayBuffer(file);
    },
    [accept, maxSize, onFile, onError]
  );

  const handleDrop = useCallback(
    (e: React.DragEvent<HTMLDivElement>) => {
      e.preventDefault();
      setIsDragging(false);
      const file = e.dataTransfer.files[0];
      if (file) validateAndRead(file);
    },
    [validateAndRead]
  );

  const handleDragOver = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(true);
  }, []);

  const handleDragLeave = useCallback((e: React.DragEvent<HTMLDivElement>) => {
    e.preventDefault();
    setIsDragging(false);
  }, []);

  const handleChange = useCallback(
    (e: React.ChangeEvent<HTMLInputElement>) => {
      const file = e.target.files?.[0];
      if (file) validateAndRead(file);
      if (inputRef.current) inputRef.current.value = "";
    },
    [validateAndRead]
  );

  return (
    <div
      role="button"
      tabIndex={0}
      className={cn(
        "group relative flex cursor-pointer flex-col items-center justify-center gap-4 rounded-xl border-2 border-dashed px-8 py-12 text-center transition-all duration-200",
        isDragging
          ? "border-primary bg-primary/5 scale-[1.01]"
          : "border-muted-foreground/20 hover:border-primary/40 hover:bg-muted/30"
      )}
      onClick={() => inputRef.current?.click()}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          inputRef.current?.click();
        }
      }}
      onDrop={handleDrop}
      onDragOver={handleDragOver}
      onDragLeave={handleDragLeave}
    >
      {/* Icon */}
      <div
        className={cn(
          "flex size-14 items-center justify-center rounded-full transition-colors",
          isDragging
            ? "bg-primary/10 text-primary"
            : "bg-muted text-muted-foreground group-hover:bg-primary/10 group-hover:text-primary"
        )}
      >
        <UploadCloud className="size-7" />
      </div>

      {/* Text */}
      <div className="space-y-1">
        <p className="text-sm font-semibold text-foreground">
          {isDragging ? "Drop file here" : "Drag & drop or click to upload"}
        </p>
        <p className="text-xs text-muted-foreground">
          Max size: {formatBytes(maxSize)}
        </p>
      </div>

      {/* Format badges */}
      <div className="flex flex-wrap justify-center gap-1.5">
        {accept.map((ext) => (
          <span
            key={ext}
            className="rounded-full border bg-background px-2.5 py-0.5 font-mono text-[11px] font-medium text-muted-foreground uppercase tracking-wide"
          >
            {ext.replace(".", "")}
          </span>
        ))}
      </div>

      <input
        ref={inputRef}
        type="file"
        className="hidden"
        accept={accept.join(",")}
        onChange={handleChange}
      />
    </div>
  );
}
