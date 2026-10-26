import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fieldsFor } from "@/mocks/document";
import { setSignedIn } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { renderAt } from "@/test/render";
import { POLL_MS } from "./queries";

// jsdom has no canvas to draw a PDF on. The generated PDFs are checked against
// the real PDF.js in src/mocks/files.test.ts; here it is enough to know which
// file the viewer was asked to show.
vi.mock("./viewer/PdfPages", () => ({
  default: ({ url }: { url: string }) => <div data-testid="pdf" data-url={url} />,
}));

beforeEach(() => setSignedIn(true));

const heading = (name: string | RegExp) => screen.findByRole("heading", { level: 1, name });

function fieldRow(label: string) {
  const term = screen.getByText(label, { selector: "dt" });
  return term.parentElement as HTMLElement;
}

describe("a document", () => {
  it("shows the original PDF beside what was read, formatted for the reader", async () => {
    renderAt("/documents/doc-002");
    expect(await heading("Globe Logistics")).toBeInTheDocument();
    expect(screen.getByText(/globe-inv-7701\.pdf · Received/)).toBeInTheDocument();

    expect(await screen.findByTestId("pdf")).toHaveAttribute(
      "data-url",
      "/api/documents/doc-002/file",
    );
    expect(within(fieldRow("Invoice number")).getByText("INV-7701")).toBeInTheDocument();
    expect(within(fieldRow("Total")).getByText("€91.19")).toBeInTheDocument();
    // Printed "24-10-2026" in Rotterdam; read and shown as a date, not a string.
    expect(within(fieldRow("Issued on")).getByText("Oct 24, 2026")).toBeInTheDocument();
    expect(within(fieldRow("Discount")).getByText("Not on the document")).toBeInTheDocument();
    expect(screen.getByText("Invoice · version 3")).toBeInTheDocument();
  });

  it("shows line items as a table", async () => {
    renderAt("/documents/doc-002");
    const items = await screen.findByRole("region", { name: "Line items" });
    const expected = fieldsFor("doc-002")?.fields.find((f) => f.name === "line_items");
    const rows = within(items).getAllByRole("row").slice(1);
    expect(rows).toHaveLength(expected && "rows" in expected ? expected.rows.length : -1);
    expect(within(items).getByText("Fuel surcharge")).toBeInTheDocument();
  });

  it("shows a receipt photo as an image, and offers a download when the browser cannot show it", async () => {
    renderAt("/documents/doc-003");
    const photo = await screen.findByRole("img", { name: "The original: receipt-3.jpg" });
    expect(photo).toHaveAttribute("src", "/api/documents/doc-003/file");

    fireEvent.error(photo);
    expect(await screen.findByText(/can't be shown in the browser/)).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Download/ })).toHaveAttribute(
      "href",
      "/api/documents/doc-003/file",
    );
  });

  it("marks a value the processor worked out rather than read", async () => {
    renderAt("/documents/doc-013");
    await heading("Fabrikam Furniture");
    expect(within(fieldRow("Total")).getByText("Calculated")).toBeInTheDocument();
    expect(within(fieldRow("Subtotal")).queryByText("Calculated")).not.toBeInTheDocument();
  });

  it("returns to the list exactly as it was filtered", async () => {
    const { user, router } = renderAt("/documents?outcome=needs_review");
    await user.click(
      (await screen.findAllByRole("link", { name: "Contoso Cloud" }))[0] as HTMLElement,
    );
    await heading("Contoso Cloud");
    await user.click(screen.getByRole("link", { name: "Back to Documents" }));
    await waitFor(() => expect(router.state.location.search).toBe("?outcome=needs_review"));
  });
});

describe("a document that needs review", () => {
  it("says why, at the field it is about", async () => {
    renderAt("/documents/doc-004");
    await heading("Contoso Cloud");
    expect(screen.getByRole("status", { name: "Needs your review" })).toHaveTextContent(
      "One field is flagged below.",
    );
    const total = fieldRow("Total");
    expect(within(total).getByText("Not on the document")).toBeInTheDocument();
    expect(within(total).getByText(/No total could be read/)).toBeInTheDocument();
  });

  it("lists problems that are about the document as a whole", async () => {
    renderAt("/documents/doc-011");
    await heading("Coop Italia");
    expect(screen.getByRole("status", { name: "Needs your review" })).toHaveTextContent(
      /The line items add up to .*, but the total says/,
    );
  });

  it("flags dates that contradict each other", async () => {
    renderAt("/documents/doc-025");
    await heading("Northwind Office Supply");
    expect(
      within(fieldRow("Due date")).getByText("The due date is before the invoice date."),
    ).toBeInTheDocument();
  });
});

describe("a document that was not read", () => {
  it("says it is being read, and updates by itself when it is", async () => {
    let calls = 0;
    server.use(
      http.get("/api/documents/doc-001/fields", () => {
        calls += 1;
        if (calls === 1) return undefined; // the mock as it is: still processing
        const done = fieldsFor("doc-002");
        if (!done) throw new Error("no doc-002");
        return HttpResponse.json({
          ...done,
          document: { ...done.document, id: "doc-001", outcome: "done" },
        });
      }),
    );
    renderAt("/documents/doc-001");
    expect(
      await screen.findByRole("status", { name: "Reading this document" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("What was read")).not.toBeInTheDocument();

    expect(
      await screen.findByText("What was read", {}, { timeout: POLL_MS + 2000 }),
    ).toBeInTheDocument();
    expect(screen.queryByText("Reading this document")).not.toBeInTheDocument();
  });

  it("explains a document no processor recognised", async () => {
    renderAt("/documents/doc-010");
    expect(await screen.findByRole("status", { name: "Not recognised" })).toBeInTheDocument();
    expect(screen.queryByText("What was read")).not.toBeInTheDocument();
  });

  it("suggests what to do about one that could not be read", async () => {
    renderAt("/documents/doc-024");
    expect(await screen.findByRole("status", { name: "Couldn't be read" })).toHaveTextContent(
      /clearer photo/,
    );
  });

  it("is not found, rather than an error, when it does not exist", async () => {
    renderAt("/documents/doc-999");
    expect(await screen.findByRole("alert")).toHaveTextContent("Document not found");
    expect(screen.getByRole("link", { name: "Back to documents" })).toHaveAttribute(
      "href",
      "/documents",
    );
  });
});
