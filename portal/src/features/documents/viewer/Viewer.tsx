import { Download, ExternalLink, FileWarning, Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import { lazy, Suspense, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";

const PdfPages = lazy(() => import("./PdfPages"));

/** Zoom, as a multiple of fitting the panel's width. */
const ZOOMS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3];
const FIT = ZOOMS.indexOf(1);
const PADDING = 32;

/** The width of an element, kept current as the window resizes. */
function useWidth() {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry) setWidth(entry.contentRect.width);
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return { ref, width };
}

/**
 * The original document, beside what was read from it: a PDF's pages or the
 * photo, as uploaded. Anything the browser cannot show -- a TIFF outside
 * Safari, a damaged PDF -- falls back to downloading it.
 */
export function Viewer({
  url,
  mimeType,
  filename,
}: {
  url: string;
  mimeType: string;
  filename: string;
}) {
  const [zoom, setZoom] = useState(FIT);
  const [failed, setFailed] = useState(false);
  const { ref, width } = useWidth();
  const shown = Math.max(200, (width - PADDING) * (ZOOMS[zoom] ?? 1));
  const isPdf = mimeType === "application/pdf";

  return (
    <section
      aria-label="Original document"
      className="flex h-full flex-col overflow-hidden rounded-lg border border-line bg-[#e8e8eb]"
    >
      <div className="flex items-center gap-1 border-b border-line bg-surface px-2 py-1.5">
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label="Zoom out"
          disabled={failed || zoom === 0}
          onClick={() => setZoom((z) => Math.max(0, z - 1))}
        >
          <ZoomOut />
        </Button>
        <span className="tabular w-12 text-center text-xs text-muted" aria-live="polite">
          {zoom === FIT ? "Fit" : `${Math.round((ZOOMS[zoom] ?? 1) * 100)}%`}
        </span>
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label="Zoom in"
          disabled={failed || zoom === ZOOMS.length - 1}
          onClick={() => setZoom((z) => Math.min(ZOOMS.length - 1, z + 1))}
        >
          <ZoomIn />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          className="size-8"
          aria-label="Fit to width"
          disabled={failed || zoom === FIT}
          onClick={() => setZoom(FIT)}
        >
          <Maximize2 />
        </Button>
        <div className="ml-auto flex items-center gap-1">
          <Button variant="ghost" size="sm" asChild>
            <a href={url} target="_blank" rel="noopener">
              <ExternalLink /> Open
            </a>
          </Button>
          <Button variant="ghost" size="sm" asChild>
            <a href={url} download={filename}>
              <Download /> Download
            </a>
          </Button>
        </div>
      </div>

      <div ref={ref} className="min-h-0 flex-1 overflow-auto p-4">
        {failed ? (
          <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
            <FileWarning className="size-8 text-faint" />
            <p className="max-w-xs text-sm text-muted">
              This file can't be shown in the browser. Download it to open it on your computer.
            </p>
          </div>
        ) : isPdf ? (
          <Suspense
            fallback={
              <div className="flex h-64 items-center justify-center">
                <Spinner />
              </div>
            }
          >
            <PdfPages url={url} width={shown} onError={() => setFailed(true)} />
          </Suspense>
        ) : (
          <img
            src={url}
            alt={`The original: ${filename}`}
            style={{ width: shown }}
            className="mx-auto max-w-none rounded-sm shadow-pop"
            onError={() => setFailed(true)}
          />
        )}
      </div>
    </section>
  );
}
