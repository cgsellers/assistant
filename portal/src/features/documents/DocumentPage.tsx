import {
  AlertTriangle,
  ArrowLeft,
  CircleSlash,
  FileSearch,
  LoaderCircle,
  Pencil,
  XCircle,
} from "lucide-react";
import { type ReactNode, useId } from "react";
import { Link, useLocation, useParams } from "react-router";
import type { DocumentFields } from "@/api/contract";
import { StatusBadge } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { ApiError } from "@/lib/api";
import { formatDocumentDate, formatMoment, formatMoney } from "@/lib/format";
import { apiUrl } from "@/lib/http";
import { cn } from "@/lib/utils";
import { FieldEditor } from "./FieldEditor";
import { FieldList } from "./FieldList";
import { useDocumentFields } from "./queries";
import { useReview } from "./useReview";
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

/** How many things hold a document in review: its own problems, and flagged fields. */
function problemCount(view: DocumentFields) {
  return view.problems.length + view.fields.filter((f) => f.problem).length;
}

function Loaded({ view }: { view: DocumentFields }) {
  const { document } = view;
  const filename = document.filename ?? "document";
  const fileUrl = apiUrl(`/documents/${encodeURIComponent(document.id)}/file`).pathname;
  const review = useReview(view);
  const { editing } = review;
  const problems = problemCount(view);

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
          <Viewer
            url={fileUrl}
            mimeType={document.mime_type}
            filename={filename}
            pointing={review.pointing}
          />
        </div>

        <div className="space-y-4">
          <Notice
            view={view}
            actions={
              document.outcome === "needs_review" &&
              !editing && (
                <div className="mt-3 flex flex-wrap gap-2">
                  <Button
                    size="sm"
                    onClick={() => review.start(view.fields.find((f) => f.problem)?.name ?? null)}
                  >
                    Review and fix
                  </Button>
                  <Button size="sm" variant="secondary" onClick={review.askApprove}>
                    Approve as it is
                  </Button>
                </div>
              )
            }
          />
          {view.processor && (
            <>
              {!editing && (
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
              )}
              <Card className={cn(editing && "ring-2 ring-brand/30")}>
                <div className="flex items-center justify-between gap-3 border-b border-line px-5 py-3">
                  <h2 className="font-semibold">
                    {editing ? "Correct what was read" : "What was read"}
                  </h2>
                  {editing ? (
                    <span className="text-xs text-muted">
                      Changes are checked again when you save
                    </span>
                  ) : (
                    <div className="flex items-center gap-3">
                      <span className="text-xs text-muted">
                        {view.processor.label} · version {view.processor.version}
                      </span>
                      <Button variant="ghost" size="sm" onClick={() => review.start()}>
                        <Pencil /> Edit
                      </Button>
                    </div>
                  )}
                </div>
                {editing ? (
                  <form
                    onSubmit={(event) => {
                      event.preventDefault();
                      review.submit();
                    }}
                  >
                    <FieldEditor
                      fields={view.fields}
                      currency={view.currency}
                      draft={editing.draft}
                      errors={review.errors}
                      focus={editing.focus}
                      onChange={review.change}
                      point={review.point}
                    />
                    <div className="sticky bottom-0 flex items-center justify-between gap-3 rounded-b-lg border-t border-line bg-surface/95 px-5 py-3 backdrop-blur">
                      <span className="text-xs text-muted" aria-live="polite">
                        {review.changed === 0
                          ? "No changes yet"
                          : review.changed === 1
                            ? "1 field changed"
                            : `${review.changed} fields changed`}
                      </span>
                      <div className="flex gap-2">
                        <Button type="button" variant="ghost" onClick={review.cancel}>
                          Cancel
                        </Button>
                        <Button type="submit" disabled={review.changed === 0 || review.saving}>
                          {review.saving && <Spinner className="text-white" label="Saving" />}
                          Save changes
                        </Button>
                      </div>
                    </div>
                  </form>
                ) : (
                  <FieldList
                    fields={view.fields}
                    currency={view.currency}
                    active={review.active}
                    point={review.point}
                  />
                )}
              </Card>
            </>
          )}
          <Details view={view} />
          <History view={view} />
        </div>
      </div>

      <ConfirmDialog
        open={review.approving}
        title={problems === 1 ? "Approve with one problem?" : `Approve with ${problems} problems?`}
        confirm="Approve"
        busy={review.saving}
        onConfirm={review.approve}
        onCancel={review.cancelApprove}
      >
        Its values will be used as they are. The{" "}
        {problems === 1 ? "problem stays" : "problems stay"} on record in the document's history.
      </ConfirmDialog>
      <ConfirmDialog
        open={review.leaving}
        title="Discard your changes?"
        confirm="Discard"
        cancel="Keep editing"
        onConfirm={review.leave}
        onCancel={review.stay}
      >
        You've changed values on this document that haven't been saved.
      </ConfirmDialog>
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
function Notice({ view, actions }: { view: DocumentFields; actions: ReactNode }) {
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
        {actions}
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

/** Everything that happened to the document, oldest first: who corrected or approved what. */
function History({ view }: { view: DocumentFields }) {
  return (
    <Card className="px-5 py-4">
      <h2 className="mb-3 text-sm font-semibold">History</h2>
      <ol aria-label="History" className="relative space-y-3 border-l border-line pl-4">
        {view.history.map((entry, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: an append-only log, never reordered
          <li key={i} className="relative text-sm">
            <span
              className="absolute top-1.5 -left-[21px] size-2 rounded-full bg-line-strong"
              aria-hidden
            />
            <p>
              {entry.what}
              {entry.by && <span className="text-muted"> · {entry.by}</span>}
            </p>
            <p className="text-xs text-muted">{formatMoment(entry.at)}</p>
          </li>
        ))}
      </ol>
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
