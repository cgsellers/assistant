import type { ClientRole } from "@/api/contract";
import type { components } from "@/api/schema";

type Me = components["schemas"]["MeOut"];

/** Who the mock API says is signed in: an admin at a client, Acme. */
export const MOCK_USER: Me = {
  kind: "user",
  name: "bo@acme.test",
  role: "admin",
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

let role: ClientRole = "admin";

/** The signed-in person's role as the mock sees it: tests try the others. */
export const mockRole = () => role;
export function setMockRole(next: ClientRole) {
  role = next;
}

/** /auth/me as the mock answers it, with the role as it is now. */
export const me = (): Me => ({ ...MOCK_USER, role });

// Kept in sessionStorage, like a session cookie: a reload keeps you signed in,
// closing the tab does not. Tests have their own, reset after each one.
const SIGNED_IN = "ledgerline-mock-signed-in";
let signedIn = globalThis.sessionStorage?.getItem(SIGNED_IN) === "yes";

export const isSignedIn = () => signedIn;

/** Tests start signed out unless they say otherwise. */
export function setSignedIn(value: boolean) {
  signedIn = value;
  globalThis.sessionStorage?.setItem(SIGNED_IN, value ? "yes" : "no");
}
