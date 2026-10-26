import { createColumnHelper, tableFeatures, useTable } from "@tanstack/react-table";
import { ArrowDown } from "lucide-react";
import type { MouseEvent } from "react";
import { Link, useNavigate } from "react-router";
import type { DocumentRow, SortKey } from "@/api/contract";
import { StatusBadge } from "@/components/status-badge";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, TD, TH, THead, TR } from "@/components/ui/table";
import { formatDay, formatDocumentDate, formatMoney, formatTime } from "@/lib/format";
import { cn } from "@/lib/utils";
import { useDocumentQuery } from "./queries";

const features = tableFeatures({});
const column = createColumnHelper<typeof features, DocumentRow>();

/** Shown wherever the document did not say, so a gap reads as a gap and not as zero. */
function Missing({ label }: { label: string }) {
  return (
    <span className="text-faint" title={label}>
      —<span className="sr-only">{label}</span>
    </span>
  );
}

/**
 * A column the API can order by. Always newest first -- the only order anyone
 * wants for documents -- so a header is either the active sort or a way to
 * make it so. The choice lives in the URL with the filters.
 */
function SortHeader({ sortKey, children }: { sortKey: SortKey; children: string }) {
  const { query, update } = useDocumentQuery();
  const active = (query.sort ?? "received") === sortKey;
  return (
    <button
      type="button"
      onClick={() => update({ sort: sortKey === "received" ? undefined : sortKey })}
      className={cn(
        "inline-flex items-center gap-1 uppercase tracking-wide hover:text-ink",
        active && "text-ink",
      )}
      title={
        active
          ? `Sorted by ${children.toLowerCase()}, newest first`
          : `Sort by ${children.toLowerCase()}`
      }
    >
      {children}
      <ArrowDown className={cn("size-3", !active && "opacity-0")} aria-hidden />
    </button>
  );
}

const columns = column.columns([
  column.accessor("received_at", {
    id: "received",
    header: () => <SortHeader sortKey="received">Received</SortHeader>,
    cell: ({ getValue }) => (
      <div className="tabular">
        <div>{formatDay(getValue())}</div>
        <div className="text-xs text-muted">{formatTime(getValue())}</div>
      </div>
    ),
  }),
  column.accessor("document_date", {
    id: "document_date",
    header: () => <SortHeader sortKey="document_date">Document date</SortHeader>,
    cell: ({ getValue }) => {
      const date = getValue();
      return date ? (
        <span className="tabular">{formatDocumentDate(date)}</span>
      ) : (
        <Missing label="No date on the document" />
      );
    },
  }),
  column.display({
    id: "supplier",
    header: "Supplier",
    cell: ({ row }) => {
      const { id, supplier, filename } = row.original;
      return (
        <div className="min-w-0">
          <Link
            to={`/documents/${id}`}
            className={cn(
              "block truncate font-medium hover:underline",
              !supplier && "font-normal text-muted",
            )}
          >
            {supplier ?? filename ?? "Untitled document"}
          </Link>
          {supplier && filename && <div className="truncate text-xs text-muted">{filename}</div>}
        </div>
      );
    },
  }),
  column.accessor((row) => row.type?.label ?? null, {
    id: "type",
    header: "Type",
    cell: ({ getValue }) => getValue() ?? <Missing label="Type not known" />,
  }),
  column.accessor("total", {
    id: "total",
    header: "Total",
    cell: ({ getValue }) => {
      const total = getValue();
      return total ? (
        <span className="tabular font-medium">{formatMoney(total.amount, total.currency)}</span>
      ) : (
        <Missing label="No total" />
      );
    },
  }),
  column.accessor("outcome", {
    id: "status",
    header: "Status",
    cell: ({ getValue }) => <StatusBadge outcome={getValue()} />,
  }),
]);

const WIDTH: Record<string, string> = {
  received: "w-36",
  document_date: "w-36",
  type: "w-28",
  total: "w-36 text-right",
  status: "w-40",
};

export function DocumentsTable({ rows, refreshing }: { rows: DocumentRow[]; refreshing: boolean }) {
  const table = useTable({ features, columns, data: rows, getRowId: (row) => row.id });
  const navigate = useNavigate();
  const sortedBy = useDocumentQuery().query.sort ?? "received";

  // The whole row opens the document; the supplier link inside is what the
  // keyboard and screen readers use, and it keeps cmd-click for a new tab.
  function open(event: MouseEvent<HTMLTableRowElement>, id: string) {
    if ((event.target as HTMLElement).closest("a, button")) return;
    if (window.getSelection()?.toString()) return;
    navigate(`/documents/${id}`);
  }

  return (
    <Table className="table-fixed">
      <THead>
        {table.getHeaderGroups().map((group) => (
          <tr key={group.id}>
            {group.headers.map((header) => (
              <TH
                key={header.id}
                className={WIDTH[header.column.id]}
                aria-sort={header.column.id === sortedBy ? "descending" : undefined}
              >
                <table.FlexRender header={header} />
              </TH>
            ))}
          </tr>
        ))}
      </THead>
      <tbody className={cn("transition-opacity", refreshing && "opacity-60")}>
        {table.getRowModel().rows.map((row) => (
          <TR
            key={row.id}
            onClick={(event) => open(event, row.original.id)}
            className="cursor-pointer transition-colors hover:bg-canvas"
          >
            {row.getAllCells().map((cell) => (
              <TD key={cell.id} className={WIDTH[cell.column.id]}>
                <table.FlexRender cell={cell} />
              </TD>
            ))}
          </TR>
        ))}
      </tbody>
    </Table>
  );
}

/** The table's shape while the first page loads, so nothing jumps when it arrives. */
export function DocumentsTableSkeleton({ rows = 8 }: { rows?: number }) {
  return (
    <Table className="table-fixed" aria-busy="true" aria-label="Loading documents">
      <THead>
        <tr>
          {columns.map((c) => (
            <TH key={c.id} className={WIDTH[c.id ?? ""]}>
              <Skeleton className="h-3 w-16" />
            </TH>
          ))}
        </tr>
      </THead>
      <tbody>
        {Array.from({ length: rows }, (_, i) => (
          // biome-ignore lint/suspicious/noArrayIndexKey: placeholders, never reordered
          <TR key={i}>
            {columns.map((c) => (
              <TD key={c.id} className={WIDTH[c.id ?? ""]}>
                <Skeleton className="h-4 w-full max-w-28" />
              </TD>
            ))}
          </TR>
        ))}
      </tbody>
    </Table>
  );
}
