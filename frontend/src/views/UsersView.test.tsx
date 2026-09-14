/* SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */

/**
 * `UsersView` — the properties where a UI mistake has a security consequence.
 *
 * Not the rendering. The three things asserted here are the ones whose failure is silent:
 *
 * 1. **The one-time password is shown and cannot be dismissed by accident.** The server keeps only
 *    a verifier, so losing this value leaves an account nobody can use. A dialog that closes on a
 *    backdrop click would look fine in every screenshot.
 * 2. **Destructive actions name their consequence** rather than asking "are you sure?", which would
 *    make retiring a login forever look like disabling one for an afternoon.
 * 3. **Actions the server would refuse are visibly unavailable** on your own row and on the last
 *    account that can sign in — the guard the Workshop Studio portal's page taught, enforced
 *    server-side and mirrored here.
 */

// `fireEvent`, not `user-event`: the other 27 test files in this suite use it and the package is
// not a dependency. Adding one for a single file is not worth the install.
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext } from "../context/AuthContext";
import * as identity from "../services/identityService";
import type { IdentityUser } from "../types/identity";
import { UsersView } from "./UsersView";

vi.mock("../services/identityService");

const asAdmin = (ui: React.ReactNode) => (
  <AuthContext.Provider
    value={{
      authenticated: true,
      user: "user:admin",
      loading: false,
      login: async () => ({ ok: true }),
      logout: async () => {},
      markSetupComplete: () => {},
    }}
  >
    {ui}
  </AuthContext.Provider>
);

// Display name deliberately differs from the login: setting them equal makes every row match
// `getByText(login)` twice — once in the Login cell, once in Display name — and the queries below
// then fail on ambiguity rather than on anything real.
const user = (login: string, extra: Partial<IdentityUser> = {}) => ({
  login,
  display_name: `${login} display`,
  disabled: false,
  must_change_password: false,
  auth_source: "local",
  email: `${login}@example.com`,
  phone_number: "",
  email_verified: false,
  phone_number_verified: false,
  given_name: "",
  family_name: "",
  zoneinfo: "",
  locale: "",
  groups: [],
  ...extra,
});

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(identity.listUsers).mockResolvedValue([
    user("admin"),
    user("operator1", { groups: ["operators"] }),
  ]);
});

describe("the one-time password", () => {
  it("is shown after creating a user", async () => {
    vi.mocked(identity.createUser).mockResolvedValue({
      login: "newbie",
      password: "SHOWN-ONLY-ONCE-abc123",
    });

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");

    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "newbie" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "newbie@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));

    expect(await screen.findByText("SHOWN-ONLY-ONCE-abc123")).toBeInTheDocument();
  });

  it("says it cannot be retrieved again", async () => {
    vi.mocked(identity.createUser).mockResolvedValue({ login: "n", password: "pw-xyz" });

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "n@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));

    await screen.findByText("pw-xyz");
    expect(screen.getByText(/only time this password is shown/i)).toBeInTheDocument();
  });

  it("has exactly one way out, so it cannot be dismissed by accident", async () => {
    vi.mocked(identity.createUser).mockResolvedValue({ login: "n", password: "pw-keepme" });

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "n@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));
    await screen.findByText("pw-keepme");

    // Escape must not close it: this value is unrecoverable.
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(screen.getByText("pw-keepme")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /i have copied the password/i }));
    await waitFor(() => expect(screen.queryByText("pw-keepme")).not.toBeInTheDocument());
  });
});

