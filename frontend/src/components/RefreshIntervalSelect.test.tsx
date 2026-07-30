import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { RefreshIntervalSelect } from "./RefreshIntervalSelect";
import { DEFAULT_REFRESH_MS, REFRESH_OFF } from "../lib/refreshInterval";

describe("RefreshIntervalSelect", () => {
  it("shows the current interval as its label", () => {
    render(<RefreshIntervalSelect valueMs={DEFAULT_REFRESH_MS} onChangeMs={vi.fn()} aria-label="Auto-refresh interval" />);
    expect(screen.getAllByText("5m").length).toBeGreaterThan(0);
  });

  it("shows Off when auto-refresh is disabled", () => {
    render(<RefreshIntervalSelect valueMs={REFRESH_OFF} onChangeMs={vi.fn()} aria-label="Auto-refresh interval" />);
    expect(screen.getAllByText("Off").length).toBeGreaterThan(0);
  });

  it("offers every supported interval", () => {
    render(<RefreshIntervalSelect valueMs={DEFAULT_REFRESH_MS} onChangeMs={vi.fn()} aria-label="Auto-refresh interval" />);
    for (const label of ["Off", "1m", "5m", "15m", "30m"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
  });
});
