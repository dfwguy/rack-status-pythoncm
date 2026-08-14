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
import { describe, it, expect } from "vitest";
import { serializeFrontmatter, parseFrontmatter, type WorkflowMetadata } from "./frontmatter";

describe("frontmatter composition round-trip", () => {
  it("preserves connection/scheduler/evaluation/capability/on_complete (no strip)", () => {
    const meta: WorkflowMetadata = {
      nccl: {
        name: "NCCL",
        type: "automated",
        capability: "sysinfo",
        on_complete: "done",
        connection: { profile: "script", sudo: true },
        scheduler: {
          kind: "slurm",
          script: "nccl_allreduce.sbatch",
          nodelist: "${target}",
          ntasks_per_node: "4",
        },
        evaluation: {
          success_when: { exit_code: 0 },
          extract: [{ name: "bw", regex: "busbw ([0-9.]+)", type: "float" }],
          assert: ["bw >= 300"],
        },
      },
    };
    const code = serializeFrontmatter(meta, "flowchart TD\n  nccl --> done");
    const { metadata, mermaidCode } = parseFrontmatter(code);

    expect(metadata.nccl.connection).toEqual({ profile: "script", sudo: true });
    expect(metadata.nccl.scheduler).toEqual({
      kind: "slurm",
      script: "nccl_allreduce.sbatch",
      nodelist: "${target}",
      ntasks_per_node: "4",
    });
    expect(metadata.nccl.evaluation).toEqual(meta.nccl.evaluation);
    expect(metadata.nccl.capability).toBe("sysinfo");
    expect(metadata.nccl.on_complete).toBe("done");
    expect(mermaidCode).toContain("nccl --> done");
  });

  it("round-trips a fanout block (N-level hops + per-node hop)", () => {
    const meta: WorkflowMetadata = {
      chk: {
        name: "IB", type: "automated", run: "ibstatus",
        fanout: {
          nodes: "${nodelist}",
          max_workers: 32,
          hops: [{ kind: "ssh", host: "${head}", user: "cmods" }],
          node: { kind: "ssh", user: "cmods", sudo: true },
        },
      },
    };
    const { metadata } = parseFrontmatter(serializeFrontmatter(meta, "graph TD\n chk --> done"));
    expect(metadata.chk.fanout).toEqual(meta.chk.fanout);
  });

  it("round-trips a resolve block", () => {
    const meta: WorkflowMetadata = {
      resolve: { name: "Resolve", type: "automated", resolve: { racks: "${rack}", into: "nodelist" } },
    };
    const { metadata } = parseFrontmatter(serializeFrontmatter(meta, "graph TD\n resolve --> done"));
    expect(metadata.resolve.resolve).toEqual({ racks: "${rack}", into: "nodelist" });
  });

  it("round-trips a multi-line run script", () => {
    const script = "export X=1\npdsh -w ${nodelist} <<'EOF' | dshbak -c\nnvidia-smi -L\nEOF\nexit 0";
    const meta: WorkflowMetadata = { chk: { name: "Check", type: "automated", run: script } };
    const { metadata } = parseFrontmatter(serializeFrontmatter(meta, "graph TD\n chk --> done"));
    expect(metadata.chk.run).toBe(script);
  });

  it("round-trips classic fields without regression", () => {
    const meta: WorkflowMetadata = {
      s1: { name: "S1", type: "manual", command: "host:echo hi", timeout: 30, on_success: "done" },
    };
    const { metadata } = parseFrontmatter(serializeFrontmatter(meta, "graph TD\n s1 --> done"));
    expect(metadata.s1.command).toBe("host:echo hi");
    expect(metadata.s1.timeout).toBe(30);
    expect(metadata.s1.on_success).toBe("done");
  });

  it("omits empty composition blocks", () => {
    const meta: WorkflowMetadata = { s1: { name: "S1", connection: {}, scheduler: {}, options: [] } };
    const code = serializeFrontmatter(meta, "graph TD\n s1");
    expect(code).not.toContain("connection:");
    expect(code).not.toContain("scheduler:");
  });
});
