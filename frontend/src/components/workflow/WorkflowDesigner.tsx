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
import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import TextField from "@mui/material/TextField";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import Autocomplete from "@mui/material/Autocomplete";
import { Text, Card, Banner } from "@nvidia/foundations-react-core";
import { Save, Play, Sparkles, Code2, LayoutGrid, Eye } from "lucide-react";
import { mermaidToReactFlow, EDGE_PATTERNS, parseNodeDef } from "./editor/mermaidToReactFlow";
import { parseFrontmatter, serializeFrontmatter, getNodeMetadata, setNodeMetadata, type NodeMetadata, type WorkflowMetadata } from "./editor/frontmatter";
import FlowDiagram from "./editor/FlowDiagram";
import NodePropertiesDialog from "./NodePropertiesDialog";
import WorkflowAIGenerator from "./WorkflowAIGenerator";
import type { Workflow, WorkflowTarget, WorkflowStep } from "../../services/workflowService";
import type { Node } from "reactflow";

interface WorkflowDesignerProps {
  workflow: Workflow | null;
  onSave: (workflow: Partial<Workflow>) => void;
  onRun?: (workflowId: string) => void;
  availableRacks?: string[];
}

const MIN_PANEL_WIDTH = 200;

export default function WorkflowDesigner({ workflow, onSave, onRun, availableRacks = [] }: WorkflowDesignerProps) {
  const [name, setName] = useState(workflow?.name || "");
  const [description, setDescription] = useState(workflow?.description || "");
  const [targetRacks, setTargetRacks] = useState<string[]>(workflow?.target?.racks || []);
  const [mermaidCode, setMermaidCode] = useState(workflow?.mermaid || "graph TD\n    start[\"Start\"] --> step1[\"Step 1\"]\n    step1 --> done([\"Done\"])");
  const [panels, setPanels] = useState<string[]>(["code", "canvas"]);
  const [showAIGenerator, setShowAIGenerator] = useState(false);
  const [codePanelWidth, setCodePanelWidth] = useState(340);
  const [previewSvg, setPreviewSvg] = useState("");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (workflow) {
      setName(workflow.name || "");
      setDescription(workflow.description || "");
      setTargetRacks(workflow.target?.racks || []);
      // Hydrate the editor frontmatter from steps[] so per-step fields authored in
      // YAML (composition blocks, method/body/options/on_complete) are preserved and
      // not stripped on the next Save.
      const { metadata: fmMeta, mermaidCode: body } = parseFrontmatter(workflow.mermaid || "");
      const merged: WorkflowMetadata = { ...fmMeta };
      for (const s of workflow.steps || []) {
        const m: NodeMetadata = { ...(fmMeta[s.id] || {}) };
        if (s.name) m.name = s.name;
        if (s.description) m.description = s.description;
        if (s.type) m.type = s.type;
        if (s.command) m.command = s.command;
        if (s.capability) m.capability = s.capability;
        if (s.run) m.run = s.run;
        if (s.method) m.method = s.method;
        if (s.parameters) m.parameters = s.parameters;
        if (s.output) m.output = s.output;
        if (s.timeout !== undefined) m.timeout = s.timeout;
        if (s.retries !== undefined) m.retries = s.retries;
        if (s.on_success) m.on_success = s.on_success;
        if (s.on_failure) m.on_failure = s.on_failure;
        if (s.on_complete) m.on_complete = s.on_complete;
        if (s.connection) m.connection = s.connection as Record<string, unknown>;
        if (s.scheduler) m.scheduler = s.scheduler as Record<string, unknown>;
        if (s.evaluation) m.evaluation = s.evaluation as Record<string, unknown>;
        if (s.fanout) m.fanout = s.fanout as Record<string, unknown>;
        if (s.resolve) m.resolve = s.resolve as Record<string, unknown>;
        if (s.body) m.body = s.body;
        if (s.options) m.options = s.options;
        merged[s.id] = m;
      }
      setMermaidCode(
        Object.keys(merged).length ? serializeFrontmatter(merged, body) : (workflow.mermaid || ""),
      );
    }
  }, [workflow]);

  // Render mermaid SVG preview
  useEffect(() => {
    if (!panels.includes("preview") || !mermaidCode.trim()) {
      setPreviewSvg("");
      return;
    }
    let cancelled = false;
    (async () => {
      try {
        const mermaid = await import("mermaid");
        mermaid.default.initialize({ startOnLoad: false, theme: "dark" });
        const { svg } = await mermaid.default.render("mermaid-preview-" + Date.now(), mermaidCode);
        if (!cancelled) setPreviewSvg(svg);
      } catch {
        if (!cancelled) setPreviewSvg("");
      }
    })();
    return () => { cancelled = true; };
  }, [mermaidCode, panels]);

  const stepTypes = useMemo(() => {
    const map: Record<string, string> = {};
    for (const step of workflow?.steps || []) {
      map[step.id] = step.type;
    }
    return map;
  }, [workflow?.steps]);

  const { nodes, edges, syntaxError } = useMemo(() => {
    try {
      const result = mermaidToReactFlow(mermaidCode, stepTypes);
      return { ...result, syntaxError: "" };
    } catch (e) {
      return { nodes: [], edges: [], syntaxError: e instanceof Error ? e.message : "Invalid mermaid syntax" };
    }
  }, [mermaidCode, stepTypes]);

  const handleSave = useCallback(() => {
    // Derive steps from frontmatter metadata + parsed nodes
    const { metadata: currentMeta } = parseFrontmatter(mermaidCode);
    const nodeIds = nodes.map((n) => n.id);

    const steps = nodeIds.map((id) => {
      const meta = currentMeta[id];
      const node = nodes.find((n) => n.id === id);
      const inferredType = node?.data?.stepType || "automated";
      return {
        id,
        name: meta?.name || node?.data?.label || id,
        type: meta?.type || inferredType,
        ...(meta?.description ? { description: meta.description } : {}),
        ...(meta?.command ? { command: meta.command } : {}),
        ...(meta?.parameters ? { parameters: meta.parameters } : {}),
        ...(meta?.capability ? { capability: meta.capability } : {}),
        ...(meta?.run ? { run: meta.run } : {}),
        ...(meta?.method ? { method: meta.method } : {}),
        ...(meta?.output ? { output: meta.output } : {}),
        ...(meta?.timeout ? { timeout: meta.timeout } : {}),
        ...(meta?.retries ? { retries: meta.retries } : {}),
        ...(meta?.on_success ? { on_success: meta.on_success } : {}),
        ...(meta?.on_failure ? { on_failure: meta.on_failure } : {}),
        ...(meta?.on_complete ? { on_complete: meta.on_complete } : {}),
        ...(meta?.connection ? { connection: meta.connection as WorkflowStep["connection"] } : {}),
        ...(meta?.scheduler ? { scheduler: meta.scheduler as WorkflowStep["scheduler"] } : {}),
        ...(meta?.evaluation ? { evaluation: meta.evaluation as WorkflowStep["evaluation"] } : {}),
        ...(meta?.fanout ? { fanout: meta.fanout as WorkflowStep["fanout"] } : {}),
        ...(meta?.resolve ? { resolve: meta.resolve as WorkflowStep["resolve"] } : {}),
        ...(meta?.body ? { body: meta.body } : {}),
        ...(meta?.options ? { options: meta.options as WorkflowStep["options"] } : {}),
      };
    });

    const target: WorkflowTarget | undefined = targetRacks.length > 0
      ? { mode: "rack", racks: targetRacks, nodes: [] }
      : undefined;

    onSave({
      id: workflow?.id,
      name,
      description,
      mermaid: mermaidCode,
      steps,
      ...(target ? { target } : {}),
    });
  }, [workflow, name, description, mermaidCode, nodes, targetRacks, onSave]);

  // Node properties dialog state
  const [propsDialogOpen, setPropsDialogOpen] = useState(false);
  const [propsDialogNodeId, setPropsDialogNodeId] = useState("");
  const [propsDialogNodeLabel, setPropsDialogNodeLabel] = useState("");

  // Parse metadata from the full code (frontmatter + mermaid)
  const { metadata } = useMemo(() => parseFrontmatter(mermaidCode), [mermaidCode]);

  // All node IDs for the combobox options
  const allNodeIds = useMemo(() => nodes.map((n) => n.id), [nodes]);

  // Double-click opens properties dialog
  const handleNodeDoubleClick = useCallback((_event: React.MouseEvent, node: Node) => {
    setPropsDialogNodeId(node.id);
    setPropsDialogNodeLabel(node.data?.label || node.id);
    setPropsDialogOpen(true);
  }, []);

  // Save node properties back to frontmatter
  const handleNodePropsSave = useCallback((nodeId: string, props: NodeMetadata, newLabel?: string) => {
    setMermaidCode((code) => {
      const { metadata: currentMeta, mermaidCode: bodyCode } = parseFrontmatter(code);
      const updatedMeta = setNodeMetadata(currentMeta, nodeId, props);

      let updatedBody = bodyCode;
      if (newLabel) {
        const escaped = nodeId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
        updatedBody = updatedBody.replace(
          new RegExp(`(${escaped})\\[["']?[^"'\\]]*["']?\\]`),
          `$1["${newLabel}"]`,
        );
        updatedBody = updatedBody.replace(
          new RegExp(`(${escaped})\\(\\[["']?[^"'\\]]*["']?\\]\\)`),
          `$1(["${newLabel}"])`,
        );
        updatedBody = updatedBody.replace(
          new RegExp(`(${escaped})\\{["']?[^"'\\}]*["']?\\}`),
          `$1{"${newLabel}"}`,
        );
      }

      return serializeFrontmatter(updatedMeta, updatedBody);
    });
  }, []);

  const nodeCounter = useRef(0);

  // Derive next unique node ID by scanning existing code for step_N patterns
  const getNextNodeId = useCallback((code: string): string => {
    const matches = code.matchAll(/step_(\d+)/g);
    let max = nodeCounter.current;
    for (const m of matches) {
      const n = parseInt(m[1], 10);
      if (n > max) max = n;
    }
    nodeCounter.current = max + 1;
    return `step_${nodeCounter.current}`;
  }, []);

  // Check if a line references a given node ID (as definition, edge source, or edge target)
  const lineReferencesNode = useCallback((line: string, nodeId: string): boolean => {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("graph ") || trimmed.startsWith("flowchart ")) return false;
    const escaped = nodeId.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
    const pattern = new RegExp(`(^|\\s|-->|\\|)${escaped}(\\s|\\[|\\(|\\{|"|$|\\s*-->)`);
    return pattern.test(trimmed);
  }, []);

  // Find the last content line index (ignoring trailing empty lines)
  const findLastContentLine = useCallback((lines: string[]): number => {
    for (let i = lines.length - 1; i >= 0; i--) {
      if (lines[i].trim()) return i;
    }
    return lines.length - 1;
  }, []);

  // Find the last line that references a specific node
  const findLastLineForNode = useCallback((lines: string[], nodeId: string): number => {
    for (let i = lines.length - 1; i >= 0; i--) {
      if (lineReferencesNode(lines[i], nodeId)) return i;
    }
    return -1;
  }, [lineReferencesNode]);

  const handleCodeAction = useCallback((action: { type: string; [key: string]: unknown }) => {
    setMermaidCode((code) => {
      const lines = code.split("\n");

      if (action.type === "addNode") {
        const stepType = action.stepType as string;
        const id = getNextNodeId(code);
        let nodeDef: string;
        if (stepType === "decision") {
          nodeDef = `    ${id}{"New Decision?"}`;
        } else if (stepType === "manual") {
          nodeDef = `    ${id}(["Manual Step"])`;
        } else {
          nodeDef = `    ${id}["New Step"]`;
        }
        // Insert after the last content line
        const insertIdx = findLastContentLine(lines);
        lines.splice(insertIdx + 1, 0, nodeDef);
        return lines.join("\n");
      }

      if (action.type === "addEdge") {
        const { source, target, label } = action as { type: string; source: string; target: string; label?: string };
        const edgeLine = label
          ? `    ${source} -->|"${label}"| ${target}`
          : `    ${source} --> ${target}`;
        // Insert after the last line that references the source node
        const sourceLineIdx = findLastLineForNode(lines, source);
        const insertIdx = sourceLineIdx >= 0 ? sourceLineIdx : findLastContentLine(lines);
        lines.splice(insertIdx + 1, 0, edgeLine);
        return lines.join("\n");
      }

      if (action.type === "deleteEdge") {
        const { source, target } = action as { type: string; source: string; target: string };
        const removedNodes: { id: string; label: string; shape: string }[] = [];
        const filtered = lines.filter((line) => {
          const trimmed = line.trim();
          for (const { regex } of EDGE_PATTERNS) {
            const parts = trimmed.split(regex);
            if (parts.length >= 2) {
              const src = parseNodeDef(parts[0].trim());
              const tgt = parseNodeDef(parts[parts.length - 1].trim());
              if (src?.id === source && tgt?.id === target) {
                if (src.label !== src.id) removedNodes.push(src);
                if (tgt.label !== tgt.id) removedNodes.push(tgt);
                return false;
              }
            }
          }
          return true;
        });
        const orphaned = removedNodes.filter((node) => {
          const escapedId = node.id.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
          const pattern = new RegExp(`(^|\\s|-->|\\|)${escapedId}(\\s|\\[|\\(|\\{|"|$|\\s*-->)`);
          return !filtered.some((line) => pattern.test(line.trim()));
        });
        for (const node of orphaned) {
          let def: string;
          if (node.shape === "stadium") def = `    ${node.id}(["${node.label}"])`;
          else if (node.shape === "diamond") def = `    ${node.id}{"${node.label}"}`;
          else if (node.shape === "rounded") def = `    ${node.id}("${node.label}")`;
          else def = `    ${node.id}["${node.label}"]`;
          filtered.push(def);
        }
        return filtered.join("\n");
      }

      if (action.type === "deleteNodes") {
        const ids = action.ids as string[];
        const filtered = lines.filter((line) => {
          for (const id of ids) {
            if (lineReferencesNode(line, id)) return false;
          }
          return true;
        });
        return filtered.join("\n");
      }

      if (action.type === "duplicateNode") {
        const { id: origId, label, stepType } = action as { type: string; id: string; label: string; stepType: string };
        const newId = getNextNodeId(code);
        let nodeDef: string;
        if (stepType === "decision") {
          nodeDef = `    ${newId}{"${label}"}`;
        } else if (stepType === "manual") {
          nodeDef = `    ${newId}(["${label}"])`;
        } else {
          nodeDef = `    ${newId}["${label}"]`;
        }
        // Insert after the original node's last reference
        const origLineIdx = findLastLineForNode(lines, origId);
        const insertIdx = origLineIdx >= 0 ? origLineIdx : findLastContentLine(lines);
        lines.splice(insertIdx + 1, 0, nodeDef);
        return lines.join("\n");
      }

      return code;
    });
  }, [getNextNodeId, lineReferencesNode, findLastContentLine, findLastLineForNode]);

  const handlePanelToggle = useCallback((_: React.MouseEvent<HTMLElement>, newPanels: string[]) => {
    if (newPanels.length > 0) setPanels(newPanels);
  }, []);

  // Draggable resize handle
  const handleResizeStart = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const startW = codePanelWidth;

    const onMove = (ev: MouseEvent) => {
      const container = containerRef.current;
      const maxW = container ? container.clientWidth - MIN_PANEL_WIDTH : 800;
      const newW = Math.max(MIN_PANEL_WIDTH, Math.min(maxW, startW + (ev.clientX - startX)));
      setCodePanelWidth(newW);
    };
    const onUp = () => {
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    document.body.style.cursor = "col-resize";
    document.body.style.userSelect = "none";
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [codePanelWidth]);

  const showCode = panels.includes("code");
  const showCanvas = panels.includes("canvas");
  const showPreview = panels.includes("preview");
  const visiblePanelCount = [showCode, showCanvas, showPreview].filter(Boolean).length;

  return (
    <Box component="section" aria-label="Workflow designer: mermaid editor and flow canvas" sx={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "hidden" }}>
      {/* Toolbar */}
      <Box sx={{ display: "flex", alignItems: "center", gap: 1.5, px: 2, py: 1, borderBottom: "1px solid", borderColor: "divider", flexShrink: 0 }}>
        <TextField
          size="small"
          placeholder="Workflow name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          sx={{ width: 200 }}
        />
        <TextField
          size="small"
          placeholder="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          sx={{ flex: 1 }}
        />

        <Autocomplete<string, true>
          multiple
          limitTags={3}
          size="small"
          options={availableRacks}
          value={targetRacks}
          onChange={(_, value) => setTargetRacks(value)}
          renderInput={(params) => (
            <TextField {...params} placeholder={targetRacks.length === 0 ? "Target racks" : ""} />
          )}
          sx={{ minWidth: 180 }}
        />

        <Divider orientation="vertical" flexItem />

        {/* Panel visibility toggles */}
        <ToggleButtonGroup
          value={panels}
          onChange={handlePanelToggle}
          size="small"
          aria-label="visible panels"
        >
          <ToggleButton value="code" aria-label="code editor" sx={{ px: 1.5, py: 0.5, gap: 0.5, textTransform: "none", fontSize: 13 }}>
            <Code2 size={15} /> Code
          </ToggleButton>
          <ToggleButton value="canvas" aria-label="canvas" sx={{ px: 1.5, py: 0.5, gap: 0.5, textTransform: "none", fontSize: 13 }}>
            <LayoutGrid size={15} /> Canvas
          </ToggleButton>
          <ToggleButton value="preview" aria-label="preview" sx={{ px: 1.5, py: 0.5, gap: 0.5, textTransform: "none", fontSize: 13 }}>
            <Eye size={15} /> Preview
          </ToggleButton>
        </ToggleButtonGroup>

        <Divider orientation="vertical" flexItem />

        <Tooltip title="AI Generate">
          <IconButton size="small" onClick={() => setShowAIGenerator(true)}>
            <Sparkles size={18} />
          </IconButton>
        </Tooltip>

        <Button
          variant="contained"
          size="small"
          startIcon={<Save size={16} />}
          onClick={handleSave}
          disabled={!name.trim()}
        >
          Save
        </Button>
        {workflow?.id && onRun && (
          <Button
            variant="outlined"
            size="small"
            startIcon={<Play size={16} />}
            onClick={() => onRun(workflow.id)}
          >
            Run
          </Button>
        )}
      </Box>

      {/* Panels */}
      <Box ref={containerRef} sx={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}>

        {/* Code editor panel */}
        {showCode && (
          <>
            <Box
              sx={{
                width: visiblePanelCount === 1 ? "100%" : codePanelWidth,
                flexShrink: 0,
                display: "flex",
                flexDirection: "column",
                overflow: "hidden",
              }}
            >
              <Box sx={{ px: 2, py: 0.75, bgcolor: "action.hover", borderBottom: "1px solid", borderColor: "divider", flexShrink: 0 }}>
                <Text kind="body/bold/sm">Template Source</Text>
              </Box>
              <Box
                component="textarea"
                ref={textareaRef}
                value={mermaidCode}
                onChange={(e: React.ChangeEvent<HTMLTextAreaElement>) => setMermaidCode(e.target.value)}
                spellCheck={false}
                sx={{
                  flex: 1,
                  p: 2,
                  fontFamily: "ui-monospace, monospace",
                  fontSize: 13,
                  lineHeight: 1.5,
                  border: "none",
                  outline: "none",
                  resize: "none",
                  bgcolor: "background.paper",
                  color: "text.primary",
                  overflow: "auto",
                }}
              />
              {syntaxError && (
                <Box sx={{ px: 2, py: 1, flexShrink: 0, borderTop: "1px solid", borderColor: "divider" }}>
                  <Banner status="error" kind="inline">
                    {syntaxError}
                  </Banner>
                </Box>
              )}
            </Box>

            {/* Resize handle */}
            {visiblePanelCount > 1 && (
              <Box
                onMouseDown={handleResizeStart}
                sx={{
                  width: 6,
                  flexShrink: 0,
                  cursor: "col-resize",
                  bgcolor: "divider",
                  transition: "background-color 0.15s",
                  "&:hover": { bgcolor: "primary.main" },
                  "&:active": { bgcolor: "primary.main" },
                }}
              />
            )}
          </>
        )}

        {/* React Flow Canvas */}
        {showCanvas && (
          <>
            <Box sx={{ flex: 1, position: "relative", minWidth: 0 }}>
              {nodes.length > 0 ? (
                <FlowDiagram
                  nodes={nodes}
                  edges={edges}
                  onNodeDoubleClick={handleNodeDoubleClick}
                  onCodeAction={handleCodeAction}
                />
              ) : (
                <Box sx={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%" }}>
                  <Card style={{ padding: 32, textAlign: "center" }}>
                    <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                      Enter valid Mermaid flowchart code to see the diagram.
                    </Text>
                  </Card>
                </Box>
              )}
            </Box>

            {/* Resize handle between canvas and preview */}
            {showPreview && (
              <Box sx={{ width: 6, flexShrink: 0, cursor: "col-resize", bgcolor: "divider", "&:hover": { bgcolor: "primary.main" } }} />
            )}
          </>
        )}

        {/* Mermaid SVG Preview */}
        {showPreview && (
          <Box
            sx={{
              flex: showCanvas ? 0.6 : 1,
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              overflow: "hidden",
            }}
          >
            <Box sx={{ px: 2, py: 0.75, bgcolor: "action.hover", borderBottom: "1px solid", borderColor: "divider", flexShrink: 0 }}>
              <Text kind="body/bold/sm">Mermaid Preview</Text>
            </Box>
            <Box
              sx={{
                flex: 1,
                overflow: "auto",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                p: 2,
                "& svg": { maxWidth: "100%", height: "auto" },
              }}
            >
              {previewSvg ? (
                <div dangerouslySetInnerHTML={{ __html: previewSvg }} />
              ) : (
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  {mermaidCode.trim() ? "Rendering preview…" : "No diagram to preview."}
                </Text>
              )}
            </Box>
          </Box>
        )}
      </Box>

      <WorkflowAIGenerator
        open={showAIGenerator}
        onClose={() => setShowAIGenerator(false)}
        onGenerated={(mermaid) => setMermaidCode(mermaid)}
      />

      <NodePropertiesDialog
        open={propsDialogOpen}
        onOpenChange={setPropsDialogOpen}
        nodeId={propsDialogNodeId}
        nodeLabel={propsDialogNodeLabel}
        metadata={getNodeMetadata(metadata, propsDialogNodeId)}
        allNodeIds={allNodeIds}
        onSave={handleNodePropsSave}
      />
    </Box>
  );
}
