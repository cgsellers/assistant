import { useEffect, useRef } from "react";
import type { Location } from "@/api/contract";

/**
 * A box over the original where a value was read. Positioned in fractions of
 * the page, so it stays put at any zoom. Brought into view when `scroll`
 * changes -- on a click, not on every hover, which would make the page jump.
 */
export function Highlight({ box, scroll }: { box: Location["box"]; scroll: number }) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (scroll)
      ref.current?.scrollIntoView?.({ block: "center", inline: "center", behavior: "smooth" });
  }, [scroll]);
  const [left, top, right, bottom] = box;
  return (
    <div
      ref={ref}
      data-testid="highlight"
      aria-hidden
      className="pointer-events-none absolute z-10 rounded-[3px] bg-brand/15 ring-2 ring-brand/70 transition-all duration-150"
      style={{
        left: `${left * 100}%`,
        top: `${top * 100}%`,
        width: `${(right - left) * 100}%`,
        height: `${(bottom - top) * 100}%`,
      }}
    />
  );
}

/** What the viewer is asked to point at. */
export interface Pointing {
  location: Location;
  scroll: number;
}
