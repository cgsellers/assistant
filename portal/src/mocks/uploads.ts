import { HttpResponse, http } from "msw";
import type { components } from "@/api/schema";
import { hasCsrf } from "./user";

type DocumentOut = components["schemas"]["DocumentOut"];

/**
 * POST /documents as the API answers it (api/routes/documents.py): refuses
 * an empty, oversized or unrecognised file with the API's own words, tells
 * the type from the bytes and not the name, and returns the original for a
 * file uploaded before (same SHA-256), marked `duplicate`.
 *
 * The mock cannot read a file, so an upload stays "processing" for a few
 * seconds and then waits in review with nothing filled in -- see document.ts.
 */

export interface Uploaded {
  out: DocumentOut;
  bytes: Uint8Array;
}

/** Newest first. Tests start from none (src/test/setup.ts). */
const uploaded: Uploaded[] = [];

export const uploads = () => uploaded;
export const uploadFor = (id: string) => uploaded.find((u) => u.out.id === id);

export function resetUploads() {
  uploaded.length = 0;
  readDelay = 4000;
}

/** How long an upload is "being read" before it is ready for review. */
let readDelay = 4000;
export const READ_DELAY = () => readDelay;
export function setReadDelay(ms: number) {
  readDelay = ms;
}

/** As core/config.py. */
export const MAX_BYTES = 20 * 1024 * 1024;

/** The type the bytes say, as storage.sniff_mime does; null for anything else. */
function sniff(bytes: Uint8Array): string | null {
  const ascii = (from: number, to: number) => String.fromCharCode(...bytes.subarray(from, to));
  if (ascii(0, 5) === "%PDF-") return "application/pdf";
  if (bytes[0] === 0xff && bytes[1] === 0xd8 && bytes[2] === 0xff) return "image/jpeg";
  if (ascii(1, 4) === "PNG" && bytes[0] === 0x89) return "image/png";
  if (ascii(0, 4) === "RIFF" && ascii(8, 12) === "WEBP") return "image/webp";
  if (ascii(0, 4) === "II*\0" || ascii(0, 4) === "MM\0*") return "image/tiff";
  if (ascii(4, 8) === "ftyp" && ["heic", "heix", "mif1"].includes(ascii(8, 12)))
    return "image/heic";
  return null;
}

async function sha256(bytes: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", bytes as BufferSource);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const refuse = (status: number, detail: string) => HttpResponse.json({ detail }, { status });

function indexOf(haystack: Uint8Array, needle: Uint8Array, from = 0): number {
  outer: for (let i = from; i <= haystack.length - needle.length; i++) {
    for (let j = 0; j < needle.length; j++) if (haystack[i + j] !== needle[j]) continue outer;
    return i;
  }
  return -1;
}

/**
 * The part named "file" of a multipart body. Read by hand rather than with
 * `request.formData()`, whose parser in Node insists on Node's own File and
 * throws under the tests' jsdom, which replaces it.
 */
async function fileFrom(
  request: Request,
): Promise<{ name: string | null; bytes: Uint8Array } | null> {
  const boundary = /boundary="?([^";]+)"?/.exec(request.headers.get("content-type") ?? "")?.[1];
  if (!boundary) return null;
  const body = new Uint8Array(await request.arrayBuffer());
  const encode = (text: string) => new TextEncoder().encode(text);
  const marker = encode(`--${boundary}`);
  const blank = encode("\r\n\r\n");
  let at = indexOf(body, marker);
  while (at !== -1) {
    const headersEnd = indexOf(body, blank, at);
    const next = indexOf(body, marker, at + marker.length);
    if (headersEnd === -1 || next === -1) return null;
    const headers = new TextDecoder().decode(body.subarray(at + marker.length, headersEnd));
    if (/name="file"/.test(headers)) {
      // The content ends with the line break before the next boundary.
      return {
        name: /filename="([^"]*)"/.exec(headers)?.[1] ?? null,
        bytes: body.slice(headersEnd + 4, next - 2),
      };
    }
    at = next;
  }
  return null;
}

export const uploadHandlers = [
  http.post("/api/documents", async ({ request }) => {
    if (!hasCsrf(request)) return refuse(403, "missing or wrong CSRF token");
    const file = await fileFrom(request);
    if (!file) return refuse(422, "No file in the upload");
    const { bytes } = file;
    if (bytes.length === 0) return refuse(400, "File is empty");
    if (bytes.length > MAX_BYTES) {
      return refuse(413, `File is ${(bytes.length / 1024 / 1024).toFixed(1)} MB, limit is 20 MB`);
    }
    const mime = sniff(bytes);
    if (!mime) return refuse(415, "Unrecognised file type. Expected an image or PDF.");

    const hash = await sha256(bytes);
    const earlier = uploaded.find((u) => u.out.sha256 === hash);
    if (earlier) return HttpResponse.json({ ...earlier.out, duplicate: true });

    const out: DocumentOut = {
      id: `up-${String(uploaded.length + 1).padStart(3, "0")}`,
      sha256: hash,
      mime_type: mime,
      original_filename: file.name,
      size_bytes: bytes.length,
      status: "uploaded",
      uploaded_at: new Date().toISOString(),
      duplicate: false,
    };
    uploaded.unshift({ out, bytes });
    return HttpResponse.json(out);
  }),
];
