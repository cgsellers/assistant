import { AlertTriangle, LocateFixed } from "lucide-react";
import type {
  Correction,
  Field,
  FieldType,
  FieldValue,
  ListField,
  ScalarField,
} from "@/api/contract";
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

export const isNumeric = (type: FieldType) =>
  type === "money" || type === "number" || type === "integer";

/**
 * Point at a value on the original: `key` is a field name or "list.row".
 * `pin` keeps it highlighted, and brings it into view.
 */
export type Point = (key: string | null, pin?: boolean) => void;

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
  return <span className={cn(isNumeric(type) && "tabular")}>{text}</span>;
}

function Badge({
  tone,
  title,
  children,
}: {
  tone: "brand" | "ok";
  title: string;
  children: string;
}) {
  return (
    <span
      title={title}
      className={cn(
        "rounded px-1.5 py-px text-[11px] font-medium",
        tone === "brand" ? "bg-brand-soft text-brand" : "bg-ok-soft text-ok",
      )}
    >
      {children}
    </span>
  );
}

export function Problem({ text }: { text: string }) {
  return (
    <p className="mt-1 flex items-start gap-1.5 text-xs text-attention">
      <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden />
      {text}
    </p>
  );
}

/** What was there before someone changed it: the audit trail, in plain sight. */
function WasRead({
  correction,
  type,
  currency,
}: {
  correction: Correction;
  type: FieldType;
  currency: string | null;
}) {
  const was = formatValue(correction.was, type, currency);
  return (
    <p className="mt-0.5 text-xs text-muted">
      {was ? `Read as ${was}` : "Nothing was read"} · changed by {correction.by}
    </p>
  );
}

function Locate({ label, onClick }: { label: string; onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={`Show ${label} on the document`}
      title="Show on the document"
      className="rounded p-0.5 text-faint opacity-0 transition-opacity group-hover:opacity-100 hover:text-brand focus-visible:opacity-100"
    >
      <LocateFixed className="size-3.5" />
    </button>
  );
}

function Scalar({
  field,
  currency,
  active,
  point,
}: {
  field: ScalarField;
  currency: string | null;
  active: boolean;
  point: Point;
}) {
  const located = field.location !== null;
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: hovering only previews what the row's Locate button does, which is how keyboards and screen readers reach it
    <div
      onMouseEnter={located ? () => point(field.name) : undefined}
      onMouseLeave={located ? () => point(null) : undefined}
      className={cn(
        "group grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] gap-4 border-b border-line px-5 py-2.5 transition-colors last:border-0",
        field.problem && "bg-attention-soft/60",
        active && "bg-brand-soft/70",
      )}
    >
      <dt className="flex items-start gap-1 text-sm text-muted">
        {field.label}
        {located && <Locate label={field.label} onClick={() => point(field.name, true)} />}
      </dt>
      <dd className="min-w-0 text-sm">
        <div className="flex flex-wrap items-center gap-2 break-words">
          <Value value={field.value} type={field.type} currency={currency} />
          {field.derived && (
            <Badge tone="brand" title="Worked out from other fields, not printed on the document">
              Calculated
            </Badge>
          )}
          {field.corrected && (
            <Badge tone="ok" title={`Changed by ${field.corrected.by}`}>
              Corrected
            </Badge>
          )}
        </div>
        {field.corrected && (
          <WasRead correction={field.corrected} type={field.type} currency={currency} />
        )}
        {field.problem && <Problem text={field.problem} />}
      </dd>
    </div>
  );
}

function List({
  field,
  currency,
  active,
  point,
}: {
  field: ListField;
  currency: string | null;
  active: string | null;
  point: Point;
}) {
  return (
    <section aria-label={field.label} className="border-t border-line px-5 py-4">
      <h3 className="mb-2 flex items-center gap-2 text-sm font-medium">
        {field.label}
        <span className="text-xs font-normal text-muted">{field.rows.length}</span>
        {field.derived && (
          <Badge tone="brand" title="Worked out from other fields, not printed on the document">
            Calculated
          </Badge>
        )}
        {field.corrected && (
          <Badge tone="ok" title={`Changed by ${field.corrected.by}`}>
            Corrected
          </Badge>
        )}
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
                      isNumeric(column.type) && "text-right",
                    )}
                  >
                    {column.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {field.rows.map((row, i) => {
                const key = `${field.name}.${i}`;
                const located = Boolean(field.row_locations[i]);
                return (
                  <tr
                    // biome-ignore lint/suspicious/noArrayIndexKey: rows as read, in document order, never reordered
                    key={i}
                    onMouseEnter={located ? () => point(key) : undefined}
                    onMouseLeave={located ? () => point(null) : undefined}
                    onClick={located ? () => point(key, true) : undefined}
                    className={cn(
                      "border-t border-line transition-colors",
                      located && "cursor-pointer",
                      active === key && "bg-brand-soft/70",
                    )}
                  >
                    {field.columns.map((column) => (
                      <td
                        key={column.name}
                        className={cn(
                          "px-3 py-2 align-top",
                          isNumeric(column.type) && "text-right",
                        )}
                      >
                        <Value
                          value={row[column.name] ?? null}
                          type={column.type}
                          currency={currency}
                        />
                      </td>
                    ))}
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {field.corrected && (
        <p className="mt-1.5 text-xs text-muted">Changed by {field.corrected.by}</p>
      )}
    </section>
  );
}

/** What was read from a document, in its processor's order: values first, then tables. */
export function FieldList({
  fields,
  currency,
  active = null,
  point = () => {},
}: {
  fields: Field[];
  currency: string | null;
  /** The key being pointed at, shown as such. */
  active?: string | null;
  point?: Point;
}) {
  const scalars = fields.filter((f): f is ScalarField => f.type !== "list");
  const lists = fields.filter((f): f is ListField => f.type === "list");
  return (
    <>
      <dl>
        {scalars.map((field) => (
          <Scalar
            key={field.name}
            field={field}
            currency={currency}
            active={active === field.name}
            point={point}
          />
        ))}
      </dl>
      {lists.map((field) => (
        <List key={field.name} field={field} currency={currency} active={active} point={point} />
      ))}
    </>
  );
}
