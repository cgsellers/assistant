import type {
  DocumentRow,
  Field,
  FieldValue,
  ListField,
  ListRow,
  Location,
  ScalarField,
} from "@/api/contract";
import { amountFor, MOCK_DOCUMENTS, SUPPLIERS as ROSTER } from "./documents";

/**
 * What each mock document says: the sale behind it, as printed on the
 * original, and what was read from it. The files the mock serves are drawn
 * from the same sale (files.ts), so the page shows an original that matches
 * its fields -- in the supplier's own number and date formats, which is the
 * normalising the product exists to do.
 *
 * Every name, address and number here is invented.
 */

interface Supplier {
  address: string[];
  taxId: string;
  /** How the supplier prints numbers and dates. */
  locale: string;
  /** Tax rate in percent: VAT, IVA, or none. */
  taxRate: number;
  taxName: string;
  items: string[];
  /** The band across the top of their invoices. */
  colour: [number, number, number];
  payment?: string;
}

const SUPPLIERS: Record<string, Supplier> = {
  "Northwind Office Supply": {
    address: ["14 Canal Street", "Manchester M1 3HE"],
    taxId: "GB 284 1937 06",
    locale: "en-GB",
    taxRate: 20,
    taxName: "VAT",
    items: [
      "A4 copier paper, 5 reams",
      "Toner cartridge, black",
      "Desk organiser",
      "Whiteboard markers, 12 pack",
    ],
    colour: [0.13, 0.36, 0.62],
  },
  "Globe Logistics": {
    address: ["Waalhaven Oostzijde 41", "3087 BM Rotterdam"],
    taxId: "NL 8531.27.604.B01",
    locale: "nl-NL",
    taxRate: 21,
    taxName: "BTW",
    items: [
      "Pallet freight Rotterdam–London",
      "Fuel surcharge",
      "Customs clearance",
      "Storage, 7 days",
    ],
    colour: [0.85, 0.42, 0.11],
  },
  "Coop Italia": {
    address: ["Via Roma 12", "20121 Milano"],
    taxId: "P.IVA 01234567890",
    locale: "it-IT",
    taxRate: 10,
    taxName: "IVA",
    items: ["Pane integrale", "Latte intero 1L", "Mozzarella", "Pomodori", "Caffè macinato"],
    colour: [0.8, 0.1, 0.1],
    payment: "Carta di credito",
  },
  "Contoso Cloud": {
    address: ["500 Pine Street, Suite 1200", "Seattle, WA 98101"],
    taxId: "EIN 91-4827365",
    locale: "en-US",
    taxRate: 0,
    taxName: "Sales tax",
    items: ["Compute, standard tier", "Object storage", "Support plan", "Data transfer"],
    colour: [0.2, 0.2, 0.55],
  },
  "Fabrikam Furniture": {
    address: ["Unit 7, Riverside Park", "Leeds LS10 1RJ"],
    taxId: "GB 731 4402 18",
    locale: "en-GB",
    taxRate: 20,
    taxName: "VAT",
    items: ["Ergonomic chair", "Standing desk frame", "Delivery and assembly", "Monitor arm"],
    colour: [0.18, 0.45, 0.3],
  },
  "Tailspin Travel": {
    address: ["Brighton Station", "Queens Road, Brighton BN1 3XP"],
    taxId: "VAT GB 987 6543 21",
    locale: "en-GB",
    taxRate: 0,
    taxName: "VAT",
    items: ["Off-peak return", "Seat reservation", "Travel insurance"],
    colour: [0.1, 0.3, 0.5],
    payment: "Contactless",
  },
  "Wide World Importers": {
    address: ["Calle de Alcalá 95", "28009 Madrid"],
    taxId: "CIF B87654321",
    locale: "es-ES",
    taxRate: 21,
    taxName: "IVA",
    items: [
      "Ceramic mugs, case of 24",
      "Tea, assorted, 6 boxes",
      "Glass jars, case of 12",
      "Packing materials",
    ],
    colour: [0.45, 0.2, 0.5],
  },
  "Café Luna": {
    address: ["Carrer de Mallorca 210", "08008 Barcelona"],
    taxId: "NIF B12345678",
    locale: "es-ES",
    taxRate: 10,
    taxName: "IVA",
    items: ["Cappuccino", "Croissant", "Zumo de naranja", "Tostada con tomate"],
    colour: [0.3, 0.2, 0.15],
    payment: "Tarjeta",
  },
};

