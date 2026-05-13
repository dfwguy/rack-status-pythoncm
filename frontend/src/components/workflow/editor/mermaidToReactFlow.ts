/**
 * Mermaid-to-ReactFlow converter.
 * Adapted from mermaid-reactflow-editor (MIT License).
 * Parses mermaid flowchart syntax and produces React Flow nodes and edges.
 * Uses dagre for automatic top-down (vertical) layout.
 */

import type { Node, Edge } from "reactflow";
import dagre from "dagre";

interface ParsedNode {
  id: string;
  label: string;
  shape: string;
  parentGroup?: string;
}

interface ParsedEdge {
  source: string;
  target: string;
  label: string;
}

interface ParseResult {
  nodes: Node[];
  edges: Edge[];
}

const NODE_PATTERNS = [
  { regex: /^(\w+)\{\{"(.+?)"\}\}/, shape: "hexagon" },
  { regex: /^(\w+)\{(.+?)\}/, shape: "diamond" },
  { regex: /^(\w+)\(\["(.+?)"\]\)/, shape: "stadium" },
  { regex: /^(\w+)\[\["(.+?)"\]\]/, shape: "subroutine" },
  { regex: /^(\w+)\(\((.+?)\)\)/, shape: "circle" },
  { regex: /^(\w+)\[\/(.+?)\/\]/, shape: "trapezoid" },
  { regex: /^(\w+)\[\\(.+?)\\]/, shape: "trapezoidAlt" },
  { regex: /^(\w+)>(.+?)\]/, shape: "asymmetric" },
  { regex: /^(\w+)\("(.+?)"\)/, shape: "rounded" },
  { regex: /^(\w+)\((.+?)\)/, shape: "rounded" },
  { regex: /^(\w+)\["(.+?)"\]/, shape: "rect" },
  { regex: /^(\w+)\[(.+?)\]/, shape: "rect" },
];

const EDGE_PATTERNS = [
  { regex: /-->\|"(.+?)"\|/, label: "$1", type: "arrow" },
  { regex: /-->\|(.+?)\|/, label: "$1", type: "arrow" },
  { regex: /-- "(.+?)" -->/, label: "$1", type: "arrow" },
  { regex: /-- (.+?) -->/, label: "$1", type: "arrow" },
  { regex: /-->/, label: "", type: "arrow" },
  { regex: /---\|"(.+?)"\|/, label: "$1", type: "line" },
  { regex: /---\|(.+?)\|/, label: "$1", type: "line" },
  { regex: /---/, label: "", type: "line" },
  { regex: /==>\|"(.+?)"\|/, label: "$1", type: "thick" },
  { regex: /==>\|(.+?)\|/, label: "$1", type: "thick" },
  { regex: /==>/, label: "", type: "thick" },
  { regex: /-\.->/, label: "", type: "dotted" },
];

function parseNodeDef(text: string): ParsedNode | null {
  const trimmed = text.trim();
  for (const { regex, shape } of NODE_PATTERNS) {
    const match = trimmed.match(regex);
    if (match) {
      return { id: match[1], label: match[2], shape };
    }
  }
  const idOnly = trimmed.match(/^(\w+)$/);
  if (idOnly) {
    return { id: idOnly[1], label: idOnly[1], shape: "rect" };
  }
  return null;
}

function getNodeStyle(shape: string, stepType?: string): Record<string, unknown> {
  const base = {
    padding: "10px 16px",
    borderRadius: shape === "rounded" || shape === "stadium" ? "20px" : shape === "diamond" ? "0" : "4px",
    border: "2px solid",
    borderColor: "#76B900",
    fontSize: "14px",
    minWidth: "120px",
    textAlign: "center" as const,
  };

  if (stepType === "decision" || shape === "diamond") {
    return { ...base, borderColor: "#fbbf24", background: "rgba(251,191,36,0.1)" };
  }
  if (stepType === "manual" || shape === "stadium" || shape === "rounded") {
    return { ...base, borderColor: "#60a5fa", background: "rgba(96,165,250,0.1)" };
  }
  return { ...base, background: "rgba(118,185,0,0.05)" };
}

