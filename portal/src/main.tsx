import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "@/App";
import "@/index.css";

async function start() {
  // `npm run dev:mocks`: the portal runs against mock handlers, with no API.
  // Used while a screen is built ahead of its endpoint; never in a build.
  if (import.meta.env.DEV && import.meta.env.VITE_MOCKS) {
    const { startMocks } = await import("@/mocks/browser");
    await startMocks();
  }
  const root = document.getElementById("root");
  if (!root) throw new Error("no #root element");
  createRoot(root).render(
    <StrictMode>
      <App />
    </StrictMode>,
  );
}

void start();
