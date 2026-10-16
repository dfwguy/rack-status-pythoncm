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
// The node page must show Rack Status's own Stage 1 verdict, not leave the bring-up automation's
// failed allreduce as the only NCCL result in view (gpu63-su20: Rack Status Stage 1 passed in Slurm
// 207363 while bring-up's allreduce read FAIL).
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchNodeStages } from "../../services/nodeStagesService";
import { stageStateLabel } from "../../lib/validationStages";
import { NodeValidationStages } from "./NodeValidationStages";

vi.mock("../../services/nodeStagesService", () => ({ fetchNodeStages: vi.fn() }));
const fetchMock = fetchNodeStages as unknown as ReturnType<typeof vi.fn>;
afterEach(() => vi.clearAllMocks());

const NODE = "sys1-gso1-p-phy-gpu63-su20";

describe("NodeValidationStages", () => {
  it("shows the Rack Status Stage 1 run that decided the node, with its job and log", async () => {
    fetchMock.mockResolvedValue({
      ok: true, stage2_source: "bringup:llm-pretrain-solo", stage2_loading: false,
      nodes: [{
        node: NODE, rack: "U5",
        stage1: { state: "done", job_id: "207363", log: "/home/cmods/nvis/logs/cmods-NCCL-1Rack-U5-1N-207363.txt", source: "rack-status" },
        stage2: { state: "done", status: "DONE" },
      }],
    });
    render(<NodeValidationStages node={NODE} logDir="/home/cmods/nvis/logs" />);
    expect(await screen.findByText(/via Rack Status run · Slurm 207363 · \/home\/cmods\/nvis\/logs\/cmods-NCCL-1Rack-U5-1N-207363\.txt/)).toBeInTheDocument();
    expect(screen.getAllByText(stageStateLabel("done"))).toHaveLength(2);
    expect(screen.getByText(/Stage 2 — single-node LLM \(bringup:llm-pretrain-solo\)/)).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith({ node: NODE, logDir: "/home/cmods/nvis/logs" });
  });

  it("says why Stage 1 failed when the result log names it", async () => {
    fetchMock.mockResolvedValue({
      ok: true, stage2_source: "", stage2_loading: false,
      nodes: [{ node: NODE, rack: "U5", stage1: { state: "fail", job_id: "1", reason: "wrong results" }, stage2: { state: "pending" } }],
    });
    render(<NodeValidationStages node={NODE} />);
    expect(await screen.findByText(/Slurm 1 · wrong results/)).toBeInTheDocument();
    expect(screen.getByText(stageStateLabel("fail"))).toBeInTheDocument();
  });

  it("reports a failed lookup instead of showing an empty verdict", async () => {
    fetchMock.mockRejectedValue(new Error("HTTP 503"));
    render(<NodeValidationStages node={NODE} />);
    expect(await screen.findByText("HTTP 503")).toBeInTheDocument();
  });

  it("says when bring-up's run of the same check is what passed Stage 1", async () => {
    fetchMock.mockResolvedValue({
      ok: true, stage2_source: "bringup:llm-pretrain-solo", stage2_loading: false,
      nodes: [{ node: NODE, rack: "U5", stage1: { state: "done", status: "DONE", source: "bringup:allreduce" }, stage2: { state: "done" } }],
    });
    render(<NodeValidationStages node={NODE} />);
    expect(await screen.findByText("via bring-up allreduce")).toBeInTheDocument();
  });
});