export function mermaidToReactFlow(
  mermaidCode: string,
  stepTypes?: Record<string, string>,
): ParseResult {
  const lines = mermaidCode.split("\n").map((l) => l.trim()).filter(Boolean);
  const nodesMap = new Map<string, ParsedNode>();
  const edges: ParsedEdge[] = [];
  let inSubgraph = false;
  let currentGroup = "";

  for (const line of lines) {
    if (line.startsWith("graph ") || line.startsWith("flowchart ")) continue;
    if (line === "end") {
      inSubgraph = false;
      currentGroup = "";
      continue;
    }
    if (line.startsWith("subgraph ")) {
      inSubgraph = true;
      currentGroup = line.replace("subgraph ", "").replace(/\[.*\]/, "").trim();
      continue;
    }
    if (line.startsWith("%%") || line.startsWith("classDef") || line.startsWith("class ") || line.startsWith("style ")) {
      continue;
    }

    let edgeFound = false;
    for (const { regex, label: labelTemplate } of EDGE_PATTERNS) {
      const parts = line.split(regex);
      if (parts.length >= 2) {
        const sourceText = parts[0].trim();
        const targetText = parts[parts.length - 1].trim();
        const edgeLabel = parts.length === 3 ? parts[1] : labelTemplate;

        const sourceNode = parseNodeDef(sourceText);
        const targetNode = parseNodeDef(targetText);

        if (sourceNode && targetNode) {
          if (!nodesMap.has(sourceNode.id)) {
            if (inSubgraph) sourceNode.parentGroup = currentGroup;
            nodesMap.set(sourceNode.id, sourceNode);
          }
          if (!nodesMap.has(targetNode.id)) {
            if (inSubgraph) targetNode.parentGroup = currentGroup;
            nodesMap.set(targetNode.id, targetNode);
          }
          edges.push({ source: sourceNode.id, target: targetNode.id, label: edgeLabel });
          edgeFound = true;
          break;
        }
      }
    }

    if (!edgeFound) {
      const node = parseNodeDef(line);
      if (node) {
        if (inSubgraph) node.parentGroup = currentGroup;
        if (!nodesMap.has(node.id)) {
          nodesMap.set(node.id, node);
        }
      }
    }
  }

  const nodeArray = Array.from(nodesMap.values());

  // Use dagre for top-down (vertical) auto-layout
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: "TB", nodesep: 60, ranksep: 80, marginx: 40, marginy: 40 });

  const NODE_WIDTH = 180;
  const NODE_HEIGHT = 50;

  for (const n of nodeArray) {
    g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  for (const e of edges) {
    g.setEdge(e.source, e.target);
  }

  dagre.layout(g);

  const rfNodes: Node[] = nodeArray.map((n) => {
    const pos = g.node(n.id);
    return {
      id: n.id,
      type: "custom",
      position: {
        x: (pos?.x ?? 0) - NODE_WIDTH / 2,
        y: (pos?.y ?? 0) - NODE_HEIGHT / 2,
      },
      data: {
        label: n.label,
        shape: n.shape,
        stepType: stepTypes?.[n.id] || (n.shape === "diamond" ? "decision" : n.shape === "rounded" || n.shape === "stadium" ? "manual" : "automated"),
        style: getNodeStyle(n.shape, stepTypes?.[n.id]),
      },
      ...(n.parentGroup ? { parentNode: n.parentGroup, extent: "parent" as const } : {}),
    };
  });

  const rfEdges: Edge[] = edges.map((e, idx) => ({
    id: `e-${e.source}-${e.target}-${idx}`,
    source: e.source,
    target: e.target,
    label: e.label,
    type: "smoothstep",
    animated: true,
    style: { stroke: "#76B900", strokeWidth: 2 },
    labelStyle: { fontSize: 12, fill: "#999", fontWeight: 500 },
    labelBgStyle: { fill: "transparent" },
  }));

  return { nodes: rfNodes, edges: rfEdges };
}
