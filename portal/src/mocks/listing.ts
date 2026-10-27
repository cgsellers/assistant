import { HttpResponse, http } from "msw";
import type { DocumentList, DocumentRow, DocumentSummary, Outcome } from "@/api/contract";
import { fieldsFor } from "./document";
import { MOCK_DOCUMENTS, TYPES } from "./documents";
import { uploads } from "./uploads";

/**
 * GET /documents, /documents/summary, /types and /export: every document as
 * it is now -- uploads first, newest first, with any corrections applied.
 */

function current(): DocumentRow[] {
  const ids = [...uploads().map((u) => u.out.id), ...MOCK_DOCUMENTS.map((row) => row.id)];
  return ids.flatMap((id) => {
    const view = fieldsFor(id);
    if (!view) return [];
    const { mime_type: _mime, size_bytes: _size, ...row } = view.document;
    return [row];
  });
}

const OUTCOMES: Outcome[] = ["processing", "done", "needs_review", "unsupported", "failed"];

function filtered(url: URL): DocumentRow[] {
  const outcome = url.searchParams.get("outcome");
  const type = url.searchParams.get("type");
  const q = url.searchParams.get("q")?.trim().toLowerCase();
  const from = url.searchParams.get("from");
  const to = url.searchParams.get("to");
  const sort = url.searchParams.get("sort") ?? "received";

  const rows = current().filter((row) => {
    if (outcome && row.outcome !== outcome) return false;
    if (type && row.type?.name !== type) return false;
    if (q && !`${row.supplier ?? ""} ${row.filename ?? ""}`.toLowerCase().includes(q)) return false;
    const day = row.received_at.slice(0, 10);
    if (from && day < from) return false;
    if (to && day > to) return false;
    return true;
  });
  if (sort === "document_date") {
    // Undated last, as the API will do it: they have no place in the order.
    return [...rows].sort((a, b) => (b.document_date ?? "").localeCompare(a.document_date ?? ""));
  }
  return rows;
}

function csv(rows: DocumentRow[]): string {
  // As the API does (doctypes/listing.py): text from documents that a
  // spreadsheet would run as a formula gets an apostrophe in front. Only the
  // text columns: a negative amount stays a number.
  const text = (value: string | null) => {
    const plain = value ?? "";
    return /^[=+\-@\t\r]/.test(plain) ? `'${plain}` : plain;
  };
  const cell = (value: string | null) => `"${(value ?? "").replaceAll('"', '""')}"`;
  const header = "Received,Document date,Supplier,Type,Total,Currency,Status,File";
  const lines = rows.map((r) =>
    [
      r.received_at,
      r.document_date,
      text(r.supplier),
      text(r.type?.label ?? null),
      r.total?.amount ?? null,
      r.total?.currency ?? null,
      r.outcome,
      text(r.filename),
    ]
      .map(cell)
      .join(","),
  );
  return [header, ...lines].join("\n");
}

export const listingHandlers = [
  http.get("/api/documents/summary", ({ request }) => {
    const rows = filtered(new URL(request.url));
    const by_outcome = Object.fromEntries(OUTCOMES.map((o) => [o, 0])) as Record<Outcome, number>;
    const totals = new Map<string, number>();
    for (const row of rows) {
      by_outcome[row.outcome] += 1;
      if (row.total?.currency) {
        // Cents, as integers: the mock must not teach the screen float money.
        const cents = Math.round(Number(row.total.amount) * 100);
        totals.set(row.total.currency, (totals.get(row.total.currency) ?? 0) + cents);
      }
    }
    const body: DocumentSummary = {
      documents: rows.length,
      by_outcome,
      totals: [...totals.entries()]
        .sort(([a], [b]) => a.localeCompare(b))
        .map(([currency, cents]) => ({ currency, amount: (cents / 100).toFixed(2) })),
    };
    return HttpResponse.json(body);
  }),

  http.get("/api/documents/types", () => HttpResponse.json(TYPES)),

  http.get("/api/documents/export", ({ request }) => {
    return new HttpResponse(csv(filtered(new URL(request.url))), {
      headers: {
        "Content-Type": "text/csv; charset=utf-8",
        "Content-Disposition": 'attachment; filename="documents.csv"',
      },
    });
  }),

  http.get("/api/documents", ({ request }) => {
    const url = new URL(request.url);
    const rows = filtered(url);
    const limit = Math.min(Number(url.searchParams.get("limit") ?? 25), 100);
    const offset = Number(url.searchParams.get("cursor") ?? 0);
    const items = rows.slice(offset, offset + limit);
    const body: DocumentList = {
      items,
      next_cursor: offset + limit < rows.length ? String(offset + limit) : null,
    };
    return HttpResponse.json(body);
  }),
];
