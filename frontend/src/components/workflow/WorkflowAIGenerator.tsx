import { useState, useCallback } from "react";
import Box from "@mui/material/Box";
import TextField from "@mui/material/TextField";
import Button from "@mui/material/Button";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import LinearProgress from "@mui/material/LinearProgress";
import { Text } from "@nvidia/foundations-react-core";
import { Bot, Sparkles } from "lucide-react";
import { streamChat } from "../../services/aiService";

interface WorkflowAIGeneratorProps {
  open: boolean;
  onClose: () => void;
  onGenerated: (mermaid: string) => void;
}

const GENERATE_PROMPT = `You are a workflow designer. Generate a Mermaid flowchart diagram based on the user's description.

Rules:
- Use "graph TD" (top-down) format
- Use descriptive node IDs (snake_case)
- Use square brackets for automated steps: step_id["Step Name"]
- Use curly braces for decision points: decision_id{"Question?"}
- Use rounded brackets for manual steps: step_id(["Manual Step Name"])
- Use stadium shape for start/end: start(["Start"]) and done(["Done"])
- Label edges with conditions: -->|"condition"|
- Keep it concise but complete
- Only output the mermaid code, no explanations

Example format:
graph TD
    start(["Start"]) -->|"begin"| check_status["Check Status"]
    check_status -->|"healthy"| done(["Done"])
    check_status -->|"unhealthy"| diagnose{"Hardware or Software?"}
    diagnose -->|"Hardware"| hw_fix(["Replace Component"])
    diagnose -->|"Software"| sw_fix["Restart Service"]
    hw_fix --> verify["Verify Fix"]
    sw_fix --> verify
    verify -->|"pass"| done
    verify -->|"fail"| escalate(["Escalate to Engineering"])`;

export default function WorkflowAIGenerator({ open, onClose, onGenerated }: WorkflowAIGeneratorProps) {
  const [description, setDescription] = useState("");
  const [generating, setGenerating] = useState(false);
  const [preview, setPreview] = useState("");
  const [error, setError] = useState("");

  const handleGenerate = useCallback(async () => {
    if (!description.trim()) return;
    setGenerating(true);
    setPreview("");
    setError("");

    let accumulated = "";

    try {
      const controller = await streamChat(
        [
          { role: "user", content: `Create a workflow for: ${description.trim()}` },
        ],
        { page: "workflows", additional_context: GENERATE_PROMPT },
        (chunk) => {
          accumulated += chunk;
          const mermaidMatch = accumulated.match(/```mermaid\n([\s\S]*?)```/) ||
            accumulated.match(/(graph\s+(?:TD|LR|TB|BT|RL)[\s\S]*)/);
          if (mermaidMatch) {
            setPreview(mermaidMatch[1].trim());
          } else {
            setPreview(accumulated);
          }
        },
        () => {
          setGenerating(false);
          const mermaidMatch = accumulated.match(/```mermaid\n([\s\S]*?)```/) ||
            accumulated.match(/(graph\s+(?:TD|LR|TB|BT|RL)[\s\S]*)/);
          const finalMermaid = mermaidMatch ? mermaidMatch[1].trim() : accumulated.trim();
          setPreview(finalMermaid);
        },
        (err) => {
          setError(err);
          setGenerating(false);
        },
      );

      return () => controller.abort();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setGenerating(false);
    }
  }, [description]);

  const handleApply = useCallback(() => {
    if (preview) {
      onGenerated(preview);
      onClose();
      setDescription("");
      setPreview("");
    }
  }, [preview, onGenerated, onClose]);

  return (
    <Dialog open={open} onClose={onClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <Bot size={20} />
        <Text kind="body/bold/md">AI Workflow Generator</Text>
      </DialogTitle>
      <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 2, pt: "16px !important" }}>
        <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
          Describe the workflow you want to create and AI will generate a mermaid flowchart diagram.
        </Text>

        <TextField
          fullWidth
          multiline
          minRows={3}
          maxRows={6}
          placeholder="Example: Create a GPU troubleshooting workflow that checks health status, runs NCCL tests, and escalates to hardware replacement if diagnostics fail."
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          disabled={generating}
        />

        <Button
          variant="contained"
          startIcon={<Sparkles size={16} />}
          onClick={handleGenerate}
          disabled={generating || !description.trim()}
        >
          {generating ? "Generating…" : "Generate Workflow"}
        </Button>

        {generating && <LinearProgress />}

        {error && (
          <Text kind="body/regular/sm" style={{ color: "var(--color-red-500)" }}>
            {error}
          </Text>
        )}

        {preview && (
          <Box
            sx={{
              bgcolor: "action.hover",
              borderRadius: 1,
              p: 2,
              fontFamily: "monospace",
              fontSize: 13,
              whiteSpace: "pre-wrap",
              maxHeight: 300,
              overflow: "auto",
              border: "1px solid",
              borderColor: "divider",
            }}
          >
            {preview}
          </Box>
        )}
      </DialogContent>
      <DialogActions>
        <MuiButton onClick={onClose}>Cancel</MuiButton>
        <MuiButton variant="contained" onClick={handleApply} disabled={!preview || generating}>
          Apply to Editor
        </MuiButton>
      </DialogActions>
    </Dialog>
  );
}
