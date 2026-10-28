import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { mockInvitation } from "@/mocks/settings";
import { renderAt } from "@/test/render";

describe("accepting an invitation", () => {
  it("says who it is for, takes a password, and signs the person in", async () => {
    const token = mockInvitation("lee@acme.test", "viewer");
    const { user, router } = renderAt(`/accept-invite?token=${token}`);

    expect(await screen.findByRole("heading", { name: "Join Acme Ltd" })).toBeInTheDocument();
    expect(screen.getByText(/You're invited as a viewer, as/)).toHaveTextContent("lee@acme.test");
    // Read once, then out of the address bar.
    await waitFor(() => expect(router.state.location.search).toBe(""));

    await user.type(screen.getByLabelText("Password"), "a long new passphrase");
    await user.click(screen.getByRole("button", { name: "Join Acme Ltd" }));
    await waitFor(() => expect(router.state.location.pathname).toBe("/documents"));
  });

  it("says what is wrong with a password the server refuses, and keeps the link", async () => {
    const token = mockInvitation("lee@acme.test");
    const { user } = renderAt(`/accept-invite?token=${token}`);
    const password = await screen.findByLabelText("Password");
    // Past the browser's own check, so the server's answer is what shows.
    password.removeAttribute("minlength");
    await user.type(password, "short");
    await user.click(screen.getByRole("button", { name: /Join/ }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "a password needs at least 12 characters",
    );
    expect(screen.getByRole("heading", { name: "Join Acme Ltd" })).toBeInTheDocument();
  });

  it("explains a link that no longer works", async () => {
    renderAt("/accept-invite?token=used-or-made-up");
    expect(await screen.findByRole("alert")).toHaveTextContent("This link doesn't work any more");
    expect(screen.getByRole("link", { name: "Sign in instead" })).toHaveAttribute(
      "href",
      "/sign-in",
    );
  });

  it("explains a link with no token at all", async () => {
    renderAt("/accept-invite");
    expect(await screen.findByRole("alert")).toHaveTextContent("This link doesn't work any more");
  });
});
