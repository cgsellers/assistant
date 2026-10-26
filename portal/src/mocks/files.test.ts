import { getDocument } from "pdfjs-dist/legacy/build/pdf.mjs";
import { describe, expect, it } from "vitest";
import { MOCK_DOCUMENTS } from "./documents";
import { invoicePdf } from "./files";
import { saleFor } from "./sales";

/** The text PDF.js reads from a generated invoice: proof the file is a valid PDF. */
async function textOf(bytes: Uint8Array): Promise<string> {
  const pdf = await getDocument({ data: bytes, useSystemFonts: false }).promise;
  const page = await pdf.getPage(1);
  const content = await page.getTextContent();
  return content.items.map((item) => ("str" in item ? item.str : "")).join("\n");
}

function row(id: string) {
  const found = MOCK_DOCUMENTS.find((d) => d.id === id);
  if (!found) throw new Error(id);
  return found;
}

describe("the mock invoices", () => {
  it("are PDFs that PDF.js opens, printed in the supplier's own formats", async () => {
    // doc-002: Globe Logistics, Rotterdam -- euros and dates the Dutch way.
    const text = await textOf(invoicePdf(saleFor(row("doc-002"))));
    expect(text).toContain("Globe Logistics");
    expect(text).toContain("INV-7701");
    expect(text).toContain("€ 91,19");
    expect(text).toMatch(/\d{1,2}-\d{1,2}-2026/);
  });

  it("leave the total off when the document did not print one", async () => {
    const cutOff = await textOf(invoicePdf(saleFor(row("doc-004"))));
    expect(cutOff).toContain("Continued on page 2");
    expect(cutOff).not.toContain("Total due");
  });
});
