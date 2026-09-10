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
import { apiFetch } from "../../lib/apiFetch";

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

/** Drop empty fields from a hop spec; return undefined if nothing meaningful is set. */
function cleanHop(h: Record<string, unknown>): Record<string, unknown> | undefined {
  const out: Record<string, unknown> = {};
  const kind = (h.kind as string) || "ssh";
  if (kind && kind !== "ssh") out.kind = kind;
  for (const k of ["host", "user", "key", "port"] as const) {
    const v = (h[k] as string | undefined)?.trim?.() ?? h[k];
    if (v) out[k] = v;
  }
  if (h.sudo === true) out.sudo = true;
  // keep an explicit kind so the shape is unambiguous
  if (Object.keys(out).length === 0) return kind === "ssh" ? undefined : { kind };
  out.kind = kind;
  return out;
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
  // Composition axes.
  const conn = (metadata.connection || {}) as Record<string, unknown>;
  const sched = (metadata.scheduler || {}) as Record<string, unknown>;
  const [capability, setCapability] = useState(metadata.capability || "");
  const [runScript, setRunScript] = useState(metadata.run || "");
  const [connProfile, setConnProfile] = useState((conn.profile as string) || "");
  const [connSudo, setConnSudo] = useState(conn.sudo === true ? "true" : conn.sudo === false ? "false" : "");
  const [schedKind, setSchedKind] = useState((sched.kind as string) || "");
  const [schedScript, setSchedScript] = useState((sched.script as string) || "");
  const [schedNodelist, setSchedNodelist] = useState((sched.nodelist as string) || "");
  const [schedNtasks, setSchedNtasks] = useState(sched.ntasks_per_node != null ? String(sched.ntasks_per_node) : "");
  const [schedNodes, setSchedNodes] = useState(sched.nodes != null ? String(sched.nodes) : "");
  const [evalText, setEvalText] = useState(metadata.evaluation ? JSON.stringify(metadata.evaluation, null, 2) : "");
  const [evalError, setEvalError] = useState("");
  // Fan-out: run this step's command across N nodes over an N-level connection chain.
  const fo = (metadata.fanout || {}) as Record<string, unknown>;
  const [fanoutOn, setFanoutOn] = useState(!!metadata.fanout);
  const [foNodes, setFoNodes] = useState((fo.nodes as string) || "${nodelist}");
  const [foMaxWorkers, setFoMaxWorkers] = useState(fo.max_workers != null ? String(fo.max_workers) : "");
  const [foHops, setFoHops] = useState<Record<string, unknown>[]>(
    Array.isArray(fo.hops) ? (fo.hops as Record<string, unknown>[]) : [],
  );
  const [foNode, setFoNode] = useState<Record<string, unknown>>((fo.node as Record<string, unknown>) || { kind: "ssh" });

  // Fetch available tools on mount
  useEffect(() => {
    apiFetch("/api/tools/available", { credentials: "same-origin" })
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
    const c = (metadata.connection || {}) as Record<string, unknown>;
    const s = (metadata.scheduler || {}) as Record<string, unknown>;
    setCapability(metadata.capability || "");
    setRunScript(metadata.run || "");
    setConnProfile((c.profile as string) || "");
    setConnSudo(c.sudo === true ? "true" : c.sudo === false ? "false" : "");
    setSchedKind((s.kind as string) || "");
    setSchedScript((s.script as string) || "");
    setSchedNodelist((s.nodelist as string) || "");
    setSchedNtasks(s.ntasks_per_node != null ? String(s.ntasks_per_node) : "");
    setSchedNodes(s.nodes != null ? String(s.nodes) : "");
    setEvalText(metadata.evaluation ? JSON.stringify(metadata.evaluation, null, 2) : "");
    setEvalError("");
    const f = (metadata.fanout || {}) as Record<string, unknown>;
    setFanoutOn(!!metadata.fanout);
    setFoNodes((f.nodes as string) || "${nodelist}");
    setFoMaxWorkers(f.max_workers != null ? String(f.max_workers) : "");
    setFoHops(Array.isArray(f.hops) ? (f.hops as Record<string, unknown>[]) : []);
    setFoNode((f.node as Record<string, unknown>) || { kind: "ssh" });
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

  const addHop = useCallback(() => setFoHops((h) => [...h, { kind: "ssh" }]), []);
  const removeHop = useCallback((idx: number) => setFoHops((h) => h.filter((_, i) => i !== idx)), []);
  const patchHop = useCallback(
    (idx: number, patch: Record<string, unknown>) =>
      setFoHops((h) => h.map((row, i) => (i === idx ? { ...row, ...patch } : row))),
    [],
  );

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

    const connection: Record<string, unknown> = {};
    if (connProfile) connection.profile = connProfile;
    if (connSudo) connection.sudo = connSudo === "true";

    const scheduler: Record<string, unknown> = {};
    if (schedKind) scheduler.kind = schedKind;
    if (schedScript.trim()) scheduler.script = schedScript.trim();
    if (schedNodelist.trim()) scheduler.nodelist = schedNodelist.trim();
    if (schedNtasks.trim()) scheduler.ntasks_per_node = schedNtasks.trim();
    if (schedNodes.trim()) scheduler.nodes = schedNodes.trim();

    let evaluation: Record<string, unknown> | undefined;
    if (evalText.trim()) {
      try {
        evaluation = JSON.parse(evalText);
      } catch {
        setEvalError("Evaluation must be valid JSON");
        return;
      }
    }

    let fanout: Record<string, unknown> | undefined;
    if (fanoutOn) {
      fanout = {};
      if (foNodes.trim()) fanout.nodes = foNodes.trim();
      if (foMaxWorkers.trim()) fanout.max_workers = parseInt(foMaxWorkers, 10);
      const hops = foHops.map(cleanHop).filter(Boolean) as Record<string, unknown>[];
      if (hops.length) fanout.hops = hops;
      const node = cleanHop(foNode);
      if (node) fanout.node = node;
    }

    const updated: NodeMetadata = {
      name: name.trim() || undefined,
      description: description.trim() || undefined,
      type: type as NodeMetadata["type"],
      command: command.trim() || undefined,
      capability: capability.trim() || undefined,
      run: runScript.trim() || undefined,
      fanout,
      parameters: Object.keys(parameters).length > 0 ? parameters : undefined,
      output: output.trim() || undefined,
      timeout: timeout ? parseInt(timeout, 10) : undefined,
      retries: retries ? parseInt(retries, 10) : undefined,
      on_success: onSuccess.trim() || undefined,
      on_failure: onFailure.trim() || undefined,
      connection: Object.keys(connection).length > 0 ? connection : undefined,
      scheduler: Object.keys(scheduler).length > 0 ? scheduler : undefined,
      evaluation,
    };

    const newLabel = name.trim() !== nodeLabel ? name.trim() : undefined;
    onSave(nodeId, updated, newLabel);
    onOpenChange(false);
  }, [nodeId, nodeLabel, name, description, type, command, capability, params, output, timeout,
      retries, onSuccess, onFailure, runScript, connProfile, connSudo, schedKind, schedScript,
      schedNodelist, schedNtasks, schedNodes, evalText, fanoutOn, foNodes, foMaxWorkers, foHops,
      foNode, onSave, onOpenChange]);

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
          <ModalCloseButton kind="secondary">Cancel</ModalCloseButton>
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
              <Button kind="secondary" size="small" onClick={() => { setUseCustomCommand(false); setCommand(""); }}>
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

        {/* Composition: Connection */}
        <Box sx={{ borderTop: "1px solid var(--border-color-subtle, #333)", pt: 2 }}>
          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8 }}>Connection</Text>
          <Flex gap="2">
            <Box sx={{ flex: 1 }}>
              <FormField name="node-conn-profile" slotLabel="Profile" slotHelp="Route (control = head; script = privileged)">
                <Select
                  items={[
                    { value: "", children: "— default (control) —" },
                    { value: "control", children: "control" },
                    { value: "script", children: "script (privileged)" },
                  ]}
                  value={connProfile}
                  onValueChange={setConnProfile}
                  size="medium"
                />
              </FormField>
            </Box>
            <Box sx={{ flex: 1 }}>
              <FormField name="node-conn-sudo" slotLabel="Sudo">
                <Select
                  items={[
                    { value: "", children: "— default —" },
                    { value: "true", children: "sudo on" },
                    { value: "false", children: "sudo off" },
                  ]}
                  value={connSudo}
                  onValueChange={setConnSudo}
                  size="medium"
                />
              </FormField>
            </Box>
          </Flex>
        </Box>

        {/* Composition: Scheduler */}
        <Box sx={{ borderTop: "1px solid var(--border-color-subtle, #333)", pt: 2 }}>
          <Text kind="body/bold/sm" style={{ display: "block", marginBottom: 8 }}>Scheduler (run across nodes)</Text>
          <FormField name="node-sched-kind" slotLabel="Kind" slotHelp="Leave as none for a plain command step">
            <Select
              items={[
                { value: "", children: "— none —" },
                { value: "slurm", children: "slurm" },
                { value: "local", children: "local" },
                { value: "runai", children: "run:AI (stub)" },
                { value: "slinky", children: "slinky (stub)" },
                { value: "maas", children: "MaaS (stub)" },
              ]}
              value={schedKind}
              onValueChange={setSchedKind}
              size="medium"
            />
          </FormField>
          {schedKind && (
            <>
              <FormField name="node-sched-script" slotLabel="Script / batch file" slotHelp="e.g. nccl_allreduce.sbatch (resolved under bin/scripts)">
                <TextInput value={schedScript} onValueChange={setSchedScript} placeholder="nccl_allreduce.sbatch" />
              </FormField>
              <Flex gap="2">
                <Box sx={{ flex: 2 }}>
                  <FormField name="node-sched-nodelist" slotLabel="Nodelist">
                    <TextInput value={schedNodelist} onValueChange={setSchedNodelist} placeholder="${target}" />
                  </FormField>
                </Box>
                <Box sx={{ flex: 1 }}>
                  <FormField name="node-sched-ntasks" slotLabel="Tasks/node">
                    <TextInput value={schedNtasks} onValueChange={setSchedNtasks} placeholder="${gpus_per_node}" />
                  </FormField>
                </Box>
                <Box sx={{ flex: 1 }}>
                  <FormField name="node-sched-nodes" slotLabel="Nodes">
                    <TextInput value={schedNodes} onValueChange={setSchedNodes} placeholder="(auto)" />
                  </FormField>
                </Box>
              </Flex>
            </>
          )}
        </Box>

        {/* Composition: Fan-out (run `run` across N nodes over an N-level connection chain) */}
        <Box sx={{ borderTop: "1px solid var(--border-color-subtle, #333)", pt: 2 }}>
          <Flex align="center" justify="between" style={{ marginBottom: 8 }}>
            <Text kind="body/bold/sm">Fan-out (run across N nodes)</Text>
            <Select
              items={[{ value: "off", children: "Off" }, { value: "on", children: "On" }]}
              value={fanoutOn ? "on" : "off"}
              onValueChange={(v) => setFanoutOn(v === "on")}
              size="small"
            />
          </Flex>
          {fanoutOn && (
            <>
              <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", display: "block", marginBottom: 8 }}>
                Runs the step's <b>Run</b> command on each node. The connection chain is app → hops… → each node;
                each hop has its own transport (ssh/local) + sudo. Leave hops empty to use the app's configured head connection.
              </Text>
              <Flex gap="2">
                <Box sx={{ flex: 2 }}>
                  <FormField name="fo-nodes" slotLabel="Nodes">
                    <TextInput value={foNodes} onValueChange={setFoNodes} placeholder="${nodelist}" />
                  </FormField>
                </Box>
                <Box sx={{ flex: 1 }}>
                  <FormField name="fo-workers" slotLabel="Max parallel">
                    <TextInput value={foMaxWorkers} onValueChange={setFoMaxWorkers} placeholder="32" />
                  </FormField>
                </Box>
              </Flex>

              <Flex align="center" justify="between" style={{ margin: "8px 0" }}>
                <Text kind="body/bold/sm">Connection hops (app → … → gateway)</Text>
                <IconButton size="small" onClick={addHop} title="Add hop">
                  <Plus size={16} />
                </IconButton>
              </Flex>
              {foHops.length === 0 && (
                <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
                  No hops → uses the app's configured head connection. Add hops for a VPN/bastion → head chain.
                </Text>
              )}
              {foHops.map((h, idx) => (
                <Flex key={idx} gap="2" align="end" style={{ marginBottom: 6 }}>
                  <Box sx={{ width: 96 }}>
                    <Select
                      items={[{ value: "ssh", children: "ssh" }, { value: "local", children: "local" }, { value: "vpn", children: "vpn" }]}
                      value={(h.kind as string) || "ssh"}
                      onValueChange={(v) => patchHop(idx, { kind: v })}
                      size="small"
                    />
                  </Box>
                  <Box sx={{ flex: 2 }}>
                    <TextInput value={(h.host as string) || ""} onValueChange={(v) => patchHop(idx, { host: v })} placeholder="host (e.g. ${head})" size="small" />
                  </Box>
                  <Box sx={{ flex: 1 }}>
                    <TextInput value={(h.user as string) || ""} onValueChange={(v) => patchHop(idx, { user: v })} placeholder="user" size="small" />
                  </Box>
                  <Box sx={{ width: 90 }}>
                    <Select
                      items={[{ value: "no", children: "sudo off" }, { value: "yes", children: "sudo on" }]}
                      value={h.sudo === true ? "yes" : "no"}
                      onValueChange={(v) => patchHop(idx, { sudo: v === "yes" })}
                      size="small"
                    />
                  </Box>
                  <IconButton size="small" onClick={() => removeHop(idx)} title="Remove hop">
                    <Trash2 size={14} />
                  </IconButton>
                </Flex>
              ))}

              <Text kind="body/bold/sm" style={{ display: "block", margin: "10px 0 6px" }}>Per-node hop</Text>
              <Flex gap="2" align="end">
                <Box sx={{ width: 96 }}>
                  <Select
                    items={[{ value: "ssh", children: "ssh" }, { value: "local", children: "local" }]}
                    value={(foNode.kind as string) || "ssh"}
                    onValueChange={(v) => setFoNode({ ...foNode, kind: v })}
                    size="small"
                  />
                </Box>
                <Box sx={{ flex: 1 }}>
                  <TextInput value={(foNode.user as string) || ""} onValueChange={(v) => setFoNode({ ...foNode, user: v })} placeholder="user (e.g. cmods)" size="small" />
                </Box>
                <Box sx={{ flex: 1 }}>
                  <TextInput value={(foNode.key as string) || ""} onValueChange={(v) => setFoNode({ ...foNode, key: v })} placeholder="key (optional)" size="small" />
                </Box>
                <Box sx={{ width: 90 }}>
                  <Select
                    items={[{ value: "no", children: "sudo off" }, { value: "yes", children: "sudo on" }]}
                    value={foNode.sudo === true ? "yes" : "no"}
                    onValueChange={(v) => setFoNode({ ...foNode, sudo: v === "yes" })}
                    size="small"
                  />
                </Box>
              </Flex>
            </>
          )}
        </Box>

        {/* Composition: Capability + Evaluation */}
        <FormField name="node-capability" slotLabel="Capability (catalog tool id)" slotHelp="Optional: run a registered tool by id instead of a raw command">
          <TextInput value={capability} onValueChange={setCapability} placeholder="e.g. sysinfo" />
        </FormField>

        <FormField name="node-run" slotLabel="Run (inline batch script / command)" slotHelp="Optional: a shell script/command run on the connection. Supports ${nodelist}, ${scripts_dir}, ${rack}. Exit non-zero (or use Evaluation) to fail the step.">
          <textarea
            value={runScript}
            onChange={(e) => setRunScript(e.target.value)}
            placeholder={"pdsh -R ssh -w ${nodelist} <<'EOF' | dshbak -c\nnvidia-smi -L\nEOF"}
            rows={5}
            style={{
              width: "100%", fontFamily: "monospace", fontSize: 12, padding: 8,
              background: "var(--surface-color, #1a1a1a)", color: "var(--text-color, #eee)",
              border: "1px solid var(--border-color-subtle, #333)", borderRadius: 4,
            }}
          />
        </FormField>

        <FormField name="node-evaluation" slotLabel="Evaluation (JSON)" slotHelp='e.g. {"extract":[{"name":"bw","regex":"busbw ([0-9.]+)","type":"float"}],"assert":["bw >= 300"]}'>
          <textarea
            value={evalText}
            onChange={(e) => { setEvalText(e.target.value); setEvalError(""); }}
            placeholder='{"success_when":{"exit_code":0},"assert":["bw >= 300"]}'
            rows={4}
            style={{
              width: "100%", fontFamily: "monospace", fontSize: 12, padding: 8,
              background: "var(--surface-color, #1a1a1a)", color: "var(--text-color, #eee)",
              border: "1px solid var(--border-color-subtle, #333)", borderRadius: 4,
            }}
          />
        </FormField>
        {evalError && (
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-danger, #f66)" }}>{evalError}</Text>
        )}

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