describe("choosing the initial password", () => {
  it("generates by default, so the safer path needs no decision", async () => {
    vi.mocked(identity.createUser).mockResolvedValue({ login: "n", password: "generated" });

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "n" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "n@example.com" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));

    await waitFor(() => expect(identity.createUser).toHaveBeenCalled());
    expect(identity.createUserWithPassword).not.toHaveBeenCalled();
  });

  it("sends a chosen password through the deriving path, never the generating one", async () => {
    vi.mocked(identity.createUserWithPassword).mockResolvedValue(undefined);

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "chosen" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "chosen@example.com" },
    });
    fireEvent.click(screen.getByLabelText(/set a password now/i));

    const password = "a-long-enough-password";
    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: password } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: password } });
    fireEvent.click(screen.getByRole("button", { name: /^create user$/i }));

    await waitFor(() =>
      expect(identity.createUserWithPassword).toHaveBeenCalledWith(
        "chosen",
        password,
        expect.objectContaining({ email: "chosen@example.com" }),
      ),
    );
    expect(identity.createUser).not.toHaveBeenCalled();
  });

  it("shows no one-time password panel for a chosen password", async () => {
    /* There is nothing to reveal — the operator already knows it — and an empty "here is the
       password" dialog would read as a bug. */
    vi.mocked(identity.createUserWithPassword).mockResolvedValue(undefined);

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "chosen" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "chosen@example.com" },
    });
    fireEvent.click(screen.getByLabelText(/set a password now/i));
    const pw = "a-long-enough-password";
    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: pw } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: pw } });
    fireEvent.click(screen.getByRole("button", { name: /^create user$/i }));

    await waitFor(() => expect(identity.createUserWithPassword).toHaveBeenCalled());
    expect(screen.queryByText(/only time this password is shown/i)).not.toBeInTheDocument();
  });

  it("will not submit a chosen password that is too short or mismatched", async () => {
    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "chosen" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "chosen@example.com" },
    });
    fireEvent.click(screen.getByLabelText(/set a password now/i));

    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: "short" } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: "short" } });
    expect(screen.getByRole("button", { name: /^create user$/i })).toBeDisabled();

    const ok = "a-long-enough-password";
    fireEvent.change(screen.getByLabelText(/^password/i), { target: { value: ok } });
    fireEvent.change(screen.getByLabelText(/confirm password/i), { target: { value: `${ok}x` } });
    expect(screen.getByRole("button", { name: /^create user$/i })).toBeDisabled();

    expect(identity.createUserWithPassword).not.toHaveBeenCalled();
  });

  it("tells the operator the password does not reach the server", async () => {
    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.click(screen.getByLabelText(/set a password now/i));

    expect(screen.getByText(/not sent to the server/i)).toBeInTheDocument();
  });
});

describe("destructive actions name their consequence", () => {
  it("says a deleted login is retired permanently", async () => {
    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");

    fireEvent.click(screen.getByRole("button", { name: /actions for operator1/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /delete/i }));

    expect(await screen.findByText(/retired permanently/i)).toBeInTheDocument();
    expect(screen.getByText(/can never be created again/i)).toBeInTheDocument();
  });

  it("says a reset signs the holder out everywhere", async () => {
    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");

    fireEvent.click(screen.getByRole("button", { name: /actions for operator1/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /reset password/i }));

    expect(await screen.findByText(/signed out everywhere/i)).toBeInTheDocument();
  });

  it("does not delete anything until confirmed", async () => {
    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");

    fireEvent.click(screen.getByRole("button", { name: /actions for operator1/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /delete/i }));
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    expect(identity.deleteUser).not.toHaveBeenCalled();
  });
});

