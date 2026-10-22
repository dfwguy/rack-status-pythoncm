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
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunComposer } from "./RunComposer";
import { RunDetails } from "./RunDetails";
import { RunHistory } from "./RunHistory";
import { functionalStatus, nodeAvailability } from "./model";
import * as service from "./service";
import {
  completed,
  inventory,
  nodes,
  preview,
  readiness,
} from "./testing/fixtures";

vi.mock("./service", () => ({
  confirmRun: vi.fn(),
  previewRun: vi.fn(),
  getArtifacts: vi.fn(),
  downloadArtifact: vi.fn(),
  getHistory: vi.fn(),
}));
beforeEach(() => {
  vi.mocked(service.previewRun).mockResolvedValue(structuredClone(preview));
  vi.mocked(service.confirmRun).mockResolvedValue(completed);
  vi.mocked(service.getArtifacts).mockResolvedValue([
    { name: "summary.json", size: 1024 },
  ]);
  vi.mocked(service.getHistory).mockResolvedValue({
    runs: [completed.activity],
    next_cursor: null,
  });
});
afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  vi.restoreAllMocks();
});
function selectPair() {
  fireEvent.click(screen.getByRole("checkbox", { name: `Select ${nodes[0]}` }));
  fireEvent.click(screen.getByRole("checkbox", { name: `Select ${nodes[1]}` }));
}
async function reviewPair() {
  selectPair();
  fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
  await screen.findByRole("button", { name: "Confirm & run" });
}

