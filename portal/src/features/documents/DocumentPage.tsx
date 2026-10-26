import {
  AlertTriangle,
  ArrowLeft,
  CircleSlash,
  FileSearch,
  LoaderCircle,
  XCircle,
} from "lucide-react";
import { type ReactNode, useId } from "react";
import { Link, useLocation, useParams } from "react-router";
import type { DocumentFields } from "@/api/contract";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { formatDocumentDate, formatMoment, formatMoney } from "@/lib/format";
import { apiUrl } from "@/lib/http";
import { cn } from "@/lib/utils";
import { FieldList } from "./FieldList";
import { useDocumentFields } from "./queries";
import { Viewer } from "./viewer/Viewer";

/** Set by the documents table, so "back" returns to the same filtered view. */
export interface FromList {
  from?: string;
}

export function DocumentPage() {
  const { id = "" } = useParams();
  const location = useLocation();
  const back = `/documents${(location.state as FromList | null)?.from ?? ""}`;
  const view = useDocumentFields(id);

  return (
    <>
      <Link
        to={back}
        className="mb-4 inline-flex items-center gap-1.5 text-sm text-muted transition-colors hover:text-ink"
      >
        <ArrowLeft className="size-4" />
        <span className="sr-only">Back to</span> Documents
      </Link>
      {view.isPending ? (
        <Loading />
      ) : view.isError ? (
        <Missing
          back={back}
          notFound={view.error instanceof ApiError && view.error.status === 404}
          detail={view.error.message}
          retry={() => view.refetch()}
        />
      ) : (
        <Loaded view={view.data} />
      )}
    </>
  );
}

function Loaded({ view }: { view: DocumentFields }) {
  const { document } = view;
  const filename = document.filename ?? "document";
  const fileUrl = apiUrl(`/documents/${encodeURIComponent(document.id)}/file`).pathname;

  return (
    <>
      <header className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="truncate text-2xl font-semibold tracking-tight">
              {document.supplier ?? filename}
            </h1>
            <StatusBadge outcome={document.outcome} />
          </div>
          <p className="mt-1 text-sm text-muted">
            {document.supplier && <>{filename} · </>}Received {formatMoment(document.received_at)}
          </p>
        </div>
      </header>

      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <div className="h-[70vh] lg:sticky lg:top-20 lg:h-[calc(100vh-7rem)]">
          <Viewer url={fileUrl} mimeType={document.mime_type} filename={filename} />
        </div>

        <div className="space-y-4">
          <Notice view={view} />
          {view.processor && (
            <>
              <Card className="grid grid-cols-3 divide-x divide-line">
                <Fact label="Total">
                  {document.total
                    ? formatMoney(document.total.amount, document.total.currency)
                    : null}
                </Fact>
                <Fact label="Document date">
                  {document.document_date ? formatDocumentDate(document.document_date) : null}
                </Fact>
                <Fact label="Type">{document.type?.label ?? null}</Fact>
              </Card>
              <Card className="overflow-hidden">
                <div className="flex items-baseline justify-between border-b border-line px-5 py-3">
                  <h2 className="font-semibold">What was read</h2>
                  <span className="text-xs text-muted">
                    {view.processor.label} · version {view.processor.version}
                  </span>
                </div>
                <FieldList fields={view.fields} currency={view.currency} />
              </Card>
            </>
          )}
          <Details view={view} />
        </div>
      </div>
    </>
  );
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="px-4 py-3">
      <p className="text-xs text-muted">{label}</p>
      <p className={cn("tabular mt-0.5 truncate font-semibold", children === null && "text-faint")}>
        {children ?? "—"}
      </p>
    </div>
  );
}

