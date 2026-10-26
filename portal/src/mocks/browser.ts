import { setupWorker } from "msw/browser";
import { handlers, setSignedIn } from "./handlers";

// In mock mode the person starts signed out, as in production: sign in as
// bo@acme.test with the password in handlers.ts.
setSignedIn(false);

export const worker = setupWorker(...handlers);