export const BILL_TO = ["Acme Ltd", "1 Market Street", "London EC2A 4NE"];

export interface Line {
  description: string;
  quantity: number;
  /** Cents. */
  unit: number;
  /** Cents, as printed. */
  amount: number;
}

/** The kinds of original the mock can serve, each with its own trouble. */
export type Variant =
  | "clean"
  /** Printed without a total: the transform works it out. */
  | "no_total_printed"
  /** Only page 1 of 2 arrived, or the receipt is torn: no total to read. */
  | "cut_off"
  /** A line printed with the wrong amount, so the lines do not add up. */
  | "lines_disagree"
  /** Due before it was issued. */
  | "dates_disagree"
  /** Not a document anyone set up: a delivery note. */
  | "delivery_note"
  /** Too blurred and dark to read. */
  | "unreadable";

export interface Sale {
  index: number;
  kind: "invoice" | "receipt";
  supplier: string;
  details: Supplier;
  number: string;
  issued: string;
  due: string;
  po: string;
  time: string;
  currency: string;
  lines: Line[];
  subtotal: number;
  tax: number;
  total: number;
  variant: Variant;
}

function variantFor(row: DocumentRow, index: number, kind: Sale["kind"]): Variant {
  if (row.outcome === "unsupported") return "delivery_note";
  if (row.outcome === "failed") return "unreadable";
  if (row.outcome === "needs_review") {
    if (index % 2 === 1) return "cut_off";
    return index % 3 === 0 && kind === "invoice" ? "dates_disagree" : "lines_disagree";
  }
  if (row.outcome === "done" && kind === "invoice" && index % 5 === 2) return "no_total_printed";
  return "clean";
}

function addDays(isoDate: string, days: number): string {
  const [y, m, d] = isoDate.split("-").map(Number) as [number, number, number];
  return new Date(Date.UTC(y, m - 1, d + days)).toISOString().slice(0, 10);
}

/** Split `cents` over `count` lines, unevenly, with whole-cent unit prices. */
function split(cents: number, items: string[], count: number, seed: number): Line[] {
  const weights = Array.from({ length: count }, (_, j) => ((seed + j * 5) % 7) + 2);
  const sum = weights.reduce((a, b) => a + b, 0);
  let left = cents;
  return weights.map((weight, j) => {
    const amount = j === count - 1 ? left : Math.floor((cents * weight) / sum);
    left -= amount;
    const quantity = [3, 2].find((q) => amount % q === 0 && amount / q >= 150) ?? 1;
    return {
      description: items[(seed + j) % items.length] as string,
      quantity,
      unit: amount / quantity,
      amount,
    };
  });
}

/** The sale behind mock document `row`. */
export function saleFor(row: DocumentRow): Sale {
  const index = Number(row.id.slice(4)) - 1;
  // Unread documents have no supplier on their row; the file still has one.
  const [supplier, kind, currency] = ROSTER[index % ROSTER.length] as [
    string,
    Sale["kind"],
    string,
  ];
  const details = SUPPLIERS[supplier] as Supplier;
  const variant = variantFor(row, index, kind);
  const total = Math.round(Number(row.total?.amount ?? amountFor(index)) * 100);
  const tax = Math.round(total - total / (1 + details.taxRate / 100));
  const subtotal = total - tax;
  // Receipt prices include tax, so their lines add up to the total.
  const lines = split(kind === "receipt" ? total : subtotal, details.items, (index % 3) + 2, index);
  if (variant === "lines_disagree") {
    const last = lines[lines.length - 1] as Line;
    last.amount += 1000;
    last.unit = last.amount / last.quantity;
  }
  const issued = row.document_date ?? addDays(row.received_at.slice(0, 10), -1);
  return {
    index,
    kind,
    supplier,
    details,
    number: kind === "invoice" ? `INV-${7700 + index}` : `${2400 + index * 13}`,
    issued,
    due: addDays(issued, variant === "dates_disagree" ? -14 : 30),
    po: `PO-${31000 + index * 7}`,
    time: `${String(8 + (index % 11)).padStart(2, "0")}:${String((index * 17) % 60).padStart(2, "0")}`,
    currency,
    lines,
    subtotal,
    tax,
    total,
    variant,
  };
}

