import type { Field, FieldsPatch, FieldType, FieldValue, ListField, ListRow } from "@/api/contract";

/**
 * Editing a document's fields: what a person types, as text, turned back into
 * the API's values. Kept apart from the screens so the rules can be tested
 * one by one.
 */

/** Text per scalar field, and per cell of each list. */
export type Draft = Record<string, string | Record<string, string>[]>;

/** The key of one cell: "line_items.2.amount". */
export const cellKey = (field: string, row: number, column: string) => `${field}.${row}.${column}`;

export function toText(value: FieldValue): string {
  if (value === null) return "";
  if (typeof value === "boolean") return value ? "true" : "false";
  return String(value);
}

export function toDraft(fields: Field[]): Draft {
  const draft: Draft = {};
  for (const field of fields) {
    draft[field.name] =
      field.type === "list"
        ? field.rows.map((row) =>
            Object.fromEntries(field.columns.map((c) => [c.name, toText(row[c.name] ?? null)])),
          )
        : toText(field.value);
  }
  return draft;
}

type Parsed = { value: FieldValue } | { error: string };

/** "1234.5", "1,234.56", "1.234,56", "12,5" -> "1234.50" etc. Anything ambiguous is refused. */
function parseAmount(text: string): string | null {
  // \s covers the no-break spaces some locales group digits with.
  const t = text.replace(/[\s']/g, "");
  // "1,234" or "12.345": a thousands separator in one locale, a decimal point
  // in another. A wrong guess is off by a factor of a thousand.
  if (/^-?\d{1,3}[.,]\d{3}$/.test(t)) return null;
  let plain: string | null = null;
  if (/^-?\d+([.,]\d{1,2})?$/.test(t)) plain = t.replace(",", ".");
  else if (/^-?\d{1,3}(,\d{3})+(\.\d{1,2})?$/.test(t)) plain = t.replaceAll(",", "");
  else if (/^-?\d{1,3}(\.\d{3})+(,\d{1,2})?$/.test(t))
    plain = t.replaceAll(".", "").replace(",", ".");
  if (plain === null) return null;
  const [whole, fraction = ""] = plain.split(".");
  return `${whole}.${fraction.padEnd(2, "0")}`;
}

export function parse(type: FieldType, text: string, required = false): Parsed {
  const t = text.trim();
  if (t === "") return required ? { error: "Required" } : { value: null };
  switch (type) {
    case "money": {
      const amount = parseAmount(t);
      return amount === null ? { error: "Enter an amount, like 1234.56" } : { value: amount };
    }
    case "date":
      return /^\d{4}-\d{2}-\d{2}$/.test(t) && !Number.isNaN(Date.parse(t))
        ? { value: t }
        : { error: "Enter a date" };
    case "number":
    case "integer": {
      const n = Number(t.replace(",", "."));
      if (!Number.isFinite(n)) return { error: "Enter a number" };
      if (type === "integer" && !Number.isInteger(n)) return { error: "Enter a whole number" };
      return { value: n };
    }
    case "boolean":
      return { value: t === "true" };
    default:
      return { value: t };
  }
}

function sameRows(field: ListField, rows: ListRow[]): boolean {
  const ordered = (list: ListRow[]) =>
    JSON.stringify(list.map((row) => field.columns.map((c) => row[c.name] ?? null)));
  return ordered(field.rows) === ordered(rows);
}

/**
 * What the draft changes, ready to send, and what is wrong with it -- by
 * field name, or by cell key for a list.
 */
export function changes(
  fields: Field[],
  draft: Draft,
): { values: FieldsPatch["values"]; errors: Record<string, string> } {
  const values: FieldsPatch["values"] = {};
  const errors: Record<string, string> = {};
  for (const field of fields) {
    const text = draft[field.name];
    if (field.type === "list") {
      const rows: ListRow[] = [];
      (Array.isArray(text) ? text : []).forEach((cells, i) => {
        const row: ListRow = {};
        for (const column of field.columns) {
          const parsed = parse(column.type, cells[column.name] ?? "");
          if ("error" in parsed) errors[cellKey(field.name, i, column.name)] = parsed.error;
          else row[column.name] = parsed.value;
        }
        rows.push(row);
      });
      if (!sameRows(field, rows)) values[field.name] = rows;
    } else {
      const parsed = parse(field.type, typeof text === "string" ? text : "", field.required);
      if ("error" in parsed) errors[field.name] = parsed.error;
      else if (parsed.value !== field.value) values[field.name] = parsed.value;
    }
  }
  return { values, errors };
}
