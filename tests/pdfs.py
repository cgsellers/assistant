"""
Born-digital PDFs for tests, written by hand.

Small enough to read: a catalog, a page tree, one built-in font, and a content
stream per page placing text at exact coordinates. Exact coordinates are the
point -- tests can then assert where text lands, which a PDF from a library
with its own layout engine would not allow.

PDF coordinates start at the bottom-left of the page, in points.
"""

from __future__ import annotations


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")


def make_pdf(
    pages: list[list[tuple[float, float, str]]],
    *,
    width: float = 595,
    height: float = 842,
    size: float = 11,
) -> bytes:
    """
    One entry per page; each a list of (x, y, text) with y from the bottom.
    An empty page list gives a page with no text layer -- a stand-in for a
    scan.
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    catalog = add(b"")  # filled in once the page tree number is known
    tree = add(b"")
    font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    page_ids = []
    for fragments in pages:
        ops = [f"BT /F1 {size} Tf {x} {y} Td ({_escape(t)}) Tj ET" for x, y, t in fragments]
        stream = "\n".join(ops).encode("latin-1")
        content = add(b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream")
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {tree} 0 R /MediaBox [0 0 {width} {height}] "
                    f"/Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>"
                ).encode()
            )
        )

    objects[catalog - 1] = f"<< /Type /Catalog /Pages {tree} 0 R >>".encode()
    kids = " ".join(f"{p} 0 R" for p in page_ids)
    objects[tree - 1] = f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()

    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\n"
        f"startxref\n{xref}\n%%EOF\n"
    ).encode()
    return bytes(out)


#: A two-column invoice: descriptions left, amounts right, the layout that
#: flattened OCR text scrambled on real receipts.
INVOICE = [
    [
        (50, 790, "ACME SUPPLIES LTD"),
        (50, 774, "VAT No GB123456789"),
        (380, 790, "INVOICE INV-2041"),
        (380, 774, "Date: 14 March 2026"),
        (50, 700, "Description"),
        (450, 700, "Amount"),
        (50, 680, "Printer paper A4 x5"),
        (450, 680, "24.50"),
        (50, 664, "Toner cartridge"),
        (450, 664, "89.99"),
        (50, 648, "Delivery"),
        (450, 648, "5.00"),
        (50, 610, "Subtotal"),
        (450, 610, "119.49"),
        (50, 594, "VAT 20%"),
        (450, 594, "23.90"),
        (50, 578, "Total due"),
        (450, 578, "143.39"),
    ]
]
