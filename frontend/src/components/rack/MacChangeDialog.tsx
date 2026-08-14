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
import { useState, useMemo, useCallback } from "react";
import Dialog from "@mui/material/Dialog";
import DialogTitle from "@mui/material/DialogTitle";
import DialogContent from "@mui/material/DialogContent";
import DialogActions from "@mui/material/DialogActions";
import Button from "@mui/material/Button";
import TextField from "@mui/material/TextField";
import IconButton from "@mui/material/IconButton";
import Checkbox from "@mui/material/Checkbox";
import FormControlLabel from "@mui/material/FormControlLabel";
import Stack from "@mui/material/Stack";
import Box from "@mui/material/Box";
import { Text } from "@nvidia/foundations-react-core";
import { Trash2, Plus } from "lucide-react";
import { useRackCommands } from "../../context/RackCommandContext";
import { macChangeBody, type MacInterfaceRow } from "../../lib/rackCommandBodies";

const MAC_RE = /^[0-9A-Fa-f]{2}(:[0-9A-Fa-f]{2}){5}$/;
const IFACE_NAME_RE = /^[A-Za-z0-9._-]{1,32}$/;
const DEFAULT_ROWS = 3;

interface MacChangeDialogProps {
  open: boolean;
  rack: string;
  node: string;
  onClose: () => void;
}

interface RowState extends MacInterfaceRow {
  id: number;
}

let rowIdCounter = 0;
function makeRow(name = "", mac = ""): RowState {
  rowIdCounter += 1;
  return { id: rowIdCounter, name, mac };
}
function makeDefaultRows(): RowState[] {
  return Array.from({ length: DEFAULT_ROWS }, () => makeRow());
}

function isRowEmpty(r: RowState): boolean {
  return !r.name.trim() && !r.mac.trim();
}
function rowNameError(r: RowState): string {
  const v = r.name.trim();
  if (isRowEmpty(r)) return "";
  if (!v) return "name required";
  if (!IFACE_NAME_RE.test(v)) return "letters/digits/._- only";
  return "";
}
function rowMacError(r: RowState): string {
  const v = r.mac.trim();
  if (isRowEmpty(r)) return "";
  if (!v) return "MAC required";
  if (!MAC_RE.test(v)) return "aa:bb:cc:dd:ee:ff";
  return "";
}
function nodeMacError(value: string): string {
  const v = value.trim();
  if (!v) return "";
  if (!MAC_RE.test(v)) return "aa:bb:cc:dd:ee:ff";
  return "";
}

