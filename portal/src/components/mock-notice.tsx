import { CircleCheck, CircleX, FlaskConical } from "lucide-react";
import { useEffect, useState } from "react";
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

type Check = { state: "checking" } | { state: "answering" } | { state: "silent"; why: string };

/**
 * Whether the mock API is really answering in this tab. Only the mock knows
 * /api/__mock; if anything else answers, requests are reaching the real API.
 */
export function MockCheck() {
  const [check, setCheck] = useState<Check>({ state: "checking" });
  useEffect(() => {
    const worker = navigator.serviceWorker?.controller
      ? `a service worker controls this tab (${navigator.serviceWorker.controller.scriptURL})`
      : "no service worker controls this tab";
    fetch("/api/__mock")
      .then(async (response) => {
        const body = response.ok ? ((await response.json()) as { mock?: boolean }) : null;
        setCheck(
          body?.mock
            ? { state: "answering" }
            : { state: "silent", why: `the real API answered (${response.status}); ${worker}` },
        );
      })
      .catch((error: Error) => setCheck({ state: "silent", why: `${error.message}; ${worker}` }));
  }, []);

  if (check.state === "checking")
    return <p className="mt-1.5 text-xs text-muted">Checking the mock API…</p>;
  if (check.state === "answering") {
    return (
      <p className="mt-1.5 flex items-center gap-1 text-xs text-ok">
        <CircleCheck className="size-3.5" aria-hidden /> The mock API is answering in this tab.
      </p>
    );
  }
  return (
    <p className="mt-1.5 flex items-start gap-1 text-xs text-danger">
      <CircleX className="mt-px size-3.5 shrink-0" aria-hidden />
      <span>The mock API is not answering in this tab: {check.why}.</span>
    </p>
  );
}
