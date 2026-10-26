import type { DocumentRow, Field, FieldValue, ListField, ScalarField } from "@/api/contract";
import { amountFor, SUPPLIERS as ROSTER } from "./documents";

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

function scalar(
  name: string,
  label: string,
  type: ScalarField["type"],
  value: FieldValue,
  extra: Partial<ScalarField> = {},
): ScalarField {
  return { name, label, type, value, derived: false, problem: null, ...extra };
}

function lineItems(sale: Sale, receipt: boolean): ListField {
  return {
    name: "line_items",
    label: "Line items",
    type: "list",
    derived: false,
    problem: null,
    columns: receipt
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
    rows: sale.lines.map(
      (line): Record<string, FieldValue> =>
        receipt
          ? { name: line.description, quantity: line.quantity, amount: cents(line.amount) }
          : {
              description: line.description,
              quantity: line.quantity,
              unit_price: cents(line.unit),
              amount: cents(line.amount),
            },
    ),
  };
}

/** The fields a processor read from `sale`, and what holds it in review. */
export function readFrom(sale: Sale): { fields: Field[]; problems: string[] } {
  const cutOff = sale.variant === "cut_off";
  const totalProblem = cutOff ? "No total could be read: the document ends before it." : null;
  const problems =
    sale.variant === "lines_disagree"
      ? [
          `The line items add up to ${cents(sale.lines.reduce((a, l) => a + l.amount, 0))}, but the ${
            sale.kind === "invoice" ? "subtotal" : "total"
          } says ${cents(sale.kind === "invoice" ? sale.subtotal : sale.total)}.`,
        ]
      : [];

  if (sale.kind === "receipt") {
    return {
      problems,
      fields: [
        scalar("vendor", "Vendor", "string", sale.supplier),
        scalar("tax_id", "Tax ID", "string", sale.details.taxId),
        scalar("issued_on", "Issued on", "date", sale.issued),
        scalar("subtotal", "Subtotal", "money", cutOff ? null : cents(sale.subtotal)),
        scalar("tax_amount", "Tax amount", "money", cutOff ? null : cents(sale.tax)),
        scalar("total", "Total", "money", cutOff ? null : cents(sale.total), {
          problem: totalProblem,
        }),
        scalar("currency", "Currency", "string", sale.currency),
        scalar(
          "payment_method",
          "Payment method",
          "string",
          cutOff ? null : (sale.details.payment ?? null),
        ),
        lineItems(sale, true),
      ],
    };
  }

  const taxes: ListField = {
    name: "taxes",
    label: "Taxes",
    type: "list",
    derived: false,
    problem: null,
    columns: [
      { name: "name", label: "Name", type: "string" },
      { name: "rate", label: "Rate", type: "string" },
      { name: "amount", label: "Amount", type: "money" },
    ],
    rows: cutOff
      ? []
      : [{ name: sale.details.taxName, rate: `${sale.details.taxRate}%`, amount: cents(sale.tax) }],
  };
  return {
    problems,
    fields: [
      scalar("invoice_number", "Invoice number", "string", sale.number),
      scalar("issued_on", "Issued on", "date", sale.issued),
      scalar("due_date", "Due date", "date", sale.due, {
        problem:
          sale.variant === "dates_disagree" ? "The due date is before the invoice date." : null,
      }),
      scalar("po_number", "PO number", "string", sale.po),
      scalar("vendor", "Vendor", "string", sale.supplier),
      scalar("vendor_tax_id", "Vendor tax ID", "string", sale.details.taxId),
      scalar("bill_to", "Bill to", "string", BILL_TO.join(", ")),
      scalar("currency", "Currency", "string", sale.currency),
      scalar("subtotal", "Subtotal", "money", cutOff ? null : cents(sale.subtotal)),
      scalar("discount", "Discount", "money", null),
      scalar("tax_amount", "Tax amount", "money", cutOff ? null : cents(sale.tax)),
      taxes,
      scalar("total", "Total", "money", cutOff ? null : cents(sale.total), {
        derived: sale.variant === "no_total_printed",
        problem: totalProblem,
      }),
      lineItems(sale, false),
    ],
  };
}
