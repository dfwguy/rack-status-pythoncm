import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import TextField from "@mui/material/TextField";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import ToggleButton from "@mui/material/ToggleButton";
import ToggleButtonGroup from "@mui/material/ToggleButtonGroup";
import { Text, Card } from "@nvidia/foundations-react-core";
import { Save, Play, Sparkles, Code2, LayoutGrid, Eye } from "lucide-react";
import { mermaidToReactFlow } from "./editor/mermaidToReactFlow";
import FlowDiagram from "./editor/FlowDiagram";
import WorkflowAIGenerator from "./WorkflowAIGenerator";
import type { Workflow } from "../../services/workflowService";
import type { Node } from "reactflow";

interface WorkflowDesignerProps {
  workflow: Workflow | null;
  onSave: (workflow: Partial<Workflow>) => void;
  onRun?: (workflowId: string) => void;
}

const MIN_PANEL_WIDTH = 200;

export default function WorkflowDesigner({ workflow, onSave, onRun }: WorkflowDesignerProps) {
  const [name, setName] = useState(workflow?.name || "");
  const [description, setDescription] = useState(workflow?.description || "");
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
      setMermaidCode(workflow.mermaid || "");
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

  const { nodes, edges } = useMemo(() => {
    try {
      return mermaidToReactFlow(mermaidCode, stepTypes);
    } catch {
      return { nodes: [], edges: [] };
    }
  }, [mermaidCode, stepTypes]);

  const handleSave = useCallback(() => {
    onSave({
      id: workflow?.id,
      name,
      description,
      mermaid: mermaidCode,
      steps: workflow?.steps || [],
    });
  }, [workflow, name, description, mermaidCode, onSave]);

  const handleNodeDoubleClick = useCallback((_event: React.MouseEvent, node: Node) => {
    const line = `    ${node.id}["${node.data.label}"]`;
    const textarea = textareaRef.current;
    if (textarea) {
      const start = textarea.selectionStart;
      const val = textarea.value;
      const newVal = val.slice(0, start) + "\n" + line + val.slice(start);
      setMermaidCode(newVal);
    }
  }, []);

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
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "hidden" }}>
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
                <Text kind="body/bold/sm">Mermaid Source</Text>
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
    </Box>
  );
}
