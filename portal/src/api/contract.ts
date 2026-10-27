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

/** The value kinds a processor's fields can have (doctypes/definition.py FieldType). */
export type FieldType = "string" | "integer" | "number" | "money" | "date" | "boolean";

/** A single value: money as "104.88", dates as YYYY-MM-DD -- the same wire form as /result. */
export type FieldValue = string | number | boolean | null;

/**
 * Where on the original a value was read: a page (from 1) and a box on it, as
 * fractions of the page's width and height from its top-left corner. Found by
 * matching the value against the OCR's words, so null when it was not printed
 * (a calculated total) or could not be matched.
 */
export interface Location {
  page: number;
  /** [left, top, right, bottom], each from 0 to 1. */
  box: [number, number, number, number];
}

/** A value someone changed by hand, and what had been read before. */
export interface Correction {
  /** Who: the signed-in user's name. */
  by: string;
  at: string;
  /** The value as read. Null for a list, whose rows are replaced whole. */
  was: FieldValue;
}

interface FieldBase {
  /** The key in /result's `data`. */
  name: string;
  /** For people: "Invoice number" for invoice_number. */
  label: string;
  /** The processor's rules refuse the document without it. */
  required: boolean;
  /** Worked out by the processor's transform rather than read off the page. */
  derived: boolean;
  /** Why this field holds the document in review, or null. */
  problem: string | null;
  /** Set once someone has changed it. */
  corrected: Correction | null;
}

export interface ScalarField extends FieldBase {
  type: FieldType;
  value: FieldValue;
  location: Location | null;
}

export type ListRow = Record<string, FieldValue>;

/** A repeated group, such as line items: shown as a table. */
export interface ListField extends FieldBase {
  type: "list";
  columns: { name: string; label: string; type: FieldType }[];
  rows: ListRow[];
  /** Where each row was read, in the same order as `rows`. */
  row_locations: (Location | null)[];
}

export type Field = ScalarField | ListField;

/**
 * GET /documents/{id}/fields: one document as the portal shows it.
 *
 * Shaped for a screen, unlike /result, which is shaped for an ERP and stays
 * stable for it: labels, types and per-field problems are here and not there.
 * 5.4 adds PATCH on the same path for corrections.
 */
export interface DocumentFields {
  document: DocumentRow & { mime_type: string; size_bytes: number };
  /** Which processor read it, or null if none has (yet). */
  processor: { name: string; label: string; version: number } | null;
  /** The document's currency, for every money field on it. */
  currency: string | null;
  /** In the processor's own order. Empty until the document has been read. */
  fields: Field[];
  /** Problems not about any one field, such as "Line items do not add up to the subtotal". */
  problems: string[];
  /** When the fields were read: ISO 8601, or null. */
  read_at: string | null;
  /**
   * Goes up with every change. A PATCH names the revision it was made
   * against, and is refused (409) if someone else saved in between.
   */
  revision: number;
  /** What happened to the document, oldest first. */
  history: { at: string; what: string; by: string | null }[];
}

/**
 * PATCH /documents/{id}/fields: correct values, approve, or both.
 *
 * The server re-runs the processor's transform and rules on the result.
 * With no problems left the document is done; with problems left it stays in
 * review unless `approve` says to accept it as it is. The answer is the
 * document's new DocumentFields.
 *
 * 409 when `revision` is not the current one; 422 when a value does not fit
 * its field, with `fields` saying which.
 */
export interface FieldsPatch {
  revision: number;
  /** Only the fields that changed. A list is sent whole. */
  values: Record<string, FieldValue | ListRow[]>;
  approve?: boolean;
}

export interface FieldsRefused {
  detail: string;
  fields?: Record<string, string>;
}

// --- Settings: a client's API keys and team (5.6) -------------------------------------
// All under /settings, for the signed-in person's own organisation only. Reading
// needs any role; every change needs "admin" (403 otherwise).

/**
 * What a person at a client may do. Not the team's "author"/"admin", which are
 * about writing processors:
 *   admin   -- everything a member can, plus API keys and the team
 *   member  -- upload, review and correct
 *   viewer  -- see documents and export them; change nothing
 */
export type ClientRole = "admin" | "member" | "viewer";

/** One live API key. GET /settings/keys lists them; revoked keys are not listed. */
export interface ApiKeyRow {
  id: string;
  name: string;
  /** The start of the key, "lk_a1B2c3D4": enough to recognise it, not to use it. */
  prefix: string;
  created_at: string;
  /** Who made it, or null for one made by the Ledgerline team. */
  created_by: string | null;
  last_used_at: string | null;
}

/**
 * POST /settings/keys {name} -> the key, with its secret. The only time the
 * secret is ever shown: Ledgerline keeps a hash of it, never the key itself.
 */
export interface CreatedKey {
  key: ApiKeyRow;
  secret: string;
}

/** DELETE /settings/keys/{id}: revoke it, at once. 204. */

/** One person at the organisation. GET /settings/members. */
export interface Member {
  id: string;
  email: string;
  role: ClientRole;
  /** "invited" until they accept and set a password. */
  status: "active" | "invited";
  last_seen_at: string | null;
  /** The person asking. */
  you: boolean;
}

/**
 * POST /settings/members {email, role} -> the invitation. Ledgerline does not
 * send email yet, so the answer carries a link for the admin to pass on: it
 * works once, for seven days.
 */
export interface Invitation {
  member: Member;
  invite_url: string;
  expires_at: string;
}

/**
 * PATCH /settings/members/{id} {role} -> the member. DELETE -> 204.
 * 409 for anything that would leave the organisation with no admin, and for
 * removing yourself.
 */
