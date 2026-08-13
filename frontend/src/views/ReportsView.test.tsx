import { render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ReportsView } from "./ReportsView";

afterEach(() => vi.restoreAllMocks());

describe("ReportsView generated report archive", () => {
  it("shows saved fleet reports alongside the existing bug-report inventory", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      if (url === "/api/reports/generated") {
        return new Response(JSON.stringify({
          ok: true,
          max_kept: 40,
          reports: [{
            id: "a".repeat(32),
            kind: "rack-status",
            format: "pdf",
            filename: "rack-status-NscaleTX.pdf",
            size: 2_097_152,
            created: 1_786_608_000,
            cluster: "NscaleTX-GB300",
            nodes: 2016,
          }],
        }), { status: 200, headers: { "Content-Type": "application/json" } });
      }
      if (url === "/api/bug-report-inventory") {
        return new Response(JSON.stringify({ ok: true, base_dir: "/reports", items: [] }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      throw new Error(`unexpected request: ${url}`);
    });

    render(<ReportsView />);

    const table = await screen.findByRole("table", { name: "Generated reports" });
    expect(within(table).getByText("rack-status-NscaleTX.pdf")).toBeInTheDocument();
    expect(within(table).getByText("NscaleTX-GB300 · 2016 nodes")).toBeInTheDocument();
    expect(within(table).getByText("2.0 MiB")).toBeInTheDocument();
    expect(screen.getByText(/newest 40 reports are retained/i)).toBeInTheDocument();
  });
});
