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
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// The subject is the two things that make a rack control different from a board widget: the
// node confirmation, and the fact that these actions are destructive. Giving the card a
// clickable surface is new, and on a card that drains nodes it is the change most worth
// pinning — a card-sized target that runs the action instead of explaining it is the kind of
// mistake that only shows up on a customer cluster.
import { fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ActionCard } from "./ActionCard";
import { ActionCardGrid } from "../widgets/ActionCardGrid";

const LONG =
  "Cold-reset the BMC (rf0) on the selected nodes via ipmitool. Takes up to two minutes per " +
  "node, and the host stays up throughout — only the management controller is restarted.";

describe("a rack control as a card", () => {
  it("shows the catalog-style badge and artwork for its category", () => {
    render(<ActionCard title="Power On" category="hardware" description="Power on the tray." />);
    expect(screen.getByText("hardware")).toBeTruthy();
    const art = document.querySelector(".nv-card-root img") as HTMLImageElement;
    expect(art).toBeTruthy();
    expect(art.src).toMatch(/hardware/);
    // Decorative: the badge and title already say what this is.
    expect(art.getAttribute("alt")).toBe("");
  });

  it("hands the media its size, in the orientation it is drawn in", () => {
    // KUI sizes `.nv-card-media` with `flex: 1` and a 400/234 aspect ratio. In a horizontal card
    // that gives the media half the row and then derives a ~410px height from that width, so a
    // full-width card ends up four times taller than its contents with the artwork stranded at
    // one end. index.css pins the wrapper, keyed off this class — if the class or the custom
    // property stops arriving, the preflight card silently goes back to being enormous.
    const { unmount } = render(
      <ActionCard title="NVLink Preflight" category="validation" description="x" orientation="horizontal" />,
    );
    const horizontal = document.querySelector(".nv-card-root") as HTMLElement;
    expect(horizontal.classList.contains("nvis-action-card")).toBe(true);
    expect(horizontal.classList.contains("nvis-action-card--horizontal")).toBe(true);
    // KUI's own layout class survives ours being added, rather than being replaced by it.
    expect(horizontal.classList.contains("nv-card-root--layout-horizontal")).toBe(true);
    expect(horizontal.style.getPropertyValue("--nvis-card-media-w")).toBe("132px");
    unmount();

    render(<ActionCard title="Power On" category="hardware" description="x" />);
    const vertical = document.querySelector(".nv-card-root") as HTMLElement;
    expect(vertical.classList.contains("nvis-action-card--vertical")).toBe(true);
    // A definite banner height, not KUI's aspect ratio. Both `.nv-card-media` and
    // `.nv-card-content` carry `flex: 1`, so an indeterminate banner leaves slack for the
    // content box to absorb — and since the content box is `align-items: flex-start`, that
    // slack appears as a void under the buttons. This property is what keeps it definite.
    expect(vertical.style.getPropertyValue("--nvis-card-media-h")).toBe("160px");
  });

  it("leaves out the subtitle line when there is no subtitle", () => {
    // Reserved with a minHeight until 160px of banner and a blank line per card added up to
    // noticeable dead space. The grid row already stretches cards to a common height, and
    // Customise mode's cells are fixed, so nothing needed the reservation.
    render(<ActionCard title="Tail Log" category="maintenance" description="Follow the log." />);
    const lines = document.querySelectorAll(".nv-card-content .nv-text");
    const texts = [...lines].map((n) => n.textContent);
    expect(texts).toContain("Tail Log");
    expect(texts).toContain("Follow the log.");
    expect(texts).not.toContain("");

    // ...and present when there is one.
    render(
      <ActionCard
        title="Preflight"
        category="validation"
        subtitle="17 node(s) · full mode"
        description="Gate before Stage 1."
      />,
    );
    expect(screen.getByText("17 node(s) · full mode")).toBeTruthy();
  });

  it("keeps the description to one line on the card and the whole of it in Details", () => {
    render(<ActionCard title="BMC Recovery" category="hardware" description={LONG} />);
    const line = screen.getByText(LONG);
    // Ellipsised rather than wrapped: a card whose height depends on how much prose it
    // carries makes a row of cards visibly ragged.
    expect(line.parentElement?.style.whiteSpace).toBe("nowrap");
    expect(line.parentElement?.style.textOverflow).toBe("ellipsis");

    fireEvent.click(screen.getByRole("button", { name: /Info/ }));
    // Twice over now: once on the card, once in the open dialog.
    expect(screen.getAllByText(LONG).length).toBeGreaterThan(1);
  });

  it("opens Details when the card is clicked, and does NOT run the action", () => {
    // The whole reason the card's click is wired to Details: these controls drain nodes and
    // reset BMCs, and a mis-click must cost nothing.
    const onRun = vi.fn();
    render(
      <ActionCard
        title="Drain Nodes"
        category="maintenance"
        description="Drain the selected nodes."
        destructive
        onRun={onRun}
      />,
    );
    fireEvent.click(document.querySelector(".nv-card-root") as HTMLElement);
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Close/ })).toBeTruthy();
  });

  it("runs from its own button without also opening Details", () => {
    // The other half of a clickable card: every control drawn on it has to stop the bubble.
    const onRun = vi.fn();
    render(
      <ActionCard
        title="Undrain Nodes"
        category="maintenance"
        description="Resume scheduling."
        buttonLabel="Undrain"
        onRun={onRun}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Undrain" }));
    expect(onRun).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole("button", { name: /Close/ })).toBeNull();
  });

  it("still names every node before a selected-node action runs", () => {
    // The selection lives in another pane, so "18 nodes" and "these 18 nodes" are not the
    // same sentence. This dialog is the only place the operator sees which.
    const onRun = vi.fn();
    render(
      <ActionCard
        title="Drain Nodes"
        category="maintenance"
        description="Drain the selected nodes."
        buttonLabel="Drain"
        destructive
        selectedNodes={new Set(["r1-n10", "r1-n2"])}
        onRun={onRun}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Drain" }));
    expect(onRun).not.toHaveBeenCalled();
    expect(screen.getByText(/following 2 node\(s\)/)).toBeTruthy();
    // Natural sort, so n2 precedes n10 as an operator reads it.
    expect(screen.getByText("r1-n2")).toBeTruthy();
    expect(screen.getByText("r1-n10")).toBeTruthy();
    expect(screen.getByText(/destructive operation/)).toBeTruthy();

    // Scoped to the dialog: the card's own Drain button is still on screen behind it, and
    // clicking that one would prove nothing about confirming.
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Drain" }));
    expect(onRun).toHaveBeenCalledTimes(1);
  });

  it("runs immediately when nothing is selected", () => {
    // Rack-level actions (Tail Log, View Parser Results) have no node list to confirm.
    const onRun = vi.fn();
    render(
      <ActionCard
        title="Tail Log"
        category="maintenance"
        description="Follow the log."
        buttonLabel="Tail Log"
        onRun={onRun}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Tail Log" }));
    expect(onRun).toHaveBeenCalledTimes(1);
  });

  it("reports a run in flight, and refuses to start a second one", () => {
    render(
      <ActionCard
        title="GPU Reset"
        category="hardware"
        description="Reset the GPUs."
        status="running"
        onRun={vi.fn()}
      />,
    );
    // Twice, deliberately: the tag over the artwork is visible while scanning a grid of
    // cards, and the button label is what you read when you go to press it again.
    expect(screen.getAllByText("Running...")).toHaveLength(2);
    expect((screen.getByRole("button", { name: /Running/ }) as HTMLButtonElement).disabled).toBe(
      true,
    );
    // KUI's own selected treatment, rather than a second visual language for "busy".
    expect(
      document.querySelector(".nv-card-root")?.classList.contains("nv-card-root--selected"),
    ).toBe(true);
  });
});

describe("the grid the controls sit in", () => {
  it("lets the browser size the columns, as the widget board does", () => {
    render(
      <ActionCardGrid>
        <ActionCard title="a" category="hardware" description="a" />
        <ActionCard title="b" category="hardware" description="b" />
      </ActionCardGrid>,
    );
    const grid = document.querySelector(".nv-grid") as HTMLElement;
    expect(grid).toBeTruthy();
    expect(grid.getAttribute("style")).toContain("minmax(340px");
    expect(document.querySelectorAll(".nv-card-root")).toHaveLength(2);
  });
});
