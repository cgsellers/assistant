import workerSrc from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useState } from "react";
import { Document, Page, pdfjs } from "react-pdf";
import "react-pdf/dist/Page/TextLayer.css";
import { Spinner } from "@/components/ui/spinner";

// PDF.js parses in a worker, served from our own origin like everything else.
pdfjs.GlobalWorkerOptions.workerSrc = workerSrc;

/** Height over width of an A4 page: the space a page holds while it renders. */
const A4 = 297 / 210;

/**
 * Every page of a PDF, stacked, at `width` pixels. A module of its own so
 * PDF.js -- most of the viewer's weight -- loads only when a PDF is opened.
 * The text layer stays on: text in the original can be selected and copied.
 */
export default function PdfPages({
  url,
  width,
  onError,
}: {
  url: string;
  width: number;
  onError: () => void;
}) {
  const [pages, setPages] = useState(0);
  return (
    <Document
      file={url}
      onLoadSuccess={({ numPages }) => setPages(numPages)}
      onLoadError={onError}
      onSourceError={onError}
      loading={<Loading />}
      error={null}
      className="flex flex-col items-center gap-4"
    >
      {Array.from({ length: pages }, (_, i) => (
        <Page
          // biome-ignore lint/suspicious/noArrayIndexKey: a page's number is its identity
          key={i}
          pageNumber={i + 1}
          width={width}
          renderAnnotationLayer={false}
          loading={<div style={{ width, height: width * A4 }} className="bg-surface" />}
          className="overflow-hidden rounded-sm bg-surface shadow-pop"
        />
      ))}
    </Document>
  );
}

function Loading() {
  return (
    <div className="flex h-64 items-center justify-center">
      <Spinner />
    </div>
  );
}
