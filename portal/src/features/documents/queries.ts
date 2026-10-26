import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router";
import type {
  DocumentFields,
  DocumentList,
  DocumentQuery,
  DocumentSummary,
  DocumentTypes,
  Outcome,
  SortKey,
} from "@/api/contract";
import { getJson } from "@/lib/http";

export const PAGE_SIZE = 25;

const OUTCOMES: readonly Outcome[] = [
  "processing",
  "done",
  "needs_review",
  "unsupported",
  "failed",
];

/**
 * The table's filters live in the URL: a filtered view can be bookmarked or
 * sent to a colleague, and the back button undoes a filter. Anything in the
 * URL that is not a valid value is ignored rather than sent on.
 */
export function useDocumentQuery() {
  const [params, setParams] = useSearchParams();

  const query = useMemo<DocumentQuery>(() => {
    const outcome = params.get("outcome");
    const sort = params.get("sort");
    return {
      outcome: OUTCOMES.includes(outcome as Outcome) ? (outcome as Outcome) : undefined,
      type: params.get("type") || undefined,
      q: params.get("q") || undefined,
      from: params.get("from") || undefined,
      to: params.get("to") || undefined,
      sort: sort === "document_date" ? "document_date" : undefined,
      cursor: params.get("cursor") || undefined,
    };
  }, [params]);

  /**
   * Change filters; any change starts again from the first page. Paging passes
   * the cursor itself, and the trail of earlier pages as navigation state.
   */
  const update = useCallback(
    (changes: QueryChanges, state?: PageTrail) => {
      setParams(
        (current) => {
          const next = new URLSearchParams(current);
          for (const [key, value] of Object.entries(changes)) {
            if (value) next.set(key, value);
            else next.delete(key);
          }
          if (!("cursor" in changes)) next.delete("cursor");
          return next;
        },
        { state },
      );
    },
    [setParams],
  );

  return { query, update };
}

export type { SortKey };

export type QueryChanges = Partial<Record<keyof DocumentQuery, string | undefined>>;

/**
 * Cursors are opaque, so "previous page" cannot be computed from one: the
 * cursors of the pages before are carried in the history entry instead.
 */
export interface PageTrail {
  trail: string[];
}

export function useDocuments(query: DocumentQuery) {
  return useQuery({
    queryKey: ["documents", query],
    queryFn: () => getJson<DocumentList>("/documents", { ...query, limit: PAGE_SIZE }),
    // Keep the last page on screen while the next loads: no flash of empty table.
    placeholderData: keepPreviousData,
  });
}

/** The headline numbers for the same filters, without paging or sorting. */
export function useSummary({ cursor: _cursor, sort: _sort, ...filters }: DocumentQuery) {
  return useQuery({
    queryKey: ["documents", "summary", filters],
    queryFn: () => getJson<DocumentSummary>("/documents/summary", filters),
    placeholderData: keepPreviousData,
  });
}

export function useDocumentTypes() {
  return useQuery({
    queryKey: ["documents", "types"],
    queryFn: () => getJson<DocumentTypes>("/documents/types"),
    staleTime: 5 * 60_000,
  });
}

/** How often a document still being read is asked about again. */
export const POLL_MS = 3000;

/** One document as the portal shows it; asks again while it is still being read. */
export function useDocumentFields(id: string) {
  return useQuery({
    queryKey: ["documents", "one", id],
    queryFn: () => getJson<DocumentFields>(`/documents/${encodeURIComponent(id)}/fields`),
    refetchInterval: (query) =>
      query.state.data?.document.outcome === "processing" ? POLL_MS : false,
  });
}
