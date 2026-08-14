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
import { afterEach, describe, expect, it, vi } from "vitest";
import { getBringupFleet } from "./bringupService";

function mockResponse(body: string, init: { status?: number; headers?: Record<string, string> } = {}) {
  const status = init.status ?? 200;
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
    headers: new Headers(init.headers || {}),
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("request shape", () => {
  it("sends the node list as a JSON body, never in the URL", async () => {
    /**
     * A cluster-sized list in a query string is a ~12 KB request line, which gunicorn rejects
     * with a bare 400 (limit_request_line = 4094). Bodies have no such limit.
     */
    const fetchMock = vi.fn(async () =>
      mockResponse(JSON.stringify({ ok: true, data: { nodes: [], columns: [] } })),
    );
    vi.stubGlobal("fetch", fetchMock);
    const nodes = Array.from({ length: 591 }, (_, i) => `ink50-dh01-h01-${i}`);
    await getBringupFleet(nodes);

    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe("/api/bringup/fleet");
    expect(url).not.toContain("nodes=");
    expect(init.method).toBe("POST");
    expect(JSON.parse(init.body as string).nodes).toHaveLength(591);
  });
});

describe("bring-up API error reporting", () => {
  it("returns the payload on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => mockResponse(JSON.stringify({ ok: true, data: { nodes: [], columns: [] } }))),
    );
    await expect(getBringupFleet()).resolves.toEqual({ nodes: [], columns: [] });
  });

  it("keeps the node list out of the error message", async () => {
    /** The first version echoed the whole URL back, producing a wall of 591 node names. */
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse("<html> <head></head></html>")));
    const nodes = Array.from({ length: 200 }, (_, i) => `ink50-dh01-h01-${i}`);
    await expect(getBringupFleet(nodes)).rejects.toThrow(
      /^\/api\/bringup\/fleet returned an HTML page/,
    );
  });

  it("explains an HTML response instead of leaking a JSON parse error", async () => {
    /**
     * The real failure this replaces: an /api/* path that the running backend does not know
     * falls through to the SPA catch-all and returns index.html, which surfaced to the operator
     * as `Unexpected token '<', "<html> <"... is not valid JSON`.
     */
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse("<html> <head></head></html>")));
    await expect(getBringupFleet()).rejects.toThrow(/HTML page instead of JSON/);
    await expect(getBringupFleet()).rejects.toThrow(/session expired|needs a restart/);
    await expect(getBringupFleet()).rejects.not.toThrow(/Unexpected token/);
  });

  it("reports the backend's own error message when it sends one", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        mockResponse(JSON.stringify({ ok: false, error: "bring-up integration disabled" }), { status: 404 }),
      ),
    );
    await expect(getBringupFleet()).rejects.toThrow("bring-up integration disabled");
  });

  it("includes a snippet when the body is neither JSON nor HTML", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => mockResponse("upstream connect error", { status: 502 })));
    await expect(getBringupFleet()).rejects.toThrow(/non-JSON response \(HTTP 502\): upstream connect error/);
  });
});
