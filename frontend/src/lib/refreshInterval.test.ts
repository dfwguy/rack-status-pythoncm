import { beforeEach, describe, expect, it } from "vitest";
import {
  DEFAULT_REFRESH_MS,
  REFRESH_INTERVALS_MS,
  REFRESH_OFF,
  loadRefreshMs,
  normalizeRefreshMs,
  refreshIntervalLabel,
  saveRefreshMs,
} from "./refreshInterval";

const KEY = "test.refreshMs";

beforeEach(() => {
  localStorage.clear();
});

describe("refreshInterval", () => {
  it("offers Off / 1m / 5m / 15m / 30m and defaults to 5m", () => {
    expect(REFRESH_INTERVALS_MS.map(refreshIntervalLabel)).toEqual(["Off", "1m", "5m", "15m", "30m"]);
    expect(DEFAULT_REFRESH_MS).toBe(5 * 60_000);
    expect(REFRESH_OFF).toBe(0);
  });

  it("normalizes unsupported values to the default", () => {
    expect(normalizeRefreshMs(60_000)).toBe(60_000);
    expect(normalizeRefreshMs(0)).toBe(0); // Off is a real choice, not a fallback
    expect(normalizeRefreshMs(1234)).toBe(DEFAULT_REFRESH_MS);
    expect(normalizeRefreshMs(null)).toBe(DEFAULT_REFRESH_MS);
    expect(normalizeRefreshMs(Number.NaN)).toBe(DEFAULT_REFRESH_MS);
  });

  it("round-trips through storage and defaults when unset or corrupt", () => {
    expect(loadRefreshMs(KEY)).toBe(DEFAULT_REFRESH_MS);
    saveRefreshMs(KEY, 15 * 60_000);
    expect(loadRefreshMs(KEY)).toBe(15 * 60_000);
    saveRefreshMs(KEY, REFRESH_OFF);
    expect(loadRefreshMs(KEY)).toBe(REFRESH_OFF);
    localStorage.setItem(KEY, "not-a-number");
    expect(loadRefreshMs(KEY)).toBe(DEFAULT_REFRESH_MS);
  });
});
