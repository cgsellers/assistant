import { BILL_TO, type Sale } from "./sales";

/**
 * The originals the mock API serves: each invoice as a real PDF, each receipt
 * as a photo, drawn from the same sale its fields were read from. Printed the
 * way the supplier prints -- "€ 1.234,56", "10/21/2026" -- so the page shows
 * what normalising does.
 */

// --- Printing in the supplier's own style ----------------------------------------

function money(sale: Sale, cents: number): string {
  return new Intl.NumberFormat(sale.details.locale, { style: "currency", currency: sale.currency })
    .format(cents / 100)
    .replace(/[  ]/g, " ");
}

function number(sale: Sale, cents: number): string {
  return new Intl.NumberFormat(sale.details.locale, { minimumFractionDigits: 2 }).format(
    cents / 100,
  );
}

function date(sale: Sale, isoDate: string): string {
  const [y, m, d] = isoDate.split("-").map(Number) as [number, number, number];
  const style =
    sale.details.locale === "en-GB" ? { day: "numeric", month: "short", year: "numeric" } : {};
  return new Intl.DateTimeFormat(sale.details.locale, {
    ...(style as object),
    timeZone: "UTC",
  }).format(new Date(Date.UTC(y, m - 1, d)));
}

// --- A PDF, by hand ---------------------------------------------------------------
// One A4 page, the built-in Helvetica (nothing to embed), text, rules and fills.

/** Helvetica's advance widths for ASCII 32-126, in thousandths of the font size. */
const WIDTHS = [
  278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556,
  556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667,
  611, 778, 722, 278, 500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667,
  667, 611, 278, 278, 278, 469, 556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500,
  222, 833, 556, 556, 556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584,
];

function width(text: string, size: number, bold: boolean): number {
  let total = 0;
  for (const char of text) total += WIDTHS[(char.codePointAt(0) ?? 0) - 32] ?? 556;
  return (total * size * (bold ? 1.06 : 1)) / 1000;
}

/** WinAnsi codes for the characters outside ASCII that suppliers print. */
const WIN_ANSI: Record<string, number> = { "€": 0x80, "–": 0x96, "—": 0x97, "’": 0x92, "•": 0x95 };

function pdfString(text: string): string {
  let out = "";
  for (const char of text) {
    const code = char.codePointAt(0) ?? 63;
    if (char === "(" || char === ")" || char === "\\") out += `\\${char}`;
    else if (code >= 32 && code < 127) out += char;
    else {
      const ansi = WIN_ANSI[char] ?? (code >= 160 && code < 256 ? code : 63);
      out += `\\${ansi.toString(8).padStart(3, "0")}`;
    }
  }
  return `(${out})`;
}

type Rgb = [number, number, number];
const INK: Rgb = [0.13, 0.13, 0.15];
const MUTED: Rgb = [0.45, 0.45, 0.5];

class Page {
  private ops: string[] = [];

  text(
    x: number,
    y: number,
    text: string,
    { size = 10, bold = false, colour = INK, align = "left" } = {},
  ) {
    const left = align === "right" ? x - width(text, size, bold) : x;
    const [r, g, b] = colour;
    this.ops.push(
      `BT /${bold ? "F2" : "F1"} ${size} Tf ${r} ${g} ${b} rg ${left.toFixed(2)} ${y} Td ${pdfString(text)} Tj ET`,
    );
  }

  fill(x: number, y: number, w: number, h: number, [r, g, b]: Rgb) {
    this.ops.push(`${r} ${g} ${b} rg ${x} ${y} ${w} ${h} re f`);
  }

  rule(x1: number, y1: number, x2: number, y2: number, grey = 0.85) {
    this.ops.push(`${grey} G 0.6 w ${x1} ${y1} m ${x2} ${y2} l S`);
  }

  toPdf(): Uint8Array {
    const content = this.ops.join("\n");
    const objects = [
      "<< /Type /Catalog /Pages 2 0 R >>",
      "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
      "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Resources << /Font << /F1 4 0 R /F2 5 0 R >> >> /Contents 6 0 R >>",
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
      "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding >>",
      `<< /Length ${content.length} >>\nstream\n${content}\nendstream`,
    ];
    // Everything above is ASCII (the strings are escaped), so characters are bytes.
    let pdf = "%PDF-1.4\n";
    const offsets = objects.map((body, i) => {
      const offset = pdf.length;
      pdf += `${i + 1} 0 obj\n${body}\nendobj\n`;
      return offset;
    });
    const xref = pdf.length;
    pdf += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
    for (const offset of offsets) pdf += `${String(offset).padStart(10, "0")} 00000 n \n`;
    pdf += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
    return new TextEncoder().encode(pdf);
  }
}

