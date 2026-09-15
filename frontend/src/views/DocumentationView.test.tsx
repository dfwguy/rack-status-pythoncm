/*
 * SPDX-FileCopyrightText: Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
 * SPDX-License-Identifier: LicenseRef-NvidiaProprietary
 *
 * NVIDIA CORPORATION, its affiliates and licensors retain all intellectual
 * property and proprietary rights in and to this material, related
 * documentation and any modifications thereto. Any use, reproduction,
 * disclosure or distribution of this material and related documentation
 * without an express license agreement from NVIDIA CORPORATION or
 * its affiliates is strictly prohibited.
 */
/**
 * The documentation opens inside the app, with a credential that is fresh when it does.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentationView } from "./DocumentationView";

vi.mock("../lib/apiFetch", () => ({
  apiFetch: vi.fn(async () => new Response("{}", { status: 200 })),
}));

const frame = () => document.querySelector("iframe");

describe("DocumentationView", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renews the session before it frames the docs, not after", async () => {
    /**
     * The order is the fix. `/docs/` is a server route behind the gate, and an iframe that
     * loads with an expired cookie renders the *login page inside the frame* — which nothing
     * out here can detect. So the renewal has to happen first.
     */
    const { apiFetch } = await import("../lib/apiFetch");
    render(<DocumentationView />);

    expect(frame()).toBeNull();
    await waitFor(() => expect(apiFetch).toHaveBeenCalled());
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(frame()?.getAttribute("src")).toBe("/docs/");
  });

  it("says so when the session cannot be renewed, instead of framing a login page", async () => {
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response("", { status: 401 }));
    render(<DocumentationView />);

    expect(await screen.findByText(/session could not be renewed/i)).toBeInTheDocument();
    expect(frame()).toBeNull();
  });

  it("keeps offering a new window, but no longer only that", async () => {
    // The old behaviour was `target="_blank"` and nothing else, which walks a desktop-shell
    // user out of the app. It stays available as a choice.
    render(<DocumentationView />);
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(screen.getByRole("button", { name: /open in a new window/i })).toBeInTheDocument();
  });
});
