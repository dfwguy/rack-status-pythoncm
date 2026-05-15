import { describe, it, expect } from "vitest";
import {
  parseFrontmatter,
  serializeFrontmatter,
  getNodeMetadata,
  setNodeMetadata,
  removeNodeMetadata,
} from "./frontmatter";

describe("parseFrontmatter", () => {
  it("returns empty metadata when no frontmatter", () => {
    const code = "graph TD\n    A --> B";
    const { metadata, mermaidCode } = parseFrontmatter(code);
    expect(metadata).toEqual({});
    expect(mermaidCode).toBe(code);
  });

  it("parses metadata with all fields", () => {
    const code = `---
metadata:
  check_status:
    name: "Check Status"
    description: "Verify nodes"
    type: automated
    command: "/api/status"
    parameters:
      rack: "p34-r07"
      timeout: "30"
    output: "result"
    timeout: 120
    retries: 3
    on_success: next_step
    on_failure: error_step
---
graph TD
    check_status["Check Status"] --> next_step`;

    const { metadata, mermaidCode } = parseFrontmatter(code);
    expect(metadata.check_status).toBeDefined();
    expect(metadata.check_status.name).toBe("Check Status");
    expect(metadata.check_status.description).toBe("Verify nodes");
    expect(metadata.check_status.type).toBe("automated");
    expect(metadata.check_status.command).toBe("/api/status");
    expect(metadata.check_status.parameters).toEqual({ rack: "p34-r07", timeout: "30" });
    expect(metadata.check_status.output).toBe("result");
    expect(metadata.check_status.timeout).toBe(120);
    expect(metadata.check_status.retries).toBe(3);
    expect(metadata.check_status.on_success).toBe("next_step");
    expect(metadata.check_status.on_failure).toBe("error_step");
    expect(mermaidCode).toContain("graph TD");
  });

  it("handles multiple nodes in metadata", () => {
    const code = `---
metadata:
  step1:
    name: "Step 1"
    type: automated
  step2:
    name: "Step 2"
    type: manual
---
graph TD
    step1 --> step2`;

    const { metadata } = parseFrontmatter(code);
    expect(Object.keys(metadata)).toHaveLength(2);
    expect(metadata.step1.type).toBe("automated");
    expect(metadata.step2.type).toBe("manual");
  });
});

describe("serializeFrontmatter", () => {
  it("returns just mermaid code when metadata is empty", () => {
    const result = serializeFrontmatter({}, "graph TD\n    A --> B");
    expect(result).toBe("graph TD\n    A --> B");
    expect(result).not.toContain("---");
  });

  it("serializes metadata with frontmatter delimiters", () => {
    const metadata = {
      step1: { name: "Test", type: "automated" as const, timeout: 60 },
    };
    const result = serializeFrontmatter(metadata, "graph TD\n    step1");
    expect(result).toContain("---");
    expect(result).toContain("metadata:");
    expect(result).toContain("step1:");
    expect(result).toContain('name: "Test"');
    expect(result).toContain("type: automated");
    expect(result).toContain("timeout: 60");
    expect(result).toContain("graph TD");
  });

  it("round-trips correctly", () => {
    const metadata = {
      node1: { name: "Node One", type: "decision" as const, command: "host:test" },
    };
    const mermaid = "graph TD\n    node1 --> done";
    const serialized = serializeFrontmatter(metadata, mermaid);
    const { metadata: parsed, mermaidCode } = parseFrontmatter(serialized);
    expect(parsed.node1.name).toBe("Node One");
    expect(parsed.node1.type).toBe("decision");
    expect(parsed.node1.command).toBe("host:test");
    expect(mermaidCode).toContain("graph TD");
  });
});

describe("getNodeMetadata", () => {
  it("returns empty object for missing node", () => {
    const result = getNodeMetadata({}, "missing");
    expect(result).toEqual({});
  });

  it("returns node metadata when present", () => {
    const metadata = { step1: { name: "Test", type: "automated" as const } };
    const result = getNodeMetadata(metadata, "step1");
    expect(result.name).toBe("Test");
  });
});

describe("setNodeMetadata", () => {
  it("creates new entry", () => {
    const result = setNodeMetadata({}, "new_node", { name: "New", type: "manual" });
    expect(result.new_node.name).toBe("New");
    expect(result.new_node.type).toBe("manual");
  });

  it("merges with existing", () => {
    const existing = { step1: { name: "Old", type: "automated" as const } };
    const result = setNodeMetadata(existing, "step1", { command: "/api/test" });
    expect(result.step1.name).toBe("Old");
    expect(result.step1.command).toBe("/api/test");
  });
});

describe("removeNodeMetadata", () => {
  it("removes a node entry", () => {
    const metadata = { a: { name: "A" }, b: { name: "B" } };
    const result = removeNodeMetadata(metadata, "a");
    expect(result.a).toBeUndefined();
    expect(result.b).toBeDefined();
  });
});
