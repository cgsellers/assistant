import type { components } from "@/api/schema";

type Me = components["schemas"]["MeOut"];

/** Who the mock API says is signed in: someone at a client, Acme. */
export const MOCK_USER: Me = {
  kind: "user",
  name: "bo@acme.test",
  role: "author",
  organisation_id: "org-acme",
  organisation_name: "Acme Ltd",
  is_staff: false,
  csrf_token: "mock-csrf",
  csrf_header: "x-csrf-token",
};

export const MOCK_PASSWORD = "correct horse battery";

/** The mock refuses a change without the session's CSRF token, as the API does. */
export function hasCsrf(request: Request): boolean {
  return request.headers.get("x-csrf-token") === MOCK_USER.csrf_token;
}
