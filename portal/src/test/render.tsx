import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { createMemoryRouter, RouterProvider } from "react-router";
import { routes } from "@/router";

/** The real routes, at `path`, with a fresh cache: what a person would see. */
export function renderAt(path: string) {
  const queries = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const user = userEvent.setup();
  const view = render(
    <QueryClientProvider client={queries}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...view, router, user };
}
