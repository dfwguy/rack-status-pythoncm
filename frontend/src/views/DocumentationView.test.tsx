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

/** As above, but carrying the page a caller asked to land on. */
const showPage = (docPage: unknown) =>
  render(
    <MemoryRouter initialEntries={[{ pathname: "/documentation", state: { docPage } }]}>
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

  it("lands on the page a caller asked for, so a deep link is worth making", async () => {
    showPage("how-to/authenticate-to-the-api.html");
    await waitFor(() => expect(frame()).not.toBeNull());
    expect(frame()?.getAttribute("src")).toBe("/docs/how-to/authenticate-to-the-api.html");
  });

  /**
   * The value is router state today, not a query parameter, so none of these is reachable by a
   * crafted link. The test is what keeps the validation in place for the day somebody wires this
   * to the URL — which is the obvious next step, and the point at which an unvalidated page would
   * frame an arbitrary origin inside our own chrome.
   */
  it.each([
    ["an absolute URL", "https://evil.example/pwn.html"],
    ["a protocol-relative URL", "//evil.example/pwn.html"],
    ["traversal out of the docs tree", "../../etc/passwd.html"],
    ["a javascript scheme", "javascript:alert(1)//.html"],
    ["something that is not a string", { toString: (): string => "ok.html" }],
  ])("refuses %s and frames the docs root instead", async (_label, page) => {
    showPage(page);
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

describe("naming the build, without depending on a session", () => {
  it("shows the version from the public endpoint", async () => {
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockImplementation(async (input: RequestInfo | URL) =>
      String(input) === "/api/about"
        ? new Response(JSON.stringify({ ok: true, version: "0.3.0-dev" }), { status: 200 })
        : new Response("<html></html>", { status: 200 }),
    );

    show();

    expect(await screen.findByText("0.3.0-dev")).toBeInTheDocument();
    expect(screen.queryByText(/version running here/i)).toBeNull();
  });

  it("asks only endpoints that need no credential", async () => {
    /**
     * The regression to prevent. This view used to read the version from `/api/app-stats`, which
     * is gated, so a page served publicly at `/docs/` depended on a session for one string. It
     * worked — the view only renders for a signed-in operator — which is precisely what made it
     * easy to miss. `/api/about` carries the same version from the same `app_version` function
     * and needs no credential.
     */
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockClear();

    show();
    await waitFor(() => expect(frame()).not.toBeNull());

    const asked = vi.mocked(apiFetch).mock.calls.map(([input]) => String(input)).sort();
    expect(asked).toEqual(["/api/about", "/docs/"]);
  });

  it("shows no version rather than an error when the endpoint cannot be read", async () => {
    // An unreachable endpoint is not a reason to withhold the documentation, and an empty slot
    // reads as "no version" rather than as something broken.
    const { apiFetch } = await import("../lib/apiFetch");
    vi.mocked(apiFetch).mockImplementation(async (input: RequestInfo | URL) =>
      String(input) === "/api/about"
        ? Promise.reject(new Error("offline"))
        : new Response("<html></html>", { status: 200 }),
    );

    show();

    await waitFor(() => expect(frame()).not.toBeNull());
    expect(screen.getByText("Documentation")).toBeInTheDocument();
  });
});
