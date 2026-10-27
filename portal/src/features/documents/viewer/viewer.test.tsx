import { screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { setSignedIn } from "@/mocks/handlers";
import { renderAt } from "@/test/render";

vi.mock("./PdfPages", () => ({
  default: () => {
    throw new Error("PDF.js could not cope");
  },
}));

beforeEach(() => setSignedIn(true));

describe("when the PDF viewer fails", () => {
  it("offers the download, and the rest of the page still works", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    renderAt("/documents/doc-002");
    expect(await screen.findByText(/can't be shown in the browser/)).toBeInTheDocument();
    expect(screen.getByText("What was read")).toBeInTheDocument();
    expect(screen.queryByText(/Unexpected Application Error/)).not.toBeInTheDocument();
  });
});

describe("iterating a stream where the browser cannot", () => {
  it("reads every chunk, as Safari now does through the polyfill", async () => {
    const prototype = ReadableStream.prototype as unknown as Record<PropertyKey, unknown>;
    const native = { iterator: prototype[Symbol.asyncIterator], values: prototype.values };
    delete prototype[Symbol.asyncIterator];
    delete prototype.values;
    try {
      vi.resetModules();
      await import("./stream-iteration");
      const stream = new ReadableStream<number>({
        start(controller) {
          for (const n of [1, 2, 3]) controller.enqueue(n);
          controller.close();
        },
      });
      const seen: number[] = [];
      for await (const chunk of stream as unknown as AsyncIterable<number>) seen.push(chunk);
      expect(seen).toEqual([1, 2, 3]);
      expect(stream.locked).toBe(false);
    } finally {
      prototype[Symbol.asyncIterator] = native.iterator;
      prototype.values = native.values;
    }
  });
});
