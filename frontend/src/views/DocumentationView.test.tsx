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
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DocumentationView } from "./DocumentationView";

vi.mock("../lib/apiFetch", () => ({
  apiFetch: vi.fn(async () => new Response("<html></html>", { status: 200 })),
}));

const frame = () => document.querySelector("iframe");

/**
 * The view navigates (its Back button) and reads router state, so it needs a router — and real
 * routes, not just a provider: without them the view stays mounted after navigating and a test
 * asserting "it left" passes against a view that went nowhere.
 */
const show = (from?: string) =>
  render(
    <MemoryRouter initialEntries={[{ pathname: "/documentation", state: from ? { from } : null }]}>
      <Routes>
        <Route path="/documentation" element={<DocumentationView />} />
        <Route path="*" element={<div data-testid="left-the-docs" />} />
      </Routes>
    </MemoryRouter>,
  );

describe("DocumentationView", () => {
  beforeEach(() => vi.clearAllMocks());

  it("frames the documentation this deployment serves", async () => {
    show();
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(frame()?.getAttribute("src")).toBe("/docs/");
  });

  it("says the documentation was never built, rather than framing the 503", async () => {
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockResolvedValueOnce(new Response("not built", { status: 503 }));

    show();

    expect(await screen.findByText(/has not been built/i)).toBeInTheDocument();
    expect(frame()).toBeNull();
  });

  it("frames the docs anyway when the check itself cannot run", async () => {
    // A transient blip must not become "no documentation".
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockRejectedValueOnce(new Error("offline"));

    show();
    await waitFor(() => expect(frame()).not.toBeNull());
  });

  it("keeps offering a new window, but no longer only that", async () => {
    // The old behaviour was `target="_blank"` and nothing else, which walks a desktop-shell user
    // out of the app entirely.
    show();
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(screen.getByRole("button", { name: /open in a new window/i })).toBeInTheDocument();
  });
});

describe("owning the viewport", () => {
  it("offers a way back to the route the reader came from", async () => {
    /**
     * Router state, not browser history, and that is the point.
     *
     * Same-origin iframe navigations push entries onto the *parent's* session history, so after
     * reading five pages `navigate(-1)` walks back through documentation pages — the operator
     * presses Back and appears not to move. The route being left is carried in state instead.
     */
    show("/compute/racks");
    await waitFor(() => expect(frame()).not.toBeNull());

    const back = screen.getByRole("button", { name: /back/i });
    expect(back).toBeInTheDocument();
    fireEvent.click(back);
    await waitFor(() => expect(screen.getByTestId("left-the-docs")).toBeInTheDocument());
  });

  it("does not trap a reader who arrived by deep link", async () => {
    // No router state — a reload or a pasted URL. Back must still go somewhere.
    show();
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(screen.getByRole("button", { name: /back/i })).toBeEnabled();
  });

  it("leaves the documentation's own chrome to the documentation", async () => {
    /**
     * The Sphinx site ships a navigation sidebar, a search box, an "On this page" rail and a
     * light/dark toggle. Ours would be a second of each on screen, which is why this view has
     * one slim bar holding only what the docs cannot provide: a way out and a way to detach.
     */
    show();
    await waitFor(() => expect(frame()).not.toBeNull());

    for (const absent of [/reload/i, /ask ai/i, /^help$/i]) {
      expect(screen.queryByRole("button", { name: absent })).toBeNull();
    }
    expect(screen.getByRole("button", { name: /open in a new window/i })).toBeInTheDocument();
    // Full-bleed: the frame carries no border or inset of its own.
    expect(frame()?.style.width).toBe("100%");
    expect(frame()?.style.height).toBe("100%");
  });
});
