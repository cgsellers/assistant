import { screen, waitFor } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { MOCK_PASSWORD, MOCK_USER, setSignedIn } from "@/mocks/handlers";
import { renderAt } from "@/test/render";

async function signIn(user: ReturnType<typeof renderAt>["user"], password = MOCK_PASSWORD) {
  await user.type(await screen.findByLabelText("Email"), MOCK_USER.name);
  await user.type(screen.getByLabelText("Password"), password);
  await user.click(screen.getByRole("button", { name: "Sign in" }));
}

describe("signing in", () => {
  it("sends a signed-out visitor to sign in, then back where they were going", async () => {
    const { user, router } = renderAt("/settings");
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();

    await signIn(user);

    await waitFor(() => expect(router.state.location.pathname).toBe("/settings"));
    expect(await screen.findByRole("button", { name: /Acme Ltd/ })).toBeInTheDocument();
  });

  it("explains a wrong password without saying which half was wrong", async () => {
    const { user } = renderAt("/sign-in");
    await signIn(user, "not the password");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That email and password don't match an account.",
    );
  });

  it("never follows a `next` that leaves the site", async () => {
    const { user, router } = renderAt("/sign-in?next=//evil.example/steal");
    await signIn(user);
    await waitFor(() => expect(router.state.location.pathname).toBe("/documents"));
  });

  it("skips the form for someone already signed in", async () => {
    setSignedIn(true);
    const { router } = renderAt("/sign-in");
    await waitFor(() => expect(router.state.location.pathname).toBe("/documents"));
  });
});

describe("signing out", () => {
  it("ends the session with the CSRF token and returns to sign in", async () => {
    setSignedIn(true);
    const { user } = renderAt("/documents");
    await user.click(await screen.findByRole("button", { name: /Acme Ltd/ }));
    await user.click(await screen.findByRole("menuitem", { name: /Sign out/ }));
    // The mock refuses a logout without the token, so reaching sign-in proves it was sent.
    expect(await screen.findByRole("heading", { name: "Sign in" })).toBeInTheDocument();
  });
});
