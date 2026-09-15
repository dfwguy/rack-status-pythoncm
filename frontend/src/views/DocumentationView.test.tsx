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
 * The documentation opens inside the app, and needs no credential to do it.
 * Author: Rafael M. Koike & Cursor Agent ("Ada")
 *
 * `/docs/` is public (see `web_auth.AUTH_BYPASS_ENDPOINTS`), so this view has one job beyond
 * framing the site: say so when the documentation was never built into the deployment, rather
 * than framing a paragraph of plain text.
 */
import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentationView } from "./DocumentationView";

vi.mock("../lib/apiFetch", () => ({
  apiFetch: vi.fn(async () => new Response("<html></html>", { status: 200 })),
}));

const frame = () => document.querySelector("iframe");

describe("DocumentationView", () => {
  beforeEach(() => vi.clearAllMocks());

  it("frames the documentation this deployment serves", async () => {
    render(<DocumentationView />);
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(frame()?.getAttribute("src")).toBe("/docs/");
  });

  it("says the documentation was never built, rather than framing the 503", async () => {
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response("not built", { status: 503 }));

    render(<DocumentationView />);

    expect(await screen.findByText(/has not been built/i)).toBeInTheDocument();
    expect(frame()).toBeNull();
  });

  it("frames the docs anyway when the check itself cannot run", async () => {
    // A transient blip must not become "no documentation".
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error("offline"));

    render(<DocumentationView />);
    await waitFor(() => expect(frame()).not.toBeNull());
  });

  it("keeps offering a new window, but no longer only that", async () => {
    // The old behaviour was `target="_blank"` and nothing else, which walks a desktop-shell user
    // out of the app entirely.
    render(<DocumentationView />);
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(screen.getByRole("button", { name: /open in a new window/i })).toBeInTheDocument();
  });
});
