/**
 * Endpoints the portal needs that the API does not have yet.
 *
 * Written before the backend, on purpose: the mock handlers implement exactly
 * these shapes, the screens are built against them, and then the backend is
 * built to match. When an endpoint lands in the API, its types come from the
 * generated schema instead and its entry here is deleted -- `npm run api`
 * plus the type checker will then point at any disagreement.
 */

/** The five states a client sees. Same vocabulary as GET /documents/{id}/result. */
export type Outcome = "processing" | "done" | "needs_review" | "unsupported" | "failed";

export interface Money {
  /** Exact decimal as a string, e.g. "104.88" -- never a float. */
  amount: string;
  /** ISO 4217, or null when the document did not say. */
  currency: string | null;
}

/** One row of GET /documents. */
export interface DocumentRow {
  id: string;
  filename: string | null;
  /** When it reached Ledgerline: ISO 8601 with a time zone. */
  received_at: string;
  /** The document's own date, as printed: YYYY-MM-DD, or null. */
  document_date: string | null;
  /** Who issued it -- the supplier on an invoice, the shop on a receipt. */
  supplier: string | null;
  /** What kind of document it was read as, e.g. { name: "invoice", label: "Invoice" }. */
  type: { name: string; label: string } | null;
  total: Money | null;
  outcome: Outcome;
}

/** GET /documents: newest first, a page at a time. */
export interface DocumentList {
  items: DocumentRow[];
  /** Pass back as `cursor` for the next page; null on the last one. */
  next_cursor: string | null;
}

export type SortKey = "received" | "document_date";

/** Query parameters of GET /documents and GET /documents/export. */
export interface DocumentQuery {
  outcome?: Outcome;
  /** A type's `name`. */
  type?: string;
  /** Matches supplier and filename, case-insensitively. */
  q?: string;
  /** Received on or after / on or before, YYYY-MM-DD. */
  from?: string;
  to?: string;
  sort?: SortKey;
  cursor?: string;
  limit?: number;
}

/** GET /documents/summary: the headline numbers for a period. */
export interface DocumentSummary {
  documents: number;
  by_outcome: Record<Outcome, number>;
  /** Spend per currency. Never added across currencies. */
  totals: Money[];
}

/** GET /documents/types: the kinds of document this client's documents were read as. */
export type DocumentTypes = { name: string; label: string }[];
