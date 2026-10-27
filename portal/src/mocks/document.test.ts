import { describe, expect, it } from "vitest";
import type { DocumentFields, FieldsRefused } from "@/api/contract";
import { fieldsFor } from "./document";
import { MOCK_DOCUMENTS } from "./documents";
import { MOCK_USER } from "./user";

describe("the mock documents", () => {
  it("read and checked from scratch, agree with the list about every document", () => {
    for (const row of MOCK_DOCUMENTS) {
      const view = fieldsFor(row.id);
      expect(view?.document, row.id).toMatchObject({
        outcome: row.outcome,
        total: row.total,
        supplier: row.supplier,
        document_date: row.document_date,
      });
    }
  });

  it("put every printed value somewhere on its page", () => {
    for (const row of MOCK_DOCUMENTS) {
      for (const field of fieldsFor(row.id)?.fields ?? []) {
        if (field.type === "list") continue;
        const printed =
          field.value !== null && !field.derived && !["currency", "subtotal"].includes(field.name);
        if (printed) expect(field.location, `${row.id} ${field.name}`).not.toBeNull();
        for (const edge of field.location?.box ?? []) {
          expect(edge).toBeGreaterThanOrEqual(0);
          expect(edge).toBeLessThanOrEqual(1);
        }
      }
    }
  });
});

describe("PATCH /documents/{id}/fields, as the backend must answer it", () => {
  const url = (id: string) => new URL(`/api/documents/${id}/fields`, window.location.origin);
  const patch = (id: string, body: unknown, csrf = MOCK_USER.csrf_token ?? "") =>
    fetch(url(id), {
      method: "PATCH",
      headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf },
      body: JSON.stringify(body),
    });

  it("refuses a change made against an old revision, and keeps the document as it was", async () => {
    const first = await patch("doc-004", { revision: 1, values: { total: "10.00" } });
    expect(first.status).toBe(200);
    expect(((await first.json()) as DocumentFields).revision).toBe(2);

    const stale = await patch("doc-004", { revision: 1, values: { total: "99.00" } });
    expect(stale.status).toBe(409);
    expect(fieldsFor("doc-004")?.document.total?.amount).toBe("10.00");
  });

  it("refuses a change without the session's CSRF token", async () => {
    expect((await patch("doc-004", { revision: 1, values: {} }, "")).status).toBe(403);
  });

  it("names each field whose value does not fit", async () => {
    const response = await patch("doc-002", {
      revision: 1,
      values: { total: "12.345", issued_on: "24/10/2026", nonsense: "x" },
    });
    expect(response.status).toBe(422);
    expect(((await response.json()) as FieldsRefused).fields).toEqual({
      total: 'An amount, like "1234.56"',
      issued_on: "A date, YYYY-MM-DD",
      nonsense: "No such field",
    });
  });

  it("holds an approval only until the next correction", async () => {
    await patch("doc-011", { revision: 1, values: {}, approve: true });
    expect(fieldsFor("doc-011")?.document.outcome).toBe("done");
    await patch("doc-011", { revision: 2, values: { vendor: "Coop" } });
    expect(fieldsFor("doc-011")?.document.outcome).toBe("needs_review");
  });
});
