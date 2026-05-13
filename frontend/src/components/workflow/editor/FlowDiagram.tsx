/**
 * React Flow canvas wrapper for workflow visualization and editing.
 * Features: add node/conditional, connect, duplicate, delete, alignment, distribute.
 * Adapted from mermaid-reactflow-editor (MIT License).
 */

import { useCallback, useEffect, useMemo, useState, useRef } from "react";
import ReactFlow, {
  Background,
  Controls,
  MiniMap,
  Panel,
  ReactFlowProvider,
  useNodesState,
  useEdgesState,
  useReactFlow,
  addEdge,
  type Node,
  type Edge,
  type OnNodesChange,
  type OnEdgesChange,
  type Connection,
  MarkerType,
} from "reactflow";
import "reactflow/dist/style.css";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import {
  Plus,
  GitBranch,
  Copy,
  Trash2,
  AlignStartVertical,
  AlignCenterVertical,
  AlignEndVertical,
  AlignStartHorizontal,
  AlignCenterHorizontal,
  AlignEndHorizontal,
  GalleryHorizontalEnd,
  GalleryVerticalEnd,
  Lock,
  Unlock,
} from "lucide-react";
import { CustomNodeMemo, DiamondNodeMemo } from "./CustomNode";

const nodeTypes = { custom: CustomNodeMemo, diamond: DiamondNodeMemo };

interface FlowDiagramProps {
  nodes: Node[];
  edges: Edge[];
  onNodesChange?: OnNodesChange;
  onEdgesChange?: OnEdgesChange;
  onNodeDoubleClick?: (event: React.MouseEvent, node: Node) => void;
  onGraphChange?: (nodes: Node[], edges: Edge[]) => void;
  activeStepId?: string | null;
  completedStepIds?: string[];
  failedStepIds?: string[];
  readOnly?: boolean;
}

let nodeIdCounter = 0;
function nextNodeId(): string {
  return `node_${Date.now()}_${++nodeIdCounter}`;
}

