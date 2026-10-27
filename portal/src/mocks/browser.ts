import { setupWorker } from "msw/browser";
import { handlers } from "./handlers";

// The mock API in the browser (`npm run dev:mocks`). Sign in with the demo
// account the sign-in page offers; see src/mocks/user.ts.
export const worker = setupWorker(...handlers);

const RESTARTED = "ledgerline-mock-worker-restarted";

/** Whether the mock, and not the real API, answers requests from this tab. */
export async function mockAnswers(): Promise<boolean> {
  try {
    const response = await fetch("/api/__mock");
    return response.ok && ((await response.json()) as { mock?: boolean }).mock === true;
  } catch {
    return false;
  }
}

/**
 * Start the mock API, and make sure it is really answering.
 *
 * The worker answers only for tabs it has been told to mock, and lets every
 * other request through to the network. In Safari a tab can end up
 * controlled by a worker that was never told -- an older instance than the
 * one MSW talks to, or one restarted with an empty memory -- and then every
 * request quietly reaches the real API. When that is found, every
 * registration is removed and the page reloaded once, which starts a
 * fresh one. If even that does not help, the page loads anyway and the
 * sign-in page says what is wrong (components/mock-notice.tsx).
 */
export async function startMocks() {
  await worker.start({ onUnhandledFrame: "bypass" });
  if (await mockAnswers()) {
    sessionStorage.removeItem(RESTARTED);
    return;
  }
  if (sessionStorage.getItem(RESTARTED)) return;
  sessionStorage.setItem(RESTARTED, "yes");
  for (const registration of await navigator.serviceWorker.getRegistrations()) {
    await registration.unregister();
  }
  location.reload();
  // Never render the half-working page; the reload takes over.
  await new Promise(() => {});
}
