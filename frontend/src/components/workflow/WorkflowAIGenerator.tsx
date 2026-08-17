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

const SYSTEM_INSTRUCTION = `You are a Mermaid flowchart code generator. You ONLY output valid Mermaid flowchart code. You NEVER output explanations, comments, or any text outside the mermaid code block.

STRICT RULES:
1. Start with "graph TD" on the first line
2. Use descriptive snake_case node IDs
3. Automated steps use square brackets: step_id["Step Name"]
4. Decision/conditional points use curly braces: decision_id{"Question?"}
5. Manual steps use stadium shape: step_id(["Manual Step Name"])
6. Start and end use stadium shape: start(["Start"]) and done(["Done"])
7. Label edges with conditions in quotes: -->|"condition"|
8. Output ONLY the raw mermaid code — no markdown fences, no prose, no explanation
9. Every workflow must have a start node and at least one end/done node
10. Decision nodes must have at least two outgoing edges with labels

EXAMPLE OUTPUT:
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

function extractMermaid(text: string): string {
  // Try to extract from markdown code fence first
  const fenced = text.match(/```(?:mermaid)?\s*\n([\s\S]*?)```/);
  if (fenced) return fenced[1].trim();

  // Try to find raw mermaid starting with graph/flowchart directive
  const raw = text.match(/((?:graph|flowchart)\s+(?:TD|LR|TB|BT|RL)[\s\S]*)/);
  if (raw) return raw[1].trim();

  // Strip any leading prose before "graph" if present
  const idx = text.indexOf("graph ");
  if (idx > 0) return text.slice(idx).trim();
  const idx2 = text.indexOf("flowchart ");
  if (idx2 > 0) return text.slice(idx2).trim();

  return text.trim();
}

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
      const controller = streamChat(
        [
          { role: "assistant", content: SYSTEM_INSTRUCTION },
          { role: "user", content: `Generate a Mermaid flowchart for the following workflow. Output ONLY the mermaid code starting with "graph TD", nothing else:\n\n${description.trim()}` },
        ],
        { page: "workflows" },
        {
          onChunk: (chunk: string) => {
            accumulated += chunk;
            setPreview(extractMermaid(accumulated));
          },
          onDone: () => {
            setGenerating(false);
            setPreview(extractMermaid(accumulated));
          },
          onError: (err: string) => {
            setError(err);
            setGenerating(false);
          },
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
        <MuiButton variant="outlined" onClick={onClose}>Cancel</MuiButton>
        <MuiButton variant="contained" onClick={handleApply} disabled={!preview || generating}>
          Apply to Editor
        </MuiButton>
      </DialogActions>
    </Dialog>
  );
}
