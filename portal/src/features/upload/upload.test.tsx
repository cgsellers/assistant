import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { setSignedIn } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { setReadDelay } from "@/mocks/uploads";
import { renderAt } from "@/test/render";

beforeEach(() => setSignedIn(true));

const PDF = (text = "an invoice") => new TextEncoder().encode(`%PDF-1.4\n% ${text}\n%%EOF\n`);
const PNG = Uint8Array.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 1, 2, 3]);

function file(bytes: Uint8Array, name: string, type: string) {
  return new File([bytes as BlobPart], name, { type });
}

async function choose(...files: File[]) {
  const input = await screen.findByLabelText("Choose files to upload");
  fireEvent.change(input, { target: { files } });
}

const row = (name: string) =>
  within(screen.getByRole("list", { name: "Uploads" }))
    .getByText(name)
    .closest("li") as HTMLElement;

/** Hold every upload at the server until `release()`; counts what arrives. */
function holdUploads() {
  let release = () => {};
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  const seen = { count: 0 };
  server.use(
    http.post("/api/documents", async () => {
      seen.count += 1;
      await gate;
      return undefined;
    }),
  );
  return { release, seen };
}

describe("uploading", () => {
  it("sends a file, and follows the document until it has been read", async () => {
    setReadDelay(0);
    renderAt("/upload");
    await choose(file(PDF(), "acme-march.pdf", "application/pdf"));

    const uploaded = row("acme-march.pdf");
    expect(await within(uploaded).findByText("Needs review")).toBeInTheDocument();
    const open = within(uploaded).getByRole("link", { name: "Open" });
    expect(open).toHaveAttribute("href", "/documents/up-001");
  });

  it("shows a new document at the top of the list as soon as it arrives", async () => {
    const { user } = renderAt("/upload");
    await choose(file(PNG, "lunch.png", "image/png"));
    expect(await within(row("lunch.png")).findByText(/Received, being read/)).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Documents" }));
    const listed = (await screen.findByRole("link", { name: "lunch.png" })).closest(
      "tr",
    ) as HTMLElement;
    expect(screen.getAllByRole("row")[1]).toBe(listed);
    expect(within(listed).getByText("Processing")).toBeInTheDocument();
  });

  it("refuses what the server would refuse, before sending it", async () => {
    const { seen } = holdUploads();
    renderAt("/upload");
    const huge = file(PDF(), "scan.pdf", "application/pdf");
    Object.defineProperty(huge, "size", { value: 25 * 1024 * 1024 });
    await choose(file(new TextEncoder().encode("notes"), "notes.txt", "text/plain"), huge);

    expect(within(row("notes.txt")).getByText("Not an image or a PDF.")).toBeInTheDocument();
    expect(
      within(row("scan.pdf")).getByText("The file is 25.0 MB; the limit is 20 MB."),
    ).toBeInTheDocument();
    expect(
      within(row("notes.txt")).queryByRole("button", { name: /Retry/ }),
    ).not.toBeInTheDocument();
    expect(seen.count).toBe(0);
  });

  it("says what the server refused, in its words, and offers no retry", async () => {
    renderAt("/upload");
    // Named like a photo; the bytes say otherwise, and the server goes by the bytes.
    await choose(file(new TextEncoder().encode("just text"), "receipt.jpg", "image/jpeg"));
    const refused = row("receipt.jpg");
    expect(
      await within(refused).findByText("Unrecognised file type. Expected an image or PDF."),
    ).toBeInTheDocument();
    expect(within(refused).queryByRole("button", { name: /Retry/ })).not.toBeInTheDocument();
  });

  it("recognises a file uploaded before", async () => {
    renderAt("/upload");
    await choose(file(PDF("same"), "first.pdf", "application/pdf"));
    await within(row("first.pdf")).findByText(/being read/);
    await choose(file(PDF("same"), "again.pdf", "application/pdf"));

    const again = row("again.pdf");
    expect(await within(again).findByText(/Already uploaded/)).toBeInTheDocument();
    expect(within(again).getByRole("link", { name: "Open" })).toHaveAttribute(
      "href",
      "/documents/up-001",
    );
  });

  it("offers a retry when the connection drops, and the retry goes through", async () => {
    let drops = 1;
    server.use(
      http.post("/api/documents", () => {
        if (drops-- > 0) return HttpResponse.error();
        return undefined;
      }),
    );
    const { user } = renderAt("/upload");
    await choose(file(PNG, "taxi.png", "image/png"));
    const failed = row("taxi.png");
    expect(
      await within(failed).findByText("The connection dropped before the file arrived."),
    ).toBeInTheDocument();

    await user.click(within(failed).getByRole("button", { name: /Retry/ }));
    expect(await within(row("taxi.png")).findByText(/Received, being read/)).toBeInTheDocument();
  });
});

describe("while files are on their way", () => {
  it("keeps going on other pages, and says so in the header", async () => {
    const { release } = holdUploads();
    const { user } = renderAt("/upload");
    await choose(
      file(PDF("a"), "a.pdf", "application/pdf"),
      file(PDF("b"), "b.pdf", "application/pdf"),
    );
    expect(await screen.findByRole("link", { name: /Uploading 2 of 2/ })).toBeInTheDocument();

    await user.click(screen.getByRole("link", { name: "Documents" }));
    expect(await screen.findByRole("heading", { name: "Documents" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Uploading 2 of 2/ })).toBeInTheDocument();

    release();
    await waitFor(() =>
      expect(screen.queryByRole("link", { name: /Uploading/ })).not.toBeInTheDocument(),
    );
    expect(await screen.findByRole("link", { name: "a.pdf" })).toBeInTheDocument();
  });

  it("can be cancelled", async () => {
    holdUploads();
    const { user } = renderAt("/upload");
    await choose(file(PDF(), "wrong-one.pdf", "application/pdf"));
    await user.click(await screen.findByRole("button", { name: "Cancel wrong-one.pdf" }));
    expect(screen.queryByRole("list", { name: "Uploads" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /Uploading/ })).not.toBeInTheDocument();
  });

  it("takes an image pasted anywhere on the page", async () => {
    renderAt("/upload");
    await screen.findByLabelText("Choose files to upload");
    const paste = new Event("paste", { bubbles: true });
    Object.defineProperty(paste, "clipboardData", {
      value: { files: [file(PNG, "image.png", "image/png")] },
    });
    window.dispatchEvent(paste);
    await screen.findByRole("list", { name: "Uploads" });
    expect(await within(row("image.png")).findByText(/Received, being read/)).toBeInTheDocument();
  });
});
