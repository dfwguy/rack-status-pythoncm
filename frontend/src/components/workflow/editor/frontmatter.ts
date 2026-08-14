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
/**
 * YAML frontmatter utilities for workflow metadata.
 * Metadata is stored between --- delimiters at the top of the mermaid code.
 */

export interface NodeMetadata {
  name?: string;
  description?: string;
  type?: "automated" | "manual" | "decision" | "ai_query" | "delay";
  command?: string;
  capability?: string;
  method?: string;
  run?: string;
  parameters?: Record<string, string>;
  output?: string;
  timeout?: number;
  retries?: number;
  on_success?: string;
  on_failure?: string;
  on_complete?: string;
  // Composition axes + fields authored in YAML; stored as objects, serialized as
  // compact JSON in the (frontend-internal) frontmatter so they survive round-trips.
  connection?: Record<string, unknown>;
  scheduler?: Record<string, unknown>;
  evaluation?: Record<string, unknown>;
  fanout?: Record<string, unknown>;
  resolve?: Record<string, unknown>;
  body?: Record<string, unknown>;
  options?: unknown[];
}

// Keys whose values are stored as compact JSON (nested objects / arrays, and the
// multi-line ``run`` script — JSON keeps newlines/quotes intact on one line).
const JSON_KEYS = ["connection", "scheduler", "evaluation", "fanout", "resolve", "body", "options", "run"] as const;

export type WorkflowMetadata = Record<string, NodeMetadata>;

/**
 * Parse YAML frontmatter from the combined code string.
 * Returns the metadata object and the remaining mermaid code.
 */
export function parseFrontmatter(code: string): { metadata: WorkflowMetadata; mermaidCode: string } {
  const trimmed = code.trimStart();

  if (!trimmed.startsWith("---")) {
    return { metadata: {}, mermaidCode: code };
  }

  const endIdx = trimmed.indexOf("\n---", 3);
  if (endIdx === -1) {
    return { metadata: {}, mermaidCode: code };
  }

  const yamlBlock = trimmed.slice(4, endIdx);
  const mermaidCode = trimmed.slice(endIdx + 4).trimStart();

  const metadata = parseSimpleYaml(yamlBlock);
  return { metadata, mermaidCode };
}

/**
 * Serialize metadata and mermaid code back into a single string with frontmatter.
 */
export function serializeFrontmatter(metadata: WorkflowMetadata, mermaidCode: string): string {
  if (Object.keys(metadata).length === 0) {
    return mermaidCode;
  }

  const yamlLines = ["---", "metadata:"];

  for (const [nodeId, props] of Object.entries(metadata)) {
    yamlLines.push(`  ${nodeId}:`);
    if (props.name) yamlLines.push(`    name: "${props.name}"`);
    if (props.description) yamlLines.push(`    description: "${props.description}"`);
    if (props.type) yamlLines.push(`    type: ${props.type}`);
    if (props.command) yamlLines.push(`    command: "${props.command}"`);
    if (props.parameters && Object.keys(props.parameters).length > 0) {
      yamlLines.push("    parameters:");
      for (const [k, v] of Object.entries(props.parameters)) {
        yamlLines.push(`      ${k}: "${v}"`);
      }
    }
    if (props.capability) yamlLines.push(`    capability: ${props.capability}`);
    if (props.method) yamlLines.push(`    method: ${props.method}`);
    // Multi-line run script: single-quoted JSON string (newlines -> \n) so it stays
    // on one frontmatter line and round-trips via JSON.parse (see JSON_KEYS).
    if (props.run) yamlLines.push(`    run: '${JSON.stringify(props.run)}'`);
    if (props.output) yamlLines.push(`    output: "${props.output}"`);
    if (props.timeout !== undefined) yamlLines.push(`    timeout: ${props.timeout}`);
    if (props.retries !== undefined) yamlLines.push(`    retries: ${props.retries}`);
    if (props.on_success) yamlLines.push(`    on_success: ${props.on_success}`);
    if (props.on_failure) yamlLines.push(`    on_failure: ${props.on_failure}`);
    if (props.on_complete) yamlLines.push(`    on_complete: ${props.on_complete}`);
    for (const jkey of JSON_KEYS) {
      const v = (props as Record<string, unknown>)[jkey];
      const nonEmpty = Array.isArray(v) ? v.length > 0 : v && typeof v === "object" && Object.keys(v).length > 0;
      if (nonEmpty) yamlLines.push(`    ${jkey}: '${JSON.stringify(v)}'`);
    }
  }

  yamlLines.push("---");
  return yamlLines.join("\n") + "\n" + mermaidCode;
}

