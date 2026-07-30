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

describe("bring-up API error reporting", () => {
  it("returns the payload on success", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => mockResponse(JSON.stringify({ ok: true, data: { nodes: [], columns: [] } }))),
    );
    await expect(getBringupFleet()).resolves.toEqual({ nodes: [], columns: [] });
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
