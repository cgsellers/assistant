import { setupWorker } from "msw/browser";
import { handlers } from "./handlers";

// The mock API in the browser (`npm run dev:mocks`). Sign in with the demo
// account the sign-in page offers; see src/mocks/user.ts.
export const worker = setupWorker(...handlers);
