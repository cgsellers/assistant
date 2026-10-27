import { HttpResponse, http } from "msw";
import type {
  DocumentFields,
  Field,
  FieldsPatch,
  FieldType,
  FieldValue,
  ListRow,
  ScalarField,
} from "@/api/contract";
import { type Saved, save, savedFor } from "./corrections";
import { MOCK_DOCUMENTS } from "./documents";
import { invoiceLocations, invoicePdf, receiptLocations, receiptPhoto } from "./files";
import { check, readFrom, saleFor } from "./sales";
import { hasCsrf, MOCK_USER } from "./user";

/** One document: its fields as the portal shows them, its original, and corrections. */

const refuse = (status: number, detail: string, fields?: Record<string, string>) =>
  HttpResponse.json({ detail, ...(fields && { fields }) }, { status });

const PROCESSORS = {
  invoice: { name: "invoice", label: "Invoice", version: 3 },
  receipt: { name: "receipt", label: "Receipt", version: 2 },
};

/** The values as read, with anything saved since laid over them. */
function corrected(read: Field[], saved: Saved | undefined): Field[] {
  if (!saved) return read;
  return read.map((field): Field => {
    if (!saved.values.has(field.name)) return field;
    const value = saved.values.get(field.name) ?? null;
    const who = saved.by.get(field.name) ?? { by: MOCK_USER.name, at: "" };
    if (field.type === "list") {
      const rows = (value ?? []) as ListRow[];
      return {
        ...field,
        rows,
        // Rows kept their place on the page; rows someone added have none.
        row_locations: rows.map((_, i) => field.row_locations[i] ?? null),
        corrected: { ...who, was: null },
      };
    }
    return { ...field, value: value as FieldValue, corrected: { ...who, was: field.value } };
  });
}

export function fieldsFor(id: string): DocumentFields | null {
  const row = MOCK_DOCUMENTS.find((d) => d.id === id);
  if (!row) return null;
  const sale = saleFor(row);
  const document = {
    ...row,
    mime_type: sale.kind === "invoice" ? "application/pdf" : "image/jpeg",
    size_bytes: sale.kind === "invoice" ? 2400 + sale.index * 13 : 48000 + sale.index * 731,
  };
  const received = { at: row.received_at, what: "Received", by: null };

  if (row.outcome !== "done" && row.outcome !== "needs_review") {
    return {
      document,
      processor: null,
      currency: null,
      fields: [],
      problems: [],
      read_at: null,
      revision: 1,
      history: [received],
    };
  }

  const saved = savedFor(id);
  const where = sale.kind === "invoice" ? invoiceLocations(sale) : receiptLocations(sale);
  const { fields, problems } = check(sale.kind, corrected(readFrom(sale, where), saved));
  const value = (name: string) => {
    const field = fields.find((f): f is ScalarField => f.name === name && f.type !== "list");
    return field?.value ?? null;
  };
  const flagged = problems.length > 0 || fields.some((f) => f.problem);
  const readAt = new Date(Date.parse(row.received_at) + 41_000).toISOString();
  const processor = PROCESSORS[sale.kind];
  const total = value("total");
  return {
    document: {
      ...document,
      outcome: flagged && !saved?.approved ? "needs_review" : "done",
      supplier: (value("vendor") as string | null) ?? null,
      document_date: (value("issued_on") as string | null) ?? null,
      total:
        total === null
          ? null
          : { amount: String(total), currency: (value("currency") as string | null) ?? null },
    },
    processor,
    currency: (value("currency") as string | null) ?? sale.currency,
    fields,
    problems,
    read_at: readAt,
    revision: saved?.revision ?? 1,
    history: [
      received,
      { at: readAt, what: `Read as ${processor.label.toLowerCase()}`, by: null },
      ...(row.outcome === "needs_review"
        ? [{ at: readAt, what: "Held for review", by: null }]
        : []),
      ...(saved?.history ?? []),
    ],
  };
}

// --- Checking what was sent ---------------------------------------------------------