export function invoicePdf(sale: Sale): Uint8Array {
  const page = new Page();
  const { details } = sale;
  const L = 50;
  const R = 545;

  page.fill(0, 822, 595, 20, details.colour);
  page.text(L, 772, sale.supplier, { size: 18, bold: true });
  details.address.forEach((line, i) => {
    page.text(L, 754 - i * 13, line, { size: 9, colour: MUTED });
  });
  page.text(L, 728, details.taxId, { size: 9, colour: MUTED });

  const delivery = sale.variant === "delivery_note";
  page.text(R, 772, delivery ? "DELIVERY NOTE" : "INVOICE", {
    size: 20,
    bold: true,
    align: "right",
  });
  const meta: [string, string][] = delivery
    ? [
        ["Delivery no.", `DN-${50200 + sale.index}`],
        ["Shipped", date(sale, sale.issued)],
      ]
    : [
        ["Invoice no.", sale.number],
        ["Date", date(sale, sale.issued)],
        ["Due date", date(sale, sale.due)],
        ["PO number", sale.po],
      ];
  meta.forEach(([label, value], i) => {
    page.text(390, 748 - i * 14, label, { size: 9, colour: MUTED });
    page.text(R, 748 - i * 14, value, { size: 9, align: "right" });
  });

  page.text(L, 690, delivery ? "DELIVER TO" : "BILL TO", { size: 8, bold: true, colour: MUTED });
  BILL_TO.forEach((line, i) => {
    page.text(L, 675 - i * 14, line, { size: 10, bold: i === 0 });
  });

  let y = 600;
  page.fill(L, y - 7, R - L, 22, [0.95, 0.95, 0.96]);
  page.text(L + 8, y, "Description", { size: 9, bold: true });
  page.text(370, y, "Qty", { size: 9, bold: true, align: "right" });
  if (!delivery) {
    page.text(455, y, "Unit price", { size: 9, bold: true, align: "right" });
    page.text(R - 8, y, "Amount", { size: 9, bold: true, align: "right" });
  }
  for (const line of sale.lines) {
    y -= 24;
    page.text(L + 8, y, line.description);
    page.text(370, y, String(line.quantity), { align: "right" });
    if (!delivery) {
      page.text(455, y, number(sale, line.unit), { align: "right" });
      page.text(R - 8, y, number(sale, line.amount), { align: "right" });
    }
    page.rule(L, y - 9, R, y - 9);
  }

  y -= 40;
  if (delivery) {
    page.text(L, y, "Goods received in good condition:  ____________________", { size: 10 });
  } else if (sale.variant === "cut_off") {
    page.text(R, y, "Continued on page 2", { size: 9, colour: MUTED, align: "right" });
  } else {
    const totals: [string, number, boolean][] = [
      ["Subtotal", sale.subtotal, false],
      [`${details.taxName} ${details.taxRate}%`, sale.tax, false],
    ];
    if (sale.variant !== "no_total_printed") totals.push(["Total due", sale.total, true]);
    for (const [label, cents, bold] of totals) {
      if (bold) page.rule(370, y + 14, R, y + 14, 0.3);
      page.text(370, y, label, { size: bold ? 12 : 10, bold });
      page.text(R - 8, y, money(sale, cents), { size: bold ? 12 : 10, bold, align: "right" });
      y -= 20;
    }
  }

  page.rule(L, 96, R, 96);
  page.text(
    L,
    80,
    delivery ? "This is not an invoice." : "Payment within 30 days. Thank you for your business.",
    {
      size: 9,
      colour: MUTED,
    },
  );
  page.text(R, 80, sale.variant === "cut_off" ? "Page 1 of 2" : "Page 1 of 1", {
    size: 9,
    colour: MUTED,
    align: "right",
  });
  return page.toPdf();
}

// --- A receipt, photographed --------------------------------------------------------

/** A 1x1 PNG: where there is no canvas to draw on (the tests), any image will do. */
const BLANK_PNG = Uint8Array.from(
  atob(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/x8AAwMCAO+ip1sAAAAASUVORK5CYII=",
  ),
  (c) => c.charCodeAt(0),
);

