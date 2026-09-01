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
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));

    expect(await screen.findByText("SHOWN-ONLY-ONCE-abc123")).toBeInTheDocument();
  });

  it("says it cannot be retrieved again", async () => {
    vi.mocked(identity.createUser).mockResolvedValue({ login: "n", password: "pw-xyz" });

    render(asAdmin(<UsersView />));
    await screen.findByText("operator1");
    fireEvent.click(screen.getByRole("button", { name: /add user/i }));
    fireEvent.change(screen.getByLabelText(/^login/i), { target: { value: "n" } });
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
    fireEvent.click(screen.getByRole("button", { name: /create and show password/i }));
    await screen.findByText("pw-keepme");

    // Escape must not close it: this value is unrecoverable.
    fireEvent.keyDown(document.activeElement ?? document.body, { key: "Escape" });
    expect(screen.getByText("pw-keepme")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /i have copied the password/i }));
    await waitFor(() => expect(screen.queryByText("pw-keepme")).not.toBeInTheDocument());
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
