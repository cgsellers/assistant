import { ChevronLeft, ChevronRight, Download, FileSearch, Inbox, Upload } from "lucide-react";
import { type ReactNode, useRef } from "react";
import { Link, useLocation } from "react-router";
import type { DocumentQuery } from "@/api/contract";
import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { apiUrl } from "@/lib/http";
import { DocumentsTable, DocumentsTableSkeleton } from "./DocumentsTable";
import { Filters } from "./Filters";
import {
  type PageTrail,
  useDocumentQuery,
  useDocuments,
  useDocumentTypes,
  useSummary,
} from "./queries";
import { SummaryBar } from "./SummaryBar";

const NO_FILTERS = {
  outcome: undefined,
  type: undefined,
  q: undefined,
  from: undefined,
  to: undefined,
};

function isFiltered(query: DocumentQuery) {
  return Boolean(query.outcome || query.type || query.q || query.from || query.to);
}

export function DocumentsPage() {
  const { query, update } = useDocumentQuery();
  const documents = useDocuments(query);
  const summary = useSummary(query);
  const types = useDocumentTypes();
  const location = useLocation();
  const top = useRef<HTMLDivElement>(null);

  const trail = (location.state as PageTrail | null)?.trail ?? [];
  // Known only when this page was reached by paging; a bookmarked later page
  // has an opaque cursor and no way to say which page it is.
  const page = query.cursor ? (trail.length ? trail.length + 1 : null) : 1;

  function turn(cursor: string | undefined, nextTrail: string[]) {
    update({ cursor }, { trail: nextTrail });
    top.current?.scrollIntoView?.({ block: "start" });
  }

  const { cursor: _cursor, sort: _sort, ...filters } = query;
  const exportUrl = apiUrl("/documents/export", { ...filters, sort: query.sort });

  return (
    <>
      <PageHeader
        title="Documents"
        description="Everything sent to Ledgerline, newest first."
        actions={
          <>
            <Button variant="secondary" asChild>
              <a href={`${exportUrl.pathname}${exportUrl.search}`} download>
                <Download /> Export CSV
              </a>
            </Button>
            <Button asChild>
              <Link to="/upload">
                <Upload /> Upload
              </Link>
            </Button>
          </>
        }
      />

      <SummaryBar
        summary={summary.data}
        loading={summary.isPending}
        onShowReview={() =>
          update({ ...NO_FILTERS, from: query.from, to: query.to, outcome: "needs_review" })
        }
      />

      <Filters query={query} types={types.data ?? []} update={update} />

      <div ref={top} className="scroll-mt-20">
        <Card className="overflow-hidden">
          {documents.isError ? (
            <State
              icon={<FileSearch />}
              title="Your documents could not be loaded"
              body={documents.error.message}
              action={
                <Button variant="secondary" onClick={() => documents.refetch()}>
                  Try again
                </Button>
              }
              role="alert"
            />
          ) : documents.isPending ? (
            <DocumentsTableSkeleton />
          ) : documents.data.items.length === 0 ? (
            isFiltered(query) ? (
              <State
                icon={<FileSearch />}
                title="No documents match these filters"
                body="Try a wider date range, or clear the filters to see everything."
                action={
                  <Button variant="secondary" onClick={() => update(NO_FILTERS)}>
                    Clear filters
                  </Button>
                }
              />
            ) : (
              <State
                icon={<Inbox />}
                title="No documents yet"
                body="Upload receipts and invoices here, or have your system send them with an API key. Each one appears here as soon as it arrives."
                action={
                  <Button asChild>
                    <Link to="/upload">
                      <Upload /> Upload documents
                    </Link>
                  </Button>
                }
              />
            )
          ) : (
            <>
              <DocumentsTable
                rows={documents.data.items}
                refreshing={documents.isPlaceholderData}
              />
              <nav
                aria-label="Pages"
                className="flex items-center justify-between border-t border-line px-4 py-3 text-sm text-muted"
              >
                <span>{page ? `Page ${page}` : ""}</span>
                <div className="flex gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={!query.cursor}
                    onClick={() => turn(trail.at(-1) || undefined, trail.slice(0, -1))}
                  >
                    <ChevronLeft /> Previous
                  </Button>
                  <Button
                    variant="secondary"
                    size="sm"
                    disabled={!documents.data.next_cursor || documents.isPlaceholderData}
                    onClick={() =>
                      turn(documents.data.next_cursor ?? undefined, [...trail, query.cursor ?? ""])
                    }
                  >
                    Next <ChevronRight />
                  </Button>
                </div>
              </nav>
            </>
          )}
        </Card>
      </div>
    </>
  );
}

function State({
  icon,
  title,
  body,
  action,
  role,
}: {
  icon: ReactNode;
  title: string;
  body: string;
  action: ReactNode;
  role?: "alert";
}) {
  return (
    <div role={role} className="flex flex-col items-center px-6 py-16 text-center">
      <div className="mb-4 flex size-11 items-center justify-center rounded-full bg-working-soft text-muted [&_svg]:size-5">
        {icon}
      </div>
      <h2 className="font-semibold">{title}</h2>
      <p className="mt-1 max-w-sm text-sm text-muted">{body}</p>
      <div className="mt-5">{action}</div>
    </div>
  );
}
