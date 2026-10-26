import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll } from "vitest";
import { setSignedIn } from "@/mocks/handlers";
import { server } from "@/mocks/server";

// Any request without a handler fails the test: a screen must not quietly
// depend on an endpoint nobody has mocked or built.
beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  setSignedIn(false);
});
afterAll(() => server.close());
