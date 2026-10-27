import { ApiError, csrfHeaders } from "@/lib/api";

/**
 * For the endpoints in src/api/contract.ts, which the generated client does
 * not know yet. Same origin and same cookie as the typed client; once an
 * endpoint is in the API's schema, its callers move to `api` and this shrinks.
 */
export function apiUrl(path: string, params: Record<string, string | number | undefined> = {}) {
  const url = new URL(`/api${path}`, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== "") url.searchParams.set(key, String(value));
  }
  return url;
}

/** The API's explanation of a failure, as an ApiError. */
async function failure(response: Response): Promise<ApiError> {
  let detail = response.statusText || "Something went wrong";
  let fields: Record<string, string> = {};
  try {
    const body = (await response.json()) as { detail?: unknown; fields?: unknown };
    if (typeof body.detail === "string") detail = body.detail;
    if (body.fields && typeof body.fields === "object")
      fields = body.fields as Record<string, string>;
  } catch {
    // Not JSON: the status text will do.
  }
  return new ApiError(response.status, detail, fields);
}

export async function getJson<T>(
  path: string,
  params: Record<string, string | number | undefined> = {},
): Promise<T> {
  const response = await globalThis.fetch(apiUrl(path, params), { credentials: "same-origin" });
  if (!response.ok) throw await failure(response);
  return (await response.json()) as T;
}

/**
 * A change, with the session's CSRF token: the answer as JSON, or undefined
 * for one with no body (204).
 */
export async function sendJson<T>(
  method: "POST" | "PUT" | "PATCH" | "DELETE",
  path: string,
  body?: unknown,
): Promise<T> {
  const response = await globalThis.fetch(apiUrl(path), {
    method,
    credentials: "same-origin",
    headers: {
      ...(body === undefined ? {} : { "Content-Type": "application/json" }),
      ...csrfHeaders(),
    },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  if (!response.ok) throw await failure(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}