export async function receiptPhoto(sale: Sale): Promise<{ bytes: Uint8Array; type: string }> {
  if (typeof OffscreenCanvas === "undefined") return { bytes: BLANK_PNG, type: "image/png" };

  const paperWidth = 380;
  const lineHeight = 22;
  const torn = sale.variant === "cut_off";
  const rows = 9 + sale.lines.length + (torn ? 0 : 7);
  const paperHeight = rows * lineHeight + 60;
  const canvas = new OffscreenCanvas(560, paperHeight + 140);
  const ctx = canvas.getContext("2d");
  if (!ctx) return { bytes: BLANK_PNG, type: "image/png" };

  // The table it was photographed on.
  const table = ctx.createLinearGradient(0, 0, canvas.width, canvas.height);
  table.addColorStop(0, "#4a4d55");
  table.addColorStop(1, "#2b2d33");
  ctx.fillStyle = table;
  ctx.fillRect(0, 0, canvas.width, canvas.height);

  ctx.save();
  if (sale.variant === "unreadable") ctx.filter = "blur(5px) brightness(0.55)";
  ctx.translate(canvas.width / 2, canvas.height / 2);
  ctx.rotate(((sale.index % 2 ? 1 : -1) * 1.4 * Math.PI) / 180);
  ctx.translate(-paperWidth / 2, -paperHeight / 2);

  ctx.shadowColor = "rgba(0,0,0,0.45)";
  ctx.shadowBlur = 24;
  ctx.shadowOffsetY = 8;
  ctx.fillStyle = "#fbfaf6";
  ctx.beginPath();
  ctx.moveTo(0, 0);
  ctx.lineTo(paperWidth, 0);
  if (torn) {
    // Torn off below the last line: a ragged edge where the totals were.
    for (let x = paperWidth; x >= 0; x -= 14) ctx.lineTo(x, paperHeight - (x % 28 ? 10 : 0));
  } else {
    ctx.lineTo(paperWidth, paperHeight);
    ctx.lineTo(0, paperHeight);
  }
  ctx.closePath();
  ctx.fill();
  ctx.shadowColor = "transparent";

  const left = 24;
  const right = paperWidth - 24;
  let y = 44;
  ctx.fillStyle = "#26262b";
  const write = (
    text: string,
    x: number,
    align: CanvasTextAlign = "left",
    size = 15,
    bold = false,
  ) => {
    ctx.font = `${bold ? "bold " : ""}${size}px "Courier New", Courier, monospace`;
    ctx.textAlign = align;
    ctx.fillText(text, x, y);
  };
  const next = (lines = 1) => {
    y += lineHeight * lines;
  };
  const dashes = () => {
    write("-".repeat(36), paperWidth / 2, "center");
    next();
  };

  write(sale.supplier.toUpperCase(), paperWidth / 2, "center", 20, true);
  next(1.3);
  for (const line of sale.details.address) {
    write(line, paperWidth / 2, "center", 13);
    next(0.85);
  }
  write(sale.details.taxId, paperWidth / 2, "center", 13);
  next(1.4);
  write(`${date(sale, sale.issued)}  ${sale.time}`, left);
  write(`No. ${sale.number}`, right, "right");
  next();
  dashes();
  for (const line of sale.lines) {
    const label = line.quantity > 1 ? `${line.quantity} x ${line.description}` : line.description;
    write(label.slice(0, 28), left);
    write(number(sale, line.amount), right, "right");
    next();
  }
  if (!torn) {
    dashes();
    write(`TOTAL ${sale.currency}`, left, "left", 18, true);
    write(number(sale, sale.total), right, "right", 18, true);
    next(1.3);
    write(`${sale.details.taxName} ${sale.details.taxRate}% incl.`, left, "left", 13);
    write(number(sale, sale.tax), right, "right", 13);
    next();
    write(sale.details.payment ?? "", left, "left", 13);
    next(1.6);
    write(
      sale.details.locale === "es-ES"
        ? "GRACIAS POR SU COMPRA"
        : sale.details.locale === "it-IT"
          ? "GRAZIE E ARRIVEDERCI"
          : "THANK YOU",
      paperWidth / 2,
      "center",
      13,
    );
  }
  ctx.restore();

  const blob = await canvas.convertToBlob({ type: "image/jpeg", quality: 0.86 });
  return { bytes: new Uint8Array(await blob.arrayBuffer()), type: "image/jpeg" };
}