describe("actions the server would refuse are unavailable", () => {
  it("will not offer to delete your own account", async () => {
    render(asAdmin(<UsersView />));
    await screen.findByText("admin");

    fireEvent.click(screen.getByRole("button", { name: /actions for admin/i }));
    expect(screen.getByRole("menuitem", { name: /delete/i })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("will not offer to disable the last account that can sign in", async () => {
    vi.mocked(identity.listUsers).mockResolvedValue([
      user("solo"),
      user("shut-out", { disabled: true }),
    ]);

    render(asAdmin(<UsersView />));
    await screen.findByText("solo");

    fireEvent.click(screen.getByRole("button", { name: /actions for solo/i }));
    expect(screen.getByRole("menuitem", { name: /disable/i })).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });

  it("marks your own row so the disabled actions make sense", async () => {
    render(asAdmin(<UsersView />));
    expect(await screen.findByText("you")).toBeInTheDocument();
  });
});

describe("the server's refusal is what the operator reads", () => {
  it("surfaces the server message rather than a generic failure", async () => {
    vi.mocked(identity.deleteUser).mockRejectedValue(
      new Error("refusing to delete the last account that can sign in"),
    );

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");

    fireEvent.click(screen.getByRole("button", { name: /actions for operator1/i }));
    fireEvent.click(screen.getByRole("menuitem", { name: /delete/i }));
    fireEvent.click(
      screen.getByRole("button", { name: /delete and retire the login/i }),
    );

    expect(
      await screen.findByText(/refusing to delete the last account/i),
    ).toBeInTheDocument();
  });
});

describe("contact attributes", () => {
  /* `.plc/briefs/user-contact-attributes-l0.md` R5. */

  it("requires an email before a user can be created", async () => {
    /* Mirrors the server rule at the point the operator can still fix it, rather than letting the
       request go and rendering a refusal. */
    render(asAdmin(<UsersView />));
    fireEvent.click(await screen.findByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "newbie" } });

    expect(screen.getByRole("button", { name: /create and show password/i })).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "newbie@example.com" },
    });
    expect(screen.getByRole("button", { name: /create and show password/i })).toBeEnabled();
  });

  it("keeps the create button disabled for an address that is obviously malformed", async () => {
    render(asAdmin(<UsersView />));
    fireEvent.click(await screen.findByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "newbie" } });
    fireEvent.change(screen.getByLabelText(/^email/i), { target: { value: "not-an-address" } });

    expect(screen.getByRole("button", { name: /create and show password/i })).toBeDisabled();
  });

  it("passes the address and phone number through to the create call", async () => {
    vi.mocked(identity.createUser).mockResolvedValue({ login: "newbie", password: "pw" });
    render(asAdmin(<UsersView />));
    fireEvent.click(await screen.findByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "newbie" } });
    fireEvent.change(screen.getByLabelText(/^email/i), {
      target: { value: "newbie@example.com" },
    });
    fireEvent.change(screen.getByLabelText(/phone number/i), {
      target: { value: "+1 555 010 4477" },
    });
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));

    await waitFor(() =>
      expect(identity.createUser).toHaveBeenCalledWith("newbie", {
        display_name: "",
        email: "newbie@example.com",
        phone_number: "+1 555 010 4477",
        given_name: "",
        family_name: "",
      }),
    );
  });

  it("shows each user's address in the table", async () => {
    render(asAdmin(<UsersView />));
    expect(await screen.findByText("admin@example.com")).toBeInTheDocument();
    expect(screen.getByText("operator1@example.com")).toBeInTheDocument();
  });

  it("flags an account that has no address rather than leaving the cell blank", async () => {
    /* Accounts predating this field — the bootstrap admin among them — cannot be notified, and
       that gap should be visible in the table rather than reading as an empty column. */
    vi.mocked(identity.listUsers).mockResolvedValue([
      user("admin", { email: "" }),
      user("operator1"),
    ]);
    render(asAdmin(<UsersView />));

    expect(await screen.findByText("none")).toBeInTheDocument();
  });

  it("marks a verified address and leaves an unverified one unmarked", async () => {
    vi.mocked(identity.listUsers).mockResolvedValue([
      user("admin", { email_verified: true }),
      user("operator1"),
    ]);
    render(asAdmin(<UsersView />));

    expect(await screen.findByText("verified")).toBeInTheDocument();
    // One tag, not two: only the verified row carries it.
    expect(screen.getAllByText("verified")).toHaveLength(1);
  });

  it("finds a user by address as well as by login", async () => {
    render(asAdmin(<UsersView />));
    await screen.findByText("admin@example.com");
    fireEvent.change(screen.getByPlaceholderText(/search/i), {
      target: { value: "operator1@example" },
    });

    expect(screen.queryByText("admin@example.com")).not.toBeInTheDocument();
    expect(screen.getByText("operator1@example.com")).toBeInTheDocument();
  });
});

