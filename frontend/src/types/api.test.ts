import { describe, it, expect } from "vitest";
import type { RackRow, RackSummary } from "./api";

describe("API types", () => {
  it("RackRow has expected fields", () => {
    const row: RackRow = {
      rack: "p5-r01",
      nemo: "done",
      last_job: "12345",
      sinfo: "idle=8",
      ct_down_unk: "UP:p5-r01-ct[01-08]",
    };
    expect(row.rack).toBe("p5-r01");
    expect(row.nemo).toBe("done");
  });

  it("RackSummary fields add up", () => {
    const summary: RackSummary = { done: 10, fail: 2, ongoing: 3, pending: 3 };
    const total = summary.done + summary.fail + summary.ongoing + summary.pending;
    expect(total).toBe(18);
  });
});
