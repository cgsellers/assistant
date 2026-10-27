import type { components } from "@/api/schema";
import { csrfHeaders } from "@/lib/api";
import { apiUrl } from "@/lib/http";

export type DocumentOut = components["schemas"]["DocumentOut"];

/**
 * Why an upload did not go through. `retry` says whether trying again could
 * help: a dropped connection may; a file of the wrong type never will.
 */
export class UploadError extends Error {
  readonly retry: boolean;

  constructor(message: string, retry: boolean) {
    super(message);
    this.retry = retry;
  }
}

function explain(status: number, detail: string | null): UploadError {
  if (status === 0) return new UploadError("The connection dropped before the file arrived.", true);
  if (status === 401)
    return new UploadError("Your session has ended. Sign in again, then retry.", true);
  if (status === 403)
    return new UploadError("The upload was refused. Reload the page, then retry.", true);
  if (status === 400 || status === 413 || status === 415 || status === 422) {
    return new UploadError(detail ?? "The file was refused.", false);
  }
  return new UploadError(`Ledgerline had a problem (${status}). Retry in a moment.`, true);
}

/**
 * POST /documents, reporting progress as the bytes go. XMLHttpRequest rather
 * than fetch, which cannot report upload progress; same origin, same session
 * cookie, and the CSRF header like any other change.
 */
export function send(file: File, onProgress: (fraction: number) => void) {
  const xhr = new XMLHttpRequest();
  const done = new Promise<DocumentOut>((resolve, reject) => {
    xhr.open("POST", apiUrl("/documents"));
    for (const [name, value] of Object.entries(csrfHeaders())) xhr.setRequestHeader(name, value);
    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      let body: unknown = null;
      try {
        body = JSON.parse(xhr.responseText);
      } catch {
        // Not JSON: the status will have to do.
      }
      if (xhr.status >= 200 && xhr.status < 300 && body) return resolve(body as DocumentOut);
      const detail = (body as { detail?: unknown } | null)?.detail;
      reject(explain(xhr.status, typeof detail === "string" ? detail : null));
    };
    xhr.onerror = () => reject(explain(0, null));
    xhr.onabort = () => reject(new DOMException("Upload cancelled", "AbortError"));
    const form = new FormData();
    form.append("file", file, file.name);
    xhr.send(form);
  });
  return { done, cancel: () => xhr.abort() };
}