describe("editing a profile", () => {
  /* `.plc/briefs/user-contact-attributes-l0.md` R5. Without this path an address can never be
     corrected, and the bootstrap admin — which has none — could never be notified at all. */

  const openRowMenu = async (login: string) => {
    const row = (await screen.findByText(login)).closest("tr");
    fireEvent.click(row!.querySelector("button")!);
  };

  it("gives the address-less bootstrap admin a way to get one", async () => {
    vi.mocked(identity.listUsers).mockResolvedValue([
      user("admin", { email: "" }),
      user("operator1"),
    ]);
    vi.mocked(identity.updateProfile).mockResolvedValue(user("admin", { email: "a@b.com" }));
    render(asAdmin(<UsersView />));

    await openRowMenu("admin");
    fireEvent.click(screen.getByText(/edit profile/i));
    fireEvent.change(screen.getByLabelText(/^email/i), { target: { value: "admin@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(identity.updateProfile).toHaveBeenCalledWith("admin", {
        email: "admin@example.com",
      }),
    );
  });

  it("sends only the fields that actually moved", async () => {
    /* Resending an unchanged address would clear a standing verified mark for no reason, because
       the server clears it whenever an address is set. */
    vi.mocked(identity.listUsers).mockResolvedValue([user("admin"), user("operator1")]);
    vi.mocked(identity.updateProfile).mockResolvedValue(user("operator1"));
    render(asAdmin(<UsersView />));

    await openRowMenu("operator1");
    fireEvent.click(screen.getByText(/edit profile/i));
    fireEvent.change(screen.getByLabelText(/display name/i), { target: { value: "Renamed" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(identity.updateProfile).toHaveBeenCalledWith("operator1", {
        display_name: "Renamed",
      }),
    );
  });

  it("writes nothing when nothing changed", async () => {
    render(asAdmin(<UsersView />));
    await openRowMenu("operator1");
    fireEvent.click(screen.getByText(/edit profile/i));
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() => expect(screen.queryByText(/edit operator1/i)).not.toBeInTheDocument());
    expect(identity.updateProfile).not.toHaveBeenCalled();
  });

  it("warns that changing a verified address drops the mark", async () => {
    vi.mocked(identity.listUsers).mockResolvedValue([
      user("admin"),
      user("operator1", { email_verified: true }),
    ]);
    render(asAdmin(<UsersView />));

    await openRowMenu("operator1");
    fireEvent.click(screen.getByText(/edit profile/i));
    fireEvent.change(screen.getByLabelText(/^email/i), { target: { value: "moved@example.com" } });

    expect(screen.getByText(/clears its verified mark/i)).toBeInTheDocument();
  });

  it("asserts verification through its own action, not the edit dialog", async () => {
    /* R4: the claim is audited separately from a profile edit, so it is a separate menu item and
       the edit dialog offers no checkbox for it. */
    vi.mocked(identity.setEmailVerified).mockResolvedValue(
      user("operator1", { email_verified: true }),
    );
    render(asAdmin(<UsersView />));

    await openRowMenu("operator1");
    fireEvent.click(screen.getByText(/mark email verified/i));

    await waitFor(() => expect(identity.setEmailVerified).toHaveBeenCalledWith("operator1", true));
  });

  it("cannot assert verification for a user with no address", async () => {
    vi.mocked(identity.listUsers).mockResolvedValue([
      user("admin", { email: "" }),
      user("operator1"),
    ]);
    render(asAdmin(<UsersView />));

    await openRowMenu("admin");
    expect(screen.getByText(/mark email verified/i).closest("li")).toHaveAttribute(
      "aria-disabled",
      "true",
    );
  });
});

describe("oidc profile fields", () => {
  /* `.plc/briefs/oidc-directory-claims-l0.md` R1. */

  const openRowMenu = async (login: string) => {
    const row = (await screen.findByText(login)).closest("tr");
    fireEvent.click(row!.querySelector("button")!);
  };

  it("collects the given and family name on create", async () => {
    vi.mocked(identity.createUser).mockResolvedValue({ login: "ada", password: "pw" });
    render(asAdmin(<UsersView />));
    fireEvent.click(await screen.findByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "ada" } });
    fireEvent.change(screen.getByLabelText(/^email/i), { target: { value: "ada@example.com" } });
    fireEvent.change(screen.getByLabelText(/first name/i), { target: { value: "Ada" } });
    fireEvent.change(screen.getByLabelText(/last name/i), { target: { value: "Lovelace" } });
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));

    await waitFor(() =>
      expect(identity.createUser).toHaveBeenCalledWith(
        "ada",
        expect.objectContaining({ given_name: "Ada", family_name: "Lovelace" }),
      ),
    );
  });

  it("edits the time zone and locale", async () => {
    vi.mocked(identity.updateProfile).mockResolvedValue(user("operator1"));
    render(asAdmin(<UsersView />));
    await openRowMenu("operator1");
    fireEvent.click(screen.getByText(/edit profile/i));
    fireEvent.change(screen.getByLabelText(/time zone/i), {
      target: { value: "Europe/London" },
    });
    fireEvent.change(screen.getByLabelText(/^locale/i), { target: { value: "en-GB" } });
    fireEvent.click(screen.getByRole("button", { name: /^save$/i }));

    await waitFor(() =>
      expect(identity.updateProfile).toHaveBeenCalledWith("operator1", {
        zoneinfo: "Europe/London",
        locale: "en-GB",
      }),
    );
  });

  it("seeds the edit dialog from the stored values", async () => {
    vi.mocked(identity.listUsers).mockResolvedValue([
      user("admin"),
      user("operator1", { given_name: "Grace", zoneinfo: "America/New_York" }),
    ]);
    render(asAdmin(<UsersView />));
    await openRowMenu("operator1");
    fireEvent.click(screen.getByText(/edit profile/i));

    expect(screen.getByLabelText(/first name/i)).toHaveValue("Grace");
    expect(screen.getByLabelText(/time zone/i)).toHaveValue("America/New_York");
  });
});
