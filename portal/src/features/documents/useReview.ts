import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { useBlocker } from "react-router";
import { toast } from "sonner";
import type { DocumentFields } from "@/api/contract";
import { ApiError } from "@/lib/api";
import { changes, type Draft, toDraft } from "./edit";
import type { Point } from "./FieldList";
import { useSaveFields } from "./queries";
import type { Pointing } from "./viewer/Highlight";

interface Editing {
  draft: Draft;
  /** The draft as it started, to tell which values have been touched. */
  initial: Draft;
  /** The field to put the cursor in first. */
  focus: string | null;
  /** Saving has been tried: show every error, not just those on changed values. */
  attempted: boolean;
  /** What the API refused, by field (a 422). */
  refused: Record<string, string>;
}

/** Whether the text at `key` (a field name or a cell key) differs from how it started. */
function touched(key: string, draft: Draft, initial: Draft): boolean {
  const [name = "", row, column] = key.split(".");
  if (row === undefined || column === undefined) return draft[name] !== initial[name];
  const cells = (rows: Draft[string] | undefined) =>
    Array.isArray(rows) ? rows[Number(row)] : undefined;
  return cells(draft[name])?.[column] !== cells(initial[name])?.[column];
}

/** Where `key` -- a field name, or "list.row" -- was read on the original. */
function locate(view: DocumentFields, key: string | null) {
  if (!key) return null;
  const [name, row] = key.split(".");
  const field = view.fields.find((f) => f.name === name);
  if (!field) return null;
  if (field.type === "list")
    return row === undefined ? null : (field.row_locations[Number(row)] ?? null);
  return field.location;
}

/**
 * Reviewing one document: pointing at values on the original, editing them,
 * saving, approving, and not losing changes on the way out.
 */
export function useReview(view: DocumentFields) {
  const id = view.document.id;
  const save = useSaveFields(id);
  const queries = useQueryClient();
  const [editing, setEditing] = useState<Editing | null>(null);
  const [approving, setApproving] = useState(false);

  // --- Pointing ---------------------------------------------------------------
  const [hovered, setHovered] = useState<string | null>(null);
  const [pinned, setPinned] = useState<{ key: string; scroll: number } | null>(null);
  const scrolls = useRef(0);
  const point: Point = (key, pin = false) => {
    if (!pin) return setHovered(key);
    scrolls.current += 1;
    setPinned((current) =>
      key === null || (current?.key === key && !editing) ? null : { key, scroll: scrolls.current },
    );
  };
  const active = hovered ?? pinned?.key ?? null;
  const location = locate(view, active);
  const pointing: Pointing | null = location
    ? { location, scroll: pinned?.key === active ? pinned.scroll : 0 }
    : null;

  // --- Editing ------------------------------------------------------------------
  const pending = editing ? changes(view.fields, editing.draft) : null;
  // Fields whose text was edited, valid or not: what the counter shows and
  // what makes leaving ask first. Not the same as what saving would send.
  const changed = editing
    ? view.fields.filter(
        (f) => JSON.stringify(editing.draft[f.name]) !== JSON.stringify(editing.initial[f.name]),
      ).length
    : 0;
  const errors: Record<string, string> = {};
  if (editing && pending) {
    for (const [key, message] of Object.entries(pending.errors)) {
      if (editing.attempted || touched(key, editing.draft, editing.initial)) errors[key] = message;
    }
    Object.assign(errors, editing.refused);
  }

  function start(focus: string | null = null) {
    const draft = toDraft(view.fields);
    setEditing({ draft, initial: draft, focus, attempted: false, refused: {} });
  }

  function change(draft: Draft) {
    setEditing((current) => current && { ...current, draft, refused: {} });
  }

  function stop() {
    setEditing(null);
    setPinned(null);
  }

  function failed(error: Error) {
    if (error instanceof ApiError && error.status === 409) {
      // Someone saved first. Theirs is shown; these edits were made against
      // values that are no longer there, so they are not kept.
      toast.error(`${error.detail} Your changes were not saved; this is the latest version.`);
      stop();
      void queries.invalidateQueries({ queryKey: ["documents", "one", id] });
    } else if (error instanceof ApiError && error.status === 422) {
      setEditing((current) => current && { ...current, attempted: true, refused: error.fields });
      toast.error(error.detail);
    } else {
      toast.error(`Not saved: ${error.message}`);
    }
  }

  function submit() {
    if (!editing || !pending) return;
    if (Object.keys(pending.errors).length) {
      setEditing({ ...editing, attempted: true });
      toast.error("Some values need fixing before saving.");
      return;
    }
    if (Object.keys(pending.values).length === 0) {
      // Retyped the same, perhaps in another style: nothing to save.
      stop();
      toast("Nothing changed.");
      return;
    }
    save.mutate(
      { revision: view.revision, values: pending.values },
      {
        onSuccess: (after) => {
          stop();
          const left = after.problems.length + after.fields.filter((f) => f.problem).length;
          if (after.document.outcome === "done") toast.success("Saved. The document is ready.");
          else
            toast.warning(
              `Saved. ${left === 1 ? "One problem remains" : `${left} problems remain`}.`,
            );
        },
        onError: failed,
      },
    );
  }

  function approve() {
    save.mutate(
      { revision: view.revision, values: {}, approve: true },
      {
        onSuccess: () => {
          setApproving(false);
          toast.success("Approved. The document is ready.");
        },
        onError: (error) => {
          setApproving(false);
          failed(error);
        },
      },
    );
  }

  // --- Not losing changes ---------------------------------------------------------
  const dirty = changed > 0;
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => event.preventDefault();
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  return {
    point,
    active,
    pointing,
    editing,
    changed,
    errors,
    saving: save.isPending,
    start,
    change,
    cancel: stop,
    submit,
    approving,
    askApprove: () => setApproving(true),
    cancelApprove: () => setApproving(false),
    approve,
    leaving: blocker.state === "blocked",
    leave: () => blocker.proceed?.(),
    stay: () => blocker.reset?.(),
  };
}
