import { ApiError } from "@/lib/api";

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

export async function getJson<T>(
  path: string,
  params: Record<string, string | number | undefined> = {},
): Promise<T> {
  const response = await globalThis.fetch(apiUrl(path, params), { credentials: "same-origin" });
  if (!response.ok) {
    let detail = response.statusText || "Something went wrong";
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") detail = body.detail;
    } catch {
      // Not JSON: the status text will do.
    }
    throw new ApiError(response.status, detail);
  }
  return (await response.json()) as T;
}