// --- What was read --------------------------------------------------------------

const cents = (value: number) => (value / 100).toFixed(2);
const toCents = (value: FieldValue) => Math.round(Number(value) * 100);

/** Where each value was printed, by field key; see files.ts. */
type Where = Map<string, Location>;

function scalar(
  where: Where,
  name: string,
  label: string,
  type: ScalarField["type"],
  value: FieldValue,
  extra: Partial<ScalarField> = {},
): ScalarField {
  return {
    name,
    label,
    type,
    value,
    required: false,
    derived: false,
    problem: null,
    corrected: null,
    location: value === null ? null : (where.get(name) ?? null),
    ...extra,
  };
}

function list(
  where: Where,
  name: string,
  label: string,
  columns: ListField["columns"],
  rows: ListRow[],
): ListField {
  return {
    name,
    label,
    type: "list",
    required: false,
    derived: false,
    problem: null,
    corrected: null,
    columns,
    rows,
    row_locations: rows.map((_, i) => where.get(`${name}.${i}`) ?? null),
  };
}

function lineItems(where: Where, sale: Sale): ListField {
  const receipt = sale.kind === "receipt";
  return list(
    where,
    "line_items",
    "Line items",
    receipt
      ? [
          { name: "name", label: "Name", type: "string" },
          { name: "quantity", label: "Quantity", type: "number" },
          { name: "amount", label: "Amount", type: "money" },
        ]
      : [
          { name: "description", label: "Description", type: "string" },
          { name: "quantity", label: "Quantity", type: "number" },
          { name: "unit_price", label: "Unit price", type: "money" },
          { name: "amount", label: "Amount", type: "money" },
        ],
    sale.lines.map(
      (line): ListRow =>
        receipt
          ? { name: line.description, quantity: line.quantity, amount: cents(line.amount) }
          : {
              description: line.description,
              quantity: line.quantity,
              unit_price: cents(line.unit),
              amount: cents(line.amount),
            },
    ),
  );
}

/**
 * The fields a processor read from `sale`, as read: before the transform and
 * the rules (see `check`), and before anyone corrected them.
 */
