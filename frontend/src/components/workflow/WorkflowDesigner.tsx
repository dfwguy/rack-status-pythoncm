import { useState, useCallback, useEffect, useMemo, useRef } from "react";
import Box from "@mui/material/Box";
import Button from "@mui/material/Button";
import TextField from "@mui/material/TextField";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Divider from "@mui/material/Divider";
import { Text, Card } from "@nvidia/foundations-react-core";
import { Save, Play, Code2, Eye, Sparkles } from "lucide-react";
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

export default function WorkflowDesigner({ workflow, onSave, onRun }: WorkflowDesignerProps) {
  const [name, setName] = useState(workflow?.name || "");
  const [description, setDescription] = useState(workflow?.description || "");
  const [mermaidCode, setMermaidCode] = useState(workflow?.mermaid || "graph TD\n    start[\"Start\"] --> step1[\"Step 1\"]\n    step1 --> done([\"Done\"])");
  const [showCode, setShowCode] = useState(true);
  const [showAIGenerator, setShowAIGenerator] = useState(false);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    if (workflow) {
      setName(workflow.name || "");
      setDescription(workflow.description || "");
      setMermaidCode(workflow.mermaid || "");
    }
  }, [workflow]);

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

  return (
    <Box sx={{ display: "flex", flexDirection: "column", height: "100%", minHeight: 0, overflow: "hidden" }}>
      {/* Toolbar */}
      <Box sx={{ display: "flex", alignItems: "center", gap: 2, px: 2, py: 1.5, borderBottom: "1px solid", borderColor: "divider", flexShrink: 0 }}>
        <TextField
          size="small"
          placeholder="Workflow name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          sx={{ width: 240 }}
        />
        <TextField
          size="small"
          placeholder="Description"
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          sx={{ flex: 1 }}
        />
        <Tooltip title="AI Generate">
          <IconButton size="small" onClick={() => setShowAIGenerator(true)}>
            <Sparkles size={18} />
          </IconButton>
        </Tooltip>
        <Tooltip title={showCode ? "Hide code" : "Show code"}>
          <IconButton size="small" onClick={() => setShowCode((v) => !v)}>
            {showCode ? <Eye size={18} /> : <Code2 size={18} />}
          </IconButton>
        </Tooltip>
        <Divider orientation="vertical" flexItem />
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

      {/* Main content: code editor + canvas */}
      <Box sx={{ flex: 1, display: "flex", overflow: "hidden", minHeight: 0 }}>
        {showCode && (
          <Box
            sx={{
              width: 380,
              flexShrink: 0,
              borderRight: "1px solid",
              borderColor: "divider",
              display: "flex",
              flexDirection: "column",
            }}
          >
            <Box sx={{ px: 2, py: 1, bgcolor: "action.hover", borderBottom: "1px solid", borderColor: "divider" }}>
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
        )}

        {/* React Flow Canvas */}
        <Box sx={{ flex: 1, position: "relative" }}>
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
      </Box>

      <WorkflowAIGenerator
        open={showAIGenerator}
        onClose={() => setShowAIGenerator(false)}
        onGenerated={(mermaid) => setMermaidCode(mermaid)}
      />
    </Box>
  );
}
