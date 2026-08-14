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
import { mermaidToReactFlow } from "./mermaidToReactFlow";

describe("mermaidToReactFlow", () => {
  it("parses a basic graph with nodes and edges", () => {
    const code = `graph TD
    A["Start"] --> B["End"]`;

    const { nodes, edges } = mermaidToReactFlow(code);
    expect(nodes).toHaveLength(2);
    expect(edges).toHaveLength(1);
    expect(nodes[0].id).toBe("A");
    expect(nodes[1].id).toBe("B");
    expect(edges[0].source).toBe("A");
    expect(edges[0].target).toBe("B");
  });

  it("parses a linear flowchart TD chain of automated steps ending in a done stadium", () => {
    // Mirrors the generated sysinfo-precheck workflow: underscore ids, spaced labels,
    // a --> chain, and a final done(["Done"]) stadium.
    const code = `flowchart TD
      bios["BIOS"] --> cpu_freq["CPU Frequency   1800 MHz"]
      cpu_freq["CPU Frequency   1800 MHz"] --> ib_devices["IB devices status  all active"]
      ib_devices["IB devices status  all active"] --> done(["Done"])`;

    const { nodes, edges } = mermaidToReactFlow(code);
    expect(nodes).toHaveLength(4);
    expect(edges).toHaveLength(3);
    const ids = nodes.map((n) => n.id);
    expect(ids).toEqual(expect.arrayContaining(["bios", "cpu_freq", "ib_devices", "done"]));
    const done = nodes.find((n) => n.id === "done");
    expect(done?.data.stepType).toBe("manual"); // stadium
  });

  it("recognizes diamond shape as decision type", () => {
    const code = `graph TD
    decide{"Is it raining?"}`;

    const { nodes } = mermaidToReactFlow(code);
    expect(nodes).toHaveLength(1);
    expect(nodes[0].type).toBe("diamond");
    expect(nodes[0].data.stepType).toBe("decision");
  });

  it("recognizes stadium shape as manual type", () => {
    const code = `graph TD
    manual(["Manual Step"])`;

    const { nodes } = mermaidToReactFlow(code);
    expect(nodes).toHaveLength(1);
    expect(nodes[0].data.stepType).toBe("manual");
  });

  it("parses edge labels", () => {
    const code = `graph TD
    A["Start"] -->|"yes"| B["End"]`;

    const { edges } = mermaidToReactFlow(code);
    expect(edges[0].label).toBe("yes");
  });

  it("strips YAML frontmatter before parsing", () => {
    const code = `---
metadata:
  step1:
    name: "Test"
    type: automated
---
graph TD
    step1["Test"] --> done(["Done"])`;

    const { nodes, edges } = mermaidToReactFlow(code);
    expect(nodes).toHaveLength(2);
    expect(edges).toHaveLength(1);
    expect(nodes[0].id).toBe("step1");
  });

  it("throws on unrecognized syntax lines", () => {
    const code = `graph TD
    A["Start"] --> B["End"]
    invalid==broken`;

    expect(() => mermaidToReactFlow(code)).toThrow("Unrecognized syntax");
  });

  it("throws on missing graph/flowchart directive", () => {
    const code = `A["Start"] --> B["End"]`;
    expect(() => mermaidToReactFlow(code)).toThrow("Missing diagram directive");
  });

  it("returns empty for empty input", () => {
    const { nodes, edges } = mermaidToReactFlow("");
    expect(nodes).toHaveLength(0);
    expect(edges).toHaveLength(0);
  });

  it("handles flowchart directive", () => {
    const code = `flowchart TD
    X["Node"] --> Y["Other"]`;

    const { nodes } = mermaidToReactFlow(code);
    expect(nodes).toHaveLength(2);
  });

  it("applies stepTypes override", () => {
    const code = `graph TD
    myStep["Custom"]`;

    const { nodes } = mermaidToReactFlow(code, { myStep: "delay" });
    expect(nodes[0].data.stepType).toBe("delay");
  });

  it("parses new step connected to done after user adds node and edges", () => {
    const code = `graph TD
    start["Start"] --> step1["Step 1"]
    step1 --> done(["Done"])
    step1 --> step_1
    step_1["New Step"]
    step_1 --> done`;

    const { nodes, edges } = mermaidToReactFlow(code);
    const nodeIds = nodes.map((n) => n.id);
    expect(nodeIds).toContain("step_1");
    expect(nodeIds).toContain("done");
    const edgeTargets = edges.filter((e) => e.target === "done");
    expect(edgeTargets.length).toBeGreaterThanOrEqual(2);
    const step1ToDone = edges.find((e) => e.source === "step_1" && e.target === "done");
    expect(step1ToDone).toBeDefined();
  });

  it("uses full label even when node definition appears after edge reference", () => {
    const code = `graph TD
    start["Start"] --> step1["Step 1"]
    step1 --> done(["Done"])
    step1 --> step_1
    step_1["New Step"]`;

    const { nodes } = mermaidToReactFlow(code);
    const step1Node = nodes.find((n) => n.id === "step_1");
    expect(step1Node).toBeDefined();
    expect(step1Node!.data.label).toBe("New Step");
    expect(step1Node!.data.shape).toBe("rect");
  });
});
