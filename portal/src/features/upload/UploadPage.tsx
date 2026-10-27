import { CloudUpload, FileImage, FileText, RotateCw, X } from "lucide-react";
import { type DragEvent, useEffect, useId, useState } from "react";
import { Link } from "react-router";
import { PageHeader } from "@/components/page-header";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { useCan } from "@/features/auth/can";
import { useDocumentFields } from "@/features/documents/queries";
import { formatMoment } from "@/lib/format";
import { cn } from "@/lib/utils";
import { ACCEPTED, pending, type Upload, uploads, useUploads } from "./queue";
import type { DocumentOut } from "./send";

export function UploadPage() {
  const { work } = useCan();
  if (!work) {
    return (
      <>
        <PageHeader title="Upload" />
        <Card className="p-10 text-center text-sm text-muted">
          Your role can see documents but not upload them. An admin at your organisation can change
          that in Settings.
        </Card>
      </>
    );
  }
  return <Uploader />;
}

function Uploader() {
  const items = useUploads();
  const left = pending(items);
  const finished = items.some((u) => u.state.step === "received" || u.state.step === "refused");

  // A screenshot or a scanned image on the clipboard: paste it anywhere on the page.
  useEffect(() => {
    const paste = (event: ClipboardEvent) => {
      const files = [...(event.clipboardData?.files ?? [])];
      if (files.length) uploads.add(files);
    };
    window.addEventListener("paste", paste);
    return () => window.removeEventListener("paste", paste);
  }, []);

  return (
    <>
      <PageHeader
        title="Upload"
        description="Receipts and invoices: photos (JPEG, PNG, WebP, HEIC, TIFF) or PDFs, up to 20 MB each."
      />
      <DropZone onFiles={(files) => uploads.add(files)} />

      {items.length > 0 && (
        <Card className="mt-6 overflow-hidden">
          <div className="flex items-center justify-between border-b border-line px-5 py-3">
            <h2 className="font-semibold">
              {left > 0
                ? `Uploading ${left} of ${items.length}`
                : `${items.length} ${items.length === 1 ? "file" : "files"}`}
            </h2>
            {finished && (
              <Button variant="ghost" size="sm" onClick={() => uploads.clearFinished()}>
                Clear finished
              </Button>
            )}
          </div>
          <ul aria-label="Uploads">
            {items.map((upload) => (
              <Row key={upload.key} upload={upload} />
            ))}
          </ul>
        </Card>
      )}

      <p className="mt-6 text-sm text-muted">
        Documents can also come straight from your own system, with an API key. You'll find keys in{" "}
        <Link to="/settings" className="font-medium text-ink hover:underline">
          Settings
        </Link>
        .
      </p>
    </>
  );
}

function DropZone({ onFiles }: { onFiles: (files: File[]) => void }) {
  const id = useId();
  const [over, setOver] = useState(false);

  function drop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    const files = [...event.dataTransfer.files];
    if (files.length) onFiles(files);
  }

  return (
    <label
      htmlFor={id}
      onDragOver={(event) => {
        event.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={drop}
      className={cn(
        "flex cursor-pointer flex-col items-center justify-center rounded-lg border-2 border-dashed border-line-strong bg-surface px-6 py-14 text-center transition-colors",
        "hover:border-brand/50 hover:bg-brand-soft/30 focus-within:border-brand focus-within:ring-2 focus-within:ring-brand/20",
        over && "border-brand bg-brand-soft/50",
      )}
    >
      <div className="mb-3 flex size-12 items-center justify-center rounded-full bg-brand-soft text-brand">
        <CloudUpload className="size-6" aria-hidden />
      </div>
      <p className="font-medium">{over ? "Drop to upload" : "Drop receipts and invoices here"}</p>
      <p className="mt-1 text-sm text-muted">
        or <span className="font-medium text-brand">choose files</span>. You can also paste an
        image.
      </p>
      <input
        id={id}
        type="file"
        multiple
        accept={[...ACCEPTED.types, ...ACCEPTED.extensions].join(",")}
        className="sr-only"
        aria-label="Choose files to upload"
        onChange={(event) => {
          const files = [...(event.target.files ?? [])];
          if (files.length) onFiles(files);
          event.target.value = "";
        }}
      />
    </label>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function Row({ upload }: { upload: Upload }) {
  const { file, state } = upload;
  const Icon =
    file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf")
      ? FileText
      : FileImage;
  const settled = state.step === "received";
  return (
    <li className="flex items-center gap-4 border-b border-line px-5 py-3 last:border-0">
      <Icon className="size-5 shrink-0 text-faint" aria-hidden />
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium">{file.name}</p>
        <div className="mt-0.5 text-xs text-muted">
          <State upload={upload} />
        </div>
      </div>
      <span className="tabular hidden shrink-0 text-xs text-muted sm:block">
        {formatSize(file.size)}
      </span>
      {state.step === "failed" && (
        <Button variant="secondary" size="sm" onClick={() => uploads.retry(upload.key)}>
          <RotateCw /> Retry
        </Button>
      )}
      {!settled && (
        <Button
          variant="ghost"
          size="icon"
          className="size-8 text-muted"
          aria-label={
            state.step === "waiting" || state.step === "sending"
              ? `Cancel ${file.name}`
              : `Remove ${file.name}`
          }
          onClick={() => uploads.remove(upload.key)}
        >
          <X />
        </Button>
      )}
    </li>
  );
}

function State({ upload }: { upload: Upload }) {
  const { state } = upload;
  switch (state.step) {
    case "waiting":
      return <span>Waiting</span>;
    case "sending": {
      const percent = Math.round(state.progress * 100);
      return (
        <div className="flex items-center gap-2">
          <div
            role="progressbar"
            aria-label={`Uploading ${upload.file.name}`}
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            className="h-1.5 w-full max-w-60 overflow-hidden rounded-full bg-working-soft"
          >
            <div
              className="h-full rounded-full bg-brand transition-[width]"
              style={{ width: `${percent}%` }}
            />
          </div>
          <span className="tabular w-9">{percent}%</span>
        </div>
      );
    }
    case "received":
      return <Received document={state.document} />;
    case "refused":
    case "failed":
      return <span className="text-danger">{state.reason}</span>;
  }
}

/** After the upload: what became of the document, followed until it has been read. */
function Received({ document }: { document: DocumentOut }) {
  const view = useDocumentFields(document.id);
  const outcome = view.data?.document.outcome;
  const open = (
    <Link to={`/documents/${document.id}`} className="font-medium text-brand hover:underline">
      Open
    </Link>
  );
  return (
    <span className="flex flex-wrap items-center gap-2">
      {document.duplicate && <span>Already uploaded {formatMoment(document.uploaded_at)} ·</span>}
      {!outcome || outcome === "processing" ? (
        <span className="inline-flex items-center gap-1.5">
          <Spinner className="size-3" label="Reading" />{" "}
          {document.duplicate ? "Being read" : "Received, being read"}
        </span>
      ) : (
        <StatusBadge outcome={outcome} />
      )}
      {open}
    </span>
  );
}
