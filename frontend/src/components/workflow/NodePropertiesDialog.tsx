import { useState, useEffect, useCallback } from "react";
import {
  Modal,
  ModalCloseButton,
  Button,
  Flex,
  Text,
  FormField,
  TextInput,
  Select,
} from "@nvidia/foundations-react-core";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import { Settings2, Plus, Trash2 } from "lucide-react";
import type { NodeMetadata } from "./editor/frontmatter";

interface AvailableTool {
  id: string;
  label: string;
  description: string;
  category: string;
  path: string;
}

interface NodePropertiesDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  nodeId: string;
  nodeLabel: string;
  metadata: NodeMetadata;
  allNodeIds: string[];
  onSave: (nodeId: string, metadata: NodeMetadata, newLabel?: string) => void;
}

const STEP_TYPE_ITEMS = [
  { value: "automated", children: "Automated" },
  { value: "manual", children: "Manual" },
  { value: "decision", children: "Decision" },
  { value: "ai_query", children: "AI Query" },
  { value: "delay", children: "Delay (wait)" },
];

export default function NodePropertiesDialog({
  open,
  onOpenChange,
  nodeId,
  nodeLabel,
  metadata,
  allNodeIds,
  onSave,
}: NodePropertiesDialogProps) {
  const [name, setName] = useState(metadata.name || nodeLabel || "");
  const [description, setDescription] = useState(metadata.description || "");
  const [type, setType] = useState(metadata.type || "automated");
  const [command, setCommand] = useState(metadata.command || "");
  const [params, setParams] = useState<string[]>(
    Object.entries(metadata.parameters || {}).map(([key, value]) => (value ? `${key}=${value}` : key)),
  );
  const [output, setOutput] = useState(metadata.output || "");
  const [timeout, setTimeout_] = useState(metadata.timeout?.toString() || "");
  const [retries, setRetries] = useState(metadata.retries?.toString() || "");
  const [onSuccess, setOnSuccess] = useState(metadata.on_success || "");
  const [onFailure, setOnFailure] = useState(metadata.on_failure || "");
  const [availableTools, setAvailableTools] = useState<AvailableTool[]>([]);
  const [useCustomCommand, setUseCustomCommand] = useState(false);

  // Fetch available tools on mount
  useEffect(() => {
    fetch("/api/tools/available", { credentials: "same-origin" })
      .then((r) => r.json())
      .then((data) => {
        if (data.ok) setAvailableTools(data.tools || []);
      })
      .catch(() => {});
  }, []);

  // Only reset form when the dialog opens or node changes — not on every metadata recalc
  useEffect(() => {
    if (!open) return;
    setName(metadata.name || nodeLabel || "");
    setDescription(metadata.description || "");
    setType(metadata.type || "automated");
    setCommand(metadata.command || "");
    setParams(Object.entries(metadata.parameters || {}).map(([key, value]) => (value ? `${key}=${value}` : key)));
    setOutput(metadata.output || "");
    setTimeout_(metadata.timeout?.toString() || "");
    setRetries(metadata.retries?.toString() || "");
    setOnSuccess(metadata.on_success || "");
    setOnFailure(metadata.on_failure || "");
    const cmd = metadata.command || "";
    const isKnownTool = availableTools.some((t) => t.id === cmd);
    setUseCustomCommand(!isKnownTool && !!cmd);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, nodeId]);

  const handleAddParam = useCallback(() => {
    setParams((p) => [...p, ""]);
  }, []);

  const handleRemoveParam = useCallback((idx: number) => {
    setParams((p) => p.filter((_, i) => i !== idx));
  }, []);

  const handleParamChange = useCallback((idx: number, val: string) => {
    setParams((p) => p.map((item, i) => (i === idx ? val : item)));
  }, []);

  const handleSave = useCallback(() => {
    const parameters: Record<string, string> = {};
    for (const p of params) {
      const trimmed = p.trim();
      if (!trimmed) continue;
      const eqIdx = trimmed.indexOf("=");
      if (eqIdx > 0) {
        parameters[trimmed.slice(0, eqIdx).trim()] = trimmed.slice(eqIdx + 1).trim();
      } else {
        parameters[trimmed] = "";
      }
    }

    const updated: NodeMetadata = {
      name: name.trim() || undefined,
      description: description.trim() || undefined,
      type: type as NodeMetadata["type"],
      command: command.trim() || undefined,
      parameters: Object.keys(parameters).length > 0 ? parameters : undefined,
      output: output.trim() || undefined,
      timeout: timeout ? parseInt(timeout, 10) : undefined,
      retries: retries ? parseInt(retries, 10) : undefined,
      on_success: onSuccess.trim() || undefined,
      on_failure: onFailure.trim() || undefined,
    };

    const newLabel = name.trim() !== nodeLabel ? name.trim() : undefined;
    onSave(nodeId, updated, newLabel);
    onOpenChange(false);
  }, [nodeId, nodeLabel, name, description, type, command, params, output, timeout, retries, onSuccess, onFailure, onSave, onOpenChange]);

  const otherNodeIds = allNodeIds.filter((id) => id !== nodeId);
  const nodeIdItems = [{ value: "", children: "— none —" }, ...otherNodeIds.map((id) => ({ value: id, children: id }))];

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      modal={true}
      style={{ width: "640px", maxWidth: "90vw" }}
      slotHeading={
        <>
          <Settings2 size={18} />
          Node Properties — {nodeId}
        </>
      }
      slotFooter={
        <>
          <ModalCloseButton kind="tertiary">Cancel</ModalCloseButton>
          <Button kind="primary" color="brand" onClick={handleSave}>
            Save
          </Button>
        </>
      }
    >
      <Flex direction="col" gap="3">
        <FormField name="node-name" slotLabel="Name" slotHelp="Display label for the node (updates the diagram)">
          <TextInput value={name} onValueChange={setName} placeholder="Step name" />
        </FormField>

        <FormField name="node-description" slotLabel="Description">
          <TextInput value={description} onValueChange={setDescription} placeholder="What this step does" />
        </FormField>

        <FormField name="node-type" slotLabel="Type">
          <Select
            items={STEP_TYPE_ITEMS}
            value={type}
            onValueChange={(v) => setType(v as NodeMetadata["type"] ?? "automated")}
            size="medium"
          />
        </FormField>

        {type === "automated" && !useCustomCommand && (
          <FormField name="node-command" slotLabel="Script / Tool" slotHelp="Select a predefined tool or choose Custom">
            <Select
              items={[
                ...availableTools.map((t) => ({ value: t.id, children: `${t.label} (${t.category})` })),
                { value: "__custom__", children: "— Custom command —" },
              ]}
              value={command}
              onValueChange={(v) => {
                if (v === "__custom__") {
                  setUseCustomCommand(true);
                  setCommand("");
                } else {
                  setCommand(v);
                }
              }}
              placeholder="Select a tool..."
              size="medium"
            />
          </FormField>
        )}

        {(type === "automated" && useCustomCommand) && (
          <FormField name="node-command" slotLabel="Custom Command" slotHelp="API endpoint or host command">
            <Flex gap="2" align="center">
              <Box sx={{ flex: 1 }}>
                <TextInput value={command} onValueChange={setCommand} placeholder="host:nvidia-smi or /api/endpoint" />
              </Box>
              <Button kind="tertiary" size="small" onClick={() => { setUseCustomCommand(false); setCommand(""); }}>
                Use predefined
              </Button>
            </Flex>
          </FormField>
        )}

        {type === "manual" && (
          <FormField name="node-command" slotLabel="Command / Script" slotHelp="Host command or script path to execute manually">
            <TextInput value={command} onValueChange={setCommand} placeholder="host:nvidia-smi or /path/to/script.sh" />
          </FormField>
        )}

        {type === "ai_query" && (
          <FormField name="node-command" slotLabel="AI Prompt / Query" slotHelp="Prompt for the AI to analyze results">
            <TextInput value={command} onValueChange={setCommand} placeholder="Analyze the GPU health status and recommend action" />
          </FormField>
        )}

        {type === "delay" && (
          <FormField name="node-timeout-delay" slotLabel="Delay Duration (seconds)" slotHelp="Number of seconds to wait before continuing to the next step">
            <TextInput value={timeout || "5"} onValueChange={setTimeout_} placeholder="5" />
          </FormField>
        )}

        {/* Parameters */}
        <Box>
          <Flex align="center" justify="between" style={{ marginBottom: 8 }}>
            <Text kind="body/bold/sm">Parameters</Text>
            <IconButton size="small" onClick={handleAddParam} title="Add parameter">
              <Plus size={16} />
            </IconButton>
          </Flex>
          {params.map((p, idx) => (
            <Flex key={idx} gap="2" align="center" style={{ marginBottom: 6 }}>
              <Box sx={{ flex: 1 }}>
                <TextInput value={p} onValueChange={(v) => handleParamChange(idx, v)} placeholder="-p or --key=value" size="small" />
              </Box>
              <IconButton size="small" onClick={() => handleRemoveParam(idx)} title="Remove">
                <Trash2 size={14} />
              </IconButton>
            </Flex>
          ))}
          {params.length === 0 && (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              No parameters. Click + to add.
            </Text>
          )}
        </Box>

        <FormField name="node-output" slotLabel="Expected Output">
          <TextInput value={output} onValueChange={setOutput} placeholder="Variable name for step output" />
        </FormField>

        <Flex gap="2">
          <Box sx={{ flex: 1 }}>
            <FormField name="node-timeout" slotLabel="Timeout (seconds)">
              <TextInput value={timeout} onValueChange={setTimeout_} placeholder="120" />
            </FormField>
          </Box>
          <Box sx={{ flex: 1 }}>
            <FormField name="node-retries" slotLabel="Retry Count">
              <TextInput value={retries} onValueChange={setRetries} placeholder="3" />
            </FormField>
          </Box>
        </Flex>

        <Flex gap="2">
          <Box sx={{ flex: 1 }}>
            <FormField name="node-on-success" slotLabel="On Success → Node">
              <Select
                items={nodeIdItems}
                value={onSuccess}
                onValueChange={setOnSuccess}
                placeholder="— none —"
                size="medium"
              />
            </FormField>
          </Box>
          <Box sx={{ flex: 1 }}>
            <FormField name="node-on-failure" slotLabel="On Failure → Node">
              <Select
                items={nodeIdItems}
                value={onFailure}
                onValueChange={setOnFailure}
                placeholder="— none —"
                size="medium"
              />
            </FormField>
          </Box>
        </Flex>
      </Flex>
    </Modal>
  );
}