export function MacChangeDialog({ open, rack, node, onClose }: MacChangeDialogProps) {
  const { runAsyncReset } = useRackCommands();
  const [nodeMac, setNodeMac] = useState("");
  const [rows, setRows] = useState<RowState[]>(makeDefaultRows);
  const [dryRun, setDryRun] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const reset = useCallback(() => {
    setNodeMac("");
    setRows(makeDefaultRows());
    setDryRun(false);
    setSubmitting(false);
  }, []);

  const handleClose = useCallback(() => {
    if (submitting) return;
    onClose();
    reset();
  }, [onClose, reset, submitting]);

  const setRow = useCallback((id: number, patch: Partial<MacInterfaceRow>) => {
    setRows((prev) => prev.map((r) => (r.id === id ? { ...r, ...patch } : r)));
  }, []);

  const addRow = useCallback(() => {
    setRows((prev) => [...prev, makeRow()]);
  }, []);

  const removeRow = useCallback((id: number) => {
    setRows((prev) => (prev.length === 1 ? prev : prev.filter((r) => r.id !== id)));
  }, []);

  const nodeMacErr = useMemo(() => nodeMacError(nodeMac), [nodeMac]);

  const validFilledRows = useMemo(
    () => rows.filter((r) => !isRowEmpty(r) && !rowNameError(r) && !rowMacError(r)),
    [rows]
  );

  const anyRowInvalid = useMemo(
    () => rows.some((r) => !!rowNameError(r) || !!rowMacError(r)),
    [rows]
  );

  const hasNodeMac = nodeMac.trim().length > 0 && !nodeMacErr;
  const canSubmit = !submitting && !nodeMacErr && !anyRowInvalid && (hasNodeMac || validFilledRows.length > 0);

  const handleRun = useCallback(async () => {
    if (!canSubmit) return;
    setSubmitting(true);
    const body = macChangeBody(
      rack,
      undefined,
      node,
      nodeMac.trim(),
      validFilledRows.map((r) => ({ name: r.name.trim(), mac: r.mac.trim() })),
      dryRun,
    );
    onClose();
    try {
      await runAsyncReset(
        dryRun ? "MAC change (dry-run)" : "MAC change",
        "mac",
        "/api/mac-change-async",
        body,
      );
    } finally {
      reset();
    }
  }, [canSubmit, dryRun, node, nodeMac, onClose, rack, reset, runAsyncReset, validFilledRows]);

  return (
    <Dialog open={open} onClose={handleClose} maxWidth="md" fullWidth>
      <DialogTitle sx={{ fontWeight: 600 }}>
        MAC Change — {node}
      </DialogTitle>
      <DialogContent dividers>
        <Stack spacing={2.5}>
          <Box>
            <Text kind="body/regular/sm" style={{ color: "var(--text-color-secondary)" }}>
              Change one or both: the device-level MAC and/or per-interface MACs.
              If the new MAC is in use by exactly one other node on this cluster,
              macswap will automatically swap them.
            </Text>
          </Box>

          <TextField
            label="Device MAC (optional)"
            placeholder="aa:bb:cc:dd:ee:ff"
            value={nodeMac}
            onChange={(e) => setNodeMac(e.target.value)}
            error={!!nodeMacErr}
            helperText={nodeMacErr || "Leave blank to keep the existing device-level MAC"}
            size="small"
            slotProps={{ htmlInput: { style: { fontFamily: "monospace" } } }}
            fullWidth
          />

          <Box>
            <Text kind="body/bold/sm" style={{ marginBottom: 8, display: "block" }}>
              Interfaces
            </Text>
            <Stack spacing={1.25}>
              {rows.map((row) => {
                const nameErr = rowNameError(row);
                const macErr = rowMacError(row);
                return (
                  <Stack key={row.id} direction="row" spacing={1} sx={{ alignItems: "flex-start" }}>
                    <TextField
                      label="Name"
                      placeholder="e.g. eth0"
                      value={row.name}
                      onChange={(e) => setRow(row.id, { name: e.target.value })}
                      error={!!nameErr}
                      helperText={nameErr || " "}
                      size="small"
                      slotProps={{ htmlInput: { style: { fontFamily: "monospace" } } }}
                      sx={{ flex: 1 }}
                    />
                    <TextField
                      label="MAC"
                      placeholder="aa:bb:cc:dd:ee:ff"
                      value={row.mac}
                      onChange={(e) => setRow(row.id, { mac: e.target.value })}
                      error={!!macErr}
                      helperText={macErr || " "}
                      size="small"
                      slotProps={{ htmlInput: { style: { fontFamily: "monospace" } } }}
                      sx={{ flex: 2 }}
                    />
                    <IconButton
                      aria-label="Remove interface row"
                      onClick={() => removeRow(row.id)}
                      disabled={rows.length <= 1}
                      size="small"
                      sx={{ mt: 0.5 }}
                    >
                      <Trash2 size={16} />
                    </IconButton>
                  </Stack>
                );
              })}
            </Stack>
            <Box sx={{ mt: 1 }}>
              <Button
                startIcon={<Plus size={14} />}
                onClick={addRow}
                size="small"
                color="inherit"
              >
                Add interface
              </Button>
            </Box>
          </Box>

          <FormControlLabel
            control={
              <Checkbox
                checked={dryRun}
                onChange={(e) => setDryRun(e.target.checked)}
                size="small"
              />
            }
            label="Dry run (print the plan, do not commit changes)"
          />
        </Stack>
      </DialogContent>
      <DialogActions sx={{ px: 3, py: 2 }}>
        <Button onClick={handleClose} color="inherit" disabled={submitting}>
          Cancel
        </Button>
        <Button
          onClick={handleRun}
          variant="contained"
          color="primary"
          disabled={!canSubmit}
        >
          {dryRun ? "Run (dry-run)" : "Run"}
        </Button>
      </DialogActions>
    </Dialog>
  );
}
