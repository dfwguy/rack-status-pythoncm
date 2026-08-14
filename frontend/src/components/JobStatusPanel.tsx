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
import { useState, useEffect, useRef, useCallback } from "react";
import Box from "@mui/material/Box";
import IconButton from "@mui/material/IconButton";
import { Card, Flex, Tag, Text, Button } from "@nvidia/foundations-react-core";
import { X, ChevronDown, ChevronUp, Clock, Terminal } from "lucide-react";

type TagColor = "blue" | "yellow" | "green" | "red" | "gray";

function phaseColor(phase: string): TagColor {
  switch (phase) {
    case "starting": return "blue";
    case "running": return "yellow";
    case "done": return "green";
    case "error": return "red";
    case "timeout": return "red";
    default: return "gray";
  }
}

function isTerminal(phase: string): boolean {
  return phase === "done" || phase === "error" || phase === "timeout";
}

function formatElapsed(createdAt: string): string {
  const start = new Date(createdAt).getTime();
  if (isNaN(start)) return "—";
  const secs = Math.floor((Date.now() - start) / 1000);
  if (secs < 60) return `${secs}s`;
  const mins = Math.floor(secs / 60);
  const rem = secs % 60;
  if (mins < 60) return `${mins}m ${rem}s`;
  const hrs = Math.floor(mins / 60);
  return `${hrs}h ${mins % 60}m`;
}

interface JobStatusPanelProps {
  jobId: string;
  jobKind: string;
  statusUrl: string;
  onDismiss: () => void;
}

interface JobStatus {
  ok: boolean;
  job_id: string;
  phase: string;
  command_display?: string;
  created_at?: string;
  started_running_at?: string;
  finished_at?: string;
  stdout?: string;
  stderr?: string;
  error?: string;
  rack?: string;
  returncode?: number;
}

export function JobStatusPanel({ jobId, jobKind, statusUrl, onDismiss }: JobStatusPanelProps) {
  const [status, setStatus] = useState<JobStatus | null>(null);
  const [error, setError] = useState("");
  const [showOutput, setShowOutput] = useState(false);
  const [elapsed, setElapsed] = useState("—");
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const elapsedRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const poll = useCallback(async () => {
    try {
      const res = await fetch(`${statusUrl}?job_id=${encodeURIComponent(jobId)}`);
      const json = await res.json();
      setStatus(json);
      setError("");
      if (json.phase && isTerminal(json.phase)) {
        if (pollRef.current) {
          clearInterval(pollRef.current);
          pollRef.current = null;
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Poll failed");
    }
  }, [jobId, statusUrl]);

  useEffect(() => {
    poll();
    pollRef.current = setInterval(poll, 3000);
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [poll]);

  useEffect(() => {
    const tick = () => {
      if (status?.created_at) {
        setElapsed(formatElapsed(status.created_at));
      }
    };
    tick();
    elapsedRef.current = setInterval(tick, 1000);
    return () => {
      if (elapsedRef.current) clearInterval(elapsedRef.current);
    };
  }, [status?.created_at]);

  const phase = status?.phase || "starting";
  const terminal = isTerminal(phase);
  const hasOutput = !!(status?.stdout || status?.stderr);

  return (
    <Card style={{ padding: 16, marginBottom: 16, border: terminal && phase !== "done" ? "1px solid var(--border-color-error, #f44336)" : undefined }}>
      <Flex gap="3" align="start" justify="between">
        <Flex gap="2" align="center" wrap="wrap" style={{ flex: 1 }}>
          <Tag color={phaseColor(phase)} kind="solid" readOnly density="compact">{phase}</Tag>
          <Text kind="body/bold/sm">{jobKind}</Text>
          {status?.rack && status.rack !== "multinode" && (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {status.rack}
            </Text>
          )}
          <Flex gap="1" align="center">
            <Clock size={14} style={{ color: "var(--text-color-subtle)" }} />
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>{elapsed}</Text>
          </Flex>
          {status?.returncode != null && terminal && (
            <Text kind="mono/sm" style={{ color: status.returncode === 0 ? "var(--text-color-secondary)" : "var(--text-color-error, #f44336)" }}>
              exit {status.returncode}
            </Text>
          )}
        </Flex>
        <IconButton size="small" onClick={onDismiss} title="Dismiss">
          <X size={16} />
        </IconButton>
      </Flex>

      {status?.command_display && (
        <Box sx={{ mt: 1 }}>
          <Flex gap="1" align="center">
            <Terminal size={12} style={{ color: "var(--text-color-subtle)" }} />
            <Text kind="mono/sm" style={{ color: "var(--text-color-secondary)" }}>
              {status.command_display}
            </Text>
          </Flex>
        </Box>
      )}

      {status?.error && (
        <Box sx={{ mt: 1 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-error, #f44336)" }}>
            {status.error}
          </Text>
        </Box>
      )}

      {error && (
        <Box sx={{ mt: 1 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-error, #f44336)" }}>
            Poll error: {error}
          </Text>
        </Box>
      )}

      {hasOutput && terminal && (
        <Box sx={{ mt: 1.5 }}>
          <Button kind="secondary" size="tiny" onClick={() => setShowOutput(!showOutput)}>
            {showOutput ? <ChevronUp size={14} /> : <ChevronDown size={14} />}
            <span style={{ marginLeft: 4 }}>{showOutput ? "Hide output" : "Show output"}</span>
          </Button>
          {showOutput && (
            <Box sx={{ mt: 1 }}>
              {status?.stdout && (
                <pre style={{
                  fontFamily: "monospace",
                  fontSize: 13,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-all",
                  margin: 0,
                  padding: 12,
                  borderRadius: 4,
                  backgroundColor: "var(--background-color-surface-base)",
                  maxHeight: 300,
                  overflow: "auto",
                  marginBottom: status?.stderr ? 8 : 0,
                }}>
                  {status.stdout}
                </pre>
              )}
              {status?.stderr && (
                <pre style={{
                  fontFamily: "monospace",
                  fontSize: 13,
                  whiteSpace: "pre-wrap",
                  wordBreak: "break-all",
                  margin: 0,
                  padding: 12,
                  borderRadius: 4,
                  backgroundColor: "var(--background-color-surface-base)",
                  color: "var(--text-color-error, #f44336)",
                  maxHeight: 200,
                  overflow: "auto",
                }}>
                  {status.stderr}
                </pre>
              )}
            </Box>
          )}
        </Box>
      )}

      {!terminal && (
        <Box sx={{ mt: 1 }}>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-subtle)", fontStyle: "italic" }}>
            Polling every 3s...
          </Text>
        </Box>
      )}
    </Card>
  );
}
