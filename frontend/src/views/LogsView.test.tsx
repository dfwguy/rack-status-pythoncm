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
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { LogsView } from "./LogsView";

afterEach(() => vi.restoreAllMocks());

describe("LogsView Grafana contract", () => {
  it("renders mapped section panels using the browser-facing Grafana origin", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation(async (input) => {
      const url = String(input);
      let body: object;
      if (url.startsWith("/api/status")) body = { ok: true, log_dir: "/logs", rows: [] };
      else if (url.startsWith("/api/multirack-status")) body = { ok: true, rows: [] };
      else if (url === "/api/observability/health") body = { ok: true, embedding_allowed: true };
      else if (url === "/api/observability/dashboards/logs") {
        body = {
          ok: true,
          config: { public_base: "", public_port: "3001" },
          sections: [{
            uid: "nscaletx-logs-events",
            dashboard_open_path: "/d/nscaletx-logs-events/_?from=now-1h",
            panels: [{ id: 10, title: "Selected tray incidents", url: "/d-solo/nscaletx-logs-events/_?panelId=10" }],
          }],
        };
      } else throw new Error(`unexpected request: ${url}`);
      return new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
    });

    render(<LogsView logDir="/logs" />);

    const frame = await screen.findByTitle("Selected tray incidents");
    expect(frame).toHaveAttribute("src", "http://localhost:3001/d-solo/nscaletx-logs-events/_?panelId=10");
    expect(screen.getByRole("link", { name: /open in grafana/i })).toHaveAttribute(
      "href",
      "http://localhost:3001/d/nscaletx-logs-events/_?from=now-1h",
    );
  });
});
