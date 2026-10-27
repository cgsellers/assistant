/**
 * `for await (const chunk of stream)` over a ReadableStream, where the browser
 * lacks it. PDF.js reads a page's text that way to build the selectable text
 * layer, and Safari 26 does not support it yet: without this, opening a PDF
 * there threw "undefined is not a function (near '...value of readableStream...')".
 *
 * The standard behaviour, installed only when missing: read until done, and
 * cancel the stream if the loop is left early, as the spec does. A side-effect
 * import, before PDF.js runs (see PdfPages).
 */

type Iterable = ReadableStream & {
  values?: (options?: { preventCancel?: boolean }) => AsyncIterableIterator<unknown>;
  [Symbol.asyncIterator]?: () => AsyncIterableIterator<unknown>;
};

const prototype = (globalThis.ReadableStream?.prototype ?? null) as Iterable | null;

if (prototype && typeof prototype[Symbol.asyncIterator] !== "function") {
  async function* values(this: ReadableStream, { preventCancel = false } = {}) {
    const reader = this.getReader();
    let finished = false;
    try {
      while (true) {
        const { done, value } = await reader.read();
        if (done) {
          finished = true;
          return;
        }
        yield value;
      }
    } finally {
      if (!finished && !preventCancel) await reader.cancel();
      reader.releaseLock();
    }
  }
  Object.defineProperty(prototype, "values", { value: values, writable: true, configurable: true });
  Object.defineProperty(prototype, Symbol.asyncIterator, {
    value: values,
    writable: true,
    configurable: true,
  });
}
