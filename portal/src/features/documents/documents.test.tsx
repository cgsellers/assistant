import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { MOCK_DOCUMENTS } from "@/mocks/documents";
import { setSignedIn } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { renderAt } from "@/test/render";

beforeEach(() => setSignedIn(true));

/** The table's body rows, once the first page is on screen. */
async function rows() {
  const table = await screen.findByRole("table", { name: undefined, busy: false });
  await waitFor(() => expect(within(table).getAllByRole("row").length).toBeGreaterThan(1));
  return within(table).getAllByRole("row").slice(1);
}

function rowFor(supplier: string) {
  const link = screen.getAllByRole("link", { name: supplier })[0];
  const row = link?.closest("tr");
  if (!row) throw new Error(`no row for ${supplier}`);
  return row;
}

describe("the documents table", () => {
  it("shows the newest page with both dates, totals as written and plain statuses", async () => {
    renderAt("/documents");
    expect(await rows()).toHaveLength(25);

    const globe = rowFor("Globe Logistics");
    expect(within(globe).getByText("€91.19")).toBeInTheDocument();
    expect(within(globe).getByText("Ready")).toBeInTheDocument();
    expect(within(globe).getByText("globe-inv-7701.pdf")).toBeInTheDocument();

    // Still being read: no supplier yet, so the file name stands in, and every gap says so.
    const first = rowFor("northwind-inv-7700.pdf");
    expect(within(first).getByText("Processing")).toBeInTheDocument();
    expect(within(first).getByText("No total")).toBeInTheDocument();
    expect(within(first).getByText("No date on the document")).toBeInTheDocument();
  });

  it("marks a document in review with no total as a gap, not as zero", async () => {
    renderAt("/documents");
    await rows();
    const contoso = rowFor("Contoso Cloud");
    expect(within(contoso).getByText("Needs review")).toBeInTheDocument();
    expect(within(contoso).getByText("No total")).toBeInTheDocument();
    expect(within(contoso).queryByText(/\$0\.00/)).not.toBeInTheDocument();
  });

  it("opens a document from anywhere on its row", async () => {
    const { user, router } = renderAt("/documents");
    await rows();
    await user.click(within(rowFor("Globe Logistics")).getByText("Ready"));
    await waitFor(() => expect(router.state.location.pathname).toBe("/documents/doc-002"));
  });

  it("pages forward and back, and stops at the last page", async () => {
    const { user, router } = renderAt("/documents");
    await rows();
    expect(screen.getByText("Page 1")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Previous/ })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText("Page 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Next/ }));
    expect(await screen.findByText("Page 3")).toBeInTheDocument();
    await waitFor(async () => expect(await rows()).toHaveLength(64 - 50));
    expect(screen.getByRole("button", { name: /Next/ })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /Previous/ }));
    expect(await screen.findByText("Page 2")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /Previous/ }));
    expect(await screen.findByText("Page 1")).toBeInTheDocument();
    expect(router.state.location.search).toBe("");
  });

  it("sorts by the document's own date when asked", async () => {
    const { user, router } = renderAt("/documents");
    await rows();
    await user.click(screen.getByRole("button", { name: /Document date/ }));
    await waitFor(() => expect(router.state.location.search).toBe("?sort=document_date"));
    expect(screen.getByRole("columnheader", { name: /Document date/ })).toHaveAttribute(
      "aria-sort",
      "descending",
    );
  });
});

describe("filters", () => {
  it("are read from the URL, so a filtered view can be bookmarked", async () => {
    renderAt("/documents?outcome=needs_review");
    const shown = await rows();
    const expected = MOCK_DOCUMENTS.filter((d) => d.outcome === "needs_review").length;
    expect(shown).toHaveLength(expected);
    for (const row of shown) expect(within(row).getByText("Needs review")).toBeInTheDocument();
  });

  it("search waits for a pause, then filters -- and never eats what is still being typed", async () => {
    const { user, router } = renderAt("/documents");
    await rows();
    const box = screen.getByLabelText("Search supplier or file name");

    await user.type(box, "globe ");
    await waitFor(() => expect(router.state.location.search).toBe("?q=globe"));
    // The trailing space was not put in the URL, and must not be taken out of the box.
    await user.type(box, "log");
    expect(box).toHaveValue("globe log");
    await waitFor(() => expect(router.state.location.search).toBe("?q=globe+log"));
    // "globe" also found an unrecognised globe-inv-7709.pdf; "globe log" only the supplier.
    await waitFor(() => expect(screen.queryByText("globe-inv-7709.pdf")).not.toBeInTheDocument());
    for (const row of await rows())
      expect(within(row).getByText("Globe Logistics")).toBeInTheDocument();
  });

  it("explain an empty result and clear in one step", async () => {
    const { user, router } = renderAt("/documents?q=nobody-by-this-name");
    expect(await screen.findByText("No documents match these filters")).toBeInTheDocument();
    expect(screen.getByLabelText("Search supplier or file name")).toHaveValue(
      "nobody-by-this-name",
    );

    await user.click(screen.getAllByRole("button", { name: /Clear filters/ })[0] as HTMLElement);
    await waitFor(() => expect(router.state.location.search).toBe(""));
    expect(await rows()).toHaveLength(25);
    expect(screen.getByLabelText("Search supplier or file name")).toHaveValue("");
  });

  it("go into the export, without the page", async () => {
    renderAt("/documents?outcome=done&q=globe&cursor=25");
    const link = await screen.findByRole("link", { name: /Export CSV/ });
    const href = link.getAttribute("href") ?? "";
    const params = new URL(href, "http://x").searchParams;
    expect(params.get("outcome")).toBe("done");
    expect(params.get("q")).toBe("globe");
    expect(params.has("cursor")).toBe(false);
  });
});

describe("the summary", () => {
  it("adds up spend per currency and never across them", async () => {
    renderAt("/documents");
    await rows();
    const spend = screen.getByText("Spend").closest("div") as HTMLElement;
    const lines = within(spend)
      .getAllByRole("listitem")
      .map((li) => li.textContent);
    expect(lines).toHaveLength(3);
    expect(lines.some((l) => l?.startsWith("€"))).toBe(true);
    expect(lines.some((l) => l?.startsWith("£"))).toBe(true);
    expect(lines.some((l) => l?.startsWith("$"))).toBe(true);
  });

  it("takes you straight to what needs review", async () => {
    const { user, router } = renderAt("/documents?type=invoice");
    await rows();
    await user.click(screen.getByRole("button", { name: /need review — show them/ }));
    await waitFor(() => expect(router.state.location.search).toBe("?outcome=needs_review"));
  });
});

describe("when there is nothing, or something went wrong", () => {
  it("welcomes a new client instead of showing an empty table", async () => {
    server.use(
      http.get("/api/documents", () => HttpResponse.json({ items: [], next_cursor: null })),
    );
    renderAt("/documents");
    expect(await screen.findByText("No documents yet")).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says what failed and offers to try again", async () => {
    let fail = true;
    server.use(
      http.get("/api/documents", () =>
        fail ? HttpResponse.json({ detail: "The service is busy" }, { status: 503 }) : undefined,
      ),
    );
    const { user } = renderAt("/documents");
    expect(await screen.findByRole("alert")).toHaveTextContent("The service is busy");

    fail = false;
    await user.click(screen.getByRole("button", { name: "Try again" }));
    expect(await rows()).toHaveLength(25);
  });
});