describe("evidence and inventory semantics", () => {
  it.each(["IDLE*", "IDLE+DRAIN", "DOWN", "RESERVED", "UNKNOWN"])(
    "does not treat %s as idle",
    (state) => {
      expect(
        nodeAvailability(
          { node: "node", states: [state], partitions: ["validation"] },
          "validation",
        ),
      ).toBe("unavailable");
    },
  );
  it("separates busy nodes and wrong partitions", () => {
    expect(
      nodeAvailability(
        { node: "node", states: ["ALLOCATED"], partitions: ["validation"] },
        "validation",
      ),
    ).toBe("busy");
    expect(nodeAvailability(inventory.nodes[0], "other")).toBe("unavailable");
  });
  it("requires scheduler completion and valid evidence before PASS", () => {
    expect(functionalStatus(completed)).toBe("PASS");
    expect(
      functionalStatus({
        ...completed,
        phase: "error",
        scheduler_state: "FAILED",
      }),
    ).toBe("FAIL");
    expect(functionalStatus({ ...completed, phase: "running" })).toBe(
      "PENDING",
    );
    expect(
      functionalStatus({ ...completed, summary_error: "identity mismatch" }),
    ).toBe("INVALID EVIDENCE");
  });
});
describe("preview and confirmation", () => {
  it("only previews until a separate confirmation; rapid duplicate clicks submit once", async () => {
    const onSubmit = vi.fn((_id: string, submit: () => Promise<unknown>) => { void submit(); });
    render(
      <RunComposer
        readiness={readiness}
        inventory={inventory}
        onSubmit={onSubmit}
      />,
    );
    await reviewPair();
    expect(screen.getByRole("button", { name: "Preview run" })).toBeEnabled();
    expect(service.confirmRun).not.toHaveBeenCalled();
    expect(service.previewRun).toHaveBeenCalledWith(
      {
        scope: "explicit",
        node_count: 2,
        nodes: nodes.slice(0, 2),
        path_comparison: true,
      },
      expect.any(AbortSignal),
    );
    const confirm = screen.getByRole("button", { name: "Confirm & run" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect(service.confirmRun).toHaveBeenCalledTimes(1);
    expect(onSubmit).toHaveBeenCalledWith(
      preview.activity_id,
      expect.any(Function),
    );
  });
  it("invalidates an existing preview when selection changes", async () => {
    render(
      <RunComposer
        readiness={readiness}
        inventory={inventory}
        onSubmit={vi.fn()}
      />,
    );
    await reviewPair();
    fireEvent.click(
      screen.getByRole("checkbox", { name: `Select ${nodes[0]}` }),
    );
    expect(
      screen.queryByRole("button", { name: "Confirm & run" }),
    ).not.toBeInTheDocument();
  });
  it("discards a late preview after configuration changes", async () => {
    let resolve!: (value: typeof preview) => void;
    vi.mocked(service.previewRun).mockReturnValueOnce(
      new Promise((r) => {
        resolve = r;
      }),
    );
    render(
      <RunComposer
        readiness={readiness}
        inventory={inventory}
        onSubmit={vi.fn()}
      />,
    );
    selectPair();
    fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
    fireEvent.click(
      screen.getByRole("checkbox", { name: `Select ${nodes[0]}` }),
    );
    await act(async () => {
      resolve(preview);
    });
    expect(
      screen.queryByRole("button", { name: "Confirm & run" }),
    ).not.toBeInTheDocument();
  });
  it("refuses an expired token even before the countdown renders again", async () => {
    render(
      <RunComposer
        readiness={readiness}
        inventory={inventory}
        onSubmit={vi.fn()}
      />,
    );
    await reviewPair();
    const old = Date.now();
    vi.spyOn(Date, "now").mockReturnValue(old + 301000);
    fireEvent.click(screen.getByRole("button", { name: "Confirm & run" }));
    expect(service.confirmRun).not.toHaveBeenCalled();
  });
  it("gates an unsupported execution context", () => {
    render(
      <RunComposer
        readiness={{ ...readiness, execution_context_ready: false }}
        inventory={inventory}
        onSubmit={vi.fn()}
      />,
    );
    selectPair();
    expect(screen.getByRole("button", { name: "Preview run" })).toBeDisabled();
  });
  it("surfaces preview failures without launching work", async () => {
    vi.mocked(service.previewRun).mockRejectedValueOnce(
      new Error("Node was drained"),
    );
    render(
      <RunComposer
        readiness={readiness}
        inventory={inventory}
        onSubmit={vi.fn()}
      />,
    );
    selectPair();
    fireEvent.click(screen.getByRole("button", { name: "Preview run" }));
    await screen.findByText("Node was drained");
    expect(service.confirmRun).not.toHaveBeenCalled();
  });
});
describe("results and attempts", () => {
  it("shows functional PASS alongside performance UNVERIFIED and per-mode coverage", () => {
    render(
      <RunDetails
        run={completed}
        activityId={completed.activity_id}
        error=""
        submitting={false}
        refresh={vi.fn()}
      />,
    );
    expect(screen.getByText("PASS")).toBeInTheDocument();
    expect(screen.getByText("UNVERIFIED")).toBeInTheDocument();
    expect(screen.getAllByText("28 / 28")).toHaveLength(4);
    expect(screen.getAllByText(/MB\/s/).length).toBeGreaterThan(2);
  });
  it("does not render invalid evidence as a successful result", () => {
    render(
      <RunDetails
        run={{ ...completed, summary_error: "Wrong attempt identity" }}
        activityId={completed.activity_id}
        error=""
        submitting={false}
        refresh={vi.fn()}
      />,
    );
    expect(screen.getByText("INVALID EVIDENCE")).toBeInTheDocument();
    expect(screen.queryByText("PASS")).not.toBeInTheDocument();
  });
  it("downloads artifacts only after opening the artifact view", async () => {
    render(
      <RunDetails
        run={completed}
        activityId={completed.activity_id}
        error=""
        submitting={false}
        refresh={vi.fn()}
      />,
    );
    expect(service.getArtifacts).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("Artifacts"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Download summary.json" }),
    );
    await waitFor(() =>
      expect(service.downloadArtifact).toHaveBeenCalledWith(
        completed.activity_id,
        "summary.json",
      ),
    );
  });
  it("preserves a failed attempt in history", async () => {
    vi.mocked(service.getHistory).mockResolvedValueOnce({
      runs: [{ ...completed.activity, phase: "error", stage: "preflight" }],
      next_cursor: null,
    });
    const onOpen = vi.fn();
    render(<RunHistory onOpen={onOpen} />);
    await screen.findByText("error");
    fireEvent.click(
      screen.getByRole("button", { name: completed.activity_id.slice(0, 8) }),
    );
    expect(onOpen).toHaveBeenCalledWith(completed.activity_id);
  });
  it("distinguishes a failed history request from empty history", async () => {
    vi.mocked(service.getHistory).mockRejectedValueOnce(
      new Error("History unavailable"),
    );
    render(<RunHistory onOpen={vi.fn()} />);
    await screen.findByText(/History unavailable/);
    expect(
      screen.queryByText("No attempts in this execution context"),
    ).not.toBeInTheDocument();
  });
});
