/**
 * How amounts and dates are shown. The person's own locale throughout; the
 * values themselves stay exact strings from the API until this last step.
 */

const locale = () => (typeof navigator !== "undefined" && navigator.language) || "en-GB";

/** "£104.88" -- in the document's own currency, or a bare number if it never said. */
export function formatMoney(amount: string, currency: string | null): string {
  const value = Number(amount);
  if (!currency) {
    return new Intl.NumberFormat(locale(), { minimumFractionDigits: 2 }).format(value);
  }
  return new Intl.NumberFormat(locale(), { style: "currency", currency }).format(value);
}

/**
 * A date printed on a document, "2026-10-21" -> "21 Oct 2026". Built from its
 * parts, never through `new Date(string)`: that reads it as UTC midnight, and
 * anywhere west of Greenwich it would show as the day before.
 */
export function formatDocumentDate(isoDate: string): string {
  const [year, month, day] = isoDate.split("-").map(Number) as [number, number, number];
  return new Intl.DateTimeFormat(locale(), {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(new Date(year, month - 1, day));
}

/** A moment, in the person's own time zone: "26 Oct 2026, 17:40". */
export function formatMoment(iso: string): string {
  return new Intl.DateTimeFormat(locale(), {
    day: "numeric",
    month: "short",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  }).format(new Date(iso));
}

/** The day of a moment, in the person's own time zone: "26 Oct 2026". */
export function formatDay(iso: string): string {
  return new Intl.DateTimeFormat(locale(), {
    day: "numeric",
    month: "short",
    year: "numeric",
  }).format(new Date(iso));
}

/** The time of a moment, in the person's own time zone: "17:40". */
export function formatTime(iso: string): string {
  return new Intl.DateTimeFormat(locale(), { hour: "2-digit", minute: "2-digit" }).format(
    new Date(iso),
  );
}
