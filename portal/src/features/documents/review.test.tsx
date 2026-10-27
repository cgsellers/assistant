import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ListField, ScalarField } from "@/api/contract";
import { fieldsFor } from "@/mocks/document";
import { setSignedIn } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { renderAt } from "@/test/render";

vi.mock("./viewer/PdfPages", () => ({
  default: ({ pointing }: { pointing: { location: unknown } | null }) => (
    <div data-testid="pdf" data-pointing={pointing ? JSON.stringify(pointing.location) : ""} />
  ),
}));

beforeEach(() => setSignedIn(true));

const heading = (name: string) => screen.findByRole("heading", { level: 1, name });
/** A field's row, read or being edited (where a required field's label says so to screen readers). */
const exactly = (label: string) => new RegExp(`^${label}( \\(required\\))?$`);
const fieldRow = (label: string) =>
  screen.getByText(label, { selector: "dt, dt label" }).closest("dt")?.parentElement as HTMLElement;
const input = (label: string) => screen.getByLabelText(exactly(label));
const button = (name: string | RegExp) => screen.getByRole("button", { name });

function scalar(id: string, name: string): ScalarField {
  const field = fieldsFor(id)?.fields.find((f) => f.name === name);
  if (!field || field.type === "list") throw new Error(`${id} ${name}`);
  return field;
}

function items(id: string): ListField {
  const field = fieldsFor(id)?.fields.find((f) => f.name === "line_items");
  if (field?.type !== "list") throw new Error(id);
  return field;
}

/** Count the PATCHes that reach the API, without changing what it answers. */
function countPatches() {
  const sent = { count: 0 };
  server.use(
    http.patch("/api/documents/:id/fields", () => {
      sent.count += 1;
      return undefined;
    }),
  );
  return sent;
}

describe("fixing a document in review", () => {
  it("starts at the flagged field, and a corrected total makes the document ready", async () => {
    const { user, router } = renderAt("/documents/doc-004");
    await heading("Contoso Cloud");
    await user.click(button("Review and fix"));

    const total = input("Total");
    expect(total).toHaveFocus();
    await user.type(total, "1234.5");
    expect(screen.getByText("1 field changed")).toBeInTheDocument();
    await user.click(button("Save changes"));

    expect(await screen.findByText("Saved. The document is ready.")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Needs your review" })).not.toBeInTheDocument();
    const row = fieldRow("Total");
    expect(within(row).getByText("$1,234.50")).toBeInTheDocument();
    expect(within(row).getByText("Corrected")).toBeInTheDocument();
    expect(within(row).getByText("Nothing was read · changed by bo@acme.test")).toBeInTheDocument();
    expect(
      within(screen.getByRole("list", { name: "History" })).getByText("Corrected Total"),
    ).toBeInTheDocument();

    // The list agrees: same status, and the total it now has.
    await router.navigate("/documents?q=contoso-inv-7703");
    const listed = await screen.findByRole("link", { name: "Contoso Cloud" });
    const listedRow = listed.closest("tr") as HTMLElement;
    expect(within(listedRow).getByText("Ready")).toBeInTheDocument();
    expect(within(listedRow).getByText("$1,234.50")).toBeInTheDocument();
  });

  it("refuses an amount it would have to guess at, and sends nothing", async () => {
    const sent = countPatches();
    const { user } = renderAt("/documents/doc-004");
    await heading("Contoso Cloud");
    await user.click(button("Review and fix"));
    await user.type(input("Total"), "1,234");

    expect(
      within(fieldRow("Total")).getByText("Enter an amount, like 1234.56"),
    ).toBeInTheDocument();
    expect(input("Total")).toHaveAttribute("aria-invalid", "true");
    await user.click(button("Save changes"));
    expect(await screen.findByText("Some values need fixing before saving.")).toBeInTheDocument();
    expect(sent.count).toBe(0);
  });

  it("checks again after saving: a problem left over keeps it in review until someone approves it", async () => {
    const { user } = renderAt("/documents/doc-011");
    await heading("Coop Italia");
    await user.click(button("Edit"));
    const vendor = input("Vendor");
    await user.clear(vendor);
    await user.type(vendor, "Coop Italia Milano");
    await user.click(button("Save changes"));

    expect(await screen.findByText("Saved. One problem remains.")).toBeInTheDocument();
    expect(await heading("Coop Italia Milano")).toBeInTheDocument();
    expect(screen.getByRole("status", { name: "Needs your review" })).toHaveTextContent(
      "The line items add up to",
    );

    await user.click(button("Approve as it is"));
    const dialog = await screen.findByRole("dialog", { name: "Approve with one problem?" });
    expect(within(dialog).getByRole("button", { name: "Cancel" })).toHaveFocus();
    await user.click(within(dialog).getByRole("button", { name: "Approve" }));

    expect(await screen.findByText("Approved. The document is ready.")).toBeInTheDocument();
    expect(screen.queryByRole("status", { name: "Needs your review" })).not.toBeInTheDocument();
    const history = screen.getByRole("list", { name: "History" });
    expect(within(history).getByText("Corrected Vendor")).toBeInTheDocument();
    expect(within(history).getByText("Approved as it is")).toBeInTheDocument();
  });

  it("fixes line items so they add up, and the rules agree", async () => {
    const lines = items("doc-011");
    const total = Math.round(Number(scalar("doc-011", "total").value) * 100);
    const others = lines.rows
      .slice(0, -1)
      .reduce((sum, row) => sum + Math.round(Number(row.amount) * 100), 0);
    const last = lines.rows.length;

    const { user } = renderAt("/documents/doc-011");
    await heading("Coop Italia");
    await user.click(button("Review and fix"));
    const amount = screen.getByLabelText(`Amount, row ${last}`);
    await user.clear(amount);
    await user.type(amount, ((total - others) / 100).toFixed(2));
    // A row added by mistake and taken out again changes nothing.
    await user.click(button("Add a row"));
    await user.click(button(`Remove row ${last + 1}`));
    await user.click(button("Save changes"));

    expect(await screen.findByText("Saved. The document is ready.")).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Line items" })).getByText(
        "Changed by bo@acme.test",
      ),
    ).toBeInTheDocument();
  });

  it("works a calculated total out again from a corrected subtotal", async () => {
    const tax = Number(scalar("doc-013", "tax_amount").value);
    const { user } = renderAt("/documents/doc-013");
    await heading("Fabrikam Furniture");
    await user.click(button("Edit"));
    const subtotal = input("Subtotal");
    await user.clear(subtotal);
    await user.type(subtotal, "1000");
    await user.click(button("Save changes"));

    await screen.findByText(/^Saved\./);
    const total = fieldRow("Total");
    expect(
      within(total).getByText(
        `£${(1000 + tax).toLocaleString("en-US", { minimumFractionDigits: 2 })}`,
      ),
    ).toBeInTheDocument();
    expect(within(total).getByText("Calculated")).toBeInTheDocument();
    // The printed lines no longer add up to the subtotal, and the rules say so.
    expect(screen.getByRole("status", { name: "Needs your review" })).toHaveTextContent(
      "but the subtotal says 1000.00",
    );
  });

  it("puts everything back on cancel", async () => {
    const { user } = renderAt("/documents/doc-002");
    await heading("Globe Logistics");
    await user.click(button("Edit"));
    await user.clear(input("Vendor"));
    await user.click(button("Cancel"));
    expect(within(fieldRow("Vendor")).getByText("Globe Logistics")).toBeInTheDocument();
    expect(screen.queryByText("Corrected")).not.toBeInTheDocument();
  });
});

