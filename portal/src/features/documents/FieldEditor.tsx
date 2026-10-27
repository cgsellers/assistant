import { Plus, Trash2 } from "lucide-react";
import { useId } from "react";
import type { Field, FieldType, ListField, ScalarField } from "@/api/contract";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { cn } from "@/lib/utils";
import { cellKey, type Draft } from "./edit";
import { isNumeric, type Point, Problem } from "./FieldList";

const SELECT =
  "h-9 w-full rounded-md border border-line-strong bg-surface px-2.5 text-sm text-ink shadow-card focus-visible:border-brand focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand/20";

/** One value's input, shaped by its type. */
function ValueInput({
  id,
  type,
  value,
  currency,
  label,
  invalid,
  describedBy,
  autoFocus,
  compact,
  onChange,
  onFocus,
}: {
  id?: string;
  type: FieldType;
  value: string;
  currency: string | null;
  label?: string;
  invalid: boolean;
  describedBy?: string;
  autoFocus?: boolean;
  compact?: boolean;
  onChange: (text: string) => void;
  onFocus?: () => void;
}) {
  const common = {
    id,
    "aria-label": label,
    "aria-invalid": invalid || undefined,
    "aria-describedby": describedBy,
    autoFocus,
    onFocus,
  };
  const size = compact ? "h-8 px-2" : "";
  const ring = invalid
    ? "border-danger focus-visible:border-danger focus-visible:ring-danger/20"
    : "";

  if (type === "boolean") {
    return (
      <select
        {...common}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(SELECT, size, ring)}
      >
        <option value="">Not on the document</option>
        <option value="true">Yes</option>
        <option value="false">No</option>
      </select>
    );
  }
  if (type === "date") {
    return (
      <Input
        {...common}
        type="date"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        className={cn(size, ring)}
      />
    );
  }
  if (type === "money" && !compact) {
    return (
      <div className="relative">
        <Input
          {...common}
          inputMode="decimal"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className={cn("tabular pr-12 text-right", ring)}
        />
        {currency && (
          <span className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-xs text-muted">
            {currency}
          </span>
        )}
      </div>
    );
  }
  return (
    <Input
      {...common}
      inputMode={isNumeric(type) ? "decimal" : undefined}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className={cn(size, ring, isNumeric(type) && "tabular text-right")}
    />
  );
}

function ScalarEditor({
  field,
  text,
  error,
  currency,
  autoFocus,
  onChange,
  point,
}: {
  field: ScalarField;
  text: string;
  error: string | undefined;
  currency: string | null;
  autoFocus: boolean;
  onChange: (text: string) => void;
  point: Point;
}) {
  const id = useId();
  return (
    <div
      className={cn(
        "grid grid-cols-[minmax(0,2fr)_minmax(0,3fr)] items-start gap-4 border-b border-line px-5 py-2.5 last:border-0",
        field.problem && "bg-attention-soft/60",
      )}
    >
      <dt className="pt-2 text-sm text-muted">
        <label htmlFor={id}>
          {field.label}
          {field.required && <span className="sr-only"> (required)</span>}
        </label>
      </dt>
      <dd className="min-w-0">
        <ValueInput
          id={id}
          type={field.type}
          value={text}
          currency={currency}
          invalid={Boolean(error)}
          describedBy={error || field.problem ? `${id}-note` : undefined}
          autoFocus={autoFocus}
          onChange={onChange}
          onFocus={() => point(field.location ? field.name : null, true)}
        />
        <div id={`${id}-note`}>
          {error ? (
            <p className="mt-1 text-xs text-danger">{error}</p>
          ) : (
            field.problem && <Problem text={field.problem} />
          )}
        </div>
      </dd>
    </div>
  );
}