function FlowDiagramInternal({
  nodes: initialNodes,
  edges: initialEdges,
  onNodeDoubleClick,
  onGraphChange,
  activeStepId,
  completedStepIds = [],
  failedStepIds = [],
  readOnly = false,
}: FlowDiagramProps) {
  const [locked, setLocked] = useState(false);
  const reactFlowWrapper = useRef<HTMLDivElement>(null);
  const reactFlowInstance = useReactFlow();

  const decoratedNodes = useMemo(() => {
    return initialNodes.map((node) => ({
      ...node,
      data: {
        ...node.data,
        isActive: node.id === activeStepId,
        isCompleted: completedStepIds.includes(node.id),
        isFailed: failedStepIds.includes(node.id),
      },
    }));
  }, [initialNodes, activeStepId, completedStepIds, failedStepIds]);

  const decoratedEdges = useMemo(() => {
    return initialEdges.map((edge) => {
      if (activeStepId && edge.target === activeStepId) {
        return { ...edge, animated: true, style: { ...edge.style, stroke: "#22c55e", strokeWidth: 3 } };
      }
      if (completedStepIds.includes(edge.source) && completedStepIds.includes(edge.target)) {
        return { ...edge, animated: false, style: { ...edge.style, stroke: "#6b7280", strokeWidth: 1.5, opacity: 0.6 } };
      }
      return edge;
    });
  }, [initialEdges, activeStepId, completedStepIds]);

  const [nodes, setNodes, onNodesChangeInternal] = useNodesState(decoratedNodes);
  const [edges, setEdges, onEdgesChangeInternal] = useEdgesState(decoratedEdges);

  // Sync local state when parent props change (e.g. mermaid code edited)
  // After updating, fit the viewport so the diagram is fully visible.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    setNodes(decoratedNodes);
    setEdges(decoratedEdges);
    // Double-RAF: first frame React Flow updates DOM, second frame we fit
    requestAnimationFrame(() => {
      requestAnimationFrame(() => {
        reactFlowInstance.fitView({ padding: 0.12, duration: 250 });
      });
    });
  }, [initialNodes, initialEdges]);

  // Notify parent of graph changes when needed
  const handleGraphChanged = useCallback(() => {
    if (onGraphChange) onGraphChange(nodes, edges);
  }, [onGraphChange, nodes, edges]);
  void handleGraphChanged;

  const handleNodesChange: OnNodesChange = useCallback(
    (changes) => {
      if (readOnly || locked) return;
      onNodesChangeInternal(changes);
    },
    [readOnly, locked, onNodesChangeInternal],
  );

  const handleEdgesChange: OnEdgesChange = useCallback(
    (changes) => {
      if (readOnly || locked) return;
      onEdgesChangeInternal(changes);
    },
    [readOnly, locked, onEdgesChangeInternal],
  );

  const onConnect = useCallback(
    (connection: Connection) => {
      if (readOnly || locked) return;
      const newEdge: Edge = {
        id: `e-${connection.source}-${connection.target}-${Date.now()}`,
        source: connection.source!,
        target: connection.target!,
        sourceHandle: connection.sourceHandle,
        targetHandle: connection.targetHandle,
        type: "smoothstep",
        animated: true,
        style: { stroke: "#76B900", strokeWidth: 2 },
        markerEnd: { type: MarkerType.ArrowClosed, color: "#76B900" },
      };
      setEdges((eds) => addEdge(newEdge, eds));
    },
    [readOnly, locked, setEdges],
  );

  // --- Editing actions ---

  const getSelectedNodes = useCallback(() => {
    return nodes.filter((n) => n.selected);
  }, [nodes]);

  const addNode = useCallback(
    (stepType: string) => {
      const id = nextNodeId();
      const label = stepType === "decision" ? "Decision?" : stepType === "manual" ? "Manual Step" : "New Step";
      const shape = stepType === "decision" ? "diamond" : stepType === "manual" ? "rounded" : "rect";
      const centerX = reactFlowWrapper.current ? reactFlowWrapper.current.clientWidth / 2 - 80 : 200;
      const newNode: Node = {
        id,
        type: "custom",
        position: { x: centerX, y: nodes.length * 100 + 50 },
        data: { label, shape, stepType },
        selected: true,
      };
      setNodes((nds) => [...nds.map((n) => ({ ...n, selected: false })), newNode]);
    },
    [nodes.length, setNodes],
  );

  const duplicateSelected = useCallback(() => {
    const selected = getSelectedNodes();
    if (selected.length === 0) return;
    const newNodes: Node[] = selected.map((n) => ({
      ...n,
      id: nextNodeId(),
      position: { x: n.position.x + 30, y: n.position.y + 60 },
      selected: true,
      data: { ...n.data },
    }));
    setNodes((nds) => [...nds.map((n) => ({ ...n, selected: false })), ...newNodes]);
  }, [getSelectedNodes, setNodes]);

  const deleteSelected = useCallback(() => {
    const selectedIds = new Set(nodes.filter((n) => n.selected).map((n) => n.id));
    if (selectedIds.size === 0) return;
    setNodes((nds) => nds.filter((n) => !selectedIds.has(n.id)));
    setEdges((eds) => eds.filter((e) => !selectedIds.has(e.source) && !selectedIds.has(e.target)));
  }, [nodes, setNodes, setEdges]);

  const GRID = 20;
  const snap = (v: number) => Math.round(v / GRID) * GRID;

  const alignNodes = useCallback(
    (axis: "left" | "centerX" | "right" | "top" | "centerY" | "bottom") => {
      const selected = getSelectedNodes();
      if (selected.length < 2) return;

      let target: number;
      switch (axis) {
        case "left":
          target = Math.min(...selected.map((n) => n.position.x));
          break;
        case "centerX":
          target = selected.reduce((sum, n) => sum + n.position.x, 0) / selected.length;
          break;
        case "right":
          target = Math.max(...selected.map((n) => n.position.x));
          break;
        case "top":
          target = Math.min(...selected.map((n) => n.position.y));
          break;
        case "centerY":
          target = selected.reduce((sum, n) => sum + n.position.y, 0) / selected.length;
          break;
        case "bottom":
          target = Math.max(...selected.map((n) => n.position.y));
          break;
      }

      target = snap(target);
      const ids = new Set(selected.map((n) => n.id));
      setNodes((nds) =>
        nds.map((n) => {
          if (!ids.has(n.id)) return n;
          const pos = { ...n.position };
          if (axis === "left" || axis === "centerX" || axis === "right") pos.x = target;
          else pos.y = target;
          return { ...n, position: pos };
        }),
      );
    },
    [getSelectedNodes, setNodes],
  );

  const distributeNodes = useCallback(
    (direction: "horizontal" | "vertical") => {
      const selected = getSelectedNodes();
      if (selected.length < 3) return;

      const sorted = [...selected].sort((a, b) =>
        direction === "horizontal" ? a.position.x - b.position.x : a.position.y - b.position.y,
      );

      const first = sorted[0].position;
      const last = sorted[sorted.length - 1].position;
      const total = direction === "horizontal" ? last.x - first.x : last.y - first.y;
      const step = total / (sorted.length - 1);

      const posMap = new Map<string, { x: number; y: number }>();
      sorted.forEach((n, i) => {
        const pos = { ...n.position };
        if (direction === "horizontal") pos.x = snap(first.x + step * i);
        else pos.y = snap(first.y + step * i);
        posMap.set(n.id, pos);
      });

      setNodes((nds) =>
        nds.map((n) => {
          const newPos = posMap.get(n.id);
          return newPos ? { ...n, position: newPos } : n;
        }),
      );
    },
    [getSelectedNodes, setNodes],
  );

  const selectedCount = nodes.filter((n) => n.selected).length;
  const showEditToolbar = !readOnly;

  return (
    <Box ref={reactFlowWrapper} sx={{ width: "100%", height: "100%", position: "relative" }}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        onNodesChange={handleNodesChange}
        onEdgesChange={handleEdgesChange}
        onConnect={onConnect}
        onNodeDoubleClick={onNodeDoubleClick}
        nodeTypes={nodeTypes}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        snapToGrid
        snapGrid={[20, 20]}
        nodesDraggable={!readOnly && !locked}
        nodesConnectable={!readOnly && !locked}
        elementsSelectable={!readOnly}
        deleteKeyCode={readOnly ? null : "Delete"}
        multiSelectionKeyCode="Shift"
        selectionOnDrag={!readOnly && !locked}
        panOnDrag={readOnly || locked ? true : [1, 2]}
        proOptions={{ hideAttribution: true }}
        defaultEdgeOptions={{
          type: "smoothstep",
          animated: true,
          style: { stroke: "#76B900", strokeWidth: 2 },
          markerEnd: { type: MarkerType.ArrowClosed, color: "#76B900" },
        }}
        style={{ width: "100%", height: "100%" }}
      >
        <Background gap={20} size={1} color="rgba(118,185,0,0.12)" />
        <Controls showInteractive={!readOnly} />
        <MiniMap
          nodeColor={(n) => {
            if (n.id === activeStepId) return "#22c55e";
            if (completedStepIds.includes(n.id)) return "#6b7280";
            if (failedStepIds.includes(n.id)) return "#ef4444";
            return "#76B900";
          }}
          maskColor="rgba(0,0,0,0.1)"
          style={{ height: 80 }}
        />

        {/* Editing toolbar panel */}
        {showEditToolbar && (
          <Panel position="top-left">
            <Box
              sx={{
                display: "flex",
                alignItems: "center",
                gap: 0.25,
                bgcolor: "background.paper",
                border: "1px solid",
                borderColor: "divider",
                borderRadius: 1.5,
                px: 0.75,
                py: 0.5,
                boxShadow: 2,
                flexWrap: "wrap",
              }}
            >
              {/* Add nodes */}
              <Tooltip title="Add Node">
                <IconButton size="small" onClick={() => addNode("automated")}>
                  <Plus size={16} />
                </IconButton>
              </Tooltip>
              <Tooltip title="Add Conditional">
                <IconButton size="small" onClick={() => addNode("decision")}>
                  <GitBranch size={16} />
                </IconButton>
              </Tooltip>

              <Divider orientation="vertical" flexItem sx={{ mx: 0.5 }} />

              {/* Edit actions */}
              <Tooltip title="Duplicate">
                <span>
                  <IconButton size="small" onClick={duplicateSelected} disabled={selectedCount === 0}>
                    <Copy size={16} />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Delete">
                <span>
                  <IconButton size="small" onClick={deleteSelected} disabled={selectedCount === 0}>
                    <Trash2 size={16} />
                  </IconButton>
                </span>
              </Tooltip>

              <Divider orientation="vertical" flexItem sx={{ mx: 0.5 }} />

              {/* Alignment */}
              <Tooltip title="Align Left">
                <span>
                  <IconButton size="small" onClick={() => alignNodes("left")} disabled={selectedCount < 2}>
                    <AlignStartVertical size={16} />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Align Center (X)">
                <span>
                  <IconButton size="small" onClick={() => alignNodes("centerX")} disabled={selectedCount < 2}>
                    <AlignCenterVertical size={16} />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Align Right">
                <span>
                  <IconButton size="small" onClick={() => alignNodes("right")} disabled={selectedCount < 2}>
                    <AlignEndVertical size={16} />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Align Top">
                <span>
                  <IconButton size="small" onClick={() => alignNodes("top")} disabled={selectedCount < 2}>
                    <AlignStartHorizontal size={16} />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Align Middle (Y)">
                <span>
                  <IconButton size="small" onClick={() => alignNodes("centerY")} disabled={selectedCount < 2}>
                    <AlignCenterHorizontal size={16} />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Align Bottom">
                <span>
                  <IconButton size="small" onClick={() => alignNodes("bottom")} disabled={selectedCount < 2}>
                    <AlignEndHorizontal size={16} />
                  </IconButton>
                </span>
              </Tooltip>

              <Divider orientation="vertical" flexItem sx={{ mx: 0.5 }} />

              {/* Distribute */}
              <Tooltip title="Distribute Horizontal">
                <span>
                  <IconButton size="small" onClick={() => distributeNodes("horizontal")} disabled={selectedCount < 3}>
                    <GalleryHorizontalEnd size={16} />
                  </IconButton>
                </span>
              </Tooltip>
              <Tooltip title="Distribute Vertical">
                <span>
                  <IconButton size="small" onClick={() => distributeNodes("vertical")} disabled={selectedCount < 3}>
                    <GalleryVerticalEnd size={16} />
                  </IconButton>
                </span>
              </Tooltip>

              <Divider orientation="vertical" flexItem sx={{ mx: 0.5 }} />

              {/* Lock */}
              <Tooltip title={locked ? "Unlock canvas" : "Lock canvas"}>
                <IconButton size="small" onClick={() => setLocked((l) => !l)}>
                  {locked ? <Lock size={16} /> : <Unlock size={16} />}
                </IconButton>
              </Tooltip>
            </Box>
          </Panel>
        )}
      </ReactFlow>
    </Box>
  );
}

export default function FlowDiagram(props: FlowDiagramProps) {
  return (
    <ReactFlowProvider>
      <FlowDiagramInternal {...props} />
    </ReactFlowProvider>
  );
}