/**
 * Get metadata for a specific node.
 */
export function getNodeMetadata(metadata: WorkflowMetadata, nodeId: string): NodeMetadata {
  return metadata[nodeId] || {};
}

/**
 * Set metadata for a specific node (merges with existing).
 */
export function setNodeMetadata(
  metadata: WorkflowMetadata,
  nodeId: string,
  props: NodeMetadata,
): WorkflowMetadata {
  return {
    ...metadata,
    [nodeId]: { ...metadata[nodeId], ...props },
  };
}

/**
 * Remove metadata for a node.
 */
export function removeNodeMetadata(metadata: WorkflowMetadata, nodeId: string): WorkflowMetadata {
  const copy = { ...metadata };
  delete copy[nodeId];
  return copy;
}

/**
 * Simple YAML parser for the metadata block (handles our specific indented format).
 */
function parseSimpleYaml(yaml: string): WorkflowMetadata {
  const metadata: WorkflowMetadata = {};
  const lines = yaml.split("\n");
  let currentNodeId = "";
  let inParameters = false;

  for (const line of lines) {
    if (!line.trim() || line.trim() === "metadata:") continue;

    const indent = line.length - line.trimStart().length;
    const content = line.trim();

    if (indent === 2 && content.endsWith(":")) {
      currentNodeId = content.slice(0, -1);
      metadata[currentNodeId] = {};
      inParameters = false;
      continue;
    }

    if (indent === 4 && currentNodeId) {
      if (content === "parameters:") {
        inParameters = true;
        metadata[currentNodeId].parameters = {};
        continue;
      }
      inParameters = false;

      const colonIdx = content.indexOf(":");
      if (colonIdx === -1) continue;
      const key = content.slice(0, colonIdx).trim();
      let value = content.slice(colonIdx + 1).trim();
      value = value.replace(/^"(.*)"$/, "$1").replace(/^'(.*)'$/, "$1");

      if ((JSON_KEYS as readonly string[]).includes(key)) {
        try {
          (metadata[currentNodeId] as Record<string, unknown>)[key] = JSON.parse(value);
        } catch {
          // ignore malformed JSON block
        }
        continue;
      }

      switch (key) {
        case "name":
          metadata[currentNodeId].name = value;
          break;
        case "description":
          metadata[currentNodeId].description = value;
          break;
        case "type":
          metadata[currentNodeId].type = value as NodeMetadata["type"];
          break;
        case "command":
          metadata[currentNodeId].command = value;
          break;
        case "capability":
          metadata[currentNodeId].capability = value;
          break;
        case "method":
          metadata[currentNodeId].method = value;
          break;
        case "output":
          metadata[currentNodeId].output = value;
          break;
        case "timeout":
          metadata[currentNodeId].timeout = parseInt(value, 10) || undefined;
          break;
        case "retries":
          metadata[currentNodeId].retries = parseInt(value, 10) || undefined;
          break;
        case "on_success":
          metadata[currentNodeId].on_success = value;
          break;
        case "on_failure":
          metadata[currentNodeId].on_failure = value;
          break;
        case "on_complete":
          metadata[currentNodeId].on_complete = value;
          break;
      }
      continue;
    }

    if (indent === 6 && inParameters && currentNodeId) {
      const colonIdx = content.indexOf(":");
      if (colonIdx === -1) continue;
      const key = content.slice(0, colonIdx).trim();
      let value = content.slice(colonIdx + 1).trim();
      value = value.replace(/^"(.*)"$/, "$1");
      if (!metadata[currentNodeId].parameters) metadata[currentNodeId].parameters = {};
      metadata[currentNodeId].parameters![key] = value;
    }
  }

  return metadata;
}