function ListEditor({
  field,
  rows,
  errors,
  currency,
  onChange,
  point,
}: {
  field: ListField;
  rows: Record<string, string>[];
  errors: Record<string, string>;
  currency: string | null;
  onChange: (rows: Record<string, string>[]) => void;
  point: Point;
}) {
  const blank = () => Object.fromEntries(field.columns.map((c) => [c.name, ""]));
  const wrong = rows.flatMap((_, i) =>
    field.columns.flatMap((c) => {
      const error = errors[cellKey(field.name, i, c.name)];
      return error ? [`Row ${i + 1}, ${c.label.toLowerCase()}: ${error}`] : [];
    }),
  );
  return (
    <section aria-label={field.label} className="border-t border-line px-5 py-4">
      <h3 className="mb-2 text-sm font-medium">{field.label}</h3>
      {field.problem && <Problem text={field.problem} />}
      <div className="overflow-x-auto rounded-md border border-line">
        <table className="w-full text-sm">
          <thead className="bg-canvas/60 text-xs text-muted">
            <tr>
              {field.columns.map((column) => (
                <th
                  key={column.name}
                  className={cn(
                    "px-2 py-2 text-left font-medium",
                    isNumeric(column.type) && "text-right",
                  )}
                >
                  {column.label}
                </th>
              ))}
              <th className="w-9">
                <span className="sr-only">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((cells, i) => (
              // biome-ignore lint/suspicious/noArrayIndexKey: rows are edited in place; their position is their identity
              <tr key={i} className="border-t border-line">
                {field.columns.map((column) => (
                  <td key={column.name} className="px-1.5 py-1.5">
                    <ValueInput
                      compact
                      type={column.type}
                      value={cells[column.name] ?? ""}
                      currency={currency}
                      label={`${column.label}, row ${i + 1}`}
                      invalid={Boolean(errors[cellKey(field.name, i, column.name)])}
                      onChange={(text) =>
                        onChange(
                          rows.map((row, j) => (j === i ? { ...row, [column.name]: text } : row)),
                        )
                      }
                      onFocus={() =>
                        point(field.row_locations[i] ? `${field.name}.${i}` : null, true)
                      }
                    />
                  </td>
                ))}
                <td className="px-1 text-center">
                  <Button
                    variant="ghost"
                    size="icon"
                    className="size-8 text-muted hover:text-danger"
                    aria-label={`Remove row ${i + 1}`}
                    onClick={() => onChange(rows.filter((_, j) => j !== i))}
                  >
                    <Trash2 />
                  </Button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {wrong.length > 0 && (
        <ul className="mt-1.5 space-y-0.5 text-xs text-danger">
          {wrong.map((message) => (
            <li key={message}>{message}</li>
          ))}
        </ul>
      )}
      <Button
        variant="ghost"
        size="sm"
        className="mt-2"
        onClick={() => onChange([...rows, blank()])}
      >
        <Plus /> Add a row
      </Button>
    </section>
  );
}

/**
 * The fields as inputs, in the same order and places as when reading them.
 * Errors show as soon as a value is changed, or for everything once saving
 * has been tried.
 */
export function FieldEditor({
  fields,
  currency,
  draft,
  errors,
  focus,
  onChange,
  point,
}: {
  fields: Field[];
  currency: string | null;
  draft: Draft;
  errors: Record<string, string>;
  /** The field to start in: the first one with a problem, when fixing a review. */
  focus: string | null;
  onChange: (draft: Draft) => void;
  point: Point;
}) {
  const scalars = fields.filter((f): f is ScalarField => f.type !== "list");
  const lists = fields.filter((f): f is ListField => f.type === "list");
  return (
    <>
      <dl>
        {scalars.map((field) => (
          <ScalarEditor
            key={field.name}
            field={field}
            text={(draft[field.name] as string | undefined) ?? ""}
            error={errors[field.name]}
            currency={currency}
            autoFocus={focus === field.name}
            onChange={(text) => onChange({ ...draft, [field.name]: text })}
            point={point}
          />
        ))}
      </dl>
      {lists.map((field) => (
        <ListEditor
          key={field.name}
          field={field}
          rows={(draft[field.name] as Record<string, string>[] | undefined) ?? []}
          errors={errors}
          currency={currency}
          onChange={(rows) => onChange({ ...draft, [field.name]: rows })}
          point={point}
        />
      ))}
    </>
  );
}
