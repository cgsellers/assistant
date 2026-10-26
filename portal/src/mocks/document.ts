import { HttpResponse, http } from "msw";
import type { DocumentFields } from "@/api/contract";
import { MOCK_DOCUMENTS } from "./documents";
import { invoicePdf, receiptPhoto } from "./files";
import { readFrom, saleFor } from "./sales";

/** One document: its fields as the portal shows them, and its original. */

const notFound = () => HttpResponse.json({ detail: "No such document" }, { status: 404 });

const PROCESSORS = {
  invoice: { name: "invoice", label: "Invoice", version: 3 },
  receipt: { name: "receipt", label: "Receipt", version: 2 },
};

export function fieldsFor(id: string): DocumentFields | null {
  const row = MOCK_DOCUMENTS.find((d) => d.id === id);
  if (!row) return null;
  const sale = saleFor(row);
  const read = row.outcome === "done" || row.outcome === "needs_review";
  const { fields, problems } = read ? readFrom(sale) : { fields: [], problems: [] };
  return {
    document: {
      ...row,
      mime_type: sale.kind === "invoice" ? "application/pdf" : "image/jpeg",
      size_bytes: sale.kind === "invoice" ? 2400 + sale.index * 13 : 48000 + sale.index * 731,
    },
    processor: read ? PROCESSORS[sale.kind] : null,
    currency: read ? sale.currency : null,
    fields,
    problems,
    read_at: read ? new Date(Date.parse(row.received_at) + 41_000).toISOString() : null,
  };
}

export const singleDocumentHandlers = [
  http.get("/api/documents/:id/fields", ({ params }) => {
    const body = fieldsFor(String(params.id));
    return body ? HttpResponse.json(body) : notFound();
  }),

  http.get("/api/documents/:id/file", async ({ params }) => {
    const row = MOCK_DOCUMENTS.find((d) => d.id === params.id);
    if (!row) return notFound();
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
