import createClient, { type Middleware } from "openapi-fetch";
import type { paths } from "@/api/schema";

/**
 * The typed API client. Paths, parameters and responses come from the API's
 * own OpenAPI schema (`npm run api`), so a renamed field is a type error here
 * rather than a blank cell in production.
 *
 * Same origin, under /api: the session cookie goes with every request on
 * its own, and nothing about it is visible to scripts (it is HttpOnly).
 */
// Absolute, from the page's own origin: browsers resolve a relative base URL,
// but the Request implementation the tests run on does not.
//
// fetch is looked up on every call rather than captured once: the client is
// created at import, before anything that wraps fetch -- the mock server in
// tests, a tracing library in production -- has had the chance to.
export const api = createClient<paths>({
  baseUrl: new URL("/api", window.location.origin).toString(),
  credentials: "same-origin",
  fetch: (request) => globalThis.fetch(request),
});

let csrfToken: string | null = null;

/** Remembered from sign-in or /auth/me; sent back on anything that changes state. */
export function setCsrfToken(token: string | null) {
  csrfToken = token;
}

const SAFE = new Set(["GET", "HEAD", "OPTIONS"]);

const csrf: Middleware = {
  onRequest({ request }) {
    if (!SAFE.has(request.method) && csrfToken) {
      request.headers.set("X-CSRF-Token", csrfToken);
    }
    return request;
  },
};

api.use(csrf);

/** An API answer that was not a success, with what the API said about it. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: string;

  constructor(status: number, detail: string) {
    super(detail);
    this.status = status;
    this.detail = detail;
  }
}

/** The data, or an ApiError carrying the API's own explanation. */
export function unwrap<T>(result: { data?: T; error?: unknown; response: Response }): T {
  if (result.data !== undefined && result.response.ok) {
    return result.data;
  }
  const error = result.error as { detail?: unknown } | undefined;
  const detail = typeof error?.detail === "string" ? error.detail : result.response.statusText;
  throw new ApiError(result.response.status, detail || "Something went wrong");
}
