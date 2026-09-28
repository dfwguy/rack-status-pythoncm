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
import { useRef, useCallback } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import IconButton from "@mui/material/IconButton";
import Tooltip from "@mui/material/Tooltip";
import Box from "@mui/material/Box";
import { Text, Tag } from "@nvidia/foundations-react-core";
import { Terminal as TerminalIcon, ExternalLink } from "lucide-react";
import { TERMINAL_BACKGROUND, useTerminal } from "../../hooks/useTerminal";
import { openApiUrl } from "../../lib/apiFetch";

export interface WebTerminalDialogProps {
  open: boolean;
  onClose: () => void;
  /** Target identifier shown in the UI and sent as the `node` query param. */
  target: string;
  /** Optional label override for the dialog title (defaults to target). */
  label?: string;
  /** "node" = SSH hop via head node to compute node (default).
   *  "headnode" = SSH directly to the head node. */
  mode?: "node" | "headnode";
}

export default function WebTerminalDialog({
  open,
  onClose,
  target,
  label,
  mode = "node",
}: WebTerminalDialogProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const displayName = label || target;

  const { sessionId } = useTerminal({
    containerRef,
    target,
    mode,
    label: displayName,
    active: open && !!target,
  });

  const handlePopOut = useCallback(() => {
    const params = new URLSearchParams();
    if (sessionId) {
      params.set("session", sessionId);
    } else {
      params.set("target", target);
      params.set("mode", mode);
    }
    params.set("label", displayName);
    void openApiUrl(`/terminal?${params}`);
    onClose();
  }, [sessionId, target, mode, displayName, onClose]);

  return (
    <Dialog
      open={open}
      onClose={onClose}
      maxWidth="lg"
      fullWidth
      slotProps={{ paper: { sx: { height: "80vh", maxHeight: "80vh", display: "flex", flexDirection: "column" } } }}
    >
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1, py: 1.5, flexShrink: 0 }}>
        <TerminalIcon size={20} />
        <Text kind="body/bold/md">Terminal</Text>
        <Tag color="green" kind="outline" readOnly density="compact">{displayName}</Tag>
        <Box sx={{ flex: 1 }} />
        <Tooltip title="Open in new tab">
          <IconButton size="small" onClick={handlePopOut} sx={{ color: "inherit" }}>
            <ExternalLink size={18} />
          </IconButton>
        </Tooltip>
      </DialogTitle>
      <DialogContent sx={{ p: 0, overflow: "hidden", bgcolor: TERMINAL_BACKGROUND, flex: 1, display: "flex" }}>
        {/* Inset on the outer box, terminal mounted in an unpadded child — padding the element
            xterm opens on does not shrink what FitAddon measures, so it clips the last line. */}
        <Box sx={{ width: "100%", flex: 1, minHeight: 0, p: 0.5, display: "flex" }}>
          <Box ref={containerRef} sx={{ flex: 1, minHeight: 0, minWidth: 0, overflow: "hidden" }} />
        </Box>
      </DialogContent>
      <DialogActions>
        <MuiButton variant="outlined" onClick={onClose}>Disconnect</MuiButton>
      </DialogActions>
    </Dialog>
  );
}
