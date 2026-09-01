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
 * Regression tests for a real bug: the change-password screen rejected a correct password.
 *
 * SRP derives `x = H(salt, H(login ":" password))`, so the identity string handed to the client has
 * to be byte-identical to the one the server enrolled. `AuthState.user` was passed, and it held a
 * *display name* after a fresh sign-in (`"Administrator"` for the bootstrap admin) and a
 * *principal* after a page reload (`"user:admin"`). Either produced a well-formed proof the server
 * could only reject — reported to the operator as "Current password is incorrect" while the password
 * was perfectly correct.
 *
 * **No existing test could have caught it.** The backend tests drove SRP with the right login, and
 * the frontend had no test that checked *which* identity the page passes on. So these assert the
 * argument rather than the outcome: that is where the defect lived.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import * as srpClient from "../lib/srpClient";
import { ChangePasswordPage } from "./ChangePasswordPage";

vi.mock("../lib/srpClient");

const NEW = "a-sufficiently-long-password";

const fillAndSubmit = (current = "current-pw", next = NEW) => {
  fireEvent.change(screen.getByLabelText(/current password/i), {
    target: { value: current },
  });
  fireEvent.change(screen.getByLabelText(/^new password/i), { target: { value: next } });
  fireEvent.change(screen.getByLabelText(/confirm new password/i), { target: { value: next } });
  fireEvent.click(screen.getByRole("button", { name: /change password/i }));
};

beforeEach(() => {
  vi.resetAllMocks();
  vi.mocked(srpClient.changePassword).mockResolvedValue({ ok: true });
});

describe("the identity handed to SRP", () => {
  it("is the login it was given, verbatim", async () => {
    render(<ChangePasswordPage login="admin" onChanged={() => {}} />);
    fillAndSubmit();

    await waitFor(() =>
      expect(srpClient.changePassword).toHaveBeenCalledWith("admin", "current-pw", NEW),
    );
  });

  it("is never a display name", async () => {
    // The exact bug: `AuthState.user` was "Administrator" straight after a fresh sign-in.
    render(<ChangePasswordPage login="admin" onChanged={() => {}} />);
    fillAndSubmit();

    await waitFor(() => expect(srpClient.changePassword).toHaveBeenCalled());
    const [identity] = vi.mocked(srpClient.changePassword).mock.calls[0];
    expect(identity).not.toBe("Administrator");
  });

  it("is never a principal string", async () => {
    // The other half: after a page reload `AuthState.user` was "user:admin".
    render(<ChangePasswordPage login="admin" onChanged={() => {}} />);
    fillAndSubmit();

    await waitFor(() => expect(srpClient.changePassword).toHaveBeenCalled());
    const [identity] = vi.mocked(srpClient.changePassword).mock.calls[0];
    expect(identity).not.toMatch(/^user:/);
  });
});

describe("a missing identity is reported as such", () => {
  it("does not attempt the exchange", async () => {
    render(<ChangePasswordPage login={null} onChanged={() => {}} />);
    fillAndSubmit();

    await waitFor(() => expect(srpClient.changePassword).not.toHaveBeenCalled());
  });

  it("says the identity is the problem, not the password", async () => {
    /* Guessing an identity here is what turned a state bug into "your password is wrong" — the
       least debuggable message this product can show. */
    render(<ChangePasswordPage login={undefined} onChanged={() => {}} />);
    fillAndSubmit();

    expect(await screen.findByText(/identity could not be determined/i)).toBeInTheDocument();
    expect(screen.queryByText(/current password is incorrect/i)).not.toBeInTheDocument();
  });
});

describe("the form guards what it can before asking the server", () => {
  it("will not submit a password shorter than the minimum", () => {
    render(<ChangePasswordPage login="admin" onChanged={() => {}} />);
    fillAndSubmit("current-pw", "short");
    expect(srpClient.changePassword).not.toHaveBeenCalled();
  });

  it("will not submit when the confirmation differs", () => {
    render(<ChangePasswordPage login="admin" onChanged={() => {}} />);
    fireEvent.change(screen.getByLabelText(/current password/i), {
      target: { value: "current-pw" },
    });
    fireEvent.change(screen.getByLabelText(/^new password/i), { target: { value: NEW } });
    fireEvent.change(screen.getByLabelText(/confirm new password/i), {
      target: { value: `${NEW}-typo` },
    });
    fireEvent.click(screen.getByRole("button", { name: /change password/i }));

    expect(srpClient.changePassword).not.toHaveBeenCalled();
    expect(screen.getByText(/do not match/i)).toBeInTheDocument();
  });

  it("will not submit the current password unchanged", () => {
    render(<ChangePasswordPage login="admin" onChanged={() => {}} />);
    fillAndSubmit(NEW, NEW);
    expect(srpClient.changePassword).not.toHaveBeenCalled();
    expect(screen.getByText(/must differ from the current one/i)).toBeInTheDocument();
  });
});

describe("after a successful change", () => {
  it("signs the operator out, because the server revoked every session", async () => {
    const onChanged = vi.fn();
    render(<ChangePasswordPage login="admin" onChanged={onChanged} />);
    fillAndSubmit();

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("surfaces the server's own message on failure", async () => {
    vi.mocked(srpClient.changePassword).mockResolvedValue({
      ok: false,
      error: "Current password is incorrect",
    });

    render(<ChangePasswordPage login="admin" onChanged={() => {}} />);
    fillAndSubmit();

    expect(await screen.findByText(/current password is incorrect/i)).toBeInTheDocument();
  });
});
