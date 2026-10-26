import { AlertTriangle } from "lucide-react";
import type { Field, FieldType, FieldValue, ListField, ScalarField } from "@/api/contract";
import { formatDocumentDate, formatMoney } from "@/lib/format";
import { cn } from "@/lib/utils";

/** A value as a person reads it: in the document's currency, dates written out. */
export function formatValue(value: FieldValue, type: FieldType, currency: string | null): string {
  if (value === null || value === "") return "";
  switch (type) {
    case "money":
      return formatMoney(String(value), currency);
    case "date":
      return formatDocumentDate(String(value));
    case "number":
    case "integer":
      return Number(value).toLocaleString();
    case "boolean":
      return value ? "Yes" : "No";
    default:
      return String(value);
  }
}

function Value({
  value,
  type,
  currency,
}: {
  value: FieldValue;
  type: FieldType;
  currency: string | null;
}) {
  const text = formatValue(value, type, currency);
  if (!text) return <span className="text-faint">Not on the document</span>;
  return <span className={cn((type === "money" || type === "number") && "tabular")}>{text}</span>;
}

function Calculated() {
  return (
    <span
      className="rounded bg-brand-soft px-1.5 py-px text-[11px] font-medium text-brand"
      title="Worked out from other fields, not printed on the document"
    >
      Calculated
    </span>
  );
}

function Problem({ text }: { text: string }) {
  return (
    <p className="mt-1 flex items-start gap-1.5 text-xs text-attention">
      <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
      {text}
    </p>
  );
}

function Scalar({ field, currency }: { field: ScalarField; currency: string | null }) {
  return (
    <div
      className={cn(
        "grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-4 border-b border-line px-5 py-2.5 last:border-0",
        field.problem && "bg-attention-soft/60",
      )}
    >
      <dt className="text-sm text-muted">{field.label}</dt>
      <dd className="min-w-0 text-sm">
        <div className="flex flex-wrap items-center gap-2 break-words">
          <Value value={field.value} type={field.type} currency={currency} />
          {field.derived && <Calculated />}
        </div>
        {field.problem && <Problem text={field.problem} />}
      </dd>
    </div>
  );
}

function List({ field, currency }: { field: ListField; currency: string | null }) {
  const numeric = (type: FieldType) => type === "money" || type === "number" || type === "integer";
  return (
    <section aria-label={field.label} className="border-t border-line px-5 py-4">
      <h3 className="mb-2 flex items-center gap-2 text-sm font-medium">
        {field.label}
        <span className="text-xs font-normal text-muted">{field.rows.length}</span>
        {field.derived && <Calculated />}
      </h3>
      {field.problem && <Problem text={field.problem} />}
      {field.rows.length === 0 ? (
        <p className="text-sm text-faint">None on the document</p>
      ) : (
        <div className="overflow-x-auto rounded-md border border-line">
          <table className="w-full text-sm">
            <thead className="bg-canvas/60 text-xs text-muted">
              <tr>
                {field.columns.map((column) => (
                  <th
                    key={column.name}
                    className={cn(
                      "px-3 py-2 text-left font-medium",
                      numeric(column.type) && "text-right",
                    )}
                  >
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {field.rows.map((row, i) => (
                // biome-ignore lint/suspicious/noArrayIndexKey: rows as read, in document order, never reordered
                <tr key={i} className="border-t border-line">
                  {field.columns.map((column) => (
                    <td
                      key={column.name}
                      className={cn("px-3 py-2 align-top", numeric(column.type) && "text-right")}
                    >
                      <Value
                        value={row[column.name] ?? null}
                        type={column.type}
                        currency={currency}
                      />
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

/** What was read from a document, in its processor's order: values first, then tables. */
export function FieldList({ fields, currency }: { fields: Field[]; currency: string | null }) {
  const scalars = fields.filter((f): f is ScalarField => f.type !== "list");
  const lists = fields.filter((f): f is ListField => f.type === "list");
  return (
    <>
      <dl>
        {scalars.map((field) => (
          <Scalar key={field.name} field={field} currency={currency} />
        ))}
      </dl>
      {lists.map((field) => (
        <List key={field.name} field={field} currency={currency} />
      ))}
    </>
  );
}
