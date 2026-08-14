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
import { useState, useEffect, useMemo } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import MuiButton from "@mui/material/Button";
import Autocomplete from "@mui/material/Autocomplete";
import MuiTextField from "@mui/material/TextField";
import Box from "@mui/material/Box";
import { Text, Tag } from "@nvidia/foundations-react-core";
import { Play, Target } from "lucide-react";
import type { WorkflowTarget } from "../../services/workflowService";
import type { RackStatusResponse } from "../../types/api";

interface WorkflowRunDialogProps {
  open: boolean;
  onClose: () => void;
  onRun: (params: Record<string, string>) => void;
  workflowName: string;
  workflowDescription?: string;
  defaultTarget?: WorkflowTarget;
  rackData: RackStatusResponse | null;
}

export default function WorkflowRunDialog({
  open,
  onClose,
  onRun,
  workflowName,
  workflowDescription,
  defaultTarget,
  rackData,
}: WorkflowRunDialogProps) {
  const [selectedRacks, setSelectedRacks] = useState<string[]>([]);
  const [targetOverride, setTargetOverride] = useState("");

  const availableRacks = useMemo(
    () => (rackData?.rows || []).map((r) => r.rack).sort(),
    [rackData],
  );

  useEffect(() => {
    if (open) {
      setSelectedRacks(defaultTarget?.racks || []);
      setTargetOverride("");
    }
  }, [open, defaultTarget]);

  const handleRun = () => {
    const params: Record<string, string> = {};
    const override = targetOverride.trim();
    if (override) {
      params.target = override;
      const rackMatch = override.match(/^([\w]+-r\d+)/);
      if (rackMatch) params.rack = rackMatch[1];
    } else if (selectedRacks.length === 1) {
      params.rack = selectedRacks[0];
    } else if (selectedRacks.length > 1) {
      params.rack = selectedRacks.join(",");
    }
    onRun(params);
    onClose();
  };

  return (
    <Dialog open={open} onClose={onClose} maxWidth="sm" fullWidth>
      <DialogTitle sx={{ display: "flex", alignItems: "center", gap: 1 }}>
        <Play size={20} />
        Run Workflow
      </DialogTitle>
      <DialogContent sx={{ display: "flex", flexDirection: "column", gap: 2.5, pt: 1 }}>
        <Box>
          <Text kind="body/bold/md">{workflowName}</Text>
          {workflowDescription && (
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)", marginTop: 4 }}>
              {workflowDescription}
            </Text>
          )}
        </Box>

        <Box sx={{ display: "flex", alignItems: "center", gap: 1 }}>
          <Target size={16} />
          <Text kind="body/bold/sm">Target</Text>
          {defaultTarget?.racks && defaultTarget.racks.length > 0 && (
            <Tag color="blue" kind="outline" readOnly density="compact">default from template</Tag>
          )}
        </Box>

        <MuiTextField
          size="small"
          label="Target nodes (hostlist)"
          placeholder="e.g. p1-r01-ct[01-02]"
          value={targetOverride}
          onChange={(e) => setTargetOverride(e.target.value)}
          helperText="Specific hostlist passed directly to scripts. Overrides rack selection below."
        />

        <Autocomplete<string, true>
          multiple
          limitTags={3}
          options={availableRacks}
          value={selectedRacks}
          onChange={(_, value) => setSelectedRacks(value)}
          disabled={!!targetOverride.trim()}
          renderInput={(params) => (
            <MuiTextField
              {...params}
              size="small"
              label="Racks"
              placeholder={selectedRacks.length === 0 ? "Select racks" : ""}
              helperText={targetOverride.trim()
                ? "Disabled — using target nodes above"
                : "Auto-generates target hostlist (rack-ct[01-18]) for scripts"}
            />
          )}
          noOptionsText={rackData ? "No racks available" : "Loading rack data…"}
        />

        {!targetOverride.trim() && selectedRacks.length === 0 && (
          <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
            No target specified. Type a hostlist above or select racks.
          </Text>
        )}
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <MuiButton variant="outlined" onClick={onClose}>Cancel</MuiButton>
        <MuiButton variant="contained" onClick={handleRun} startIcon={<Play size={16} />}>
          Run
        </MuiButton>
      </DialogActions>
    </Dialog>
  );
}
