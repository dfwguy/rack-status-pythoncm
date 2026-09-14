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
// ActionCard — a hard-coded rack control, wearing the board's card.
// Author: Rafael M. Koike & Cursor Agent ("Ada")
//
// These twenty-odd controls are deliberately *not* catalog widgets: powering on a tray or
// draining a node is not a tool somebody adds, removes or versions, so there is no document to
// drive and no board to arrange. What they share with a widget is the picture, and that comes
// from `ActionCardFace` rather than from a second copy of the same markup.
//
// What stays here is what a rack control means, and both halves have bitten before:
//
//   - **the node confirmation.** Drain, BMC recovery and GPU reset apply to whatever is
//     selected, and the selection lives in another pane. The dialog lists the exact hostnames
//     before anything runs, because "18 nodes" and "these 18 nodes" are not the same sentence.
//   - **destructive styling.** Without an explicit colour a destructive action renders in the
//     neutral palette — identical to a harmless one. Cancel and Drain must look like what
//     they are.
//
// The description is a single ellipsised line on the card, with the full text in Details. That
// is not a truncation we settled for: several of these descriptions are three or four lines
// (NVLink preflight's is a paragraph), and a card whose height depends on how much prose it
// carries makes a row of cards visibly ragged.
import { useState } from "react";
import type { ReactNode } from "react";
import { Badge, Button as KuiButton, Tag, Text } from "@nvidia/foundations-react-core";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Button from "@mui/material/Button";
import List from "@mui/material/List";
import ListItem from "@mui/material/ListItem";
import ListItemText from "@mui/material/ListItemText";
import Box from "@mui/material/Box";
import { ActionCardFace } from "../widgets/ActionCardFace";
import { CATEGORY_COLORS } from "../widgets/toolImages";
import { withoutBubbling } from "../widgets/cardInteraction";
import { useOrientationFromGrid } from "../widgets/useCardOrientation";
import type { CardOrientation } from "../widgets/widgetLayout";

export type ActionStatus = "idle" | "running" | "success" | "error";

interface ActionCardProps {
  title: string;
  description: string;
  /**
   * Badge label and artwork, matching the catalog's tool categories.
   *
   * Passed per card rather than per tab because it describes the action, not where it is
   * drawn: the Services tab's Tail Log is maintenance, its System Info is hardware.
   */
  category: string;
  /** One extra ellipsised line — a version, a target, a scope. */
  subtitle?: string;
  icon?: ReactNode;
  buttonLabel?: string;
  status?: ActionStatus;
  onRun?: () => void | Promise<void>;
  disabled?: boolean;
  destructive?: boolean;
  selectedNodes?: Set<string>;
  /** Called immediately before the confirm dialog opens (when nodes are selected). */
  onBeforeConfirmDialog?: () => void;
  /** Overrides the enclosing `ActionCardGrid`; normally left unset. */
  orientation?: CardOrientation;
  /** Extra rows for the Details modal — a target list, a caveat, a link. */
  details?: ReactNode;
}

function StatusTag({ status }: { status: ActionStatus }) {
  if (status === "running")
    return (
      <Tag color="yellow" kind="solid" readOnly density="compact">
        Running...
      </Tag>
    );
  if (status === "success")
    return (
      <Tag color="green" kind="solid" readOnly density="compact">
        Done
      </Tag>
    );
  if (status === "error")
    return (
      <Tag color="red" kind="solid" readOnly density="compact">
        Error
      </Tag>
    );
  return null;
}

export function ActionCard({
  title,
  description,
  category,
  subtitle,
  icon,
  buttonLabel = "Run",
  status = "idle",
  onRun,
  disabled,
  destructive,
  selectedNodes,
  onBeforeConfirmDialog,
  orientation,
  details,
}: ActionCardProps) {
  const fromGrid = useOrientationFromGrid();
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [detailsOpen, setDetailsOpen] = useState(false);
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
      <ActionCardFace
        title={title}
        subtitle={subtitle}
        description={description}
        category={category}
        orientation={orientation ?? fromGrid}
        selected={effectiveRunning}
        status={<StatusTag status={effectiveRunning ? "running" : status} />}
        onDetails={() => setDetailsOpen(true)}
        actions={
          <KuiButton
            kind={destructive ? "primary" : "secondary"}
            color={destructive ? "danger" : "brand"}
            size="small"
            onClick={withoutBubbling(handleClick)}
            disabled={disabled || effectiveRunning}
          >
            {effectiveRunning ? "Running..." : buttonLabel}
          </KuiButton>
        }
      />

      <Dialog open={detailsOpen} onClose={() => setDetailsOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontWeight: 600, display: "flex", alignItems: "center", gap: 1.5 }}>
          {/* The icon each call site already passes. It is redundant beside the artwork on the
              card itself, but it reads well here, so the prop keeps earning its place. */}
          {icon ? <Box sx={{ display: "inline-flex", color: "text.secondary" }}>{icon}</Box> : null}
          {title}
        </DialogTitle>
        <DialogContent dividers>
          <Box sx={{ mb: 2 }}>
            <Badge kind="solid" color={CATEGORY_COLORS[category] ?? "gray"}>
              {category}
            </Badge>
          </Box>
          {/* The whole description, wrapped — this modal is the reason the card can afford to
              show one line of it. */}
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            {description}
          </Text>
          {details ? <Box sx={{ mt: 2 }}>{details}</Box> : null}
          {destructive && (
            <Text
              kind="body/regular/sm"
              style={{ color: "var(--text-color-error, #f44336)", display: "block", marginTop: 16 }}
            >
              This is a destructive operation.
            </Text>
          )}
        </DialogContent>
        <DialogActions sx={{ px: 3, py: 2 }}>
          <Button onClick={() => setDetailsOpen(false)} color="inherit">
            Close
          </Button>
        </DialogActions>
      </Dialog>

      <Dialog open={confirmOpen} onClose={() => setConfirmOpen(false)} maxWidth="sm" fullWidth>
        <DialogTitle sx={{ fontWeight: 600 }}>{title}</DialogTitle>
        <DialogContent dividers>
          <Text
            kind="body/regular/sm"
            style={{ color: "var(--text-color-secondary)", marginBottom: 16 }}
          >
            This action will be applied to the following {sortedNodes.length} node(s):
          </Text>
          <List
            dense
            sx={{
              bgcolor: "background.default",
              borderRadius: 1,
              maxHeight: 240,
              overflow: "auto",
            }}
          >
            {sortedNodes.map((node) => (
              <ListItem key={node} sx={{ py: 0.25 }}>
                <ListItemText
                  primary={node}
                  slotProps={{ primary: { sx: { fontFamily: "monospace", fontSize: 14 } } }}
                />
              </ListItem>
            ))}
          </List>
          {destructive && (
            <Text
              kind="body/regular/sm"
              style={{ color: "var(--text-color-error, #f44336)", marginTop: 16 }}
            >
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
