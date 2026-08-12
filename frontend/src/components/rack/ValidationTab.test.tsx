import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { ValidationTab } from "./ValidationTab";

vi.mock("../../context/RackCommandContext", () => ({
  useRackCommands: () => ({
    runAsyncValidationFamily: vi.fn(),
    launchAsyncValidationFamily: vi.fn(),
    runSyncJson: vi.fn(),
    openText: vi.fn(),
  }),
}));
vi.mock("./NvlinkPreflightPanel", () => ({ NvlinkPreflightPanel: () => null }));
vi.mock("./ValidationActivitiesTable", () => ({ ValidationActivitiesTable: () => null }));

describe("ValidationTab pinned recipes", () => {
  it("labels the proven Llama run as fixed at 50 steps and shows exact image inventory", () => {
    render(
      <ValidationTab
        rack="d01"
        selectedNodes={new Set(["d01-01"])}
        disabled={false}
      />,
    );

    expect(screen.getByText("Llama 3.1 405B FP8 — 1024 GPUs / 50 Steps")).toBeInTheDocument();
    expect(screen.getByText(/256-node \/ 1024-GPU multirack validation/i)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Multirack Only" })).toBeDisabled();
    expect(screen.getByText("NeMo 26.04.01 Validation")).toBeInTheDocument();
    expect(screen.getByText("MNUberGEMM 26.03-v1.17 Validation")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Plan GPT-OSS" })).toBeEnabled();
  });

  it("does not permit the single-node GPT-OSS overhead benchmark on multiple nodes", () => {
    render(
      <ValidationTab
        rack="d01"
        selectedNodes={new Set(["d01-01", "d01-02"])}
        disabled={false}
      />,
    );

    expect(screen.getByRole("button", { name: "Plan GPT-OSS" })).toBeDisabled();
    expect(screen.getByText(/exactly one selected node/i)).toBeInTheDocument();
  });
});
