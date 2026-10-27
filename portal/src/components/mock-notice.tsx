import { FlaskConical } from "lucide-react";
import { MOCK_PASSWORD, MOCK_USER } from "@/mocks/user";

/** True when `npm run dev:mocks` is serving: every answer comes from src/mocks, not the API. */
export const MOCKS = import.meta.env.DEV && Boolean(import.meta.env.VITE_MOCKS);

/** The demo account, offered on the sign-in page while the mock API is serving. */
export const DEMO = { email: MOCK_USER.name, password: MOCK_PASSWORD };

/** Says plainly that the data is made up, so no one mistakes it for the real API. */
export function MockBadge() {
  if (!MOCKS) return null;
  return (
    <span
      title="Running against the mock API in src/mocks; nothing here is real"
      className="inline-flex items-center gap-1 rounded-full bg-attention-soft px-2 py-0.5 text-[11px] font-medium text-attention"
    >
      <FlaskConical className="size-3" aria-hidden />
      Mock data
    </span>
  );
}
