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
import { describe, expect, it } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { REPORT } from "../__fixtures__/report";
import { readEmbeddedReport } from "../lib/embedded";
import { StaticTrackerPage } from "../static/StaticTrackerPage";
import { DeploymentTracker } from "./DeploymentTracker";

describe("DeploymentTracker", () => {
  it("shows the headline counts, with a handed-over tile only when there is one", () => {
    render(<DeploymentTracker report={REPORT} />);
    expect(screen.getByTestId("kpi-total")).toHaveTextContent("5");
    expect(screen.getByTestId("kpi-remediation")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-handed_over")).toHaveTextContent("1");
    expect(screen.getByTestId("kpi-percent")).toHaveTextContent("40%");

    const none = { ...REPORT, summary: { ...REPORT.summary, counts: { ...REPORT.summary.counts, handed_over: 0 } } };
    render(<DeploymentTracker report={none} />);
    expect(screen.getAllByTestId("kpi-handed_over")).toHaveLength(1);
  });

  it("draws one tile per node and filters them by state and search", () => {
    const { container } = render(<DeploymentTracker report={REPORT} />);
    expect(container.querySelectorAll("[data-node]")).toHaveLength(5);
    expect(screen.getByTestId("heatmap-shown")).toHaveTextContent("5 nodes shown");

    fireEvent.change(screen.getByLabelText("Search nodes"), { target: { value: "fabric" } });
    expect(screen.getByTestId("heatmap-shown")).toHaveTextContent("1 nodes shown");
    // A filtered-out tile is dimmed, not removed, so the floor keeps its shape.
    expect(container.querySelectorAll("[data-node]")).toHaveLength(5);
    const dim = container.querySelector('[data-node="gpu1-su20"]') as HTMLElement;
    expect(dim.style.opacity).toBe("0.15");

    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(screen.getByTestId("heatmap-shown")).toHaveTextContent("5 nodes shown");
  });

  it("explains a hovered tile in one line, reason included", () => {
    const { container } = render(<DeploymentTracker report={REPORT} />);
    fireEvent.mouseOver(container.querySelector('[data-node="gpu3-su20"]')!);
    expect(screen.getByText(/gpu3-su20 · rack V21 · In remediation/)).toHaveTextContent("fabric not up");
  });

  it("opens the table from scalable unit to row to rack", () => {
    render(<DeploymentTracker report={REPORT} />);
    const table = screen.getByRole("table", { name: /Node detail/ });
    // The first SU starts open, showing its rows but not their racks.
    expect(within(table).getByText("Row A")).toBeInTheDocument();
    expect(within(table).queryByText("V20")).not.toBeInTheDocument();
    fireEvent.click(within(table).getByRole("button", { name: "Expand Row A" }));
    expect(within(table).getByText("V20")).toBeInTheDocument();
    fireEvent.click(within(table).getByRole("button", { name: "Collapse SU20" }));
    expect(within(table).queryByText("Row A")).not.toBeInTheDocument();
  });

  it("cards an SU's rows, or its racks when it sits in one row", () => {
    render(<DeploymentTracker report={REPORT} />);
    const units = screen.getByText("Scalable units & rows").closest("div")!.parentElement!;
    // SU20 spans rows A and B; SU19 is one row, so its rack U19 gets the card.
    expect(within(units).getAllByText("Row A").length).toBeGreaterThan(0);
    expect(within(units).getByText("U19")).toBeInTheDocument();
    expect(within(units).getByText("Rack S1 done · S2 done")).toBeInTheDocument();
  });

  it("prints the backend's definitions, not its own", () => {
    render(<DeploymentTracker report={REPORT} />);
    expect(screen.getByText("Both stages passed.")).toBeInTheDocument();
  });

  it("names the hosts it left out instead of dropping them silently", () => {
    render(<DeploymentTracker report={REPORT} />);
    expect(screen.getByTestId("tracker-unplaced")).toHaveTextContent("appserver-nc");
  });
});

describe("the static page", () => {
  function docWith(text: string | null): Document {
    const doc = document.implementation.createHTMLDocument("t");
    if (text !== null) {
      const el = doc.createElement("script");
      el.id = "tracker-data";
      el.type = "application/json";
      el.textContent = text;
      doc.body.appendChild(el);
    }
    return doc;
  }

  it("reads the dataset the backend embedded", () => {
    expect(readEmbeddedReport(docWith(JSON.stringify(REPORT)))?.meta.cluster).toBe("Nscale NC");
  });

  it("treats a bare bundle, bad JSON or a foreign shape as no data", () => {
    expect(readEmbeddedReport(docWith("null"))).toBeNull();
    expect(readEmbeddedReport(docWith("{not json"))).toBeNull();
    expect(readEmbeddedReport(docWith('{"ok":true}'))).toBeNull();
    expect(readEmbeddedReport(docWith(null))).toBeNull();
  });

  it("says how to get data when it has none, and shows the snapshot time when it has", () => {
    const { unmount } = render(<StaticTrackerPage report={null} />);
    expect(screen.getByText("No tracker data in this page")).toBeInTheDocument();
    unmount();
    render(<StaticTrackerPage report={REPORT} />);
    expect(screen.getByText("Snapshot 2026-10-13 12:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("Nscale NC · Deployment Tracker")).toBeInTheDocument();
  });
});