function fits(type: FieldType, value: unknown): string | null {
  if (value === null) return null;
  switch (type) {
    case "money":
      return typeof value === "string" && /^-?\d+(\.\d{1,2})?$/.test(value)
        ? null
        : 'An amount, like "1234.56"';
    case "date":
      return typeof value === "string" && /^\d{4}-\d{2}-\d{2}$/.test(value)
        ? null
        : "A date, YYYY-MM-DD";
    case "integer":
      return Number.isInteger(value) ? null : "A whole number";
    case "number":
      return typeof value === "number" && Number.isFinite(value) ? null : "A number";
    case "boolean":
      return typeof value === "boolean" ? null : "Yes or no";
    default:
      return typeof value === "string" ? null : "Text";
  }
}

function refusals(fields: Field[], values: FieldsPatch["values"]): Record<string, string> {
  const wrong: Record<string, string> = {};
  for (const [name, value] of Object.entries(values)) {
    const field = fields.find((f) => f.name === name);
    if (!field) {
      wrong[name] = "No such field";
    } else if (field.type === "list") {
      const rows = Array.isArray(value) ? (value as ListRow[]) : null;
      const bad = rows
        ? rows.flatMap((row) =>
            field.columns.map((c) => fits(c.type, row[c.name] ?? null)).filter(Boolean),
          )
        : ["A list of rows"];
      if (bad.length) wrong[name] = String(bad[0]);
    } else {
      const problem = fits(field.type, value);
      if (problem) wrong[name] = problem;
    }
  }
  return wrong;
}

export const singleDocumentHandlers = [
  http.get("/api/documents/:id/fields", ({ params }) => {
    const body = fieldsFor(String(params.id));
    return body ? HttpResponse.json(body) : refuse(404, "No such document");
  }),

  http.patch("/api/documents/:id/fields", async ({ params, request }) => {
    if (!hasCsrf(request)) return refuse(403, "missing or wrong CSRF token");
    const id = String(params.id);
    const current = fieldsFor(id);
    if (!current) return refuse(404, "No such document");
    if (!current.processor) return refuse(409, "Nothing has been read from this document yet.");
    const body = (await request.json()) as FieldsPatch;
    if (body.revision !== current.revision) {
      return refuse(409, "Someone else changed this document since you opened it.");
    }
    const wrong = refusals(current.fields, body.values);
    if (Object.keys(wrong).length) return refuse(422, "Some values don't fit their fields.", wrong);

    const at = new Date().toISOString();
    const prior = savedFor(id);
    const names = Object.keys(body.values);
    const labels = names.map((name) => current.fields.find((f) => f.name === name)?.label ?? name);
    const saved: Saved = {
      values: new Map([...(prior?.values ?? []), ...Object.entries(body.values)]),
      by: new Map([
        ...(prior?.by ?? []),
        ...names.map((name) => [name, { by: MOCK_USER.name, at }] as const),
      ]),
      approved: Boolean(body.approve),
      revision: current.revision + 1,
      history: [
        ...(prior?.history ?? []),
        ...(names.length
          ? [{ at, what: `Corrected ${labels.join(", ")}`, by: MOCK_USER.name }]
          : []),
        ...(body.approve ? [{ at, what: "Approved as it is", by: MOCK_USER.name }] : []),
      ],
      row: {},
    };
    save(id, saved);
    const after = fieldsFor(id) as DocumentFields;
    const { outcome, supplier, document_date, total } = after.document;
    saved.row = { outcome, supplier, document_date, total };
    return HttpResponse.json(after);
  }),

  http.get("/api/documents/:id/file", async ({ params }) => {
    const row = MOCK_DOCUMENTS.find((d) => d.id === params.id);
    if (!row) return refuse(404, "No such document");
    const sale = saleFor(row);
    const { bytes, type } =
      sale.kind === "invoice"
        ? { bytes: invoicePdf(sale), type: "application/pdf" }
        : await receiptPhoto(sale);
    return new HttpResponse(bytes, {
      headers: {
        "Content-Type": type,
        "Content-Disposition": `inline; filename="${row.filename ?? "document"}"`,
      },
    });
  }),
];
