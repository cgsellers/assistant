import { Download, ExternalLink, FileWarning, Maximize2, ZoomIn, ZoomOut } from "lucide-react";
import { Component, lazy, type ReactNode, Suspense, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Highlight, type Pointing } from "./Highlight";

const PdfPages = lazy(() => import("./PdfPages"));

/**
 * A failure inside the PDF viewer -- a damaged file, a browser missing
 * something PDF.js needs -- becomes the viewer's own "can't be shown" state,
 * rather than an error that takes the whole page and its fields with it.
 */
class Contained extends Component<
  { onError: () => void; children: ReactNode },
  { failed: boolean }
> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidCatch(error: Error) {
    console.error("The document viewer failed:", error);
    this.props.onError();
  }

  render() {
    return this.state.failed ? null : this.props.children;
  }
}

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
 * photo, as uploaded, with the value being looked at highlighted on it.
 * Anything the browser cannot show -- a TIFF outside Safari, a damaged PDF --
 * falls back to downloading it.
 */
export function Viewer({
  url,
  mimeType,
  filename,
  pointing = null,
}: {
  url: string;
  mimeType: string;
  filename: string;
  /** Where on the original to highlight: the value being looked at or corrected. */
  pointing?: Pointing | null;
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
            <Contained onError={() => setFailed(true)}>
              <PdfPages
                url={url}
                width={shown}
                pointing={pointing}
                onError={() => setFailed(true)}
              />
            </Contained>
          </Suspense>
        ) : (
          <div className="relative mx-auto" style={{ width: shown }}>
            <img
              src={url}
              alt={`The original: ${filename}`}
              className="block w-full rounded-sm shadow-pop"
              onError={() => setFailed(true)}
            />
            {pointing?.location.page === 1 && (
              <Highlight box={pointing.location.box} scroll={pointing.scroll} />
            )}
          </div>
        )}
      </div>
    </section>
  );
}
