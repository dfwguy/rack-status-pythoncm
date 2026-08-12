import { afterEach, describe, expect, it, vi } from "vitest";
import { fetchValidationActivities } from "./validationActivitiesService";

afterEach(() => vi.restoreAllMocks());

describe("fetchValidationActivities", () => {
  it("surfaces an API error instead of presenting it as an empty activity list", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: false, error: "activity store unavailable" }), {
        status: 503,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(fetchValidationActivities({ logDir: "/cluster/logs" })).rejects.toThrow(
      "activity store unavailable",
    );
  });

  it("returns a valid empty list without treating it as an error", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ ok: true, activities: [] }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );

    await expect(fetchValidationActivities({ logDir: "/cluster/logs" })).resolves.toEqual([]);
  });
});
