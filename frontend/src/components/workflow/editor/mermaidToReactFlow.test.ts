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
});