export function readFrom(sale: Sale, where: Where): Field[] {
  const cutOff = sale.variant === "cut_off";
  const required = { required: true };

  if (sale.kind === "receipt") {
    return [
      scalar(where, "vendor", "Vendor", "string", sale.supplier, required),
      scalar(where, "tax_id", "Tax ID", "string", sale.details.taxId),
      scalar(where, "issued_on", "Issued on", "date", sale.issued, required),
      scalar(where, "subtotal", "Subtotal", "money", cutOff ? null : cents(sale.subtotal)),
      scalar(where, "tax_amount", "Tax amount", "money", cutOff ? null : cents(sale.tax)),
      scalar(where, "total", "Total", "money", cutOff ? null : cents(sale.total), required),
      scalar(where, "currency", "Currency", "string", sale.currency),
      scalar(
        where,
        "payment_method",
        "Payment method",
        "string",
        cutOff ? null : (sale.details.payment ?? null),
      ),
      lineItems(where, sale),
    ];
  }

  return [
    scalar(where, "invoice_number", "Invoice number", "string", sale.number, required),
    scalar(where, "issued_on", "Issued on", "date", sale.issued, required),
    scalar(where, "due_date", "Due date", "date", sale.due),
    scalar(where, "po_number", "PO number", "string", sale.po),
    scalar(where, "vendor", "Vendor", "string", sale.supplier, required),
    scalar(where, "vendor_tax_id", "Vendor tax ID", "string", sale.details.taxId),
    scalar(where, "bill_to", "Bill to", "string", BILL_TO.join(", ")),
    scalar(where, "currency", "Currency", "string", sale.currency),
    scalar(where, "subtotal", "Subtotal", "money", cutOff ? null : cents(sale.subtotal)),
    scalar(where, "discount", "Discount", "money", null),
    scalar(where, "tax_amount", "Tax amount", "money", cutOff ? null : cents(sale.tax)),
    list(
      where,
      "taxes",
      "Taxes",
      [
        { name: "name", label: "Name", type: "string" },
        { name: "rate", label: "Rate", type: "string" },
        { name: "amount", label: "Amount", type: "money" },
      ],
      cutOff
        ? []
        : [
            {
              name: sale.details.taxName,
              rate: `${sale.details.taxRate}%`,
              amount: cents(sale.tax),
            },
          ],
    ),
    // The invoice processor's transform fills a total the page did not print.
    scalar(
      where,
      "total",
      "Total",
      "money",
      sale.variant === "cut_off" || sale.variant === "no_total_printed" ? null : cents(sale.total),
      required,
    ),
    lineItems(where, sale),
  ];
}

/**
 * What the backend does after every read and every correction: the
 * processor's transform, then its rules. Stands in for doctypes/transform.py
 * and doctypes/validate.py, with the invoice and receipt processors' rules.
 */
export function check(kind: Sale["kind"], read: Field[]): { fields: Field[]; problems: string[] } {
  const fields = read.map((field) => ({ ...field, problem: null }) as Field);
  const byName = (name: string) => fields.find((f) => f.name === name);
  const value = (name: string) => {
    const field = byName(name);
    return field && field.type !== "list" ? field.value : null;
  };
  const problems: string[] = [];

  // The transform: an invoice with no printed total gets subtotal - discount + tax.
  const total = byName("total");
  if (kind === "invoice" && total && total.type !== "list") {
    const worked = total.derived && !total.corrected;
    if (total.value === null || worked) {
      const subtotal = value("subtotal");
      const tax = value("tax_amount");
      const figured = subtotal !== null && tax !== null;
      total.value = figured
        ? cents(toCents(subtotal) - toCents(value("discount")) + toCents(tax))
        : null;
      total.derived = figured;
      if (figured) total.location = null;
    }
  }

  for (const field of fields) {
    if (field.required && field.type !== "list" && (field.value === null || field.value === "")) {
      field.problem =
        field.name === "total"
          ? "No total could be read from the document."
          : "Required, but not found on the document.";
    }
  }

  const due = value("due_date");
  const issued = value("issued_on");
  const dueField = byName("due_date");
  if (dueField && due && issued && String(due) < String(issued)) {
    dueField.problem = "The due date is before the invoice date.";
  }

  const items = byName("line_items");
  const against = kind === "invoice" ? "subtotal" : "total";
  const expected = value(against);
  if (items?.type === "list" && items.rows.length && expected !== null) {
    const sum = items.rows.reduce((a, row) => a + toCents(row.amount ?? 0), 0);
    if (sum !== toCents(expected)) {
      problems.push(`The line items add up to ${cents(sum)}, but the ${against} says ${expected}.`);
    }
  }
  return { fields, problems };
}

/**
 * A processor's fields with nothing read: what the mock shows for an upload,
 * which it cannot really read. Required fields then flag themselves.
 */
export function blankFields(kind: Sale["kind"]): Field[] {
  const example = MOCK_DOCUMENTS[kind === "invoice" ? 1 : 2] as DocumentRow;
  return readFrom(saleFor(example), new Map()).map(
    (field): Field =>
      field.type === "list"
        ? { ...field, rows: [], row_locations: [] }
        : { ...field, value: null, location: null },
  );
}
