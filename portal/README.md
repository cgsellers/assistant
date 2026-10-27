# Ledgerline client portal

The website a client's staff use: their documents, each one beside what was read from
it, and fixing what needs review. A React app talking to the Ledgerline API on the same
origin under `/api`. The decisions behind it are in `docs/DESIGN.md` §9.4.

```bash
npm install
npm run dev          # http://localhost:5173, using the API on :8000 through /api
npm run dev:mocks    # no backend: the mock API in src/mocks
npm run api          # regenerate src/api/schema.d.ts after a backend change
npm test && npm run lint && npm run typecheck && npm run build
```

With `dev:mocks`, the sign-in page offers the demo account (`bo@acme.test`, password `correct horse battery`) and checks that the mock API is really answering in the tab; a "Mock data" badge stays in the header.

## Layout

| Path | What it is |
|---|---|
| `src/api/schema.d.ts` | Types generated from the API's OpenAPI schema. Never edited by hand. |
| `src/api/contract.ts` | Endpoints the portal needs that the API does not have yet. Each is deleted once the API serves it. |
| `src/mocks/` | MSW handlers, shared by the browser (`dev:mocks`) and the tests. They are the contract. |
| `src/mocks/files.ts` | The mock originals: each invoice a generated PDF, each receipt a canvas-drawn photo, matching its fields. |
| `src/components/ui/` | Buttons, inputs, tables and menus, on Radix primitives. Our own code, not a dependency. |
| `src/features/` | One folder per area: `auth`; `documents` (the list, one document, its viewer in `viewer/`, review and correction); `upload` (the queue, which outlives the page). |
| `src/lib/` | The API client, formatting of money and dates, small helpers. |

Tests render the real routes against the mock API (`src/test/render.tsx`), and any
request without a handler fails the test.