/** What state the document is in, when it is anything but ready. */
function Notice({ view }: { view: DocumentFields }) {
  const outcome = view.document.outcome;
  if (outcome === "needs_review") {
    const flagged = view.fields.filter((f) => f.problem).length;
    return (
      <Callout tone="attention" icon={<AlertTriangle />} title="Needs your review">
        {view.problems.length > 0 && (
          <ul className="list-disc space-y-1 pl-4">
            {view.problems.map((problem) => (
              <li key={problem}>{problem}</li>
            ))}
          </ul>
        )}
        {flagged > 0 && (
          <p className={cn(view.problems.length > 0 && "mt-2")}>
            {flagged === 1 ? "One field is" : `${flagged} fields are`} flagged below.
          </p>
        )}
        {view.problems.length === 0 && flagged === 0 && (
          <p>Something on this document needs checking.</p>
        )}
      </Callout>
    );
  }
  if (outcome === "processing") {
    return (
      <Callout
        tone="working"
        icon={<LoaderCircle className="animate-spin" />}
        title="Reading this document"
      >
        Usually done within a minute. This page updates by itself.
      </Callout>
    );
  }
  if (outcome === "unsupported") {
    return (
      <Callout tone="working" icon={<CircleSlash />} title="Not recognised">
        It doesn't match any of the document types set up for you, so nothing was read from it. If
        Ledgerline should read documents like this one, get in touch.
      </Callout>
    );
  }
  if (outcome === "failed") {
    return (
      <Callout tone="danger" icon={<XCircle />} title="Couldn't be read">
        Often a blurred or dark photo. Uploading a clearer photo, or the original PDF, usually
        works.
      </Callout>
    );
  }
  return null;
}

const TONES = {
  attention: "border-attention/25 bg-attention-soft text-attention",
  working: "border-line bg-working-soft text-working",
  danger: "border-danger/20 bg-danger-soft text-danger",
};

function Callout({
  tone,
  icon,
  title,
  children,
}: {
  tone: keyof typeof TONES;
  icon: ReactNode;
  title: string;
  children: ReactNode;
}) {
  const titleId = useId();
  return (
    <div
      role="status"
      aria-labelledby={titleId}
      className={cn("flex gap-3 rounded-lg border p-4", TONES[tone])}
    >
      <span className="mt-0.5 [&_svg]:size-4">{icon}</span>
      <div className="min-w-0 text-sm">
        <p id={titleId} className="font-semibold">
          {title}
        </p>
        <div className="mt-1 text-ink-soft">{children}</div>
      </div>
    </div>
  );
}

function Details({ view }: { view: DocumentFields }) {
  const { document } = view;
  const rows: [string, string][] = [
    ["File", document.filename ?? "—"],
    ["Size", formatSize(document.size_bytes)],
    ["Received", formatMoment(document.received_at)],
    ["Read", view.read_at ? formatMoment(view.read_at) : "Not yet"],
  ];
  return (
    <Card className="px-5 py-3">
      <h2 className="sr-only">File details</h2>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-6 gap-y-1.5 text-sm">
        {rows.map(([label, value]) => (
          <div key={label} className="contents">
            <dt className="text-muted">{label}</dt>
            <dd className="truncate text-right">{value}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} bytes`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function Loading() {
  return (
    <div role="status" aria-busy="true" aria-label="Loading document">
      <Skeleton className="mb-2 h-8 w-72" />
      <Skeleton className="mb-6 h-4 w-96" />
      <div className="grid gap-6 lg:grid-cols-[minmax(0,7fr)_minmax(0,5fr)]">
        <Skeleton className="h-[70vh] rounded-lg" />
        <div className="space-y-4">
          <Skeleton className="h-20 rounded-lg" />
          <Skeleton className="h-96 rounded-lg" />
        </div>
      </div>
    </div>
  );
}

function Missing({
  back,
  notFound,
  detail,
  retry,
}: {
  back: string;
  notFound: boolean;
  detail: string;
  retry: () => void;
}) {
  return (
    <Card role="alert" className="flex flex-col items-center px-6 py-16 text-center">
      <div className="mb-4 flex size-11 items-center justify-center rounded-full bg-working-soft text-muted">
        <FileSearch className="size-5" />
      </div>
      <h1 className="font-semibold">
        {notFound ? "Document not found" : "This document could not be loaded"}
      </h1>
      <p className="mt-1 max-w-sm text-sm text-muted">
        {notFound ? "It may have been removed, or the link may be wrong." : detail}
      </p>
      <div className="mt-5 flex gap-2">
        {!notFound && (
          <Button variant="secondary" onClick={retry}>
            Try again
          </Button>
        )}
        <Button asChild>
          <Link to={back}>Back to documents</Link>
        </Button>
      </div>
    </Card>
  );
}
