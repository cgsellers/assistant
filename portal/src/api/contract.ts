import type { components } from "@/api/schema";

/**
 * Endpoints the portal needs that the API does not have yet, and the names
 * the screens use for those it has.
 *
 * Written before the backend, on purpose: the mock handlers implement exactly
 * these shapes, the screens are built against them, and then the backend is
 * built to match. When an endpoint lands in the API, its types come from the
 * generated schema instead and its entry here is deleted -- `npm run api`
 * plus the type checker will then point at any disagreement.
 */

// --- Built: the documents list ------------------------------------------------------
// GET /documents, /summary, /types and /export are in the API. Their types come
// from its schema (src/api/schema.d.ts); the names stay, so the screens and the
// contracts still to be built below keep using them.

type Schemas = components["schemas"];

/** The five states a client sees. Same vocabulary as GET /documents/{id}/result. */
export type Outcome = Schemas["DocumentRowOut"]["outcome"];
export type Money = Schemas["MoneyOut"];
export type DocumentRow = Schemas["DocumentRowOut"];
export type DocumentList = Schemas["DocumentListOut"];
export type DocumentSummary = Schemas["DocumentSummaryOut"];
export type DocumentTypes = Schemas["DocTypeOut"][];

export type SortKey = "received" | "document_date";

/** Query parameters of GET /documents, /summary and /export. */
export interface DocumentQuery {
  outcome?: Outcome;
  /** A type's `name`. */
  type?: string;
  /** Matches supplier and filename, case-insensitively. */
  q?: string;
  /** Received on or after / on or before, YYYY-MM-DD, as days in `tz`. */
  from?: string;
  to?: string;
  /** The person's IANA time zone, e.g. "Europe/Madrid"; the API assumes UTC without it. */
  tz?: string;
  sort?: SortKey;
  cursor?: string;
  limit?: number;
}

// --- Built: one document's fields ---------------------------------------------------
// GET /documents/{id}/fields is in the API. Its PATCH, below, is not yet.

/** The value kinds a processor's fields can have (doctypes/definition.py FieldType). */
export type FieldType = Schemas["ScalarFieldOut"]["type"];
/** A single value: money as "104.88", dates as YYYY-MM-DD -- the same wire form as /result. */
export type FieldValue = Schemas["ScalarFieldOut"]["value"];
/**
 * Where on the original a value was read: a page (from 1) and a box on it, as
 * fractions of the page from its top-left, [left, top, right, bottom].
 */
export type Location = Schemas["LocationOut"];
/** A value someone changed by hand, and what had been read before. */
export type Correction = Schemas["CorrectionOut"];
export type ScalarField = Schemas["ScalarFieldOut"];
export type ListRow = Schemas["ListFieldOut"]["rows"][number];
/** A repeated group, such as line items: shown as a table. */
export type ListField = Schemas["ListFieldOut"];
export type Field = ScalarField | ListField;
/** GET /documents/{id}/fields: one document as the portal shows it. */
export type DocumentFields = Schemas["DocumentFieldsOut"];

// --- Still to build ------------------------------------------------------------------

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
