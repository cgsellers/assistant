import { HttpResponse, http } from "msw";
import type {
  DocumentList,
  DocumentRow,
  DocumentSummary,
  DocumentTypes,
  Money,
  Outcome,
} from "@/api/contract";

/**
 * Acme's documents, as the mock API serves them. Deterministic -- the same
 * rows every run -- so tests can name them, and varied enough to look like a
 * real month: several suppliers and currencies, mostly ready, a few needing
 * review, one still being read, one not recognised.
 */
const SUPPLIERS: [string, string, string][] = [
  ["Northwind Office Supply", "invoice", "GBP"],
  ["Globe Logistics", "invoice", "EUR"],
  ["Coop Italia", "receipt", "EUR"],
  ["Contoso Cloud", "invoice", "USD"],
  ["Fabrikam Furniture", "invoice", "GBP"],
  ["Tailspin Travel", "receipt", "GBP"],
  ["Wide World Importers", "invoice", "EUR"],
  ["Café Luna", "receipt", "EUR"],
];

const TYPES: DocumentTypes = [
  { name: "invoice", label: "Invoice" },
  { name: "receipt", label: "Receipt" },
];

function outcomeFor(index: number): Outcome {
  if (index === 0) return "processing";
  if (index === 9) return "unsupported";
  if (index === 23) return "failed";
  if (index % 7 === 3) return "needs_review";
  return "done";
}

function amountFor(index: number): string {
  const base = ((index * 7919) % 250000) / 100 + 12;
  return base.toFixed(2);
}

export const MOCK_DOCUMENTS: DocumentRow[] = Array.from({ length: 64 }, (_, index) => {
  const [supplier, type, currency] = SUPPLIERS[index % SUPPLIERS.length] as [
    string,
    string,
    string,
  ];
  const outcome = outcomeFor(index);
  // Newest first: two to three a day, back from 26 October 2026.
  const received = new Date(Date.UTC(2026, 9, 26, 17, 40) - index * 9.5 * 3600_000);
  const issued = new Date(received.getTime() - ((index % 5) + 1) * 86400_000);
  const read = outcome !== "processing" && outcome !== "unsupported" && outcome !== "failed";
  // Some documents in review are there because no total could be read --
  // the commonest reason, so the screen has to show it well.
  const missingTotal = outcome === "needs_review" && index % 2 === 1;
  const total: Money | null = read && !missingTotal ? { amount: amountFor(index), currency } : null;
  return {
    id: `doc-${String(index + 1).padStart(3, "0")}`,
    filename:
      type === "invoice"
        ? `${supplier.split(" ")[0]?.toLowerCase()}-inv-${7700 + index}.pdf`
        : `receipt-${index + 1}.jpg`,
    received_at: received.toISOString(),
    document_date: read ? issued.toISOString().slice(0, 10) : null,
    supplier: read ? supplier : null,
    type:
      outcome === "unsupported" || outcome === "processing"
        ? null
        : (TYPES.find((t) => t.name === type) ?? null),
    total,
    outcome,
  };
});

const OUTCOMES: Outcome[] = ["processing", "done", "needs_review", "unsupported", "failed"];

function filtered(url: URL): DocumentRow[] {
  const outcome = url.searchParams.get("outcome");
  const type = url.searchParams.get("type");
  const q = url.searchParams.get("q")?.trim().toLowerCase();
  const from = url.searchParams.get("from");
  const to = url.searchParams.get("to");
  const sort = url.searchParams.get("sort") ?? "received";

  const rows = MOCK_DOCUMENTS.filter((row) => {
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
  const cell = (value: string | null) => `"${(value ?? "").replaceAll('"', '""')}"`;
  const header = "Received,Document date,Supplier,Type,Total,Currency,Status,File";
  const lines = rows.map((r) =>
    [
      r.received_at,
      r.document_date,
      r.supplier,
      r.type?.label ?? null,
      r.total?.amount ?? null,
      r.total?.currency ?? null,
      r.outcome,
      r.filename,
    ]
      .map(cell)
      .join(","),
  );
  return [header, ...lines].join("\n");
}

export const documentHandlers = [
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
