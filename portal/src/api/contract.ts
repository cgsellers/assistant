import type { components } from "@/api/schema";

/**
 * The names the screens use for the API's types. It began as the contract for
 * endpoints the API did not have yet: the mocks implemented these shapes, the
 * screens were built against them, then the backend was built to match. Every
 * endpoint is built now, so each name is the generated schema's type, and
 * `npm run api` plus the type checker point at any disagreement.
 */

// --- Built: the documents list ------------------------------------------------------
// GET /documents, /summary, /types and /export are in the API. Their types come
// from its schema (src/api/schema.d.ts); the names stay, so the screens keep using them.

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
// GET and PATCH /documents/{id}/fields are in the API.

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

/**
 * PATCH /documents/{id}/fields: correct values, approve, or both. Answered
 * with the document's new DocumentFields; 409 when `revision` is not the
 * current one (or nothing has been read), 422 when a value does not fit its
 * field, with `fields` saying which.
 */
export type FieldsPatch = Schemas["FieldsPatchIn"];
export type FieldsRefused = Schemas["FieldsRefusedOut"];

// --- Built: settings ------------------------------------------------------------------
// /settings/keys and /settings/members are in the API. For people only: a key is
// refused. Anyone signed in may read; every change needs an admin (403).

/**
 * What a person at a client may do. Not the team's "author", which is about
 * writing processors -- though a member of the team's own organisation may
 * hold it, which is why a Member's role can be one more than this.
 *   admin   -- everything a member can, plus API keys and the team
 *   member  -- upload, review and correct
 *   viewer  -- see documents and export them; change nothing
 */
export type ClientRole = "admin" | "member" | "viewer";

/** One live API key. Revoked keys are not listed. */
export type ApiKeyRow = Schemas["ApiKeyOut"];
/** A key, with its secret: the only time the secret is ever shown. */
export type CreatedKey = Schemas["CreatedKeyOut"];
/** One person at the organisation, or someone invited. */
export type Member = Schemas["MemberOut"];
/** An invitation, as a link for an admin to pass on: Ledgerline does not send email yet. */
export type Invitation = Schemas["InvitationCreatedOut"];
/** Whose invitation a link is (POST /auth/invitation). */
export type InvitationOffer = Schemas["InvitationOut"];
