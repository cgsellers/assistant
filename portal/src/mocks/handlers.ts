import { HttpResponse, http } from "msw";
import { singleDocumentHandlers } from "./document";
import { listingHandlers } from "./listing";
import { settingsHandlers } from "./settings";
import { uploadHandlers } from "./uploads";
import { isSignedIn, MOCK_PASSWORD, MOCK_USER, me, setSignedIn } from "./user";

/**
 * The API as the portal expects it, for running without a backend and for
 * tests. Shapes come from the generated schema, so a mock cannot drift from
 * the real response. Endpoints the API does not have yet are mocked here
 * first: each handler is the contract the backend then has to meet.
 */
export { MOCK_PASSWORD, MOCK_USER, setSignedIn };

export const handlers = [
  // Only the mock answers this: the sign-in page asks it to show whether the
  // mock API is really answering in this tab, or requests reach the real one.
  http.get("/api/__mock", () => HttpResponse.json({ mock: true })),
  ...listingHandlers,
  ...singleDocumentHandlers,
  ...uploadHandlers,
  ...settingsHandlers,
  http.get("/api/auth/me", () =>
    isSignedIn()
      ? HttpResponse.json(me())
      : HttpResponse.json({ detail: "not signed in" }, { status: 401 }),
  ),
  http.post("/api/auth/login", async ({ request }) => {
    const body = (await request.json()) as { email: string; password: string };
    if (body.email !== MOCK_USER.name || body.password !== MOCK_PASSWORD) {
      return HttpResponse.json({ detail: "wrong email or password" }, { status: 401 });
    }
    setSignedIn(true);
    return HttpResponse.json(me());
  }),
  http.post("/api/auth/logout", ({ request }) => {
    if (request.headers.get("x-csrf-token") !== MOCK_USER.csrf_token) {
      return HttpResponse.json({ detail: "missing or wrong CSRF token" }, { status: 403 });
    }
    setSignedIn(false);
    return new HttpResponse(null, { status: 204 });
  }),
];