describe("when saving does not go through", () => {
  it("shows the latest version when someone else saved first", async () => {
    server.use(
      http.patch("/api/documents/:id/fields", () =>
        HttpResponse.json(
          { detail: "Someone else changed this document since you opened it." },
          { status: 409 },
        ),
      ),
    );
    const { user } = renderAt("/documents/doc-004");
    await heading("Contoso Cloud");
    await user.click(button("Review and fix"));
    await user.type(input("Total"), "10");
    await user.click(button("Save changes"));

    expect(
      await screen.findByText(/Your changes were not saved; this is the latest version/),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Save changes" })).not.toBeInTheDocument();
  });

  it("shows what the API refused at the field it refused", async () => {
    server.use(
      http.patch("/api/documents/:id/fields", () =>
        HttpResponse.json(
          {
            detail: "Some values don't fit their fields.",
            fields: { total: "Larger than any invoice allows" },
          },
          { status: 422 },
        ),
      ),
    );
    const { user } = renderAt("/documents/doc-004");
    await heading("Contoso Cloud");
    await user.click(button("Review and fix"));
    await user.type(input("Total"), "99999999");
    await user.click(button("Save changes"));

    expect(
      await within(fieldRow("Total")).findByText("Larger than any invoice allows"),
    ).toBeInTheDocument();
    expect(button("Save changes")).toBeInTheDocument();
  });

  it("asks before leaving with changes unsaved", async () => {
    const { user, router } = renderAt("/documents/doc-004");
    await heading("Contoso Cloud");
    await user.click(button("Review and fix"));
    await user.type(input("Total"), "10");

    await user.click(screen.getByRole("link", { name: "Back to Documents" }));
    const dialog = await screen.findByRole("dialog", { name: "Discard your changes?" });
    await user.click(within(dialog).getByRole("button", { name: "Keep editing" }));
    expect(router.state.location.pathname).toBe("/documents/doc-004");
    expect(input("Total")).toHaveValue("10");

    await user.click(screen.getByRole("link", { name: "Back to Documents" }));
    await user.click(await screen.findByRole("button", { name: "Discard" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/documents"));
  });
});

describe("pointing at the original", () => {
  it("highlights where a value was read on the photo, and keeps it when pinned", async () => {
    const { user } = renderAt("/documents/doc-003");
    await heading("Coop Italia");
    const where = scalar("doc-003", "total").location;
    if (!where) throw new Error("no location");

    await user.hover(fieldRow("Total"));
    const box = await screen.findByTestId("highlight");
    expect(box.style.left).toBe(`${where.box[0] * 100}%`);
    expect(box.style.top).toBe(`${where.box[1] * 100}%`);
    await user.unhover(fieldRow("Total"));
    expect(screen.queryByTestId("highlight")).not.toBeInTheDocument();

    await user.click(button("Show Total on the document"));
    await user.unhover(fieldRow("Total"));
    expect(screen.getByTestId("highlight")).toBeInTheDocument();
  });

  it("asks the PDF viewer to point at a value, and at the input being corrected", async () => {
    const { user } = renderAt("/documents/doc-002");
    await heading("Globe Logistics");
    const pdf = await screen.findByTestId("pdf");

    await user.hover(fieldRow("Invoice number"));
    expect(JSON.parse(pdf.dataset.pointing ?? "null")).toEqual(
      scalar("doc-002", "invoice_number").location,
    );

    await user.unhover(fieldRow("Invoice number"));
    await user.click(button("Edit"));
    await user.click(input("Due date"));
    expect(JSON.parse(screen.getByTestId("pdf").dataset.pointing ?? "null")).toEqual(
      scalar("doc-002", "due_date").location,
    );
  });
});
