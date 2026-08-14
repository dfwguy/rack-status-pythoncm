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
import { useState } from "react";
import { Card, Flex, Tag, Text, Button as KuiButton } from "@nvidia/foundations-react-core";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Button from "@mui/material/Button";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemText from "@mui/material/ListItemText";
import type { ReactNode } from "react";

export type ActionStatus = "idle" | "running" | "success" | "error";

interface ActionCardProps {
  title: string;
  description: string;
  icon?: ReactNode;
  buttonLabel?: string;
  status?: ActionStatus;
  onRun?: () => void | Promise<void>;
  disabled?: boolean;
  destructive?: boolean;
  selectedNodes?: Set<string>;
  /** Called immediately before the confirm dialog opens (when nodes are selected). */
  onBeforeConfirmDialog?: () => void;
}

function StatusTag({ status }: { status: ActionStatus }) {
  if (status === "idle") return null;
  if (status === "running") return <Tag color="yellow" kind="solid" readOnly density="compact">Running...</Tag>;
  if (status === "success") return <Tag color="green" kind="solid" readOnly density="compact">Done</Tag>;
  if (status === "error") return <Tag color="red" kind="solid" readOnly density="compact">Error</Tag>;
  return null;
}

export function ActionCard({
  title,
  description,
  icon,
  buttonLabel = "Run",
  status = "idle",
  onRun,
  disabled,
  destructive,
  selectedNodes,
  onBeforeConfirmDialog,
}: ActionCardProps) {
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [localRunning, setLocalRunning] = useState(false);
  const effectiveRunning = status === "running" || localRunning;

  const invokeRun = () => {
    const ret = onRun?.();
    if (ret && typeof (ret as Promise<void>).then === "function") {
      setLocalRunning(true);
      (ret as Promise<void>).finally(() => setLocalRunning(false));
    }
  };

  const handleClick = () => {
    if (selectedNodes && selectedNodes.size > 0) {
      onBeforeConfirmDialog?.();
      setConfirmOpen(true);
    } else {
      invokeRun();
    }
  };

  const handleConfirm = () => {
    setConfirmOpen(false);
    invokeRun();
  };

  const sortedNodes = selectedNodes
    ? [...selectedNodes].sort((a, b) => a.localeCompare(b, undefined, { numeric: true }))
    : [];

  return (
    <>
      <Card style={{ padding: 20 }}>
        <Flex gap="3" align="start">
          {icon && (
            <div style={{ color: "var(--text-color-secondary)", marginTop: 2 }}>{icon}</div>
          )}
          <div style={{ flex: 1, minWidth: 0 }}>
            <Flex gap="2" align="center" style={{ marginBottom: 4 }}>
              <Text kind="body/bold/md">{title}</Text>
              <StatusTag status={effectiveRunning ? "running" : status} />
            </Flex>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              {description}
            </Text>
          </div>
          <KuiButton
            kind={destructive ? "primary" : "secondary"}
            // Without an explicit colour a destructive action renders in the neutral palette,
            // i.e. identical to a harmless one. Cancel/Drain must look like what they are.
            color={destructive ? "danger" : "neutral"}
            size="small"
            onClick={handleClick}
            disabled={disabled || effectiveRunning}
          >
            {effectiveRunning ? "Running..." : buttonLabel}
          </KuiButton>
        </Flex>
      </Card>

      <Dialog
        open={confirmOpen}
        onClose={() => setConfirmOpen(false)}
        maxWidth="sm"
        fullWidth
      >
        <DialogTitle sx={{ fontWeight: 600 }}>
          {title}
        </DialogTitle>
        <DialogContent dividers>
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginBottom: 16 }}>
            This action will be applied to the following {sortedNodes.length} node(s):
          </Text>
          <List dense sx={{ bgcolor: "background.default", borderRadius: 1, maxHeight: 240, overflow: "auto" }}>
            {sortedNodes.map((node) => (
              <ListItem key={node} sx={{ py: 0.25 }}>
                <ListItemText
                  primary={node}
                  slotProps={{
                    primary: {
                      sx: { fontFamily: "monospace", fontSize: 14 },
                    },
                  }}
                />
              </ListItem>
            ))}
          </List>
          {destructive && (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-error, #f44336)", marginTop: 16 }}>
              This is a destructive operation. Please confirm you want to proceed.
            </Text>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={() => setConfirmOpen(false)} color="inherit">
            Cancel
          </Button>
          <Button
            onClick={handleConfirm}
            variant="contained"
            color={destructive ? "error" : "primary"}
          >
            {buttonLabel}
          </Button>
        </DialogActions>
      </Dialog>
    </>
  );
}
