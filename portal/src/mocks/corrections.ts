import type { FieldValue, ListRow } from "@/api/contract";

/**
 * What people have changed, per document: the mock API's memory between
 * requests. Tests start from nothing (src/test/setup.ts resets it).
 */
export interface Saved {
  values: Map<string, FieldValue | ListRow[]>;
  by: Map<string, { by: string; at: string }>;
  approved: boolean;
  revision: number;
  history: { at: string; what: string; by: string | null }[];
}

const store = new Map<string, Saved>();

export function savedFor(id: string): Saved | undefined {
  return store.get(id);
}

export function save(id: string, saved: Saved) {
  store.set(id, saved);
}

export function resetCorrections() {
  store.clear();
}
