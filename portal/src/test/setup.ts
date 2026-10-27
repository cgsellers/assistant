import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { toast } from "sonner";
import { afterAll, afterEach, beforeAll } from "vitest";
import { resetCorrections } from "@/mocks/corrections";
import { setSignedIn } from "@/mocks/handlers";
import { server } from "@/mocks/server";

// Any request without a handler fails the test: a screen must not quietly
// depend on an endpoint nobody has mocked or built.
beforeAll(() => server.listen({ onUnhandledFrame: "error" }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  setSignedIn(false);
  resetCorrections();
  // Sonner keeps toasts in a module-level store and replays the active ones
  // to every new Toaster: without this, one test's toasts show up in the next.
  toast.dismiss();
});
afterAll(() => server.close());
