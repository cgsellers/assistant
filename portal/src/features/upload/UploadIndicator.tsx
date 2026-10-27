import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { NavLink } from "react-router";
import { Spinner } from "@/components/ui/spinner";
import { pending, uploads, useUploads } from "./queue";

/**
 * What uploads mean for the whole app, wherever the person is: the list and
 * summary refresh as each document arrives, and closing the tab mid-upload
 * asks first.
 */
export function useUploadsEverywhere() {
  const queries = useQueryClient();
  const left = pending(useUploads());

  useEffect(
    () =>
      uploads.onReceived(() => {
        void queries.invalidateQueries({
          queryKey: ["documents"],
          predicate: (q) => q.queryKey[1] !== "one",
        });
      }),
    [queries],
  );

  useEffect(() => {
    if (!left) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [left]);
}

/** "Uploading 2 of 5" in the header while anything is on its way; a way back to the list. */
export function UploadIndicator() {
  const items = useUploads();
  const left = pending(items);
  if (!left) return null;
  return (
    <NavLink
      to="/upload"
      className="inline-flex items-center gap-1.5 rounded-full bg-brand-soft px-2.5 py-1 text-xs font-medium text-brand hover:bg-brand-soft/70"
    >
      <Spinner className="size-3" label="Uploading" />
      Uploading {left} of {items.length}
    </NavLink>
  );
}
