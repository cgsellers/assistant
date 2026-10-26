/// <reference types="vitest/config" />
import { fileURLToPath, URL } from "node:url";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// The portal talks to the API under /api on its own origin. In development
// the dev server forwards /api/* to FastAPI with the prefix removed, so the
// session cookie and the CSRF header behave exactly as they will behind the
// production proxy: same origin, no CORS, no third-party cookies.
const API = process.env.LEDGERLINE_API ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    // PDF.js is the one chunk over Vite's 500 kB default (630 kB). It loads
    // only when a PDF is opened, never with the app, so the limit is raised
    // just past it: anything else growing that large still warns.
    chunkSizeWarningLimit: 700,
    rolldownOptions: {
      output: {
        // The framework in its own chunk: it changes with dependency upgrades,
        // not with every deploy, so browsers keep it cached across releases.
        codeSplitting: {
          groups: [
            {
              name: "framework",
              test: /node_modules[\\/](react|react-dom|react-router|scheduler|@tanstack[\\/](query-core|react-query))[\\/]/,
            },
          ],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: API,
        changeOrigin: false,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: ["./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
  },
});
