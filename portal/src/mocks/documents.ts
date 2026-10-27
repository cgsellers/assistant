import type { DocumentRow, DocumentTypes, Money, Outcome } from "@/api/contract";

/**
 * Acme's documents, as the mock API serves them. Deterministic -- the same
 * rows every run -- so tests can name them, and varied enough to look like a
 * real month: several suppliers and currencies, mostly ready, a few needing
 * review, one still being read, one not recognised.
 */
export const SUPPLIERS: [string, string, string][] = [
  ["Northwind Office Supply", "invoice", "GBP"],
  ["Globe Logistics", "invoice", "EUR"],
  ["Coop Italia", "receipt", "EUR"],
  ["Contoso Cloud", "invoice", "USD"],
  ["Fabrikam Furniture", "invoice", "GBP"],
  ["Tailspin Travel", "receipt", "GBP"],
  ["Wide World Importers", "invoice", "EUR"],
  ["Café Luna", "receipt", "EUR"],
];

export const TYPES: DocumentTypes = [
  { name: "invoice", label: "Invoice" },
  { name: "receipt", label: "Receipt" },
];

function outcomeFor(index: number): Outcome {
  if (index === 0) return "processing";
  if (index === 9) return "unsupported";
  if (index === 23) return "failed";
  if (index % 7 === 3) return "needs_review";
  return "done";
}

/** What document `index` adds up to: an invoice's hundreds or thousands, a receipt's few euros. */
export function amountFor(index: number): string {
  const kind = SUPPLIERS[index % SUPPLIERS.length]?.[1];
  const base =
    kind === "receipt" ? ((index * 7919) % 6000) / 100 + 3 : ((index * 7919) % 250000) / 100 + 12;
  return base.toFixed(2);
}

export const MOCK_DOCUMENTS: DocumentRow[] = Array.from({ length: 64 }, (_, index) => {
  const [supplier, type, currency] = SUPPLIERS[index % SUPPLIERS.length] as [
    string,
    string,
    string,
  ];
  const outcome = outcomeFor(index);
  // Newest first: two to three a day, back from 26 October 2026.
  const received = new Date(Date.UTC(2026, 9, 26, 17, 40) - index * 9.5 * 3600_000);
  const issued = new Date(received.getTime() - ((index % 5) + 1) * 86400_000);
  const read = outcome !== "processing" && outcome !== "unsupported" && outcome !== "failed";
  // Some documents in review are there because no total could be read --
  // the commonest reason, so the screen has to show it well.
  const missingTotal = outcome === "needs_review" && index % 2 === 1;
  const total: Money | null = read && !missingTotal ? { amount: amountFor(index), currency } : null;
  return {
    id: `doc-${String(index + 1).padStart(3, "0")}`,
    filename:
      type === "invoice"
        ? `${supplier.split(" ")[0]?.toLowerCase()}-inv-${7700 + index}.pdf`
        : `receipt-${index + 1}.jpg`,
    received_at: received.toISOString(),
    document_date: read ? issued.toISOString().slice(0, 10) : null,
    supplier: read ? supplier : null,
    type:
      outcome === "unsupported" || outcome === "processing"
        ? null
        : (TYPES.find((t) => t.name === type) ?? null),
    total,
    outcome,
  };
});
