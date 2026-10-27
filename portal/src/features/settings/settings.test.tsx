import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";
import { setSignedIn } from "@/mocks/handlers";
import { server } from "@/mocks/server";
import { setMockRole } from "@/mocks/user";
import { renderAt } from "@/test/render";

beforeEach(() => setSignedIn(true));

const keysTable = () => screen.findByRole("table", { name: "API keys" });
const teamTable = () => screen.findByRole("table", { name: "Team" });

describe("API keys", () => {
  it("shows a new key's secret once, ready to copy and use, and never again", async () => {
    const { user } = renderAt("/settings");
    await keysTable();
    await user.click(screen.getByRole("button", { name: "Create key" }));
    const form = await screen.findByRole("dialog", { name: "Create an API key" });
    await user.type(within(form).getByLabelText("Name"), "Sage import");
    await user.click(within(form).getByRole("button", { name: "Create key" }));

    const shown = await screen.findByRole("dialog", { name: "Your new key" });
    const secret = (within(shown).getByLabelText("The new key") as HTMLInputElement).value;
    expect(secret).toMatch(/^lk_[A-Za-z0-9_-]{43}$/);
    expect(within(shown).getByText(/It won't be shown again/)).toBeInTheDocument();
    expect(within(shown).getByText(new RegExp(`Bearer ${secret}`))).toBeInTheDocument();

    await user.click(within(shown).getByRole("button", { name: "Copy" }));
    expect(await within(shown).findByRole("button", { name: "Copied" })).toBeInTheDocument();
    expect(await navigator.clipboard.readText()).toBe(secret);

    await user.click(within(shown).getByRole("button", { name: "Done" }));
    const row = within(await keysTable())
      .getByText("Sage import")
      .closest("tr") as HTMLElement;
    expect(within(row).getByText(`${secret.slice(0, 11)}…`)).toBeInTheDocument();
    expect(screen.queryByText(secret)).not.toBeInTheDocument();
  });

  it("revokes a key after saying what that will break", async () => {
    const { user } = renderAt("/settings");
    await keysTable();
    await user.click(screen.getByRole("button", { name: "Revoke NetSuite production" }));
    const confirm = await screen.findByRole("dialog", { name: 'Revoke "NetSuite production"?' });
    expect(confirm).toHaveTextContent("Anything using it stops working at once.");
    await user.click(within(confirm).getByRole("button", { name: "Revoke key" }));

    expect(await screen.findByText('"NetSuite production" is revoked.')).toBeInTheDocument();
    await waitFor(async () =>
      expect(within(await keysTable()).queryByText("NetSuite production")).not.toBeInTheDocument(),
    );
  });
});

describe("the team", () => {
  it("invites someone and hands over the link, since no email is sent", async () => {
    const { user } = renderAt("/settings");
    await teamTable();
    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    const form = await screen.findByRole("dialog", { name: "Invite someone" });
    await user.type(within(form).getByLabelText("Email"), "Lee@Acme.test");
    await user.click(within(form).getByRole("radio", { name: /Viewer/ }));
    await user.click(within(form).getByRole("button", { name: "Create invitation" }));

    const sent = await screen.findByRole("dialog", { name: "Send them this link" });
    expect(sent).toHaveTextContent("pass this link to lee@acme.test yourself");
    expect((within(sent).getByLabelText("Invitation link") as HTMLInputElement).value).toMatch(
      /\/accept-invite\?token=/,
    );
    await user.click(within(sent).getByRole("button", { name: "Done" }));

    const row = within(await teamTable())
      .getByText("lee@acme.test")
      .closest("tr") as HTMLElement;
    expect(within(row).getByText("Invited")).toBeInTheDocument();
    expect(within(row).getByRole("combobox", { name: "Role of lee@acme.test" })).toHaveValue(
      "viewer",
    );
  });

  it("says why when someone is already in the team", async () => {
    const { user } = renderAt("/settings");
    await teamTable();
    await user.click(screen.getByRole("button", { name: "Invite someone" }));
    const form = await screen.findByRole("dialog", { name: "Invite someone" });
    await user.type(within(form).getByLabelText("Email"), "ana.ruiz@acme.test");
    await user.click(within(form).getByRole("button", { name: "Create invitation" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "ana.ruiz@acme.test is already in the team.",
    );
  });

  it("changes a role, and shows the server's reason when it refuses", async () => {
    const { user } = renderAt("/settings");
    await teamTable();
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Role of ana.ruiz@acme.test" }),
      "admin",
    );
    expect(await screen.findByText("ana.ruiz@acme.test is now admin.")).toBeInTheDocument();

    server.use(
      http.patch("/api/settings/members/:id", () =>
        HttpResponse.json({ detail: "Acme Ltd needs at least one admin." }, { status: 409 }),
      ),
    );
    await user.selectOptions(
      screen.getByRole("combobox", { name: "Role of ana.ruiz@acme.test" }),
      "viewer",
    );
    expect(await screen.findByText("Acme Ltd needs at least one admin.")).toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByRole("combobox", { name: "Role of ana.ruiz@acme.test" })).toHaveValue(
        "admin",
      ),
    );
  });

  it("removes someone, but never offers to remove yourself", async () => {
    const { user } = renderAt("/settings");
    await teamTable();
    expect(screen.queryByRole("button", { name: "Remove bo@acme.test" })).not.toBeInTheDocument();
    expect(
      screen.queryByRole("combobox", { name: "Role of bo@acme.test" }),
    ).not.toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: "Remove audit@external-accountants.test" }),
    );
    const confirm = await screen.findByRole("dialog", {
      name: "Remove audit@external-accountants.test?",
    });
    await user.click(within(confirm).getByRole("button", { name: "Remove" }));
    await waitFor(async () =>
      expect(
        within(await teamTable()).queryByText("audit@external-accountants.test"),
      ).not.toBeInTheDocument(),
    );
  });
});

describe("someone who may only look", () => {
  beforeEach(() => setMockRole("viewer"));

  it("sees settings, and can change none of them", async () => {
    renderAt("/settings");
    await teamTable();
    expect(screen.getByText("Only an admin can change API keys and the team.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create key" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Revoke/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Invite someone" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("is offered nothing that changes documents, anywhere", async () => {
    const { router } = renderAt("/documents");
    await screen.findAllByRole("link", { name: "Globe Logistics" });
    expect(screen.queryByRole("link", { name: "Upload" })).not.toBeInTheDocument();

    await router.navigate("/documents/doc-004");
    expect(await screen.findByRole("status", { name: "Needs your review" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Review and fix" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve as it is" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit" })).not.toBeInTheDocument();

    await router.navigate("/upload");
    expect(
      await screen.findByText(/Your role can see documents but not upload them/),
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Choose files to upload")).not.toBeInTheDocument();
  });
});
