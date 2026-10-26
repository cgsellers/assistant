import { Search, X } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import type { DocumentQuery, DocumentTypes } from "@/api/contract";
import { OUTCOME_LABEL } from "@/components/status-badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  ANY,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import type { QueryChanges } from "./queries";

/** Search waits for a pause in typing, so the table does not refetch per keystroke. */
const SEARCH_DELAY_MS = 300;

export function Filters({
  query,
  types,
  update,
}: {
  query: DocumentQuery;
  types: DocumentTypes;
  update: (changes: QueryChanges) => void;
}) {
  const [search, setSearch] = useState(query.q ?? "");
  // What this box last put in the URL. When the URL's q changes to anything
  // else -- Clear filters, the back button -- the box follows; when it is only
  // catching up with earlier typing, the box is left alone.
  const sent = useRef(query.q ?? "");

  useEffect(() => {
    const q = query.q ?? "";
    if (q !== sent.current) {
      sent.current = q;
      setSearch(q);
    }
  }, [query.q]);

  useEffect(() => {
    const q = search.trim();
    if (q === sent.current) return;
    const timer = setTimeout(() => {
      sent.current = q;
      update({ q: q || undefined });
    }, SEARCH_DELAY_MS);
    return () => clearTimeout(timer);
  }, [search, update]);

  const filtered = Boolean(query.outcome || query.type || query.q || query.from || query.to);

  return (
    <div className="mb-4 flex flex-wrap items-center gap-2">
      <div className="relative w-full max-w-xs">
        <Search className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-faint" />
        <Input
          aria-label="Search supplier or file name"
          placeholder="Search supplier or file"
          className="pl-9"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
      </div>

      <Select
        value={query.outcome ?? ANY}
        onValueChange={(value) => update({ outcome: value === ANY ? undefined : value })}
      >
        <SelectTrigger aria-label="Status">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>Any status</SelectItem>
          {(Object.keys(OUTCOME_LABEL) as (keyof typeof OUTCOME_LABEL)[]).map((outcome) => (
            <SelectItem key={outcome} value={outcome}>
              {OUTCOME_LABEL[outcome]}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <Select
        value={query.type ?? ANY}
        onValueChange={(value) => update({ type: value === ANY ? undefined : value })}
      >
        <SelectTrigger aria-label="Type">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ANY}>Any type</SelectItem>
          {types.map((type) => (
            <SelectItem key={type.name} value={type.name}>
              {type.label}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>

      <div className="flex items-center gap-1.5 text-sm text-muted">
        <span>Received</span>
        <Input
          type="date"
          aria-label="Received from"
          className="w-[9.5rem]"
          value={query.from ?? ""}
          max={query.to}
          onChange={(e) => update({ from: e.target.value || undefined })}
        />
        <span>to</span>
        <Input
          type="date"
          aria-label="Received to"
          className="w-[9.5rem]"
          value={query.to ?? ""}
          min={query.from}
          onChange={(e) => update({ to: e.target.value || undefined })}
        />
      </div>

      {filtered && (
        <Button
          variant="ghost"
          size="sm"
          onClick={() =>
            update({
              outcome: undefined,
              type: undefined,
              q: undefined,
              from: undefined,
              to: undefined,
            })
          }
        >
          <X /> Clear filters
        </Button>
      )}
    </div>
  );
}
